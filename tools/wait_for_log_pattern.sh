#!/usr/bin/env bash
# Block until PATTERN appears in the CURRENT run segment, then exit.
# Silent by design: a poller's accumulated stdout is re-billed every time the
# autopilot reads it back, so print once at the end, never per iteration.
#
#   tools/wait_for_log_pattern.sh 'phase-c/file' [timeout_s] [poll_s]
#
# Exit 0 = matched (prints the matching lines), 1 = timed out, 2 = bad usage.
# Never put `python` + `pipeline.py` in this script's argv: run_microcrate.sh
# refuses to start while `pgrep -f "[p]ython.*pipeline.py"` matches, and a
# sleeping poller carrying that string in its command line looks like a live
# pipeline for its whole run.
set -u

PATTERN="${1:-}"
TIMEOUT_S="${2:-1800}"
POLL_S="${3:-20}"
[ -z "$PATTERN" ] && { echo "usage: $0 PATTERN [timeout_s] [poll_s]" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEADLINE=$(( $(date +%s) + TIMEOUT_S ))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    HITS="$(bash "$HERE/log_segment.sh" "$PATTERN" 2>/dev/null)"
    if [ -n "$HITS" ]; then
        printf '%s\n' "$HITS"
        exit 0
    fi
    sleep "$POLL_S"
done

echo "TIMEOUT after ${TIMEOUT_S}s — no match for: $PATTERN" >&2
exit 1
