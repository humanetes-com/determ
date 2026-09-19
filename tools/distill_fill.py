#!/usr/bin/env python3
"""Distil one fill's schedule inputs from `logs/events.jsonl` into a fixture.

WHY. The walk harness (`tests/walks/harness.py`) replays the TDD loop with
the FILL stubbed whole, so no walk can assert anything about the order the
routes ran in or the budget they had. The fill schedule
(`domain/fill_schedule.py`) decides exactly that, and its recorded fixtures
have to come from the record the way the walks' do — from the events the
loop wrote, never from a hand-made list of units.

WHAT IT READS. For `--run RUN_ID --iter N`:
  - `phase_c.session.plan` of that fill: the leftover tests, each test's
    executed slice (site labels), the packed sessions, the hollow count;
  - `phase_c.fill.schedule` when the fill ran under the schedule (post-fix
    logs) — its runs are the candidates verbatim (`schedule_runs`);
  - `phase_c.tdd.iter` of that fill: `failed`, `failed_pairs`, `incomplete`
    so a recorded test can derive the unmeasurable set (failed names
    with no completed-binary pair);
  - `phase_c.crash.localized` inside the fill: the blamed locators;
  - `phase_c.fill.route_plan` / `phase_c.file.fill.start` of that fill —
    which files the file route opened (pre-fix logs);
  - `phase_c.route.run` rows of EARLIER fills of the same cluster, as the
    ledger the schedule would have read;
  - for each file run of THIS fill: its `phase_c.file.fill.revert`
    attempts (attempt number, `spliced`, `planned`, `retrying`), its
    `phase_c.file.fill.done`, its `phase_c.file.installable_offered`
    complement and its own `phase_c.route.run` row — the attempt sequence
    one route run went through (`route_attempts`), so the harness can
    replay a revert-then-keep through the fill's attempt bookkeeping.

WHAT `route_attempts` DERIVES, named per run under `source`. Events carry
counts, not names, for a file-first attempt: the row's `reverted` list is
the names attempt 1 landed and reverted (pre-fix rows hold the union over
every reverting attempt — one revert event means it is exact); the units
the run DISPATCHED are the schedule run's targets plus the installable
complement offered for that file (`planned` on the revert event checks the
count); and the names a following keep landed are the row's `kept` list
when the row records one, else the dispatched set — a compile revert does
not restrict the retry's plan, and the retry's `fns_filled` equalled the
reverted attempt's `spliced` on the fills distilled so far.

WHAT IT CANNOT READ, and how the fixture says so. Pre-fix events carry no
stub inventory. `--stubs a::b,c::d` names the stubs the fixture assumes;
the fixture records them under `stubs` with `stubs_source: "argument"`, and
the recorded test checks that the hollow relation they induce on the
recorded slices reproduces the plan event's `hollow` count — the fixture is
wrong when it does not, and the test says so instead of passing. Post-fix
events carry the stubs on the schedule's runs and need no argument.

USAGE
    python tools/distill_fill.py --run 334902-2774708c --iter 11 \
        --stubs src/error.rs::object_downcast,src/error.rs::object_boxed \
        -o tests/fixtures/fills/334902-2774708c_iter11.json

HOW TO READ THE OUTPUT. The fixture is JSON: `run_id`, `iter`, `cluster`,
`tests` (name -> [sites]), `sessions` ([{tests, units}] as packed),
`hollow_recorded` (the plan event's count), `stubs`, `stubs_source`,
`file_runs` ([{file, targets, stubs}] — from the schedule event when present,
else one per file that holds a stub, targets = that file's sites in the
slices), `schedule_runs` (the schedule event's candidate runs, all kinds),
`failed` / `failed_pairs` / `incomplete` (the iter event), `crash_blamed`
([[file, fn], ...] from crash.localized), `ledger_rows` (prior route rows
for the cluster: with `ledger_source: "index"` the first N cluster rows of
`logs/route_ledger/<project>/index.jsonl`, N being the `ledger N row(s)`
the schedule loaded, kept for the candidates' work keys and every hop-pair
row; with `"events"` the stream's rows, for fills whose schedule event
predates the count), `route_attempts` ([{label, dispatched, planned_recorded,
attempts: [{outcome, units}], recorded_outcome, source}] for this fill's
file runs that went through more than one attempt or recorded a revert),
`classes` ([{unit, route, class, keep, net_pass_delta, newly_red,
reading_run}] — every `phase_c.fill.class` of the fill with the fill
reading that preceded it: the latest `phase_c.fill.reading` of the same
route before the class event, which is the keep site's own measurement
since a keep classifies right after its gate; `reading_run` names the run
label the reading carried so a mismatch is visible, and a class with no
reading before it carries `net_pass_delta: null`. `keep` says the class
was charged at a KEEP site — `cashed` / `unreachable` always are, and a
`wrong` is when the event right before it is the file route's
`phase_c.file.fill.mismatch` for the same group; every other class is a
revert, a drop or a decline and carries no reading of its own). The class
fold test refolds the keep classes through `class_for_kept` with the
reading (the fix) and without it (the pre-fix input shape, the negative
control) and compares the histograms with the recorded one.
The recorded test builds the candidate runs from it and asserts the plan's
sentence about the order, with the fixed pre-schedule walk as the negative
control; a run with `route_attempts` is replayed through the fill's attempt
bookkeeping and its row's outcome, kept and reverted are the sentence.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _events(path: Path, run_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run_id not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if str(ev.get("run_id", "")) != run_id:
                continue
            out.append(ev)
    return out


def _work_key(kind: str, targets: Iterable[Any]) -> str:
    """The schedule's served-under-another-identity key, as a string —
    kind plus targets, with the bodies sha deliberately OUT. That is the
    key a route's history accumulates under: an identity carrying the
    bodies sha restarts whenever a run keeps any body at all."""
    # The two events spell a locator differently — the schedule event as
    # "file::fn", the route-run row as ["file", "fn"] — so normalise
    # before comparing. Matching the raw spellings silently found nothing
    # and the fixture came back with an empty history.
    names = sorted(
        f"{t[0]}::{t[1]}" if isinstance(t, (list, tuple)) and len(t) == 2
        else str(t)
        for t in targets or ())
    return f"{kind}|{','.join(names)}"


def _prior_candidate_rows(path: Path, cluster: str,
                          schedule_runs: list[dict[str, Any]],
                          run_id: str) -> list[dict[str, Any]]:
    """`phase_c.route.run` rows of OTHER runs, for this fill's candidates.

    Scoped to the candidate runs' work keys and this cluster, so the
    fixture carries the history the schedule would have read for the runs
    it is choosing among, and not the rest of a ledger that grows without
    bound. Ordered as the stream is, which is time.
    """
    wanted = {_work_key(str(r.get("kind", "")), r.get("targets") or [])
              for r in schedule_runs}
    if not wanted:
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "phase_c.route.run" not in line or cluster not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if _kind(ev) != "phase_c.route.run":
                continue
            if str(ev.get("run_id", "")) == run_id:
                continue          # this run's rows come from the slice above
            d = ev.get("data") or {}
            if str(d.get("cluster", "")) != cluster:
                continue
            if _work_key(str(d.get("kind", "")),
                         d.get("targets") or []) in wanted:
                out.append(d)
    return out


def _index_prior_rows(index: Path, cluster: str, count: int,
                      schedule_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first `count` rows of the cluster in the route-ledger index —
    the rows the schedule itself loaded (`ledger N row(s)` on its line) —
    kept for the fill's candidate work keys and for every hop-pair row.

    The event stream is not the ledger: it rotates, and a row the stream
    no longer holds is still read by the schedule. Pair rows are kept
    whatever their targets because the per-test pay clock
    (`unpaid_tries_for_test`) reads every pair row aimed at a test, not
    only the rows of the candidate pair. Run `3893442-cb547388` fill 1
    (2026-09-19) distilled from the stream alone did not reproduce its own
    recorded schedule line: the display pair's grace read as unspent and
    every run behind it came back `pair_wait`."""
    wanted = {_work_key(str(r.get("kind", "")), r.get("targets") or [])
              for r in schedule_runs}
    out: list[dict[str, Any]] = []
    seen = 0
    try:
        fh = index.open(encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            if seen >= count:
                break
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict) or str(raw.get("cluster", "")) != cluster:
                continue
            seen += 1
            kind = str(raw.get("kind", ""))
            if kind == "pair" or _work_key(kind, raw.get("targets") or []) in wanted:
                out.append(raw)
    return out


