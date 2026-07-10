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

from pipeline_core.domain import model_notes as _mn  # noqa: E402
from pipeline_core.domain.run_health import analyze, parse_iter_readings  # noqa: E402


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
    args = ap.parse_args()

    seg = segment_lines(Path(args.log))
    notes: list[str] = []
    for fr in sorted(Path(args.project).glob("functional_requirements_*.md")):
        try:
            _, section = _mn.slice_model_notes(fr.read_text(encoding="utf-8"))
            notes.extend(_mn.parse_note_bullets(section))
        except Exception:
            pass

    findings = analyze(seg, notes)
    readings = parse_iter_readings(seg)

    if not args.brief:
        print(f"run-health — segment lines={len(seg)} notes={len(notes)} "
              f"iter readings={len(readings)}")
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
