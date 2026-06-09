#!/usr/bin/env bash
# Rebuild the skeleton-extractor binary (picks up the prettyplease skeleton-format
# fix) and re-run the microcrate L3 smoke FROM SCRATCH. Validates that phase-c now
# converges WITHOUT leaning on the task3 inline-retry rescue.
#
# Requires vLLM up on :8000 (bash docker_vllm.sh). Does NOT touch the anyhow
# workspace — continue.sh resumes tmp/anyhow independently afterwards.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

LOG=/tmp/pipeline_micro.log

# --- guard: never clobber an in-flight run (e.g. anyhow) ---------------------
if pgrep -f "[p]ython.*pipeline.py" > /dev/null 2>&1; then
    echo "A pipeline.py is already running (PID $(pgrep -f '[p]ython.*pipeline.py' | tr '\n' ' '))."
    echo "Stop it before running the microcrate smoke (it shares the process guard)."
    exit 1
fi

# --- 1. rebuild the extractor (the fix lives here) --------------------------
echo "[1/4] rebuilding skeleton-extractor ..."
( cd tools/skeleton_extractor && cargo build --release 2>&1 | tail -2 )

# --- 2. preflight: vLLM must answer -----------------------------------------
echo "[2/4] checking vLLM on :8000 ..."
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "ERROR: vLLM not reachable on :8000. Start it first:  bash docker_vllm.sh"
    exit 1
fi

# --- 3. fresh workspace ------------------------------------------------------
echo "[3/4] wiping tmp/microcrate for a from-scratch run ..."
rm -rf tmp/microcrate

# --- 4. launch + follow ------------------------------------------------------
echo "[4/4] launching microcrate pipeline → $LOG"
nohup .venv/bin/python pipeline.py --repo tests/fixtures/microcrate > "$LOG" 2>&1 &
PID=$!
echo "Pipeline started (PID $PID). Following the log — Ctrl-C stops the TAIL, not the run."
echo "----------------------------------------------------------------------"
tail -f "$LOG"
