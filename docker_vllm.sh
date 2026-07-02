
#  If it responds cleanly, the model loaded. Then watch nvidia-smi during a pipeline call:

#  watch -n1 nvidia-smi

#  If GPU memory used is well below total (e.g. 38GB used out of 48GB), you have headroom. Each +8192 tokens of context needs
#  roughly +1-2GB of KV cache across both GPUs at the default KV precision.

#!/usr/bin/env bash
# Starts the vLLM Docker container. Reads settings from .env.
# Saves container ID to /tmp/vllm_container.id for lifecycle management.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_env() { grep -E "^[[:space:]]*$1=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]'; }

# True (rc 0) when the HF hub cache holds a COMPLETE snapshot for repo id "$1":
# a real weight file (safetensors/bin/gguf/pt) present, no broken/empty symlink
# targets, no `*.incomplete` blobs. Echoes the reason on failure. Lets us fail
# fast instead of waiting ~20 min or silently pulling a multi-GB download.
_model_cached() {
    local repo="$1"
    local hub="${HF_HOME:-$HOME/.cache/huggingface}/hub"
    local dir="$hub/models--${repo//\//--}"          # "Org/Name" -> "models--Org--Name"
    [[ -d "$dir" ]] || { echo "not in cache ($dir)"; return 1; }
    local snap=""
    snap=$(ls -dt "$dir"/snapshots/*/ 2>/dev/null | head -1) || true
    [[ -n "$snap" ]] || { echo "no snapshot dir"; return 1; }
    local f have=0 broken=0
    shopt -s nullglob
    for f in "$snap"*.safetensors "$snap"*.bin "$snap"*.gguf "$snap"*.pt; do
        have=1
        [[ -s "$f" ]] || broken=1                     # -s follows symlink: false if target missing/empty
    done
    shopt -u nullglob
    [[ "$have" -eq 1 ]]   || { echo "no weight files in snapshot"; return 1; }
    [[ "$broken" -eq 0 ]] || { echo "partial download (broken/empty weight blobs)"; return 1; }
    local inc=""
    inc=$(ls "$dir"/blobs/*.incomplete 2>/dev/null | head -1) || true
    [[ -z "$inc" ]] || { echo "download in progress (.incomplete blobs)"; return 1; }
    return 0
}

VLLM_MODEL=$(_env VLLM_MODEL_NAME)
VLLM_URL=$(_env VLLM_BASE_URL); VLLM_URL="${VLLM_URL:-http://localhost:8000/v1}"
VLLM_PORT="${VLLM_URL##*:}"; VLLM_PORT="${VLLM_PORT%%/*}"; VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_TP=$(_env VLLM_TENSOR_PARALLEL); VLLM_TP="${VLLM_TP:-1}"
VLLM_MAX_LEN=$(_env VLLM_MAX_MODEL_LEN); VLLM_MAX_LEN="${VLLM_MAX_LEN:-131072}"
HF_TOKEN=$(_env HF_TOKEN)
CONTAINER_ID_FILE=/tmp/vllm_container.id

# Parse flags: --y / -y skips the confirmation prompts below.
# --allow-download bypasses the "model fully cached?" gate (lets vLLM fetch it).
_YES=false
_ALLOW_DL=false
for _arg in "$@"; do
    [[ "$_arg" == "--y" || "$_arg" == "-y" ]] && _YES=true
    [[ "$_arg" == "--allow-download" ]] && _ALLOW_DL=true
done

# Fail fast BEFORE touching any container: if VLLM_MODEL_NAME isn't fully in the
# HF cache, abort with the reason + the repos that ARE cached, so the user fixes
# .env instead of waiting 20 min or triggering a surprise download.
if [[ -z "$VLLM_MODEL" ]]; then
    echo "[error] VLLM_MODEL_NAME is empty in .env — nothing to load." >&2
    exit 1
fi
if [[ "$_ALLOW_DL" == false ]]; then
    if ! _reason=$(_model_cached "$VLLM_MODEL"); then
        echo "[error] model '$VLLM_MODEL' is not fully downloaded: $_reason" >&2
        echo "        Repos present in the HF cache:" >&2
        ls -1d "${HF_HOME:-$HOME/.cache/huggingface}"/hub/models--* 2>/dev/null \
            | sed 's#.*/models--##; s#--#/#; s/^/          /' >&2
        echo "        → fix VLLM_MODEL_NAME in .env to one of those, or pass --allow-download to fetch it." >&2
        exit 1
    fi
    echo "Model '$VLLM_MODEL' is fully present in the HF cache ✓"
fi

# Detect a running pipeline first — restarting vLLM mid-pipeline would
# crash whatever LLM call is in flight. Warn (or kill with --y).
_pipeline_pids=$(pgrep -f "pipeline.py" 2>/dev/null || true)
if [[ -n "$_pipeline_pids" ]]; then
    if [[ "$_YES" == true ]]; then
        echo "Stopping running pipeline (PIDs: $_pipeline_pids) before vLLM restart ..."
        kill $_pipeline_pids 2>/dev/null || true
        sleep 2
        pkill -9 -f "pipeline.py" 2>/dev/null || true
        # Also clean up dangling continue.sh + lock so the next launch is fresh.
        pkill -f "continue.sh" 2>/dev/null || true
        rm -f /tmp/determ_continue.lock
    else
        echo "WARNING: pipeline.py is running (PIDs: $_pipeline_pids)."
        echo "Restarting vLLM now will crash its in-flight LLM call."
        echo -n "Stop the pipeline and proceed? [y/N] "
        read -r _reply
        if [[ "$_reply" =~ ^[Yy]$ ]]; then
            echo "Stopping pipeline ..."
            kill $_pipeline_pids 2>/dev/null || true
            sleep 2
            pkill -9 -f "pipeline.py" 2>/dev/null || true
            pkill -f "continue.sh" 2>/dev/null || true
            rm -f /tmp/determ_continue.lock
        else
            echo "Aborted — pipeline left running, vLLM not restarted."
            exit 0
        fi
    fi
fi

# Detect a running vLLM container (by id-file OR by image match) and ask
# before stopping it. Skipped with --y/-y.
_running_cid=""
if [[ -f "$CONTAINER_ID_FILE" ]]; then
    _candidate=$(cat "$CONTAINER_ID_FILE")
    if docker inspect -f '{{.State.Running}}' "$_candidate" 2>/dev/null | grep -q true; then
        _running_cid="$_candidate"
    fi
fi
if [[ -z "$_running_cid" ]]; then
    # Fallback: look for any running vllm-openai container.
    _running_cid=$(docker ps -q --filter ancestor=vllm/vllm-openai:latest | head -1)
fi

if [[ -n "$_running_cid" ]]; then
    if [[ "$_YES" == true ]]; then
        echo "Stopping existing vLLM container ($_running_cid) ..."
        docker stop "$_running_cid" > /dev/null
    else
        echo -n "vLLM container is running (ID $_running_cid). Stop and restart? [y/N] "
        read -r _reply
        if [[ "$_reply" =~ ^[Yy]$ ]]; then
            echo "Stopping existing vLLM container ($_running_cid) ..."
            docker stop "$_running_cid" > /dev/null
        else
            echo "Aborted — current container left running."
            exit 0
        fi
    fi
fi
rm -f "$CONTAINER_ID_FILE"

echo "Starting vLLM Docker container (model: $VLLM_MODEL) ..."
_hf_env=()
[[ -n "$HF_TOKEN" ]] && _hf_env=(-e "HF_TOKEN=${HF_TOKEN}")

CID=$(docker run -d \
    --runtime nvidia --gpus all \
    "${_hf_env[@]}" \
    -e NCCL_P2P_DISABLE=1 \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    -p "${VLLM_PORT}:${VLLM_PORT}" \
    --ipc=host \
    vllm/vllm-openai:latest \
    "$VLLM_MODEL" \
    --port "$VLLM_PORT" \
    --tensor-parallel-size "$VLLM_TP" \
    --max-model-len "$VLLM_MAX_LEN" \
    --gpu-memory-utilization 0.90 \
    --enable-prefix-caching \
    --max-num-seqs 4 \
    --reasoning-parser "$(_env VLLM_REASONING_PARSER)")


echo "$CID" > "$CONTAINER_ID_FILE"
echo "Container started ($CID)"
echo "Live log: docker logs -f $CID"
echo "Waiting for vLLM to be ready (model load + CUDA graph compilation: 10-20 min) ..."
for _i in $(seq 1 240); do
    sleep 5
    if curl -sf "http://localhost:${VLLM_PORT}/health" > /dev/null 2>&1; then
        echo "vLLM server ready (${_i} checks, $(( _i * 5 / 60 ))m $(( _i * 5 % 60 ))s)."
        exit 0
    fi
    if (( _i % 12 == 0 )); then
        echo "  ... still loading ($(( _i * 5 / 60 ))m elapsed)"
    fi
done
echo "[warn] vLLM not ready after 20 min — check: docker logs $CID"
