#!/usr/bin/env python
"""Compute the escalated cohort's dispatch order — before and after the
producer promotion — on the LIVE cluster tree, exactly as the TDD loop would.

WHY. The producer ordering (`domain/dispatch_batch.promote_producers` fed by
`domain/installable_fns.producer_dispatch_ranks`) shipped on 2026-08-25 with
a tracker note "validated on the live cohort (builders = …)" that was the
function's own output read as confirmation: it was MISSING the two wrong
constructors (`construct_from_adhoc`, `construct_from_display`) the ordering
existed to promote, and the plan's fixture — the first batch of 12 holds all
five `construct_from_*` — had never been computed. Computed, it failed
(`construct_from_display` at slot 13). A second trap on the way: enumerating
the extractor from `<cluster>/src` yields `file='error.rs'`, the loop's
records carry `src/error.rs`, and every join with the escalated set comes
back EMPTY — `builders: []` — which reads as "the promotion is a no-op". This
script is the check the rule now requires (CLAUDE.md, "A plan's fixture is
COMPUTED on the live records, never described"): run it before claiming the
ordering does anything, and before every launch that expects a specific
first batch, because prettyplease re-emits bodies and a spacing change can
silently stop `MessageError(` or `Self::construct(` from resolving.

USAGE.
    .venv/bin/python scripts/walk_escalated_cohort_order.py [--project anyhow]
        [--cluster stable] [--cap 12] [--events logs/events.jsonl] [--run <id>]

No GPU, no vLLM, no pipeline running required; reads only. Inputs, all from
disk: the extractor binary (`tools/skeleton_extractor/target/release/
skeleton-extractor`) run on `tmp/<project>/.skeleton-filled/<cluster>` (the
ROOT, as the loop does); the last `phase_c.attribution.escalated` event per
test in `events.jsonl` (the escalated `added` lists); the floor
`tmp/<project>/.skeleton/_tdd_floor_<cluster>.json` (`futile_units`, the
accessors the tests still attribute to); the baked leaf counts
`.skeleton/<cluster>/_leaf_order.json`; and `order_frontier` itself.

HOW TO INTERPRET THE OUTPUT.
    records      — `fns=N types=M rooted=[…]`: the top-level dirs the
                   records name. From the cluster root that is `src` AND
                   `tests` (the walk crosses the tests symlink; the loop sees
                   the same). A `!! … match no record` line right after is the
                   rooting trap — exit 2, every join below would be void.
    escalated    — `run=<id>` and, per fired test, how many units that run's
                   LAST fire for it added. One run only (`--run`, default the
                   run of the last fire on file): the events file holds every
                   run's fires and their union names units of other trees.
    builders     — the constructor-role units (body constructs a crate
                   type). On the stable 98-tree: the six `construct*` plus
                   `Error::context`. An empty set with a non-empty cohort is
                   a rooting or extractor failure, never a real answer.
    callees      — units another escalated unit CALLS (resolved). `new`,
                   `from`, `kind.rs::new` appearing here means the resolver
                   regressed to token matching.
    BEFORE       — the first `cap` units in `order_frontier`'s order = what
                   the loop would have dispatched without the promotion.
    AFTER        — the first `cap` units the loop WILL dispatch.
    fixture      — the six constructors present/missing from AFTER's batch.
                   MISSING non-empty = the ordering does not do its job on
                   this tree; do not launch expecting the constructor
                   reading in batch 1.
    next batch   — AFTER's second `cap`, so a pair the plan expects together
                   (`render` + `__dispatch_ensure`) can be checked.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.artifacts import load_baked_leaf_order  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.application.reconstruct.scheduling import order_frontier  # noqa: E402
from pipeline_core.domain.dispatch_batch import promote_producers  # noqa: E402
from pipeline_core.domain.installable_fns import producer_dispatch_ranks  # noqa: E402
from pipeline_core.domain.models import FnLocator  # noqa: E402

_SIX = ("construct", "construct_from_adhoc", "construct_from_boxed",
        "construct_from_context", "construct_from_display", "construct_from_std")


def _loc(s: str) -> FnLocator:
    """`FnLocator(file='src/x.rs', fn_name='y')` as the event serialises it."""
    f = s.split("file='")[1].split("'")[0]
    n = s.split("fn_name='")[1].split("'")[0]
    return FnLocator(f, n)


def _fmt(k: FnLocator) -> str:
    return f"{k.file}::{k.fn_name}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--cap", type=int, default=12)
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--run", default="", help=(
        "run_id whose escalation fire to order (default: the run of the last "
        "fire in the events file)"))
    ap.add_argument("--extractor", default=(
        "tools/skeleton_extractor/target/release/skeleton-extractor"))
    a = ap.parse_args()

    root = Path("tmp") / a.project
    cluster_dir = root / ".skeleton-filled" / a.cluster
    ex = CliExtractor(Path(a.extractor))
    types = ex.enumerate_types(cluster_dir) or []
    fns = [r.to_dict() for r in (ex.enumerate_fns(cluster_dir) or [])]
    # Enumerated from the cluster ROOT the walk also crosses the `tests`
    # symlink, so records carry `src/…` AND `tests/…`; the loop sees the same.
    roots = sorted({str(r.get("file", "")).split("/", 1)[0] for r in fns})
    print(f"records      fns={len(fns)} types={len(types)} rooted={roots}")

    # The escalation product of ONE run: `events.jsonl` holds every run's
    # fires (4 323 on 2026-08-25, several projects and trees), so the union
    # names units that no longer exist. Scope to `--run`, default the run of
    # the last fire on file, and keep each test's LAST fire within it — the
    # product the loop re-merges at its next iteration.
    fires: list[tuple[str, str, list[str]]] = []
    with open(a.events, encoding="utf-8") as fh:
        for line in fh:
            if '"phase_c.attribution.escalated"' not in line:
                continue
            e = json.loads(line)
            if e.get("product") not in (None, a.project):
                continue
            d = e.get("data", e)
            fires.append((str(e.get("run_id", "")), str(d["test"]),
                          list(d.get("added", []))))
    run = a.run or (fires[-1][0] if fires else "")
    latest: dict[str, list[str]] = {}
    for rid, test, added in fires:
        if rid == run:
            latest[test] = added
    frontier: dict[FnLocator, set[str]] = {}
    for test, added in latest.items():
        for s in added:
            frontier.setdefault(_loc(s), set()).add(test)
    esc = frozenset(frontier)
    print(f"escalated    run={run or '<none>'} " + (", ".join(
        f"{t}:{len(v)}" for t, v in sorted(latest.items())) or "(no fire)"))
    if not esc:
        print("  no escalation fire recorded for this run — nothing to order")
        return 1
    # The join the ranking depends on: every escalated key must name a
    # record. An empty intersection is the rooting trap (`error.rs` vs
    # `src/error.rs`), never a real "no builders".
    keys = {(str(r.get("file", "")), str(r.get("fn_name", ""))) for r in fns}
    unjoined = sorted(_fmt(k) for k in esc if (k.file, k.fn_name) not in keys)
    if unjoined:
        print(f"  !! {len(unjoined)} escalated unit(s) match no record — "
              f"rooting/extractor mismatch: {unjoined[:6]}")
        return 2

    floor_path = root / ".skeleton" / f"_tdd_floor_{a.cluster}.json"
    if floor_path.exists():
        floor = json.loads(floor_path.read_text(encoding="utf-8"))
        for test, units in (floor.get("futile_units") or {}).items():
            for u in units:
                f, n = u.split("::", 1)
                frontier.setdefault(FnLocator(f, n), set()).add(test)

    ordered = order_frontier(frontier, load_baked_leaf_order(root, a.cluster))
    builders, callees = producer_dispatch_ranks(esc, fns, types)
    after = [k for k, _ in promote_producers(ordered, esc, builders, callees)]
    before = [k for k, _ in ordered]
    cap = a.cap or len(after)

    print("builders     " + ", ".join(sorted(map(_fmt, builders))))
    print("callees      " + ", ".join(sorted(map(_fmt, callees))))
    print(f"BEFORE [{cap}]  " + ", ".join(map(_fmt, before[:cap])))
    print(f"AFTER  [{cap}]  " + ", ".join(map(_fmt, after[:cap])))
    first = {k.fn_name for k in after[:cap] if k.file == "src/error.rs"}
    present = [n for n in _SIX if n in first]
    missing = [n for n in _SIX if n not in first]
    print(f"fixture      present={present} MISSING={missing}")
    print("next batch   " + ", ".join(map(_fmt, after[cap:2 * cap])))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
