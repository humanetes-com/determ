#!/usr/bin/env bash
# Periodic pipeline-run watcher: liveness + trend analysis of a growing log,
# once per interval, until the watched PID exits.
#
# Trend analysis exists because a keyword-only watcher (Traceback/SUCCESS/etc)
# reports "nothing to act on" for an entire stretch where the pipeline is
# technically alive and producing log lines but making NO real progress —
# a stalled fix loop, a stall-counter climbing toward its cap, or a retro-round
# reset batch that fired and paid off nothing all look identical to "healthy"
# under a pure crash/success grep. This script tracks pass-count and
# stall-counter across checks and raises an explicit ALERT the moment the
# trend itself is the problem, not just a crash.
#
# A single-purpose script exists so the Monitor tool's command stays a
# trivial `bash tools/pipeline_watcher.sh ...` invocation instead of a long
# inline while-loop — the same reason tools/wait_for_pid.sh exists: compound
# inline shell trips the shell tool's static-analysis safety check, for both
# Bash AND Monitor calls.
#
# Usage: bash tools/pipeline_watcher.sh LOG PID STATE_FILE INTERVAL_SECONDS [EXTRA_PATTERN] [STALE_CHECKS]
#   LOG              path to the growing pipeline log file
#   PID              process id being watched
#   STATE_FILE       scratch file this script uses to remember the last line count
#   INTERVAL_SECONDS seconds to sleep between checks
#   EXTRA_PATTERN    optional extended-grep pattern (case-insensitive) of extra
#                     lines worth flagging, ORed into the built-in set below
#   STALE_CHECKS     consecutive no-progress checks before raising the
#                     no-progress ALERT (default 3 — e.g. 45min at a 900s interval)
set -euo pipefail
LOG="$1"
PID="$2"
STATE="$3"
INTERVAL="$4"
EXTRA="${5:-}"
STALE_LIMIT="${6:-3}"
TREND_STATE="${STATE}.trend"

BUILTIN='Traceback|CRITICAL|FATAL|pipeline: complete|SUCCESS:|CONVERGED|WARNING: all tests passed|API drift|Killed|OOM|graceful'
PATTERN="$BUILTIN"
if [ -n "$EXTRA" ]; then
    PATTERN="$BUILTIN|$EXTRA"
fi

# Baseline on arm: without this, a fresh STATE file makes the first tick treat
# the ENTIRE log as new lines, flagging matches from PREVIOUS runs' segments as
# if they happened in this one (twice misread as live retrigger failures).
if [ ! -f "$STATE" ]; then
    wc -l < "$LOG" > "$STATE"
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

    LATEST_PASS_LINE=$(grep -o 'pass=[0-9]*[^;]*fail=[0-9]*' "$LOG" | tail -1 || true)
    CUR_PASS=$(printf '%s' "$LATEST_PASS_LINE" | grep -o 'pass=[0-9]*' | grep -o '[0-9]*' || true)
    LATEST_STALL=$(grep -o 'stall=[0-9]*/[0-9]*' "$LOG" | tail -1 || true)

    if [ -f "$TREND_STATE" ]; then
        read -r PREV_PASS STALE_COUNT PREV_STALL < "$TREND_STATE"
    else
        PREV_PASS=""
        STALE_COUNT=0
        PREV_STALL=""
    fi

    ALERTS=()
    if [ -n "$CUR_PASS" ] && [ "$CUR_PASS" = "$PREV_PASS" ]; then
        STALE_COUNT=$((STALE_COUNT + 1))
        if [ "$STALE_COUNT" -ge "$STALE_LIMIT" ]; then
            MINUTES=$((STALE_COUNT * INTERVAL / 60))
            ALERTS+=("NO PASS-COUNT PROGRESS for $STALE_COUNT consecutive check(s) (~${MINUTES}min) — still $LATEST_PASS_LINE")
        fi
    else
        STALE_COUNT=0
    fi
    if [ -n "$LATEST_STALL" ] && [ -n "$PREV_STALL" ] && [ "$LATEST_STALL" != "$PREV_STALL" ]; then
        OLD_N=$(printf '%s' "$PREV_STALL" | grep -o '[0-9]*' | head -1 || true)
        NEW_N=$(printf '%s' "$LATEST_STALL" | grep -o '[0-9]*' | head -1 || true)
        if [ -n "$OLD_N" ] && [ -n "$NEW_N" ] && [ "$NEW_N" -gt "$OLD_N" ]; then
            ALERTS+=("STALL COUNTER INCREASED: $PREV_STALL -> $LATEST_STALL")
        fi
    fi
    if printf '%s\n' "$SEGMENT" | grep -q 're-admitted' 2>/dev/null; then
        if printf '%s\n' "$SEGMENT" | grep -q 'improved=False' 2>/dev/null; then
            ALERTS+=("RESET BATCH HAD ZERO PAYOFF — a re-admit fired this interval and the following cargo test showed improved=False")
        else
            ALERTS+=("re-admit batch fired this interval (see FLAGGED lines) — watch next check for payoff")
        fi
    fi

    echo "$CUR_PASS $STALE_COUNT $LATEST_STALL" > "$TREND_STATE"

    echo "[$(date -u +%H:%M:%SZ)] proc=${ALIVE} (pid $PID) new_log_lines=${NEWLINES} latest=${LATEST_PASS_LINE:-none} ${LATEST_STALL:-}"
    # Deep-analysis detectors (poisoned notes, attribution futility, zero-payoff
    # burn, frozen counters, repeated heals, stall decision distance) — the
    # coded version of the manual investigation that found the 2026-07-10
    # plateau causes. Never fails the watch; findings become event lines.
    "$(dirname "$0")/../.venv/bin/python" "$(dirname "$0")/run_health.py" --brief 2>/dev/null || true
    if [ ${#ALERTS[@]} -gt 0 ]; then
        echo "  ALERT:"
        printf '  - %s\n' "${ALERTS[@]}"
    fi
    if [ -n "$FLAGS" ]; then
        echo "  FLAGGED:"
        printf '%s\n' "$FLAGS" | sed 's/^/    /'
    fi
    if [ "$ALIVE" = "DEAD" ]; then
        echo "  DONE (or crashed) — process $PID is no longer running."
        break
    fi
done
