#!/usr/bin/env bash
# Static analysis — run before starting the pipeline to catch type errors.
# Focuses on return-type mismatches and tuple unpack errors, not full strictness.
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
echo "Done."
