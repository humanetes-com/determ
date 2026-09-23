#!/usr/bin/env python3
"""Replay recorded fill schedules with and without each schedule exemption,
under the route-row verdict and under the per-unit verdict.

WHY THIS EXISTS.  The fill schedule grew three exemptions while it still
convicted units from run-level route rows: the pair's grace (a reverted hop
pair ranks as never-served and holds the runs it blocks, `pair_wait`), the
coverage-split deferral, and the bounded re-admission of a re-buy after
`readmit_after` deferrals.  On 2026-09-23 the schedule moved to per-unit
verdicts off the body ledger (run `3131-f6b37c1a` deferred its blocker's
per-fn batch three fills running on route-row convictions; run
`271137-e728ae95` validated the new verdict live).  Some of those exemptions
may only ever have compensated for fake convictions.  An exemption that
changes the outcome under the route-row verdict and changes nothing under the
per-unit verdict was compensation and is a deletion candidate; one that still
changes outcomes under the per-unit verdict is load-bearing.  This script is
the measurement that decides which; the deletion is a separate change.

USAGE.
  python scripts/exemption_replay.py [--since YYYY-MM-DD] [--run RUN_ID ...]
                                     [--examples N]
    --since     schedule events at or after this date (default: 7 days
                before the newest event).
    --run       restrict to these run ids.
    --examples  fills to print per differing cell (default 3).
  Read-only: reads logs/events.jsonl (`phase_c.fill.schedule`,
  `phase_c.dispatch.service_order`), logs/route_ledger/<project>/index.jsonl
  and logs/body_ledger/<project>; no GPU, no model, no writes.

WHAT IS AND IS NOT REPLAYED.  Each fill is rebuilt from its schedule event:
the planned runs (carved pieces are folded back into the run they were cut
from), the route ledger cut at the event's timestamp, the body ledger as
the fill ranked on it, and the iteration's standing-FLAT set.  Events that
carry `inputs` replay those arguments.  `inputs.standing_flat` maps a unit
to the body sha the ranking compared (a name alone never matches a landing's
sha).  `inputs.body_rows` is how many cluster rows that verdict was built
from, in file order; the replay takes that prefix.  An older event has
neither, so its body rows are every row except this run's rows at or after
the fill's iteration, PLUS this iteration's non-landed rows: the sweep
grades the fill with the same iteration number it then writes landings
under, and dropping every row at that iteration drops the grades the
schedule just received.  The standing-FLAT names come from the service-order
event and the sha from the latest flat row in that visible prefix.  The
service-order event is missing when dispatch order did not move, and then
the flat set is empty.  Older events also replay hollow, blame, attribution
and caps empty: that approximation is shared by all arms, so a DIFFERENCE
between arms is still the exemption's, but the absolute order is not the
live one, and the coverage-split arm cannot fire without `attributed`.
The `exact` count on the first line says how many fills carried their
inputs.  Divisibility is as the code builds it today: per-fn, file and impl
runs carry `unit_tests`.

HOW TO INTERPRET THE OUTPUT.
  fills N (runs R, since D, exact X) — the replayed population; X of them
      recorded their inputs and replay as decided.
  live firings: pair_wait=… coverage_split=… rebuy_readmitted=… grace_spent=…
      how often each exemption decided a run in the recorded schedules.
  <verdict> / <exemption> removed: changed K of N fills
      K fills whose opened set (planned run label + the units opened for it)
      differs from the same verdict with every exemption on.  One example
      line per fill: `+label{units}` opened only without the exemption,
      `-label{units}` opened only with it.
  Decision rule: an exemption with K>0 under `route-row` and K=0 under
  `per-unit` only compensated for route-row convictions — delete it.  K>0
  under `per-unit` means it still decides fills the new verdict leaves to
  it — keep it, and read the examples to see which.  A cell with K=0 under
  both verdicts decided nothing replayable in this window; that is not
  evidence it is dead (read the live-firings line before deleting).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.body_ledger_store import load_entries  # noqa: E402
from pipeline_core.domain import fill_schedule as fsch  # noqa: E402
from pipeline_core.domain.fill_schedule import (  # noqa: E402
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    schedule,
    unit_evidence_from,
)
from pipeline_core.domain.models import FnLocator  # noqa: E402

_DIVISIBLE = (RouteKind.PER_FN, RouteKind.FILE, RouteKind.IMPL)


def _loc(s: str) -> FnLocator:
    f, _sep, fn = s.partition("::")
    return FnLocator(f, fn)


def _events(since: str | None, runs: set[str]) -> tuple[list[dict], dict]:
    sched: list[dict] = []
    order: dict[tuple[Any, Any, int], dict] = {}
    with open(ROOT / "logs" / "events.jsonl", encoding="utf-8",
              errors="replace") as fh:
        for line in fh:
            if ('"phase_c.fill.schedule"' not in line
                    and '"phase_c.dispatch.service_order"' not in line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if runs and e.get("run_id") not in runs:
                continue
            if e["event"] == "phase_c.fill.schedule":
                if e["data"].get("scheduled"):
                    sched.append(e)
            else:
                data = e.get("data") or {}
                order[(e["run_id"], data.get("cluster"),
                       int(data.get("iter") or 0))] = data
    if since is None and sched:
        newest = max(e["ts"] for e in sched)[:10]
        since = (datetime.fromisoformat(newest) - timedelta(days=7)).date().isoformat()
    return [e for e in sched if since is None or e["ts"][:10] >= since], order


def _planned(data: dict) -> list[RouteRun]:
    """The planned runs of one schedule event; carved pieces are folded back
    into their parent (its targets are the union of its pieces')."""
    by_parent: dict[str, list[dict]] = defaultdict(list)
    planned: list[dict] = []
    for r in [*data.get("runs", ()), *data.get("deferred", ())]:
        if r.get("parent"):
            by_parent[r["parent"]].append(r)
        else:
            planned.append(r)
    for pieces in by_parent.values():
        head = dict(pieces[0])
        head["label"] = head["label"].split(" [", 1)[0]
        for key in ("targets", "stubs", "target_tests", "files"):
            head[key] = sorted({x for p in pieces for x in p.get(key) or ()})
        head["bodies_sha"] = ""
        planned.append(head)
    out: list[RouteRun] = []
    for r in planned:
        kind = RouteKind(r["kind"])
        targets = sorted((_loc(t) for t in r["targets"]),
                         key=lambda u: (u.file, u.fn_name))
        tests = frozenset(r.get("target_tests") or ())
        out.append(RouteRun(
            kind=kind, label=r["label"], targets=frozenset(targets),
            target_tests=tests,
            stubs_in_targets=frozenset(_loc(t) for t in r.get("stubs") or ()),
            files=tuple(r.get("files") or ()), seq=int(r.get("seq") or 0),
            bodies_sha=str(r.get("bodies_sha") or ""),
            unit_tests=(tuple((t, tests) for t in targets)
                        if kind in _DIVISIBLE else ())))
    return out


def _state_name(entry: Any) -> str:
    state = getattr(entry, "state", "")
    return str(getattr(state, "value", state))


def rows_visible_at_schedule(entries: Iterable[Any], *, run_id: str,
                             iteration: int,
                             body_rows: int | None) -> list[Any]:
    """The body-ledger rows the fill's verdict was built from.

    `body_rows` is that length in cluster file order, recorded on the
    schedule event. Without it, keep every other run's rows, this run's
    earlier iterations, and this iteration's non-landed rows. The sweep
    writes those grades under the iteration the fill then shares, and the
    fill's own landings are the `landed` rows at that iteration.
    """
    rows = list(entries)
    if body_rows is not None:
        return rows[:body_rows]
    visible: list[Any] = []
    for entry in rows:
        if getattr(entry, "run_id", "") != run_id:
            visible.append(entry)
            continue
        it = int(getattr(entry, "iteration", 0) or 0)
        if it < iteration or (it == iteration and _state_name(entry) != "landed"):
            visible.append(entry)
    return visible


def standing_flat_at(inputs: Mapping[str, Any], flat_names: Iterable[str],
                     entries: Iterable[Any]) -> dict[FnLocator, str]:
    """Unit to body sha for the standing-FLAT set.

    A recorded map is the ranking's own. Names from a service-order event
    take the sha of the latest flat row in the visible prefix: the grade
    matches a landing only when the sha is that body's, and the empty
    string matches none.
    """
    recorded = inputs.get("standing_flat")
    if isinstance(recorded, dict):
        return {_loc(str(unit)): str(sha) for unit, sha in recorded.items()}
    latest: dict[FnLocator, str] = {}
    for entry in entries:
        if _state_name(entry) != "flat":
            continue
        key = getattr(entry, "key", None)
        if key is None:
            continue
        latest[FnLocator(key.file, key.fn_name)] = str(entry.body_sha)
    return {unit: latest[unit] for unit in (_loc(n) for n in flat_names)
            if unit in latest}


def _service_order(order: Mapping[tuple[Any, ...], dict], run_id: str,
                   cluster: Any, iteration: int) -> dict:
    found = order.get((run_id, cluster, iteration))
    if found is not None:
        return found
    same = [data for (rid, _cluster, it), data in order.items()
            if rid == run_id and it == iteration]
    return same[-1] if len(same) == 1 else {}


def _opened(s: fsch.FillSchedule) -> frozenset[tuple[str, frozenset[str]]]:
    got: dict[str, set[str]] = defaultdict(set)
    for x in s.ordered:
        got[x.run.label.split(" [", 1)[0]] |= {
            f"{t.file}::{t.fn_name}" for t in x.run.targets}
    return frozenset((k, frozenset(v)) for k, v in got.items())


def _no_pair_grace() -> Any:
    """Every pair has spent its grace: it neither ranks a revert as
    never-served nor holds the runs it blocks."""
    return mock.patch.object(fsch, "consecutive_unpaid",
                             lambda *a, **k: 10 ** 9)


def _arms() -> dict[str, tuple[dict[str, Any], Callable[[], Any] | None]]:
    return {
        "all exemptions on": ({}, None),
        "pair grace removed": ({}, _no_pair_grace),
        "re-admission removed": ({"readmit_after": 10 ** 9}, None),
        "coverage split removed": ({"attributed": ()}, None),
    }


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since")
    ap.add_argument("--run", nargs="*", default=[])
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    args = ap.parse_args(list(argv) if argv is not None else None)

    events, order = _events(args.since, set(args.run))
    ledger = ROOT / "logs" / "route_ledger" / args.project / "index.jsonl"
    with open(ledger, encoding="utf-8") as fh:
        rows = [r for r in (RouteLedgerRow.from_dict(json.loads(line))
                            for line in fh) if r is not None]
    body = load_entries(ROOT, args.project, cluster=args.cluster)
    live: Counter[str] = Counter()
    for e in events:
        for r in [*e["data"]["runs"], *e["data"]["deferred"]]:
            if r["reason"] in ("pair_wait", "coverage_split",
                               "rebuy_readmitted", "grace_spent"):
                live[r["reason"]] += 1
            for lapse in r.get("lapsed") or ():
                live[f"lapsed {lapse}"] += 1
    runs_seen = {e["run_id"] for e in events}
    exact = sum(1 for e in events if "inputs" in e["data"])
    print(f"fills {len(events)} (runs {len(runs_seen)}, since "
          f"{min((e['ts'][:10] for e in events), default='-')}, exact {exact})")
    print("live firings: " + ", ".join(f"{k}={v}" for k, v in sorted(live.items())))

    arms = _arms()
    diffs: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in events:
        data = e["data"]
        run_id, it = e["run_id"], int(data.get("iter") or 0)
        oracle = next((r.oracle_fp for r in rows if r.run_id == run_id), "")
        prior = [r for r in rows if r.started[:19] < e["ts"][:19]]
        inp = data.get("inputs") or {}
        body_rows = inp.get("body_rows")
        visible = rows_visible_at_schedule(
            body, run_id=run_id, iteration=it,
            body_rows=body_rows if isinstance(body_rows, int) else None)
        so = _service_order(order, run_id, data.get("cluster"), it)
        flat = standing_flat_at(inp, so.get("flat") or (), visible)
        ev = unit_evidence_from(visible, flat, oracle_fp=oracle)
        planned = _planned(data)
        recorded = {
            "hollow": {t: frozenset(_loc(u) for u in us)
                       for t, us in (inp.get("hollow") or {}).items()},
            "crash_blamed": frozenset(_loc(u) for u in inp.get("crash_blamed") or ()),
            "attributed": frozenset(_loc(u) for u in inp.get("attributed") or ()),
            "unmeasurable_tests": frozenset(inp.get("unmeasurable_tests") or ()),
            "readmit_after": int(inp.get("readmit_after") or 2),
            "session_min_turns": int(inp.get("session_min_turns") or 0),
            "max_per_kind": {RouteKind(k): int(v) for k, v in
                             (inp.get("max_per_kind") or {}).items()},
        }
        for verdict, uev in (("route-row", None), ("per-unit", ev)):
            base: frozenset | None = None
            for name, (kw, patch) in arms.items():
                call = {"oracle_fp": oracle,
                        "budget_minutes": float(data.get("budget_minutes") or 0),
                        "unit_evidence": uev, **recorded, **kw}
                if patch is not None:
                    with patch():
                        got = _opened(schedule(planned, prior, **call))
                else:
                    got = _opened(schedule(planned, prior, **call))
                if base is None:
                    base = got
                    continue
                if got != base:
                    plus = ", ".join(f"+{k}{{{len(v)}}}" for k, v in sorted(got - base))
                    minus = ", ".join(f"-{k}{{{len(v)}}}" for k, v in sorted(base - got))
                    diffs[(verdict, name)].append(
                        f"{run_id} fill {it}: {plus} {minus}".strip())
    for verdict in ("route-row", "per-unit"):
        for name in list(arms)[1:]:
            hits = diffs.get((verdict, name), [])
            print(f"{verdict} / {name}: changed {len(hits)} of {len(events)} fills")
            for line in hits[:args.examples]:
                print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
