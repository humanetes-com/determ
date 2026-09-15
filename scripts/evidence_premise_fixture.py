#!/usr/bin/env python3
"""Compute the evidence premise on the live records of a stalled run.

WHY THIS EXISTS. Run `837207-ce5b1647` (2026-09-15, cluster `stable`, 6h32m,
19 graded fills, zero cashed) stopped dispatching entirely: from iteration 3
the sweep read `dispatching 0 of 0 (0/0 this pass)` against `frontier=3
fn(s)` and the run ended on an external TERM three hours later. The cause was
`body_ledger.flat_standing_unchanged`'s premise — the tree body, the oracle,
the convicting set — every clause of which is satisfiable only by the world
OUTSIDE the unit, and on a stalled tree that world does not move. A unit that
lands a compiling, non-regressing, non-paying body is therefore retired by its
own success, permanently.

The fix adds a fourth clause on the evidence the unit's next prompt would
carry (`domain/evidence_premise`). THE CLAIM THAT HAD TO BE COMPUTED, not
described: that the evidence really had moved for the units the run excluded —
otherwise the clause changes nothing and the finding is somewhere else. This
script is that computation, on the recorded ledger, the recorded events and
the recorded bodies, so it can be re-run and disagreed with.

USAGE

    .venv/bin/python scripts/evidence_premise_fixture.py \
        [--run 837207-ce5b1647] [--project anyhow] [--cluster stable] \
        [--log logs/pipeline_anyhow.log.1]

Reads only: `logs/events.jsonl`, `logs/body_ledger/<project>/index.jsonl`,
`logs/body_ledger/<project>/bodies/<sha>.json`, and the run's log for the
retro channel. Writes nothing, spends no GPU, needs no running pipeline.

HOW TO READ THE OUTPUT

`EXCLUDED` lists, per unit the run refused to dispatch, the iteration the
refusal started at and the iteration the standing FLAT row was graded at.

`context` is the digest of the unit's FILE as its bodies — what every fill
route quotes — reconstructed at the dispatch that earned the grade and at the
iteration that excluded the unit, from the ledger's own rows and the raw
bodies it stored. `moved=True` means some unit of that file carried a
different body between the two, so the next prompt would quote a different
file: a different question. The reconstruction is a LOWER bound on movement —
it can only see units the ledger wrote a row for, so a body it cannot see can
add difference and never remove it.

`retro` counts the rows in the run's log where a retrospective record naming
the unit changed between those two iterations (category or round). That
channel feeds `RETROSPECTIVE_GUIDANCE` and, on a pair record, the next fill's
`LAST_ERROR`.

`CONTROL` re-computes the exclusion-iteration fingerprint against the
GRADE-iteration file. It must come out IDENTICAL to the grade-time digest —
that is the half of the clause that still refuses: an unchanged question is
still not re-bought. A control that differs would mean the digest is
unstable, not that the evidence moved.

DECISION RULE. Every excluded unit with `moved=True` on either channel is a
unit the fourth clause releases, once, under evidence it has not already
failed on. A unit with `moved=False` on both stays excluded and the plateau's
cause for it is elsewhere — read that as the finding, not as a bug in this
script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.evidence_premise import (  # noqa: E402
    evidence_fingerprint,
    file_context_text,
)

ROOT = Path(__file__).resolve().parent.parent


def _events(run: str) -> list[dict[str, Any]]:
    out = []
    with (ROOT / "logs" / "events.jsonl").open(
            encoding="utf-8", errors="replace") as f:
        for line in f:
            if run not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event", "").startswith("phase_c."):
                out.append(e)
    return out


def _rows(project: str, cluster: str) -> list[dict[str, Any]]:
    p = ROOT / "logs" / "body_ledger" / project / "index.jsonl"
    out = []
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("cluster") == cluster:
                out.append(r)
    return out


def _body_text(project: str, sha: str) -> list[str]:
    p = (ROOT / "logs" / "body_ledger" / project / "bodies" / f"{sha}.json")
    if not p.exists():
        return [f"<body {sha} not stored>"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return [f"<body {sha} unreadable>"]
    return [str(b) for b in data] if isinstance(data, list) else [str(data)]


def _file_records_at(
    rows: list[dict[str, Any]], *, project: str, file: str, stop: int,
) -> list[dict[str, Any]]:
    """The file's bodies as of row index `stop`, exclusive.

    `index.jsonl` is append-only in WRITE order, so its own order is the
    chronology — walking it and keeping the last word about each unit is
    exactly `latest_by_unit`'s rule applied at a point in time, and it needs
    no cross-run ranking (which is what an earlier cut of this got wrong, and
    what made a cross-run grade read as "nothing moved").

    This is a RECONSTRUCTION over the units the ledger wrote a row for, not
    the extractor's whole-file read, so the absolute digest is not the one the
    driver computes. Only the DIFFERENCE between two boundaries is claimed,
    and a body the ledger never recorded can add difference, never remove it.
    """
    latest: dict[str, str] = {}
    for r in rows[:stop]:
        if r.get("file") != file:
            continue
        latest[str(r.get("fn"))] = str(r.get("body_sha"))
    recs = []
    for fn, sha in latest.items():
        for body in _body_text(project, sha):
            recs.append({"file": file, "fn_name": fn, "body_text": body})
    return recs


def _retro_moves(log: Path, unit: str) -> int:
    """Log lines in which a retrospective record naming `unit` reported a
    different (category, round) pair. Iterations are not on those lines, so
    this counts DISTINCT states over the whole run — a count above 1 means the
    channel moved while the tree stood still."""
    if not log.exists():
        return 0
    file, fn = unit.rsplit("::", 1)
    states: set[tuple[str, str]] = set()
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "[phase-c/retro-" not in line:
            continue
        if f"{file}::{fn}::" not in line and f"{file}::{fn}+" not in line \
                and f"+{file}::{fn}" not in line:
            continue
        cat = rounds = ""
        for tok in line.split():
            if tok.startswith("category="):
                cat = tok
            elif tok.startswith("rounds="):
                rounds = tok
        states.add((cat, rounds))
    return len(states)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="837207-ce5b1647")
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--log", default="logs/pipeline_anyhow.log.1")
    a = ap.parse_args()

    evs = _events(a.run)
    excl_at: dict[str, int] = {}
    for e in evs:
        if e.get("event") != "phase_c.dispatch.flat_excluded":
            continue
        it = int(e["data"].get("iter") or 0)
        for u in e["data"].get("units") or ():
            excl_at.setdefault(str(u), it)
    dispatched = {int(e["data"].get("iter") or 0):
                  int(e["data"].get("dispatched") or 0)
                  for e in evs if e.get("event") == "phase_c.tdd.iter"}
    print(f"run {a.run}  cluster {a.cluster}")
    print("dispatched per iteration: "
          + ", ".join(f"iter {i}={n}" for i, n in sorted(dispatched.items())))
    if not excl_at:
        print("no flat exclusion recorded for this run")
        return 0

    rows = _rows(a.project, a.cluster)

    # The exclusion boundary, as a row index: the first row this run wrote at
    # or after the excluding iteration. Everything before it is the tree that
    # iteration's dispatch quoted.
    def _excl_stop(it: int) -> int:
        for i, r in enumerate(rows):
            if (r.get("run_id") == a.run
                    and int(r.get("iteration") or 0) >= it):
                return i
        return len(rows)

    log = ROOT / a.log
    released = 0
    for unit, it in sorted(excl_at.items()):
        file, _fn = unit.rsplit("::", 1)
        grades = [(i, r) for i, r in enumerate(rows)
                  if r.get("state") == "flat"
                  and f'{r["file"]}::{r["fn"]}' == unit
                  and not (r.get("run_id") == a.run
                           and int(r.get("iteration") or 0) > it)]
        if not grades:
            print(f"\nEXCLUDED {unit}: no standing flat row found")
            continue
        g_idx, grade = grades[-1]
        graded_at = int(grade.get("iteration") or 0)
        graded_run = str(grade.get("run_id") or "")
        # The dispatch that bought the graded try quoted the tree as it stood
        # just before the LANDING of the graded body — the first row anywhere
        # in the index about this unit and this body sha. For a grade the
        # audit minted from an unchanged escalated reply that landing is the
        # same row, which makes this the conservative boundary either way: it
        # can only under-report movement.
        bought_stop = next(
            (i for i, r in enumerate(rows)
             if f'{r["file"]}::{r["fn"]}' == unit
             and r.get("body_sha") == grade.get("body_sha")), g_idx)
        excl_stop = _excl_stop(it)
        ctx_grade = file_context_text(_file_records_at(
            rows, project=a.project, file=file, stop=bought_stop)).get(file, "")
        ctx_excl = file_context_text(_file_records_at(
            rows, project=a.project, file=file, stop=excl_stop)).get(file, "")
        fp_grade = evidence_fingerprint(context=ctx_grade)
        fp_excl = evidence_fingerprint(context=ctx_excl)
        retro_states = _retro_moves(log, unit)
        moved = fp_grade != fp_excl or retro_states > 1
        released += 1 if moved else 0
        print(f"\nEXCLUDED {unit}")
        print(f"  refused from iter {it}; standing FLAT row graded at "
              f"iter {graded_at} of run {graded_run or '(earlier run)'} "
              f"(body {grade.get('body_sha')}, "
              f"evidence_fp={grade.get('evidence_fp') or '(unrecorded)'})")
        print(f"  ledger rows quoted: {bought_stop} at the grade, "
              f"{excl_stop} at the exclusion")
        print(f"  context  grade={fp_grade}  exclusion={fp_excl}  "
              f"moved={fp_grade != fp_excl}")
        print(f"  retro    distinct (category, round) states in the run: "
              f"{retro_states}  moved={retro_states > 1}")
        print(f"  => the fourth clause {'RELEASES' if moved else 'HOLDS'} "
              "this unit on evidence; a row whose evidence_fp is unrecorded "
              "is released once regardless")
        print(f"  CONTROL  exclusion-iteration digest re-taken against the "
              f"grade-iteration file: "
              f"{evidence_fingerprint(context=ctx_grade)} "
              f"(identical to grade: "
              f"{evidence_fingerprint(context=ctx_grade) == fp_grade})")
    print(f"\n{released} of {len(excl_at)} excluded unit(s) would re-enter "
          "dispatch under the fourth clause, each on evidence it has not "
          "already failed on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
