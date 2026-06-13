#!/usr/bin/env bash
# Static analysis + smoke tests — run before starting the pipeline to catch type errors,
# idiom regressions, extraction-wiring breaks, and gen_tests helper regressions.
# Tool config (ruff rule set, mypy strictness) lives in pyproject.toml; this script just
# runs the gates and FAILS on any finding. For the slower cargo coverage smoke, see test.sh.
#
# Parallel layout: the four static gates run concurrently (outputs buffered to temp
# files so the log stays readable), then pytest runs on all cores via xdist
# (--dist loadfile: tests in one file share a worker — file-local fixtures stay safe).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY_TARGETS=(pipeline.py monitor.py model_interact.py pipeline_core)

GATE_TMP="$(mktemp -d)"
trap 'rm -rf "$GATE_TMP"' EXIT

# Gating now — a type error fails the build. (A blind ruff autofix once renamed a loop var
# into a same-named Thread variable; only mypy caught the resulting clash. Hence: hard gate.)
"$SCRIPT_DIR/.venv/bin/mypy" "${PY_TARGETS[@]}" --no-error-summary \
    > "$GATE_TMP/mypy.out" 2>&1 &
PID_MYPY=$!
"$SCRIPT_DIR/.venv/bin/ruff" check "${PY_TARGETS[@]}" tools tests --no-cache --quiet \
    > "$GATE_TMP/ruff.out" 2>&1 &
PID_RUFF=$!
"$SCRIPT_DIR/.venv/bin/lint-imports" --config pyproject.toml \
    > "$GATE_TMP/layers.out" 2>&1 &
PID_LAYERS=$!
"$SCRIPT_DIR/.venv/bin/python" tools/check_extraction_invariants.py \
    > "$GATE_TMP/extraction.out" 2>&1 &
PID_EXTRACTION=$!
"$SCRIPT_DIR/.venv/bin/python" tools/loc_ratchet.py \
    > "$GATE_TMP/loc.out" 2>&1 &
PID_LOC=$!

FAILED=0
gate() {  # gate <name> <pid> <outfile>
    local name="$1" pid="$2" out="$3"
    if wait "$pid"; then
        echo "  OK  $name clean"
    else
        echo "  FAIL  $name:"
        cat "$out"
        FAILED=1
    fi
}

echo "Running static gates in parallel (mypy, ruff, import-linter, extraction invariants)..."
gate "mypy" "$PID_MYPY" "$GATE_TMP/mypy.out"
gate "ruff" "$PID_RUFF" "$GATE_TMP/ruff.out"
gate "layer contract (adapters > application > ports > domain)" "$PID_LAYERS" "$GATE_TMP/layers.out"
gate "extraction invariants" "$PID_EXTRACTION" "$GATE_TMP/extraction.out"
gate "LOC ratchet (god-files only shrink)" "$PID_LOC" "$GATE_TMP/loc.out"
[ "$FAILED" -eq 0 ] || exit 1

echo ""
echo "Running pytest (xdist, all cores) + coverage ratchet (cargo coverage tests skip unless SMOKE_FULL=1)..."
# Slice 13: coverage is gated on EVERYTHING under refactor — pipeline.py (the engine
# being decomposed) and all of pipeline_core (where refactored code lands) — via a
# per-target ratchet (.coverage_ratchet.json): floors initialize from the first run,
# auto-raise when new tests land, and FAIL any change that loses coverage.
# domain/ keeps its absolute 95% minimum inside the ratchet (MIN_FLOORS). The
# `pipeline_all` target (pipeline.py + pipeline_core combined) is relocation-invariant:
# moving covered code out of the pipeline.py god-file can't drop it (LOC ratchet
# enforces the shrinking instead).
"$SCRIPT_DIR/.venv/bin/python" -m pytest -n auto --dist loadfile \
    --cov=pipeline --cov=pipeline_core --cov-report=
"$SCRIPT_DIR/.venv/bin/python" tools/coverage_ratchet.py || { echo "coverage ratchet failed (see above)"; exit 1; }

echo ""
echo "Done."
