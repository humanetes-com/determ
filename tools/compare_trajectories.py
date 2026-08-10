#!/usr/bin/env python3
"""Put two runs' phase-C trajectories side by side, to compare MODELS.

The question this answers is "which model gets the tree further, per iteration
and per hour" — not "which emits tokens faster". Those come apart: a model whose
iterations are slower but whose fills land can reach a pass count in fewer
sweeps, and a tokens/sec comparison ranks it last.

It reads `logs/events.jsonl`, not the text log. The per-iteration summary line
carries the state vector but no timestamp — only cargo's own duration, in
`(0.8s)` — so wall-clock per iteration is not recoverable from it. The
`phase_c.tdd.iter` event carries the same vector plus `ts`, and is keyed by
`run_id`, which is what separates two arms sharing one log file.

What to read, and in what order. `pass_count` alone cannot tell "filling a
producer chain that has not closed yet" from "locked out of the fns that
matter" — a single endpoint is noise; the same crate on 2026-08-07 finished at
81 on one run and 21 on another. The frontier and the exhausted count can:

    healthy re-climb   frontier 84 -> 26 -> 44, exhausted 0 -> 50 -> 1,
                       and one 75-fn batch took pass 13 -> 83 in a sweep
    deadlock           frontier 63 -> 44 -> 18 -> 17, exhausted 0 -> 29 -> 24
                       -> 30, fills still landing at 39-109 an iteration

so both are printed on every row, and the summary reports iterations-to-best
rather than best alone.

Usage:

    python tools/compare_trajectories.py \\
        --arm qwen=2219473-e43ea61c --arm muse=2514529-5e77dafb \\
        --cluster stable

    python tools/compare_trajectories.py --list        # run ids in the sink
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_EVENT = "phase_c.tdd.iter"
_DEFAULT_SINK = Path("logs/events.jsonl")


@dataclass(frozen=True, slots=True)
class Sweep:
    """One iteration's state vector, as the loop measured it."""

    iter_n: int
    cluster: str
    ts: datetime
    pass_count: int
    fail_count: int
    oracle: int
    frontier: int
    exhausted: int
    kind: str
    stall: int
    improved: bool


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def read_sweeps(sink: Path, run_id: str, cluster: str | None) -> list[Sweep]:
    """Every `phase_c.tdd.iter` this run emitted, in log order.

    Streamed and substring-prefiltered: the sink reaches 100 MB+ on a long run,
    and json.loads on every line of it is the difference between a second and a
    minute. A malformed line is skipped rather than fatal — the sink is appended
    to by a live run and the last line can be a partial write.
    """
    out: list[Sweep] = []
    with sink.open(errors="replace") as fh:
        for line in fh:
            if _EVENT not in line or run_id not in line:
                continue
            try:
                rec: dict[str, Any] = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") != _EVENT or rec.get("run_id") != run_id:
                continue
            data = rec.get("data") or {}
            if cluster and data.get("cluster") != cluster:
                continue
            ts = _parse_ts(str(rec.get("ts", "")))
            if ts is None:
                continue
            # `exhausted` is not in the payload under that name: the loop prints
            # `N skipped as exhausted` and emits the eligible frontier. Derive
            # nothing — report what is there, and leave the text log as the
            # place the skipped count is read from.
            out.append(Sweep(
                iter_n=int(data.get("iter", 0)),
                cluster=str(data.get("cluster", "")),
                ts=ts,
                pass_count=int(data.get("pass_count", 0)),
                fail_count=int(data.get("fail_count", 0)),
                oracle=int(data.get("oracle_size", 0)),
                frontier=int(data.get("frontier_size", 0)),
                exhausted=-1,
                kind=str(data.get("kind", "")),
                stall=int(data.get("stall", 0)),
                improved=bool(data.get("improved", False)),
            ))
    return out


def by_cluster(sweeps: list[Sweep]) -> list[tuple[str, list[Sweep]]]:
    """Split a run's sweeps per cluster, in the order the run reached them.

    Clusters run sequentially and each restarts its iteration count from 1
    against its own oracle, so a flat listing reads as one trajectory that
    resets — `pass=88 oracle=106` followed by `pass=0 oracle=113` is two
    clusters, not a collapse. Comparing arms means comparing the same cluster.
    """
    groups: dict[str, list[Sweep]] = {}
    for s in sweeps:
        groups.setdefault(s.cluster, []).append(s)
    return list(groups.items())


