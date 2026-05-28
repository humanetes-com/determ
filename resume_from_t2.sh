#!/usr/bin/env bash
# Resume the pipeline from TASK 2, preserving all existing fills in
# .skeleton-filled/. Use this when you want to re-run task2 + task3 with
# new pipeline code WITHOUT throwing away the previous Phase C work.
#
# What this does NOT do (vs redo.sh):
#   - Does NOT wipe .skeleton-filled/. Existing fills survive — the
#     idempotent seed in _generate_one_cluster preserves them, and
#     Phase C's TDD loop targets only remaining `unimplemented!()` stubs.
#   - Does NOT roll back the workspace to the augmented tag. The merged
#     source in tmp/anyhow/src/ from the previous run stays in place
#     (task2 will re-merge into it, which is idempotent for unchanged
#     items).
#
# What it DOES do:
#   - Kills any running pipeline.
#   - Deletes passing-* tags so task3 isn't considered locked.
#   - Clears task2 / task3 / task4 entries from pipeline_state.md so
#     those tasks re-run with the current pipeline.py code.
#   - Launches continue.sh, which resumes at the first unfinished task.
#
# Result: ~1-5 minutes for resume (vs ~3 hours for redo.sh --force).
# Phase C runs but does very little if all fns were already filled.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/tmp/anyhow"

_FORCE=false
for _arg in "$@"; do
    [[ "$_arg" == "--force" ]] && _FORCE=true
done

# 1. Stop any running pipeline.
if pgrep -f "pipeline.py" > /dev/null 2>&1; then
    _pids="$(pgrep -f pipeline.py | tr '\n' ' ')"
    if [[ "$_FORCE" == true ]]; then
        echo "[resume-from-t2] force-killing pipeline (PID $_pids)"
        kill $(pgrep -f "pipeline.py") 2>/dev/null || true
        sleep 2
        pkill -9 -f "pipeline.py" 2>/dev/null || true
        sleep 1
    else
        echo "[resume-from-t2] graceful-stopping pipeline (PID $_pids)"
        # shellcheck source=lib/graceful_stop.sh
        source "$SCRIPT_DIR/lib/graceful_stop.sh"
        graceful_stop_pipeline 0
    fi
else
    echo "[resume-from-t2] no pipeline running"
fi

# 2. Delete passing-* tags so task3 won't consider this state locked.
if [[ -d "$PROJECT_DIR/.git" ]]; then
    _tags=$(git -C "$PROJECT_DIR" tag -l "passing-*" 2>/dev/null || true)
    if [[ -n "$_tags" ]]; then
        echo "[resume-from-t2] deleting locked tags: $(echo $_tags | tr '\n' ' ')"
        echo "$_tags" | xargs -r -I{} git -C "$PROJECT_DIR" tag -d {} 2>/dev/null || true
    fi
fi

# 3. Clear task2 / task3 / task4 sentinels from pipeline_state so those
# tasks re-run. Keeps task0a/0b/0c/strip/task1 — they're either expensive
# (task0c) or cheap to re-trust (strip/task1).
_STATE_FILE="$PROJECT_DIR/pipeline_state.md"
if [[ -f "$_STATE_FILE" ]]; then
    echo "[resume-from-t2] clearing task2/task3/task4 entries from $_STATE_FILE"
    # Match lines like "## task2/anyhow" or "## task2-4/anyhow" — any
    # heading whose task is 2, 3, or 4. Delete the heading and the
    # contiguous block below it (until the next "## " heading or EOF).
    python3 - "$_STATE_FILE" << 'PYEOF'
import re, sys
p = sys.argv[1]
with open(p) as f:
    text = f.read()
# Remove `## taskN[/...]` sections where N in {2, 3, 4}.
out = re.sub(
    r"(?m)^##\s+task[234][^\n]*\n(?:(?!^## )[^\n]*\n?)*",
    "",
    text,
)
with open(p, "w") as f:
    f.write(out)
print("  cleared")
PYEOF
fi

# 4. Relaunch via continue.sh — resumes at the first unfinished task,
# which is now task2 (we just cleared it).
echo "[resume-from-t2] launching continue.sh --y"
exec "$SCRIPT_DIR/continue.sh" --y
