#!/usr/bin/env bash
# WHY. `redo.sh` is the only lever that wipes the reconstruction, and it does
# more than a t2a restart: `continue.sh --keep-augmented` resets the workspace
# to the task0c tag and re-runs strip, task1 (FR synthesis, model calls) and
# the t1.5 bake, which re-authors the layout — so a run launched through it
# answers to a different `.skeleton/<cluster>` than the run before, and the
# two are not comparable. `restart_from.sh task2a` is the opposite: it keeps
# every fill (the cluster seed copies the skeleton only `if not dst.exists()`).
# The gap between them — a fresh stub tree under the SAME layout, the same
# generated tests and the same FRs, re-entering the TDD loop at iteration 0 —
# had no owner; it was asked for on 2026-09-07 to measure a batch of loop
# fixes from a clean tree without re-spending t1.
#
# USAGE.  bash scripts/redo_from_t2a.sh [--force]
#   Stops a running pipeline (gracefully; `--force` SIGKILLs), removes
#   tmp/anyhow/.skeleton-filled (the filled trees) and tmp/anyhow/.best_tree
#   (heal's durable restore tier — left in place it would restore OLD filled
#   files into the fresh tree the first time heal fires), then execs
#   `restart_from.sh task2a`, which clears the task2a..task2-4 state lines and
#   the `passing-*` lock tags and launches `continue.sh --y --no-tail` behind
#   the pre-run gate. Kept on purpose: `.skeleton/<cluster>` (the layout the
#   bodies answer to), `tests/*_gen_*.rs` and `src/*.gen_tests_*.rs` (t0c),
#   the FRs (t1), `.phase_c_logs/retrospectives.json` (its records carry tree
#   premises and are retired on read when the premise moved), and the body
#   and route ledgers under `logs/` (designed to survive a workspace wipe: a
#   body proven under this layout and this oracle is re-seeded at cluster
#   entry and the entry line says how many — `re-seeded N unit(s)`). The
#   per-cluster floors in `.skeleton/_tdd_floor_*.json` are keyed on
#   `tree_sha` and reconciled by the driver at cluster entry. The wiped trees
#   are tracked in the workspace repo and stay reachable by commit sha.
#
# HOW TO INTERPRET THE OUTPUT. The script prints what it stops and removes,
# then the launcher's lines. The reading that confirms it did what it says is
# the first cluster-entry sweep in logs/pipeline_anyhow.log: `iter 0: cluster
# entry — re-seeded N unit(s)` (N is the ledger's contribution, 0 with a cold
# ledger) followed by `iter 1: cargo test … pass=P` with P at the stub tree's
# baseline for that cluster (every stub panics; only tests that need no
# reconstructed body pass), not at the previous run's score.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$SCRIPT_DIR/tmp/anyhow"
_FORCE=false
for _arg in "$@"; do [[ "$_arg" == "--force" ]] && _FORCE=true; done

if pgrep -f "[p]ipeline.py" > /dev/null 2>&1; then
    _pids="$(pgrep -f '[p]ipeline.py' | tr '\n' ' ')"
    if [[ "$_FORCE" == true ]]; then
        echo "[redo-from-t2a] force-killing pipeline (PID $_pids)"
        kill $(pgrep -f '[p]ipeline.py') 2>/dev/null || true; sleep 2
        pkill -9 -f "[p]ipeline.py" 2>/dev/null || true; sleep 1
    else
        echo "[redo-from-t2a] graceful-stopping pipeline (PID $_pids)"
        # shellcheck source=../lib/graceful_stop.sh
        source "$SCRIPT_DIR/lib/graceful_stop.sh"
        graceful_stop_pipeline 0
    fi
else
    echo "[redo-from-t2a] no pipeline running"
fi
for _d in .skeleton-filled .best_tree; do
    if [[ -d "$PROJECT_DIR/$_d" ]]; then
        echo "[redo-from-t2a] removing $PROJECT_DIR/$_d ($(du -sh "$PROJECT_DIR/$_d" | cut -f1))"
        rm -rf "$PROJECT_DIR/$_d"
    fi
done
echo "[redo-from-t2a] kept: .skeleton (layout), generated tests, FRs, retrospectives, body/route ledgers"
exec bash "$SCRIPT_DIR/restart_from.sh" task2a
