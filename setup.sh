#!/usr/bin/env bash
# Full environment setup for determ pipeline.
# Idempotent — safe to run multiple times. Skips anything already installed.
#
# Installs: Rust, cargo tools, Python venv + deps.
# Backend-specific: Ollama + model pull (BACKEND=ollama) or vLLM (BACKEND=vllm).
# Usage: bash setup.sh

set -euo pipefail

# Suppress color output from all subcommands (safe in screen/dumb terminals)
export NO_COLOR=1
export TERM=dumb
export CARGO_TERM_COLOR=never
export CLICOLOR=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

ok()   { echo "[ok]   $*"; }
skip() { echo "[skip] $*"; }
info() { echo "       $*"; }
fail() { echo "[fail] $*"; exit 1; }

# ── read BACKEND and model settings from .env ────────────────────────────────
BACKEND="ollama"
OLLAMA_MODEL="qwen3:32b"
if [[ -f "$ENV_FILE" ]]; then
    _b=$(grep -E '^[[:space:]]*BACKEND=' "$ENV_FILE" | tail -1 | cut -d'=' -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]' || true)
    [[ -n "$_b" ]] && BACKEND="$_b"
    _m=$(grep -E '^MODEL=' "$ENV_FILE" | tail -1 | cut -d'=' -f2 | tr -d '[:space:]' || true)
    [[ -n "$_m" ]] && OLLAMA_MODEL="$_m"
fi
info "Backend: $BACKEND"
info "Model from .env: $OLLAMA_MODEL"

echo ""
echo "========================================"
echo " determ setup"
echo "========================================"

# ── 1. apt packages ───────────────────────────────────────────────────────────
echo ""
echo "-- System packages --"
NEEDED_PKGS=()
for pkg in curl build-essential python3 python3-venv python3-pip; do
    dpkg -s "$pkg" &>/dev/null || NEEDED_PKGS+=("$pkg")
