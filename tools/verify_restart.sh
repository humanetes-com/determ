#!/usr/bin/env bash
# One-shot post-restart verification: armed sha, resume-vs-clean, first iter.
set -u
LOG=logs/pipeline_anyhow.log
PID="${1:-}"
for _ in $(seq 1 30); do
    grep -q "PIPELINE STARTED" "$LOG" && break
    sleep 2
done
echo "=== armed line ==="
grep "PIPELINE STARTED" "$LOG" | tail -1
echo "=== HEAD ==="
git log --oneline -1
echo "=== process ==="
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    ps -o lstart=,etime= -p "$PID"
    ls -l /proc/"$PID"/fd/1
else
    pgrep -af "[p]ython.*pipeline.py" || echo "NOT RUNNING"
fi
echo "=== layout-sync / retrigger / iter so far ==="
grep -nE "layout-sync|could not be located|superseded|iter [0-9]+: cargo test rc|clean=True|--clean" "$LOG" | tail -12
