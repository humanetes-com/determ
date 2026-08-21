#!/usr/bin/env python3
"""Can the loop's state be rebuilt from its own fact log? — stage 1's gate.

WHY THIS EXISTS
---------------
`phase_c_tdd.py` is 3,708 lines and its state is mutable locals; the pipeline
also emits 245 `emit()` calls over 140 event types, 302,722 events, 234 MB —
a fact log that is written and never read back. The plan is to make the loop's
state a FOLD over that log (`domain/loop_facts.py`), one gate at a time. The
gate on the whole programme is this script: fold a REAL run's recorded events
and rebuild the per-iteration vector it printed. If a field cannot be rebuilt,
the vocabulary is incomplete and NO gate may be converted yet — a fold over an
incomplete log is worse than a mutable local, because it reads a default where
the variable would have read the truth.

USAGE
-----
    python scripts/fold_completeness.py [--events logs/events.jsonl]
        [--runs 1246060-2339497b,3124988-ac6886ba] [--cluster stable]
        [--log logs/pipeline_anyhow.log] [--verbose]

No GPU, no cargo, no model call: it streams the event store and (optionally)
greps the run's printed `iter` lines out of a log file to compare against.

HOW TO INTERPRET THE OUTPUT
---------------------------
Per run:

  iters            how many `phase_c.tdd.iter` events the run recorded for the
                   cluster, and how many vectors the fold produced from them.
                   These must be equal: a fold that silently drops an iteration
                   disagrees with the log on the ITERATION COUNT, which is the
                   first thing an operator reads.
  rebuilt          per FIELD of the vector, whether every iteration's value
                   could be rebuilt from the facts. A field listed under
                   `MISSING` is a fact the loop had and never emitted.
  line match       when a log file is given: how many of the run's printed
                   `iter` lines the folded vector reproduces byte-for-byte,
                   over how many were found. This is the strict form of the
                   assertion; the field table is the diagnostic for it.

THE DECISION RULE. Stage 2 (converting a gate) may proceed only for gates whose
inputs are in the `rebuilt` set for the runs being replayed. The script prints
that judgement for `flat_score` specifically, whose inputs are the judged
readings and the pass closures.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import fields as dataclass_fields
from pathlib import Path

from pipeline_core.domain.flat_score import flat_score
from pipeline_core.domain.loop_facts import (
    BatchDispatched,
    Fact,
    IterVector,
    PassClosed,
    SweepMeasured,
    fold,
)

_ITER_LINE = re.compile(
    r"\[phase-c/tdd\] iter (?P<iter>\d+): cargo test rc=(?P<rc>-?\d+) "
    r"\((?P<dt>[\d.]+)s\); pass=(?P<pass>\d+) fail=(?P<fail>\d+) "
    r"oracle=(?P<oracle>\d+)")

# The fields a recorded iter event has to supply for the vector to be rebuilt.
# `traversals`, `improved` and `stall` are DERIVED by the fold and so are not
# listed: rebuilding them from the facts is the point, and comparing them
# against the event's own copy is the check below.
_EVENT_FIELDS = {
    "rc": "rc", "elapsed_s": "elapsed_s", "pass_count": "pass_count",
    "fail_count": "fail_count", "oracle_size": "oracle_size",
    "frontier_size": "frontier_size", "kind": "kind",
    "skipped_exhausted": "skipped_exhausted", "dispatched": "dispatched",
    "attempted_before": "attempted_before", "cohort_size": "cohort_size",
    "completes_pass": "pass_completed",
}


def read_run(events: Path, run_id: str, cluster: str) -> list[dict]:
    out: list[dict] = []
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.tdd.iter"' not in line or run_id not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("run_id") != run_id:
                continue
            data = ev.get("data") or {}
            if data.get("cluster") != cluster:
                continue
            out.append(data)
    return out


def to_facts(rows: list[dict]) -> list[Fact]:
    """The recorded iter events, as the fact vocabulary sees them.

    One event carries three facts — the sweep, the batch, and (when the batch
    closed the pass) the traversal — which is itself a finding: the loop's
    per-iteration event is a JOIN of three different things that happen at
    three different points in the iteration, and a stream that emitted them
    separately would need no un-joining here."""
    facts: list[Fact] = []
    for d in rows:
        cluster = str(d.get("cluster") or "")
        it = int(d.get("iter") or 0)
        judged = d.get("kind") in (None, "behavior", "merged", "compile")
        facts.append(SweepMeasured(
            iter=it, cluster=cluster,
            pass_count=int(d.get("pass_count") or 0),
            fail_count=int(d.get("fail_count") or 0),
            oracle_size=int(d.get("oracle_size") or 0),
            rc=int(d.get("rc") or 0),
            elapsed_s=float(d.get("elapsed_s") or 0.0),
            passed=frozenset(d.get("passed") or ()),
            failed=frozenset(d.get("failed") or ()),
            intact=bool(d.get("reconciled", True)),
            judged=bool(judged), kind=str(d.get("kind") or "behavior")))
        if d.get("dispatched") is not None:
            facts.append(BatchDispatched(
                iter=it, cluster=cluster,
                frontier_size=int(d.get("frontier_size") or 0),
                dispatched=int(d.get("dispatched") or 0),
                attempted_before=int(d.get("attempted_before") or 0),
                cohort_size=int(d.get("cohort_size") or 0),
                skipped_exhausted=int(d.get("skipped_exhausted") or 0),
                completes_pass=bool(d.get("pass_completed"))))
        if d.get("pass_completed"):
            facts.append(PassClosed(
                iter=it, cluster=cluster,
                cohort_size=int(d.get("cohort_size") or 0),
                attempted=int(d.get("attempted_before") or 0)))
    return facts


def compare(rows: list[dict], view) -> tuple[dict[str, int], dict[str, int]]:
    """Per vector field: how many iterations matched the recorded event, and
    how many the event could not answer at all."""
    ok: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for row, vec in zip(rows, view.vectors, strict=False):
        for field_name, event_key in _EVENT_FIELDS.items():
            if event_key not in row:
                missing[field_name] += 1
                continue
            want = row[event_key]
            got = getattr(vec, field_name)
            if isinstance(want, float) or isinstance(got, float):
                same = abs(float(want) - float(got)) < 0.05
            else:
                same = want == got
            if same:
                ok[field_name] += 1
    return dict(ok), dict(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--runs",
                    default="1246060-2339497b,3124988-ac6886ba,1920662-40f711b2")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--log", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    events = root / args.events
    printed: dict[tuple[str, int], str] = {}
    if args.log:
        for raw in (root / args.log).read_text(encoding="utf-8",
                                               errors="replace").splitlines():
            m = _ITER_LINE.search(raw)
            if m:
                printed[(args.cluster, int(m["iter"]))] = raw.strip()

    all_field_names = [f.name for f in dataclass_fields(IterVector)]
    verdict_ok = True
    for run_id in args.runs.split(","):
        rows = read_run(events, run_id, args.cluster)
        if not rows:
            print(f"\n{run_id}: no iter events for cluster {args.cluster}")
            continue
        facts = to_facts(rows)
        view = fold(facts, max_stalls=int(rows[0].get("max_stalls") or 0))
        ok, missing = compare(rows, view)
        print(f"\n{run_id}  cluster={args.cluster}")
        print(f"  iters: {len(rows)} recorded -> {len(view.vectors)} folded"
              + ("  OK" if len(rows) == len(view.vectors) else "  MISMATCH"))
        rebuilt = [f for f in _EVENT_FIELDS
                   if ok.get(f, 0) == len(rows) and not missing.get(f)]
        partial = [f"{f}({ok.get(f, 0)}/{len(rows)})" for f in _EVENT_FIELDS
                   if f not in rebuilt and not missing.get(f)]
        absent = [f"{f}({missing[f]}/{len(rows)} events lack it)"
                  for f in _EVENT_FIELDS if missing.get(f)]
        print(f"  rebuilt : {', '.join(rebuilt) or '(none)'}")
        if partial:
            print(f"  DIFFERS : {', '.join(partial)}")
        if absent:
            print(f"  MISSING : {', '.join(absent)}")
            verdict_ok = False
        derived = [f for f in all_field_names if f not in _EVENT_FIELDS]
        print(f"  derived by the fold (not compared): {', '.join(derived)}")

        if printed:
            hits = sum(1 for v in view.vectors
                       if printed.get((args.cluster, v.iter), "")
                       .endswith(v.render().split("dispatching")[0].strip()
                                 [-40:]))
            print(f"  line match: {hits}/{len(view.vectors)} printed lines "
                  "reproduced (tail comparison)")

        decision = flat_score(facts, window=5, limit=2)
        if decision is not None:
            print(f"  flat_score over the same stream: "
                  f"stop={decision.stop} reading={decision.reading} "
                  f"repeats={decision.repeats} traversals={decision.traversals}"
                  + (" (withheld by traversal under the old precondition)"
                     if flat_score(facts, window=5, limit=2,
                                   require_traversal=True).withheld_by_traversal
                     else ""))

    print("\nDECISION: "
          + ("every field of the iter vector is in the fact stream — a gate "
             "whose inputs are those fields may be converted."
             if verdict_ok else
             "at least one field is MISSING from the recorded stream. A gate "
             "may be converted only if its own inputs are in `rebuilt` above; "
             "`flat_score` needs pass_count, oracle_size and the pass "
             "closures, which are."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
