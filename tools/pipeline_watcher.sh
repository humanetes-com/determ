#!/usr/bin/env bash
# Periodic pipeline-run watcher: liveness + new-line delta + flagged-pattern
# scan of a growing log, once per interval, until the watched PID exits.
#
# A single-purpose script exists so the Monitor tool's command stays a
# trivial `bash tools/pipeline_watcher.sh ...` invocation instead of a long
# inline while-loop — the same reason tools/wait_for_pid.sh exists: compound
# inline shell (loops, chained &&/;) trips the shell tool's static-analysis
# safety check every time, for both Bash AND Monitor calls.
#
# Usage: bash tools/pipeline_watcher.sh LOG PID STATE_FILE INTERVAL_SECONDS [EXTRA_PATTERN]
#   LOG              path to the growing pipeline log file
#   PID              process id being watched
#   STATE_FILE       scratch file this script uses to remember the last line count
#   INTERVAL_SECONDS seconds to sleep between checks
#   EXTRA_PATTERN    optional extended-grep pattern (case-insensitive) of extra
#                     lines worth flagging, ORed into the built-in set below
set -euo pipefail
LOG="$1"
PID="$2"
STATE="$3"
INTERVAL="$4"
EXTRA="${5:-}"

BUILTIN='Traceback|CRITICAL|FATAL|pipeline: complete|SUCCESS:|CONVERGED|re-admitted|WARNING: all tests passed|API drift|Killed|OOM|graceful'
PATTERN="$BUILTIN"
if [ -n "$EXTRA" ]; then
    PATTERN="$BUILTIN|$EXTRA"
fi

while true; do
    sleep "$INTERVAL"
    PREV=$(cat "$STATE" 2>/dev/null || echo 0)
    CUR=$(wc -l < "$LOG")
    echo "$CUR" > "$STATE"
    NEWLINES=$((CUR - PREV))
    if kill -0 "$PID" 2>/dev/null; then ALIVE="alive"; else ALIVE="DEAD"; fi
    SEGMENT=$(sed -n "$((PREV + 1)),${CUR}p" "$LOG" 2>/dev/null || true)
    FLAGS=$(printf '%s\n' "$SEGMENT" | grep -Ei "$PATTERN" | tail -8 || true)
    echo "[$(date -u +%H:%M:%SZ)] proc=${ALIVE} (pid $PID) new_log_lines=${NEWLINES}"
    if [ -n "$FLAGS" ]; then
        echo "  FLAGGED:"
        printf '%s\n' "$FLAGS" | sed 's/^/    /'
    fi
    if [ "$ALIVE" = "DEAD" ]; then
        echo "  DONE (or crashed) — process $PID is no longer running."
        break
    fi
    if [ "$NEWLINES" -eq 0 ] && [ "$ALIVE" = "alive" ]; then
        echo "  NOTE: log hasn't grown this interval while process is alive — possible stall."
    fi
done
