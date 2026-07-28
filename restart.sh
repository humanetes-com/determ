#!/usr/bin/env bash
# Restarts the pipeline process only — does NOT manage the inference server.
# Assumes the server (ollama/vllm/sglang) is already running.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND=$(grep -E '^[[:space:]]*BACKEND=' "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
BACKEND="${BACKEND:-ollama}"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LLM_DEBUG_LOG="$LOG_DIR/debug.log"
LLM_STREAM_LOG="$LOG_DIR/stream.log"
export LLM_DEBUG_LOG LLM_STREAM_LOG

# Parse arguments
_YES=false
_FORCE=false
_REPO=""
_args=("$@")
for (( i=0; i<${#_args[@]}; i++ )); do
    case "${_args[$i]}" in
        --y)     _YES=true ;;
        --force) _FORCE=true ;;
        --repo)  _REPO="${_args[$((i+1))]}"; i=$((i+1)) ;;
    esac
done

# Default repo if not specified
if [[ -z "$_REPO" ]]; then
    _REPO="https://github.com/dtolnay/anyhow"
fi

# shellcheck source=lib/graceful_stop.sh
source "$SCRIPT_DIR/lib/graceful_stop.sh"

if pgrep -f "pipeline.py" > /dev/null 2>&1; then
    if [[ "$_FORCE" == true ]]; then
        if [[ "$_YES" == false ]]; then
            echo -n "Pipeline is running (PID $(pgrep -f pipeline.py)). Force-kill? [y/N] "
            read -r _reply
            [[ "$_reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
        fi
        pkill -f "pipeline.py" 2>/dev/null && echo "Killed existing pipeline" || true
        sleep 2
    else
        if [[ "$_YES" == true ]]; then
            graceful_stop_pipeline 0
        else
            if ! graceful_stop_pipeline 1; then
                exit 0
            fi
        fi
    fi
else
    echo "No pipeline running."
fi

> "$LLM_DEBUG_LOG"
> "$LLM_STREAM_LOG"
# The run log is ARCHIVED, never truncated, even on a from-scratch restart: this
# script wipes the reconstruction, so its log is the only surviving record of
# what the previous attempt reached. Same numbering pipeline.py's rotation uses
# (.1 newest), so a reader walks one sequence across both.
if [ -s "$LOG_DIR/pipeline_anyhow.log" ]; then
    _n=1
    while [ -e "$LOG_DIR/pipeline_anyhow.log.$_n" ]; do _n=$((_n + 1)); done
    while [ "$_n" -gt 1 ]; do
        mv "$LOG_DIR/pipeline_anyhow.log.$((_n - 1))" "$LOG_DIR/pipeline_anyhow.log.$_n"
        _n=$((_n - 1))
    done
    mv "$LOG_DIR/pipeline_anyhow.log" "$LOG_DIR/pipeline_anyhow.log.1"
fi
: > "$LOG_DIR/pipeline_anyhow.log"

# Workspace cleanup is owned by the pipeline. This script always launches with
# `--repo <repo> --clean`; pipeline.py's --clean path resets the workspace fully
# (git_clean_reset: `git reset --hard` + `git clean -fd` + checkout default, plus
# rmtree + re-clone for URL repos — see pipeline.py ~line 21703 / ~4212), which
# removes every gen_* block. The old script-side cargo-clean/strip was both
# redundant and keyed on a stale path (tmp/anyhow-master; the workspace is
# tmp/<repo-name>), so it never ran. Removed to keep one source of truth.

# ---------------------------------------------------------------------------
# Ensure inference server is running
# ---------------------------------------------------------------------------
if [[ "$BACKEND" == "vllm" ]]; then
    _vllm_url=$(grep -E "^[[:space:]]*VLLM_BASE_URL=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    _vllm_port="${_vllm_url##*:}"; _vllm_port="${_vllm_port%%/*}"; _vllm_port="${_vllm_port:-8000}"
    _container_running=false
    if [[ -f /tmp/vllm_container.id ]]; then
        _cid=$(cat /tmp/vllm_container.id)
        if docker inspect -f '{{.State.Running}}' "$_cid" 2>/dev/null | grep -q true; then
            _container_running=true
        fi
    fi
    if curl -sf "http://localhost:${_vllm_port}/health" > /dev/null 2>&1; then
        echo "vLLM already healthy — skipping relaunch."
    elif [[ "$_container_running" == true ]]; then
        echo "vLLM container is running but still loading — waiting for it to be ready ..."
        until curl -sf "http://localhost:${_vllm_port}/health" > /dev/null 2>&1; do
            sleep 10
            echo "  ... still loading"
        done
        echo "vLLM ready."
    else
        echo "vLLM not running — starting ..."
        bash "$SCRIPT_DIR/docker_vllm.sh"
    fi
fi

cd "$SCRIPT_DIR"
echo "Starting pipeline with repo: $_REPO"
# `>>`, not `>`: pipeline.py bounds this log by truncating it IN PLACE (it
# cannot make the shell reopen a renamed file). A non-append descriptor keeps
# its offset across that truncation and pads the file with NUL bytes up to the
# old size; O_APPEND recomputes the offset to EOF on every write. The file was
# archived and recreated empty just above, so `>>` starts from zero regardless.
nohup .venv/bin/python pipeline.py --repo "$_REPO" --clean --audit >> "$LOG_DIR/pipeline_anyhow.log" 2>&1 &
echo "Pipeline started (PID $!) — backend: $BACKEND"
echo "Pipeline log : tail -f $LOG_DIR/pipeline_anyhow.log"
echo "Server log   : tail -f $LOG_DIR/server.log"
echo "Debug log    : tail -f $LLM_DEBUG_LOG"
echo "Stream log   : tail -f $LLM_STREAM_LOG"
