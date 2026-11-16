#!/bin/bash
# Read-only preflight for the production checkout. Does not start a receiver,
# does not open credentials.json or .env, and does not POST anywhere.
set -u
cd "$(dirname "$(readlink -f "$0")")/.." || exit 2
echo "preflight only: no process started, no credential file opened, no webhook called"
for f in credentials.json .env state.json pending.json outbox.json history.log forward_seen.json quarantine.json quarantine-rules.json; do
  if [ -e "$f" ]; then
    echo "PRESENT left untouched: $f"
  else
    echo "absent: $f"
  fi
done
if [ -e start_forward.sh ]; then
  echo "LOCAL starter exists: start_forward.sh — do not delete it; compare with supervise.sh"
fi
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git diff --quiet -- run_forward.sh 2>/dev/null; then
    echo "LOCAL MODIFICATION: run_forward.sh differs from HEAD. Do not overwrite it blindly."
  else
    echo "run_forward.sh matches HEAD"
  fi
  if git ls-files --others --exclude-standard -- start_forward.sh | grep -q .; then
    echo "UNTRACKED: start_forward.sh"
  fi
fi
echo "next: read deploy/RECEIVE_RECOVERY.md before copying anything or starting supervise.sh"
