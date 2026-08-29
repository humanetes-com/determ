#!/usr/bin/env python3
"""Fail the gate when the fixture crates have no container-distinct homonyms.

WHY. Homonym collapse at write is invisible on hardcrate/microcrate (0%
keys with more than one DISTINCT container) and is the anyhow plateau
(25% of keys — measured 2026-08-29). A (file, fn_name) count treats
``#[cfg]`` toggle arms as homonyms: hardcrate ``cap_limit`` and
microcrate ``capacity_hint`` share a name across two arms with
``container=""`` both, so they collide under ``FnSite`` too and are
not the class ``FnSite`` fixes. Counting distinct containers per key
reproduces the plan's numbers (hardcrate/microcrate 0.0%, argh 21.7%
worst 4) and restores the gate's ability to fail.

USAGE. ``python tools/check_ambiguous_fixtures.py`` from the repo root
(check.sh runs it). Needs the release skeleton-extractor and the src
trees under tmp/. A missing required crate is a hard failure, not a
skip — deleting argh used to leave the gate green on the cfg-toggle
noise.

HOW TO INTERPRET. Per crate: keys, container-distinct-homonym keys and
%, worst distinct-container count on one key. The gate fails when
max(homonym%) == 0 (identity collapse cannot be tested) or when a
listed crate is absent/empty.
"""
from __future__ import annotations

import sys
from collections import defaultdict
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


def container_homonym_of(
    triples: Sequence[tuple[str, str, str]],
) -> dict[str, float | int]:
    """keys / homonym_keys / homonym_pct / worst for (file, name, container).

    Two rows with the same (file, name) and the same container (including
    both empty — cfg toggle arms) are one site, not a homonym.
    """
    by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for file, name, container in triples:
        by_key[(file, name)].add(container)
    n_keys = len(by_key)
    n_hom = sum(1 for cs in by_key.values() if len(cs) > 1)
    worst = max((len(cs) for cs in by_key.values()), default=0)
    hom_pct = (100.0 * n_hom / n_keys) if n_keys else 0.0
    return {
        "fns": len(triples),
        "keys": n_keys,
        "homonym_keys": n_hom,
        "homonym_pct": hom_pct,
        "worst": worst,
    }


def would_fail(stats: Sequence[dict[str, float | int]]) -> bool:
    """True when the fixture set has no container-distinct homonyms."""
    if not stats:
        return True
    return max(float(s["homonym_pct"]) for s in stats) == 0.0


def _enumerate(src: Path) -> list[tuple[str, str, str]]:
    sys.path.insert(0, str(ROOT))
    from pipeline_core.adapters.extractor import CliExtractor
    ext = CliExtractor(EXTRACTOR)
    recs = ext.enumerate_fns(src)
    if recs is None:
        return []
    return [(r.file, r.fn_name, r.container or "") for r in recs]


def evaluate(
    crates: Sequence[tuple[str, Path]] = CRATES,
    *,
    enumerate_fn=None,
) -> int:
    """Run the gate over ``crates``. Return 0 ok, 1 fail.

    ``enumerate_fn(src) -> triples`` is injected by tests; live uses
    the skeleton-extractor.
    """
    enum = enumerate_fn or _enumerate
    if enumerate_fn is None and not EXTRACTOR.exists():
        print(f"FAIL  skeleton-extractor missing at {EXTRACTOR}", file=sys.stderr)
        return 1
    rows: list[dict[str, float | int]] = []
    for name, src in crates:
        if not src.is_dir():
            print(f"FAIL  required fixture {name} missing at {src}",
                  file=sys.stderr)
            return 1
        triples = enum(src)
        if not triples:
            print(f"FAIL  required fixture {name} enumerate_fns empty/failed",
                  file=sys.stderr)
            return 1
        st = container_homonym_of(triples)
        rows.append(st)
        print(
            f"  {name:12} keys={st['keys']} "
            f"container-distinct-homonym keys={st['homonym_keys']} "
            f"({st['homonym_pct']:.1f}%) worst={st['worst']}"
        )
    if would_fail(rows):
        print(
            "FAIL  max container-distinct-homonym% == 0 — fixture set "
            "has no (file, name) key with more than one container; "
            "identity collapse cannot be tested (need a crate like argh).",
            file=sys.stderr,
        )
        return 1
    print("  OK  fixture ambiguity (max container-distinct-homonym% > 0)")
    return 0


def main() -> int:
    return evaluate()


if __name__ == "__main__":
    raise SystemExit(main())
