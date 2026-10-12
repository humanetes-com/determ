#!/usr/bin/env bash
# restart_from.sh <stage> [--force] [--keep-fills] — re-run from <stage> onward.
#
# Resumable boundaries (where the pipeline_state has a real phase entry):
#   task0c  → re-run the test augmentation and everything after it, KEEPING the
#             task0d behaviour harvest. Needs the original source back, so the
#             launch adds --restart-from-pre-augment: the pipeline resets the
#             workspace to the task0d-done-<module> tag (the commit after the
#             harvest), archives the reconstruction beside the workspace, and
#             re-runs task0c → strip → task1 → task2. A workspace without that
#             tag (its harvest predates it) resets to task0c-pre-<module> and
#             harvests once more, which writes the tag.
#             Refused here, before anything is stopped, when task0c-pre-* is absent.
#   task0d  → as task0c, plus the behaviour harvest (--reharvest-behaviours):
#             resets to task0c-pre-<module> (the `pipeline: task0b done` commit)
#             and deletes task0d-done-<module>. Only when task0d itself changed.
#   task1   → re-run FR synthesis + the whole reconstruct loop. Fills stay:
#             the seed copies a skeleton file only when the destination is missing.
#   task2 / task2a (t2, t2a) → re-run the per-cluster fill and everything after
#             it, and DROP what that phase wrote: .skeleton-filled, .best_tree,
#             the floors, the conviction store, the ask ledgers and the red
#             ledgers. The layout, the generated tests and the FRs stay, so the
#             loop seeds stubs under the same scaffold. This is the task0c
#             analogue: that stage restores the commit from before its outputs;
#             this stage has no such commit, so the outputs are removed.
#             --keep-fills skips the removal and re-enters on the bodies on disk.
#   task2b / task2c → skip the per-cluster fill. The filled trees stay.
#   task3 / task4   → keep every fill; straight to test / genesis.
#
# task0a/task0b and earlier still need redo.sh or a re-clone. redo.sh is the
# lever that also re-runs strip and task1 and re-authors the layout.
#
# Mechanics: deletes the chosen `[<key>...]` lines from pipeline_state.md so the
# phase is no longer "done", deletes passing-* tags (so task3 isn't seen locked),
# and relaunches via continue.sh --y --no-tail (resumes at the first unfinished
# phase). --no-tail is load-bearing: without it the launcher's epilogue is
# `tail -f`, and aborting that Cursor terminal SIGTERMs the process group
# (2026-08-29, run 929966-abe13d11, exit 143 after 54m).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/tmp/anyhow"

STAGE="${1:-}"
_FORCE=false
_KEEP_FILLS=false
for _arg in "$@"; do
    [[ "$_arg" == "--force" ]] && _FORCE=true
    [[ "$_arg" == "--keep-fills" ]] && _KEEP_FILLS=true
done
_STAGE="$STAGE"
[[ "$_STAGE" =~ ^t[0-9] ]] && _STAGE="task${_STAGE#t}"

# Stage→phase-key mapping lives in the shared lib (also used by run_microcrate.sh).
# shellcheck source=lib/restart_from_lib.sh
source "$SCRIPT_DIR/lib/restart_from_lib.sh"
# shellcheck source=lib/pipeline_pids.sh
source "$SCRIPT_DIR/lib/pipeline_pids.sh"
if ! _KEYS_STR=$(restart_from_keys "$STAGE"); then
    echo "usage: restart_from.sh <stage> [--force] [--keep-fills]"
    echo "  stage ∈ task0d | task0c | task1 | task2 | task2a | task2b | task2c | task3 | task4"
    echo "    task0d = re-run the behaviour harvest AND the test augmentation (resets to task0c-pre-<module>)"
    echo "    task0c = re-run the test augmentation, keeping the harvest (resets to task0d-done-<module>)"
    echo "    task2 / task2a = re-run the per-cluster fill from stubs (drops its products)"
    echo "    task2c = re-run only the merged-tree fill (skip per-cluster + merge; fills stay)"
    echo "    --keep-fills = with task2/task2a, re-enter on the bodies already on disk"
    echo "  earlier stages (task0a/task0b) → use redo.sh"
    exit 2
fi
read -ra _KEYS <<< "$_KEYS_STR"
echo "[restart-from] stage=$STAGE → clearing phase entries: ${_KEYS[*]}"

# task0c needs the pre-augmentation restore point. Check it BEFORE stopping the
# pipeline or clearing any state: a refusal must cost nothing, and a launch that
# aborts inside pipeline.py would leave the state keys cleared with nothing
# restored.
_LAUNCH_ARGS=(--y --no-tail)
if [[ "$_STAGE" == "task0c" || "$_STAGE" == "task0d" ]]; then
    if [[ -z "$(git -C "$PROJECT_DIR" tag -l 'task0c-pre-*' 2>/dev/null)" ]]; then
        echo "[restart-from] ERROR: no task0c-pre-* tag in $PROJECT_DIR." >&2
        echo "  The pipeline writes it before task0c on a git-managed run; a workspace" >&2
        echo "  built before that has none. Tag the 'pipeline: task0b done' commit by hand:" >&2
        echo "    git -C $PROJECT_DIR log --oneline | grep 'task0b done'" >&2
        echo "    git -C $PROJECT_DIR tag task0c-pre-<module> <sha>" >&2
        exit 2
    fi
    _LAUNCH_ARGS+=(--restart-from-pre-augment)
    [[ "$_STAGE" == "task0d" ]] && _LAUNCH_ARGS+=(--reharvest-behaviours)
fi

# 1. Stop any running pipeline.
if pipeline_running; then
    _pids="$(pipeline_pids | tr '\n' ' ')"
    if [[ "$_FORCE" == true ]]; then
        echo "[restart-from] force-killing pipeline (PID $_pids)"
        kill_pipeline
    else
        echo "[restart-from] graceful-stopping pipeline (PID $_pids)"
        # shellcheck source=lib/graceful_stop.sh
        source "$SCRIPT_DIR/lib/graceful_stop.sh"
        graceful_stop_pipeline 0
    fi
else
    echo "[restart-from] no pipeline running"
fi

# 2. task2/task2a: drop that phase's products so the seed copies stubs.
#    Not inside restart_from_clear — hardcrate --from and microcrate call
#    that and resume an existing workspace. --keep-fills is the re-entry
#    that leaves the bodies in place.
if restart_from_drops_t2a "$_STAGE" && [[ "$_KEEP_FILLS" != true ]]; then
    restart_from_drop_t2a_products "$PROJECT_DIR"
fi

# 3-4. Delete passing-* lock tags + clear the chosen phase entries (shared lib).
restart_from_clear "$PROJECT_DIR" "${_KEYS[@]}"

# 5. Relaunch — continue.sh resumes at the first unfinished phase (for task0c,
# after pipeline.py has reset the workspace to the pre-augmentation tag).
echo "[restart-from] launching continue.sh ${_LAUNCH_ARGS[*]}"
exec "$SCRIPT_DIR/continue.sh" "${_LAUNCH_ARGS[@]}"
