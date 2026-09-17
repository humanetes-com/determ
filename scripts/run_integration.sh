#!/usr/bin/env bash
# run_integration.sh — the integration tests, cargo-backed ones included.
#
# WHY. Unit tests and walks feed the pipeline hand-written or recorded cargo
# output, so they keep passing when cargo, trybuild or rustc change what they
# print. The integration tests drive the real adapters (the pipeline's cargo
# runner, the syn extractor, the file readers) over small invented crates that
# reproduce one defect class each. The first one exists because run
# 603120-24aabfc5 (2026-09-16) spent a fill on six constructors that a
# trybuild test's nested, cfg-less build had put on the compile frontier of a
# tree that compiled; hardcrate never had that shape and could not see it.
#
# USAGE.
#   bash scripts/run_integration.sh            # every integration test
#   bash scripts/run_integration.sh -k pull    # extra args go to pytest
# No GPU, no vLLM, no model. Needs the skeleton extractor release binary
# (bash pre_run.sh builds it) and cargo with the dev-dependencies in the local
# registry cache (tests run cargo --offline). Target dirs are reused under
# $TMPDIR/determ-integration-target, so only the first run compiles trybuild.
# Not part of check.sh: the cargo-backed tests skip there (INTEGRATION unset).
# Run it after a Rust toolchain, cargo or trybuild update, and whenever a
# change touches how cargo output or source files are read.
#
# HOW TO INTERPRET.
#   N passed            every class still behaves as its fix assumed.
#   a test_premise_* failure   the external tool changed shape (the message
#                       says which); the fix built on that shape must be
#                       re-derived — do not edit the premise to match.
#   any other failure   the class is open again on the current code; treat it
#                       as a pipeline defect before the next launch.
#   skipped             INTEGRATION=1 did not reach pytest; run via this script.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
INTEGRATION=1 exec .venv/bin/python -m pytest -p no:cacheprovider \
    -m integration tests/integration "$@"
