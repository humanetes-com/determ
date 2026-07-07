#!/usr/bin/env bash
# hardcrate_multi — SCALE + real-nightly-cluster smoke, sibling to run_hardcrate.sh
# (CODE_ARCHITECTURE.md R10.7). Different axis from the hardcrate difficulty ladder:
# every fn here is intentionally EASY — the point is enough fns/files for
# PIPELINE_PARALLELISM>1 to have genuine worker contention (both real bugs found
# 2026-07-07 — the impl-fill file-affinity race and the graceful-stop thread bug —
# were invisible at hardcrate's ~70-fn scale and only surfaced on anyhow's
# ~4000-line run), plus a REAL nightly-toolchain cluster (core/build.rs mirrors
# anyhow's own `error_generic_member_access` probe exactly, not a `--cfg` stand-in).
#
# Runs `hcm_core` ALONE. The workspace's cross-crate half (`ext/`, consuming
# `core`'s Scorer trait across a real crate boundary) needs the pipeline to grow
# Cargo `[workspace]` awareness first — technical_debt.md #58, not yet built.
# `cargo_package_name()` returns "" for a workspace-root Cargo.toml (no [package]
# table), and there is no workspace-member-crate handling anywhere in the
# pipeline today, so pointing `--repo` at the workspace root would fail
# immediately/uninformatively rather than exercising anything interesting.
#
# NON-GATING like run_hardcrate.sh: this just runs and reports; no recovery
# ratchet (that measures L3-RECOVERY difficulty, not scale — doesn't apply here).
#
# Requires vLLM on :8000 (bash docker_vllm.sh). Never touches tmp/anyhow|hardcrate|microcrate.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(pwd)"

LOG="$SCRIPT_DIR/logs/pipeline_hcm_$(date -u +%Y%m%d-%H%M%S).log"
FIXTURE="$SCRIPT_DIR/tests/fixtures/hardcrate_multi/core"
STAGED="$SCRIPT_DIR/tmp/hardcrate_multi_src/hcm_core"   # --repo basename → workspace tmp/hcm_core
WS="$SCRIPT_DIR/tmp/hcm_core"
mkdir -p "$SCRIPT_DIR/logs"

export PIPELINE_PARALLELISM="${PIPELINE_PARALLELISM:-4}"

# --- guards (same pattern as run_hardcrate.sh) ------------------------------
if pgrep -f "[p]ython.*pipeline.py" > /dev/null 2>&1; then
    echo "A pipeline.py is already running — stop it before this smoke."
    exit 1
fi
if ! curl -sf http://localhost:8000/health > /dev/null 2>&1; then
    echo "ERROR: vLLM not reachable on :8000. Start it first:  bash docker_vllm.sh"
    exit 1
fi

echo "[hcm] regenerating fixture from tools/hardcrate_multi_gen.py (idempotent) ..."
.venv/bin/python tools/hardcrate_multi_gen.py "$@"

echo "[hcm] staging hcm_core (never point --repo at the tracked fixture itself) ..."
rm -rf "$SCRIPT_DIR/tmp/hardcrate_multi_src" "$WS"
mkdir -p "$(dirname "$STAGED")"
cp -r "$FIXTURE" "$STAGED"
rm -rf "$STAGED/target"

echo "[hcm] launching hcm_core pipeline → $LOG"
nohup .venv/bin/python pipeline.py --repo "$STAGED" --audit > "$LOG" 2>&1 &
PID=$!
echo "Pipeline started (PID $PID). Following the log until it exits (Ctrl-C only stops the tail)."
echo "----------------------------------------------------------------------"
tail --pid="$PID" -f "$LOG" || true
wait "$PID" 2>/dev/null || true

echo "======================================================================"
if grep -q "complete — all tests pass iter" "$LOG"; then
    ITERS="$(grep -oE 'complete — all tests pass iter [0-9]+' "$LOG" | grep -oE '[0-9]+' | tail -1)"
    echo "CONVERGED: GREEN(iter${ITERS})  log=$(basename "$LOG")"
else
    echo "DID NOT CONVERGE — see $LOG"
fi
