#!/usr/bin/env bash
# L3-RECOVERY smoke. Runs the full real-model pipeline
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
FROM_STAGE=""
DROP_FILLS=false
for arg in "$@"; do
    case "$arg" in
        --complexity=*) COMPLEXITY="${arg#--complexity=}" ;;
        --from=*)       FROM_STAGE="${arg#--from=}" ;;
        --drop-fills)   DROP_FILLS=true ;;
        *) echo "unknown arg: $arg  (usage: run_hardcrate.sh [--complexity=N] [--from=STAGE [--drop-fills]])"
           echo "  --from=STAGE  resume the EXISTING tmp/hardcrate run from that step"
           echo "                (preserves fills); STAGE ∈ t1|t2|t2a|t2b|t2c|t3|t4"
           echo "  --drop-fills  with --from=t2|t2a: first remove what task 2a wrote"
           echo "                (filled tree, best tree, floor, ask/red/conviction"
           echo "                ledgers) and move logs/body_ledger/hardcrate aside,"
           echo "                so the fill starts on stubs"
           exit 2 ;;
    esac
done
# --drop-fills re-runs the per-cluster fill on stubs: only a t2/t2a resume
# does that (a later stage never refills; without --from the workspace is
# rebuilt anyway). Checked before the process and vLLM guards.
if [[ "$DROP_FILLS" == true ]]; then
    # shellcheck source=lib/restart_from_lib.sh
    source "$SCRIPT_DIR/lib/restart_from_lib.sh"
    if [[ -z "$FROM_STAGE" ]] || ! restart_from_drops_t2a "$FROM_STAGE"; then
        echo "ERROR: --drop-fills needs --from=t2 or --from=t2a (got '${FROM_STAGE:-no --from}')"
        exit 2
    fi
fi
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

if [[ -n "$FROM_STAGE" ]]; then
    # --from STAGE: resume the EXISTING workspace from that step — no wipe, no
    # crate re-assembly (that would overwrite the fills with fresh stubs). Clear
    # the chosen phase entries + passing tags via the shared lib, then resume
    # with --project (auto-detects the git workspace at tmp/hardcrate).
    if [[ ! -d "$WS" ]]; then
        echo "ERROR: no tmp/hardcrate workspace to resume — run without --from first."
        exit 1
    fi
    # shellcheck source=lib/restart_from_lib.sh
    source "$SCRIPT_DIR/lib/restart_from_lib.sh"
    if ! KEYS_STR=$(restart_from_keys "$FROM_STAGE"); then
        echo "ERROR: unknown stage '$FROM_STAGE' (t1|t2|t2a|t2b|t2c|t3|t4)"; exit 2
    fi
    read -ra KEYS <<< "$KEYS_STR"
    # --drop-fills: the same product drop restart_from.sh task2a applies to
    # anyhow. Without it a resume re-enters on the filled tree, and a tree
    # the last run left green closes task 2a on its first sweep with nothing
    # filled (run `3669371-9d5a7361`, 2026-11-06: 168/168, so a keep-gate
    # change could not be read on fill 1).
    # The proven-body ledger lives outside the workspace and re-seeds every
    # body it vouches for at cluster entry, so after the drop alone the fill
    # would open on the same proven tree. It is moved aside, not deleted: it
    # is the provenance cache, and the moved copy stays on disk.
    if [[ "$DROP_FILLS" == true ]]; then
        restart_from_drop_t2a_products "$WS"
        BODY_LEDGER="$SCRIPT_DIR/logs/body_ledger/hardcrate"
        if [[ -d "$BODY_LEDGER" ]]; then
            ASIDE="$BODY_LEDGER.set-aside-$(date -u +%Y%m%dT%H%M%SZ)"
            mv "$BODY_LEDGER" "$ASIDE"
            echo "[hard] body ledger moved aside → $ASIDE (no re-seed of proven bodies)"
        fi
    fi
    echo "[hard] resume from $FROM_STAGE → clearing phase entries: ${KEYS[*]}"
    restart_from_clear "$WS" "${KEYS[@]}"
    echo "[hard] resuming hardcrate pipeline (from $FROM_STAGE) → $LOG"
    nohup .venv/bin/python pipeline.py --project hardcrate --audit > "$LOG" 2>&1 &
    PID=$!
else
    echo "[hard] rebuilding skeleton-extractor ..."
    ( cd tools/skeleton_extractor && cargo build --release 2>&1 | tail -2 )
    echo "[hard] assembling complexity=$LEVEL crate → $STAGED"
    .venv/bin/python tools/hardcrate_select.py --crate "$STAGED" --from "$FIXTURE" --max-level "$LEVEL"
    echo "[hard] wiping tmp/hardcrate for a from-scratch recovery run ..."
    rm -rf "$WS"
    echo "[hard] launching hardcrate (complexity=$LEVEL) pipeline → $LOG"
    nohup .venv/bin/python pipeline.py --repo "$STAGED" --audit > "$LOG" 2>&1 &
    PID=$!
fi
echo "Pipeline started (PID $PID). Following the log until it exits (Ctrl-C only stops the tail)."
echo "----------------------------------------------------------------------"
tail --pid="$PID" -f "$LOG" || true
set +e; wait "$PID" 2>/dev/null; PIPE_RC=$?; set -e

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '-')"

