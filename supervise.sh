#!/bin/bash
# Foreground receiver supervisor. Restarts a crashed or hung forward.py with a capped
# backoff, and refuses to run a second poller. Exit 14 means the iLink login is expired
# (the child stays in a no-poll hold; this parent keeps watching it). Exit 2 means
# another receiver already holds the lock.
#
# nohup/setsid of this script is not restart persistence. If PID1 is tini and neither
# systemd nor cron is running, nothing starts it again after the container or this
# session stops. Check with: python3 install_autostart.py --check
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec python3 -u receive_supervisor.py serve
