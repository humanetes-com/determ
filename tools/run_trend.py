#!/usr/bin/env python3
"""One row per RUN, across the whole log history — the read that makes a
multi-run plateau visible in a single glance.

    .venv/bin/python tools/run_trend.py [--log L] [--project tmp/anyhow]
                                        [--runs N]

Every per-run number the operator already reads (`pass=`, fills, reverts) is
scoped to one process. Five processes each reporting "pass=36, found a bug,
fixed it, restarted" is indistinguishable from progress until they are put in
one table:

    run              started              git      iters  pass          fills rev
    1857661-0842cfde 2026-07-28 01:11:35  d9d83e8      3  36 → 36 /122     18   4

The verdict lines below the table name the three things that misled every
single-segment pass: a score unchanged across runs, an oracle denominator that
moved (so the scores were never comparable), and a workspace reset (so earlier
scores describe a tree that no longer exists).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from log_files import read_all  # noqa: E402

from pipeline_core.domain.run_history import (  # noqa: E402
    RunSummary,
    analyze_history,
    creation_time_from_reflog,
    parse_run_summaries,
)


def workspace_created(project: Path, branch: str = "pipeline") -> str:
    """When the workspace's pipeline branch was CREATED, as
    `YYYY-MM-DD HH:MM:SS` **in UTC**. Empty when the reflog has no creation
    entry — which is a different fact from "never reset" and must stay
    distinguishable.

    This is the only durable record that a `--clean` wiped the reconstruction:
    the commits it discarded are gone, but the branch-creation entry survives.

    UTC is not cosmetic. The only consumer compares this against the run-log
    headers, which are stamped UTC, by plain string prefix — so a reading in the
    machine's local zone silently shifts the boundary by the UTC offset and
    reports every run started inside that window as predating a workspace it
    actually postdates. `--date=format:` renders LOCAL time; only
    `format-local:` honours TZ."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project), "reflog", "show", branch,
             "--date=format-local:%Y-%m-%d %H:%M:%S"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "TZ": "UTC"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return creation_time_from_reflog(out)


def _score_cell(r: RunSummary) -> str:
    if r.first is None or r.last is None:
        return "— (no scored iter)"
    oracle = f" /{r.last.oracle}" if r.last.oracle is not None else ""
    return f"{r.first.pass_count} → {r.last.pass_count}{oracle}"


def render(runs: list[RunSummary]) -> list[str]:
    head = (f"{'run':<17} {'started':<20} {'git':<8} {'iters':>5} "
            f"{'pass':<16} {'fills':>5} {'rev':>4}")
    rows = [head, "-" * len(head)]
    for r in runs:
        rows.append(
            f"{(r.run_id or '?')[:17]:<17} {(r.started or '?')[:19]:<20} "
            f"{(r.git_sha or '?')[:8]:<8} {r.iterations:>5} "
            f"{_score_cell(r):<16} {r.fills_landed:>5} {r.reverts:>4}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/pipeline_anyhow.log")
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--runs", type=int, default=0, help="last N runs (0 = all)")
    ap.add_argument("--min-runs", type=int, default=2,
                    help="plateau alert threshold, in runs")
    args = ap.parse_args()

    runs = parse_run_summaries(read_all(Path(args.log)))
    if not runs:
        print(f"run_trend: no PIPELINE STARTED header in {args.log}(+archives)")
        return 0
    if args.runs > 0:
        runs = runs[-args.runs:]

    for line in render(runs):
        print(line)

    created = workspace_created(Path(args.project))
    findings = analyze_history(runs, workspace_created=created,
                               min_runs=args.min_runs)
    print()
    if not findings:
        print(f"TREND: no cross-run finding across {len(runs)} run(s)")
        return 0
    for f in findings:
        prefix = {"info": "·", "warn": "⚠", "alert": "‼"}[f.severity]
        print(f"TREND {prefix} [{f.kind}] {f.subject} — {f.evidence}")
        print(f"    → {f.action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
