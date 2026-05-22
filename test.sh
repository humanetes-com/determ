#!/usr/bin/env bash
# Full smoke test — runs check.sh plus the cargo-driven coverage measurement
# test on fixtures/tiny/. Use this before changing anything in the coverage
# provider code so coverage-measurement regressions get caught in ~30-60s
# instead of via a 30-minute anyhow run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

bash "$SCRIPT_DIR/check.sh"

echo ""
echo "Running cargo-driven coverage smoke test on fixtures/tiny/..."
"$SCRIPT_DIR/.venv/bin/python" tests/test_pipeline_smoke.py --full