def format_arm(label: str, run_id: str, cluster: str, sweeps: list[Sweep]) -> str:
    """One arm's trajectory for one cluster, one row per iteration."""
    if not sweeps:
        return f"── {label}  ({run_id})\n   no {_EVENT} events — wrong run id, or the run never reached phase C\n"
    start = sweeps[0].ts
    lines = [f"── {label}  ({run_id})  cluster={cluster}",
             "   iter    pass   fail  oracle  frontier  stall  elapsed   +min  kind"]
    prev = start
    for s in sweeps:
        elapsed = (s.ts - start).total_seconds() / 60.0
        delta = (s.ts - prev).total_seconds() / 60.0
        prev = s.ts
        lines.append(
            f"   {s.iter_n:>4}  {s.pass_count:>6}  {s.fail_count:>5}  {s.oracle:>6}  "
            f"{s.frontier:>8}  {s.stall:>5}  {elapsed:>6.1f}m  {delta:>5.1f}  {s.kind}")
    return "\n".join(lines) + "\n"


def summarise(label: str, sweeps: list[Sweep]) -> str:
    """The comparison line: how far, in how many sweeps, in how long.

    Iterations-to-best is the headline rather than best alone. Two arms that
    both end at 88 are not equivalent if one got there in 3 sweeps and the other
    in 14 — and the pass count says they are.
    """
    if not sweeps:
        return f"   {label:<12} no data"
    best = max(s.pass_count for s in sweeps)
    first_best = next(s for s in sweeps if s.pass_count == best)
    minutes = (first_best.ts - sweeps[0].ts).total_seconds() / 60.0
    total = (sweeps[-1].ts - sweeps[0].ts).total_seconds() / 60.0
    return (f"   {label:<12} best pass={best:<4} first reached at iter {first_best.iter_n} "
            f"({minutes:.0f}m); {len(sweeps)} sweep(s) over {total:.0f}m; "
            f"final frontier={sweeps[-1].frontier} kind={sweeps[-1].kind}")


def list_runs(sink: Path) -> list[tuple[str, str, int]]:
    """(run_id, first ts, sweep count) for every run that reached phase C."""
    seen: dict[str, list[Any]] = {}
    with sink.open(errors="replace") as fh:
        for line in fh:
            if _EVENT not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") != _EVENT:
                continue
            run = str(rec.get("run_id", "?"))
            if run not in seen:
                seen[run] = [str(rec.get("ts", "")), 0]
            seen[run][1] += 1
    return [(r, v[0], int(v[1])) for r, v in seen.items()]


def _arm(spec: str) -> tuple[str, str]:
    label, _, run_id = spec.partition("=")
    if not run_id:
        raise argparse.ArgumentTypeError(f"--arm wants LABEL=RUN_ID, got {spec!r}")
    return label, run_id


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="append", type=_arm, default=[],
                    metavar="LABEL=RUN_ID", help="an arm to compare (repeat for each)")
    ap.add_argument("--cluster", default=None, help="restrict to one cluster")
    ap.add_argument("--sink", type=Path, default=_DEFAULT_SINK)
    ap.add_argument("--list", action="store_true", help="list run ids in the sink and exit")
    args = ap.parse_args(argv)

    if not args.sink.exists():
        print(f"[error] no event sink at {args.sink}", file=sys.stderr)
        return 1

    if args.list:
        for run, ts, n in sorted(list_runs(args.sink), key=lambda r: r[1]):
            print(f"  {run:<24} {ts}  {n:>4} sweep(s)")
        return 0

    if len(args.arm) < 2:
        print("[error] give at least two --arm LABEL=RUN_ID to compare", file=sys.stderr)
        return 2

    collected = [(label, run, read_sweeps(args.sink, run, args.cluster))
                 for label, run in args.arm]
    for label, run, sweeps in collected:
        groups = by_cluster(sweeps)
        if not groups:
            print(format_arm(label, run, args.cluster or "?", []))
        for cluster, rows in groups:
            print(format_arm(label, run, cluster, rows))
    print("── summary")
    for label, _run, sweeps in collected:
        for cluster, rows in by_cluster(sweeps) or [(args.cluster or "?", [])]:
            print(summarise(f"{label}/{cluster}", rows))
    print("\n   Read the frontier alongside the score: a frontier collapsing while")
    print("   pass climbs is a re-climb; a frontier shrinking while fills keep")
    print("   landing and pass does not move is a deadlock, and the score cannot")
    print("   tell them apart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
