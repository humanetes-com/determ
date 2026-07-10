#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Single-instance lock — prevents two simultaneous continue.sh invocations
# from each launching a pipeline (the race that produced the duplicate-PID
# bug). Exits cleanly if another invocation is in flight.
# ---------------------------------------------------------------------------
_LOCKFILE="/tmp/determ_continue.lock"
exec 200>"$_LOCKFILE"
if ! flock -n 200; then
    echo "Another continue.sh is already running (lock: $_LOCKFILE). Exiting."
    exit 1
fi

# Read BACKEND from .env (grep for uncommented BACKEND= line)
BACKEND=$(grep -E '^[[:space:]]*BACKEND=' "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
BACKEND="${BACKEND:-ollama}"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
SERVER_LOG="$LOG_DIR/server.log"
LLM_DEBUG_LOG="$LOG_DIR/debug.log"
LLM_STREAM_LOG="$LOG_DIR/stream.log"

# ---------------------------------------------------------------------------
# Stop existing pipeline. Graceful by default — the pipeline exits at its
# next per-fn checkpoint, no in-flight work is lost. Use --force to revert
# to the legacy abrupt SIGTERM-then-SIGKILL behaviour. During the graceful
# wait the user can press [c] to cancel or [f] to force-kill.
# ---------------------------------------------------------------------------
# shellcheck source=lib/graceful_stop.sh
source "$SCRIPT_DIR/lib/graceful_stop.sh"
# shellcheck source=lib/pipeline_pids.sh
source "$SCRIPT_DIR/lib/pipeline_pids.sh"

_YES=false
_FORCE=false
_WIPE=false
_KEEP_AUGMENTED=false
_NO_TAIL=false
for _arg in "$@"; do
    [[ "$_arg" == "--y" ]]              && _YES=true
    [[ "$_arg" == "--force" ]]          && _FORCE=true
    # --graceful kept for back-compat; now the default (no-op).
    [[ "$_arg" == "--graceful" ]]       && true
    [[ "$_arg" == "--wipe" ]]           && _WIPE=true && _YES=true
    [[ "$_arg" == "--keep-augmented" ]] && _KEEP_AUGMENTED=true
    [[ "$_arg" == "--no-tail" ]]        && _NO_TAIL=true
done

# Policy: an unflagged `continue.sh` invocation NEVER kills a running
# pipeline. The user must opt in via --y (graceful stop) or --force
# (SIGTERM then SIGKILL). This prevents the surprise where a quick
# `continue.sh` to restart with new flags silently kills an in-flight run.
if pipeline_running; then
    _running_pids="$(pipeline_pids | tr '\n' ' ')"
    if [[ "$_FORCE" == true ]]; then
        echo "Pipeline running (PID $_running_pids) — force-killing."
        kill_pipeline
    elif [[ "$_YES" == true ]]; then
        echo "Pipeline running (PID $_running_pids) — graceful stop."
        graceful_stop_pipeline 0
    else
        echo "Pipeline already running (PID $_running_pids)."
        echo "  continue.sh alone will NOT kill it. Choose one:"
        echo "    bash continue.sh --y      # graceful stop, then relaunch"
        echo "    bash continue.sh --force  # SIGTERM+SIGKILL, then relaunch"
        echo "    bash redo.sh              # kill + wipe .skeleton-filled + relaunch"
        echo "  Or attach a monitor:"
        echo "    tail -f $LOG_DIR/pipeline_anyhow.log"
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Kill existing monitor / any old socket listeners
# ---------------------------------------------------------------------------
pkill -f "socket_capture" 2>/dev/null || true  # kill legacy listener if still running
sleep 1

# ---------------------------------------------------------------------------
# Kill + restart the inference server
# ---------------------------------------------------------------------------
if [[ "$BACKEND" == "sglang" ]]; then
    SGLANG_PID_FILE="/tmp/sglang.pid"
    if [[ -f "$SGLANG_PID_FILE" ]]; then
        OLD_PID=$(cat "$SGLANG_PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "Stopping SGLang server (PID $OLD_PID) ..."
            kill "$OLD_PID"
            sleep 2
        fi
        rm -f "$SGLANG_PID_FILE"
    fi
    pkill -f "sglang.launch_server" 2>/dev/null || true
    sleep 1
    echo "Starting SGLang server ..."
    source "$SCRIPT_DIR/.venv/bin/activate" 2>/dev/null || true
    bash "$SCRIPT_DIR/build_sglang.sh"

elif [[ "$BACKEND" == "vllm" ]]; then
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
        echo "vLLM container is running but still loading — waiting ..."
        until curl -sf "http://localhost:${_vllm_port}/health" > /dev/null 2>&1; do
            sleep 10
            echo "  ... still loading"
        done
        echo "vLLM ready."
    else
        bash "$SCRIPT_DIR/docker_vllm.sh"
    fi

else
    if curl -sf http://localhost:11434/ > /dev/null 2>&1; then
        echo "Ollama already running — skipping restart."
    elif systemctl is-active --quiet ollama 2>/dev/null; then
        echo "Restarting Ollama service ..."
        sudo systemctl restart ollama
        sleep 3
    elif pgrep -x ollama > /dev/null 2>&1; then
        echo "Stopping Ollama process ..."
        pkill -x ollama 2>/dev/null || true
        sleep 2
        echo "Starting Ollama ..."
        nohup ollama serve > "$SERVER_LOG" 2>&1 &
        sleep 3
    else
        echo "Ollama does not appear to be running — skipping server restart."
    fi
fi

# ---------------------------------------------------------------------------
# Set up env vars
# ---------------------------------------------------------------------------
cd "$SCRIPT_DIR"
source .venv/bin/activate

OLLAMA_CHUNK_TIMEOUT=60
> "$LLM_DEBUG_LOG"
> "$LLM_STREAM_LOG"
export LLM_DEBUG_LOG LLM_STREAM_LOG OLLAMA_CHUNK_TIMEOUT OLLAMA_MAX_THINK_CHARS

# ---------------------------------------------------------------------------
# For vLLM: wait until the inference endpoint accepts requests (health passes
# before CUDA graph compilation finishes; inference fails until that's done)
# ---------------------------------------------------------------------------
if [[ "$BACKEND" == "vllm" ]]; then
    _vllm_url=$(grep -E "^[[:space:]]*VLLM_BASE_URL=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    _vllm_port="${_vllm_url##*:}"; _vllm_port="${_vllm_port%%/*}"; _vllm_port="${_vllm_port:-8000}"
    _model=$(grep -E "^[[:space:]]*VLLM_MODEL_NAME=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    echo "Waiting for vLLM inference endpoint to be ready ..."
    for _i in $(seq 1 240); do
        _status=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST "http://localhost:${_vllm_port}/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d "{\"model\":\"${_model}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" \
            2>/dev/null)
        if [[ "$_status" == "200" ]]; then
            echo "vLLM inference ready ($(( _i * 5 / 60 ))m $(( _i * 5 % 60 ))s)."
            break
        fi
        sleep 5
        if (( _i % 12 == 0 )); then
            echo "  ... inference not ready yet ($(( _i * 5 / 60 ))m elapsed, last status: ${_status})"
        fi
        if [[ "$_i" -eq 240 ]]; then
            echo "[warn] vLLM inference not ready after 20 min — starting pipeline anyway"
        fi
    done
fi

# ---------------------------------------------------------------------------
# Wipe: fresh log only. Workspace cleanup is owned by the pipeline.
#
# --wipe launches with `--repo <URL> --clean`, and pipeline.py's --clean path
# does the complete reset itself: for a URL repo it rmtree's + re-clones from
# upstream HEAD (setup_workspace, ~line 4212); then git_clean_reset (~line
# 21703) runs `git reset --hard` + `git clean -fd` + checkout default branch,
# wiping every gen_* block and working edit. A script-side cargo-clean/strip
# would only duplicate that — and the old hardcoded tmp/anyhow-master path was
# stale (workspace is tmp/<repo-name>, e.g. tmp/anyhow), so it never ran.
# ---------------------------------------------------------------------------
if [[ "$_WIPE" == true ]]; then
    > "$LOG_DIR/pipeline_anyhow.log"
fi

# ---------------------------------------------------------------------------
# Start the pipeline
# ---------------------------------------------------------------------------
_PIPELINE_ARGS="--project anyhow --audit"
[[ "$_WIPE" == true ]]           && _PIPELINE_ARGS="--repo https://github.com/dtolnay/anyhow --clean --audit"
[[ "$_KEEP_AUGMENTED" == true ]] && _PIPELINE_ARGS="--project anyhow --restart-from-augmented --audit"

# Final guard against a duplicate slipping in despite the flock (e.g. if
# someone bypassed the lock or pgrep was momentarily wrong above).
if pipeline_running; then
    echo "Pipeline already running (PID $(pipeline_pids | head -1)). Not launching another."
    exit 0
fi
nohup .venv/bin/python pipeline.py $_PIPELINE_ARGS >> "$LOG_DIR/pipeline_anyhow.log" 2>&1 200<&- &
echo "Pipeline started (PID $!) — backend: $BACKEND"
echo "Pipeline log : tail -f $LOG_DIR/pipeline_anyhow.log"
echo "Guards log   : tail -f $LOG_DIR/pipeline_guards.log"
echo "Debug log    : tail -f $LLM_DEBUG_LOG"
echo "Stream log   : tail -f $LLM_STREAM_LOG"
if [[ "$BACKEND" != "ollama" ]] || [[ -s "$SERVER_LOG" ]]; then
    echo "Server log   : tail -f $SERVER_LOG"
fi

# Release the launch lock before tailing the log forever. The lock's job
# was to prevent two simultaneous LAUNCHES — once the pipeline is running
# (PID echoed above), other continue.sh invocations should be free to act
# (they'll hit the running-pipeline guard above instead). Closing fd 200
# also prevents the tail child from inheriting the lock (the bug where a
# zombie `tail -f` from a prior run kept the file's fd open and held the
# advisory flock indefinitely).
flock -u 200
exec 200<&-

# --no-tail: exit right after launch, for programmatic callers (autopilot).
# Wrapping the tailing default in `timeout N` is NOT a safe substitute:
# coreutils timeout signals the whole process GROUP, and nohup only shields
# SIGHUP — the just-launched pipeline gets SIGTERMed ~N seconds in
# (observed: run 12643 died silently 60s after launch).
[[ "$_NO_TAIL" == true ]] && exit 0

tail -f "$LOG_DIR/pipeline_anyhow.log"
