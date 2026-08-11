#!/usr/bin/env bash
# The shebang MUST stay on line 1: with anything above it — even a blank line or
# a comment — the kernel sees no `#!`, execve fails ENOEXEC, and running the file
# directly from fish exits 1 printing NOTHING at all. Notes go below.
#
# If the server responds cleanly, the model loaded. Then watch nvidia-smi during
# a pipeline call:
#
#   watch -n1 nvidia-smi
#
# If GPU memory used is well below total (e.g. 38GB used out of 48GB), you have
# headroom. Each +8192 tokens of context needs roughly +1-2GB of KV cache across
# both GPUs at the default KV precision.
#
# Starts the vLLM Docker container. Reads settings from .env.
# Saves container ID to /tmp/vllm_container.id for lifecycle management.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A MISSING key is the normal case, not an error: the single-switch design below
# leaves VLLM_MODEL_NAME, VLLM_IMAGE and both parser keys unset in .env and
# derives them. grep exits 1 on no match, `set -o pipefail` promotes that to the
# pipeline's status, and `VAR=$(_env KEY)` takes the substitution's status — so
# under `set -e` the very first lookup killed the script with NO output at all
# (.env has MODEL=Qwen/Qwen3.6-27B-FP8 and no VLLM_MODEL_NAME line). The trailing
# `|| true` makes an absent key echo empty and succeed, which is what every
# `${VAR:-default}` below already assumes.
_env() { grep -E "^[[:space:]]*$1=" "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]' || true; }

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

# MODEL is the one switch. VLLM_MODEL_NAME falls back to it exactly as the
# Python side does (`os.getenv("VLLM_MODEL_NAME", MODEL)` in
# pipeline_core/adapters/llm_transports.py), and TaskModels.from_env defaults
# every TASK*_MODEL to it too, so with the per-key overrides left unset in .env
# a single MODEL= line moves the server, the served name, the per-task models
# and — through MODEL_CAPS and model_tier, both keyed on that same string — the
# reasoning dialect and the prompt tier.
VLLM_MODEL=$(_env VLLM_MODEL_NAME); VLLM_MODEL="${VLLM_MODEL:-$(_env MODEL)}"
VLLM_URL=$(_env VLLM_BASE_URL); VLLM_URL="${VLLM_URL:-http://localhost:8000/v1}"
VLLM_PORT="${VLLM_URL##*:}"; VLLM_PORT="${VLLM_PORT%%/*}"; VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_TP=$(_env VLLM_TENSOR_PARALLEL); VLLM_TP="${VLLM_TP:-1}"

# Server-side defaults that are a property of the MODEL, not of this box. An
# explicit .env key still wins; these only fill a gap, so switching models does
# not mean remembering four more lines. Keyed on the model string, like
# MODEL_CAPS on the Python side — the two tables carry different facts (server
# flags here, request dialect there) and are keyed the same on purpose.
#
#   image      Muse Glimmer needs vLLM 0.27.0+ for its architecture and the
#              muse_glimmer parsers, shipped as vllm/vllm-openai:muse-glimmer;
#              Qwen3.6-27B-FP8 runs on :latest.
#   parsers    muse_glimmer emits reasoning and tool calls in its own framing.
#   max len    a CEILING, not just a default. Qwen3.6-FP8 (~30 GB) leaves room
#              for 131072 across the two 24 GB cards; the Muse FP8-block weights
#              are 32.8 GB and the model keeps a BF16 vision tower and a
#              202k-entry BF16 output head, so the KV pool that remains will not
#              hold it. This one stays an explicit .env key rather than a pure
#              derivation because the PYTHON side reads VLLM_MAX_MODEL_LEN
#              directly for its budget math (VLLMTransport._ctx, whose own
#              default is 65536) — deleting the key to let the launcher derive it
#              would quietly halve the client's context while the server kept
#              serving 131072. So: keep the key, and refuse a value the model
#              cannot honour instead of letting vLLM fail at KV allocation with
#              an error that names no cause.
case "$VLLM_MODEL" in
    *Muse-Glimmer*)
        _def_image="vllm/vllm-openai:muse-glimmer"
        # 32768 was a conservative guess, never a measurement. Measured
        # 2026-08-11 from the engine's own startup line at TP=2 on 2x RTX 3090:
        #   GPU KV cache size: 189,405 tokens
        #     (Qwen3.6-27B-FP8 on the same box reports 136,533)
        # A single 131,072-token sequence therefore fits with room to spare, and
        # the old ceiling was forcing every comparison against Qwen to run at a
        # quarter of Qwen's native window for no reason the hardware supports.
        _def_reasoning="muse_glimmer"; _def_tool="muse_glimmer"; _max_len_ceiling=131072 ;;
    *Qwen3.6*|*qwen3*)
        _def_image="vllm/vllm-openai:latest"
        _def_reasoning="qwen3"; _def_tool=""; _max_len_ceiling=131072 ;;
    *)
        # Unknown model: no parser rather than a guessed one. A wrong
        # --reasoning-parser does not fail, it just stops splitting reasoning
        # out of the answer, so guessing costs more than declining.
        _def_image="vllm/vllm-openai:latest"
        _def_reasoning=""; _def_tool=""; _max_len_ceiling=131072 ;;
esac
VLLM_MAX_LEN=$(_env VLLM_MAX_MODEL_LEN); VLLM_MAX_LEN="${VLLM_MAX_LEN:-$_max_len_ceiling}"
if (( VLLM_MAX_LEN > _max_len_ceiling )); then
    echo "[error] VLLM_MAX_MODEL_LEN=$VLLM_MAX_LEN exceeds the $_max_len_ceiling this box has" >&2
    echo "        measured room for with '$VLLM_MODEL' at TP=$VLLM_TP." >&2
    echo "        → set VLLM_MAX_MODEL_LEN=$_max_len_ceiling in .env (the Python budget math" >&2
    echo "          reads the same key, so it must be the value the server is started with)." >&2
    exit 1
