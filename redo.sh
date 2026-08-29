#!/usr/bin/env bash
# Kill any running pipeline, wipe the working .skeleton-filled tree, and
# relaunch from the task0c-augmented tag. This is the "clean baseline"
# operation we keep doing manually: equivalent to
#   pkill pipeline.py && rm -rf .skeleton-filled && continue.sh --y --keep-augmented --no-tail
#
# Flags:
#   --force  SIGKILL the running pipeline immediately (default is graceful)
#   --full   also wipe .skeleton/ (forces the bake step to re-run)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/tmp/anyhow"

_FORCE=false
_FULL=false
for _arg in "$@"; do
    [[ "$_arg" == "--force" ]] && _FORCE=true
    [[ "$_arg" == "--full" ]]  && _FULL=true
done

# 1. Stop any running pipeline.
if pgrep -f "pipeline.py" > /dev/null 2>&1; then
    _pids="$(pgrep -f pipeline.py | tr '\n' ' ')"
    if [[ "$_FORCE" == true ]]; then
        echo "[redo] force-killing pipeline (PID $_pids)"
        kill $(pgrep -f "pipeline.py") 2>/dev/null || true
        sleep 2
        pkill -9 -f "pipeline.py" 2>/dev/null || true
        sleep 1
    else
        echo "[redo] graceful-stopping pipeline (PID $_pids)"
        # shellcheck source=lib/graceful_stop.sh
        source "$SCRIPT_DIR/lib/graceful_stop.sh"
        graceful_stop_pipeline 0
    fi
else
    echo "[redo] no pipeline running"
fi

# 2. Wipe the working directory. .skeleton-filled is always safe to wipe
# (it's the iteration scratch). .skeleton requires re-bake on next run,
# slightly slower but still seconds — only wiped on --full.
if [[ -d "$PROJECT_DIR/.skeleton-filled" ]]; then
    echo "[redo] wiping $PROJECT_DIR/.skeleton-filled"
    rm -rf "$PROJECT_DIR/.skeleton-filled"
fi
if [[ "$_FULL" == true ]] && [[ -d "$PROJECT_DIR/.skeleton" ]]; then
    echo "[redo] wiping $PROJECT_DIR/.skeleton (will re-bake)"
    rm -rf "$PROJECT_DIR/.skeleton"
fi

# 3. Relaunch from augmented tag. --y is required because we already did
# the stop above; continue.sh would otherwise refuse to launch (running
# guard) or bail (no-running guard). --keep-augmented restores the
# workspace to the task0c checkpoint and re-runs strip + task1 + task2.
echo "[redo] relaunching continue.sh --y --keep-augmented --no-tail"
exec "$SCRIPT_DIR/continue.sh" --y --keep-augmented --no-tail