done
if [[ ${#NEEDED_PKGS[@]} -gt 0 ]]; then
    info "Installing: ${NEEDED_PKGS[*]}"
    if sudo -n true 2>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "${NEEDED_PKGS[@]}"
        ok "apt packages installed"
    else
        echo "[warn] Cannot sudo non-interactively. Run manually:"
        echo "       sudo apt-get install -y ${NEEDED_PKGS[*]}"
    fi
else
    skip "apt packages already present"
fi

# ── 2. Rust ───────────────────────────────────────────────────────────────────
echo ""
echo "-- Rust / cargo --"
export PATH="$HOME/.cargo/bin:$PATH"
if command -v cargo &>/dev/null; then
    skip "cargo $(cargo --version)"
else
    info "Installing Rust via rustup..."
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path
    export PATH="$HOME/.cargo/bin:$PATH"
    ok "Rust installed"
fi
cargo --version &>/dev/null || fail "cargo not found — open a new shell and re-run"

# ── 3. cargo-nextest ─────────────────────────────────────────────────────────
echo ""
echo "-- cargo-nextest --"
if cargo nextest --version &>/dev/null 2>&1; then
    skip "nextest $(cargo nextest --version 2>/dev/null)"
else
    info "Installing cargo-nextest..."
    cargo install cargo-nextest --locked 2>&1 | tail -2
    ok "cargo-nextest installed"
fi

# ── 4. cargo-tarpaulin (optional) ────────────────────────────────────────────
echo ""
echo "-- cargo-tarpaulin (optional, for coverage augmentation) --"
if cargo tarpaulin --version &>/dev/null 2>&1; then
    skip "tarpaulin $(cargo tarpaulin --version 2>/dev/null)"
else
    info "Installing cargo-tarpaulin..."
    if cargo install cargo-tarpaulin --locked 2>&1 | tail -2; then
        ok "cargo-tarpaulin installed"
    else
        echo "[warn] cargo-tarpaulin failed to compile — skipping (TASK 0c will not run)"
    fi
fi

# ── 5. Ollama (only when BACKEND=ollama) ──────────────────────────────────────
echo ""
echo "-- Ollama --"
if [[ "$BACKEND" == "ollama" ]]; then
    if command -v ollama &>/dev/null; then
        skip "ollama $(ollama --version)"
    else
        info "Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
        ok "Ollama installed"
    fi

    # Ensure Ollama service is running
    if ! pgrep -x ollama &>/dev/null; then
        info "Starting Ollama service..."
        ollama serve &>/dev/null &
        sleep 3
    fi
    ok "Ollama service running"
else
    skip "BACKEND=$BACKEND — skipping Ollama install"
fi

# ── 6. Ollama model (only when BACKEND=ollama) ────────────────────────────────
echo ""
echo "-- Ollama model --"
if [[ "$BACKEND" == "ollama" ]]; then
    echo "-- Ollama model: $OLLAMA_MODEL --"
    if ollama list 2>/dev/null | grep -q "^${OLLAMA_MODEL}"; then
        skip "$OLLAMA_MODEL already pulled"
    else
        info "Pulling $OLLAMA_MODEL (may be large — this will take a while)..."
        ollama pull "$OLLAMA_MODEL"
        ok "$OLLAMA_MODEL ready"
    fi
else
    skip "BACKEND=$BACKEND — skipping Ollama model pull"
fi

# ── 7. Python venv ────────────────────────────────────────────────────────────
echo ""
echo "-- Python venv --"
VENV="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV/bin/python"
VENV_PIP="$VENV/bin/pip"

VENV_OK=false
if [[ -f "$VENV_PYTHON" ]] && [[ -f "$VENV_PIP" ]]; then
    "$VENV_PIP" --version &>/dev/null 2>&1 && VENV_OK=true || true
fi

if [[ "$VENV_OK" == "false" ]]; then
    info "Creating/repairing .venv..."
    rm -rf "$VENV"
    python3 -m venv "$VENV"
    ok ".venv created"
else
    skip ".venv already healthy"
fi

info "Upgrading pip..."
"$VENV_PIP" install --upgrade pip --quiet

info "Installing Python dependencies..."
"$VENV_PIP" install -r "$SCRIPT_DIR/requirements.txt" --quiet
ok "Python deps installed ($("$VENV_PYTHON" --version))"

# ── 8. vLLM (only when BACKEND=vllm) ─────────────────────────────────────────
echo ""
echo "-- vLLM --"
if [[ "$BACKEND" == "vllm" ]]; then
    if "$VENV_PYTHON" -c "import vllm" 2>/dev/null; then
        skip "vllm already installed ($("$VENV_PYTHON" -c 'import vllm; print(vllm.__version__)' 2>/dev/null || echo 'version unknown'))"
    else
        info "Installing vllm (requires CUDA — this may take several minutes)..."
        "$VENV_PIP" install vllm --quiet
        ok "vllm installed"
    fi

    # Verify the GGUF model file exists if VLLM_MODEL_NAME is a local path.
    VLLM_MODEL_NAME=$(grep -E '^[[:space:]]*VLLM_MODEL_NAME=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2 | tr -d '[:space:]' || true)
    if [[ "$VLLM_MODEL_NAME" == /* ]]; then
        if [[ -f "$VLLM_MODEL_NAME" ]]; then
            ok "GGUF model found: $VLLM_MODEL_NAME"
        else
            echo "[warn] GGUF model not found: $VLLM_MODEL_NAME"
            echo "       Download: wget -c https://huggingface.co/unsloth/Qwen3.6-27B-GGUF/resolve/main/Qwen3.6-27B-Q8_0.gguf"
        fi
    else
        info "VLLM_MODEL_NAME=$VLLM_MODEL_NAME (HuggingFace — will be downloaded on first serve)"
    fi
else
    skip "BACKEND=$BACKEND — skipping vLLM install"
fi

# ── 9. GPU / context length check ────────────────────────────────────────────
echo ""
echo "-- GPU / context --"
if command -v nvidia-smi &>/dev/null; then
    READ=$(nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader 2>/dev/null || true)
    if [[ -n "$READ" ]]; then
        GPU_NAME=$(echo "$READ" | cut -d',' -f1 | xargs)
        TOTAL_MIB=$(echo "$READ" | cut -d',' -f2 | tr -d ' MiB')
        FREE_MIB=$(echo "$READ" | cut -d',' -f3 | tr -d ' MiB')
        info "GPU: $GPU_NAME | total: ${TOTAL_MIB} MiB | free: ${FREE_MIB} MiB"

        if   (( FREE_MIB > 6000 )); then CTX=32768
        elif (( FREE_MIB > 2000 )); then CTX=16384
        else                              CTX=8192
        fi
        info "Setting OLLAMA_NUM_CTX=$CTX"
        if grep -q '^OLLAMA_NUM_CTX=' "$ENV_FILE" 2>/dev/null; then
            sed -i "s/^OLLAMA_NUM_CTX=.*/OLLAMA_NUM_CTX=$CTX/" "$ENV_FILE"
        else
            echo "OLLAMA_NUM_CTX=$CTX" >> "$ENV_FILE"
        fi
        ok "OLLAMA_NUM_CTX=$CTX"
    fi
else
    skip "nvidia-smi not found — GPU check skipped"
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " Setup complete."
echo "========================================"
echo ""
echo "To start the pipeline:"
echo "  bash continue.sh"
echo ""
echo "To check status:"
echo "  tail -f /tmp/pipeline_anyhow.log"
echo "  tail -f /tmp/server.log    # inference server"
echo "  tail -f /tmp/debug.log     # LLM call trace"
