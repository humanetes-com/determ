#!/usr/bin/env python
"""WHY — the fill schedule's coverage-split deferral had no lapse, and a pair
row that paid one leftover restarted the unpaid streak of a leftover it did
not move. This recomputes the recorded fills of a run under the shipped
schedule and under the schedule at git HEAD, on the live route ledger, so the
claim "the split runs now open once the attributed write has spent its grace"
is a computed output with its negative control beside it, not a description.

Run `11358-e5de094e` (2026-09-15, cluster `stable`): fills 1–3 deferred every
run holding the coverage owners its leftovers executed through
(`coverage_split`), three fills running, while the pair of the leftovers had
a ledger of dozens of unpaid tries. Fill 3's pair cashed +2 on two of three
leftovers; from then on the third leftover's pair-unpaid count read 0/3 and 1/3
(`measured-owners: ... (pair unpaid 1/3)`), although no row had moved it.

USAGE

    python scripts/coverage_split_lapse_fixture.py
    python scripts/coverage_split_lapse_fixture.py --run 11358-e5de094e \
        --project anyhow --cluster stable [--events logs/events.jsonl] [--rev HEAD]

Reads only: the route ledger (`logs/route_ledger/<project>/index.jsonl`), one
pass over `logs/events.jsonl` for the run's `phase_c.fill.schedule` events,
and `git show <rev>:pipeline_core/domain/fill_schedule.py` for the control.
No GPU, no vLLM, no workspace write. A few seconds on a 256 MB events file.

The inputs the schedule event does not record are reconstructed and the
reconstruction is CHECKED before anything is computed on it:
`attributed` (the driver's snapshot) is taken as the fill's PAIR targets, and
`hollow` / `crash_blamed` / `unmeasurable` as empty. The `record` line
compares the control schedule's pair_wait / coverage_split deferrals on those
inputs with the deferrals the event recorded; `MATCH` means the fixture is the
record, `DIFFERS` means a missing input matters and the rest of that fill's
block is not evidence.

HOW TO INTERPRET THE OUTPUT

Per recorded fill:

* `ledger  N row(s) before the fill` — rows whose `started` precedes the
  fill's schedule event: what that schedule could read.
* `leftovers` — every aimed test of the fill, with `attributed-writes k/g`
  (graded non-split writes aimed at it since one turned it green, the count
  the coverage-split bound reads), `pair k/g` (the same over PAIR rows, the
  count pair_wait and the measured-owners withholding read), and
  `row-level pair k` (the old walk, which stopped at any cashing row).
* `record` — MATCH / DIFFERS, as above.
* `control (<rev>)` — the HEAD schedule: which runs it opened, which it
  deferred and why.
* `shipped` — the working-tree schedule on the same inputs: opened runs with
  `grace spent: ...` where a deferral lapsed, and what is still deferred.

The decision rule: on a fill whose record MATCHes, a run the control defers
`coverage_split` and the shipped schedule opens with `grace spent:
coverage_split` is the lapse firing on the live record; a run both defer is a
leftover whose attributed write has not spent its grace yet (read its
`attributed-writes` count). Legacy rows (written before the green set was
recorded) answer at row level, so on this ledger a cash before the field
existed still stops the per-test walk — the `row-level` and per-test counts
agree up to such a row and diverge only after rows carry the set — except
where the same process aimed a later row at the leftover, which is recorded
evidence the cash did not turn it green (`row_paid_test`, `aimed_later`).

The closing `next process` block reads the WHOLE ledger, as a relaunch will:
per-leftover counts, and the measured-owners withholding under the shipped
and the control walk. A leftover in the control's withholding and not in the
shipped one is a leftover whose measured owners the next iteration admits.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_core.domain.fill_schedule as shipped  # noqa: E402
from pipeline_core.domain.models import FnLocator  # noqa: E402


def _control_module(rev: str) -> ModuleType:
    src = subprocess.run(
        ["git", "show", f"{rev}:pipeline_core/domain/fill_schedule.py"],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout
    tmp = Path(tempfile.mkdtemp()) / "fill_schedule_control.py"
    tmp.write_text(src)
    spec = importlib.util.spec_from_file_location("fill_schedule_control", tmp)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fill_schedule_control"] = mod
    spec.loader.exec_module(mod)
    return mod


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _schedules(events: Path, run: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run not in line or "phase_c.fill.schedule" not in line:
                continue
            d = json.loads(line)
            if d.get("event") == "phase_c.fill.schedule":
                out.append((d["ts"], d.get("data") or {}))
    return out


def _loc(text: str) -> FnLocator:
    f, _, fn = text.rpartition("::")
    return FnLocator(f, fn)


def _run(mod: ModuleType, raw: dict[str, Any]) -> Any:
    return mod.RouteRun(
        kind=mod.RouteKind(raw["kind"]), label=raw["label"],
        targets=frozenset(_loc(t) for t in raw.get("targets") or ()),
        target_tests=frozenset(raw.get("target_tests") or ()),
        stubs_in_targets=frozenset(_loc(t) for t in raw.get("stubs") or ()),
        files=tuple(raw.get("files") or ()), seq=int(raw.get("seq") or 0),
        bodies_sha=raw.get("bodies_sha") or "")


def _ledger(mod: ModuleType, project: str, cluster: str) -> list[Any]:
    path = ROOT / "logs" / "route_ledger" / project / "index.jsonl"
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if cluster and raw.get("cluster") not in ("", cluster):
                continue
            r = mod.RouteLedgerRow.from_dict(raw)
            if r is not None:
                rows.append(r)
    return rows


def _render(sched: Any) -> list[str]:
    lines = []
    for s in sched.ordered:
        lapsed = getattr(s, "lapsed", ())
        lines.append(f"      open   {s.run.label} ({s.reason}"
                     + (f"; grace spent: {', '.join(lapsed)}" if lapsed else "")
                     + ")")
    for s in sched.deferred:
        lines.append(f"      defer  {s.run.label} ({s.reason})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--run", default="11358-e5de094e")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--rev", default="HEAD")
    args = ap.parse_args()

    control = _control_module(args.rev)
    rows_new = _ledger(shipped, args.project, args.cluster)
    rows_old = _ledger(control, args.project, args.cluster)
    grace = shipped.DEFAULT_UNPAID_GRACE
    fills = _schedules(ROOT / args.events, args.run)
    print(f"run {args.run}: {len(fills)} recorded fill schedule(s); ledger "
          f"{len(rows_new)} row(s); grace {grace}; control {args.rev}")
    for ts, data in fills:
        # A row's `started` is written to the second, and the first run of a
        # fill starts in the same second its schedule is emitted; comparing
        # against the millisecond timestamp would read that run's own row as
        # history the schedule had.
        cut = _ts(ts).replace(microsecond=0)
        before_new = [r for r in rows_new if r.started and _ts(r.started) < cut]
        before_old = [r for r in rows_old if r.started and _ts(r.started) < cut]
        oracle = next((r.oracle_fp for r in reversed(before_new)
                       if r.run_id == args.run and r.oracle_fp), "")
        if not oracle:
            oracle = next((r.oracle_fp for r in reversed(rows_new)
                           if r.run_id == args.run and r.oracle_fp), "")
        raws = list(data.get("runs") or ()) + list(data.get("deferred") or ())
        runs_new = [_run(shipped, r) for r in raws]
        runs_old = [_run(control, r) for r in raws]
        attributed = frozenset().union(*(
            r.targets for r in runs_new if r.kind is shipped.RouteKind.PAIR))
        budget = float(data.get("budget_minutes") or 0.0)
        print(f"\nfill {data.get('iter')} at {ts[:19]}: {len(raws)} candidate "
              f"run(s); ledger {len(before_new)} row(s) before the fill; "
              f"oracle {oracle[:12]}")
        tests = sorted({t for r in runs_new for t in r.target_tests})
        for t in tests:
            aw = shipped.unpaid_attributed_writes(before_new, t, attributed, oracle)
            pr = shipped.unpaid_pair_buys(before_new, t, oracle)
            old = control.unpaid_pair_buys(before_old, t, oracle) if hasattr(
                control, "unpaid_pair_buys") else "n/a"
            print(f"    leftover {t}: attributed-writes {aw}/{grace}, "
                  f"pair {pr}/{grace}, row-level pair {old}")
        ctl = control.schedule(
            runs_old, before_old, oracle_fp=oracle, hollow={},
            budget_minutes=budget, attributed=attributed,
            max_per_kind={control.RouteKind.SESSION: 4})
        new = shipped.schedule(
            runs_new, before_new, oracle_fp=oracle, hollow={},
            budget_minutes=budget, attributed=attributed,
            max_per_kind={shipped.RouteKind.SESSION: 4})
        recorded = {(r["label"], r["reason"]) for r in data.get("deferred") or ()
                    if r.get("reason") in ("coverage_split", "pair_wait")}
        recomputed = {(s.run.label, s.reason) for s in ctl.deferred
                      if s.reason in ("coverage_split", "pair_wait")}
        print("    record  " + ("MATCH" if recorded == recomputed else
                                f"DIFFERS recorded={sorted(recorded)} "
                                f"control={sorted(recomputed)}"))
        print(f"    control ({args.rev})")
        print("\n".join(_render(ctl)))
        print("    shipped")
        print("\n".join(_render(new)))
    # The ledger as the NEXT process will read it: every row, including the
    # last fill's. The withholding decides whether the leftovers' measured
    # owners enter the frontier at all; the counts decide whether a run that
    # holds one then opens.
    if fills:
        _ts_last, last = fills[-1]
        oracle = next((r.oracle_fp for r in reversed(rows_new)
                       if r.run_id == args.run and r.oracle_fp), "")
        tests = sorted({t for r in (last.get("runs") or ()) + (last.get("deferred") or ())
                        for t in r.get("target_tests") or ()})
        pair_targets = frozenset().union(*(
            _run(shipped, r).targets
            for r in (last.get("runs") or ()) + (last.get("deferred") or ())
            if r.get("kind") == shipped.RouteKind.PAIR.value))
        from pipeline_core.domain.escalation_lifetime import (
            withholding_paired_tests,
        )
        unpaid = {t: shipped.unpaid_pair_buys(rows_new, t, oracle) for t in tests}
        old_unpaid = {t: control.unpaid_pair_buys(rows_old, t, oracle)
                      for t in tests}
        print(f"\nnext process: ledger {len(rows_new)} row(s), oracle {oracle[:12]}")
        for t in tests:
            aw = shipped.unpaid_attributed_writes(rows_new, t, pair_targets, oracle)
            print(f"    leftover {t}: attributed-writes {aw}/{grace}, "
                  f"pair {unpaid[t]}/{grace}, row-level pair {old_unpaid[t]}")
        print(f"    measured-owners withholding  shipped: "
              f"{sorted(withholding_paired_tests(tests, unpaid_buys=unpaid))}")
        print(f"    measured-owners withholding  control: "
              f"{sorted(withholding_paired_tests(tests, unpaid_buys=old_unpaid))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
