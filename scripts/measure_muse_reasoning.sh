#!/usr/bin/env bash
# Measure muse's natural reasoning-length distribution on real anyhow-scale
# prompts, to size VLLM_MAX_THINK_CHARS and judge the temperature pin.
#
# WHY THIS EXISTS
#   The 60000 think-cap was sized inside a measured EMPTY BAND of qwen
#   reasoning lengths (36,570-69,830 chars), where a cap separates healthy
#   calls from runaways. Muse has no such band: on the 2026-08-12 anyhow run
#   (`298540-f792975a`) median reasoning was 26,461 chars and the p90 was
#   60,001 — the cap WAS the p90 — 14.4% of calls hit it and ~30% of call-
#   hours were spent inside capped streams; the 2026-08-13 run
#   (`58819-ee78a1d1`) needed the thinking-exhausted followup on 114 of 341
#   calls (~33%). A cap inside a populated region is a per-call tax (a second
#   GPU round-trip), not a separator. Muse's model card also ships no
#   sampling defaults, so vLLM serves it at temperature 1.0 with no nucleus
#   truncation — the plausible mechanism for reasoning that rambles instead
#   of converging. This measurement is what a new cap (and any sampling pin)
#   must be derived from.
#
# USAGE
#   bash scripts/measure_muse_reasoning.sh [RUN_ID ...]
#
#   RUN_IDs name the recorded runs whose retained prompts are sampled
#   (default: the two muse anyhow runs above — sample from the runs whose
#   regime you are tuning for, since prompt shape drives reasoning length).
#   Knobs via env: N (prompts per cell, default 10), STRENGTHS (default
#   "high" — the pipeline's operating point; add medium,low for the full
#   grid), MIN_PROMPT_CHARS (default 60000), PIN_T/PIN_P (pinned-sampling
#   cell, default 0.7/0.8).
#
#   Preconditions: vLLM on :8000 serving the MODEL named in .env, and
#   NOTHING ELSE on the GPU — this is a measurement; a concurrent pipeline
#   both skews it and is skewed by it. It runs serially by design. Cost:
#   cells x N calls with NO client think cap, several minutes per call at
#   anyhow prompt sizes (~1.5-2.5 h for 2 cells x 10 on 2x RTX 3090).
#
# INTERPRETING THE PER-CELL SUMMARY
#   high/as-served  n=10  reasoning med=26,400 p90=48,000 max=55,000 \
#       would-hit-cap(60,000)=1/10  med=210s/call
#
#   - med/p90/max: the NATURAL reasoning-length distribution (chars), with
#     no client cap breaking streams. This is the object of study.
#   - would-hit-cap(X): how many of these healthy, uncapped calls the
#     CURRENT cap X would have broken mid-stream (each break = a followup
#     round-trip bought nothing).
#   - med s/call: wall cost per call; reasoning length is the main driver.
#
#   Deriving the cap: place it ABOVE the healthy distribution, in empty
#   territory — comfortably past the max of the healthy cell (rule of thumb:
#   ~1.2-1.5x max, rounded), so it only ever fires on genuine runaways. If
#   would-hit-cap is 0/N at the current value, the current cap is already a
#   separator and should stand. If max approaches --max-tokens, the
#   distribution is truncated, not natural — raise --max-tokens and re-run.
#
#   The same p90 is the gate for PHASE_C_FOLLOWUP_THINK (default off). The
#   escalated followup's thinking reserve is VLLM_THINKING_BUDGET +
#   VLLM_CONTENT_BUDGET; live 8192+8192 sits below Muse@high's measured
#   p90 of 73718 chars, so enabling the switch at that size is the
#   starved-rung class that emptied the constructor main stream on
#   82157-476ddc16. Place VLLM_THINKING_BUDGET above this cell's p90
#   BEFORE setting PHASE_C_FOLLOWUP_THINK=1. Until that measurement
#   runs, the switch stays off — it is not a lever to leave held as debt.
#
#   Judging the pin: compare the pinned cell against as-served. A materially
#   lower med/p90 at similar content length says temperature 1.0 is what
#   rambles; the sampling row in `domain/model_defaults.py` is justified
#   only after the retained response bodies (saved under the --out dir) also
#   pass a splice-based QUALITY check — shorter reasoning that fills worse
#   is not a win, so do not pin on length alone.
#
#   Raw per-call records land in <out>/results.jsonl, response bodies under
#   <out>/bodies/ for the later quality pass.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

RUNS=("$@")
if [ ${#RUNS[@]} -eq 0 ]; then
    RUNS=(58819-ee78a1d1 298540-f792975a)
fi

MODEL_LINE="$(grep -E '^MODEL=' .env | head -1 | cut -d= -f2-)"
CAP_LINE="$(grep -E '^VLLM_MAX_THINK_CHARS=' .env | head -1 | cut -d= -f2-)"
STRENGTH_LINE="$(grep -E '^VLLM_REASONING_STRENGTH=' .env | head -1 | cut -d= -f2-)"
if [ -z "$MODEL_LINE" ]; then
    echo "measure_muse_reasoning: no MODEL= line in .env" >&2
    exit 2
fi
if ! curl -s --max-time 5 localhost:8000/v1/models | grep -qF "$MODEL_LINE"; then
    echo "measure_muse_reasoning: vLLM on :8000 is not serving $MODEL_LINE —" \
         "start it first (docker_vllm.sh --no-pull --y)." >&2
    exit 1
fi

# Restrict prompt selection to the named runs: the tool takes the first N
# qualifying prompts in event order, and the full events file starts with
# runs from other models and regimes.
SLICE="$(mktemp --tmpdir measure_muse_requests_XXXX.jsonl)"
trap 'rm -f "$SLICE"' EXIT
RUN_ALT="$(IFS='|'; echo "${RUNS[*]}")"
grep -h '"llm.request"' logs/events.jsonl | grep -E "$RUN_ALT" > "$SLICE" || true
if [ ! -s "$SLICE" ]; then
    echo "measure_muse_reasoning: no llm.request events for run(s)" \
         "${RUNS[*]} in logs/events.jsonl" >&2
    exit 2
fi
echo "measure_muse_reasoning: $(wc -l < "$SLICE") request event(s) from ${RUNS[*]}"

MODEL="$MODEL_LINE" \
VLLM_MAX_THINK_CHARS="${CAP_LINE:-60000}" \
VLLM_REASONING_STRENGTH="${STRENGTH_LINE:-high}" \
.venv/bin/python tools/measure_reasoning_economics.py \
    --events "$SLICE" \
    --prompts-dir logs/prompts \
    --n "${N:-10}" \
    --min-prompt-chars "${MIN_PROMPT_CHARS:-60000}" \
    --strengths "${STRENGTHS:-high}" \
    --pin-temperature "${PIN_T:-0.7}" \
    --pin-top-p "${PIN_P:-0.8}" \
    --out logs/reasoning_econ
