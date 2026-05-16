#!/usr/bin/env bash
# Downloads Qwen/Qwen3.6-27B-AWQ to ~/.cache/huggingface using a lightweight
# Python Docker container. No GPU usage — safe to run alongside the pipeline.
# Resumable: re-running this script continues an interrupted download.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_env() { grep -E "^[[:space:]]*$1=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]'; }

HF_TOKEN=$(_env HF_TOKEN)
MODEL="mattbucci/Qwen3.6-27B-AWQ"
LOG=/tmp/dl_awq.log

echo "Starting download of $MODEL in background ..."
echo "Log: $LOG"
echo "Cache: ~/.cache/huggingface"

nohup docker run --rm \
    -e HF_TOKEN="$HF_TOKEN" \
    -e HF_HUB_ENABLE_HF_TRANSFER=1 \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    python:3.11-slim \
    bash -c "
        pip install -q huggingface_hub hf_transfer && \
        python -c \"
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='$MODEL',
    repo_type='model',
    ignore_patterns=['*.msgpack', '*.h5', 'flax_model*', 'tf_model*'],
)
print('Download complete.')
\"" > "$LOG" 2>&1 &

echo "Download container started (PID $!). Monitor with: tail -f $LOG"
