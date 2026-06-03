#!/usr/bin/env bash
# Static analysis + smoke tests — run before starting the pipeline to catch type errors,
# idiom regressions, extraction-wiring breaks, and gen_tests helper regressions.
# Tool config (ruff rule set, mypy strictness) lives in pyproject.toml; this script just
# runs the gates and FAILS on any finding. For the slower cargo coverage smoke, see test.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY_TARGETS=(pipeline.py monitor.py model_interact.py pipeline_core)

echo "Running mypy (config: pyproject.toml; pipeline_core held to strict-optional)..."
# Gating now — a type error fails the build. (A blind ruff autofix once renamed a loop var
# into a same-named Thread variable; only mypy caught the resulting clash. Hence: hard gate.)
"$SCRIPT_DIR/.venv/bin/mypy" "${PY_TARGETS[@]}" --no-error-summary \
    || { echo "mypy found type errors (see above)"; exit 1; }
echo "  OK  mypy clean"

echo ""
echo "Running ruff (config: pyproject.toml — F,I,UP,C4,B,SIM + select E codes)..."
"$SCRIPT_DIR/.venv/bin/ruff" check "${PY_TARGETS[@]}" tools --no-cache --quiet \
    || { echo "ruff found issues (see above)"; exit 1; }
echo "  OK  ruff clean"

echo ""
echo "Checking task-extraction invariants (deps fields declared==provided==consumed, no unbound names)..."
"$SCRIPT_DIR/.venv/bin/python" tools/check_extraction_invariants.py || exit 1

echo ""
echo "Running pytest + domain coverage floor (cargo coverage tests skip unless SMOKE_FULL=1)..."
# Gate the DOMAIN layer's coverage — it is the stable, OSS-facing contract and currently
# ~98%; the floor stops it regressing. The application/ engine is mid-migration and not gated
# here (its real coverage comes later via the fill-replay harness, backlog R4.3).
"$SCRIPT_DIR/.venv/bin/python" -m pytest \
    --cov=pipeline_core.domain --cov-report=term-missing:skip-covered --cov-fail-under=95

echo ""
echo "Done."
