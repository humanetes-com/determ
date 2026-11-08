#!/usr/bin/env python3
"""How much of each phase's wall time had LLM calls overlapping.

WHY. On run 336559-eb0222a5 (2026-11-07) task0d's rule-probe stage ran
its judge calls one at a time (about 9 minutes each, four hours over two
worlds) while vLLM serves --max-num-seqs 4 and PIPELINE_PARALLELISM=4.
The book build next to it runs on 4 lanes. Which other phases leave the
server's lanes idle is a question about the record, not about the code a
reader happens to remember, so this script answers it from events.

USAGE.
    python scripts/llm_concurrency_by_phase.py [--run RUN_ID] [--runs N]
        [--events logs/events.jsonl] [--phases logs/phases.jsonl]
        [--min-calls 3]
Reads only. No GPU, no vLLM, no cargo. Without --run it reports the last
N (default 3) runs that made at least one LLM call. Each call's interval
is [response ts - duration_ms + its llm.slot wait_ms, response ts] (the
wait for a server slot is not time on the server); it is charged to the
innermost phase scope open at its start (phase.enter / phase.exit pairs
in phases.jsonl, matched by scope).

HOW TO INTERPRET. One row per (run, phase), phases with fewer than
--min-calls calls omitted:
    wall      minutes from phase.enter to phase.exit (or to the last event
              if still open, marked *)
    calls     LLM calls that started inside the phase
    llm_min   summed call minutes (the work, if it ran serially)
    busy%     share of wall time with at least one call in flight
    conc      mean calls in flight over the busy time (1.00 = strictly
              serial; the ceiling is the server's max-num-seqs)
    serial_min  minutes with exactly one call in flight
Decision rule: a phase with high busy% and conc near 1.00 is LLM-bound and
serial; if its calls are independent, lanes would cut its wall by up to
the server's seq count. A phase with low busy% is bound by something else
(cargo, coverage), and lanes on its LLM calls buy little.

--by-route (with --run, or with the --aggregate selection flags): the same columns per fill route kind (session,
file, per-fn, impl, pair) inside task 2a, from phase_c.route.run events
(each carries its own started/ended span). Use it to tell which routes of
a fill are serial by construction (one transcript, one tree) from those
whose units already share lanes. wall sums each run's own span (runs of
one wave overlap, so it can exceed the fill); covered is the time at
least one run of that kind was open; alone% the share of covered time
with exactly one open (a parallel route near 100% runs singleton waves).

--aggregate: one row per phase name over the selected runs (--runs N, or
--since YYYY-MM-DD, optionally --product NAME), every scope of that phase
at any depth, with ALL of the run's calls clipped to the scope's span (a
parent's row therefore includes its children's calls). Extra columns:
    n         scopes of that phase summed
    idle_min  wall minutes with no call in flight: cargo, coverage, git,
              Python, or waiting on a lock. A phase whose idle_min is most
              of its wall is not bound by the model, and lanes on its
              calls cannot shorten it; its plan is about the tool runs.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _t(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _phases(path: Path) -> dict[str, list[dict]]:
    """run_id -> list of {phase, scope, parent, start, end}."""
    open_: dict[tuple[str, str], dict] = {}
    out: dict[str, list[dict]] = defaultdict(list)
    for line in path.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        run, ev = e.get("run_id"), e.get("event")
        if not run or ev not in ("phase.enter", "phase.exit"):
            continue
        # Events written before scopes existed carry none: pair those by
        # phase name (the latest open enter of that name), not by a shared
        # "None" key that lets one run's enters overwrite each other and
        # leave scopes open to the run's end.
        key = (run, str(e["scope"]) if e.get("scope") is not None else f"name:{e.get('phase')}")
        if ev == "phase.enter":
            rec = {"phase": e.get("phase"), "scope": e.get("scope"), "start": _t(e["ts"]), "end": None}
            open_[key] = rec
            out[run].append(rec)
        elif key in open_:
            open_.pop(key)["end"] = _t(e["ts"])
    return out


def _calls(path: Path, runs: set[str] | None) -> dict[str, list[tuple[float, float]]]:
    """Per run, each call's [start, end] on the server. `llm.response` is
    timed by the ask client from before the call waited for a server slot
    (`pipeline_core/llm/slots.py`); the `llm.slot` event of the same prompt
    sha on the same thread carries that wait and is subtracted, so a call
    queued behind the process's slot width is not counted as in flight."""
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    waits: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    with path.open() as f:
        for line in f:
            is_resp = '"llm.response"' in line
            if not is_resp and '"llm.slot"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            run = e.get("run_id")
            if not run or (runs is not None and run not in runs):
                continue
            d = e.get("data") or {}
            key = (run, str(e.get("tid") or e.get("thread") or ""), str(d.get("prompt_sha") or ""))
            if not is_resp:
                waits[key].append((d.get("wait_ms") or 0) / 1000)
                continue
            end = _t(e["ts"])
            wait = waits[key].pop(0) if waits.get(key) else 0.0
            out[run].append((end - (e.get("duration_ms") or 0) / 1000 + wait, end))
    return out


