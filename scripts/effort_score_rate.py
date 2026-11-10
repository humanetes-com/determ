#!/usr/bin/env python3
"""Score rate of task 2a runs side by side, so two reasoning levels can be compared.

WHY. On 2026-11-07 the reasoning level stopped being one process-wide maximum
(Qwen `xhigh`) and became a per-call level, `medium` by default
(`LLM_EFFORT`, `domain/effort_policy.py`). A maximum-level call is several
times slower, so a maximum run is slower per call; the user's impression on
2026-11-09 was that it nonetheless SCORED faster. "Slower" and "scores faster"
are two different denominators: minutes per call, and tests gained per hour of
fill. The events already hold both; they only need joining per fill. Runs
compared on the first reading: `336559-eb0222a5` (xhigh, t2a fills 1-2),
`3323407-984b07f2` (medium, resumed on 336559's tree), `630503-f5ca6540`,
`2833915-23e0c633`, `4053951-cf38fd3a` (medium, oracle 424 tree).

USAGE.
    python scripts/effort_score_rate.py RUN_ID [RUN_ID ...]
        [--events logs/events.jsonl] [--cluster stable] [--curve]

    Offline: reads the events file only (one pass), no GPU, no cargo. A run
    still live is read up to its last event; its open fill is printed as
    `open` with the latest keep reading as its "after".

HOW TO INTERPRET THE OUTPUT.
    header per run: product, oracle (the denominator; two runs on different
        oracles were built by different construction and are not the same
        problem), the level mix of every t2a call (`effort` on
        llm.stream_meta; `xhigh` is the maximum on Qwen), and the tree_sha of
        the entry sweep (equal shas = same starting tree).
    one row per fill (sweep N -> sweep N+1): wall minutes, calls,
        stream-min (summed llm.response durations; exceeds wall when calls
        overlap), completion tokens, pass before -> after (pass_count, the
        per-world count the iter line prints), +tests, tests per wall hour,
        tests per stream hour, tests per 100k completion tokens.
    totals: the same over all fills of the run, and the t2a wall from the
        first fill's start.
    --curve: every keep reading (phase_c.fill.reading) as minutes since t2a
        entry and pass_count, so two runs can be overlaid on one time axis.

    The decision rule. "Max scores faster" is true only if, from the SAME
    entry tree (same tree_sha, same oracle, same code), the max run's tests
    per wall hour is higher. Rows from different trees, different starting
    pass or different code measure the tree, not the level: on a tree where
    the remaining reds are blocked by still-stubbed siblings, no level moves
    them, and a fill that starts on the producers cashes at any level.
    Tests per stream hour and per 100k tokens separate "the model answered
    better per unit of thinking" from "the fill had cheaper work".
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _load(path: Path, runs: set[str], cluster: str) -> dict[str, dict]:
    out: dict[str, dict] = {
        r: {"iters": [], "readings": [], "calls": [], "effort": collections.Counter(),
            "fill_enter": None, "product": "", "sha": "", "comp": {}}
        for r in runs
    }
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"run_id"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            r = ev.get("run_id")
            if r not in out:
                continue
            rec = out[r]
            kind = ev.get("event", "")
            d = ev.get("data") or {}
            rec["product"] = ev.get("product", rec["product"])
            rec["sha"] = (ev.get("pipeline_sha") or rec["sha"])[:8]
            in_t2a = str(ev.get("in_phase", "")).startswith("t2a")
            if kind == "phase.enter" and ev.get("phase") == "t2a.fill" and rec["fill_enter"] is None:
                rec["fill_enter"] = _ts(ev["ts"])
            elif kind == "phase_c.tdd.iter" and (d.get("cluster") or cluster) == cluster:
                rec["iters"].append((_ts(ev["ts"]), d))
            elif kind == "phase_c.fill.reading" and (ev.get("cluster") or cluster) == cluster:
                rec["readings"].append((_ts(ev["ts"]), int(ev.get("pass_count") or 0)))
            elif kind == "llm.response" and in_t2a:
                rec["calls"].append((_ts(ev["ts"]), float(ev.get("duration_ms") or 0) / 60000.0,
                                     d.get("prompt_sha")))
            elif kind == "llm.stream_meta" and in_t2a:
                # Runs before the level split record no `effort`; their
                # reasoning_payload still carries the word that was sent.
                payload = d.get("reasoning_payload") or {}
                level = d.get("effort") or payload.get("reasoning_effort") or payload.get("reasoning_strength")
                rec["effort"][level or "unrecorded"] += 1
                rec["comp"][(d.get("prompt_sha"), ev.get("thread"))] = int(d.get("completion_tokens") or 0)
    return out


def _rate(gain: float, minutes: float) -> str:
    return f"{gain * 60.0 / minutes:6.1f}" if minutes > 0 else "     -"


def _report(run: str, rec: dict, curve: bool) -> None:
    iters = rec["iters"]
    if not iters:
        print(f"\n{run}: no phase_c.tdd.iter event in this cluster")
        return
    first = iters[0][1]
    print(f"\n{run}  product={rec['product']} code={rec['sha']} oracle={first.get('oracle_size')} "
          f"entry tree={first.get('tree_sha')} levels={dict(rec['effort'])}")
    comp_by_sha: dict[str, int] = collections.defaultdict(int)
    for (sha, _thread), tok in rec["comp"].items():
        comp_by_sha[sha] += tok
    print("  fill  wall-min calls stream-min  compl-tok  pass before->after   +tests  /wall-h /stream-h /100k-tok")
    bounds = [(t, int(d.get("pass_count") or 0)) for t, d in iters]
    last_t = max([t for t, _ in rec["readings"]] + [t for t, _, _ in rec["calls"]] + [bounds[-1][0]])
    if rec["readings"] and rec["readings"][-1][0] > bounds[-1][0]:
        bounds.append((last_t, rec["readings"][-1][1]))
        open_tail = True
    else:
        open_tail = False
    tot = collections.Counter()
    for i in range(len(bounds) - 1):
        (t0, p0), (t1, p1) = bounds[i], bounds[i + 1]
        calls = [c for c in rec["calls"] if t0 <= c[0] < t1]
        smin = sum(c[1] for c in calls)
        tok = sum(comp_by_sha.get(c[2], 0) for c in calls)
        wall = (t1 - t0).total_seconds() / 60.0
        gain = p1 - p0
        label = "open" if open_tail and i == len(bounds) - 2 else str(i + 1)
        per_tok = f"{gain * 1e5 / tok:8.1f}" if tok else "       -"
        print(f"  {label:>4} {wall:9.1f} {len(calls):5d} {smin:10.1f} {tok:10d}  {p0:6d} -> {p1:<6d}   {gain:+6d}  "
              f"{_rate(gain, wall)}   {_rate(gain, smin)}  {per_tok}")
        tot.update({"wall": wall, "calls": len(calls), "smin": smin, "tok": tok, "gain": gain})
    start = rec["fill_enter"] or bounds[0][0]
    span = (bounds[-1][0] - start).total_seconds() / 60.0
    print(f"  all  {tot['wall']:9.1f} {tot['calls']:5d} {tot['smin']:10.1f} {tot['tok']:10d}  "
          f"{bounds[0][1]:6d} -> {bounds[-1][1]:<6d}   {tot['gain']:+6d}  {_rate(tot['gain'], tot['wall'])}   "
          f"{_rate(tot['gain'], tot['smin'])}   (t2a wall from fill entry {span:.0f} min)")
    if curve:
        print("  curve (min since t2a.fill entry, pass_count):")
        pts = [(t, d.get("pass_count")) for t, d in iters] + rec["readings"]
        for t, p in sorted(pts, key=lambda x: x[0]):
            print(f"    {(t - start).total_seconds() / 60.0:7.1f} {p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--curve", action="store_true")
    a = ap.parse_args()
    data = _load(Path(a.events), set(a.runs), a.cluster)
    for r in a.runs:
        _report(r, data[r], a.curve)


if __name__ == "__main__":
    main()
