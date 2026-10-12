#!/usr/bin/env bash
# WHY. `redo.sh` re-runs strip, task1 and the t1.5 bake, so the next fill
# answers to a new layout. A fresh stub tree under the SAME layout, the same
# generated tests and the same FRs is `restart_from.sh task2a`: that stage
# drops the per-cluster fill's products and re-seeds stubs. This name remains
# so a command written on 2026-09-07 still means that, and the removal stays
# in one place.
#
# USAGE.  bash scripts/redo_from_t2a.sh [--force]
#   Exec `restart_from.sh task2a` with the same arguments. That stops a running
#   pipeline (gracefully; `--force` SIGKILLs), removes tmp/anyhow/.skeleton-filled
#   and tmp/anyhow/.best_tree, the floors, the conviction store, the ask
#   ledgers and the red ledgers, clears the task2a..task2-4 state lines and
#   the passing-* lock tags, and launches continue.sh --y --no-tail behind the
#   pre-run gate. --keep-fills is refused: this script exists to drop the fill.
#   Kept: .skeleton/<cluster> (the layout), the generated tests, the FRs, and
#   the body and route ledgers under logs/ (a body proven under this layout
#   and this oracle is re-seeded at cluster entry, and the entry line says
#   how many). The removed trees stay reachable by commit sha.
#
# HOW TO INTERPRET THE OUTPUT. The launcher prints what it stops and removes.
# The reading is the first cluster entry in logs/pipeline_anyhow.log:
# `re-seeded N unit(s)` (N is the ledger, 0 with a cold ledger) followed by
# `iter 1: cargo test … pass=P` with P at the stub baseline plus those
# re-seeded bodies, not at the previous run's score.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for _arg in "$@"; do
    if [[ "$_arg" == "--keep-fills" ]]; then
        echo "[redo-from-t2a] --keep-fills contradicts this script; use restart_from.sh task2a --keep-fills" >&2
        exit 2
    fi
done
exec bash "$SCRIPT_DIR/restart_from.sh" task2a "$@"