def _kind(ev: dict[str, Any]) -> str:
    return str(ev.get("event") or ev.get("kind") or "")


def _site(label: str) -> dict[str, str]:
    """`src/error.rs::fmt (impl Debug for Error)` -> file, fn, container."""
    head, _, cont = label.partition(" (")
    file, _, fn = head.partition("::")
    return {"file": file, "fn": fn, "container": cont.rstrip(")")}


def _route_attempts(fill: list[dict[str, Any]], iter_n: int) -> list[dict[str, Any]]:
    """The attempt sequence of each file run of this fill that reverted at
    least once, from the fill's own events (see the module docstring for
    what is derived and why)."""
    reverts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    done: dict[str, dict[str, Any]] = {}
    offered: dict[str, list[str]] = defaultdict(list)
    for e in fill:
        d = e.get("data") or {}
        k = _kind(e)
        if k == "phase_c.file.fill.revert":
            reverts[str(d.get("file", ""))].append(d)
        elif k == "phase_c.file.fill.done":
            done[str(d.get("file", ""))] = d
        elif k == "phase_c.file.installable_offered":
            offered[str(d.get("file", ""))].extend(str(f) for f in d.get("fns") or [])
    out: list[dict[str, Any]] = []
    for e in fill:
        if _kind(e) != "phase_c.route.run":
            continue
        row = e.get("data") or {}
        if row.get("kind") != "file" or int(row.get("iteration", -1)) != iter_n:
            continue
        file = str((row.get("label") or "").removeprefix("file "))
        if not reverts.get(file):
            continue
        targets = [f"{f}::{fn}" for f, fn in (row.get("targets") or [])]
        dispatched = sorted(set(targets) | {f"{file}::{fn}" for fn in offered.get(file, [])})
        reverted = sorted(f"{f}::{fn}" for f, fn in (row.get("reverted") or []))
        kept_row = sorted(f"{f}::{fn}" for f, fn in (row.get("kept") or []))
        attempts = [{"outcome": "reverted", "units": reverted,
                     "attempt": int(r.get("attempt", 0)),
                     "spliced": int(r.get("spliced", 0))}
                    for r in reverts[file]]
        landed = int(done.get(file, {}).get("fns_filled", 0) or 0)
        source = ("row.reverted per revert event; dispatched = targets + "
                  "installable_offered")
        if landed and reverts[file][-1].get("retrying"):
            attempts.append({"outcome": "kept",
                             "units": kept_row or dispatched,
                             "attempt": len(attempts) + 1, "spliced": landed})
            source += ("; kept = row.kept" if kept_row
                       else "; kept = dispatched (derived: unrestricted retry, "
                            "fns_filled == spliced)")
        out.append({
            "label": str(row.get("label", "")), "dispatched": dispatched,
            "planned_recorded": int(reverts[file][0].get("planned", 0) or 0),
            "attempts": attempts,
            "recorded_outcome": str(row.get("outcome", "")),
            "recorded_kept": len(row.get("kept") or []),
            "recorded_reverted": len(row.get("reverted") or []),
            "source": source})
    return out