fi
VLLM_IMAGE=$(_env VLLM_IMAGE); VLLM_IMAGE="${VLLM_IMAGE:-$_def_image}"
VLLM_REASONING=$(_env VLLM_REASONING_PARSER); VLLM_REASONING="${VLLM_REASONING:-$_def_reasoning}"
VLLM_TOOL=$(_env VLLM_TOOL_CALL_PARSER); VLLM_TOOL="${VLLM_TOOL:-$_def_tool}"
HF_TOKEN=$(_env HF_TOKEN)
CONTAINER_ID_FILE=/tmp/vllm_container.id

# Parse flags: --y / -y skips the confirmation prompts below.
# --allow-download bypasses the "model fully cached?" gate (lets vLLM fetch it).
# --no-pull keeps whatever image copy is on disk (offline restart after a crash).
_YES=false
_ALLOW_DL=false
_NO_PULL=false
for _arg in "$@"; do
    [[ "$_arg" == "--y" || "$_arg" == "-y" ]] && _YES=true
    [[ "$_arg" == "--allow-download" ]] && _ALLOW_DL=true
    [[ "$_arg" == "--no-pull" ]] && _NO_PULL=true
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
        # `|| _reply=""` : with no tty (invoked from a script, or stdin closed)
        # read hits EOF and returns 1, which under `set -e` aborts here instead
        # of taking the safe N branch that prints why it stopped.
        read -r _reply || _reply=""
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
    # Fallback: any running vllm-openai container, WHATEVER tag it was started
    # from. An ancestor filter pinned to one tag misses a server left over from
    # a different image (a :latest container while VLLM_IMAGE now names
    # :muse-glimmer), and that container still holds both GPUs — the new one
    # would then OOM at load with no hint as to why.
    _running_cid=$(docker ps --format '{{.ID}} {{.Image}}' \
        | awk '$2 ~ /vllm\/vllm-openai/ {print $1}' | head -1)
fi

if [[ -n "$_running_cid" ]]; then
    if [[ "$_YES" == true ]]; then
        echo "Stopping existing vLLM container ($_running_cid) ..."
        docker stop "$_running_cid" > /dev/null
    else
        echo -n "vLLM container is running (ID $_running_cid). Stop and restart? [y/N] "
        # `|| _reply=""` : with no tty (invoked from a script, or stdin closed)
        # read hits EOF and returns 1, which under `set -e` aborts here instead
        # of taking the safe N branch that prints why it stopped.
        read -r _reply || _reply=""
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

# Pull before every launch. `docker run` only fetches an image it does not
# already have LOCALLY, so a moving tag never moves once pulled: this box can
# sit on a months-old :latest and report the vLLM version that shipped with it,
# which is how a model that needs a newer server fails as "unknown
# architecture" rather than as "your image is stale". A no-op when current.
# Non-fatal: an offline relaunch after a crash must still start on the local
# copy, and the run below fails loudly by itself if there is no copy at all.
if [[ "$_NO_PULL" == false ]]; then
    echo "Pulling $VLLM_IMAGE (skip with --no-pull) ..."
    docker pull "$VLLM_IMAGE" || echo "[warn] pull failed — using the local copy of $VLLM_IMAGE, which may be stale."
else
    echo "Skipping image pull (--no-pull) — using the local copy of $VLLM_IMAGE."
fi
if ! _img_built=$(docker image inspect -f '{{.Created}}' "$VLLM_IMAGE" 2>/dev/null); then
    echo "[error] image '$VLLM_IMAGE' is not present locally and could not be pulled." >&2
    exit 1
fi
echo "Image $VLLM_IMAGE built ${_img_built%T*}"

echo "Starting vLLM Docker container (model: $VLLM_MODEL) ..."
echo "  tp=$VLLM_TP  max-model-len=$VLLM_MAX_LEN  reasoning-parser=${VLLM_REASONING:-<none>}  tool-call-parser=${VLLM_TOOL:-<none>}"
_hf_env=()
[[ -n "$HF_TOKEN" ]] && _hf_env=(-e "HF_TOKEN=${HF_TOKEN}")

# Parser flags are built rather than inlined: an empty VLLM_REASONING_PARSER
# used to reach vLLM as `--reasoning-parser ""`, and the tool-call parser is
# per-model (muse_glimmer needs one, the Qwen path does not).
_parser_args=()
[[ -n "$VLLM_REASONING" ]] && _parser_args+=(--reasoning-parser "$VLLM_REASONING")
[[ -n "$VLLM_TOOL" ]] && _parser_args+=(--enable-auto-tool-choice --tool-call-parser "$VLLM_TOOL")

CID=$(docker run -d \
    --runtime nvidia --gpus all \
    "${_hf_env[@]}" \
    -e NCCL_P2P_DISABLE=1 \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    -p "${VLLM_PORT}:${VLLM_PORT}" \
    --ipc=host \
    "$VLLM_IMAGE" \
    "$VLLM_MODEL" \
    --port "$VLLM_PORT" \
    --tensor-parallel-size "$VLLM_TP" \
    --max-model-len "$VLLM_MAX_LEN" \
    --gpu-memory-utilization 0.90 \
    --enable-prefix-caching \
    --max-num-seqs 4 \
    "${_parser_args[@]}")


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
