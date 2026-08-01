#!/usr/bin/env python3
"""Rule on pass-count claims ACROSS runs, using the F6 provenance already stamped
on every `phase_c.tdd.iter` event.

    .venv/bin/python tools/pass_delta.py [--events logs/events.jsonl]
                                         [--product anyhow] [--cluster stable]
                                         [--last N]

A rising pass count means nothing unless the DENOMINATOR held: `pass=14
oracle=69` → `pass=35 oracle=111` on a tree whose content hash never changed is
a bigger suite, not progress. `run_health.py` only ever sees one run segment, so
a denominator that moves across a restart was invisible — this reads the whole
event stream and prints a comparable/NOT-comparable verdict per transition.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.measurement import (  # noqa: E402
    Measurement,
    compare_measurements,
)


def _pairs(raw: object) -> frozenset[tuple[str, str]]:
    """`[[binary, test], …]` → a set of pairs. Pre-93.6 events have no such
    field and yield the empty set, which the report then treats as "this
    reading cannot be diffed by pair" rather than as "nothing passed"."""
    if not isinstance(raw, list):
        return frozenset()
    out: set[tuple[str, str]] = set()
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.add((str(item[0]), str(item[1])))
    return frozenset(out)


def load_measurements(events: Path, product: str | None,
                      cluster: str | None) -> list[tuple[str, Measurement]]:
    """(timestamp, Measurement) for every tdd-iteration event, in file order.
    Torn lines and pre-F6 events (no `oracle_size`) are skipped."""
    out: list[tuple[str, Measurement]] = []
    if not events.exists():
        return out
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"oracle_size"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") != "phase_c.tdd.iter":
                continue
            if product and rec.get("product") != product:
                continue
            data = rec.get("data") or {}
            if cluster and data.get("cluster") != cluster:
                continue
            try:
                out.append((rec.get("ts", ""), Measurement(
                    cluster=str(data.get("cluster", "")),
                    iteration=int(data.get("iter", 0)),
                    tree_sha=str(data.get("tree_sha", "")),
                    oracle_size=int(data["oracle_size"]),
                    pass_count=int(data["pass_count"]),
                    fail_count=int(data["fail_count"]),
                    passed=frozenset(data.get("passed") or ()),
                    failed=frozenset(data.get("failed") or ()),
                    passed_pairs=_pairs(data.get("passed_pairs")),
                    failed_pairs=_pairs(data.get("failed_pairs")),
                    incomplete=frozenset(data.get("incomplete") or ()),
                    oracle_static=int(data.get("oracle_static") or 0),
                )))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def report(rows: list[tuple[str, Measurement]], last: int) -> list[str]:
    """One verdict line per transition between consecutive DISTINCT readings.
    Repeats of an identical reading carry no information and are collapsed."""
    uniq: list[tuple[str, Measurement]] = []
    for ts, m in rows:
        if uniq and (uniq[-1][1].tree_sha, uniq[-1][1].pass_count,
                     uniq[-1][1].oracle_size) == (m.tree_sha, m.pass_count,
                                                  m.oracle_size):
            continue
        uniq.append((ts, m))
    lines: list[str] = []
    for (_, prev), (ts, cur) in zip(uniq, uniq[1:], strict=False):
        d = compare_measurements(prev, cur)
        mark = "OK  " if d.comparable else "NOT "
        lines.append(f"{ts[:19]} {mark}{d.verdict}")
        # Diff by BINARY-QUALIFIED verdict when both readings carry them. The
        # bare-name diff is what made every delta in the handovers +-3: a name
        # that runs in two binaries cannot be told apart, and a crashed binary
        # keeps its shared names "observed" via a surviving one.
        if prev.passed_pairs and cur.passed_pairs:
            won = cur.passed_pairs - prev.passed_pairs
            lost = prev.passed_pairs - cur.passed_pairs
            lines.append(
                f"{ts[:19]}      by pair: +{len(won)} -{len(lost)} "
                f"(net {len(won) - len(lost):+d})"
                + (f"  gained {sorted(won)[:4]}" if won else "")
                + (f"  lost {sorted(lost)[:4]}" if lost else ""))
        for m, whose in ((prev, "prev"), (cur, "cur")):
            rec = m.reconcile()
            if not rec.reconciled:
                lines.append(f"{ts[:19]}      {whose} UNRECONCILED: {rec.note}")
    return lines[-last:] if last > 0 else lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--product", default="anyhow")
    ap.add_argument("--cluster", default=None)
    ap.add_argument("--last", type=int, default=8)
    args = ap.parse_args()

    rows = load_measurements(Path(args.events), args.product, args.cluster)
    if not rows:
        print("pass-delta: no F6 measurement events found")
        return 0
    for line in report(rows, args.last):
        print(f"PASS-DELTA {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
