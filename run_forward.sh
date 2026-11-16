#!/bin/bash
# Compatibility entry. The stock receiver is now ./supervise.sh (singleton, backoff, hang).
# A deployed tree may have a locally modified copy of this file plus an untracked
# start_forward.sh. Do not overwrite those blindly; compare and keep local path/env
# changes, then point the local starter at supervise.sh.
#
# nohup/setsid is not restart persistence across a container stop when PID1 is tini.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec ./supervise.sh
