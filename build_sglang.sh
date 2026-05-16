#!/usr/bin/env bash
# build_sglang.sh — Install SGLang into a Python 3.11 venv and launch the server
#
# SGLang requires Python <= 3.13 (torch.compile hard-errors on 3.14+).
# This script creates a dedicated .venv_sglang using python311, installs
# sglang there, and launches the server. The main pipeline .venv (3.14)
# is untouched.
#
# Usage:
#   bash /home/user/determ/build_sglang.sh
#
# To stop the server:
#   kill $(cat /tmp/sglang.pid)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GGUF="$SCRIPT_DIR/Qwen3.6-27B-Q8_0.gguf"
SERVED_MODEL="qwen3.6-27b-q8"
PORT=30000
LOG="/tmp/sglang_server.log"
PID_FILE="/tmp/sglang.pid"
MIN_SIZE_GB=25
SGLANG_VENV="$SCRIPT_DIR/.venv_sglang"

# ---------------------------------------------------------------------------
# CHECK 1 — GGUF present and correct size
# ---------------------------------------------------------------------------
if [[ ! -f "$GGUF" ]]; then
    echo "ERROR: GGUF not found: $GGUF"
    echo "Download it first:  bash $SCRIPT_DIR/dl_from_huggingface.sh"
    exit 1
fi

ACTUAL_GB=$(du -BG "$GGUF" | awk '{print $1}' | tr -d 'G')
if (( ACTUAL_GB < MIN_SIZE_GB )); then
    echo "ERROR: $GGUF is only ${ACTUAL_GB} GB — this is the mislabeled Unsloth Dynamic file."
    echo "Download the real Q8_0 (~28.6 GB) first:  bash $SCRIPT_DIR/dl_from_huggingface.sh"
    exit 1
fi
echo "[1/4] GGUF verified: ${ACTUAL_GB} GB  ✓"

# ---------------------------------------------------------------------------
# CHECK 2 — Python 3.11 (SGLang requirement: torch.compile broken on 3.14+)
# ---------------------------------------------------------------------------
PYTHON311=""
for candidate in python3.11 python311; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON311="$candidate"
        break
    fi
done

if [[ -z "$PYTHON311" ]]; then
    echo "Python 3.11 not found — installing via pacman ..."
    sudo pacman -S --noconfirm python311
    PYTHON311="python3.11"
fi
echo "[2/4] Python: $($PYTHON311 --version)  ✓"

# Create dedicated SGLang venv if it doesn't exist
if [[ ! -f "$SGLANG_VENV/bin/activate" ]]; then
    echo "      Creating SGLang venv at $SGLANG_VENV ..."
    "$PYTHON311" -m venv "$SGLANG_VENV"
fi
# shellcheck disable=SC1090
source "$SGLANG_VENV/bin/activate"
echo "      Activated: $SGLANG_VENV"

# ---------------------------------------------------------------------------
# CHECK 3 — Install / upgrade sglang into the 3.11 venv
# ---------------------------------------------------------------------------
echo "[3/4] Installing sglang ..."
pip install --upgrade pip --quiet

# Ensure Rust is available for outlines_core source build
if ! command -v rustc &>/dev/null; then
    echo "      Rust not found — installing rustup ..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --quiet
    # shellcheck disable=SC1090
    source "$HOME/.cargo/env"
    echo "      Rust installed: $(rustc --version)"
