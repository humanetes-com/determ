"""Measure a model's reasoning-length economics on real, retained prompts.

Why this tool exists: `VLLM_MAX_THINK_CHARS=60000` was sized inside a measured
EMPTY BAND of qwen reasoning lengths (36,570-69,830), where a cap separates
healthy calls from runaways. On the muse anyhow run `298540-f792975a`
(2026-08-12) both sizing properties failed: median reasoning was 26,461 chars,
p90 was 60,001 — the cap WAS the p90 — 27 of 187 calls (14.4%) hit it, and
30.1% of the run's call-hours were spent inside capped streams. A cap inside a
populated region is a tax, not a separator. The muse model card also ships no
sampling defaults, so vLLM serves it at temperature 1.0 with no nucleus
truncation — the plausible mechanism for reasoning that rambles to a cap
instead of converging. `domain/model_defaults.py` deliberately carries no
sampling pin until a measurement justifies one; this tool IS that measurement.

What it does: replays a fixed set of retained prompts (every `llm.request` is
content-addressed into `logs/prompts/<sha256>.txt`; this tool selects the
anyhow-scale ones from `events.jsonl`) against the live vLLM server, across a
grid of reasoning strengths x sampling settings, with a fixed request seed and
NO client-side think cap — the point is the natural length distribution, from
which a model-scoped cap can be derived. Per cell it reports the reasoning
length distribution, the rate at which the CURRENT cap would have fired, and
throughput. Response bodies are saved per call so fill quality can be judged
later by splicing them; this tool deliberately does not run cargo.

Run it only when nothing else is on the GPU — it is a measurement, and a
concurrent pipeline both skews it and is skewed by it. Serial by design for
the same reason.

Usage:
    .venv/bin/python tools/measure_reasoning_economics.py \
        --events logs/events.jsonl --prompts-dir logs/prompts \
        --n 10 --min-prompt-chars 60000 \
        --strengths high,medium,low \
        --pin-temperature 0.7 --pin-top-p 0.8 \
        --out logs/reasoning_econ
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Cell:
    """One grid cell: a reasoning strength x a sampling setting."""
    strength: str
    sampling: str                    # "as-served" | "pinned"
    temperature: float | None
    top_p: float | None

    @property
    def name(self) -> str:
        return f"{self.strength}/{self.sampling}"


@dataclass(slots=True)
class CallRecord:
    prompt_sha: str
    cell: str
    reasoning_chars: int
    content_chars: int
    seconds: float
    finish_reason: str | None
    prompt_tokens: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_sha": self.prompt_sha, "cell": self.cell,
            "reasoning_chars": self.reasoning_chars,
            "content_chars": self.content_chars,
            "seconds": round(self.seconds, 1),
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
        }


@dataclass(slots=True)
class CellSummary:
    records: list[CallRecord] = field(default_factory=list)

    def line(self, name: str, current_cap: int) -> str:
        lens = sorted(r.reasoning_chars for r in self.records)
        if not lens:
            return f"{name:18s}  (no calls)"
        med = statistics.median(lens)
        p90 = lens[max(0, int(len(lens) * 0.9) - 1)]
        would_cap = sum(1 for n in lens if n >= current_cap)
        med_s = statistics.median(r.seconds for r in self.records)
        return (f"{name:18s}  n={len(lens):2d}  reasoning med={med:,.0f} "
                f"p90={p90:,} max={lens[-1]:,}  "
                f"would-hit-cap({current_cap:,})={would_cap}/{len(lens)}  "
                f"med={med_s:.0f}s/call")


def select_prompts(events: Path, prompts_dir: Path, *, n: int,
                   min_chars: int) -> list[tuple[str, str]]:
    """The fixed prompt set: first N distinct retained prompts of at least
    `min_chars`, in event order (deterministic for a given events file)."""
    picked: list[tuple[str, str]] = []
    seen: set[str] = set()
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "llm.request" not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") != "llm.request":
                continue
            meta = (ev.get("data") or {}).get("prompt") or {}
            sha = str(meta.get("sha256") or "")
            if not sha or sha in seen or int(meta.get("char_count") or 0) < min_chars:
                continue
            path = prompts_dir / f"{sha}.txt"
            if not path.exists():
                continue
            seen.add(sha)
            picked.append((sha, path.read_text(encoding="utf-8",
                                               errors="replace")))
            if len(picked) >= n:
                break
    return picked


def run_one(client: Any, *, model: str, prompt: str, cell: Cell, seed: int,
            max_tokens: int, rep_penalty: float) -> tuple[int, int, float,
                                                          str | None,
                                                          int | None, str]:
    """One streamed call; returns (reasoning_chars, content_chars, seconds,
    finish_reason, prompt_tokens, content). Mirrors `VLLMTransport.stream`'s
    request shape (chat_template_kwargs dialect, repetition_penalty, usage
    chunk) so the measurement describes the calls the pipeline actually
    makes — minus the think cap, which is exactly the variable under study."""
    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {"reasoning_strength": cell.strength},
        "repetition_penalty": rep_penalty,
        "seed": seed,
    }
    kwargs: dict[str, Any] = {}
    if cell.temperature is not None:
        kwargs["temperature"] = cell.temperature
    if cell.top_p is not None:
        kwargs["top_p"] = cell.top_p
    t0 = time.monotonic()
    reasoning = content = ""
    finish: str | None = None
    ptok: int | None = None
    with client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens, extra_body=extra_body, stream=True,
        stream_options={"include_usage": True}, **kwargs,
    ) as stream:
        for chunk in stream:
            if chunk.usage and chunk.usage.prompt_tokens:
                ptok = chunk.usage.prompt_tokens
            if not chunk.choices:
                continue
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
            delta = chunk.choices[0].delta
            r = (getattr(delta, "reasoning", None)
                 or getattr(delta, "reasoning_content", None))
            if r:
                reasoning += r
            if delta.content:
                content += delta.content
    return (len(reasoning), len(content), time.monotonic() - t0, finish,
            ptok, content)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--prompts-dir", type=Path, default=Path("logs/prompts"))
    ap.add_argument("--out", type=Path, default=Path("logs/reasoning_econ"))
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--min-prompt-chars", type=int, default=60000)
    ap.add_argument("--strengths", default="high,medium,low")
    ap.add_argument("--pin-temperature", type=float, default=0.7)
    ap.add_argument("--pin-top-p", type=float, default=0.8)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--max-tokens", type=int, default=32768)
    args = ap.parse_args()

    from openai import OpenAI
    base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
    model = os.getenv("VLLM_MODEL_NAME") or os.getenv("MODEL") or ""
    if not model:
        print("Set MODEL or VLLM_MODEL_NAME (the served model id).")
        return 2
    # The served model's resolved knobs (env → profile → default), the values
    # the transport sends; this tool's own defaults (60000 / 1.05) had
    # drifted from both the transport's and the profile's.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline_core.domain.model_profile import resolve_model_knobs
    knobs = resolve_model_knobs(os.environ, model)
    current_cap = knobs.max_think_chars
    rep_penalty = knobs.repetition_penalty

    prompts = select_prompts(args.events, args.prompts_dir, n=args.n,
                             min_chars=args.min_prompt_chars)
    if not prompts:
        print(f"No retained prompts of >= {args.min_prompt_chars:,} chars "
              f"found via {args.events} + {args.prompts_dir}.")
        return 2
    cells = [Cell(s, "as-served", None, None)
             for s in args.strengths.split(",")]
    cells += [Cell(s, "pinned", args.pin_temperature, args.pin_top_p)
              for s in args.strengths.split(",")]
    total = len(cells) * len(prompts)
    print(f"model={model}  prompts={len(prompts)} (fixed set, seed={args.seed})"
          f"  cells={len(cells)}  calls={total}  cap-under-study={current_cap:,}")

    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / "calls.jsonl"
    client = OpenAI(base_url=base_url, api_key="vllm", timeout=1800)
    summaries: dict[str, CellSummary] = {c.name: CellSummary() for c in cells}
    done = 0
    with results.open("a", encoding="utf-8") as sink:
        for cell in cells:
            for sha, prompt in prompts:
                done += 1
                r_len, c_len, secs, finish, ptok, content = run_one(
                    client, model=model, prompt=prompt, cell=cell,
                    seed=args.seed, max_tokens=args.max_tokens,
                    rep_penalty=rep_penalty)
                rec = CallRecord(sha, cell.name, r_len, c_len, secs, finish,
                                 ptok)
                summaries[cell.name].records.append(rec)
                sink.write(json.dumps(rec.as_dict()) + "\n")
                sink.flush()
                body_dir = args.out / "bodies" / cell.name.replace("/", "_")
                body_dir.mkdir(parents=True, exist_ok=True)
                (body_dir / f"{sha[:16]}.txt").write_text(
                    content, encoding="utf-8")
                print(f"  [{done}/{total}] {cell.name:18s} {sha[:12]}  "
                      f"reasoning={r_len:,} content={c_len:,} {secs:.0f}s "
                      f"finish={finish}")
    print("\nPer-cell summary (derive the model-scoped cap from the max of a "
          "healthy cell, not from another model's empty band):")
    for cell in cells:
        print("  " + summaries[cell.name].line(cell.name, current_cap))
    print(f"\nRaw records: {results}; response bodies under {args.out}/bodies/ "
          "(judge fill quality separately by splicing them).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
