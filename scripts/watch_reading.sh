#!/usr/bin/env bash
# WHY: a from-scratch reading (e.g. run 39786-fd38e72f, technical_debt.md
#   "Reading run ...") is read at moments the log announces: each t2a sweep,
#   a cluster ending, a HEALTH line, a phase change, the process exiting. The
#   existing waiters do not fit a re-armed in-session loop:
#   tools/wait_for_log_pattern.sh matches anywhere in the run segment, so a
#   re-arm fires at once on the line that woke the last one, and it does not
#   notice the pipeline dying; scripts/interactive_run_watch.sh wakes only on
#   the process exiting. This one wakes on the FIRST NEW matching line since
#   it was armed, on the process exiting, on a log that stopped growing, or on
#   its deadline, whichever comes first, and says which.
#
# USAGE: bash scripts/watch_reading.sh [LOG] [PATTERN]
#   LOG      default logs/pipeline_anyhow.log
#   PATTERN  extended regex; default: t2a sweep lines, cluster_failed, green /
#            floor closes, HEALTH, Traceback, the nightly cluster, TASK 2b/3/4
#   Env: DEADLINE (s, default 3600), POLL (s, default 30), STALE (s, default
#        2700: no log growth for this long is a stall alert).
#   Arm it with the Bash tool's run_in_background from the session that wants
#   the wake; re-arm after each wake. Zero GPU; never signals anything.
#
# HOW TO INTERPRET the first output line (exit code in brackets):
#   MATCH [0]          new matching line(s) follow; read them against the
#                      reading checklist
#   DEADLINE [1]       nothing matched for DEADLINE seconds; the last
#                      progress line follows (healthy if it is recent)
#   PIPELINE_GONE [3]  no pipeline process; the log tail follows
#   STALLED [4]        the log did not grow for STALE seconds while the
#                      process is alive: read the debug log
set -u

LOG="${1:-logs/pipeline_anyhow.log}"
PATTERN="${2:-\[phase-c/tdd\] iter [0-9]+: cargo test|cluster_failed|Cluster done|floor met|HEALTH ‼|Traceback|\[nightly\] \[phase-c/tdd\] iter 1:|TASK 2b|TASK 3|TASK 4}"
DEADLINE_S="${DEADLINE:-3600}"
POLL_S="${POLL:-30}"
STALE_S="${STALE:-2700}"

alive() { pgrep -f '[p]ipeline.py --project' >/dev/null; }
size() { stat -c %s "$LOG" 2>/dev/null || echo 0; }

start=$(date +%s)
offset=$(size)
last_growth=$start
last_size=$offset

while :; do
    now=$(date +%s)
    cur=$(size)
    if [ "$cur" -lt "$offset" ]; then offset=0; fi   # rotated
    if [ "$cur" -gt "$last_size" ]; then last_growth=$now; last_size=$cur; fi
    if [ "$cur" -gt "$offset" ]; then
        hits=$(tail -c +"$((offset + 1))" "$LOG" | grep -E -- "$PATTERN" | tail -20)
        if [ -n "$hits" ]; then
            echo "MATCH ($(date -u +%H:%M:%SZ))"
            printf '%s\n' "$hits"
            exit 0
        fi
        offset=$cur
    fi
    if ! alive; then
        echo "PIPELINE_GONE ($(date -u +%H:%M:%SZ))"
        tail -15 "$LOG"
        exit 3
    fi
    if [ $((now - last_growth)) -ge "$STALE_S" ]; then
        echo "STALLED: log unchanged for $((now - last_growth))s ($(date -u +%H:%M:%SZ))"
        tail -5 "$LOG"
        exit 4
    fi
    if [ $((now - start)) -ge "$DEADLINE_S" ]; then
        echo "DEADLINE: nothing matched in ${DEADLINE_S}s ($(date -u +%H:%M:%SZ)); last lines:"
        tail -3 "$LOG" | cut -c1-300
        exit 1
    fi
    sleep "$POLL_S"
done