def _classes(fill: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every fill class of the fill, paired with the latest fill reading
    of the same route that preceded it (see the module docstring)."""
    last_reading: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for e in fill:
        k = _kind(e)
        if k == "phase_c.fill.reading":
            last_reading[str(e.get("route") or "")] = e
            continue
        if k != "phase_c.fill.class":
            prev = e
            continue
        d = e.get("data") or {}
        route = str(d.get("route") or "")
        rd = last_reading.get(route)
        cls = str(d.get("class", ""))
        mismatch = (prev is not None and _kind(prev) == "phase_c.file.fill.mismatch"
                    and str((prev.get("data") or {}).get("group", "")) == str(d.get("unit", "")))
        prev = e
        out.append({
            "ts": str(e.get("ts", "")),
            "unit": str(d.get("unit", "")), "route": route,
            "class": cls,
            "keep": cls in ("cashed", "unreachable") or (cls == "wrong" and mismatch),
            "net_pass_delta": (int(rd.get("net_pass_delta", 0) or 0)
                               if rd is not None else None),
            "newly_red": list(rd.get("newly_red") or []) if rd is not None else [],
            "reading_run": str(rd.get("run") or "") if rd is not None else "",
        })
    return out


def distil(run_id: str, iter_n: int, stubs: list[str], *,
           events_path: Path,
           ledger_index: Path | None = None) -> dict[str, Any]:
    evs = _events(events_path, run_id)
    iters = [e for e in evs if _kind(e) == "phase_c.tdd.iter"]
    # The fill of iteration N starts at the N-th `phase_c.tdd.iter` and ends
    # at the next one (or the end of the run).
    starts = [i for i, e in enumerate(evs) if _kind(e) == "phase_c.tdd.iter"
              and int((e.get("data") or {}).get("iter", -1)) == iter_n]
    if not starts:
        raise SystemExit(f"no phase_c.tdd.iter {iter_n} for run {run_id} "
                         f"({len(iters)} iteration event(s) found)")
    start = starts[-1]
    end = next((i for i in range(start + 1, len(evs))
                if _kind(evs[i]) == "phase_c.tdd.iter"), len(evs))
    fill = evs[start:end]
    cluster = str((evs[start].get("data") or {}).get("cluster", ""))
    plan = next((e for e in fill if _kind(e) == "phase_c.session.plan"), None)
    if plan is None:
        raise SystemExit(f"no phase_c.session.plan inside iteration {iter_n}")
    pd = plan.get("data") or {}
    tests = {t: [_site(s) for s in sites]
             for t, sites in dict(pd.get("slices") or {}).items()}
    sessions = [{"tests": list(s.get("tests") or []), "units": int(s.get("units", 0))}
                for s in (pd.get("sessions") or [])]
    sched = next((e for e in fill if _kind(e) == "phase_c.fill.schedule"), None)
    file_runs: list[dict[str, Any]] = []
    stubs_source = "argument"
    stub_set = set(stubs)
    if sched is not None:
        sd = sched.get("data") or {}
        for r in list(sd.get("runs") or []) + list(sd.get("deferred") or []):
            if r.get("kind") == "file":
                file_runs.append({"file": (r.get("files") or [""])[0],
                                  "targets": list(r.get("targets") or []),
                                  "stubs": list(r.get("stubs") or [])})
            for st in r.get("stubs") or []:
                stub_set.add(str(st))
        if stub_set:
            stubs_source = "schedule_event"
    if not file_runs:
        by_file: dict[str, set[str]] = defaultdict(set)
        for sites in tests.values():
            for s in sites:
                by_file[s["file"]].add(f"{s['file']}::{s['fn']}")
        for f, locs in sorted(by_file.items()):
            f_stubs = sorted(loc for loc in locs if loc in stub_set)
            if f_stubs:
                file_runs.append({"file": f, "targets": sorted(locs),
                                  "stubs": f_stubs})
    ledger_rows = [e.get("data") or {} for e in evs[:start]
                   if _kind(e) == "phase_c.route.run"
                   and str((e.get("data") or {}).get("cluster", "")) == cluster]
    idat = evs[start].get("data") or {}
    schedule_runs: list[dict[str, Any]] = []
    if sched is not None:
        sd = sched.get("data") or {}
        # Both halves of the event. Taking `runs` alone recorded only what
        # the schedule OPENED, so a fill whose defect is what it deferred
        # distilled to a one-run fixture and the deferred set — the thing
        # under test — was not in the record at all (fill 2 of run
        # `681204-b695ab3f`, 2026-09-14: one pair opened, six runs
        # pair_wait). The candidate set is the union; `deferred` on each
        # raw run says which side it was on.
        schedule_runs = list(sd.get("runs") or []) + list(
            sd.get("deferred") or [])
    # The ledger a schedule reads is CROSS-RUN and this distiller sees one
    # run, so the rows above are only a tail of it. Whatever history the
    # fill's own candidates have is the part a recorded test needs: a rule
    # that keys on "this route, these targets, graded N times without
    # paying" is invisible in a five-row tail of a 674-row ledger (run
    # `681204-b695ab3f` fill 2, 2026-09-14). Carry the earlier rows of
    # those same candidates too, from the whole stream and nothing else,
    # so the fixture stays small and the history in it is the record.
    loaded = int(((sched or {}).get("data") or {}).get("ledger_rows", 0) or 0)
    ledger_source = "events"
    if ledger_index is not None and loaded and ledger_index.exists():
        # The index prefix the schedule read supersedes the stream: it holds
        # this run's earlier rows too, in the order they were appended.
        ledger_rows = _index_prior_rows(ledger_index, cluster, loaded,
                                        schedule_runs)
        ledger_source = "index"
    else:
        ledger_rows = _prior_candidate_rows(
            events_path, cluster, schedule_runs, run_id) + ledger_rows
    crash_blamed: list[list[str]] = []
    seen_blame: set[tuple[str, str]] = set()
    for e in fill:
        if _kind(e) != "phase_c.crash.localized":
            continue
        blamed = (e.get("data") or {}).get("blamed")
        if not (isinstance(blamed, list) and len(blamed) == 2):
            continue
        pair = (str(blamed[0]), str(blamed[1]))
        if pair in seen_blame:
            continue
        seen_blame.add(pair)
        crash_blamed.append([pair[0], pair[1]])
    return {
        "run_id": run_id, "iter": iter_n, "cluster": cluster,
        "ts": str(plan.get("ts", "")),
        "route_attempts": _route_attempts(fill, iter_n),
        "tests": tests, "sessions": sessions,
        "hollow_recorded": int(pd.get("hollow", 0) or 0),
        "fitted_recorded": int(pd.get("fitted", 0) or 0),
        "stubs": sorted(stub_set), "stubs_source": stubs_source,
        "file_runs": file_runs, "ledger_rows": ledger_rows,
        "ledger_source": ledger_source,
        "schedule_runs": schedule_runs,
        "failed": list(idat.get("failed") or []),
        "failed_pairs": [list(p) for p in (idat.get("failed_pairs") or [])],
        "incomplete": list(idat.get("incomplete") or []),
        "crash_blamed": crash_blamed,
        "classes": _classes(fill),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--stubs", default="",
                    help="comma-separated file::fn locators assumed hollow")
    ap.add_argument("--events", type=Path, default=REPO / "logs" / "events.jsonl")
    ap.add_argument("--ledger-index", type=Path,
                    default=REPO / "logs" / "route_ledger" / "anyhow" / "index.jsonl",
                    help="route-ledger index; its first N cluster rows (N from "
                         "the schedule event) are the ledger the fill read")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args(argv)
    stubs = [s.strip() for s in args.stubs.split(",") if s.strip()]
    fixture = distil(args.run, args.iter, stubs, events_path=args.events,
                     ledger_index=args.ledger_index)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(f"wrote {args.out}: {len(fixture['tests'])} test(s), "
          f"{len(fixture['sessions'])} session(s), {len(fixture['file_runs'])} "
          f"file run(s), {len(fixture.get('schedule_runs') or [])} "
          f"schedule run(s), {len(fixture['stubs'])} stub(s) "
          f"({fixture['stubs_source']}), hollow recorded "
          f"{fixture['hollow_recorded']}, {len(fixture['ledger_rows'])} "
          f"ledger row(s), {len(fixture['route_attempts'])} run(s) with "
          "recorded attempts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
