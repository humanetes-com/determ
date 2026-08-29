#!/usr/bin/env python3
"""Fail the gate when the fixture crates all have unique (file, fn_name) keys.

WHY. Homonym collapse at write is invisible on hardcrate/microcrate (0%
ambiguous keys) and is the anyhow plateau (25% of keys, 47% of fns behind
them — measured 2026-08-29). A gate that only extracts anyhow never sees a
crate whose names are unique, so it cannot tell whether identity is being
tested. This gate enumerates the live fixture trees and FAILS when every
crate's ambiguous-key rate is 0 — the fixture set no longer exercises the
class. argh (set_flag/parse at 4) is the homonym fixture; hardcrate and
microcrate are the unique-name negative controls.

USAGE. ``python tools/check_ambiguous_fixtures.py`` from the repo root
(check.sh runs it). Needs the release skeleton-extractor and the src trees
under tmp/. Missing trees are skipped and named; if none remain, the gate
fails (nothing measured).

HOW TO INTERPRET. Per crate: fns, keys, ambiguous% (keys with count>1),
behind% (fns sharing a key), worst (max homonyms on one key). The gate
fails when max(ambiguous%) == 0.
"""
from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR = (
    ROOT / "tools" / "skeleton_extractor" / "target" / "release"
    / "skeleton-extractor"
)
CRATES: tuple[tuple[str, Path], ...] = (
    ("hardcrate", ROOT / "tmp" / "hardcrate" / "src"),
    ("microcrate", ROOT / "tmp" / "microcrate" / "src"),
    ("argh", ROOT / "tmp" / "argh-master" / "argh" / "src"),
)


def ambiguity_of(file_fn: Sequence[tuple[str, str]]) -> dict[str, float | int]:
    """fns / keys / ambiguous_pct / behind_pct / worst for (file, name) pairs."""
    n_fns = len(file_fn)
    counts = Counter(file_fn)
    n_keys = len(counts)
    n_amb_keys = sum(1 for c in counts.values() if c > 1)
    n_behind = sum(c for c in counts.values() if c > 1)
    worst = max(counts.values()) if counts else 0
    amb_pct = (100.0 * n_amb_keys / n_keys) if n_keys else 0.0
    behind_pct = (100.0 * n_behind / n_fns) if n_fns else 0.0
    return {
        "fns": n_fns,
        "keys": n_keys,
        "ambiguous_keys": n_amb_keys,
        "ambiguous_pct": amb_pct,
        "behind": n_behind,
        "behind_pct": behind_pct,
        "worst": worst,
    }


def would_fail(stats: Sequence[dict[str, float | int]]) -> bool:
    """True when the fixture set has no homonyms — identity is untested."""
    if not stats:
        return True
    return max(float(s["ambiguous_pct"]) for s in stats) == 0.0


def _enumerate(src: Path) -> list[tuple[str, str]]:
    sys.path.insert(0, str(ROOT))
    from pipeline_core.adapters.extractor import CliExtractor
    ext = CliExtractor(EXTRACTOR)
    recs = ext.enumerate_fns(src)
    if recs is None:
        return []
    return [(r.file, r.fn_name) for r in recs]


def main() -> int:
    if not EXTRACTOR.exists():
        print(f"FAIL  skeleton-extractor missing at {EXTRACTOR}", file=sys.stderr)
        return 1
    rows: list[dict[str, float | int]] = []
    skipped: list[str] = []
    for name, src in CRATES:
        if not src.is_dir():
            skipped.append(f"{name} ({src})")
            continue
        pairs = _enumerate(src)
        if not pairs:
            skipped.append(f"{name} (enumerate_fns empty/failed)")
            continue
        st = ambiguity_of(pairs)
        rows.append(st)
        print(
            f"  {name:12} fns={st['fns']} keys={st['keys']} "
            f"AMBIGUOUS={st['ambiguous_keys']} ({st['ambiguous_pct']:.1f}%) "
            f"behind={st['behind']} ({st['behind_pct']:.1f}%) "
            f"worst={st['worst']}"
        )
    if skipped:
        print("  skipped: " + "; ".join(skipped))
    if would_fail(rows):
        print(
            "FAIL  max ambiguous% == 0 — fixture set has no homonyms; "
            "identity collapse cannot be tested (need a crate like argh "
            "with shared (file, fn_name) keys).",
            file=sys.stderr,
        )
        return 1
    print("  OK  fixture ambiguity (max ambiguous% > 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
