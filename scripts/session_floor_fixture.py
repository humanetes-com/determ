#!/usr/bin/env python3
"""Distill the recorded fills a session-floor replay needs into one fixture.

WHY
---
The fill schedule reserves each opened slice session a floor of
`SLICE_SESSION_MIN_TURNS` turns at a measured price. Until 2026-10-05 that
price was the per-call median (`minutes / model_calls`) over the ledger's
session rows, and on run `450922-661c5e9f` (cluster stable) it reserved a
fresh slice 11.9 min at fill 9 and 12.6 min at fill 10 while a session's
first turn ran 13-33 min. Six of those seven sessions ran one turn and were
cut by the share; a cut row ranks `served`, so the same aimed tests were
opened and cut again. The schedule now prices the first turn apart from the
turns after it (`fill_schedule.session_turn_price`) and grants the share-cut
exemption once per aimed test set (`UnitService.cut_by_share`). The test
`tests/test_session_floor_recorded.py` replays fills 9-12 of that run against
the ledger the live schedule read; this script writes its fixture.

The rows of that run carry no `first_turn_minutes` (the field did not exist
yet). Each of its session rows is backfilled from the run's own
`phase_c.session.turn` events: minutes from the row's `started` to its first
turn event. That is the measurement the field now records, taken from the
event log instead of the session's clock.

USAGE
-----
    python scripts/session_floor_fixture.py \\
        [--run 450922-661c5e9f] [--cluster stable] [--fills 9 10 11 12] \\
        [-o tests/fixtures/fills/450922-661c5e9f_session_floor.json]

Read-only on logs/: reads logs/events.jsonl (`phase_c.fill.schedule`,
`phase_c.session.start`, `phase_c.session.turn`) and
logs/route_ledger/<project>/index.jsonl. Writes only the fixture.

HOW TO INTERPRET THE OUTPUT
---------------------------
One line per fill: the fill, its planned run count, its ledger cut (rows
visible to that fill's schedule) and its oracle. Then the backfill line:
how many of the run's session rows got a first-turn reading, and how many
had no turn event (a row with none keeps None and prices like a row from
before the field). A backfill count below the run's session-row count with
model calls is a join failure worth reading before trusting the fixture.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _events(path: Path, run_id: str
            ) -> tuple[dict[int, dict], list[tuple[str, datetime]]]:
    """Schedule events of `run_id` by fill, and its session start / turn
    events in order as (event, timestamp)."""
    sched: dict[int, dict] = {}
    turns: list[tuple[str, datetime]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run_id not in line:
                continue
            if ('"phase_c.fill.schedule"' not in line
                    and '"phase_c.session.turn"' not in line
                    and '"phase_c.session.start"' not in line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("run_id") != run_id:
                continue
            if e["event"] == "phase_c.fill.schedule":
                data = e.get("data") or {}
                if data.get("scheduled"):
                    sched[int(data["iter"])] = {**data, "ts": e["ts"]}
            else:
                turns.append((e["event"], _ts(e["ts"])))
    return sched, turns


def _planned(data: dict) -> list[dict]:
    """The planned runs of one schedule event, carved pieces folded back into
    the run they were cut from (`scripts/exemption_replay.py`'s rule)."""
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
    keep = ("kind", "label", "targets", "target_tests", "stubs", "files",
            "seq", "bodies_sha")
    return [{k: r.get(k) for k in keep} for r in planned]


def _backfill(rows: list[dict], turns: list[tuple[str, datetime]],
              run_id: str) -> tuple[int, int]:
    """Set `first_turn_minutes` on `run_id`'s session rows: minutes from the
    row's `started` to the first turn event after the first session start at
    or after it (a row's `started` is truncated to the second, so a turn
    event of the session before it can share that second). Returns
    (filled, missing)."""
    filled = missing = 0
    for r in rows:
        if (r.get("run_id") != run_id or r.get("kind") != "session"
                or not r.get("model_calls") or not r.get("started")):
            continue
        start = _ts(r["started"])
        end = start.timestamp() + 60.0 * float(r.get("minutes") or 0.0) + 5.0
        at = next((i for i, (ev, t) in enumerate(turns)
                   if ev == "phase_c.session.start"
                   and t.timestamp() >= start.timestamp()), None)
        first = None if at is None else next(
            (t for ev, t in turns[at + 1:] if ev == "phase_c.session.turn"),
            None)
        if first is None or first.timestamp() > end:
            missing += 1
            continue
        r["first_turn_minutes"] = round((first - start).total_seconds() / 60.0, 3)
        filled += 1
    return filled, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="450922-661c5e9f")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--fills", type=int, nargs="+", default=[9, 10, 11, 12])
    ap.add_argument("--events", default=str(ROOT / "logs" / "events.jsonl"))
    ap.add_argument("-o", "--out", default=str(
        ROOT / "tests" / "fixtures" / "fills"
        / "450922-661c5e9f_session_floor.json"))
    a = ap.parse_args(argv)

    sched, turns = _events(Path(a.events), a.run)
    ledger_path = ROOT / "logs" / "route_ledger" / a.project / "index.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    rows = [r for r in rows if r.get("cluster") == a.cluster]
    own = [r for r in rows if r.get("run_id") == a.run]
    oracle = own[-1]["oracle_fp"] if own else ""
    # The price reads every session row of the cluster; the ranking reads
    # the rows under the fill's oracle. Nothing else is kept.
    rows = [r for r in rows
            if r.get("kind") == "session" or r.get("oracle_fp") == oracle]
    # Rows of runs after this one, if any, are not part of any cut.
    last = max(i for i, r in enumerate(rows) if r.get("run_id") == a.run)
    rows = rows[:last + 1]
    filled, missing = _backfill(rows, turns, a.run)

    fills: list[dict[str, Any]] = []
    for it in a.fills:
        data = sched[it]
        # The ledger the fill's schedule read: everything before this run's
        # first row at this fill's iteration.
        cut = next((i for i, r in enumerate(rows)
                    if r.get("run_id") == a.run
                    and int(r.get("iteration") or 0) >= it), len(rows))
        fills.append({
            "iter": it, "ts": data["ts"], "budget_minutes": data["budget_minutes"],
            "inputs": data.get("inputs") or {}, "ledger_cut": cut,
            "planned": _planned(data),
            "recorded": [{k: r.get(k) for k in (
                "kind", "label", "reason", "share_minutes", "floor_minutes",
                "floor_note", "deferred", "target_tests", "bodies_sha")}
                for r in [*data.get("runs", ()), *data.get("deferred", ())]],
        })
        print(f"fill {it}: {len(fills[-1]['planned'])} planned run(s), "
              f"ledger cut {cut} of {len(rows)}, oracle {oracle}")
    print(f"backfill: {filled} session row(s) of {a.run} given a first turn, "
          f"{missing} without a turn event")
    out = {"run_id": a.run, "cluster": a.cluster, "oracle_fp": oracle,
           "source": "scripts/session_floor_fixture.py", "rows": rows,
           "fills": fills}
    Path(a.out).write_text(json.dumps(out, indent=0, sort_keys=True) + "\n",
                           encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
