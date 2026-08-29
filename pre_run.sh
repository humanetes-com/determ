#!/usr/bin/env bash
# The gate a run has to pass before `continue.sh` will start one.
#
# Three stages, in this order, because they answer different questions and each is
# unreadable while the one before it is red:
#
#   1. the extractor build — "is the binary the pipeline runs the one these sources
#      describe". The stamp covers the Rust sources; the pipeline executes a
#      compiled artefact, and only a build keeps the two from drifting apart.
#   2. check.sh — "is the code correct". Static gates, the pytest suite, the
#      coverage ratchet. A type error or a broken invariant fails here.
#   3. walks    — "does the loop's decision sequence go where the theory says".
#      Each walk replays a recorded pathological run through the real
#      `run_phase_c_tdd` with cargo and the model stubbed, and asserts on the
#      DECISION the fix was supposed to make reachable. A walk carries its own
#      negative control: the same walk under the pre-fix grading must reproduce
#      the bad outcome, or the walk is proving nothing.
#
# Why a gate and not a habit. Every gate here is cheap — seconds — and the thing
# it stands in front of is not: run `259637-36db9aad` (2026-08-07) spent nine
# hours, 148 landed fills and +5 tests inside a deadlock that a walk over its own
# first five iterations decides instantly. The discipline of "run the tests before
# launching" decays exactly when you are in a hurry, which is when the expensive
# runs get launched, so it is enforced by refusing the launch instead.
#
# On success this writes .pre_run_stamp: the content hash of the tree the gates
# actually ran against (lib/tree_stamp.sh). continue.sh recomputes that hash and
# refuses to launch when it has moved, so a pass cannot be inherited by an edit
# that came after it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=lib/tree_stamp.sh
source "$SCRIPT_DIR/lib/tree_stamp.sh"

_WALKS_ONLY=false
for _arg in "$@"; do
    [[ "$_arg" == "--walks-only" ]] && _WALKS_ONLY=true
done

echo "=============================================================="
echo " PRE-RUN GATE"
echo "=============================================================="

# --- stage 1: the binary the pipeline actually runs ------------------------ #
# The stamp hashes the extractor's Rust SOURCES and prunes target/, and nothing on
# the launch path ever rebuilt the binary — only setup.sh runs `cargo build
# --release`. So an extractor edit moved the stamp, check.sh compiled the extractor
# from source under the pinned gate toolchain and passed, this gate stamped, and the
# pipeline went on running a release binary from days earlier: one stamp, two
# behaviours, no signal.
#
# Measured 2026-08-29, run 955468-7b947bd0. target/release/skeleton-extractor was
# built 2026-08-24 21:31 while src/model.rs and src/enumerate.rs carried the
# 2026-08-28 16:48 change adding TypeRecord.mod_path and MethodRecord.file/mod_path.
# Enumerating the live tree with that binary returned StdError with no mod_path key
# at all, so type_nameable_from never walled and hidden_symbols_from_types returned
# empty; TYPES_IN_SCOPE kept advertising `ext_context` (declared in a private `mod
# ext` in another file) to src/error.rs fills, and the compiler kept answering
# error[E0603] `module ext is private` at impl attempt 4 and error[E0599] at
# attempt 6.
#
# Building here — rather than warning that the binary looks stale — is what makes
# the class unrepeatable: after a passing gate the binary is by construction the one
# these sources describe. A warning would just be the "build before launching"
# habit that the rest of this file exists to stop relying on.
if [[ "$_WALKS_ONLY" == true ]]; then
    echo ""
    echo "--- stage 1/3: extractor build ....... SKIPPED (--walks-only) ---"
else
    echo ""
    echo "--- stage 1/3: cargo build --release (the extractor binary the pipeline runs) ---"
    if ! (cd "$SCRIPT_DIR/tools/skeleton_extractor" && cargo build --release); then
        echo ""
        echo "PRE-RUN FAILED at stage 1 (extractor build). No stamp written; continue.sh will refuse."
        exit 1
    fi
fi

# --- stage 2: is the code correct ----------------------------------------- #
if [[ "$_WALKS_ONLY" == true ]]; then
    # For the fix-iterate loop only: a walk failure is the signal being chased,
    # and re-running the full static sweep between attempts costs more than it
    # catches. This path deliberately writes NO stamp — see the end of the file.
    echo ""
    echo "--- stage 2/3: check.sh ....... SKIPPED (--walks-only) ---"
else
    echo ""
    echo "--- stage 2/3: check.sh (static gates, pytest, coverage ratchet) ---"
    if ! bash "$SCRIPT_DIR/check.sh"; then
        echo ""
        echo "PRE-RUN FAILED at stage 2 (check.sh). No stamp written; continue.sh will refuse."
        # check.sh's pytest run collects the walks too — they are left in it so the
        # coverage they carry keeps counting against the ratchet. So a walk can fail
        # here, under a stage-2 heading; check.sh's own output names the test.
        exit 1
    fi
fi

# --- stage 3: does the loop decide what the theory says -------------------- #
echo ""
echo "--- stage 3/3: walks (recorded runs replayed through the real loop) ---"
set +e
"$SCRIPT_DIR/.venv/bin/python" -m pytest -m walk --timeout=120 --timeout-method=thread -q
_WALK_RC=$?
set -e

# pytest exits 5 for "no tests collected". That is not a pass and it is not a
# failure — it is an EMPTY gate, and the difference matters enough to say out
# loud rather than let a green line imply coverage that does not exist.
if [[ "$_WALK_RC" -eq 5 ]]; then
    echo ""
    echo "  WARNING  no walks are registered (pytest -m walk collected nothing)."
    echo "           Stage 3 vouched for NOTHING. Every control-loop pathology"
    echo "           this repo has hit so far is still un-gated."
elif [[ "$_WALK_RC" -ne 0 ]]; then
    echo ""
    echo "PRE-RUN FAILED at stage 3 (walks). No stamp written; continue.sh will refuse."
    echo "A walk failure means the loop's decisions diverged from the trajectory the"
    echo "fix predicted — read the walk's assertion, not the pass count."
    exit 1
fi

# --- the stamp ------------------------------------------------------------- #
# Only a FULL pass may vouch for a tree. --walks-only skipped stage 1, so it
# cannot claim the tree is launchable; it is a fast inner loop for iterating on a
# fix, and it leaves any existing stamp exactly as it found it.
if [[ "$_WALKS_ONLY" == true ]]; then
    echo ""
    echo "Walks pass. NO stamp written (--walks-only skipped the extractor build and check.sh)."
    echo "Run 'bash pre_run.sh' with no flags before launching."
    exit 0
fi

_STAMP="$(pipeline_tree_stamp "$SCRIPT_DIR")"
{
    echo "$_STAMP"
    echo "# gated $(date -Iseconds) on $(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo 'no-git')"
    echo "# walks: $([[ "$_WALK_RC" -eq 5 ]] && echo 'NONE REGISTERED' || echo 'pass')"
} > "$(pipeline_stamp_file "$SCRIPT_DIR")"

echo ""
echo "=============================================================="
echo " PRE-RUN PASSED — tree $_STAMP is cleared to launch."
echo "=============================================================="
