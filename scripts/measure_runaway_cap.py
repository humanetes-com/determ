#!/usr/bin/env python3
"""Measure a model family's per_call runaway cap: re-ask the same retained
prompts uncapped through the served model's profile payload, and size the cap
from where healthy reasoning actually ends.

WHY: under VLLM_REASONING_BUDGET=per_call a call is cut only when prompt +
reasoning fill the context. Muse's uncapped tail was measured (2026-09-21, no
cap); Qwen's never was — its last run was 2026-08-12, before per_call, and the
`fixed`-era "runaway above 69,830 chars" may be a request ceiling, the way
Muse's "empty band" turned out to be. `scripts/measure_capped_uncapped.py`
cannot take this reading: it keeps only cut calls (under per_call those are
context exhaustions), drops sessions, has no book route, and posts
`reasoning_strength` on every re-ask, which the Qwen template drops. Here the
payload is the PROFILE's on payload for the served id, and one selection file
pins the same prompt shas for both models.

USAGE
  # 1. choose the prompts once (any server up or not), from the newest runs:
  .venv/bin/python scripts/measure_runaway_cap.py select --per-route 5
  # 2. re-ask them on the served model (vLLM on :8000 serving MODEL):
  .venv/bin/python scripts/measure_runaway_cap.py ask --tag qwen-2026-10-xx
  # Muse on the same selection, for the comparison:
  .venv/bin/python scripts/measure_runaway_cap.py ask --tag muse-2026-10-xx
  # one reasoning level (default max), and a word the profile does not send
  # yet, for calibrating it (Muse's xhigh before Muse is sent MAX as xhigh):
  VLLM_EFFORT_WORD_MAX=xhigh .venv/bin/python scripts/measure_runaway_cap.py \
      ask --tag muse-xhigh-2026-11-xx --effort max

  --effort LEVEL  (ask) the `EffortLevel` every call is sent at: `max`
           (default; what the profile's cap and reserve are measured at) or
           `medium`, to give that level its own `level_calibration`. The
           word the level is sent as is the profile's, or
           `VLLM_EFFORT_WORD_<LEVEL>` from the environment; the first line
           prints the payload, so check it names the word being calibrated.
           Each row carries `effort` and the word in `payload`.

  select   picks complete requests per route — book (t0d.book), file
           (`_ask_fill`), impl (`_fill_turns`), session (`_make_session_route`)
           — newest first, distinct sha, retained under logs/prompts. A
           single prompt is stored whole; a transcript (impl, session) is
           stored as `turns_digest_text`, the whole turn list, and only its
           FIRST turn (system + opening user, `turns == 2`) is taken, so every
           route is measured on the request that opens it.
           Writes logs/reasoning_econ/runaway/selection.json.
  ask      re-asks every selected prompt serially through the pipeline's
           own vLLM transport (the one model_interact builds): its template
           kwargs (reasoning dialect and effort), repetition penalty, input
           count from the server's /tokenize, and max_tokens from the same
           budget planner the LLM controller calls (per_call: context -
           prompt tokens - 512). Only the controller's recovery is left out:
           no runaway char cap, no followup, no retry, so each row is one
           uncapped call. The first line prints the payload and penalty the
           transport sends; check them against the run's "model profile:"
           line. Appends calls.jsonl.

  Cost: serial calls on the GPU; a long healthy call runs 15-40 min. 20
  prompts is several hours. Do not run beside a live pipeline on another
  model (the server serves one model).

HOW TO INTERPRET THE OUTPUT
  One line per call: route, sha12, prompt chars, reasoning chars, content
  chars, finish. Then per route and overall: healthy (finish=stop with
  content) count and reasoning p50/p90/max, and length (context exhausted).
  The proposed cap is the healthy maximum + 25%, rounded up to 1,000 — the
  headroom Muse's 200000 fixed cap carries over its 159,457 tail. A sample
  that is mostly finish=length proposes NO cap: the profile stays
  uncalibrated. Copy the proposal into the family's profile
  (pipeline_core/domain/model_profile.py: runaway_think_chars,
  runaway_calibrated=True, and the tag in `calibration`).

  The second PROPOSAL line is the planner thinking reserve (the profile's
  `thinking_budget`, tokens): the healthy p90 reasoning in tokens, rounded up
  to 1,024. It sizes the input caps (`safe_input_char_limit`): the largest
  admitted prompt still leaves this much room, so a reserve under the healthy
  tail admits prompts that cannot finish a healthy call. Rows carry
  completion_tokens since 2026-11-06; older rows need --cpt (completion
  chars per token, e.g. measured over a run's llm.stream_meta rows) or the
  line says "no reserve". Copy it into `thinking_budget` with the tag.

  report   --tag T [--cpt X]: recompute both proposals from calls.jsonl
           without the GPU.

  The selection is newest-first per route, so it may hold no prompt near the
  input cap; the region between the largest selected prompt and the cap is
  where the reserve decides whether a healthy call fits, and is unmeasured
  until such prompts are selected.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

OUT = Path("logs/reasoning_econ/runaway")
ROUTES = ("book", "file", "impl", "session")


def route_of(ev: dict[str, Any]) -> str | None:
    data = ev.get("data") or {}
    caller = data.get("caller") or ""
    if str(ev.get("in_phase") or "").startswith("t0d.book"):
        return "book"
    if "_make_session_route" in caller:
        return "session"
    if "_ask_fill" in caller:
        return "file"
    if "_fill_turns" in caller:
        return "impl"
    return None


def select(events: Path, prompts: Path, per_route: int) -> dict[str, list[dict[str, Any]]]:
    picked: dict[str, list[dict[str, Any]]] = {r: [] for r in ROUTES}
    seen: set[str] = set()
    lines = events.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if '"llm.request"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        data = ev.get("data") or {}
        turns = data.get("turns")
        if turns is not None and turns != 2:
            continue
        route = route_of(ev)
        sha = (data.get("prompt") or {}).get("sha256")
        if route is None or not sha or sha in seen or len(picked[route]) >= per_route:
            continue
        if not (prompts / f"{sha}.txt").exists():
            continue
        seen.add(sha)
        picked[route].append({"sha": sha, "system": (data.get("system") or {}).get("sha256"),
                              "run": ev.get("run_id"), "ts": ev.get("ts"),
                              "transcript": turns is not None,
                              "chars": (data.get("prompt") or {}).get("char_count")})
        if all(len(v) >= per_route for v in picked.values()):
            break
    return picked


def transcript_messages(digest: str) -> list[dict[str, str]]:
    """Invert `domain.text.turns_digest_text`: `<<role>>\ncontent` parts
    joined by `\n<<turn>>\n`. The system turn already carries the ask
    client's disclaimer."""
    out = []
    for part in digest.split("\n<<turn>>\n"):
        head, _, body = part.partition("\n")
        out.append({"role": head.strip("<>"), "content": body})
    return out


