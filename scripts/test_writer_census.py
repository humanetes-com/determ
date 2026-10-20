#!/usr/bin/env python3
"""Per-writer census of the oracle's test writers: replies, replies that did
not build, recovered by that writer's own repair, admitted, and admitted in
more than one build world.

WHY. The plan for one shared test writer (2026-10-20) is sized by what each
writer's repair and world handling actually buys. The writers that add a test
file to the oracle are four: task 0c's augmentation (`task0c_augment_module`,
repair = the inline fix ask in `_finalize_and_commit`), task 0d's behaviour
emitter (`emit_behaviour_tests`, rule and reach probes with no repair), task
0d's doc-example emitter (`emit_doc_example_tests`, no model), and task 4's
pinning pass (`run_pinning_pass`, repair = `repair_of` inside
`build_pinning_test_prompt`). Before this script the comparison was a
prediction ("t0c recovers most, the others few or none; only t0c spans
worlds"); this counts it from the record.

USAGE.
    python scripts/test_writer_census.py [--logs logs] [RUN_ID ...]

Reads `logs/pipeline_anyhow.log*` (rotated segments, oldest first; a run is
the span from its `PIPELINE STARTED … run_id=X` line to the next one) and
`logs/events.jsonl`. With no RUN_ID it reports every run in the logs that
did any test writing. No GPU, no cargo, no model; seconds.

HOW TO INTERPRET THE OUTPUT. One block per run, one line per writer that
wrote anything in it:
  t0c       blocks=N needed_repair=R (compile=C own_fail=F) recovered=K
            (built after the fix ask) unrecovered=U; appended=A
            no_coverage_revert=V admitted~=A-V; settled worlds: S blocks
            settled, M of them kept in more than one world.
  t0d.probe proposed=P dropped_not_building=D (no repair exists, so D is
            the loss a shared repair could recover); emitted captures per
            cluster, and withdrawn.
  t0d.doc   examples=E kept=K (settled; no model, so no repair question).
  t4.pin    attempts=N did_not_build=B (refused original_did_not_run on
            build) admitted=A, admitted at attempt>1 after a non-building
            attempt (recovered by repair_of)=R; worlds of the admitted
            files (a pin is gated to the world that wrote it and never
            settled, so >1 is expected to be 0).
Decision rule: a repair is worth sharing in proportion to the
did-not-build count of the writers that lack it times the recovery rate of
the writer that has it; a world settle is worth sharing when a writer that
does not settle admits files whose content would pass in another world
(not measurable here: the census shows only where settling runs at all).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

_START = re.compile(r"PIPELINE STARTED .*run_id=(\S+)")
_FIX = re.compile(r"\] (Compile error|Failing own test\(s\)) in (\S+) "
                  r"\(fix attempt (\d+)\) — sending to LLM")
_APPENDED = re.compile(r"\] Appended gen_tests_\d+ to (\S+) ")
_NOCOV = re.compile(r"\] (\S+) — block produced no coverage increase.*reverting")
_REVERT = re.compile(
    r"\] (?:LLM returned no fix — reverting (\S+?)\.?$"
    r"|fix would silence rather than correct .* — reverting (\S+?)\.?$"
    r"|No error improvement in (\S+) — reverting"
    r"|Error floor stalled in (\S+) at "
    r"|Unclosed delimiter — reverting (\S+?)\.?$"
    r"|\S+: gen_tests_\d+ in (\S+) refused by contract twice)")
_PROBE_DROP = re.compile(r"\[task0d\] cluster (\S+): probe for \S+ dropped \(failed")
_RULE_PROBES = re.compile(r"\[task0d\] cluster (\S+): rule-probes: \d+ rule\(s\) asked, "
                          r"(\d+) program\(s\) proposed, (\d+) captured")
_REACH = re.compile(r"\[task0d\] cluster (\S+): reach probes: (\d+) function\(s\)")
_EMITTED = re.compile(r"\[task0d\] cluster (\S+): (\d+) captured behaviour\(s\) emitted")
_WITHDRAWN = re.compile(r"\[task0d\] cluster (\S+): .*?(\d+) capture\(s\) withdrawn")


def _segments(log_dir: Path) -> dict[str, list[str]]:
    rotated = sorted(log_dir.glob("pipeline_anyhow.log.*"),
                     key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0,
                     reverse=True)
    files = [*rotated, log_dir / "pipeline_anyhow.log"]
    out: dict[str, list[str]] = defaultdict(list)
    run = None
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(errors="replace").splitlines():
            m = _START.search(line)
            if m:
                run = m.group(1)
            if run:
                out[run].append(line)
    return out


def _t0c(lines: list[str]) -> dict[str, int] | None:
    c: Counter[str] = Counter()
    open_: dict[str, str] = {}
    repaired_appended: set[str] = set()
    for line in lines:
        if m := _FIX.search(line):
            kind, path, n = m.group(1), m.group(2), int(m.group(3))
            if n == 1 and path not in open_:
                c["needed_repair"] += 1
                c["compile" if kind.startswith("Compile") else "own_fail"] += 1
                open_[path] = kind
            continue
        if m := _APPENDED.search(line):
            path = m.group(1)
            c["appended"] += 1
            if open_.pop(path, None) is not None:
                c["recovered"] += 1
                repaired_appended.add(path)
            continue
        if m := _NOCOV.search(line):
            c["no_coverage_revert"] += 1
            repaired_appended.discard(m.group(1))
            continue
        if m := _REVERT.search(line):
            path = next(g for g in m.groups() if g)
            c["reverted"] += 1
            if open_.pop(path, None) is not None:
                c["unrecovered"] += 1
    if not (c["appended"] or c["needed_repair"]):
        return None
    c["blocks"] = c["appended"] + c["reverted"]
    return dict(c)


def _t0d(lines: list[str]) -> dict[str, object] | None:
    proposed: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    emitted: Counter[str] = Counter()
    withdrawn: Counter[str] = Counter()
    for line in lines:
        if m := _PROBE_DROP.search(line):
            dropped[m.group(1)] += 1
        elif m := _RULE_PROBES.search(line):
            proposed[m.group(1)] += int(m.group(2))
        elif m := _REACH.search(line):
            proposed[m.group(1)] += int(m.group(2))
        elif m := _EMITTED.search(line):
            emitted[m.group(1)] += int(m.group(2))
        elif m := _WITHDRAWN.search(line):
            withdrawn[m.group(1)] += int(m.group(2))
    if not (proposed or dropped or emitted):
        return None
    return {"proposed": dict(proposed), "dropped_not_building": dict(dropped),
            "emitted": dict(emitted), "withdrawn": dict(withdrawn)}


def _events(path: Path) -> dict[str, list[dict[str, object]]]:
    keep = ("task0c.test_worlds", "task4.pin_admitted", "task4.pin_refused",
            "task0d.doc_examples", "task0d.behaviour_tests_emitted")
    out: dict[str, list[dict[str, object]]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(errors="replace") as fh:
        for line in fh:
            if not any(k in line for k in keep):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") in keep:
                out[str(e.get("run_id"))].append(e)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="logs")
    ap.add_argument("runs", nargs="*")
    a = ap.parse_args(argv)
    log_dir = Path(a.logs)
    segs = _segments(log_dir)
    evs = _events(log_dir / "events.jsonl")
    runs = a.runs or [r for r in segs if r in evs or _t0c(segs[r]) or _t0d(segs[r])]
    for run in runs:
        lines = segs.get(run, [])
        ev = evs.get(run, [])
        rows: list[str] = []
        if t0c := _t0c(lines):
            settled = [e for e in ev if e["event"] == "task0c.test_worlds"]
            multi = sum(1 for e in settled
                        if len((e.get("data") or {}).get("worlds") or []) > 1)
            rows.append(
                f"  t0c       blocks={t0c.get('blocks', 0)} needed_repair="
                f"{t0c.get('needed_repair', 0)} (compile={t0c.get('compile', 0)} "
                f"own_fail={t0c.get('own_fail', 0)}) recovered={t0c.get('recovered', 0)} "
                f"unrecovered={t0c.get('unrecovered', 0)}; appended={t0c.get('appended', 0)} "
                f"no_coverage_revert={t0c.get('no_coverage_revert', 0)} admitted~="
                f"{t0c.get('appended', 0) - t0c.get('no_coverage_revert', 0)}; settled "
                f"worlds: {len(settled)} blocks settled, {multi} kept in >1 world")
        if t0d := _t0d(lines):
            rows.append(f"  t0d.probe {json.dumps(t0d)}")
        for e in ev:
            if e["event"] == "task0d.doc_examples":
                d = e.get("data") or {}
                rows.append(f"  t0d.doc   examples={d.get('examples')} "
                            f"kept={len(d.get('kept') or [])}")
        pins = [e for e in ev if e["event"].startswith("task4.pin")]
        if pins:
            att = len(pins)
            nobuild = [e for e in pins if e["event"] == "task4.pin_refused"
                       and str((e.get("data") or {}).get("reason")) == "original_did_not_run"]
            adm = [e for e in pins if e["event"] == "task4.pin_admitted"]
            sites_nobuild = {((e.get("data") or {}).get("site"),
                              (e.get("data") or {}).get("world")) for e in nobuild}
            rec = sum(1 for e in adm
                      if int((e.get("data") or {}).get("attempt") or 1) > 1
                      and ((e.get("data") or {}).get("site"),
                           (e.get("data") or {}).get("world")) in sites_nobuild)
            worlds = Counter(str((e.get("data") or {}).get("world")) for e in adm)
            rows.append(f"  t4.pin    attempts={att} did_not_build={len(nobuild)} "
                        f"admitted={len(adm)} recovered_by_repair={rec} "
                        f"admitted_worlds={dict(worlds)} (one world each, unsettled)")
        if rows:
            print(run)
            print("\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