def _coverage(ivs: list[tuple[float, float]], lo: float, hi: float) -> tuple[float, float, float]:
    """(busy seconds, call-seconds, seconds with exactly one in flight) within [lo, hi]."""
    pts = []
    for s, e in ivs:
        s, e = max(s, lo), min(e, hi)
        if e > s:
            pts += [(s, 1), (e, -1)]
    pts.sort()
    busy = work = single = 0.0
    depth, prev = 0, lo
    for x, d in pts:
        if depth:
            busy += x - prev
            work += depth * (x - prev)
            if depth == 1:
                single += x - prev
        depth += d
        prev = x
    return busy, work, single


def _by_route(path: Path, runs: set[str]) -> None:
    ivs: dict[str, list[tuple[float, float]]] = defaultdict(list)
    routes: list[tuple[str, str, float, float]] = []
    with path.open() as f:
        for line in f:
            if '"llm.response"' not in line and '"phase_c.route.run"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            run = e.get("run_id")
            if run not in runs:
                continue
            if e.get("event") == "llm.response":
                end = _t(e["ts"])
                ivs[run].append((end - (e.get("duration_ms") or 0) / 1000, end))
            elif e.get("event") == "phase_c.route.run":
                d = e.get("data") or {}
                if d.get("started"):
                    routes.append((run, str(d.get("kind")), _t(d["started"]), _t(e["ts"])))
    agg: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    print(f"runs={len({r for r, *_ in routes})}")
    for run, kind, s, e in routes:
        busy, work, single = _coverage(ivs[run], s, e)
        row = agg[kind]
        row[0] += 1
        row[1] += e - s
        row[2] += work
        row[3] += single
    # Time covered by at least one run of a kind, and by exactly one: a
    # parallel route whose runs are alone most of their covered time is
    # running singleton waves or a straggler tail.
    spans: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for run, kind, s, e in routes:
        spans[(run, kind)].append((s, e))
    covered: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for (_run, kind), iv in spans.items():
        cov, _work, alone = _coverage(iv, min(s for s, _ in iv), max(e for _, e in iv))
        covered[kind][0] += cov
        covered[kind][1] += alone
    print(f"{'route':10} {'runs':>5} {'wall':>7} {'llm_min':>8} {'conc':>5} {'serial_min':>10} "
          f"{'covered':>8} {'alone%':>6}")
    for kind, (n, wall, work, single) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        cov, alone = covered[kind]
        print(f"{kind:10} {int(n):5} {wall/60:7.0f} {work/60:8.0f} {work/wall if wall else 0:5.2f} "
              f"{single/60:10.0f} {cov/60:8.0f} {100*alone/cov if cov else 0:5.0f}%")


def _select_runs(phases_path: Path, n_runs: int, since: str | None,
                 product: str | None) -> list[str]:
    phases = _phases(phases_path)
    products: dict[str, str] = {}
    for line in phases_path.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("run_id"):
            products.setdefault(e["run_id"], str(e.get("product")))
    runs = [r for r, recs in phases.items() if recs and (product is None or products.get(r) == product)]
    if since:
        lo = _t(since + "T00:00:00+00:00")
        runs = [r for r in runs if phases[r][0]["start"] >= lo]
    else:
        runs = sorted(runs, key=lambda r: phases[r][0]["start"])[-n_runs:]
    return runs


