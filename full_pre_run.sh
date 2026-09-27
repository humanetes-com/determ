#!/usr/bin/env bash
# The slow gate: everything pre_run.sh checks, then the tests too slow or too
# tool-bound for it.
#
# WHY. pre_run.sh stays fast on purpose (seconds, plus one tiny cargo build for
# its gate stage), and check.sh skips every cargo-backed integration test. Those
# tests are the only ones whose inputs are what real tools print — cargo,
# libtest, rustc, the extractor — and a unit test's fixture is a capture someone
# wrote. So a change to how the pipeline measures could turn a decision off with
# every fast test green: test isolation (2026-09-19) rebuilt aborted binaries as
# completed sections, and the stale-refill probe and the schedule's unmeasurable
# demotion never fired again until run 118933-877c43bd (2026-09-27). The first
# run of this script's stage also found an integration test left stale for three
# days by the 2026-09-24 conversation refactor, because nothing ran it.
#
# USAGE.
#   bash full_pre_run.sh [pre_run.sh flags]
# Runs pre_run.sh with the given flags (it writes the stamp on success), then
# the whole integration suite, including the modules pre_run's gate stage
# does not cover (`scripts/run_integration.sh`, the existing launcher, minus
# the `gate` tests pre_run just ran). Needs cargo and the pinned toolchain;
# no GPU, no model, no vLLM. Offline (`--offline` cargo). A few minutes.
#
# HOW TO INTERPRET THE OUTPUT.
#   "FULL PRE-RUN PASSED"      — the stamp pre_run wrote stands; continue.sh
#                                will launch.
#   "FULL PRE-RUN FAILED at …" — pre_run failed (no stamp), or a slow stage
#                                failed and this script REMOVED the stamp
#                                pre_run wrote, so continue.sh refuses until
#                                both are green. The failing test's assertion
#                                names the reader or decision that no longer
#                                matches what the real tool reports.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=lib/tree_stamp.sh
source "$SCRIPT_DIR/lib/tree_stamp.sh"

echo "=============================================================="
echo " FULL PRE-RUN GATE — pre_run.sh, then the slow stages"
echo "=============================================================="

if ! bash "$SCRIPT_DIR/pre_run.sh" "$@"; then
    echo ""
    echo "FULL PRE-RUN FAILED at pre_run.sh (see above). No stamp."
    exit 1
fi

_revoke() {
    rm -f "$(pipeline_stamp_file "$SCRIPT_DIR")"
    echo ""
    echo "FULL PRE-RUN FAILED at $1. The stamp pre_run.sh wrote was REMOVED;"
    echo "continue.sh will refuse until full_pre_run.sh passes."
    exit 1
}

echo ""
echo "--- slow stage 1/1: integration suite (real cargo/rustc/extractor, no model) ---"
set +e
bash "$SCRIPT_DIR/scripts/run_integration.sh" -m "integration and not gate" \
    --timeout=600 --timeout-method=thread -q
_INT_RC=$?
set -e
if [[ "$_INT_RC" -eq 5 ]]; then
    _revoke "slow stage 1 (no integration test collected — the suite vouched for nothing)"
elif [[ "$_INT_RC" -ne 0 ]]; then
    _revoke "slow stage 1 (integration suite)"
fi

echo ""
echo "=============================================================="
echo " FULL PRE-RUN PASSED — pre_run.sh and every slow stage are green."
echo "=============================================================="
