"""Reasoning cost per phase, from the LLM stream records of one or more runs.

WHY: deciding whether a phase should call the model with thinking off needs
the phase's own numbers: how many calls, how much of each reply is reasoning
against content, and the wall the calls took. `llm.stream_meta` carries the
reasoning/content split per call and its `in_phase`; `llm.response` carries
`duration_ms`. The two are joined by order within a thread (a request, its
stream meta and its response are written by the calling thread in that order).

USAGE:
    .venv/bin/python scripts/measure_phase_reasoning.py RUN_ID [RUN_ID ...] \
        [--events logs/events.jsonl] [--phase-prefix t0d]

HOW TO INTERPRET: one line per (run, phase). `reasoning/content` is the ratio
of reasoning characters to answer characters; a phase whose answer is a short
structured reply (rules, verdicts, phrases) with a ratio in the tens is paying
mostly for thinking. `minutes` is the summed call duration (calls run on lanes,
so it can exceed the phase wall). `thinking on` counts the calls that ran with
thinking enabled.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--phase-prefix", default="")
    args = ap.parse_args()
    runs = set(args.runs)
    metas: dict[tuple[str, str], list[dict]] = defaultdict(list)
    durations: dict[tuple[str, str], list[float]] = defaultdict(list)
    pending: dict[tuple[str, int], str] = {}
    with Path(args.events).open(encoding="utf-8") as fh:
        for line in fh:
            if '"llm.' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            run = ev.get("run_id")
            if run not in runs:
                continue
            phase = str(ev.get("in_phase") or "")
            if not phase.startswith(args.phase_prefix):
                continue
            kind = ev.get("event")
            if kind == "llm.stream_meta":
                metas[(run, phase)].append(ev.get("data") or {})
            elif kind == "llm.request":
                pending[(run, int(ev.get("tid") or 0))] = phase
            elif kind == "llm.response":
                ph = pending.pop((run, int(ev.get("tid") or 0)), phase)
                durations[(run, ph)].append(float(ev.get("duration_ms") or 0) / 60000.0)
    for key in sorted(set(metas) | set(durations)):
        run, phase = key
        ms = [m for m in metas.get(key, []) if not m.get("bookkeeping")]
        reasoning = [int(m.get("reasoning_chars") or 0) for m in ms]
        content = [int(m.get("content_chars") or 0) for m in ms]
        on = sum(1 for m in ms if m.get("enable_thinking"))
        mins = durations.get(key, [])
        ratio = (sum(reasoning) / sum(content)) if sum(content) else float("inf")
        print(f"{run} {phase}: calls {len(ms)} (thinking on {on}), "
              f"reasoning p50 {statistics.median(reasoning) if reasoning else 0:.0f} "
              f"chars, content p50 {statistics.median(content) if content else 0:.0f} chars, "
              f"reasoning/content {ratio:.1f}, minutes {sum(mins):.1f} "
              f"(p50 {statistics.median(mins) if mins else 0:.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
