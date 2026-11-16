#!/usr/bin/env python3
"""Singleton supervisor for the WeChat receiver.

The poller (forward.py) is a child. This process restarts it with a capped
backoff after a crash or a stalled poll, and it does not restart an expired
iLink login (ret/errcode -14). Alerts go out through the existing webhook,
once per episode. No new network service is introduced.

nohup is not restart persistence. install_autostart() only installs a unit
when this machine's PID1 is systemd, or a crontab line when cron is running.
"""
import fcntl, json, logging, os, signal, subprocess, sys, time

AUTH_EXIT = 14
LOCK_EXIT = 2
TRANSIENT_ALERT_AFTER = 5
HANG_S = 150

AUTH_TEXT = ("微信接收已停止：iLink 登录态失效（ret/errcode=-14）。需要重新扫码绑定后才能收消息。"
             "接收器不会反复重试。发送功能不受影响。")
TRANSIENT_TEXT = "微信接收连续多次网络失败，正在退避重试。发送功能不受影响。"
RECOVER_TEXT = "微信接收已恢复，轮询重新有进展。"
HUNG_TEXT = "微信接收轮询没有进展，已判定卡住并重启。发送功能不受影响。"
UNSTABLE_TEXT = "微信接收进程反复退出，已按上限放慢重启。请查看本机日志，不要再开第二个轮询。"
ALERT_TEXT = {"receiver_hung": HUNG_TEXT, "receiver_unstable": UNSTABLE_TEXT, "auth_expired": AUTH_TEXT,
              "receive_degraded": TRANSIENT_TEXT, "recovered": RECOVER_TEXT}

log = logging.getLogger("forward")


class Decision:
    def __init__(self, action, backoff, alert=None):
        self.action, self.backoff, self.alert = action, backoff, alert


def ilink_error_code(obj):
    """0 when both fields are absent or zero. -14 if either field is -14.

    Any other first non-zero value is returned as-is. Numeric strings count.
    """
    if not isinstance(obj, dict):
        return 0
    codes = []
    for key in ("ret", "errcode"):
        if key not in obj or obj[key] in (None, ""):
            continue
        try:
            codes.append(int(obj[key]))
        except (TypeError, ValueError):
            continue
    if -14 in codes:
        return -14
    for n in codes:
        if n != 0:
            return n
    return 0


def classify_poll(obj=None, exc=None):
    """ok | timeout | transient | auth. Timeout is a normal empty long-poll."""
    if exc is not None:
        if isinstance(exc, TimeoutError):
            return "timeout"
        return "transient"
    if ilink_error_code(obj) == -14:
        return "auth"
    if not isinstance(obj, dict) or ilink_error_code(obj) != 0:
        return "transient"
    return "ok"


def backoff_seconds(attempt, base=2, cap=60):
    """1-based consecutive failure count -> seconds, exponential and capped."""
    n = max(1, int(attempt))
    return min(int(cap), int(base) * (2 ** (n - 1)))


class RestartPolicy:
    """Crashes and hangs restart with a capped backoff. Auth stops."""

    def __init__(self, base=2, cap=60, alert_after=5):
        self.base, self.cap, self.alert_after = base, cap, alert_after
        self.attempt = 0
        self.alerted = False

    def decide(self, kind, now=None):
        if kind == "auth":
            return Decision("stop", 0, "auth_expired")
        self.attempt += 1
        delay = backoff_seconds(self.attempt, self.base, self.cap)
        alert = None
        if self.attempt >= self.alert_after and not self.alerted:
            alert = "receiver_hung" if kind == "hang" else "receiver_unstable"
        return Decision("restart", delay, alert)

    def note_alerted(self):
        """Call only after the alert POST succeeded. A failed POST must be retried."""
        self.alerted = True


def hang_verdict(hb, now, hang_s, child_pid, spawned_at):
    """Hang only on this child's own heartbeat.

    A file left by the previous pid is not evidence that the new process is stuck.
    The child has hang_s to write its own stamp. auth_expired on its own stamp is
    never a hang.
    """
    hb = hb or {}
    own = child_pid is not None and hb.get("pid") == child_pid
    if not own:
        if now - spawned_at > hang_s:
            return "hang"
        return "starting"
    return assess_heartbeat(hb, now, hang_s, True)


