#!/usr/bin/env bash
# Restarts the pipeline process only — does NOT manage the inference server.
# Assumes the server (ollama/vllm/sglang) is already running.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND=$(grep -E '^[[:space:]]*BACKEND=' "$SCRIPT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
BACKEND="${BACKEND:-ollama}"

LLM_DEBUG_LOG=/tmp/debug.log
LLM_STREAM_LOG=/tmp/stream.log
export LLM_DEBUG_LOG LLM_STREAM_LOG

# Ask confirmation unless --y passed
_YES=false
for _arg in "$@"; do [[ "$_arg" == "--y" ]] && _YES=true; done

if pgrep -f "pipeline.py" > /dev/null 2>&1; then
    if [[ "$_YES" == false ]]; then
        echo -n "Pipeline is running (PID $(pgrep -f pipeline.py)). Kill it? [y/N] "
        read -r _reply
        [[ "$_reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
    fi
    pkill -f "pipeline.py" 2>/dev/null && echo "Killed existing pipeline" || true
    sleep 2
else
    echo "No pipeline running."
fi

> "$LLM_DEBUG_LOG"
> "$LLM_STREAM_LOG"
> /tmp/pipeline_anyhow.log

# Clean cargo build artifacts — removes stale build-script outputs from dev-deps
# that can cause spurious "couldn't read OUT_DIR/xxx.rs" failures on cargo test --no-run.
echo "Running cargo clean on tmp/anyhow-master ..."
(cd "$SCRIPT_DIR/tmp/anyhow-master" && cargo clean 2>&1 | tail -3)

# Strip any leftover gen_* test blocks (also done by --clean in pipeline, belt+suspenders).
echo "Stripping gen_* blocks from tmp/anyhow-master/src ..."
python3 - <<'PYEOF'
import re, pathlib, sys
gen_re = re.compile(r'\n+#\[cfg\(test\)\](?:\n#\[[^\]]*\])*\n(?:pub )?mod gen_\w+\s*\{.*', re.DOTALL)
stripped = []
for p in pathlib.Path("tmp/anyhow-master/src").glob("*.rs"):
    raw = p.read_text(); cleaned = gen_re.sub("", raw).rstrip() + "\n"
    if cleaned != raw:
        p.write_text(cleaned); stripped.append(p.name)
if stripped:
    print(f"  Stripped: {', '.join(stripped)}")
else:
    print("  Nothing to strip.")
PYEOF

# ---------------------------------------------------------------------------
# Ensure inference server is running
# ---------------------------------------------------------------------------
if [[ "$BACKEND" == "vllm" ]]; then
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
        echo "vLLM container is running but still loading — waiting for it to be ready ..."
        until curl -sf "http://localhost:${_vllm_port}/health" > /dev/null 2>&1; do
            sleep 10
            echo "  ... still loading"
        done
        echo "vLLM ready."
    else
        echo "vLLM not running — starting ..."
        bash "$SCRIPT_DIR/docker_vllm.sh"
    fi
fi

cd "$SCRIPT_DIR"
nohup .venv/bin/python pipeline.py --project anyhow-master --clean > /tmp/pipeline_anyhow.log 2>&1 &
# To use full git-managed workspace mode: replace above with:
# nohup .venv/bin/python pipeline.py --repo tmp/anyhow-master --clean > /tmp/pipeline_anyhow.log 2>&1 &
echo "Pipeline started (PID $!) — backend: $BACKEND"
echo "Pipeline log : tail -f /tmp/pipeline_anyhow.log"
echo "Server log   : tail -f /tmp/server.log"
echo "Debug log    : tail -f $LLM_DEBUG_LOG"
echo "Stream log   : tail -f $LLM_STREAM_LOG"
