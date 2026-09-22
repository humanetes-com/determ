#!/usr/bin/env python3
"""Re-ask the prompts the client think cap actually CUT, with no cap, and
report how long their reasoning runs when nothing stops it.

WHY THIS EXISTS
  `VLLM_MAX_THINK_CHARS=120000` was sized on 2026-08-13 from a population
  whose healthy maximum was 78,019 chars. Recomputed 2026-09-21 over every
  joined llm.request/stream_meta/response row since 2026-09-14, the
  file/impl/pair route's HEALTHY reasoning runs p50 73,962 / p90 109,726 /
  max 119,973 chars and 14% of its calls (54 of 383) end on the cap; session
  turns 15% (27 of 185). A healthy maximum 27 chars under the cap is a
  censored distribution: the cap sits inside it. The healthy rows cannot say
  how long the cut calls would have run — only re-asking the cut prompts
  uncapped can. `scripts/measure_muse_reasoning.sh` cannot take this reading
  as written: it samples the first N prompts above a size (not the ones that
  were cut), it forces a pinned-sampling cell per strength, and it caps every
  call at `--max-tokens 32768`, which is ~139K chars of reasoning at the
  measured 4.25 chars/token — barely above the cap under study.

USAGE
  .venv/bin/python scripts/measure_capped_uncapped.py \
      [--since 2026-09-19] [--n 10] [--route file] [--out DIR]

  --route    file (the `_ask_fill` caller: file-first / per-impl / pair),
             per-fn (`ask_per_fn_body`), or any. Session turns are excluded:
             a turn >1 is a transcript, and the prompt store keeps only the
             last message, so re-asking it is not the call that was cut.
  --since    only calls whose llm.request ts >= this date (UTC).
  --n        prompts to re-ask (most recent cut prompts first, distinct sha).
  Knobs: VLLM_BASE_URL (default http://localhost:8000/v1), MODEL from .env,
  VLLM_REASONING_STRENGTH (default high), VLLM_REPETITION_PENALTY (1.05),
  MAX_MODEL_LEN (default 131072). Each call gets max_tokens = MAX_MODEL_LEN
  - (prompt chars / 3) - 2048, so reasoning is bounded only by context.

  Preconditions and cost: vLLM on :8000 serving MODEL. Calls run serially.
  An uncapped call that was cut at 120K chars runs ~15–40 min, so N=10 is
  2–6 h. It may run beside a live pipeline: reasoning LENGTH does not depend
  on server load, but the seconds column then describes a shared GPU and
  must not be used to size a wall-clock knob. KV: one extra call of up to
  ~131K tokens beside PIPELINE_PARALLELISM=4 fits the 499,930-token pool
  measured at --gpu-memory-utilization 0.97 (2026-09-21) with preemption as
  the worst case, not failure.

HOW TO INTERPRET THE OUTPUT
  One line per call:
    [i/N] <prompt sha12> chars=<prompt chars> cut_at=<reasoning chars the
    live call had when the cap fired> -> reasoning=<uncapped chars>
    content=<chars> finish=<stop|length> <s>
  Then the summary:
    uncapped reasoning med / p90 / max over the N cut prompts
    converged-under(X) for X in 150K/200K/250K/300K: how many of the N
    finished naturally (finish=stop with content) at or under X chars
    runaway: finish=length (context exhausted) — reasoning that did not end
    when the context did is a spiral, not a longer healthy call.
  Decision rule (the cap-inside-the-healthy-band reading of 2026-09-21): set
  VLLM_MAX_THINK_CHARS at the smallest X whose converged-under(X) covers
  the p90 of these N, and VLLM_THINKING_BUDGET >= X/4.25 tokens (round up
  to a multiple of 4096). If most calls end as runaway, or the uncapped
  max exceeds ~250K chars, a raise buys length, not answers: leave the cap
  and push the evidence-slot lever instead. Raw records go to
  <out>/calls.jsonl, reasoning and content bodies to <out>/bodies/, so the
  converged bodies can be judged for quality (did the longer reasoning
  reach a different body than the capped call's follow-up?).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _env_line(key: str) -> str:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def cut_calls(events: Path, *, since: str, route: str) -> list[dict[str, Any]]:
    """Joined calls whose stream ended on the client cap, newest first,
    distinct by prompt sha."""
    pend: dict[tuple[Any, Any], dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"llm.' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            key = (ev.get("run_id"), ev.get("thread"))
            data = ev.get("data") or {}
            kind = ev.get("event")
            if kind == "llm.request":
                pend[key] = {"ts": ev.get("ts", ""), "run": ev.get("run_id"),
                             "caller": data.get("caller") or "",
                             "prompt": (data.get("prompt") or {}).get("sha256"),
                             "system": (data.get("system") or {}).get("sha256"),
                             "chars": (data.get("prompt") or {}).get("char_count")}
            elif kind == "llm.stream_meta" and key in pend:
                pend[key]["meta"] = data
            elif kind == "llm.response" and key in pend:
                out.append(pend.pop(key))
    wanted = {"file": "_ask_fill", "per-fn": "ask_per_fn_body", "any": ""}[route]
    seen: set[str] = set()
    picked = []
    for c in reversed(out):
        meta = c.get("meta") or {}
        if c["ts"] < since or meta.get("finish_reason") == "stop":
            continue
        if "_make_session_route" in c["caller"] or "extract_self_note" in c["caller"]:
            continue
        if wanted and wanted not in c["caller"]:
            continue
        if not c["prompt"] or c["prompt"] in seen:
            continue
        seen.add(c["prompt"])
        c["cut_at"] = meta.get("reasoning_chars")
        picked.append(c)
    return picked


def ask(client: Any, *, model: str, messages: list[dict[str, str]],
        max_tokens: int, strength: str, rep: float) -> dict[str, Any]:
    t0 = time.monotonic()
    reasoning = content = ""
    finish = None
    ptok = None
    with client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, stream=True,
        stream_options={"include_usage": True},
        extra_body={"chat_template_kwargs": {"reasoning_strength": strength},
                    "repetition_penalty": rep, "seed": 17},
    ) as stream:
        for chunk in stream:
            if chunk.usage and chunk.usage.prompt_tokens:
                ptok = chunk.usage.prompt_tokens
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
    return {"reasoning": reasoning, "content": content, "finish": finish,
            "prompt_tokens": ptok, "seconds": time.monotonic() - t0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--prompts-dir", type=Path, default=Path("logs/prompts"))
    ap.add_argument("--since", default="2026-09-19")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--route", choices=("file", "per-fn", "any"), default="file")
    ap.add_argument("--out", type=Path,
                    default=Path("logs/reasoning_econ/capped_uncapped"))
    args = ap.parse_args()

    from openai import OpenAI
    model = os.getenv("VLLM_MODEL_NAME") or _env_line("MODEL")
    strength = os.getenv("VLLM_REASONING_STRENGTH") or _env_line("VLLM_REASONING_STRENGTH") or "high"
    rep = float(os.getenv("VLLM_REPETITION_PENALTY", "1.05"))
    ctx = int(os.getenv("MAX_MODEL_LEN", "131072"))
    cap = int(_env_line("VLLM_MAX_THINK_CHARS") or "120000")
    calls = [c for c in cut_calls(args.events, since=args.since, route=args.route)
             if (args.prompts_dir / f"{c['prompt']}.txt").exists()][: args.n]
    if not calls:
        print(f"no cut {args.route} calls since {args.since} with a retained prompt")
        return 2
    print(f"model={model} strength={strength} cap-under-study={cap:,} "
          f"route={args.route} since={args.since} calls={len(calls)}", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bodies").mkdir(exist_ok=True)
    client = OpenAI(base_url=os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1"),
                    api_key="vllm", timeout=7200)
    rows = []
    with (args.out / "calls.jsonl").open("a", encoding="utf-8") as sink:
        for i, c in enumerate(calls, 1):
            prompt = (args.prompts_dir / f"{c['prompt']}.txt").read_text(
                encoding="utf-8", errors="replace")
            messages = []
            sys_path = args.prompts_dir / f"{c['system']}.txt"
            if c.get("system") and sys_path.exists():
                messages.append({"role": "system",
                                 "content": sys_path.read_text(encoding="utf-8")})
            messages.append({"role": "user", "content": prompt})
            max_tokens = ctx - len(prompt) // 3 - 2048
            r = ask(client, model=model, messages=messages, max_tokens=max_tokens,
                    strength=strength, rep=rep)
            row = {"prompt_sha": c["prompt"], "run": c["run"], "ts": c["ts"],
                   "caller": c["caller"], "prompt_chars": len(prompt),
                   "cut_at": c["cut_at"], "reasoning_chars": len(r["reasoning"]),
                   "content_chars": len(r["content"]), "finish": r["finish"],
                   "prompt_tokens": r["prompt_tokens"], "max_tokens": max_tokens,
                   "seconds": round(r["seconds"], 1)}
            rows.append(row)
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            stem = args.out / "bodies" / c["prompt"][:16]
            stem.with_suffix(".reasoning.txt").write_text(r["reasoning"], encoding="utf-8")
            stem.with_suffix(".content.txt").write_text(r["content"], encoding="utf-8")
            print(f"  [{i}/{len(calls)}] {c['prompt'][:12]} chars={len(prompt):,} "
                  f"cut_at={c['cut_at']} -> reasoning={row['reasoning_chars']:,} "
                  f"content={row['content_chars']:,} finish={r['finish']} "
                  f"{row['seconds']:.0f}s", flush=True)
    lens = sorted(r["reasoning_chars"] for r in rows)
    q = lambda f: lens[min(len(lens) - 1, int(f * len(lens)))]
    print(f"\nuncapped reasoning over {len(lens)} cut prompts: "
          f"med={q(0.5):,} p90={q(0.9):,} max={lens[-1]:,}")
    for x in (150_000, 200_000, 250_000, 300_000):
        ok = sum(1 for r in rows if r["finish"] == "stop" and r["content_chars"]
                 and r["reasoning_chars"] <= x)
        print(f"  converged-under({x:,}) = {ok}/{len(rows)}")
    print(f"  runaway (finish=length) = {sum(1 for r in rows if r['finish'] == 'length')}/{len(rows)}")
    print(f"raw: {args.out / 'calls.jsonl'}; bodies: {args.out / 'bodies'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
