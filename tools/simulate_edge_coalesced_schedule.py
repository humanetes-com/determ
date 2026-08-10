#!/usr/bin/env python3
"""Replay one TDD iteration's per-fn phase under an edge-checked level schedule.

The per-fn engine dispatches stubs in ref-count LEVELS with a barrier between
levels (`group_priority_levels`): `min(parallelism, len(level))` workers, so a
level of one runs alone and a straggler holds back every lower-count fn behind
it whether or not any of them references it. Measured on run `1159463-b1e352e5`
(2026-08-09, cluster `stable`): the per-fn phase was 5.0 of the iteration's
9.3 h, running near concurrency 1 against PIPELINE_PARALLELISM=4.

The barrier's purpose is that a caller is never filled before the callees it
references have landed. Ref-count equality is a coarse proxy for that relation.
This tool computes the actual fn→fn reference edges from the crate's ORIGINAL
bodies (the same source the bake's `_leaf_order.json` counts were taken from),
merges adjacent levels wherever no edge crosses the merged boundary — which
preserves exactly the guarantee the barrier provides, statically and
deterministically — and replays the phase's measured unit durations both ways
under the same worker count. The delta is what the scheduling change would
have recovered on that iteration, before anyone edits the engine.

Reading the result:
  - `real wall` is the phase's actual duration between its first and last
    events. The `barrier` replay should land near it; a divergence beyond
    ~20% means the cost model missed something and the projection is not
    trustworthy — it is printed as a warning, not hidden.
  - edges are matched by bare fn NAME (the fidelity `_leaf_order.json`
    itself has). A name collision manufactures an extra edge, which BLOCKS a
    merge — so collisions bias the projection downward, never upward.
  - `--cargo-frac F` treats F of every unit's span as serialized under the
    tree-wide splice+cargo lock and the rest as parallel model time. The
    lock does not parallelize, so sweep several F values; the truth lies
    between them.

    .venv/bin/python tools/simulate_edge_coalesced_schedule.py \\
        --run-id 1159463-b1e352e5 --iter 3

`--iter N` selects the work window that ENDS at the summary carrying
`iter=N` — the same numbering the pipeline log narrates. An iteration still
in flight (no closing summary yet) runs to the newest event and says so.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.artifacts import load_baked_leaf_order  # noqa: E402
from pipeline_core.application.reconstruct.scheduling import (  # noqa: E402
    group_priority_levels,
    leaf_first_fn_order,
)
from pipeline_core.domain.models import FnLocator, Stub  # noqa: E402
from pipeline_core.domain.rust_fn_headers import locate_all_fns  # noqa: E402
from pipeline_core.domain.rust_scan import contains_word  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Unit:
    """One completed per-fn fill unit inside the window: location + wall span.

    The span covers everything the unit did — model calls, fix attempts,
    retro rounds, its serialized cargo runs — because that is what the
    schedule actually paid for it."""
    file: str
    fn_name: str
    span_s: float


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def read_run_events(events_path: Path, run_id: str) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    with events_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("run_id") == run_id and str(rec.get("event", "")).startswith("phase_c."):
                out.append(rec)
    return out


def iteration_window(
    events: list[dict[str, object]], iter_n: int,
) -> tuple[float, float, bool]:
    """(start, end, closed) of the work window ending at the `iter=N` summary.

    The `phase_c.tdd.iter` event carries the sweep that CLOSES the previous
    window: iteration N's work runs from the iter=N-1 event to the iter=N
    event. A missing closing event means the iteration is still in flight —
    the window runs to the newest event and `closed` is False."""
    stamps: dict[int, float] = {}
    for e in events:
        if e["event"] == "phase_c.tdd.iter":
            data = e.get("data") or {}
            if isinstance(data, dict) and "iter" in data:
                stamps[int(str(data["iter"]))] = _ts(str(e["ts"]))
    if iter_n - 1 not in stamps:
        raise SystemExit(f"no iter={iter_n - 1} summary in this run's events "
                         f"(have: {sorted(stamps)}) — cannot open the window")
    start = stamps[iter_n - 1]
    if iter_n in stamps:
        return start, stamps[iter_n], True
    return start, max(_ts(str(e["ts"])) for e in events), False


def per_fn_units(
    events: list[dict[str, object]], start: float, end: float,
) -> tuple[list[Unit], int]:
    """Pair fn.fill.start with its terminal event; drop unpaired starts.

    A fn re-dispatched inside the window (refill after a revert) yields one
    unit per dispatch — each occupied a schedule slot."""
    open_starts: dict[str, tuple[str, float]] = {}
    units: list[Unit] = []
    dropped = 0
    for e in events:
        t = _ts(str(e["ts"]))
        if not start <= t <= end:
            continue
        data = e.get("data") or {}
        if not isinstance(data, dict):
            continue
        fn = str(data.get("fn") or "?")
        if e["event"] == "phase_c.fn.fill.start":
            if fn in open_starts:
                dropped += 1
            open_starts[fn] = (str(data.get("file") or "?"), t)
        elif e["event"] in ("phase_c.fn.fill.done", "phase_c.fn.fill.fail"):
            if fn in open_starts:
                file, t0 = open_starts.pop(fn)
                units.append(Unit(file=file, fn_name=fn, span_s=t - t0))
        elif e["event"] == "phase_c.fn.fill.skip":
            open_starts.pop(fn, None)
    dropped += len(open_starts)
    return units, dropped


def original_body(project: Path, git_ref: str, file: str, fn_name: str) -> str:
    """Concatenated body text of every fn with this name in the file, at the
    pre-strip source. Empty when the file or fn is absent there (a generated
    helper has no original body → no outgoing edges, conservatively)."""
    try:
        text = subprocess.run(
            ["git", "-C", str(project), "show", f"{git_ref}:{file}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return ""
    return "\n".join(
        text[body_start:body_end]
        for _sig, _s, body_start, body_end in locate_all_fns(text, fn_name))


def reference_edges(
    bodies: dict[tuple[str, str], str], names: frozenset[str],
) -> set[tuple[str, str]]:
    """(referencing_fn, referenced_fn) name pairs, self-references excluded."""
    edges: set[tuple[str, str]] = set()
    for (_file, fn), body in bodies.items():
        for other in names:
            if other != fn and contains_word(body, other):
                edges.add((fn, other))
    return edges


def build_levels(
    units: list[Unit], counts: dict[str, int],
) -> list[list[Unit]]:
    """The engine's own leveling, applied to the observed units: leaf-first
    order (`leaf_first_fn_order`), then contiguous equal-ref-count grouping
    (`group_priority_levels`) — both imported so the fidelity is the code's."""
    by_loc: dict[FnLocator, list[Unit]] = {}
    for u in units:
        by_loc.setdefault(FnLocator(u.file, u.fn_name), []).append(u)
    ordered = leaf_first_fn_order(list(by_loc), counts)
    stubs = [Stub(file=loc[0], fn_name=loc[1]) for loc in ordered]
    levels: list[list[Unit]] = []
    for level in group_priority_levels(stubs, counts):
        levels.append([u for _i, r in level
                       for u in by_loc[FnLocator(r.file, r.fn_name)]])
    return levels


