#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read BACKEND from .env (grep for uncommented BACKEND= line)
BACKEND=$(grep -E '^[[:space:]]*BACKEND=' "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
BACKEND="${BACKEND:-ollama}"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
SERVER_LOG="$LOG_DIR/server.log"
LLM_DEBUG_LOG="$LOG_DIR/debug.log"
LLM_STREAM_LOG="$LOG_DIR/stream.log"

# ---------------------------------------------------------------------------
# Kill existing pipeline (ask confirmation unless --y passed)
# ---------------------------------------------------------------------------
_YES=false
_GRACEFUL=false
_WIPE=false
for _arg in "$@"; do
    [[ "$_arg" == "--y" ]]        && _YES=true
    [[ "$_arg" == "--graceful" ]] && _GRACEFUL=true
    [[ "$_arg" == "--wipe" ]]     && _WIPE=true && _YES=true
done

if [[ "$_GRACEFUL" == true ]]; then
    if pgrep -f "pipeline.py" > /dev/null 2>&1; then
        touch /tmp/pipeline_graceful_stop
        echo "Graceful stop requested — waiting for pipeline to finish current attempt..."
        while pgrep -f "pipeline.py" > /dev/null 2>&1; do sleep 3; done
        echo "Pipeline stopped gracefully. Relaunching..."
    else
        echo "No pipeline running — launching fresh."
    fi
elif pgrep -f "pipeline.py" > /dev/null 2>&1; then
    if [[ "$_YES" == false ]]; then
        echo -n "Pipeline is running (PID $(pgrep -f pipeline.py)). Kill it? [y/N] "
        read -r _reply
        [[ "$_reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
    fi
    kill $(pgrep -f "pipeline.py") 2>/dev/null && echo "Killed existing pipeline"
    sleep 2
    pkill -9 -f "pipeline.py" 2>/dev/null || true
    sleep 1
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
# Wipe: cargo clean + strip gen_* blocks (only when --wipe passed)
# ---------------------------------------------------------------------------
if [[ "$_WIPE" == true ]]; then
    > "$LOG_DIR/pipeline_anyhow.log"
    if [[ -d "$SCRIPT_DIR/tmp/anyhow-master" ]]; then
        echo "Running cargo clean on tmp/anyhow-master ..."
        (cd "$SCRIPT_DIR/tmp/anyhow-master" && cargo clean 2>&1 | tail -3)
        echo "Stripping gen_* blocks from tmp/anyhow-master/src ..."
        python3 - <<'PYEOF'
import re, pathlib
gen_re = re.compile(r'\n+(?:// GRCOV_EXCL_START\n)?#\[cfg\(test\)\](?:\n#\[[^\]]*\])*\n(?:pub )?mod gen_\w+\s*\{.*', re.DOTALL)
stripped = []
for p in pathlib.Path("tmp/anyhow-master/src").glob("*.rs"):
    raw = p.read_text(); cleaned = gen_re.sub("", raw).rstrip() + "\n"
    if cleaned != raw:
        p.write_text(cleaned); stripped.append(p.name)
print(f"  Stripped: {', '.join(stripped)}" if stripped else "  Nothing to strip.")
PYEOF
    else
        echo "tmp/anyhow-master not found — will be cloned by pipeline."
    fi
fi

# ---------------------------------------------------------------------------
# Start the pipeline
# ---------------------------------------------------------------------------
_PIPELINE_ARGS="--project anyhow"
[[ "$_WIPE" == true ]] && _PIPELINE_ARGS="--repo https://github.com/dtolnay/anyhow --clean"
nohup .venv/bin/python pipeline.py $_PIPELINE_ARGS > "$LOG_DIR/pipeline_anyhow.log" 2>&1 &
echo "Pipeline started (PID $!) — backend: $BACKEND"
echo "Pipeline log : tail -f $LOG_DIR/pipeline_anyhow.log"
echo "Guards log   : tail -f $LOG_DIR/pipeline_guards.log"
echo "Debug log    : tail -f $LLM_DEBUG_LOG"
echo "Stream log   : tail -f $LLM_STREAM_LOG"
if [[ "$BACKEND" != "ollama" ]] || [[ -s "$SERVER_LOG" ]]; then
    echo "Server log   : tail -f $SERVER_LOG"
fi
tail -f "$LOG_DIR/pipeline_anyhow.log"
