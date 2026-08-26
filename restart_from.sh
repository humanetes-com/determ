#!/usr/bin/env bash
# restart_from.sh <stage> — re-run the pipeline from <stage> onward, preserving
# all existing fills in .skeleton-filled/.
#
# Resumable boundaries (where the pipeline_state has a real phase entry):
#   task0c  → re-run the test augmentation and everything after it. Needs the
#             original source back, so the launch adds --restart-from-pre-augment:
#             the pipeline resets the workspace to the task0c-pre-<module> tag
#             (the `pipeline: task0b done` commit), archives the reconstruction
#             beside the workspace, and re-runs task0c → strip → task1 → task2.
#             Refused here, before anything is stopped, when that tag is absent.
#   task1   → re-run FR synthesis + the whole reconstruct loop
#   task2   → re-run the reconstruct loop (task2a per-cluster → task2b merge →
#             task2c merged-fill → task3 test → task4 genesis)
#
# task2a/task2b/task2c/task3/task4 are accepted as aliases for task2 — they are
# sub-stages of the single `task2-4` reconstruct phase (atomic within one
# generate() call / loop), so restarting any of them re-runs the loop. They
# exist as VISIBILITY labels in the monitor's phase bar, not as separate
# resumable phases. task0a/task0b and earlier still need redo.sh or a re-clone.
#
# Mechanics: deletes the chosen `[<key>...]` lines from pipeline_state.md so the
# phase is no longer "done", deletes passing-* tags (so task3 isn't seen locked),
# and relaunches via continue.sh --y (resumes at the first unfinished phase).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/tmp/anyhow"

STAGE="${1:-}"
_FORCE=false
for _arg in "$@"; do [[ "$_arg" == "--force" ]] && _FORCE=true; done

# Stage→phase-key mapping lives in the shared lib (also used by run_microcrate.sh).
# shellcheck source=lib/restart_from_lib.sh
source "$SCRIPT_DIR/lib/restart_from_lib.sh"
# shellcheck source=lib/pipeline_pids.sh
source "$SCRIPT_DIR/lib/pipeline_pids.sh"
if ! _KEYS_STR=$(restart_from_keys "$STAGE"); then
    echo "usage: restart_from.sh <stage> [--force]"
    echo "  stage ∈ task0c | task1 | task2 | task2a | task2b | task2c | task3 | task4"
    echo "    task0c = re-run the test augmentation (resets to the task0c-pre-<module> tag)"
    echo "    task2c = re-run only the merged-tree fill (skip per-cluster + merge)"
    echo "  earlier stages (task0a/task0b) → use redo.sh"
    exit 2
fi
read -ra _KEYS <<< "$_KEYS_STR"
echo "[restart-from] stage=$STAGE → clearing phase entries: ${_KEYS[*]}"

# task0c needs the pre-augmentation restore point. Check it BEFORE stopping the
# pipeline or clearing any state: a refusal must cost nothing, and a launch that
# aborts inside pipeline.py would leave the state keys cleared with nothing
# restored.
_LAUNCH_ARGS=(--y)
if [[ "$STAGE" == "task0c" || "$STAGE" == "t0c" ]]; then
    if [[ -z "$(git -C "$PROJECT_DIR" tag -l 'task0c-pre-*' 2>/dev/null)" ]]; then
        echo "[restart-from] ERROR: no task0c-pre-* tag in $PROJECT_DIR." >&2
        echo "  The pipeline writes it before task0c on a git-managed run; a workspace" >&2
        echo "  built before that has none. Tag the 'pipeline: task0b done' commit by hand:" >&2
        echo "    git -C $PROJECT_DIR log --oneline | grep 'task0b done'" >&2
        echo "    git -C $PROJECT_DIR tag task0c-pre-<module> <sha>" >&2
        exit 2
    fi
    _LAUNCH_ARGS+=(--restart-from-pre-augment)
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

# 2-3. Delete passing-* lock tags + clear the chosen phase entries (shared lib).
restart_from_clear "$PROJECT_DIR" "${_KEYS[@]}"

# 4. Relaunch — continue.sh resumes at the first unfinished phase (for task0c,
# after pipeline.py has reset the workspace to the pre-augmentation tag).
echo "[restart-from] launching continue.sh ${_LAUNCH_ARGS[*]}"
exec "$SCRIPT_DIR/continue.sh" "${_LAUNCH_ARGS[@]}"
