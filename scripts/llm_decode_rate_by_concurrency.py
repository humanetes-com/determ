#!/usr/bin/env python3
"""Per-call decode rate of the served model, bucketed by how many calls
were in flight beside it.

WHY. Entries 398/399 of the trackers (2026-11-07, run 336559-eb0222a5)
predict that putting task 0d's serial model calls on 4 lanes cuts each
stage's wall "by roughly the conc ratio". That holds only if one call's
tokens per second stays near its serial rate when 3 others share the
server. The book build already runs at conc 3.4-3.8 next to stages that
run at 1.00, so the record can answer it without a launch.

USAGE.
    python scripts/llm_decode_rate_by_concurrency.py [--since YYYY-MM-DD]
        [--product NAME] [--model SUBSTR] [--min-tokens 500]
        [--effort medium|max|-] [--events logs/events.jsonl]
--effort keeps only calls sent at that reasoning level (`llm.stream_meta`
`effort`; `-` = rows recorded before the level existed, all at the maximum).
A level changes reasoning length, not the decode rate, so read one level at
a time when the record mixes them.
Reads only. No GPU, no vLLM, no cargo. Joins llm.response (duration_ms,
prompt_sha) with llm.stream_meta (completion_tokens, prompt_tokens) by
(run, prompt_sha, nearest ts). A call's interval is [ts - duration, ts];
the wait for a server slot (`llm.slot` wait_ms, same thread and prompt
sha) is taken off its start; its concurrency is the time-weighted mean number of calls in flight over
that interval, itself included, rounded to the nearest integer bucket.

HOW TO INTERPRET. One row per bucket:
    calls        calls in the bucket (completion_tokens >= --min-tokens)
    med_tok_s    median completion tokens per second of one call
    p25 / p75    quartiles of the same
    agg_tok_s    med_tok_s x bucket: the server's throughput at that width
    speedup      agg_tok_s / the bucket-1 median: what a stage gains in
                 wall by running that many calls at once
Decision rule: a lane plan's predicted wall saving is the speedup at the
width it will run, not the width. If speedup(4) is near 4, "walls drop by
the conc ratio" holds; if it is near 2, they drop by half. Prefill is in
the duration too: compare buckets of similar prompt size (med_prompt).
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _t(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--since", default="2026-10-26")
    ap.add_argument("--product", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--min-tokens", type=int, default=500)
    ap.add_argument("--effort", default=None)
    a = ap.parse_args()

    resp: dict[str, list[tuple[float, float, str, str]]] = defaultdict(list)
    meta: dict[tuple[str, str], list[tuple[float, dict]]] = defaultdict(list)
    waits: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for line in a.events.open():
        if ('"llm.response"' not in line and '"llm.stream_meta"' not in line
                and '"llm.slot"' not in line):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        run = e.get("run_id")
        if not run or e.get("ts", "") < a.since:
            continue
        if a.product and e.get("product") != a.product:
            continue
        d = e.get("data") or {}
        sha = d.get("prompt_sha")
        if not sha:
            continue
        ts = _t(e["ts"])
        if e["event"] == "llm.slot":
            waits[(run, str(e.get("tid") or ""), sha)].append((d.get("wait_ms") or 0) / 1000.0)
            continue
        if e["event"] == "llm.response":
            key = (run, str(e.get("tid") or ""), sha)
            wait = waits[key].pop(0) if waits.get(key) else 0.0
            dur = (e.get("duration_ms") or 0) / 1000.0 - wait
            if dur > 0:
                resp[run].append((ts - dur, ts, sha, e.get("in_phase") or ""))
        elif not d.get("bookkeeping"):
            if a.model and a.model not in str(d.get("model", "")):
                continue
            if a.effort and str(d.get("effort") or "-") != a.effort:
                continue
            meta[(run, sha)].append((ts, d))

    rows: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for run, calls in resp.items():
        for s, e, sha, _ph in calls:
            cands = meta.get((run, sha))
            if not cands:
                continue
            _mts, d = min(cands, key=lambda c: abs(c[0] - e))
            tok = int(d.get("completion_tokens") or 0)
            if tok < a.min_tokens:
                continue
            # time-weighted in-flight count over [s, e]
            pts = sorted({s, e, *(x for o in calls for x in o[:2] if s < x < e)})
            acc = 0.0
            for lo, hi in zip(pts, pts[1:], strict=False):
                mid = (lo + hi) / 2
                acc += (hi - lo) * sum(1 for o in calls if o[0] <= mid < o[1])
            conc = acc / (e - s)
            rows[max(1, round(conc))].append((tok / (e - s), int(d.get("prompt_tokens") or 0)))

    base = statistics.median(r for r, _ in rows[1]) if rows.get(1) else None
    print(f"since={a.since} product={a.product or 'any'} min_tokens={a.min_tokens} "
          f"effort={a.effort or 'any'}")
    print(f"{'bucket':>6} {'calls':>6} {'med_tok_s':>9} {'p25':>6} {'p75':>6} "
          f"{'agg_tok_s':>9} {'speedup':>7} {'med_prompt':>10}")
    for b in sorted(rows):
        rs = sorted(r for r, _ in rows[b])
        q = statistics.quantiles(rs, n=4) if len(rs) >= 4 else [rs[0], rs[len(rs) // 2], rs[-1]]
        med = statistics.median(rs)
        agg = med * b
        sp = f"{agg / base:7.2f}" if base else "      -"
        print(f"{b:6d} {len(rs):6d} {med:9.1f} {q[0]:6.1f} {q[-1]:6.1f} {agg:9.1f} {sp} "
              f"{int(statistics.median(p for _, p in rows[b])):10d}")


if __name__ == "__main__":
    main()
