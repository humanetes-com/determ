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

# Map the requested stage → the phase-entry keys to clear (the stage + every
# stage after it). task2 reconstruction is now granular:
#   task2a per-cluster fill → task2b merge → task2c merged-fill → task3 → task4
# Clearing a sub-stage but KEEPING the earlier ones "done" makes generate() skip
# the earlier stages on resume (e.g. task2c skips per-cluster + merge and runs
# only the merged-tree fill). task2-4 is the loop-complete marker — always
# cleared so the reconstruct loop re-enters.
case "$STAGE" in
    task1)         _KEYS=(task1 task2a task2b task2c task2-4) ;;
    task2|task2a)  _KEYS=(task2a task2b task2c task2-4) ;;
    task2b)        _KEYS=(task2b task2c task2-4) ;;
    task2c)        _KEYS=(task2c task2-4) ;;
    task3|task4)   _KEYS=(task2-4) ;;   # keep task2a/b/c done → generate no-ops → straight to test/fix
    *)
        echo "usage: restart_from.sh <stage> [--force]"
        echo "  stage ∈ task1 | task2 | task2a | task2b | task2c | task3 | task4"
        echo "    task2c = re-run only the merged-tree fill (skip per-cluster + merge)"
        echo "  earlier stages (task0c/strip) → use redo.sh"
        exit 2 ;;
esac
echo "[restart-from] stage=$STAGE → clearing phase entries: ${_KEYS[*]}"

# 1. Stop any running pipeline.
if pgrep -f "pipeline.py" > /dev/null 2>&1; then
    _pids="$(pgrep -f pipeline.py | tr '\n' ' ')"
    if [[ "$_FORCE" == true ]]; then
        echo "[restart-from] force-killing pipeline (PID $_pids)"
        kill $(pgrep -f "pipeline.py") 2>/dev/null || true; sleep 2
        pkill -9 -f "pipeline.py" 2>/dev/null || true; sleep 1
    else
        echo "[restart-from] graceful-stopping pipeline (PID $_pids)"
        # shellcheck source=lib/graceful_stop.sh
        source "$SCRIPT_DIR/lib/graceful_stop.sh"
        graceful_stop_pipeline 0
    fi
else
    echo "[restart-from] no pipeline running"
fi

# 2. Delete passing-* tags (task3 lock) when re-running the reconstruct loop.
if printf '%s\n' "${_KEYS[@]}" | grep -q 'task2-4'; then
    if [[ -d "$PROJECT_DIR/.git" ]]; then
        _tags=$(git -C "$PROJECT_DIR" tag -l "passing-*" 2>/dev/null || true)
        if [[ -n "$_tags" ]]; then
            echo "[restart-from] deleting locked tags: $(echo $_tags | tr '\n' ' ')"
            echo "$_tags" | xargs -r -I{} git -C "$PROJECT_DIR" tag -d {} 2>/dev/null || true
        fi
    fi
fi

# 3. Delete the chosen `[<key>...]` lines from pipeline_state.md (bracket format,
#    NOT '## key'). Reset the `current:` marker too.
_STATE_FILE="$PROJECT_DIR/pipeline_state.md"
if [[ -f "$_STATE_FILE" ]]; then
    echo "[restart-from] clearing ${_KEYS[*]} entries from $_STATE_FILE"
    python3 - "$_STATE_FILE" "${_KEYS[@]}" << 'PYEOF'
import re, sys
path = sys.argv[1]; keys = sys.argv[2:]
with open(path) as f:
    lines = f.read().splitlines()
alt = "|".join(re.escape(k) for k in keys)
pat = re.compile(rf"^\[(?:{alt})(?:/[^\]]*)?\]")
kept = []
removed = 0
for ln in lines:
    if pat.match(ln):
        removed += 1
        continue
    if ln.startswith("current:"):
        ln = "current: "
    kept.append(ln)
with open(path, "w") as f:
    f.write("\n".join(kept) + "\n")
print(f"  removed {removed} phase line(s)")
PYEOF
fi

# 4. Relaunch — continue.sh resumes at the first unfinished phase.
echo "[restart-from] launching continue.sh --y"
exec "$SCRIPT_DIR/continue.sh" --y
