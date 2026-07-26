#!/usr/bin/env bash
# Block silently until the CURRENT run segment produces its first fill verdict
# (FILLED / KEPT / reverted / U2 test failed / compile failed), then print just
# the matching lines. Prints nothing while waiting — a chatty poller costs
# tokens every time its stdout is read back.
#
#   bash tools/wait_for_fill_verdict.sh [MAX_SECONDS]
#
# Must not carry "python" + "pipeline.py" in its argv: run_microcrate.sh treats
# such a process as a live pipeline and skips its cycle.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="logs/pipeline_anyhow.log"
DEADLINE=$(( $(date +%s) + ${1:-3600} ))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    hits=$(bash tools/log_segment.sh 'FILLED\|KEPT [0-9]\|reverted (\|U2 test failed\|compile failed (attempt' 2>/dev/null)
    if [ -n "$hits" ]; then
        printf '%s\n' "$hits"
        exit 0
    fi
    sleep 60
done
echo "TIMEOUT — no fill verdict in the current segment of $LOG"
exit 1
