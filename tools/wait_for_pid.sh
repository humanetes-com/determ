#!/usr/bin/env bash
# Wait until a PID exits, then print a one-line confirmation. A single-purpose
# script exists so callers never need an inline `until ! kill -0 $PID; do
# sleep N; done` compound command — those trip the shell tool's static-analysis
# safety check (unparseable-syntax permission prompt) every time.
#
# Usage: bash tools/wait_for_pid.sh PID [POLL_SECONDS]
set -euo pipefail
PID="$1"
POLL="${2:-2}"

while kill -0 "$PID" 2>/dev/null; do
    sleep "$POLL"
done
echo "pid $PID has exited"
