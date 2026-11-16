#!/usr/bin/env python3
"""Report or install receive-supervisor autostart. Never treats nohup as persistence.

--check   print JSON {pid1, mechanism, installed} and exit 0
--install install a systemd --user unit only when PID1 is systemd and systemctl
          is running, or a crontab line only when cron/crond is running, then
          re-read the enabled state. Otherwise print the runtime-only limit and
          exit 2 without writing a unit.
"""
import json, os, subprocess, sys
import receive_supervisor as guard

REPO = os.path.dirname(os.path.abspath(__file__))


def live_detect():
    try:
        pid1 = open("/proc/1/comm", encoding="utf-8").read().strip()
    except OSError:
        pid1 = ""
    systemd_running = False
    if pid1 == "systemd":
        try:
            probe = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True)
            systemd_running = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            systemd_running = False
    cron_running = False
    try:
        names = set(subprocess.check_output(["ps", "-eo", "comm"], text=True).split())
        cron_running = bool(names & {"cron", "crond"})
    except (OSError, subprocess.SubprocessError):
        cron_running = False
    return guard.detect_autostart(pid1, systemd_running, cron_running)


def _write_unit(text):
    dest_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "grokbot-wechat-receive.service")
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, dest)
    return dest


def _enable_unit(_path):
    subprocess.check_call(["systemctl", "--user", "daemon-reload"])
    subprocess.check_call(["systemctl", "--user", "enable", "--now", "grokbot-wechat-receive.service"])
    out = subprocess.check_output(
        ["systemctl", "--user", "is-enabled", "grokbot-wechat-receive.service"], text=True).strip()
    return out


def _install_cron(repo_dir):
    line = "*/5 * * * * %s/watchdog.sh" % repo_dir
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    body = current.stdout if current.returncode == 0 else ""
    if line not in body:
        body = body.rstrip() + "\n" + line + "\n"
        wrote = subprocess.run(["crontab", "-"], input=body, text=True)
        if wrote.returncode != 0:
            raise RuntimeError("crontab rejected the watchdog line")
    check = subprocess.check_output(["crontab", "-l"], text=True)
    if line not in check:
        raise RuntimeError("crontab did not list the watchdog line after install")


def install(info):
    if info.get("mechanism") == "systemd":
        return guard.install_autostart(info, repo_dir=REPO, write_unit=_write_unit, enable_unit=_enable_unit)
    if info.get("mechanism") == "cron":
        return guard.install_autostart(info, repo_dir=REPO, install_cron=_install_cron)
    return guard.install_autostart(info, repo_dir=REPO)


def main(argv):
    info = live_detect()
    if "--install" in argv:
        rc, message = install(info)
        print(message)
        return rc
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