else
    echo "      Rust found: $(rustc --version)"
    [[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env" || true
fi

pip install --upgrade sglang --quiet
pip install --upgrade gguf --quiet 2>/dev/null || true

# Patch transformers GGUF loader: add qwen35 as alias for qwen3.
# Unsloth uses "qwen35" as the GGUF architecture name for Qwen3.6;
# transformers only knows "qwen3", so the server fails to start without this.
GGML_FILE="$SGLANG_VENV/lib/python3.11/site-packages/transformers/integrations/ggml.py"
if [[ -f "$GGML_FILE" ]] && ! grep -q '"qwen35"' "$GGML_FILE"; then
    python3.11 - "$GGML_FILE" <<'PYEOF'
import sys, re
path = sys.argv[1]
src = open(path).read()
# Add qwen35 config block after qwen3 block
src = src.replace(
    '    "qwen3_moe": {',
    '    "qwen35": {\n'
    '        "context_length": "max_position_embeddings",\n'
    '        "block_count": "num_hidden_layers",\n'
    '        "feed_forward_length": "intermediate_size",\n'
    '        "embedding_length": "hidden_size",\n'
    '        "rope.dimension_count": None,\n'
    '        "rope.freq_base": "rope_theta",\n'
    '        "attention.head_count": "num_attention_heads",\n'
    '        "attention.head_count_kv": "num_key_value_heads",\n'
    '        "attention.layer_norm_rms_epsilon": "rms_norm_eps",\n'
    '        "vocab_size": "vocab_size",\n'
    '    },\n'
    '    "qwen3_moe": {',
    1
)
# Add qwen35 to converter map
src = src.replace(
    '    "qwen3": GGUFQwen2Converter,',
    '    "qwen3": GGUFQwen2Converter,\n    "qwen35": GGUFQwen2Converter,',
    1
)
open(path, 'w').write(src)
PYEOF
    echo "      Patched ggml.py for qwen35  ✓"
else
    grep -q '"qwen35"' "$GGML_FILE" \
        && echo "      ggml.py qwen35 patch already present  ✓" \
        || echo "      WARNING: ggml.py not found — skipping ggml patch"
fi

# Also patch modeling_gguf_pytorch_utils.py to alias qwen35 → qwen3 in the
# updated_architecture section. The GGUF_SUPPORTED_ARCHITECTURES list-based
# check in that file requires this second patch to pass.
GGUF_UTILS="$SGLANG_VENV/lib/python3.11/site-packages/transformers/modeling_gguf_pytorch_utils.py"
if [[ -f "$GGUF_UTILS" ]] && ! grep -q '"qwen35"' "$GGUF_UTILS"; then
    sed -i 's/    elif "minimax-m2" in architecture:/    elif architecture == "qwen35":\n        updated_architecture = "qwen3"\n    elif "minimax-m2" in architecture:/' "$GGUF_UTILS"
    echo "      Patched modeling_gguf_pytorch_utils.py for qwen35  ✓"
else
    grep -q '"qwen35"' "$GGUF_UTILS" \
        && echo "      modeling_gguf_pytorch_utils.py qwen35 patch already present  ✓" \
        || echo "      WARNING: modeling_gguf_pytorch_utils.py not found"
fi
# Clear stale .pyc cache so the patched source is picked up immediately
find "$SGLANG_VENV/lib/python3.11/site-packages/transformers" \
    \( -name "ggml*.pyc" -o -name "modeling_gguf*.pyc" \) 2>/dev/null | xargs rm -f
echo "      sglang installed  ✓"

# ---------------------------------------------------------------------------
# CHECK 4 — Kill any existing server on $PORT
# ---------------------------------------------------------------------------
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "      Stopping existing SGLang server (PID $OLD_PID) ..."
        kill "$OLD_PID" && sleep 2
    fi
    rm -f "$PID_FILE"
fi
pkill -f "sglang.launch_server" 2>/dev/null || true
sleep 1

# ---------------------------------------------------------------------------
# LAUNCH — SGLang server (using the 3.11 venv python)
# ---------------------------------------------------------------------------
echo "[4/4] Launching SGLang server on port $PORT ..."
echo "      Model:  $GGUF"
echo "      Log:    $LOG"
echo ""

nohup env SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1 "$SGLANG_VENV/bin/python" -m sglang.launch_server \
    --model-path "$GGUF" \
    --served-model-name "$SERVED_MODEL" \
    --context-length 131072 \
    --tensor-parallel-size 2 \
    --mem-fraction-static 0.88 \
    --reasoning-parser qwen3 \
    --chunked-prefill-size 4096 \
    --max-running-requests 4 \
    --port "$PORT" \
    --host 0.0.0.0 \
    > "$LOG" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo "      Server PID: $SERVER_PID  (saved to $PID_FILE)"

# ---------------------------------------------------------------------------
# WAIT — poll /health until ready (up to 10 minutes)
# ---------------------------------------------------------------------------
echo ""
echo "Waiting for SGLang server to become ready (loading ~28 GB into VRAM) ..."
echo "  Log: tail -f $LOG"
echo ""
ELAPSED=0
READY=0
while (( ELAPSED < 600 )); do
    if curl -sf "http://localhost:${PORT}/health" > /dev/null 2>&1; then
        READY=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        echo "ERROR: SGLang server process died (PID $SERVER_PID)."
        echo "Last 20 lines of $LOG:"
        tail -20 "$LOG"
        exit 1
    fi
    sleep 10
    (( ELAPSED += 10 ))
    LAST=$(tail -1 "$LOG" 2>/dev/null || true)
    printf "\r  [%3ds]  %s                    " "$ELAPSED" "${LAST:0:72}"
done
echo ""

if (( READY == 0 )); then
    echo "ERROR: server did not respond after 10 minutes."
    echo "Last 20 lines of $LOG:"
    tail -20 "$LOG"
    exit 1
fi

echo ""
echo "SGLang server is ready  ✓"
echo ""
echo "─────────────────────────────────────────────────────────"
echo "  To switch the pipeline to SGLang, edit .env:"
echo ""
echo "    BACKEND=sglang"
echo "    SGLANG_BASE_URL=http://localhost:${PORT}/v1"
echo "    SGLANG_MODEL_NAME=${SERVED_MODEL}"
echo "    SGLANG_THINKING_BUDGET=8192"
echo ""
echo "  Then restart the pipeline:"
echo "    cd $SCRIPT_DIR && source .venv/bin/activate"
echo "    nohup python pipeline.py --project anyhow-master > /tmp/pipeline_anyhow.log 2>&1 &"
echo ""
echo "  To stop the server:"
echo "    kill \$(cat $PID_FILE)"
echo "─────────────────────────────────────────────────────────"
