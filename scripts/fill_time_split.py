#!/usr/bin/env python3
"""Where one TDD fill's wall clock went: model streams, judging, idle workers.

WHY. Run `336559-eb0222a5` (2026-11-07/08, cluster `stable`, oracle 433) spent
4h06m and 3h42m on fills 1 and 2 for +8 tests each. The question "where do
the four hours go" had three candidate owners already in the tracker: the
reasoning level of every call, the serial judging of a keep once failing
binaries abort, and workers idle between claims. The per-unit events carry
all three, but only joined: a unit's interval (`phase_c.fn.fill.start` to
`.done`/`.fail` on the same worker thread), the model calls inside it
(`llm.response` duration_ms joined to `llm.stream_meta` by thread and prompt
sha), and the gaps on each worker between units.

USAGE.
    python scripts/fill_time_split.py <run_id> --start ISO --end ISO
        [--events logs/events.jsonl] [--workers 4] [--units]

    --start/--end bound the fill (UTC, e.g. 2026-11-08T01:55:00); take them
    from `phase_c.fill.schedule` and `phase_c.cluster.done`, or from the
    `[phase-c/schedule] fill N:` line and the next `iter N+1: running`.
    Offline: reads the events file only, no GPU, no cargo.

HOW TO INTERPRET THE OUTPUT.
    calls / stream-min / completion tok / tok/s per stream: the model half.
        stream-min is summed per call, so it can exceed wall minutes when
        streams overlap. tok/s per stream is the decode rate one call saw.
    content median / reasoning median (chars): a tiny content median behind a
        large reasoning median means the minutes are thinking, not answer;
        that is the reasoning-level lever.
    unit-min / of which llm / of which other: worker minutes inside units.
        "other" is everything a unit spends outside a model stream: compile,
        the keep's test readings, waiting on the splice lock. When it grows
        through the fill, judging, not the model, is the cost.
    worker-min capacity / idle: workers x fill wall minus unit-min. Idle is
        a worker with no unit claimed (a parked claim, a level join, a
        ground re-read before a claim, the budget's tail).
    ideal wall at this model cost: stream-min / workers, i.e. the fill if
        judging and idle were free. The gap to the real wall is what a
        judging or scheduling fix can buy; stream-min itself is what a
        reasoning-level fix can buy.
    --units prints each unit: minutes in the unit and in model streams.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from pathlib import Path


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--units", action="store_true")
    a = ap.parse_args()
    start = _ts(a.start if "+" in a.start else a.start + "+00:00")
    end = _ts(a.end if "+" in a.end else a.end + "+00:00")

    meta: dict[tuple[str, str], dict] = {}
    calls: list[tuple[str, dt.datetime, float, dict]] = []
    units: list[tuple[str, dt.datetime, dt.datetime, str, str]] = []
    open_: dict[str, tuple[dt.datetime, str]] = {}
    with Path(a.events).open() as fh:
        for line in fh:
            if a.run_id not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("run_id") != a.run_id:
                continue
            t = _ts(e["ts"])
            if not (start <= t <= end):
                continue
            k, d, th = e.get("event"), e.get("data") or {}, e.get("thread", "")
            if k == "llm.stream_meta":
                meta[(th, d.get("prompt_sha", ""))] = d
            elif k == "llm.response":
                m = meta.get((th, d.get("prompt_sha", "")), {})
                calls.append((th, t, e.get("duration_ms", 0) / 1000, m))
            elif k == "phase_c.fn.fill.start":
                open_[th] = (t, f"{d.get('file')}::{d.get('fn')}")
            elif k in ("phase_c.fn.fill.done", "phase_c.fn.fill.fail") and th in open_:
                s, u = open_.pop(th)
                units.append((th, s, t, u, k.rsplit(".", 1)[-1]))

    stream_s = sum(c[2] for c in calls)
    comp = sum(c[3].get("completion_tokens") or 0 for c in calls)
    content = [c[3].get("content_chars") or 0 for c in calls if c[3]]
    reason = [c[3].get("reasoning_chars") or 0 for c in calls if c[3]]
    wall_min = (end - start).total_seconds() / 60
    unit_min = sum((u[2] - u[1]).total_seconds() for u in units) / 60
    llm_in_units = 0.0
    rows = []
    for th, s, t, u, how in units:
        llm = sum(c[2] for c in calls if c[0] == th and s <= c[1] <= t) / 60
        llm_in_units += llm
        rows.append((s, th, u, how, (t - s).total_seconds() / 60, llm))
    cap = a.workers * wall_min
    print(f"run {a.run_id} fill {start:%H:%M}-{end:%H:%M} UTC wall {wall_min:.1f} min")
    print(f"calls {len(calls)} stream-min {stream_s / 60:.1f} completion tok {comp}"
          f" tok/s per stream {comp / stream_s if stream_s else 0:.1f}")
    if content:
        print(f"content median {statistics.median(content):.0f} chars,"
              f" reasoning median {statistics.median(reason):.0f} chars")
    print(f"units {len(units)} unit-min {unit_min:.1f} of which llm {llm_in_units:.1f}"
          f" other {unit_min - llm_in_units:.1f}")
    print(f"worker-min capacity {cap:.1f} idle {cap - unit_min:.1f}"
          f" ({100 * (cap - unit_min) / cap if cap else 0:.0f}%)")
    print(f"ideal wall at this model cost {stream_s / 60 / a.workers:.1f} min")
    if a.units:
        for s, th, u, how, um, llm in sorted(rows):
            print(f"  {s:%H:%M:%S} {th[-2:]} {u} {how} unit {um:.1f} llm {llm:.1f}"
                  f" other {um - llm:.1f}")


if __name__ == "__main__":
    main()
