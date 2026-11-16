#!/bin/bash
# Optional outer check, for a cron line or a scheduled routine that actually runs.
# The supervisor itself restarts crashes and hangs. This script must not start a
# second poller, and it must not restart an expired login (heartbeat status
# auth_expired): that would poll getupdates again.
# It does nothing useful unless something executes it. nohup is not a substitute.
cd "$(dirname "$(readlink -f "$0")")" || exit 2
DATA="${BRIDGE_DATA_DIR:-.}"
here="$(pwd -P)"

supervisor_alive() {
  local p="$1"
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q 'receive_supervisor.py'
}

forward_here() {
  local p
  for p in $(pgrep -f '[f]orward.py' || true); do
    [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$here" ] && return 0
  done
  return 1
}

hb_status() {
  BRIDGE_DATA_DIR="$DATA" python3 -c '
import json, os
p = os.path.join(os.environ["BRIDGE_DATA_DIR"], "forward.heartbeat")
try:
    d = json.load(open(p))
    print(d.get("status") or "missing")
except Exception:
    print("missing")
'
}

status="$(hb_status)"
spid="$(cat "$DATA/supervisor.pid" 2>/dev/null || true)"
if [ "$status" = "auth_expired" ]; then
  echo "AUTH_EXPIRED pid=${spid:-none}"
  exit 0
fi
if supervisor_alive "$spid"; then
  echo "OK supervisor pid=$spid status=$status"
  exit 0
fi

nohup setsid ./supervise.sh >> "$DATA/supervisor.out" 2>&1 < /dev/null &
sleep 2
newpid="$(cat "$DATA/supervisor.pid" 2>/dev/null || true)"
status="$(hb_status)"
if [ "$status" = "auth_expired" ]; then
  echo "AUTH_EXPIRED pid=${newpid:-none}"
  exit 0
fi
if supervisor_alive "$newpid"; then
  echo "RESTARTED (supervisor absent) pid=$newpid"
  echo "$(date '+%F %T') watchdog started supervisor" >> "$DATA/forward.log"
  exit 0
fi
if forward_here; then
  echo "OK forward.py already polling; not starting a second receiver"
  exit 0
fi
echo "FAILED (supervisor absent) - see $DATA/supervisor.out"
exit 1
