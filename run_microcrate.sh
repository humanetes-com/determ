#!/usr/bin/env bash
# Re-run the microcrate L3 smoke. Two modes:
#
#   bash run_microcrate.sh              FULL: rebuild extractor, fresh tmp/microcrate,
#                                       from scratch. Use after an extractor/skeletonise/
#                                       bake change (re-bakes with the new binary).
#   bash run_microcrate.sh --from=task3 FAST: keep tmp/microcrate (bake + fills), clear
#                                       phase keys from <stage> onward, resume. Use for
#                                       pure phase-c/task3 PYTHON changes.
#       stage ∈ task1 | task2 | task2a | task2b | task2c | task3 | task4
#
# Requires vLLM up on :8000 (bash docker_vllm.sh). Never touches tmp/anyhow.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(pwd)"

LOG="$SCRIPT_DIR/logs/pipeline_micro_$(date -u +%Y%m%d-%H%M%S).log"
mkdir -p "$SCRIPT_DIR/logs"
WS="$SCRIPT_DIR/tmp/microcrate"
BIN="$SCRIPT_DIR/tools/skeleton_extractor/target/release/skeleton-extractor"

# The smoke always runs PARALLEL (exported env wins over .env): the C10
# parallel-streams audit needs concurrent-call evidence, and a serial smoke
# would never exercise the per-thread stream / per-tree lock machinery.
export PIPELINE_PARALLELISM="${PIPELINE_PARALLELISM:-4}"

FROM=""; FORCE=false
for arg in "$@"; do
    case "$arg" in
        --from=*) FROM="${arg#--from=}" ;;
        --force)  FORCE=true ;;
        *) echo "unknown arg: $arg"; echo "usage: run_microcrate.sh [--from=<stage>] [--force]"; exit 2 ;;
    esac
done

# --- guard: never clobber an in-flight run (e.g. anyhow) ---------------------
if pgrep -f "[p]ython.*pipeline.py" > /dev/null 2>&1; then
    echo "A pipeline.py is already running (PID $(pgrep -f '[p]ython.*pipeline.py' | tr '\n' ' '))."
    echo "Stop it before running the microcrate smoke (it shares the process guard)."
    exit 1
fi

# --- preflight: vLLM must answer --------------------------------------------
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "ERROR: vLLM not reachable on :8000. Start it first:  bash docker_vllm.sh"
    exit 1
fi

if [[ -z "$FROM" ]]; then
    # ===================== FULL run =========================================
    echo "[full] rebuilding skeleton-extractor ..."
    ( cd tools/skeleton_extractor && cargo build --release 2>&1 | tail -2 )
    echo "[full] wiping tmp/microcrate for a from-scratch run ..."
    rm -rf "$WS"
    echo "[full] launching microcrate pipeline → $LOG"
    nohup .venv/bin/python pipeline.py --repo tests/fixtures/microcrate --audit > "$LOG" 2>&1 &