def coalesce(
    levels: list[list[Unit]], edges: set[tuple[str, str]],
) -> list[list[Unit]]:
    """Merge adjacent levels while no reference edge crosses the merge.

    Only later→earlier edges matter: the barrier's guarantee is that a fn
    never starts before a HIGHER-count fn it references has completed.
    Same-level fns get no ordering today and none is added."""
    groups: list[list[Unit]] = []
    for level in levels:
        if groups:
            names_in_group = {u.fn_name for u in groups[-1]}
            blocked = any((u.fn_name, b) in edges
                          for u in level for b in names_in_group)
            if not blocked:
                groups[-1] = groups[-1] + level
                continue
        groups.append(list(level))
    return groups


def simulate(
    groups: list[list[Unit]], workers: int, cargo_frac: float,
) -> float:
    """List-schedule each group over `workers`, barrier between groups.

    Each unit is (1-f)·span of parallel model time then f·span under the
    tree-wide splice+cargo lock. Units are taken in the given order by the
    earliest-free worker — the same greedy pull the engine's queue does."""
    t_group = 0.0
    lock_free = 0.0
    for group in groups:
        free = [t_group] * max(1, workers)
        for u in group:
            w = min(range(len(free)), key=lambda i: free[i])
            model_end = free[w] + u.span_s * (1.0 - cargo_frac)
            cargo_start = max(model_end, lock_free)
            end = cargo_start + u.span_s * cargo_frac
            lock_free = end
            free[w] = end
        t_group = max(free)
    return t_group


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--iter", type=int, required=True, dest="iter_n")
    ap.add_argument("--events", type=Path, default=_ROOT / "logs" / "events.jsonl")
    ap.add_argument("--project", type=Path, default=_ROOT / "tmp" / "anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--git-ref", default="master",
                    help="ref holding the ORIGINAL (pre-strip) bodies")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cargo-frac", type=float, nargs="+",
                    default=[0.0, 0.25, 0.5])
    args = ap.parse_args()

    events = read_run_events(args.events, args.run_id)
    if not events:
        raise SystemExit(f"no phase_c events for run {args.run_id} in {args.events}")
    start, end, closed = iteration_window(events, args.iter_n)
    units, dropped = per_fn_units(events, start, end)
    if not units:
        raise SystemExit("no completed per-fn units in the window")
    fn_times = [_ts(str(e["ts"])) for e in events
                if str(e["event"]).startswith("phase_c.fn.")
                and start <= _ts(str(e["ts"])) <= end]
    real_wall = (max(fn_times) - min(fn_times)) if fn_times else 0.0

    counts = load_baked_leaf_order(args.project, args.cluster)
    if not counts:
        print(f"WARNING: no _leaf_order.json for cluster {args.cluster} — "
              "every unit lands in one level, the barrier baseline is vacuous")
    bodies = {(u.file, u.fn_name):
              original_body(args.project, args.git_ref, u.file, u.fn_name)
              for u in units}
    missing = sorted(fn for (_f, fn), b in bodies.items() if not b)
    names = frozenset(u.fn_name for u in units)
    edges = reference_edges(bodies, names)

    levels = build_levels(units, counts)
    groups = coalesce(levels, edges)

    hours = 3600.0
    print(f"window: iter {args.iter_n} "
          f"({'closed' if closed else 'STILL IN FLIGHT'}), "
          f"{(end - start) / hours:.1f}h total, "
          f"per-fn phase real wall {real_wall / hours:.1f}h")
    print(f"units: {len(units)} completed ({dropped} unpaired dropped), "
          f"levels: {len(levels)} (sizes {[len(x) for x in levels]})")
    print(f"edges: {len(edges)} among {len(names)} fn names"
          + (f"; no original body for {missing} (no outgoing edges assumed)"
             if missing else ""))
    print(f"coalesced: {len(groups)} group(s) (sizes {[len(x) for x in groups]})")
    base0 = simulate(levels, args.workers, 0.0)
    if real_wall and abs(base0 - real_wall) / real_wall > 0.20:
        print(f"WARNING: barrier replay {base0 / hours:.1f}h diverges >20% "
              f"from real {real_wall / hours:.1f}h — cost model suspect, "
              "treat projections as bounds only")
    for f in args.cargo_frac:
        base = simulate(levels, args.workers, f)
        merged = simulate(groups, args.workers, f)
        print(f"cargo-frac {f:.2f}:  barrier {base / hours:.2f}h  "
              f"edge-coalesced {merged / hours:.2f}h  "
              f"saving {(base - merged) / hours:.2f}h "
              f"({(1 - merged / base) * 100:.0f}%)" if base else "n/a")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
