
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

VLLM_MODEL=$(_env VLLM_MODEL_NAME)
VLLM_URL=$(_env VLLM_BASE_URL); VLLM_URL="${VLLM_URL:-http://localhost:8000/v1}"
VLLM_PORT="${VLLM_URL##*:}"; VLLM_PORT="${VLLM_PORT%%/*}"; VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_TP=$(_env VLLM_TENSOR_PARALLEL); VLLM_TP="${VLLM_TP:-1}"
VLLM_MAX_LEN=$(_env VLLM_MAX_MODEL_LEN); VLLM_MAX_LEN="${VLLM_MAX_LEN:-131072}"
HF_TOKEN=$(_env HF_TOKEN)
CONTAINER_ID_FILE=/tmp/vllm_container.id

# Stop existing container if any
if [[ -f "$CONTAINER_ID_FILE" ]]; then
    OLD_CID=$(cat "$CONTAINER_ID_FILE")
    if docker inspect "$OLD_CID" > /dev/null 2>&1; then
        echo "Stopping existing vLLM container ($OLD_CID) ..."
        docker stop "$OLD_CID" > /dev/null
    fi
    rm -f "$CONTAINER_ID_FILE"
fi

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
    --enforce-eager \
    --max-num-seqs 1 \
    --reasoning-parser qwen3)

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
