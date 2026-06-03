#!/usr/bin/env bash
# Static analysis + smoke tests — run before starting the pipeline to catch
# type errors and gen_tests helper regressions.
# Focuses on return-type mismatches and tuple unpack errors, not full strictness.
# For the slower cargo-driven coverage smoke test, see test.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Running mypy on pipeline.py..."

"$SCRIPT_DIR/.venv/bin/mypy" pipeline.py \
    --ignore-missing-imports \
    --warn-return-any \
    --check-untyped-defs \
    --no-strict-optional \
    --no-error-summary \
    2>&1 | grep -E "error:|warning:|Found [0-9]+ error" || true

echo ""
echo "Running mypy on monitor.py..."
"$SCRIPT_DIR/.venv/bin/mypy" monitor.py \
    --ignore-missing-imports \
    --warn-return-any \
    --check-untyped-defs \
    --no-strict-optional \
    --no-error-summary \
    2>&1 | grep -E "error:|warning:|Found [0-9]+ error" || true

echo ""
echo "Running mypy on pipeline_core/..."
"$SCRIPT_DIR/.venv/bin/mypy" pipeline_core \
    --ignore-missing-imports \
    --warn-return-any \
    --check-untyped-defs \
    --no-strict-optional \
    --no-error-summary \
    2>&1 | grep -E "error:|warning:|Found [0-9]+ error" || true

echo ""
echo "Running ruff (pyflakes bug rules: undefined names, unused imports/vars, redefinitions)..."
# F = pyflakes rule family (real bug classes). F541 (f-string without placeholder) is
# cosmetic-only and excluded; everything else in F is a genuine defect. Fail on any finding
# (engineering principle: fix all reported errors, including pre-existing).
"$SCRIPT_DIR/.venv/bin/ruff" check pipeline.py monitor.py pipeline_core tools \
    --select F --ignore F541 --no-cache --quiet || { echo "ruff found issues (see above)"; exit 1; }
echo "  OK  ruff F-rules clean"

echo ""
echo "Checking task-extraction invariants (deps fields declared==provided==consumed, no unbound names)..."
"$SCRIPT_DIR/.venv/bin/python" tools/check_extraction_invariants.py || exit 1

echo ""
echo "Running pure-Python smoke tests..."
"$SCRIPT_DIR/.venv/bin/python" tests/test_pipeline_smoke.py

echo ""
echo "Done."
