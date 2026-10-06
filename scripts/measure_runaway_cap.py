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

  select   picks complete requests per route — book (t0d.book), file
           (`_ask_fill`), impl (`_fill_turns`), session (`_make_session_route`)
           — newest first, distinct sha, retained under logs/prompts. A
           single prompt is stored whole; a transcript (impl, session) is
           stored as `turns_digest_text`, the whole turn list, and only its
           FIRST turn (system + opening user, `turns == 2`) is taken, so every
           route is measured on the request that opens it.
           Writes logs/reasoning_econ/runaway/selection.json.
  ask      re-asks every selected prompt serially with max_tokens = context
           - prompt tokens - 512 (the per_call request), the served model's
           profile ON payload, and no client cap. Appends calls.jsonl.

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

from pipeline_core.domain.model_profile import (  # noqa: E402
    reasoning_payload,
    require_profile,
    resolve_model_knobs,
    served_model,
)

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


def _env() -> dict[str, str]:
    from dotenv import dotenv_values
    env = {k: v for k, v in dotenv_values(".env").items() if v is not None}
    env.update(os.environ)
    return env


def ask_one(client: Any, *, model: str, messages: list[dict[str, str]], max_tokens: int,
            payload: dict[str, Any], rep: float) -> dict[str, Any]:
    t0 = time.monotonic()
    reasoning = content = ""
    finish = None
    with client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, stream=True,
        stream_options={"include_usage": True},
        extra_body={"chat_template_kwargs": payload, "repetition_penalty": rep, "seed": 17},
    ) as stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
            d = chunk.choices[0].delta
            r = getattr(d, "reasoning", None) or getattr(d, "reasoning_content", None)
            if r:
                reasoning += r
            if d.content:
                content += d.content
    return {"reasoning_chars": len(reasoning), "content_chars": len(content),
            "finish": finish, "seconds": round(time.monotonic() - t0, 1)}


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
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    sel_path = OUT / "selection.json"

    if args.cmd == "select":
        picked = select(args.events, args.prompts_dir, args.per_route)
        sel_path.write_text(json.dumps(picked, indent=1) + "\n")
        for r, v in picked.items():
            print(f"{r:8s} {len(v)} prompts from runs {sorted({x['run'] for x in v})}")
        return 0

    from openai import OpenAI
    env = _env()
    model = served_model(env)
    prof = require_profile(model, env.get("MODEL_FAMILY"))
    knobs = resolve_model_knobs(env)
    payload = reasoning_payload(prof.dialect, True, strength_on=knobs.strength_on,
                                strength_off=knobs.strength_off)
    picked = json.loads(sel_path.read_text())
    client = OpenAI(base_url=env.get("VLLM_BASE_URL", "http://localhost:8000/v1"),
                    api_key="vllm", timeout=7200)
    rep = float(env.get("VLLM_REPETITION_PENALTY", "1.05"))
    print(f"model={model} family={prof.family} payload={payload} ctx={knobs.max_model_len}")
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
                max_tokens = knobs.max_model_len - len(prompt) // 3 - 512
                r = ask_one(client, model=model, messages=messages, max_tokens=max_tokens,
                            payload=payload, rep=rep)
                row = {"tag": args.tag, "model": model, "family": prof.family,
                       "route": route, "sha": it["sha"], "prompt_chars": len(prompt), **r}
                rows.append(row)
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                print(f"  {route:8s} {it['sha'][:12]} prompt={len(prompt):,} "
                      f"reasoning={r['reasoning_chars']:,} content={r['content_chars']:,} "
                      f"finish={r['finish']} {r['seconds']}s", flush=True)
    for route in ROUTES:
        rr = [r for r in rows if r["route"] == route]
        h = [r["reasoning_chars"] for r in rr if r["finish"] == "stop" and r["content_chars"]]
        print(f"{route:8s} healthy {len(h)}/{len(rr)} p50 {_pct(h, .5):,} p90 {_pct(h, .9):,} "
              f"max {max(h, default=0):,} length {sum(1 for r in rr if r['finish'] == 'length')}")
    cap, why = proposal(rows)
    print(f"PROPOSAL ({prof.family}, tag {args.tag}): {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