else
    # ===================== FAST --from run ==================================
    # shellcheck source=lib/restart_from_lib.sh
    source "$SCRIPT_DIR/lib/restart_from_lib.sh"
    if ! _KEYS_STR=$(restart_from_keys "$FROM"); then
        echo "invalid --from stage: $FROM"
        echo "  stage ∈ task1 | task2 | task2a | task2b | task2c | task3 | task4"
        echo "  earlier stages (extractor/skeletonise/bake) → full run (no --from)"
        exit 2
    fi
    read -ra _KEYS <<< "$_KEYS_STR"

    [[ -d "$WS" ]] || { echo "ERROR: no tmp/microcrate workspace — run a FULL run first."; exit 1; }

    # STALENESS GUARD: --from reuses the baked .skeleton. If the extractor binary
    # is NEWER than the bake, the bake is stale w.r.t. the binary → --from would
    # validate old artifacts. Refuse (Hard Rule 11 discipline: resume point must
    # be downstream of what changed). --force overrides.
    SKEL="$WS/.skeleton"
    if [[ "$FORCE" != true && -e "$BIN" && -d "$SKEL" ]]; then
        if [[ "$BIN" -nt "$SKEL" ]]; then
            echo "ERROR: extractor binary is newer than the baked skeleton ($SKEL)."
            echo "  The bake is stale relative to the binary — --from would test old artifacts."
            echo "  Do a FULL run (bash run_microcrate.sh) to re-bake, or --force to override."
            exit 1
        fi
    fi

    echo "[fast] --from=$FROM → clearing phase entries: ${_KEYS[*]}"
    restart_from_clear "$WS" "${_KEYS[@]}"
    # Clear POISONED per-fn exhaustion state. retrospectives.json compounds
    # "learning" by design, but after an infra fix (splice/fill/prompt) the prior
    # GENUINELY_HARD / >=2-retro verdicts are wrong — they'd skip the very fns we
    # want re-attempted. For a throwaway validation fixture, always start fns
    # fresh on --from. (restart_from.sh/anyhow deliberately KEEP these — real
    # reconstruction wants compounding learning.)
    if [[ -e "$WS/.phase_c_logs/retrospectives.json" ]] || compgen -G "$WS/.skeleton/_failures/*.txt" > /dev/null; then
        echo "[fast] clearing poisoned exhaustion state (retrospectives.json + _failures)"
        rm -f "$WS/.phase_c_logs/retrospectives.json"
        rm -f "$WS"/.skeleton/_failures/*.txt
    fi
    # When re-running a FILL stage (task2*), reset .skeleton-filled so phase-c
    # re-fills from the baked stubs. Phase-c only seeds a file into .skeleton-filled
    # `if not dst.exists()` (pipeline.py ~16050), so a completed workspace's filled
    # bodies persist → "0 stubs remaining" → the run no-ops in ~500ms. Deleting it
    # forces a true re-fill (the point of --from for validating a fill fix). For
    # task3/task4 (no fill re-run) we KEEP the fills and just re-test them.
    if printf '%s\n' "${_KEYS[@]}" | grep -qE 'task2[abc]'; then
        echo "[fast] resetting .skeleton-filled → phase-c re-fills from baked stubs"
        rm -rf "$WS/.skeleton-filled"
    fi
    echo "[fast] resuming microcrate pipeline → $LOG"
    nohup .venv/bin/python pipeline.py --project microcrate --audit > "$LOG" 2>&1 &
fi

PID=$!
echo "Pipeline started (PID $PID). Following the log until the run exits"
echo "(Ctrl-C stops the tail AND skips the audit gate — the run keeps going)."
echo "----------------------------------------------------------------------"
tail --pid="$PID" -f "$LOG" || true
wait "$PID" 2>/dev/null || true

# --- audit gate: the smoke is green ONLY if every invariant has evidence -----
# --strict: SKIP = FAIL (a check with nothing to verify is an alarm, not a
# pass — this is how the session-65 failures slipped past the auditor).
# Explicit exemptions (--allow-skip), each with its precondition the fixture
# cannot GUARANTEE — these checks still gate (PASS/FAIL) whenever the run does
# produce their evidence; only their absence is tolerated:
#   C1,C2,C3,C4  need ≥1 (C3: ≥2, C4: ≥3) task4 iterations — a run that
#                converges in iteration 1 (inline retry heals the behavior
#                traps) never engages task4
#   C6           needs ratchet activity — only long fix loops save/restore
#   C8           needs a task0c harness-fix attempt — only when the model
#                emits non-compiling tests, which cannot be forced
echo "======================================================================"
echo "FLOW AUDIT (strict — SKIP counts as FAIL):"
if .venv/bin/python tools/flow_audit.py --strict --log "$LOG" --project "$WS" \
        --allow-skip 'C1,C2,C3,C4,C6,C8'; then
    echo "SMOKE GREEN — every invariant verified with evidence."
else
    echo "SMOKE RED — fix the FAILs (or add an explicit --allow-skip) before any real run."
    exit 1
fi
