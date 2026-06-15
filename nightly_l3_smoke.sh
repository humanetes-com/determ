#!/usr/bin/env bash
# R10.5 — unattended L3 real-model convergence smoke + verdict ledger.
#
# Runs the full microcrate pipeline (real local model) from scratch and appends a
# one-line verdict to logs/l3_nightly.ledger so convergence health is trend-tracked
# across nights without re-reading whole logs. Exits 0 on SMOKE GREEN, non-zero on
# RED / setup failure — so any LOCAL scheduler can gate on it.
#
# This is the L3 nightly wiring. It is LOCAL ONLY by design: L3 needs the GPU box's
# live model (vLLM on :8000), which no cloud CI runner has, and the project forbids
# cloud/remote schedule routines. Wire it with the user's own crontab, e.g. 03:30 daily:
#
#     30 3 * * *  cd /home/user/determ && bash nightly_l3_smoke.sh >> logs/l3_nightly.out 2>&1
#
# It self-skips (exit 0, ledger SKIP) when vLLM is down or another pipeline.py is
# in flight, so a scheduled invocation never clobbers a real run or false-alarms.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
LEDGER="logs/l3_nightly.ledger"
mkdir -p logs
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GIT="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"

ledger() {  # <verdict> <extra>
    printf '%s  %-5s  git=%s  %s\n' "$NOW" "$1" "$GIT" "${2:-}" >> "$LEDGER"
}

# Self-skip rather than fail when the environment isn't ready for an L3 run.
if pgrep -f "[p]ython.*pipeline.py" > /dev/null 2>&1; then
    ledger SKIP "reason=pipeline_already_running"
    echo "[l3-nightly] a pipeline.py is already running — skipping."
    exit 0
fi
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    ledger SKIP "reason=vllm_down"
    echo "[l3-nightly] vLLM not reachable on :8000 — skipping (start: bash docker_vllm.sh)."
    exit 0
fi

START=$(date +%s)
bash run_microcrate.sh
RC=$?
DUR=$(( $(date +%s) - START ))
LOG="$(ls -t logs/pipeline_micro_*.log 2>/dev/null | head -1)"

if [[ $RC -eq 0 ]]; then
    ledger GREEN "dur=${DUR}s log=${LOG##*/}"
else
    ledger RED   "dur=${DUR}s rc=$RC log=${LOG##*/}"
fi
exit $RC