# A killed pipeline is an ABORT, not a measurement. The ledger append and the
# ratchet used to run unconditionally after the pipeline exited, so a run
# stopped mid-pass wrote a durable `converged=RED recovered=0/5` row and a
# REGRESSION exit — four of the eleven rows appended on 2026-08-11 are such
# aborts (their logs end mid `[phase-c/per-fn]` with no completion marker and
# no Traceback). rc>=128 is death by signal.
if (( PIPE_RC >= 128 )); then
    printf '%s complexity=%s recovered=-/5 converged=ABORTED(sig%d) git=%s log=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LEVEL" "$((PIPE_RC - 128))" "$GIT_SHA" "$(basename "$LOG")" \
        | tee -a "$LEDGER"
    echo "ABORTED — the pipeline was killed before completing; no recovery"
    echo "measurement was taken and the ratchet is untouched."
    exit "$PIPE_RC"
fi

# --- recovery measurement (NOT a pass/fail gate) ----------------------------
echo "======================================================================"
echo "RECOVERY AUDIT (C1/C2/C3/C4/C6 should PASS — they SKIP on the microcrate):"
AUDIT_OUT="$(.venv/bin/python tools/flow_audit.py --log "$LOG" --project "$WS" || true)"
echo "$AUDIT_OUT"

# recovered = how many of the 5 recovery invariants have real PASS evidence.
# evaluated = how many produced ANY verdict (PASS or FAIL). A SKIP is "the
# run gave this invariant nothing to measure" — C1-C6 measure recovery FROM
# DAMAGE, and a run that converges on iteration 1 produces none, so all five
# SKIP precisely BECAUSE the run went well. 28 of the 33 ledger rows up to
# 2026-08-12 are recovered=0/5 for that reason, every one exiting 1 against a
# baseline a complexity-2 run set on 2026-06-17; the exit code carried no
# information for ~2 months.
RECOVERED=0
EVALUATED=0
for c in C1 C2 C3 C4 C6; do
    if printf '%s\n' "$AUDIT_OUT" | grep -qE "^${c} .*  PASS "; then
        RECOVERED=$((RECOVERED + 1))
        EVALUATED=$((EVALUATED + 1))
    elif printf '%s\n' "$AUDIT_OUT" | grep -qE "^${c} .*  FAIL "; then
        EVALUATED=$((EVALUATED + 1))
    fi
done

if grep -q "complete — all tests pass iter" "$LOG"; then
    ITERS="$(grep -oE 'complete — all tests pass iter [0-9]+' "$LOG" | grep -oE '[0-9]+' | tail -1)"
    CONVERGED="GREEN(iter${ITERS})"
else
    CONVERGED="RED"
fi
printf '%s complexity=%s recovered=%d/5 evaluated=%d/5 converged=%s git=%s log=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LEVEL" "$RECOVERED" "$EVALUATED" "$CONVERGED" "$GIT_SHA" "$(basename "$LOG")" \
    | tee -a "$LEDGER"

# --- ratchet: recovery capability may only go UP ----------------------------
# Per-complexity baseline: the legacy flat `recovered_audits` (4, set by a
# complexity-2 run that took three iterations) is deliberately not read — a
# high-water from an easier crate is not a capability claim about this one.
BASELINE="$(.venv/bin/python -c "
import json, sys
try:
    data = json.load(open('$RATCHET'))
except Exception:
    data = {}
print(int((data.get('per_complexity') or {}).get('$LEVEL', 0)))
" 2>/dev/null || echo 0)"
echo "----------------------------------------------------------------------"
if (( EVALUATED == 0 )); then
    echo "recovery ratchet: NOT EVALUATED — every invariant SKIPPED for want of"
    echo "evidence (the run produced no damage to recover from). Convergence:"
    echo "${CONVERGED}. Raising the traps is what re-arms this measurement."
    exit 0
fi
echo "recovery ratchet: this run ${RECOVERED}/5 (evaluated ${EVALUATED})   baseline ${BASELINE}/5 (complexity ${LEVEL})"
if (( RECOVERED > BASELINE )); then
    .venv/bin/python -c "
import json
try:
    data = json.load(open('$RATCHET'))
except Exception:
    data = {}
per = data.get('per_complexity') or {}
per['$LEVEL'] = $RECOVERED
json.dump({'per_complexity': per,
           '_comment': 'max C1/C2/C3/C4/C6 PASS per complexity level; '
                       'raise the traps once a level hits 5'},
          open('$RATCHET', 'w'), indent=2)
"
    echo "RATCHET UP → ${RECOVERED}/5 at complexity ${LEVEL} (commit $RATCHET). The pipeline got better."
elif (( RECOVERED == BASELINE )); then
    echo "HOLD — no regression. $(( 5 - RECOVERED )) recovery audit(s) still to unlock (= pipeline tasks)."
else
    echo "REGRESSION — recovery dropped ${BASELINE} → ${RECOVERED} at complexity ${LEVEL}. A capability we HAD is gone."
    exit 1
fi
