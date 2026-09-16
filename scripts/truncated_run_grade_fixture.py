#!/usr/bin/env python
"""WHY — a route run whose every fill reply was cut mid-deliberation was rowed
as a graded try, so the next schedule ranked the route on a measurement that
never happened; and the clock that lapses a pair's right to defer every
competing run at share zero skipped exactly those rows, so grading them
honestly would have made the lapse arrive LATER. The two changes have to ship
together, and "together" is a claim about live records, not a description.
This recomputes them on the ledger and the events of a recorded run.

Run `366282-7c585f74` (2026-09-15, cluster `nightly`) is the instance. Fill 2
opened three pair runs and one file run in 49 minutes; two of the pair runs
made ONE fill call each and both were cut at 120,000 reasoning chars, their
forced completions carrying 517 and 599 chars — one of them re-emitting the
bodies already on the tree, logged FILLED and kept on non-regression. All four
runs rowed as graded tries, the iteration's own audit read `0 of 12 dispatched
unit(s) landed a semantic change`, and the sweep that followed was the run's
only flat one. Those same four graded rows are what spent the pair's grace, so
fill 3 opened the file runs the previous fill had deferred, and they cashed
+7 and +7.

USAGE

    python scripts/truncated_run_grade_fixture.py
    python scripts/truncated_run_grade_fixture.py --run 366282-7c585f74 \
        --project anyhow --cluster nightly [--events logs/events.jsonl]
        [--rev HEAD] [--fill 3]

Reads only: the route ledger (`logs/route_ledger/<project>/index.jsonl`), one
pass over `logs/events.jsonl`, and `git show <rev>:` for the control module.
No GPU, no vLLM, no workspace write.

The join the events do not record directly: a stream row carries no units, so
a call is assigned to the route run whose [started, started+minutes] window
holds its REQUEST, and a call is a FILL call unless its caller is the
retrospective ask (the unjoined diagnosis call the pair makes after a revert).
A row whose window overlaps another's is printed `overlap` and its grade is
not claimed — the assignment is not unique there.

HOW TO INTERPRET THE OUTPUT

* `runs` — per graded route run: the fill calls assigned to it, how many were
  cut (`finish_reason=length` or `stop=think_cap` or a forced completion), the
  outcome the ledger recorded, and `-> unmeasured` where the shipped grading
  would have rowed it UNMEASURED instead. That line IS the claim "these runs
  were not measured"; if it is empty the run's fill replies finished.
* `fill N` — the recorded fill's schedule recomputed three ways on the same
  candidate runs, with the ledger rows the fill could read:
  - `record` — the control (git `<rev>`) on the ledger AS WRITTEN. The
    claim under test is about pair_wait, so what must reproduce is the
    fill's pair_wait deferrals; `pair_wait MATCH` says so. The
    coverage-split half does NOT reproduce and is not claimed: it reads the
    driver's `attributed` snapshot (pair locators plus the attributed units
    of every leftover test, taken before owner-map admission), which no
    event records, and this script approximates it with the fill's pair
    targets. That approximation is held IDENTICAL across the three
    recomputations below, so every difference between them is the regrade
    and the clock; it is not evidence about the absolute schedule.
  - `control + regrade` — the control walk on the ledger with the truncated
    rows flipped to UNMEASURED: the trap. The control's grace clock skips
    UNMEASURED rows, so the pair's streak shrinks and the competing runs stay
    deferred.
  - `shipped + regrade` — the working tree on the same flipped ledger: the
    clock counts a fill the route SPENT, so the lapse still fires.

The decision rule: the fix is only safe if `shipped + regrade` opens the runs
that `record` opened. A run `record` opens and `control + regrade` defers is
the fill the honest grade would have cost on its own — the reason the two
changes are one change.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline_core.domain.fill_schedule as shipped  # noqa: E402
from pipeline_core.domain.models import FnLocator  # noqa: E402
from pipeline_core.domain.reasoning_cap import is_reasoning_capped  # noqa: E402

_RETRO = "_ask_retrospective"


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


def _events(path: Path, run: str) -> list[dict[str, Any]]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("run_id") == run:
                out.append(d)
    return out


def _calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(request ts, thread, caller, cut?) per model call, paired request →
    the next stream row on the same thread."""
    reqs = [e for e in events if e.get("event") == "llm.request"]
    metas = [e for e in events if e.get("event") == "llm.stream_meta"]
    used: set[int] = set()
    calls = []
    for r in reqs:
        meta = None
        for i, m in enumerate(metas):
            if i in used or m["thread"] != r["thread"] or m["ts"] < r["ts"]:
                continue
            used.add(i)
            meta = m
            break
        calls.append({
            "ts": _ts(r["ts"]), "thread": r["thread"],
            "caller": str((r.get("data") or {}).get("caller") or ""),
            "cut": bool(meta) and is_reasoning_capped(meta.get("data") or {}),
            "meta": (meta or {}).get("data") or {}})
    return calls


def _loc(text: str) -> FnLocator:
    f, _, fn = text.rpartition("::")
    return FnLocator(f, fn)


