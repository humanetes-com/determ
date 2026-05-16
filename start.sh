#!/usr/bin/env bash
# start.sh — Pre-flight checks before starting the pipeline.
#
# Usage:
#   bash start.sh --project argh-master
#   bash start.sh --project argh-master --force    # skip prompts, apply suggestions
#   bash start.sh --project argh-master --clean    # passed through to pipeline.py

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
FORCE=0
PROJECT=""
PASSTHROUGH_ARGS=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

_args=("$@")
i=0
while [ $i -lt ${#_args[@]} ]; do
    arg="${_args[$i]}"
    case "$arg" in
        --force)
            FORCE=1
            ;;
        --project)
            i=$((i+1))
            PROJECT="${_args[$i]}"
            PASSTHROUGH_ARGS="$PASSTHROUGH_ARGS --project $PROJECT"
            ;;
        --project=*)
            PROJECT="${arg#--project=}"
            PASSTHROUGH_ARGS="$PASSTHROUGH_ARGS $arg"
            ;;
        --clean|--augment-tests|--nolog|--progress)
            PASSTHROUGH_ARGS="$PASSTHROUGH_ARGS $arg"
            ;;
        *)
            PASSTHROUGH_ARGS="$PASSTHROUGH_ARGS $arg"
            ;;
    esac
    i=$((i+1))
done

if [ -z "$PROJECT" ]; then
    echo "[error] --project is required."
    echo "Usage: bash start.sh --project <name> [--force] [--clean]"
    exit 1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

confirm() {
    local msg="$1"
    local suggestion="$2"
    echo ""
    echo "[warn] $msg"
    echo "       Suggestion: $suggestion"
    if [ "$FORCE" -eq 1 ]; then
        echo "       [--force] Applying suggestion and continuing."
        return 0
    fi
    printf "       Apply and proceed? [y/N] "
    read -r reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        return 0
    else
        echo ""
        echo "[stop] Aborted. To proceed without fixing, run:"
        echo "       bash start.sh --project $PROJECT$PASSTHROUGH_ARGS --force"
        exit 1
    fi
}

env_get() {
    grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d ' ' || true
}

env_set() {
    local key="$1"
    local val="$2"
    if grep -q "^$key=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^$key=.*|$key=$val|" "$ENV_FILE"
    else
        echo "$key=$val" >> "$ENV_FILE"
    fi
    echo "       [ok] .env updated: $key=$val"
}

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

echo ""
echo "Pre-flight checks for project: $PROJECT"
echo "-------------------------------------------"

# ---------------------------------------------------------------------------
# CHECK 1 — .env exists
# ---------------------------------------------------------------------------

if [ ! -f "$ENV_FILE" ]; then
    echo "[error] .env not found at $ENV_FILE"
    echo "        Run: cp $SCRIPT_DIR/.env.example $ENV_FILE  then edit MODEL= and optionally ANTHROPIC_API_KEY="
    exit 1
fi
echo "[ok] .env found."

# ---------------------------------------------------------------------------
# CHECK 2 — .venv exists and Python dependencies installed
# ---------------------------------------------------------------------------

VENV="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV" ]; then
    if confirm \
        ".venv not found — Python virtual environment is missing." \
        "Create it now:  python3 -m venv .venv && pip install -r requirements.txt"
    then
        echo "       Creating .venv..."
        python3 -m venv "$VENV"
        source "$VENV/bin/activate"
        pip install -q -r "$SCRIPT_DIR/requirements.txt"
        echo "       [ok] .venv created and dependencies installed."
    fi
