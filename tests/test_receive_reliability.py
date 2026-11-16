"""Receiver supervisor, auth stop, backoff, quarantine, and pending durability. No network."""
import json, os, subprocess, sys, tempfile, textwrap, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "BRIDGE_DATA_DIR" not in os.environ:
    os.environ["BRIDGE_DATA_DIR"] = tempfile.mkdtemp(prefix="bridge-recv-")

import receive_supervisor as guard  # noqa: E402
import forward  # noqa: E402
import ilink  # noqa: E402


def msg(mid, text, ms):
    return {
        "message_type": 1,
        "message_id": mid,
        "from_user_id": "user@im.wechat",
        "create_time_ms": ms,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


class BackoffAndClassifyTest(unittest.TestCase):
    def test_transient_backoff_grows_then_caps_and_resets_by_attempt(self):
        self.assertEqual([guard.backoff_seconds(i) for i in (1, 2, 3, 4, 5, 6, 7)],
                         [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(guard.backoff_seconds(1), 2)

    def test_classify_auth_timeout_and_transient(self):
        self.assertEqual(guard.classify_poll({"ret": -14}), "auth")
        self.assertEqual(guard.classify_poll({"ret": 0, "errcode": -14}), "auth")
        self.assertEqual(guard.classify_poll({"errcode": "-14"}), "auth")
        self.assertEqual(guard.classify_poll({"ret": 0, "msgs": []}), "ok")
        self.assertEqual(guard.classify_poll({"ret": -1, "errcode": -1}), "transient")
        self.assertEqual(guard.classify_poll(exc=TimeoutError("long poll")), "timeout")
        self.assertEqual(guard.classify_poll(exc=OSError("network down")), "transient")


class RestartAndLockTest(unittest.TestCase):
    def test_crash_restarts_with_bounded_backoff_auth_does_not(self):
        policy = guard.RestartPolicy(base=2, cap=60, alert_after=3)
        first = policy.decide("crash", now=1000)
        second = policy.decide("crash", now=1005)
        third = policy.decide("hang", now=1010)
        self.assertEqual(first.action, "restart")
        self.assertEqual(first.backoff, 2)
        self.assertIsNone(first.alert)
        self.assertEqual(second.backoff, 4)
        self.assertEqual(third.action, "restart")
        self.assertEqual(third.alert, "receiver_hung")
        again = policy.decide("crash", now=1020)
        self.assertLessEqual(again.backoff, 60)
        self.assertIsNone(again.alert)
        auth = guard.RestartPolicy().decide("auth", now=1000)
        self.assertEqual((auth.action, auth.backoff, auth.alert), ("stop", 0, "auth_expired"))

    def test_serve_restarts_after_crash_and_stops_on_auth_exit(self):
        sleeps = []
        spawned = {"n": 0}

        def spawn_crash(_lock):
            spawned["n"] += 1
            return _Dead(1)

        alerter = guard.AlertDeduper(os.path.join(self._dir(), "a.json"), lambda p: None, clock=lambda: 0, min_interval=0)
        rc = guard.serve(spawn_crash, guard.RestartPolicy(), alerter, lambda: None, lambda: 1000,
                         sleeps.append, max_cycles=2)
        self.assertEqual(spawned["n"], 2)
        self.assertEqual(sleeps, [2, 4])
        self.assertEqual(rc, 0)
        sleeps.clear()
        alerts = []
        alerter = guard.AlertDeduper(os.path.join(self._dir(), "auth.json"), alerts.append, clock=lambda: 0, min_interval=0)
        rc = guard.serve(lambda _l: _Dead(14), guard.RestartPolicy(), alerter, lambda: None, lambda: 1000, sleeps.append)
        self.assertEqual(rc, guard.AUTH_EXIT)
        self.assertEqual(sleeps, [])
        self.assertEqual([a["kind"] for a in alerts], ["auth_expired"])

    def test_second_start_does_not_acquire_lock(self):
        path = os.path.join(self._dir(), "receiver.lock")
        held = guard.acquire_receiver_lock(path)
        self.assertIsNotNone(held)
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, %r)
            import receive_supervisor as guard
            raise SystemExit(0 if guard.acquire_receiver_lock(%r) is None else 3)
        """ % (ROOT, path))
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        spawned = {"n": 0}
        rc = guard.serve(lambda _l: spawned.__setitem__("n", spawned["n"] + 1), guard.RestartPolicy(),
                         guard.AlertDeduper(os.path.join(self._dir(), "b.json"), lambda p: None),
                         lambda: None, lambda: 0, lambda _s: None, lock_path=path)
        self.assertEqual(rc, guard.LOCK_EXIT)
        self.assertEqual(spawned["n"], 0)
        held.close()

    def test_stale_poll_heartbeat_is_hang_and_auth_is_not(self):
        self.assertEqual(guard.assess_heartbeat(
            {"status": "in_poll", "updated_at": 1000}, now=1151, hang_s=150, pid_alive=True), "hang")
        self.assertEqual(guard.assess_heartbeat(
            {"status": "in_poll", "updated_at": 1000}, now=1149, hang_s=150, pid_alive=True), "ok")
        self.assertEqual(guard.assess_heartbeat(
            {"status": "backing_off", "updated_at": 1000}, now=1060, hang_s=150, pid_alive=True), "ok")
        self.assertEqual(guard.assess_heartbeat(
            {"status": "auth_expired", "updated_at": 1}, now=99999, hang_s=150, pid_alive=True), "auth_expired")
        self.assertEqual(guard.assess_heartbeat(
            {"status": "in_poll", "updated_at": 1}, now=99999, hang_s=150, pid_alive=False), "dead")
        killed = {"n": 0}

        class Hung:
            def __init__(self):
                self.returncode = None
            def poll(self):
                return self.returncode
            def kill(self):
                killed["n"] += 1
                self.returncode = -9

        sleeps = []
        rc = guard.serve(lambda _l: Hung(), guard.RestartPolicy(alert_after=1),
                         guard.AlertDeduper(os.path.join(self._dir(), "h.json"), lambda p: None),
                         lambda: {"status": "in_poll", "updated_at": 0}, lambda: 1000,
                         sleeps.append, hang_s=150, max_cycles=1)
        self.assertEqual(rc, 0)
        self.assertEqual(killed["n"], 1)
        self.assertEqual(sleeps, [2])

    def test_live_auth_hold_is_not_killed_or_respawned(self):
        spawned = {"n": 0}
        killed = {"n": 0}

        class Alive:
            def poll(self):
                return None
            def kill(self):
                killed["n"] += 1

        sleeps = {"n": 0}

        def sleep(_s):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise StopIteration

        try:
            guard.serve(lambda _l: spawned.__setitem__("n", spawned["n"] + 1) or Alive(),
                        guard.RestartPolicy(),
                        guard.AlertDeduper(os.path.join(self._dir(), "auth-live.json"), lambda p: None),
                        lambda: {"status": "auth_expired", "updated_at": 1, "pid": 1},
                        lambda: 99999, sleep, hang_s=150)
        except StopIteration:
            pass
        self.assertEqual(spawned["n"], 1)
        self.assertEqual(killed["n"], 0)

    def _dir(self):
        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.mkdtemp(prefix="recv-")
        return self._tmp


class _Dead:
    def __init__(self, code):
        self.returncode = code
    def poll(self):
        return self.returncode
    def kill(self):
        self.returncode = -9


class AlertAndAuthTest(unittest.TestCase):
    def test_auth_alert_is_deduped_and_recovery_reopens(self):
        posted = []
        path = os.path.join(tempfile.mkdtemp(prefix="alert-"), "alert_state.json")
        clock = {"t": 1000.0}
        alerter = guard.AlertDeduper(path, posted.append, clock=lambda: clock["t"], min_interval=900)
        self.assertTrue(alerter.fail("auth_expired", guard.AUTH_TEXT))
        self.assertFalse(alerter.fail("auth_expired", guard.AUTH_TEXT))
        self.assertEqual(len(posted), 1)
        body = posted[0]
        self.assertEqual(body["source"], "wechat-clawbot")
        self.assertTrue(body["alert"])
        self.assertTrue(body["notify_user"])
        self.assertEqual(body["kind"], "auth_expired")
        blob = json.dumps(body)
        self.assertNotIn("bot_token", blob)
        self.assertNotIn("Bearer", blob)
        self.assertNotIn("texts", body)
        clock["t"] += 5
        self.assertTrue(alerter.recover(guard.RECOVER_TEXT))
        self.assertFalse(alerter.recover(guard.RECOVER_TEXT))
        self.assertEqual(posted[1]["kind"], "recovered")
        self.assertTrue(alerter.fail("auth_expired", guard.AUTH_TEXT))
        self.assertEqual(len(posted), 3)

    def test_auth_step_stops_without_sleep_and_second_alert_is_suppressed(self):
        tmp = tempfile.mkdtemp(prefix="auth-step-")
        hb = os.path.join(tmp, "heartbeat.json")
        posted = []
        sleeps = []
        alerter = guard.AlertDeduper(os.path.join(tmp, "a.json"), posted.append, clock=lambda: 1, min_interval=0)
        batcher = forward.Batcher(os.path.join(tmp, "pending.json"), quiet=3, cap=60,
                                   poster=lambda body: (200, "ok"), clock=lambda: 0,
                                   history_path=os.path.join(tmp, "h.log"))
        store = guard.QuarantineStore(os.path.join(tmp, "q.json"))
        action, fails = forward.step(batcher, store, alerter, 0, fetch=lambda: {"kind": "auth", "ret": -14},
                                      sleep=sleeps.append, heartbeat_path=hb, rules=[])
        self.assertEqual(action, "stop_auth")
        self.assertEqual(sleeps, [])
        self.assertEqual(json.load(open(hb))["status"], "auth_expired")
        action, _ = forward.step(batcher, store, alerter, fails, fetch=lambda: {"kind": "auth", "ret": -14},
                                  sleep=sleeps.append, heartbeat_path=hb, rules=[])
        self.assertEqual(action, "stop_auth")
        self.assertEqual(len(posted), 1)
        beats = []
        guard.hold_auth(sleeps.append, beats.append, interval=30, cycles=2)
        self.assertEqual(beats, ["auth_expired", "auth_expired"])
        self.assertEqual(sleeps, [30, 30])

    def test_transient_step_backs_off_alerts_once_then_recovers(self):
        tmp = tempfile.mkdtemp(prefix="tr-")
        posted = []
        sleeps = []
        alerter = guard.AlertDeduper(os.path.join(tmp, "a.json"), posted.append, clock=lambda: 1, min_interval=0)
        batcher = forward.Batcher(os.path.join(tmp, "pending.json"), quiet=3, cap=60,
                                   poster=lambda body: (200, "ok"), clock=lambda: 0,
                                   history_path=os.path.join(tmp, "h.log"))
        store = guard.QuarantineStore(os.path.join(tmp, "q.json"))
        fails = 0
        for _ in range(6):
            action, fails = forward.step(
                batcher, store, alerter, fails, fetch=lambda: {"kind": "transient", "error": "URLError"},
                sleep=sleeps.append, heartbeat_path=os.path.join(tmp, "hb.json"), rules=[])
            self.assertEqual(action, "backoff")
        self.assertEqual(sleeps, [2, 4, 8, 16, 32, 60])
        self.assertEqual([p["kind"] for p in posted], ["receive_degraded"])
        action, fails = forward.step(
            batcher, store, alerter, fails, fetch=lambda: {"kind": "timeout"},
            sleep=sleeps.append, heartbeat_path=os.path.join(tmp, "hb.json"), rules=[])
        self.assertEqual((action, fails), ("ok", 0))
        self.assertEqual(posted[-1]["kind"], "recovered")
        forward.step(batcher, store, alerter, 0, fetch=lambda: {"kind": "timeout"},
                     sleep=sleeps.append, heartbeat_path=os.path.join(tmp, "hb.json"), rules=[])
        self.assertEqual([p["kind"] for p in posted], ["receive_degraded", "recovered"])


class PendingAndQuarantineTest(unittest.TestCase):
    def test_pending_survives_reload_and_duplicate_is_not_posted(self):
        tmp = tempfile.mkdtemp(prefix="pend-")
        path = os.path.join(tmp, "pending.json")
        hist = os.path.join(tmp, "h.log")
        t = {"n": 1000.0}
        def clock():
            return t["n"]
        sent = []
        b = forward.Batcher(path, quiet=3, cap=60, poster=lambda body: sent.append(body) or (200, "ok"),
                            clock=clock, history_path=hist)
        payload = {"message_id": 7, "texts": ["keep me"], "create_time_ms": 7, "items": []}
        self.assertTrue(b.add(payload))
        self.assertFalse(b.add(dict(payload)))
        self.assertEqual(len(json.load(open(path))["messages"]), 1)
        b.poster = lambda body: None
        t["n"] += 5
        self.assertFalse(b.maybe_flush())
        b2 = forward.Batcher(path, quiet=3, cap=60, poster=lambda body: sent.append(body) or (200, "ok"),
                             clock=clock, history_path=hist)
        self.assertEqual([m["message_id"] for m in b2.msgs], [7])
        self.assertFalse(b2.add(dict(payload)))
        t["n"] += 5
        self.assertTrue(b2.maybe_flush())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["count"], 1)

    def test_quarantine_keeps_audit_and_forwards_other_messages(self):
        rules = [{
            "reason": "already-handled",
            "text_contains": "ALREADY-HANDLED",
            "create_time_ms_min": 1794808320000,
            "create_time_ms_max": 1794808440000,
        }]
        held = {"message_id": 9, "texts": ["please ALREADY-HANDLED this"], "create_time_ms": 1794808380000}
        other = {"message_id": 10, "texts": ["a different request"], "create_time_ms": 1794808385000}
        store = guard.QuarantineStore(os.path.join(tempfile.mkdtemp(prefix="q-"), "q.json"))
        pending = []
        self.assertEqual(guard.route_payload(held, rules, store, pending.append), "held")
        self.assertEqual(guard.route_payload(other, rules, store, pending.append), "pending")
        self.assertEqual(guard.route_payload(held, rules, store, pending.append), "held")
        self.assertEqual([p["message_id"] for p in pending], [10])
        items = store.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "held")
        self.assertEqual(items[0]["payload"]["texts"], held["texts"])
        self.assertEqual(store.release(9)["message_id"], 9)
        self.assertEqual(store.items()[0]["status"], "released")
        self.assertEqual(store.items()[0]["payload"]["texts"], held["texts"])

    def test_backlog_mode_holds_old_messages_only(self):
        store = guard.QuarantineStore(os.path.join(tempfile.mkdtemp(prefix="b-"), "q.json"))
        pending = []
        old = {"message_id": 1, "texts": ["old"], "create_time_ms": 100}
        live = {"message_id": 2, "texts": ["live"], "create_time_ms": 5000}
        self.assertEqual(guard.route_payload(old, [], store, pending.append, backlog_before_ms=1000), "held")
        self.assertEqual(guard.route_payload(live, [], store, pending.append, backlog_before_ms=1000), "pending")
        self.assertEqual(store.items()[0]["reason"], "backlog")
        self.assertEqual([p["message_id"] for p in pending], [2])

    def test_cursor_is_saved_only_after_pending_is_durable(self):
        tmp = tempfile.mkdtemp(prefix="cur-")
        pending_path = os.path.join(tmp, "pending.json")
        seen_path = os.path.join(tmp, "seen.json")
        order = []
        raw = msg(42, "hello", 1794808380000)
        outcome = {"kind": "ok", "msgs": [raw], "state": {"get_updates_buf": "cursor-2"}, "save": True}
        t = {"n": 1000.0}
        batcher = forward.Batcher(pending_path, quiet=3, cap=60, poster=lambda body: (200, "ok"),
                                   clock=lambda: t["n"], history_path=os.path.join(tmp, "h.log"))
        store = guard.QuarantineStore(os.path.join(tmp, "q.json"))
        posted = []
        alerter = guard.AlertDeduper(os.path.join(tmp, "a.json"), posted.append, clock=lambda: 1, min_interval=0)

        def save_state(st):
            self.assertEqual(json.load(open(pending_path))["messages"][0]["message_id"], 42)
            self.assertIn(42, json.load(open(seen_path)))
            order.append(("cursor", st["get_updates_buf"]))

        action, fails = forward.step(
            batcher, store, alerter, 0, fetch=lambda: outcome, sleep=lambda _s: None, save_state=save_state,
            seen_path=seen_path, rules=[], heartbeat_path=os.path.join(tmp, "hb.json"))
        self.assertEqual((action, fails), ("ok", 0))
        self.assertEqual(order, [("cursor", "cursor-2")])
        # same message again is idempotent: still one pending row, cursor save sees the existing row
        action, _ = forward.step(
            batcher, store, alerter, 0, fetch=lambda: outcome, sleep=lambda _s: None, save_state=save_state,
            seen_path=seen_path, rules=[], heartbeat_path=os.path.join(tmp, "hb.json"))
        self.assertEqual(len(json.load(open(pending_path))["messages"]), 1)
        self.assertEqual(len(order), 2)

    def test_fetch_auth_does_not_advance_cursor_or_touch_network_twice(self):
        calls = {"n": 0}
        saved = []
        orig_post, orig_load, orig_save = ilink.post, ilink.load_state, ilink.save_state
        try:
            def post(endpoint, body, timeout=15):
                calls["n"] += 1
                self.assertEqual(endpoint, "ilink/bot/getupdates")
                return json.dumps({"ret": -14, "errcode": -14, "errmsg": "session expired"})
            ilink.post = post
            ilink.load_state = lambda: {"get_updates_buf": "cursor-1"}
            ilink.save_state = lambda st: saved.append(st)
            outcome = forward.fetch()
        finally:
            ilink.post, ilink.load_state, ilink.save_state = orig_post, orig_load, orig_save
        self.assertEqual(outcome["kind"], "auth")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(saved, [])


class AutostartDetectTest(unittest.TestCase):
    def test_tini_without_cron_is_runtime_only(self):
        info = guard.detect_autostart(pid1="tini", systemd_running=False, cron_running=False)
        self.assertEqual(info["mechanism"], "runtime-only")
        self.assertFalse(info["installed"])
        rc, message = guard.install_autostart(info)
        self.assertEqual(rc, 2)
        self.assertIn("runtime-only", message)
        self.assertIn("nohup", message)
        unit = guard.systemd_unit_text("/opt/grokbot-wechat")
        self.assertIn("/opt/grokbot-wechat/supervise.sh", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("credentials", unit)
        self.assertNotIn("WECHAT_WEBHOOK", unit)


if __name__ == "__main__":
    unittest.main()