def _aggregate(events: Path, phases_path: Path, n_runs: int, since: str | None,
               product: str | None) -> None:
    phases = _phases(phases_path)
    runs = _select_runs(phases_path, n_runs, since, product)
    calls = _calls(events, set(runs))
    agg: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0, 0.0, 0.0, 0.0])
    for run in runs:
        ivs = calls.get(run, [])
        last = max([e for _, e in ivs] + [r["end"] or r["start"] for r in phases[run]])
        for r in phases[run]:
            end = r["end"] if r["end"] is not None else last
            wall = end - r["start"]
            if wall <= 0:
                continue
            busy, work, single = _coverage(ivs, r["start"], end)
            row = agg[str(r["phase"])]
            row[0] += 1
            row[1] += wall
            row[2] += sum(1 for s, _ in ivs if r["start"] <= s < end)
            row[3] += work
            row[4] += busy
            row[5] += single
    print(f"runs={len(runs)}")
    print(f"{'phase':26} {'n':>4} {'wall':>7} {'calls':>6} {'llm_min':>8} {'busy%':>6} {'conc':>5} "
          f"{'serial_min':>10} {'idle_min':>8}")
    for ph, (n, wall, k, work, busy, single) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        if wall < 60:
            continue
        print(f"{ph[:26]:26} {int(n):4} {wall/60:7.0f} {int(k):6} {work/60:8.0f} "
              f"{100*busy/wall:5.0f}% {work/busy if busy else 0:5.2f} {single/60:10.0f} {(wall-busy)/60:8.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--phases", default="logs/phases.jsonl")
    ap.add_argument("--min-calls", type=int, default=3)
    ap.add_argument("--by-route", action="store_true")
    ap.add_argument("--aggregate", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--product")
    a = ap.parse_args()
    if a.aggregate:
        _aggregate(Path(a.events), Path(a.phases), a.runs, a.since, a.product)
        return
    if a.by_route:
        _by_route(Path(a.events), {a.run} if a.run else
                  set(_select_runs(Path(a.phases), a.runs, a.since, a.product)))
        return
    phases = _phases(Path(a.phases))
    calls = _calls(Path(a.events), {a.run} if a.run else None)
    if a.run:
        runs = [a.run]
    else:
        ordered = sorted((r for r in calls if r in phases), key=lambda r: min(s for s, _ in calls[r]))
        runs = ordered[-a.runs:]
    print(f"{'run':18} {'phase':28} {'wall':>7} {'calls':>5} {'llm_min':>8} {'busy%':>6} {'conc':>5} {'serial_min':>10}")
    for run in runs:
        ivs = calls.get(run, [])
        last = max((e for _, e in ivs), default=0.0)
        recs = phases.get(run, [])
        # innermost scope open at each call's start = the latest-started open record
        per: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for s, e in ivs:
            best = None
            for i, r in enumerate(recs):
                end = r["end"] if r["end"] is not None else float("inf")
                if r["start"] <= s < end and (best is None or r["start"] >= recs[best]["start"]):
                    best = i
            if best is not None:
                per[best].append((s, e))
        for i, r in enumerate(recs):
            mine = per.get(i, [])
            if len(mine) < a.min_calls:
                continue
            end = r["end"] if r["end"] is not None else max(last, r["start"])
            wall = end - r["start"]
            busy, work, single = _coverage(mine, r["start"], end)
            mark = "*" if r["end"] is None else ""
            conc = work / busy if busy else 0.0
            print(f"{run:18} {str(r['phase'])[:28]:28} {wall/60:6.0f}{mark:1} {len(mine):5} "
                  f"{work/60:8.0f} {100*busy/wall if wall else 0:5.0f}% {conc:5.2f} {single/60:10.0f}")


if __name__ == "__main__":
    main()