else
    echo "[ok] .venv found."
    # Check that required packages are importable.
    MISSING=()
    source "$VENV/bin/activate"
    for pkg in anthropic openai dotenv; do
        if ! python -c "import $pkg" 2>/dev/null; then
            MISSING+=("$pkg")
        fi
    done
    if [ ${#MISSING[@]} -gt 0 ]; then
        if confirm \
            "Missing Python packages: ${MISSING[*]}" \
            "Install them:  pip install -r requirements.txt"
        then
            pip install -q -r "$SCRIPT_DIR/requirements.txt"
            echo "       [ok] Dependencies installed."
        fi
    else
        echo "[ok] Python dependencies present (anthropic, openai, dotenv)."
    fi
fi

# ---------------------------------------------------------------------------
# CHECK 3 — project directory exists
# ---------------------------------------------------------------------------

PROJECT_DIR="$SCRIPT_DIR/$PROJECT"
if [ ! -d "$PROJECT_DIR" ]; then
    echo ""
    echo "[error] Project directory not found: $PROJECT_DIR"
    echo "        Place the project folder inside $SCRIPT_DIR/ and retry."
    exit 1
fi
echo "[ok] Project directory: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# CHECK 4 — Compute optimal OLLAMA_NUM_CTX from GPU VRAM and model architecture
#
# Strategy: total_vram - model_weight_vram - cuda_overhead = kv_headroom
#           max_ctx = kv_headroom / kv_bytes_per_token
#
# KV bytes per token depends on the KV cache precision (OLLAMA_KV_CACHE_TYPE):
#   fp16  → 2 bytes/element  (default, highest quality)
#   q8_0  → 1 byte/element   (2× context, minimal quality loss)
#   q4_0  → 0.5 bytes/element (4× context, small quality loss)
#
# Model weight VRAM is estimated from parameter count × bits-per-weight for the
# active quantization (read from Ollama's /api/show if available).
# ---------------------------------------------------------------------------

_TOTAL_VRAM=""
if command -v nvidia-smi &>/dev/null; then
    _TOTAL_VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits \
        2>/dev/null | head -1 | tr -d ' ')
fi

if [ -z "$_TOTAL_VRAM" ] || ! [[ "$_TOTAL_VRAM" =~ ^[0-9]+$ ]]; then
    echo "[skip] Cannot read GPU VRAM — skipping context size check."
else
    _CTX_OLLAMA_HOST=$(env_get "OLLAMA_BASE_URL")
    _CTX_OLLAMA_HOST="${_CTX_OLLAMA_HOST:-http://localhost:11434}"
    _CTX_OLLAMA_HOST="${_CTX_OLLAMA_HOST%/v1}"
    _CTX_MODEL=$(env_get "MODEL")
    _CTX_KV_TYPE=$(env_get "OLLAMA_KV_CACHE_TYPE")

    # Try to get model architecture from Ollama (may not be running yet — that's ok).
    _MODEL_JSON=$(curl -sf --max-time 4 "$_CTX_OLLAMA_HOST/api/show" \
        -d "{\"name\":\"${_CTX_MODEL}\"}" 2>/dev/null || echo "{}")

    # Also check /api/ps — if the model is already loaded we can read actual size_vram
    # instead of estimating. This is far more accurate than the theoretical calculation
    # because it accounts for activations, embedding tables, and other runtime overhead
    # that param_count × bits/weight misses by ~15%.
    _PS_JSON=$(curl -sf --max-time 4 "$_CTX_OLLAMA_HOST/api/ps" 2>/dev/null || echo "{}")

    # Python does the math and prints: RECOMMENDED_CTX KV_TYPE EXPLANATION
    _CTX_RESULT=$(python3 - "$_TOTAL_VRAM" "$_CTX_MODEL" "${_CTX_KV_TYPE}" \
        "$_MODEL_JSON" "$_PS_JSON" <<'PYEOF'
import sys, json, math

total_vram_mb = int(sys.argv[1])
model_name    = sys.argv[2].lower()
kv_type_env   = sys.argv[3].strip()

try:
    info = json.loads(sys.argv[4])
except Exception:
    info = {}

try:
    ps = json.loads(sys.argv[5])
except Exception:
    ps = {}

mi      = info.get("model_info", {})
details = info.get("details", {})

# ── Architecture ────────────────────────────────────────────────────────────
def _mi(key, fallback):
    for prefix in ("qwen3", "llama", "mistral", "phi3", "gemma"):
        v = mi.get(f"{prefix}.{key}")
        if v is not None:
            return int(v)
    return fallback

layers   = _mi("block_count",                   32)
kv_heads = _mi("attention.head_count_kv",        8)
key_dim  = _mi("attention.key_length",         128)
val_dim  = _mi("attention.value_length",       128)
params   = int(mi.get("general.parameter_count", 0))
quant    = details.get("quantization_level", "Q4_K_M")

# ── KV cache bytes per token ─────────────────────────────────────────────────
kv_type = kv_type_env.lower() if kv_type_env else "fp16"
KV_BYTES_PER_ELEM = {"fp16": 2.0, "f16": 2.0, "q8_0": 1.0, "q4_0": 0.5}
elem_bytes = KV_BYTES_PER_ELEM.get(kv_type, 2.0)
kv_bytes_per_token = int(layers * kv_heads * (key_dim + val_dim) * elem_bytes)

# ── Base model VRAM — prefer actual measurement over theoretical estimate ─────
# /api/ps reports size_vram = model weights + current KV cache.
# Subtract the KV for the loaded context to isolate the model base VRAM.
measured_base_mb = 0
source = "theoretical"
for m in ps.get("models", []):
    if model_name.split(":")[0] in m.get("name", "").lower():
        loaded_ctx   = m.get("context_length", 0)
        loaded_vram  = m.get("size_vram", 0) // (1024 * 1024)
        # Estimate the KV cache portion currently loaded.
        # Use fp16 as default if we don't know the loaded KV type.
        # Use the actual KV precision (from env var) — fp16 would over-subtract KV
        # and underestimate the model base, leading to an inflated headroom figure.
        loaded_kv_mb = int(layers * kv_heads * (key_dim + val_dim)
                           * KV_BYTES_PER_ELEM.get(kv_type, 2.0) * loaded_ctx
                           / (1024 * 1024))
        measured_base_mb = max(0, loaded_vram - loaded_kv_mb)
        source = f"measured (loaded ctx={loaded_ctx}, size_vram={loaded_vram}MB, est_kv={loaded_kv_mb}MB)"
        break

if measured_base_mb > 0:
    # Use actual measurement + small safety margin (512 MB) for runtime variance.
    kv_headroom_mb = total_vram_mb - measured_base_mb - 512
else:
    # Fall back to theoretical estimate. Use a 20% overhead multiplier on top of
    # raw parameter-count estimate — empirically corrects for activations, buffers,
    # and embedding tables that pure bits/weight math misses.
    BITS = {
        "Q2_K": 2.63, "Q3_K_S": 3.00, "Q3_K_M": 3.35, "Q3_K_L": 3.60,
        "Q4_0": 4.50, "Q4_K_S": 4.37, "Q4_K_M": 4.85,
        "Q5_0": 5.50, "Q5_K_S": 5.50, "Q5_K_M": 5.69,
        "Q6_K": 6.56, "Q8_0":   8.50, "F16":   16.00, "F32":   32.00,
    }
    bits_per_w = BITS.get(quant, 4.85)
    if params > 0:
        raw_mb = int(params * bits_per_w / 8 / 1024 / 1024)
    else:
        import re
        m2 = re.search(r"(\d+(?:\.\d+)?)b", model_name)
        b = float(m2.group(1)) * 1e9 if m2 else total_vram_mb * 0.8 * 1024 * 1024
        raw_mb = int(b * bits_per_w / 8 / 1024 / 1024)
    model_vram_mb = int(raw_mb * 1.20)   # 20% overhead for runtime buffers
    kv_headroom_mb = total_vram_mb - model_vram_mb - 1024
    source = f"theoretical ({quant}, {params//10**9 if params else '?'}B params, model_est={model_vram_mb}MB)"

if kv_headroom_mb <= 0:
    print(f"4096 {kv_type} (no headroom — {source})")
    sys.exit(0)

# ── Compute max context for each KV precision ─────────────────────────────────
base_kv_bytes = layers * kv_heads * (key_dim + val_dim) * 2  # FP16 baseline
ctx_by_kv = {}
for kv, divisor in [("fp16", 1), ("q8_0", 2), ("q4_0", 4)]:
    kb = base_kv_bytes // divisor
    mc = int(kv_headroom_mb * 1024 * 1024 / kb)
    pp = 4096
    while pp * 2 <= mc and pp * 2 <= 131072:
        pp *= 2
    ctx_by_kv[kv] = pp

if kv_type_env:
    suggested_kv = kv_type
    p = ctx_by_kv.get(kv_type, 4096)
else:
    suggested_kv = "q4_0"
    p = ctx_by_kv["q4_0"]

explanation = (
    f"total={total_vram_mb}MB  source={source}  "
    f"kv_headroom={kv_headroom_mb}MB  "
    f"fp16→{ctx_by_kv['fp16']}  q8_0→{ctx_by_kv['q8_0']}  q4_0→{ctx_by_kv['q4_0']}  "
    f"recommended={p} ({suggested_kv})"
)
print(f"{p} {suggested_kv} {explanation}")
PYEOF
    )

    RECOMMENDED_CTX=$(echo "$_CTX_RESULT" | awk '{print $1}')
    RECOMMENDED_KV=$(echo "$_CTX_RESULT"  | awk '{print $2}')
    CTX_EXPLAIN=$(echo   "$_CTX_RESULT"   | cut -d' ' -f3-)

    CURRENT_CTX=$(env_get "OLLAMA_NUM_CTX")
    CURRENT_KV=$(env_get  "OLLAMA_KV_CACHE_TYPE")

    echo "       VRAM math: $CTX_EXPLAIN"

    # ── Update OLLAMA_NUM_CTX ──────────────────────────────────────────────────
    if [ -z "$CURRENT_CTX" ]; then
        if confirm \
            "OLLAMA_NUM_CTX not set." \
            "Set OLLAMA_NUM_CTX=$RECOMMENDED_CTX"
        then env_set "OLLAMA_NUM_CTX" "$RECOMMENDED_CTX"
        fi
    elif [ "$CURRENT_CTX" -lt "$RECOMMENDED_CTX" ] 2>/dev/null; then
        if confirm \
            "OLLAMA_NUM_CTX=$CURRENT_CTX but VRAM supports $RECOMMENDED_CTX." \
            "Increase to OLLAMA_NUM_CTX=$RECOMMENDED_CTX"
        then env_set "OLLAMA_NUM_CTX" "$RECOMMENDED_CTX"
        fi
    else
        echo "[ok] OLLAMA_NUM_CTX=$CURRENT_CTX (VRAM supports up to $RECOMMENDED_CTX)."
    fi

    # ── Suggest OLLAMA_KV_CACHE_TYPE if it would unlock more context ───────────
    if [ -z "$CURRENT_KV" ] && [ "$RECOMMENDED_KV" != "fp16" ]; then
        if confirm \
            "OLLAMA_KV_CACHE_TYPE not set — defaulting to fp16 limits context size." \
            "Set OLLAMA_KV_CACHE_TYPE=$RECOMMENDED_KV (doubles/quadruples ctx vs fp16)"
        then env_set "OLLAMA_KV_CACHE_TYPE" "$RECOMMENDED_KV"
        fi
    elif [ -n "$CURRENT_KV" ]; then
        echo "[ok] OLLAMA_KV_CACHE_TYPE=$CURRENT_KV."
    fi
fi

# ---------------------------------------------------------------------------
# CHECK 5 — Ollama server running
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL=$(env_get "OLLAMA_BASE_URL")
OLLAMA_HOST="${OLLAMA_BASE_URL:-http://localhost:11434}"
OLLAMA_HOST="${OLLAMA_HOST%/v1}"

if curl -sf --max-time 3 "$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
    echo "[ok] Ollama server is running at $OLLAMA_HOST."
else
    if confirm \
        "Ollama server is not responding at $OLLAMA_HOST." \
        "Start it:  sudo systemctl start ollama  (or: ollama serve &)"
    then
        if systemctl is-enabled ollama &>/dev/null 2>&1; then
            sudo systemctl start ollama
        else
            ollama serve &>/dev/null &
        fi
        echo "       Waiting for Ollama to start..."
        for i in $(seq 1 10); do
            sleep 2
            if curl -sf --max-time 2 "$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
                echo "       [ok] Ollama started."
                break
            fi
            if [ "$i" -eq 10 ]; then
                echo "       [error] Ollama did not start in time. Start it manually and retry."
                exit 1
            fi
        done
    fi
fi

# ---------------------------------------------------------------------------
# CHECK 6 — Model available locally
# ---------------------------------------------------------------------------

MODEL=$(env_get "MODEL")
if [ -z "$MODEL" ]; then
    echo "[skip] MODEL not set in .env — skipping model availability check."
elif [[ "$MODEL" == claude-* ]]; then
    echo "[ok] Claude API model '$MODEL' — no local pull needed."
    # Check API key is set
    API_KEY=$(env_get "ANTHROPIC_API_KEY")
    if [ -z "$API_KEY" ]; then
        if confirm \
            "MODEL=$MODEL requires ANTHROPIC_API_KEY but it is not set in .env." \
            "Add ANTHROPIC_API_KEY=sk-ant-... to .env"
        then
            echo ""
            printf "       Paste your API key: "
            read -r key
            env_set "ANTHROPIC_API_KEY" "$key"
        fi
    else
        echo "[ok] ANTHROPIC_API_KEY is set."
    fi
else
    TAGS=$(curl -sf --max-time 3 "$OLLAMA_HOST/api/tags" 2>/dev/null || echo "")
    if echo "$TAGS" | grep -q "\"${MODEL}\""; then
        echo "[ok] Model '$MODEL' is available locally."
    else
        if confirm \
            "Model '$MODEL' not found in Ollama local library." \
            "Pull it now:  ollama pull $MODEL  (may take several minutes)"
        then
            echo "       Pulling $MODEL..."
            ollama pull "$MODEL"
            echo "       [ok] Model pulled."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# All checks passed — start the pipeline
# ---------------------------------------------------------------------------

echo ""
echo "-------------------------------------------"
echo "[ok] All checks passed. Starting pipeline..."
echo ""

cd "$SCRIPT_DIR"
source "$VENV/bin/activate"

nohup python pipeline.py --project "$PROJECT" $PASSTHROUGH_ARGS > /tmp/pipeline_live.log 2>&1 &
PID=$!

echo "[ok] Pipeline started (PID $PID)"
echo "     Log:    tail -f /tmp/pipeline_live.log"
echo "     Status: pgrep -la python | grep pipeline"
