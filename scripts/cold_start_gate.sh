#!/usr/bin/env bash
# cold_start_gate.sh — the from-scratch path's standing regression gate.
#
# WHY.  Fixes in this repo are validated on trees already deep in a
# pathological mid-game: restart_from.sh preserves fills, and every walk
# replays a recorded pathological state.  The cold path — t1.5 authoring a
# layout from nothing, first fills, cold attribution — is therefore
# systematically undertested, which is the measured shape of "repair live,
# restart from scratch, it fails again" (2026-08-14 discussion; the pattern
# census's chronics table shows the same escalations recurring across every
# restart from 2026-07-27 to 2026-08-14).  This script is the cheap gate:
# one full from-scratch hardcrate reconstruction, no preserved state, so a
# regression in the cold path fails HERE in minutes instead of surfacing
# hours into the next anyhow run.
#
# USAGE.
#   bash scripts/cold_start_gate.sh [--complexity=N]
# Preconditions and cost:
#   - vLLM must already be serving on :8000 (docker_vllm.sh) — this script
#     refuses to start it, because starting the server also upgrades it and
#     that must never happen as a side effect of a gate.
#   - The GPU must be otherwise idle: NEVER run this beside a live anyhow
#     run — the sweep binaries and the model server are shared.
#   - Wall cost is the full hardcrate t0a..t4 (tens of minutes, model calls
#     included).  Run it per batch of pipeline changes (with run_smokes.sh),
#     not per edit.
#   - It wipes and re-assembles tmp/hardcrate BY DESIGN: preserving state is
#     exactly what this gate exists not to do.  It never touches tmp/anyhow.
#
# HOW TO INTERPRET THE OUTPUT.
#   The last line is "COLD-START GATE: PASS" or "COLD-START GATE: FAIL rc=N".
#   PASS means the full cold path (clone, augment, strip, FR synthesis, t1.5
#   authoring, TDD fill, merge, test) completed under the current tree — the
#   cold path holds for this batch.  FAIL means the regression is IN THE COLD
#   PATH even if the mid-game smokes and walks are green — diagnose from
#   run_hardcrate.sh's log (it prints its location), and treat the failure as
#   a first-class pathology: distill a walk from it like any live one.
#   A refusal (exit 2) is not a verdict: vLLM was down or a pipeline was
#   already running, and the gate did not measure anything.

set -euo pipefail
cd "$(dirname "$0")/.."

COMPLEXITY="${1:-}"

if ! curl -sf --max-time 3 localhost:8000/health >/dev/null 2>&1; then
    echo "cold_start_gate: vLLM not serving on :8000 — start it first" >&2
    echo "(docker_vllm.sh); this gate never starts the server itself." >&2
    exit 2
fi
# lib/pipeline_pids.sh owns pipeline-process detection: its anchored pattern
# matches any real interpreter running pipeline.py (--project AND --repo
# launches) while never matching a monitor/tail whose argv merely contains
# the string — the guard-collision trap its header records.
source lib/pipeline_pids.sh
if pipeline_running; then
    echo "cold_start_gate: a pipeline run is live — the GPU and workspace" >&2
    echo "are shared; run this gate only on an idle box." >&2
    exit 2
fi

rc=0
bash run_hardcrate.sh ${COMPLEXITY:+"$COMPLEXITY"} || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "COLD-START GATE: PASS"
else
    echo "COLD-START GATE: FAIL rc=$rc"
fi
exit "$rc"
