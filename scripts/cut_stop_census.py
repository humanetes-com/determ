#!/usr/bin/env python3
"""WHY. The run-level LLM cut stop (`domain/reasoning_cap.py::CutRate`) is
configured as a SHARE of a window — `.env` had `LLM_CUT_STOP_SHARE=4`,
`_WINDOW=100`, `_MIN_CALLS=25` — and takes its verdict from call 25, where
the denominator is still 25 and the smallest non-zero share it can compute is
4%. Run `190214-8de7c7a5` (2026-09-22) was stopped by ONE cut call: `calls=25
(of 25 seen) cut=1 share=4.0%`, which ended a per-fn fill at `0 call(s)` and
cost a relaunch. Sizing that stop is a claim about which readings it may
prevent, so it needs the recorded distribution rather than a guess: how many
cut calls a HEALTHY run carries early, and how many runs each candidate rule
would have stopped.

The rows are `llm.stream_meta` events, folded per run in emission order with
the pipeline's own `is_cut`. Two caveats, both conservative in the same
direction: the events carry no route, so the pipeline's bookkeeping calls
(the notes extractor), which the live fold excludes, are counted here — the
cut counts below are an upper bound; and a run that spans a log rotation is
still one `run_id`, so its calls are complete.

USAGE

    .venv/bin/python scripts/cut_stop_census.py [--events logs/events.jsonl]
                                                [--window 100] [--share 4]
                                                [--min-calls 25] [--since YYYY-MM-DD]

No GPU, no vLLM, no pipeline state: it reads the event log and prints. On the
full log (~284 MB) it takes about a minute.

HOW TO INTERPRET THE OUTPUT

`runs` is how many runs made at least one main-stream call. `cut/calls` is
the whole corpus. Then, per candidate rule, the runs it would have STOPPED
and the call index it would have fired at:

- `share+floor` is the shipped rule at the given knobs (`share >= SHARE`
  from call `MIN_CALLS` on, over the last `WINDOW` marks). This is the one
  that fired on 1/25.
- `count` is the candidate: fire when the number of cut calls in the last
  `WINDOW` reaches `ceil(SHARE% x WINDOW)` — 4 cut calls at 4%/100 — with no
  floor, because the count is its own floor.

`stopped_with_healthy_window` is the number the decision rests on: runs a
rule stops whose cut rate over their first `WINDOW` calls is BELOW the
configured share. Those are the false stops — the rule ended a run that was
not the run the stop exists for. A rule that shows 0 there and still stops
the genuinely cut runs is the one to ship. `median_fire_index` says how much
GPU a true stop costs before it fires: lower is cheaper, and a rule that
only fires after 100 calls on a run that was cut from call 1 is paying for
the diagnosis it already had.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.reasoning_cap import is_cut  # noqa: E402


def _fires_share(marks: list[bool], window: int, share: float,
                 min_calls: int) -> int | None:
    """1-based index of the call the shipped rule fires at, or None."""
    for i in range(len(marks)):
        seen = i + 1
        if seen < min_calls:
            continue
        w = marks[max(0, seen - window):seen]
        if w and 100.0 * sum(w) / len(w) >= share:
            return seen
    return None


def _fires_count(marks: list[bool], window: int, threshold: int) -> int | None:
    """1-based index of the call the count rule fires at, or None."""
    for i in range(len(marks)):
        w = marks[max(0, i + 1 - window):i + 1]
        if sum(w) >= threshold:
            return i + 1
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--window", type=int, default=100)
    ap.add_argument("--share", type=float, default=4.0)
    ap.add_argument("--min-calls", type=int, default=25)
    ap.add_argument("--since", default="")
    args = ap.parse_args()

    by_run: dict[str, list[bool]] = defaultdict(list)
    first_ts: dict[str, str] = {}
    with open(args.events, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"llm.stream_meta"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") != "llm.stream_meta":
                continue
            ts = str(ev.get("ts") or "")
            if args.since and ts[:10] < args.since:
                continue
            run = str(ev.get("run_id") or "?")
            # `followup=True` on a stream row says the MAIN stream was cut
            # and a forced completion ran — `is_cut` counts it, so dropping
            # those rows drops exactly the cuts (1 of 10,954 "cut calls" on
            # the first run of this script, against 1,810 of 12,002 on the
            # record `CutRate`'s docstring quotes).
            by_run[run].append(is_cut(ev.get("data") or {}))
            first_ts.setdefault(run, ts)

    threshold = max(1, math.ceil(args.share * args.window / 100.0))
    calls = sum(len(m) for m in by_run.values())
    cut = sum(sum(m) for m in by_run.values())
    print(f"events={args.events} since={args.since or 'all'} "
          f"runs={len(by_run)} calls={calls} cut={cut} "
          f"({100.0 * cut / max(1, calls):.1f}%)")
    print(f"knobs: share={args.share}% window={args.window} "
          f"min_calls={args.min_calls} -> count threshold={threshold}")

    rules = {
        "share+floor": lambda m: _fires_share(
            m, args.window, args.share, args.min_calls),
        "count": lambda m: _fires_count(m, args.window, threshold),
    }
    for name, fire in rules.items():
        stopped: list[tuple[str, int, int, int]] = []
        for run, marks in by_run.items():
            at = fire(marks)
            if at is None:
                continue
            head = marks[:args.window]
            stopped.append((run, at, sum(head), len(head)))
        healthy = [r for r in stopped
                   if 100.0 * r[2] / max(1, r[3]) < args.share]
        idx = [r[1] for r in stopped]
        print(f"\n{name}: stopped {len(stopped)}/{len(by_run)} run(s); "
              f"stopped_with_healthy_window {len(healthy)}; "
              f"median_fire_index "
              f"{int(statistics.median(idx)) if idx else 0}")
        for run, at, hc, hn in sorted(stopped, key=lambda r: r[1])[:12]:
            flag = "  <- healthy window" if 100.0 * hc / max(1, hn) < args.share else ""
            print(f"    {first_ts.get(run, '')[:16]} {run:>20} fires at call "
                  f"{at:>4}; first {hn} call(s) carry {hc} cut{flag}")

    # The distribution the floor is really about: cut calls in the first
    # `window` of every run, so "how many healthy runs carry 1-3 early cuts"
    # is answered without reference to any rule.
    heads = sorted(sum(m[:args.window]) for m in by_run.values())
    dist: dict[int, int] = defaultdict(int)
    for h in heads:
        dist[min(h, 10)] += 1
    print(f"\ncut calls in each run's first {args.window}: "
          + ", ".join(f"{k if k < 10 else '10+'}:{dist[k]}"
                      for k in sorted(dist)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
