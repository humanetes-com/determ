#!/usr/bin/env python3
"""Hourly deep-analysis of the live run — the CODED version of the manual
investigation that found the poisoned-notes / attribution-futility plateau
(2026-07-10). Run by pipeline_watcher.sh every interval and on demand:

    .venv/bin/python tools/run_health.py [--log logs/pipeline_anyhow.log]
                                         [--project tmp/anyhow] [--brief]

Prints the operator block (phase, trend, findings, decision distance).
Exit code 0 always — it reports, the operator decides.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from log_files import read_all  # noqa: E402
from pass_delta import load_measurements, report  # noqa: E402
from run_trend import workspace_created  # noqa: E402

from pipeline_core.domain import model_notes as _mn  # noqa: E402
from pipeline_core.domain.run_health import analyze, parse_iter_readings  # noqa: E402
from pipeline_core.domain.run_history import (  # noqa: E402
    analyze_history,
    parse_run_summaries,
)


def segment_lines(log: Path) -> list[str]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    start = 0
    for i, ln in enumerate(lines):
        if "PIPELINE STARTED" in ln:
            start = i
    return lines[start:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/pipeline_anyhow.log")
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--brief", action="store_true",
                    help="findings only (for the watcher's event stream)")
    ap.add_argument("--events", default="logs/events.jsonl")
    args = ap.parse_args()

    seg = segment_lines(Path(args.log))
    notes: list[str] = []
    for fr in sorted(Path(args.project).glob("functional_requirements_*.md")):
        try:
            _, section = _mn.slice_model_notes(fr.read_text(encoding="utf-8"))
            notes.extend(_mn.parse_note_bullets(section))
        except Exception:
            pass

    # Cross-RUN findings run FIRST and are merged into the same list: every
    # detector in `analyze` is scoped to one segment, so a plateau spanning
    # several runs is unaskable there — which is how five consecutive runs each
    # read as healthy while the score had not moved in 46 hours. This is the
    # one step every operator pass already performs, so the cross-run question
    # gets asked whether or not anyone thinks to ask it.
    history = parse_run_summaries(read_all(Path(args.log)))
    findings = analyze_history(
        history, workspace_created=workspace_created(Path(args.project)),
    ) + analyze(seg, notes)
    readings = parse_iter_readings(seg)

    if not args.brief:
        print(f"run-health — segment lines={len(seg)} notes={len(notes)} "
              f"iter readings={len(readings)} runs in history={len(history)}")
    # Cross-RUN pass-count comparability: `analyze` only ever sees one segment,
    # so a denominator that moved across a restart cannot be caught there — and
    # an incomparable delta read as progress is how a plateau gets declared over.
    product = Path(args.project).name.split("-")[0]
    for line in report(load_measurements(Path(args.events), product, None), 3):
        print(f"PASS-DELTA {line}")
    if not findings:
        print("HEALTH: no findings")
        return 0
    for f in findings:
        prefix = {"info": "·", "warn": "⚠", "alert": "‼"}[f.severity]
        print(f"HEALTH {prefix} [{f.kind}] {f.subject} — {f.evidence}")
        if not args.brief:
            print(f"    → {f.action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