def _run_of(mod: ModuleType, raw: dict[str, Any]) -> Any:
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
        lines.append(f"        open   {s.run.label} ({s.reason}"
                     + (f"; grace spent: {', '.join(lapsed)}" if lapsed else "")
                     + ")")
    for s in sched.deferred:
        lines.append(f"        defer  {s.run.label} ({s.reason})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="nightly")
    ap.add_argument("--run", default="366282-7c585f74")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--fill", type=int, default=3)
    args = ap.parse_args()

    control = _control_module(args.rev)
    events = _events(ROOT / args.events, args.run)
    calls = _calls(events)
    rows_new = _ledger(shipped, args.project, args.cluster)
    rows_old = _ledger(control, args.project, args.cluster)
    grace = shipped.DEFAULT_UNPAID_GRACE
    mine = [r for r in rows_new if r.run_id == args.run]
    print(f"run {args.run}, cluster {args.cluster}: {len(mine)} row(s), "
          f"{len(calls)} model call(s); grace {grace}; control {args.rev}")

    windows = [(r, _ts(r.started), r.minutes) for r in mine if r.started]
    regrade: set[tuple[str, str]] = set()   # (label, started) of rows to flip
    print("\n  runs")
    for r, start, minutes in windows:
        if minutes <= 0:
            continue
        end = start.timestamp() + minutes * 60.0
        # A wave runs its runs concurrently and their windows genuinely
        # overlap, so no call can be assigned to one of them. Sequential
        # runs abut — a row's `started` is written to the second and the
        # next opens in the same one — so only an intersection longer than
        # that rounding counts.
        overlap = any(
            o is not r and o_min > 0
            and min(end, o_start.timestamp() + o_min * 60.0)
            - max(start.timestamp(), o_start.timestamp()) > 30.0
            for o, o_start, o_min in windows)
        own = [c for c in calls
               if start.timestamp() <= c["ts"].timestamp() < end]
        fill_calls = [c for c in own if _RETRO not in c["caller"]]
        cut = [c for c in fill_calls if c["cut"]]
        verdict = ""
        if fill_calls and len(cut) == len(fill_calls) and not overlap:
            verdict = "  -> unmeasured"
            regrade.add((r.label, r.started))
        print(f"    {r.started[11:19]}  {minutes:5.1f}m  "
              f"{r.outcome.value:<9} {r.label[:58]:<58} "
              f"fill calls {len(fill_calls)}, cut {len(cut)}"
              + ("  (overlap)" if overlap else "") + verdict)

    scheds = [(e["ts"], e.get("data") or {}) for e in events
              if e.get("event") == "phase_c.fill.schedule"]
    want = [(ts, d) for ts, d in scheds if int(d.get("iter") or 0) == args.fill]
    if not want:
        print(f"\n  no recorded schedule for fill {args.fill}")
        return 1
    ts, data = want[0]
    cut_at = _ts(ts).replace(microsecond=0)
    raws = list(data.get("runs") or ()) + list(data.get("deferred") or ())
    oracle = next((r.oracle_fp for r in reversed(mine) if r.oracle_fp), "")
    budget = float(data.get("budget_minutes") or 0.0)

    def _before(rows: list[Any], flip: bool, mod: ModuleType) -> list[Any]:
        out = []
        for r in rows:
            if not r.started or _ts(r.started) >= cut_at:
                continue
            if flip and (r.label, r.started) in regrade:
                r = replace(r, outcome=mod.RunOutcome.UNMEASURED)
            out.append(r)
        return out

    attributed = frozenset().union(*(
        frozenset(_loc(t) for t in (raw.get("targets") or ()))
        for raw in raws if raw.get("kind") == "pair"), frozenset())
    print(f"\n  fill {args.fill} at {ts[:19]}: {len(raws)} candidate run(s), "
          f"oracle {oracle[:12]}, budget {budget:.0f}m, "
          f"{len(regrade)} row(s) regraded")

    def _sched(mod: ModuleType, flip: bool, rows: list[Any]) -> Any:
        return mod.schedule(
            [_run_of(mod, raw) for raw in raws], _before(rows, flip, mod),
            oracle_fp=oracle, hollow={}, budget_minutes=budget,
            today_order=[mod.RouteKind(k) for k in
                         ("session", "pair", "file", "impl", "per-fn")],
            attributed=attributed)

    recorded_open = {raw["label"] for raw in (data.get("runs") or ())}
    recorded_wait = {raw["label"] for raw in (data.get("deferred") or ())
                     if raw.get("reason") == "pair_wait"}
    for name, mod, flip, rows in (
            ("record", control, False, rows_old),
            ("control + regrade", control, True, rows_old),
            ("shipped + regrade", shipped, True, rows_new)):
        sched = _sched(mod, flip, rows)
        opened = {s.run.label for s in sched.ordered}
        tag = ""
        if name == "record":
            wait = {s.run.label for s in sched.deferred
                    if s.reason == "pair_wait"}
            tag = ("  pair_wait MATCH" if wait == recorded_wait
                   else f"  pair_wait DIFFERS (record {len(recorded_wait)}, "
                        f"here {len(wait)})")
        print(f"\n    {name}{tag}")
        for line in _render(sched):
            print(line)
        if name != "record":
            waited = {s.run.label for s in sched.deferred
                      if s.reason == "pair_wait"}
            lost = sorted((recorded_open - opened) & waited)
            print(f"        -> {len(lost)} run(s) the record opened and this "
                  f"defers pair_wait: {', '.join(lost) if lost else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
