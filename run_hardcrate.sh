#!/usr/bin/env bash
# L3-RECOVERY smoke — CODE_ARCHITECTURE.md R10.7. Runs the full real-model pipeline
# against the HARDCRATE (hard-on-purpose traps) and measures how much of the
# FAILURE-RECOVERY loop the pipeline can drive: the flow-audit recovery invariants
# C1/C2/C3/C4/C6 should PASS (they SKIP on the happy-path microcrate).
#
# NON-GATING by design: a trap the pipeline can't yet recover is the NEXT pipeline
# task, not a build break. The ONLY hard failure is a REGRESSION — fewer recovery
# audits PASS than the committed `.hardcrate_ratchet.json` baseline (a capability we
# had is gone). Progress ratchets the baseline up; "win with any repo" = drive that
# number to 5 (then make the traps harder).
#
# Requires vLLM on :8000 (bash docker_vllm.sh). Never touches tmp/anyhow|microcrate.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(pwd)"

LOG="$SCRIPT_DIR/logs/pipeline_hard_$(date -u +%Y%m%d-%H%M%S).log"
LEDGER="$SCRIPT_DIR/logs/l3_hardcrate.ledger"
RATCHET="$SCRIPT_DIR/.hardcrate_ratchet.json"
WS="$SCRIPT_DIR/tmp/hardcrate"
mkdir -p "$SCRIPT_DIR/logs"

export PIPELINE_PARALLELISM="${PIPELINE_PARALLELISM:-4}"

# --complexity=N (default = max defined): assemble a crate with traps of level ≤ N.
# 0 = baseline (microcrate-equivalent), 1 = current traps, 2..10 = harder (future).
COMPLEXITY=""
for arg in "$@"; do
    case "$arg" in
        --complexity=*) COMPLEXITY="${arg#--complexity=}" ;;
        *) echo "unknown arg: $arg  (usage: run_hardcrate.sh [--complexity=N])"; exit 2 ;;
    esac
done
FIXTURE="$SCRIPT_DIR/tests/fixtures/hardcrate"
STAGED="$SCRIPT_DIR/tmp/hardcrate_src/hardcrate"   # --repo basename → workspace tmp/hardcrate
LEVEL="${COMPLEXITY:-$(.venv/bin/python -c 'import tools.hardcrate_select as h; print(h.max_level())')}"

# --- guards (shared process guard + vLLM health, like run_microcrate.sh) ----
if pgrep -f "[p]ython.*pipeline.py" > /dev/null 2>&1; then
    echo "A pipeline.py is already running — stop it before the hardcrate smoke."
    exit 1
fi
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "ERROR: vLLM not reachable on :8000. Start it first:  bash docker_vllm.sh"
    exit 1
fi

echo "[hard] rebuilding skeleton-extractor ..."
( cd tools/skeleton_extractor && cargo build --release 2>&1 | tail -2 )
echo "[hard] assembling complexity=$LEVEL crate → $STAGED"
.venv/bin/python tools/hardcrate_select.py --crate "$STAGED" --from "$FIXTURE" --max-level "$LEVEL"
echo "[hard] wiping tmp/hardcrate for a from-scratch recovery run ..."
rm -rf "$WS"
echo "[hard] launching hardcrate (complexity=$LEVEL) pipeline → $LOG"
nohup .venv/bin/python pipeline.py --repo "$STAGED" --audit > "$LOG" 2>&1 &
PID=$!
echo "Pipeline started (PID $PID). Following the log until it exits (Ctrl-C only stops the tail)."
echo "----------------------------------------------------------------------"
tail --pid="$PID" -f "$LOG" || true
wait "$PID" 2>/dev/null || true

# --- recovery measurement (NOT a pass/fail gate) ----------------------------
echo "======================================================================"
echo "RECOVERY AUDIT (C1/C2/C3/C4/C6 should PASS — they SKIP on the microcrate):"
AUDIT_OUT="$(.venv/bin/python tools/flow_audit.py --log "$LOG" --project "$WS" || true)"
echo "$AUDIT_OUT"

# recovered = how many of the 5 recovery invariants have real PASS evidence.
RECOVERED=0
for c in C1 C2 C3 C4 C6; do
    if printf '%s\n' "$AUDIT_OUT" | grep -qE "^${c} .*  PASS "; then
        RECOVERED=$((RECOVERED + 1))
    fi
done

if grep -q "complete — all tests pass iter" "$LOG"; then
    ITERS="$(grep -oE 'complete — all tests pass iter [0-9]+' "$LOG" | grep -oE '[0-9]+' | tail -1)"
    CONVERGED="GREEN(iter${ITERS})"
else
    CONVERGED="RED"
fi
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '-')"
printf '%s complexity=%s recovered=%d/5 converged=%s git=%s log=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LEVEL" "$RECOVERED" "$CONVERGED" "$GIT_SHA" "$(basename "$LOG")" \
    | tee -a "$LEDGER"

# --- ratchet: recovery capability may only go UP ----------------------------
BASELINE=0
[[ -f "$RATCHET" ]] && BASELINE="$(.venv/bin/python -c "import json,sys;print(json.load(open('$RATCHET')).get('recovered_audits',0))" 2>/dev/null || echo 0)"
echo "----------------------------------------------------------------------"
echo "recovery ratchet: this run ${RECOVERED}/5   baseline ${BASELINE}/5"
if (( RECOVERED > BASELINE )); then
    printf '{\n  "recovered_audits": %d,\n  "_comment": "max C1/C2/C3/C4/C6 PASS on the hardcrate; raise the traps once this hits 5"\n}\n' "$RECOVERED" > "$RATCHET"
    echo "RATCHET UP → ${RECOVERED}/5 (commit $RATCHET). The pipeline got better."
elif (( RECOVERED == BASELINE )); then
    echo "HOLD — no regression. $(( 5 - RECOVERED )) recovery audit(s) still to unlock (= pipeline tasks)."
else
    echo "REGRESSION — recovery dropped ${BASELINE} → ${RECOVERED}. A capability we HAD is gone."
    exit 1
fi
