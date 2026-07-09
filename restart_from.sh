#!/usr/bin/env bash
# restart_from.sh <stage> — re-run the pipeline from <stage> onward, preserving
# all existing fills in .skeleton-filled/.
#
# Resumable boundaries (where the pipeline_state has a real phase entry):
#   task1   → re-run FR synthesis + the whole reconstruct loop
#   task2   → re-run the reconstruct loop (task2a per-cluster → task2b merge →
#             task2c merged-fill → task3 test → task4 genesis)
#
# task2a/task2b/task2c/task3/task4 are accepted as aliases for task2 — they are
# sub-stages of the single `task2-4` reconstruct phase (atomic within one
# generate() call / loop), so restarting any of them re-runs the loop. They
# exist as VISIBILITY labels in the monitor's phase bar, not as separate
# resumable phases. task0c/strip and earlier need the original source → use
# redo.sh (Hard Rule 11).
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
    echo "  stage ∈ task1 | task2 | task2a | task2b | task2c | task3 | task4"
    echo "    task2c = re-run only the merged-tree fill (skip per-cluster + merge)"
    echo "  earlier stages (task0c/strip) → use redo.sh"
    exit 2
fi
read -ra _KEYS <<< "$_KEYS_STR"
echo "[restart-from] stage=$STAGE → clearing phase entries: ${_KEYS[*]}"

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

# 4. Relaunch — continue.sh resumes at the first unfinished phase.
echo "[restart-from] launching continue.sh --y"
exec "$SCRIPT_DIR/continue.sh" --y
