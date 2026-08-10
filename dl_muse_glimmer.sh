#!/usr/bin/env bash
# Downloads a Muse-Glimmer-30B checkpoint to ~/.cache/huggingface using a
# lightweight Python Docker container. No GPU usage — safe to run alongside the
# pipeline. Resumable: re-running continues an interrupted download.
#
# Usage:  bash dl_muse_glimmer.sh [fp8|bf16|draft]     (default: fp8)
#
# Which variant this box can actually serve, measured on the two RTX 3090s
# (24 GiB each, compute capability 8.6, no NVLink — NCCL_P2P_DISABLE=1 in
# docker_vllm.sh):
#
#   fp8   RedHatAI/Muse-Glimmer-30B-FP8-block   32.8 GB  -> ~16.4 GB/GPU at TP=2.
#         Block-wise FP8 (128x128 weight blocks, dynamic per-group activations);
#         vision tower, embeddings and lm_head stay BF16. Ampere has no FP8
#         tensor cores, so vLLM runs it weight-only through the FP8 Marlin
#         kernel (fused fp8->bf16 dequant). Weights therefore stay 8-bit in
#         VRAM. This is the same path that already serves Qwen3.6-27B-FP8 here
#         at VLLM_TENSOR_PARALLEL=2.
#
#   bf16  meta-models/Muse-Glimmer-30B          59.6 GB  -> does NOT fit in 48 GB.
#         Downloadable for requantising locally; not servable on this machine.
#
#   draft meta-models/Muse-Glimmer-30B-assistant 5.1 GB  -> DFlash speculative
#         decoding companion. Only worth pulling once the base model is loading
#         with several GB of headroom left; at TP=2 the FP8 base already leaves
#         only ~7 GB/GPU for KV cache.
#
# Deliberately NOT offered: the NVFP4 checkpoints (Inferact, unsloth, Preyazz).
# NVFP4 kernels are Blackwell-only — sm_120/sm_100 — and will not run on sm_86.
# The GGUF builds are llama.cpp/Ollama artefacts, not vLLM inputs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_env() { grep -E "^[[:space:]]*$1=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]'; }

VARIANT="${1:-fp8}"
case "$VARIANT" in
    fp8)   MODEL="RedHatAI/Muse-Glimmer-30B-FP8-block" ;;
    bf16)  MODEL="meta-models/Muse-Glimmer-30B" ;;
    draft) MODEL="meta-models/Muse-Glimmer-30B-assistant" ;;
    */*)   MODEL="$VARIANT" ;;                      # explicit repo id passthrough
    *)     echo "unknown variant '$VARIANT' (want: fp8 | bf16 | draft | Org/Repo)" >&2; exit 2 ;;
esac

HF_TOKEN=$(_env HF_TOKEN)
LOG=/tmp/dl_muse_glimmer.log

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
    ignore_patterns=['*.msgpack', '*.h5', 'flax_model*', 'tf_model*', 'original/*', '*.gguf'],
)
print('Download complete.')
\"" > "$LOG" 2>&1 &

echo "Download container started (PID $!). Monitor with: tail -f $LOG"
echo
echo "To serve it: uncomment the Muse-Glimmer block in .env (it sets the model,"
echo "the :muse-glimmer image — vLLM 0.27.0+, which the 0.20.2 venv here is not —"
echo "the muse_glimmer reasoning/tool-call parsers, and a max-model-len that fits"
echo "the KV cache the FP8 weights leave free) and comment the Qwen lines above it."
echo "Then: bash docker_vllm.sh   (it pulls the image before each launch)"
