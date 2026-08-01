#!/usr/bin/env python3
"""Remove repeated attributes left in a filled tree by a since-fixed splice.

A splice that re-attached an item's attributes instead of replacing them
accumulated copies silently: rustc treats repeated `cfg`/`cold` as idempotent,
so no build ever failed, and the only symptom was that any count of those
attributes measured the duplication rather than the annotation. The splice was
repaired on 2026-07-27 (the replacement span now expands back over the outer
attributes) and the duplication has been frozen ever since — what remains is
fossil with no live writer.

This removes the fossil. It changes no compilation; it makes the attribute
counts mean what they say.

    python tools/dedupe_attribute_runs.py tmp/anyhow/.skeleton-filled/stable/src
    python tools/dedupe_attribute_runs.py <dir> --write

Dry-run by default. `--write` rewrites in place and then asks the syn extractor
to re-parse every file it changed, restoring any file that stops parsing —
hand-editing a tree the fill loop also writes is only safe while the pipeline
is down, and only with the parse gate.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.domain.rust_scan import (  # noqa: E402
    drop_duplicate_attributes,
    duplicate_attribute_runs,
)

EXTRACTOR = (Path(__file__).resolve().parent / "skeleton_extractor" / "target"
             / "release" / "skeleton-extractor")


def parses(path: Path) -> bool:
    """True iff the extractor's syn parse-check accepts `path`.

    Fail-CLOSED, unlike the pipeline's own `parse_ok`: this decides whether to
    keep a hand-edit, and "the checker was unavailable" is not a reason to keep
    one."""
    if not EXTRACTOR.exists():
        return False
    try:
        r = subprocess.run([str(EXTRACTOR), "--parse-check", "--input", str(path)],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return False
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", type=Path, help="source dir to scan (recursively)")
    ap.add_argument("--write", action="store_true",
                    help="apply the edits (default: report only)")
    args = ap.parse_args()

    files = sorted(args.directory.rglob("*.rs"))
    if not files:
        print(f"no .rs files under {args.directory}")
        return 1

    total_dups = 0
    changed: list[Path] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        dups = duplicate_attribute_runs(text)
        if not dups:
            continue
        total_dups += len(dups)
        cleaned = drop_duplicate_attributes(text)
        rel = path.relative_to(args.directory)
        print(f"{rel}: {len(dups)} duplicated attribute(s), "
              f"{len(text.splitlines()) - len(cleaned.splitlines())} line(s) to drop")
        if not args.write:
            continue
        path.write_text(cleaned, encoding="utf-8")
        if not parses(path):
            path.write_text(text, encoding="utf-8")
            print(f"  RESTORED {rel} — it no longer parses after the edit")
            return 2
        remaining = duplicate_attribute_runs(path.read_text(encoding="utf-8"))
        if remaining:
            path.write_text(text, encoding="utf-8")
            print(f"  RESTORED {rel} — {len(remaining)} duplicate(s) survived")
            return 2
        changed.append(path)

    if not total_dups:
        print(f"clean: no duplicated attributes under {args.directory}")
        return 0
    if not args.write:
        print(f"\n{total_dups} duplicated attribute(s) across {args.directory} "
              "— re-run with --write to remove them")
        return 0
    print(f"\nrewrote {len(changed)} file(s); all re-parse and are now clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