def assess_heartbeat(hb, now, hang_s, pid_alive):
    """ok | starting | hang | dead | auth_expired.

    auth_expired is never a hang: killing it would poll again and storm.
    A missing heartbeat while the pid is alive is 'starting', not a hang.
    """
    if not pid_alive:
        return "dead"
    if not hb:
        return "starting"
    if hb.get("status") == "auth_expired":
        return "auth_expired"
    try:
        updated = int(hb.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated = 0
    if now - updated > hang_s:
        return "hang"
    return "ok"


def atomic_json(path, obj, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def write_heartbeat(path, status, now=None, ret=None):
    doc = {"pid": os.getpid(), "status": status, "updated_at": int(time.time() if now is None else now)}
    if ret is not None:
        doc["last_ret"] = ret
    atomic_json(path, doc)


def read_heartbeat(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def acquire_receiver_lock(path):
    """Exclusive non-blocking lock. None if another receiver holds it.

    The returned file object must stay open. The fd is inheritable so a child
    poller keeps the singleton lock if this supervisor exits first.
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.set_inheritable(fd, True)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return os.fdopen(fd, "a+")


def adopt_or_lock(path):
    """Child path: keep WECHAT_LOCK_FD open. Otherwise take the lock ourselves."""
    raw = os.environ.get("WECHAT_LOCK_FD")
    if raw:
        fd = int(raw)
        os.set_inheritable(fd, True)
        return os.fdopen(fd, "a+")
    return acquire_receiver_lock(path)


def receiver_pid_matches(pid, code_dir):
    """True only for this checkout's forward.py / supervisor. Never a foreign inode."""
    try:
        cmd = open("/proc/%d/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
        cwd = os.path.realpath(os.readlink("/proc/%d/cwd" % pid))
    except OSError:
        return False
    if cwd != os.path.realpath(code_dir):
        return False
    return "forward.py" in cmd or "receive_supervisor.py" in cmd


def _recorded_pids(hb, lock_path):
    pids = []
    if hb and hb.get("pid") is not None:
        try:
            pids.append(int(hb["pid"]))
        except (TypeError, ValueError):
            pass
    try:
        text = open(os.path.join(os.path.dirname(lock_path), "supervisor.pid"), encoding="utf-8").read()
        pids.append(int(text.strip()))
    except (OSError, ValueError):
        pass
    return pids


def prepare_lock(path, read_heartbeat_fn, now, hang_s, kill, sleep, candidate_pids=None, is_ours=None, code_dir=None):
    """Take the singleton lock.

    A live or auth_expired holder is refused. A stale heartbeat may be replaced
    by stopping only pids this receiver recorded, after a cmdline and cwd check.
    """
    lock = acquire_receiver_lock(path)
    if lock is not None:
        return lock
    hb = read_heartbeat_fn()
    if assess_heartbeat(hb, now, hang_s, True) != "hang":
        return None
    pids = list(candidate_pids()) if candidate_pids else _recorded_pids(hb, path)
    mine = os.getpid()

    def ours(pid):
        if is_ours is not None:
            return bool(is_ours(pid))
        if not code_dir:
            return False
        return receiver_pid_matches(pid, code_dir)

    for pid in pids:
        if pid and pid != mine and ours(pid):
            kill(pid)
    for _ in range(25):
        sleep(0.2)
        lock = acquire_receiver_lock(path)
        if lock is not None:
            return lock
    return None


class AlertDeduper:
    """One webhook post per open failure episode. Recovery clears it."""

    def __init__(self, path, poster, clock=None, min_interval=900):
        self.path, self.poster = path, poster
        self.clock = clock or time.time
        self.min_interval = min_interval
        self.state = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc, dict):
                doc.setdefault("open", None)
                return doc
        except Exception:
            pass
        return {"open": None}

    def _save(self):
        atomic_json(self.path, self.state, mode=0o600)

    def _body(self, kind, text):
        return {"source": "wechat-clawbot", "alert": True, "notify_user": True,
                "kind": kind, "text": text, "dedupe_key": kind}

    def fail(self, kind, text):
        if self.state.get("open") == kind:
            return False
        now = self.clock()
        if (self.state.get("last_failed_kind") == kind and self.min_interval
                and now - (self.state.get("last_failed_at") or 0) < self.min_interval):
            return False
        try:
            self.poster(self._body(kind, text))
        except Exception as e:
            self.state["last_failed_kind"] = kind
            self.state["last_failed_at"] = now
            self._save()
            log.warning("alert %s not delivered: %s", kind, type(e).__name__)
            return False
        self.state["open"] = kind
        self.state["last_failed_kind"] = None
        self.state["last_failed_at"] = None
        self._save()
        return True

    def recover(self, text):
        if not self.state.get("open"):
            return False
        now = self.clock()
        if (self.state.get("last_failed_kind") == "recovered" and self.min_interval
                and now - (self.state.get("last_failed_at") or 0) < self.min_interval):
            return False
        try:
            self.poster(self._body("recovered", text))
        except Exception as e:
            self.state["last_failed_kind"] = "recovered"
            self.state["last_failed_at"] = now
            self._save()
            log.warning("recovery alert not delivered: %s", type(e).__name__)
            return False
        self.state["open"] = None
        self.state["last_failed_kind"] = None
        self.state["last_failed_at"] = None
        self._save()
        return True


def load_rules(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return []
    if isinstance(doc, dict):
        rules = doc.get("rules") or []
    elif isinstance(doc, list):
        rules = doc
    else:
        return []
    return [r for r in rules if isinstance(r, dict)]


def match_quarantine(payload, rules):
    """A rule matches only when every constraint it sets matches. Empty rules match nothing."""
    texts = "".join(payload.get("texts") or [])
    try:
        ms = int(payload.get("create_time_ms") or 0)
    except (TypeError, ValueError):
        ms = 0
    for rule in rules or []:
        keys = ("message_id", "text_contains", "create_time_ms_min", "create_time_ms_max")
        if not any(k in rule for k in keys):
            continue
        if "message_id" in rule and payload.get("message_id") != rule["message_id"]:
            continue
        if "text_contains" in rule and str(rule["text_contains"]) not in texts:
            continue
        if "create_time_ms_min" in rule and ms < int(rule["create_time_ms_min"]):
            continue
        if "create_time_ms_max" in rule and ms > int(rule["create_time_ms_max"]):
            continue
        return rule
    return None


def should_hold(payload, rules, backlog_before_ms=None):
    try:
        ms = int(payload.get("create_time_ms") or 0)
    except (TypeError, ValueError):
        ms = 0
    if backlog_before_ms is not None and (not ms or ms < int(backlog_before_ms)):
        return {"reason": "backlog"}
    return match_quarantine(payload, rules)


class QuarantineStore:
    """Held inbound messages. Release changes status; the payload stays for audit."""

    def __init__(self, path):
        self.path = path

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            return {"items": []}
        if not isinstance(doc, dict):
            return {"items": []}
        doc.setdefault("items", [])
        return doc

    def _save(self, doc):
        atomic_json(self.path, doc, mode=0o600)

    def items(self):
        return list(self._load()["items"])

    def hold(self, payload, rule):
        doc = self._load()
        mid = payload.get("message_id")
        for item in doc["items"]:
            if mid is not None and item.get("message_id") == mid:
                return False
        doc["items"].append({
            "message_id": mid, "status": "held",
            "reason": (rule or {}).get("reason") or "rule",
            "held_at": time.time(), "released_at": None, "payload": payload,
        })
        self._save(doc)
        return True

    def release(self, message_id):
        doc = self._load()
        for item in doc["items"]:
            if item.get("status") != "held":
                continue
            if item.get("message_id") != message_id and str(item.get("message_id")) != str(message_id):
                continue
            item["status"] = "released"
            item["released_at"] = time.time()
            self._save(doc)
            return item.get("payload")
        return None


def sweep_preexisting_pending(pending_path, store):
    """Move every message already in pending.json into the quarantine audit.

    Used only for the recovery start. The pending file is replaced with an empty
    document after the holds are on disk, so the flusher cannot post them.
    """
    try:
        with open(pending_path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return []
    msgs = [m for m in (doc.get("messages") or []) if isinstance(m, dict)]
    if not msgs:
        return []
    for payload in msgs:
        store.hold(payload, {"reason": "preexisting-pending"})
    atomic_json(pending_path, {"messages": [], "oldest": None, "last": None})
    return msgs


def route_payload(payload, rules, store, pending_add, backlog_before_ms=None):
    rule = should_hold(payload, rules, backlog_before_ms)
    if rule:
        if store is not None:
            store.hold(payload, rule)
        return "held"
    pending_add(payload)
    return "pending"


def commit_poll(outcome, persist, save_state):
    """Persist inbound messages before the getupdates cursor is written."""
    if outcome.get("msgs"):
        persist(outcome["msgs"])
    if outcome.get("save") and outcome.get("state") is not None:
        save_state(outcome["state"])


def hold_auth(sleep, beat, interval=30, cycles=None, retry_alert=None):
    """Stay up without calling getupdates. A restart loop must not see us exit.

    retry_alert is safe to call every interval: AlertDeduper suppresses repeats
    and retries a failed POST only after its own gap.
    """
    n = 0
    while cycles is None or n < cycles:
        beat("auth_expired")
        if retry_alert:
            try:
                retry_alert()
            except Exception:
                log.warning("auth alert retry failed")
        sleep(interval)
        n += 1


def serve(spawn, policy, alerter, read_heartbeat_fn, clock, sleep, hang_s=HANG_S,
          max_cycles=None, check_s=5, lock_path=None, stop_when=None):
    """Run spawn() children until auth exit, stop_when, or max_cycles crashes.

    lock_path is optional so tests can drive the loop without a lock. When set,
    a held lock returns LOCK_EXIT and does not spawn.
    """
    lock = None
    if lock_path is not None:
        lock = acquire_receiver_lock(lock_path)
        if lock is None:
            return LOCK_EXIT
    cycles = 0
    while True:
        if stop_when and stop_when():
            return 0
        spawned_at = clock()
        proc = spawn(lock)
        child_pid = getattr(proc, "pid", None)
        hung = False
        while proc.poll() is None:
            if stop_when and stop_when():
                proc.kill()
                return 0
            verdict = hang_verdict(read_heartbeat_fn(), clock(), hang_s, child_pid, spawned_at)
            if verdict == "hang":
                proc.kill()
                hung = True
                if proc.poll() is None and hasattr(proc, "wait"):
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                if proc.poll() is None:
                    proc.returncode = -9
                break
            sleep(check_s)
        code = proc.poll()
        if code is None:
            code = getattr(proc, "returncode", None)
        hb = read_heartbeat_fn() or {}
        # A child that already recorded auth_expired must not be spawned again:
        # its exit is the hold ending, not a crash. Match pid so a stale file
        # from a previous process does not block a new child that has not
        # written a heartbeat yet.
        same_child = hb.get("pid") is not None and hb.get("pid") == getattr(proc, "pid", None)
        if code == AUTH_EXIT or (hb.get("status") == "auth_expired" and same_child):
            alerter.fail("auth_expired", AUTH_TEXT)
            return AUTH_EXIT
        cycles += 1
        decision = policy.decide("hang" if hung else "crash", clock())
        if decision.alert and alerter.fail(decision.alert, ALERT_TEXT.get(decision.alert, decision.alert)):
            policy.note_alerted()
        if decision.action == "stop":
            return 1
        sleep(decision.backoff)
        if max_cycles is not None and cycles >= max_cycles:
            return 0


def detect_autostart(pid1, systemd_running, cron_running):
    if pid1 == "systemd" and systemd_running:
        mechanism = "systemd"
    elif cron_running:
        mechanism = "cron"
    else:
        mechanism = "runtime-only"
    return {"pid1": pid1, "systemd_running": bool(systemd_running), "cron_running": bool(cron_running),
            "mechanism": mechanism, "installed": False}


def runtime_only_message(pid1):
    return ("INSTALLED=no MECHANISM=runtime-only PID1=%s. "
            "systemd is not PID1 and cron is not running, so no service was installed. "
            "nohup/setsid only detaches the process; it is not restart persistence and will not "
            "come back after this container stops. Run ./supervise.sh in the foreground from the "
            "container entrypoint (or the existing long-lived session). An outer restart of the "
            "container helps only when that entrypoint starts supervise.sh again."
            % (pid1 or "unknown"))


def systemd_unit_text(repo_dir):
    repo_dir = os.path.abspath(repo_dir)
    exe = os.path.join(repo_dir, "supervise.sh")
    # Exit 14 (auth hold requested a stop) and exit 2 (another receiver holds the lock)
    # must not be restarted by systemd, or a dead login becomes a poll storm.
    return "\n".join([
        "[Unit]",
        "Description=grokbot-wechat receive supervisor",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=%s" % repo_dir,
        "ExecStart=%s" % exe,
        "Restart=on-failure",
        "RestartSec=5",
        "RestartPreventExitStatus=2 14",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def install_autostart(info, repo_dir=None, write_unit=None, enable_unit=None, install_cron=None):
    """Install only a mechanism this process can verify. Runtime-only returns exit 2."""
    if info.get("mechanism") == "runtime-only":
        return 2, runtime_only_message(info.get("pid1"))
    repo_dir = os.path.abspath(repo_dir or os.path.dirname(os.path.abspath(__file__)))
    if info.get("mechanism") == "systemd":
        if write_unit is None or enable_unit is None:
            return 2, "systemd detected but no installer callbacks were provided; nothing written"
        path = write_unit(systemd_unit_text(repo_dir))
        enabled = enable_unit(path)
        if enabled != "enabled":
            return 2, "systemd unit written but is-enabled returned %r" % (enabled,)
        return 0, "INSTALLED=systemd unit=%s enabled=yes" % path
    if info.get("mechanism") == "cron":
        if install_cron is None:
            return 2, "cron detected but no installer callback was provided; nothing written"
        install_cron(repo_dir)
        return 0, "INSTALLED=cron"
    return 2, runtime_only_message(info.get("pid1"))


def spawn_forward(lock, code_dir):
    env = os.environ.copy()
    cmd = [sys.executable, "-u", os.path.join(code_dir, "forward.py")]
    kwargs = {"cwd": code_dir, "env": env}
    if lock is not None:
        fd = lock.fileno()
        os.set_inheritable(fd, True)
        env["WECHAT_LOCK_FD"] = str(fd)
        kwargs["pass_fds"] = (fd,)
    return subprocess.Popen(cmd, **kwargs)


def _data_paths():
    import config
    data = config.DATA_DIR
    return data, config.CODE_DIR


def _write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def run_serve():
    import forward
    data, code = _data_paths()
    hb_path = os.path.join(data, "forward.heartbeat")
    alert_path = os.path.join(data, "alert_state.json")
    lock_path = os.path.join(data, "receiver.lock")
    _write_text(os.path.join(data, "supervisor.pid"), str(os.getpid()) + "\n")
    stop = {"v": False}

    def on_stop(signum, _frame):
        stop["v"] = True
        log.info("supervisor stop signal %s", signum)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    def poster(payload):
        result = forward.post_webhook(payload, tries=1)
        if not (isinstance(result, tuple) and 200 <= int(result[0]) < 300):
            raise RuntimeError("alert webhook failed")
        return result

    def kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    lock = prepare_lock(lock_path, lambda: read_heartbeat(hb_path), time.time(), HANG_S, kill, time.sleep,
                        code_dir=code)
    if lock is None:
        log.error("another receiver holds the lock and its heartbeat is not a hang; not starting")
        return LOCK_EXIT
    log.info("supervisor start pid=%s", os.getpid())
    # serve() also locks when given lock_path. We already hold it, so pass None and keep `lock` alive.
    return serve(lambda _held: spawn_forward(lock, code), RestartPolicy(),
                 AlertDeduper(alert_path, poster), lambda: read_heartbeat(hb_path),
                 time.time, time.sleep, stop_when=lambda: stop["v"])


def _quarantine_paths():
    data, _code = _data_paths()
    return data, os.path.join(data, "quarantine.json"), os.path.join(data, "receiver.lock")


def quarantine_list():
    _data, path, _lock = _quarantine_paths()
    items = QuarantineStore(path).items()
    if not items:
        print("quarantine empty (%s)" % path)
        return 0
    for item in items:
        payload = item.get("payload") or {}
        text = "".join(payload.get("texts") or [])
        print(json.dumps({
            "message_id": item.get("message_id"), "status": item.get("status"),
            "reason": item.get("reason"), "create_time_ms": payload.get("create_time_ms"),
            "text_preview": text[:80],
        }, ensure_ascii=False))
    return 0


def quarantine_release(message_id):
    data, path, lock_path = _quarantine_paths()
    probe = acquire_receiver_lock(lock_path)
    if probe is None:
        print("receiver is running; stop it before release so its pending buffer cannot overwrite the file")
        return 1
    try:
        store = QuarantineStore(path)
        payload = store.release(message_id)
        if payload is None:
            try:
                payload = store.release(int(message_id))
            except ValueError:
                payload = None
        if payload is None:
            print("no held message_id %s" % message_id)
            return 1
        import forward
        forward.Batcher(os.path.join(data, "pending.json")).add(payload)
        print("released message_id=%s into pending.json; audit row kept. start the receiver to flush it" % payload.get("message_id"))
        return 0
    finally:
        probe.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        return run_serve()
    if cmd == "quarantine-list":
        return quarantine_list()
    if cmd == "quarantine-release":
        if len(argv) != 2:
            print("usage: receive_supervisor.py quarantine-release MESSAGE_ID")
            return 2
        return quarantine_release(argv[1])
    print("usage: receive_supervisor.py [serve|quarantine-list|quarantine-release MESSAGE_ID]")
    return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
