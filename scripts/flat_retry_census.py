#!/usr/bin/env python3
"""WHY. Two writers answer "is this a new question for this unit" and they
disagree. The ask ledger (`candidate_tiers.AskLedger`, read by `due_reading`)
re-owes a unit to a red test when the test's own failure text moved
(`ask_fingerprint`). The flat exclusion (`body_ledger.flat_standing_unchanged`,
read by the fill schedule as `standing_flat` and by `drop_standing_flat`)
keeps a unit whose kept body was graded FLAT out of the next fill while the
unit's own evidence has not moved: same oracle, no new convicting test, same
`evidence_fp` (the file the prompt quotes, the retrospective rows, the crash
excerpt). A test whose printed value moved because a body in ANOTHER file
landed is a new ask for the ledger and an unchanged question for the flat
exclusion, so the schedule defers the run the ledger says is owed (review of
the restated close plan, 2026-11-04: 37 owed units whose only rows were
`rebuy_flat`). Which premise is right is a measured question: after a FLAT
grade, does the unit's next try pay more when the tests' text moved, when
the unit's own evidence moved, both, or neither.

USAGE.  python scripts/flat_retry_census.py [--ledger
        logs/body_ledger/anyhow/index.jsonl] [--events logs/events.jsonl]
        [--cluster stable]
Read-only, no GPU, safe while a pipeline runs. Joins three records:
  * body-ledger FLAT rows (unit, run, iteration, oracle, convicting tests,
    evidence_fp), and for each one the unit's NEXT landing under the same
    oracle (any later process) with that landing's grade (the next non-landed
    row of the same run, unit and body sha);
  * `phase_c.tdd.owed`, for the failure-text fingerprint of each convicting
    test at the FLAT row's sweep and at the next landing's sweep (a
    convicting test missing from either reading is not compared);
  * the next landing's own `evidence_fp`, against the FLAT row's.
Only FLAT rows whose oracle has owed readings (2026-10-09 on) are counted.

HOW TO INTERPRET THE OUTPUT. One line per cell, then the baseline:
  text=moved|same    a convicting test's failure fingerprint differed
                     between the two sweeps / none did (`?` when no
                     convicting test was in both owed readings);
  evidence=moved|same the next landing's evidence_fp differs from the FLAT
                     row's (`?` when either is empty);
  n                  FLAT rows in the cell that had a next landing;
  proven             the next landing was graded proven (the keep turned its
                     tests: the try paid);
  advanced           graded advanced (the failure moved past the unit);
  flat / other       graded flat again / refuted, unmeasured or ungraded.
  baseline           first landings of units with no earlier landing under
                     the same oracle, same grading: what a fresh try pays.
Then the same grading per landing, by the schedule reason of the run that
kept it (`phase_c.route.run` reason). `rebuy_readmitted` is a re-buy the
schedule deferred `readmit_after` times and then ran: the only tries in the
record that the flat exclusion had held back. If its proven% is near
`never_served`, the deferral delayed a try that pays like a fresh one.
THE DECISION RULE. Compare proven% of text=moved,evidence=same against the
baseline and against text=same,evidence=same.
  * If text=moved,evidence=same pays near the baseline and clearly above
    text=same,evidence=same, the ledger's premise is right: the test's
    failure text belongs in the flat exclusion's evidence fingerprint.
  * If it pays near text=same,evidence=same (near zero), the flat premise is
    right: the ledger should not re-owe a unit whose standing flat body's
    own evidence is unchanged.
  * If no cell pays, both writers are buying re-tries that do not pay, and
    that is the finding. A cell with n under ~10 decides nothing.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

OWED = '"phase_c.tdd.owed"'


def _owed_fps(events: Path) -> dict[tuple[str, int], dict[str, str]]:
    out: dict[tuple[str, int], dict[str, str]] = {}
    with events.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if OWED not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = ev.get("data") or {}
            out[(ev.get("run_id", ""), int(d.get("iter", 0)))] = {
                t: r.get("fingerprint") or ""
                for t, r in (d.get("tests") or {}).items()
                if not r.get("held_by")}
    return out


def _bare(t: str) -> str:
    return t.rsplit("::", 1)[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", type=Path,
                    default=Path("logs/body_ledger/anyhow/index.jsonl"))
    ap.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    ap.add_argument("--cluster", default="stable")
    args = ap.parse_args()

    fps = _owed_fps(args.events)
    rows: list[dict] = []
    with args.ledger.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("cluster") == args.cluster and r.get("run_id"):
                rows.append(r)

    # Grade of each landing: the next non-landed row of the same run, unit
    # and body sha (the ledger's own rule, `unit_evidence_from`).
    grade: dict[int, str] = {}
    open_: dict[tuple[str, str, str, str], int] = {}
    for i, r in enumerate(rows):
        k = (r["run_id"], r["file"], r["fn"], r["body_sha"])
        if r["state"] == "landed":
            open_[k] = i
        elif k in open_:
            grade[open_[k]] = r["state"]

    landings: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r["state"] == "landed":
            landings[(r["file"], r["fn"], r.get("oracle_fp", ""))].append(i)

    def sweep_key(r: dict) -> tuple[str, int]:
        return (r["run_id"], int(r.get("iteration", 0)))

    cells: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for i, r in enumerate(rows):
        if r["state"] != "flat" or not r.get("oracle_fp"):
            continue
        f0 = fps.get(sweep_key(r))
        if f0 is None:
            continue
        nxt = next((j for j in landings[(r["file"], r["fn"], r["oracle_fp"])]
                    if j > i), None)
        if nxt is None:
            continue
        n = rows[nxt]
        f1 = fps.get(sweep_key(n))
        conv = {_bare(t) for t in r.get("convicting") or ()}
        b0 = {_bare(t): v for t, v in f0.items()}
        b1 = {_bare(t): v for t, v in (f1 or {}).items()}
        both = [t for t in conv if t in b0 and t in b1]
        text = ("?" if not both else
                "moved" if any(b0[t] != b1[t] for t in both) else "same")
        e0, e1 = r.get("evidence_fp") or "", n.get("evidence_fp") or ""
        evid = "?" if not (e0 and e1) else ("moved" if e0 != e1 else "same")
        g = grade.get(nxt, "ungraded")
        cell = cells[(text, evid)]
        cell["n"] += 1
        cell[g if g in ("proven", "advanced", "flat") else "other"] += 1

    base: Counter[str] = Counter()
    seen: set[tuple[str, str, str]] = set()
    for i, r in enumerate(rows):
        if r["state"] != "landed" or not r.get("oracle_fp"):
            continue
        k = (r["file"], r["fn"], r["oracle_fp"])
        if k in seen:
            continue
        seen.add(k)
        if sweep_key(r) not in fps:
            continue
        g = grade.get(i, "ungraded")
        base["n"] += 1
        base[g if g in ("proven", "advanced", "flat") else "other"] += 1

    def _fmt(c: Counter[str]) -> str:
        n = c["n"] or 1
        return (f"n={c['n']:4d} proven={c['proven']:3d} "
                f"({100 * c['proven'] / n:4.1f}%) advanced={c['advanced']:3d} "
                f"flat={c['flat']:3d} other={c['other']:3d}")

    # Landings graded by the schedule reason of the run that kept them: a
    # re-buy the schedule deferred and then re-admitted is the one place the
    # record holds a try the flat exclusion had held back.
    reason_of: dict[tuple[str, int, str], str] = {}
    with args.events.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.route.run"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = ev.get("data") or {}
            if d.get("cluster", args.cluster) != args.cluster:
                continue
            for f_, n_ in d.get("kept") or ():
                reason_of[(ev.get("run_id", ""), int(d.get("iteration", 0)),
                           f"{f_}::{n_}")] = str(d.get("reason") or "?")
    by_reason: dict[str, Counter[str]] = defaultdict(Counter)
    for i, r in enumerate(rows):
        if r["state"] != "landed":
            continue
        rs = reason_of.get((r["run_id"], int(r.get("iteration", 0)),
                            f"{r['file']}::{r['fn']}"))
        if rs is None:
            continue
        g = grade.get(i, "ungraded")
        by_reason[rs]["n"] += 1
        by_reason[rs][g if g in ("proven", "advanced", "flat") else "other"] += 1

    print("landings graded, by the schedule reason of the run that kept them:")
    for rs, c in sorted(by_reason.items(), key=lambda x: -x[1]["n"]):
        print(f"  {rs:18s} {_fmt(c)}")
    print("next try after a FLAT grade, same oracle:")
    for (text, evid) in sorted(cells):
        print(f"  text={text:5s} evidence={evid:5s} {_fmt(cells[(text, evid)])}")
    print(f"  baseline (first landing under the oracle)  {_fmt(base)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
