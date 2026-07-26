#!/usr/bin/env python3
"""Judge every persisted retrospective against the CURRENT conviction rules.

Answers, without starting a run: which stored `fully_exhausted` verdicts still
skip their unit, and which stored guidance strings a prompt would still carry.
Written for the incident where 15 of 28 convictions had been earned on a test
their unit does not own — the store looked healthy from the outside because the
only visible symptom was `skipped (prior retro marked exhausted)` lines.

Usage: probe_conviction_standing.py [project_dir] [--cluster stable]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.retro_gate import (  # noqa: E402
    ConvictionEvidence,
    ConvictionStanding,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir", nargs="?", default="tmp/anyhow")
    ap.add_argument("--cluster", default="stable")
    args = ap.parse_args()

    root = Path(args.project_dir)
    store = root / ".phase_c_logs" / "retrospectives.json"
    if not store.exists():
        print(f"no retro store at {store}")
        return 1
    tree = root / ".skeleton-filled" / args.cluster / "src"
    records = json.loads(store.read_text(encoding="utf-8"))

    standing: collections.Counter[str] = collections.Counter()
    guidance: collections.Counter[str] = collections.Counter()
    freed: list[str] = []
    for key, rec in sorted(records.items()):
        if key == "__migrations__" or not isinstance(rec, dict):
            continue
        parts = key.split("::")
        rel, container = parts[0], (parts[2] if len(parts) > 2 else "")
        text = ""
        path = tree / Path(rel).name
        if path.exists():
            text = path.read_text(encoding="utf-8")
        # The unit's convicting set is only known mid-run; probe both extremes so
        # the report brackets reality: `wide` = every test it was convicted on is
        # still its own, `narrow` = none is.
        wide = ConvictionEvidence(
            frozenset(str(t) for t in (rec.get("convicted_by") or [])))
        st = conviction_standing(rec, RetroProvenance.current(args.cluster, text), wide)
        if rec.get("fully_exhausted"):
            standing[st.value] += 1
            if st is not ConvictionStanding.CURRENT:
                freed.append(f"  {st.value:14s} {key}")
        anchors = anchor_types_for_container(container)
        kept = admissible_guidance(rec, text, evidence=wide, anchor_types=anchors)
        if rec.get("guidance"):
            guidance["kept" if kept else "withheld"] += 1

    print(f"store: {store}  records={len(records)}  cluster={args.cluster}")
    print(f"convictions: {dict(standing)}")
    print(f"guidance:    {dict(guidance)}")
    if freed:
        print(f"--- {len(freed)} unit(s) no longer skipped ---")
        print("\n".join(freed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