def _pipeline_transport() -> Any:
    """The transport the pipeline would build for this .env: `.env` loaded
    without override, as model_interact does before importing the transports
    (they read MODEL and the knobs at construction), then the same factory.
    Building our own client is what let this script send Muse's repetition
    penalty and no reasoning effort to Qwen3.8 (2026-11-06)."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from pipeline_core.adapters.llm_transports import VLLMTransport, _backend_factory
    transport = _backend_factory()
    if not isinstance(transport, VLLMTransport):
        raise SystemExit(f"BACKEND resolves to {type(transport).__name__}; "
                         "the per_call runaway cap is a vLLM measurement")
    return transport


def ask_one(transport: Any, messages: list[dict[str, str]], effort: Any) -> dict[str, Any]:
    """One call planned and streamed exactly as the controller's first
    attempt is at `effort`, minus its char cap and recovery."""
    from pipeline_core.adapters.llm_transports import MAX_TOKENS
    from pipeline_core.llm.budget import request_max_tokens
    from pipeline_core.llm.events import StreamMeta
    plan = request_max_tokens(
        transport.spec(effort=effort), transport.budget_state(),
        input_chars=sum(len(m.get("content") or "") for m in messages),
        caller_max_tokens=MAX_TOKENS, default_sentinel=MAX_TOKENS, enable_thinking=True,
        input_tokens=transport.count_input_tokens(messages, enable_thinking=True,
                                                  effort=effort))
    meta = StreamMeta()
    t0 = time.monotonic()
    reasoning = content = 0
    for d in transport.stream(messages, model=transport.served_model,
                              max_tokens=plan.request_max, enable_thinking=True, meta=meta,
                              effort=effort):
        reasoning += len(d.reasoning)
        content += len(d.content)
    return {"reasoning_chars": reasoning, "content_chars": content,
            "finish": meta.finish_reason, "prompt_tokens": meta.prompt_tokens,
            "completion_tokens": meta.completion_tokens,
            "max_tokens": plan.request_max, "seconds": round(time.monotonic() - t0, 1)}


def _pct(xs: list[int], q: float) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    return s[min(len(s) - 1, int(math.ceil(q * len(s))) - 1)]


def proposal(rows: list[dict[str, Any]]) -> tuple[int | None, str]:
    healthy = [r["reasoning_chars"] for r in rows if r["finish"] == "stop" and r["content_chars"]]
    length = sum(1 for r in rows if r["finish"] == "length")
    if not rows or length * 2 >= len(rows) or not healthy:
        return None, f"no cap: {length}/{len(rows)} ended on the context (stay uncalibrated)"
    cap = int(math.ceil(max(healthy) * 1.25 / 1000.0) * 1000)
    return cap, f"cap {cap:,} = healthy max {max(healthy):,} + 25% ({len(healthy)}/{len(rows)} healthy)"


def reasoning_tokens(row: dict[str, Any], cpt: float | None) -> float | None:
    """The row's reasoning in tokens: its completion tokens split by the
    reasoning share of its characters, else its reasoning chars over `cpt`
    (rows written before completion tokens were recorded). None when neither
    is known."""
    done = row.get("completion_tokens")
    chars = row["reasoning_chars"] + row["content_chars"]
    if isinstance(done, int) and done > 0 and chars > 0:
        return done * row["reasoning_chars"] / chars
    return row["reasoning_chars"] / cpt if cpt else None


def reserve_proposal(rows: list[dict[str, Any]], cpt: float | None) -> tuple[int | None, str]:
    """The family's planner thinking reserve (`thinking_budget`, tokens): the
    healthy p90 reasoning in tokens, rounded up to 1,024.

    Under per_call the reserve is not what a call may spend; it is what the
    input cap keeps free, so it is the room the LARGEST admitted prompt still
    leaves for reasoning (`safe_input_char_limit`). A reserve below the
    healthy tail admits prompts that cannot finish a healthy call: Qwen3.8's
    inherited 32,768 against this sample's p90 of 174,137 chars (2026-11-06).
    p90 rather than the max because the max sizes the runaway cap and, as a
    reserve, would shrink every prompt to give one call in twenty its room."""
    healthy = [r for r in rows if r["finish"] == "stop" and r["content_chars"]]
    toks = [t for t in (reasoning_tokens(r, cpt) for r in healthy) if t is not None]
    if not healthy or len(toks) < len(healthy):
        return None, (f"no reserve: {len(healthy) - len(toks)}/{len(healthy)} healthy row(s) "
                      "carry no completion tokens; pass --cpt (completion chars per token)")
    p90 = sorted(toks)[min(len(toks) - 1, int(math.ceil(0.9 * len(toks))) - 1)]
    reserve = int(math.ceil(p90 / 1024.0) * 1024)
    return reserve, f"reserve {reserve:,} = healthy p90 reasoning {p90:,.0f} tok rounded up to 1,024"


def report(rows: list[dict[str, Any]], family: str, tag: str, cpt: float | None) -> None:
    for route in ROUTES:
        rr = [r for r in rows if r["route"] == route]
        h = [r["reasoning_chars"] for r in rr if r["finish"] == "stop" and r["content_chars"]]
        print(f"{route:8s} healthy {len(h)}/{len(rr)} p50 {_pct(h, .5):,} p90 {_pct(h, .9):,} "
              f"max {max(h, default=0):,} length {sum(1 for r in rr if r['finish'] == 'length')}")
    _cap, why = proposal(rows)
    print(f"PROPOSAL ({family}, tag {tag}): {why}")
    _res, why = reserve_proposal(rows, cpt)
    print(f"PROPOSAL ({family}, tag {tag}): {why}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    s.add_argument("--prompts-dir", type=Path, default=Path("logs/prompts"))
    s.add_argument("--per-route", type=int, default=5)
    a = sub.add_parser("ask")
    a.add_argument("--tag", required=True, help="names this reading in calls.jsonl and the profile")
    a.add_argument("--prompts-dir", type=Path, default=Path("logs/prompts"))
    a.add_argument("--cpt", type=float, default=None,
                   help="completion chars per token, for rows without completion tokens")
    a.add_argument("--effort", default="max", choices=("medium", "max"),
                   help="the reasoning level every call is sent at")
    rp = sub.add_parser("report", help="recompute both proposals from calls.jsonl, no GPU")
    rp.add_argument("--tag", required=True)
    rp.add_argument("--cpt", type=float, default=None,
                    help="completion chars per token, for rows without completion tokens")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    sel_path = OUT / "selection.json"

    if args.cmd == "select":
        picked = select(args.events, args.prompts_dir, args.per_route)
        sel_path.write_text(json.dumps(picked, indent=1) + "\n")
        for r, v in picked.items():
            print(f"{r:8s} {len(v)} prompts from runs {sorted({x['run'] for x in v})}")
        return 0

    if args.cmd == "report":
        rows = [json.loads(line) for line in (OUT / "calls.jsonl").read_text().splitlines()
                if line.strip()]
        rows = [r for r in rows if r.get("tag") == args.tag]
        if not rows:
            print(f"no rows tagged {args.tag!r} in {OUT / 'calls.jsonl'}")
            return 2
        report(rows, str(rows[0].get("family")), args.tag, args.cpt)
        return 0

    transport = _pipeline_transport()
    from pipeline_core.domain.model_profile import (
        EffortLevel,
        require_profile,
        resolve_model_knobs,
    )
    effort = EffortLevel(args.effort)
    model = transport.served_model
    prof = require_profile(model, os.environ.get("MODEL_FAMILY"))
    knobs = resolve_model_knobs(os.environ, model)
    picked = json.loads(sel_path.read_text())
    payload = transport.reasoning_payload(True, effort=effort)
    spec = transport.spec(effort=effort)
    print(f"model={model} family={prof.family} effort={effort.value} payload={payload} "
          f"repetition_penalty={knobs.repetition_penalty} ctx={spec.ctx_len} "
          f"per_call={spec.per_call_reasoning}")
    rows: list[dict[str, Any]] = []
    with (OUT / "calls.jsonl").open("a", encoding="utf-8") as sink:
        for route, items in picked.items():
            for it in items:
                prompt = (args.prompts_dir / f"{it['sha']}.txt").read_text(
                    encoding="utf-8", errors="replace")
                messages: list[dict[str, str]] = []
                if it.get("transcript"):
                    messages = transcript_messages(prompt)
                else:
                    sp = args.prompts_dir / f"{it['system']}.txt" if it.get("system") else None
                    if sp is not None and sp.exists():
                        messages.append({"role": "system",
                                         "content": sp.read_text(encoding="utf-8")})
                    messages.append({"role": "user", "content": prompt})
                res = ask_one(transport, messages, effort)
                row = {"tag": args.tag, "model": model, "family": prof.family,
                       "effort": effort.value, "payload": payload,
                       "route": route, "sha": it["sha"], "prompt_chars": len(prompt), **res}
                rows.append(row)
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                print(f"  {route:8s} {it['sha'][:12]} prompt={len(prompt):,} "
                      f"reasoning={res['reasoning_chars']:,} content={res['content_chars']:,} "
                      f"finish={res['finish']} max_tokens={res['max_tokens']:,} "
                      f"{res['seconds']}s", flush=True)
    report(rows, prof.family, args.tag, args.cpt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
