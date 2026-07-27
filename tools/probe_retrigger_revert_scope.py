#!/usr/bin/env python3
"""Replay the layout-retrigger revert-scope decision chain against a live tree.

`retrigger_layout` enumerates the live fns from the CRATE ROOT while the layout
plan enumerates them from `src/`; the two bases must both work or the revert
scope silently reads empty and the run logs "reverted 0 referencing fn(s)" as a
success. Prints each stage so a zero can be attributed to a real empty scope
rather than to an enumeration that returned nothing.

    .venv/bin/python tools/probe_retrigger_revert_scope.py <crate_dir> [name ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.domain.layout_retrigger import find_fns_referencing  # noqa: E402
from pipeline_core.domain.oracle_immutability import production_fns  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    crate = Path(sys.argv[1]).resolve()
    names = frozenset(sys.argv[2:]) or frozenset({"Error", "ErrorImpl", "vtable"})

    ex = CliExtractor(
        Path(__file__).resolve().parent
        / "skeleton_extractor/target/release/skeleton-extractor")
    for label, d in (("crate root", crate), ("src dir", crate / "src")):
        recs = ex.enumerate_fns(d)
        if recs is None:
            print(f"{label:10} {str(d):55} -> None (extractor unavailable)")
            continue
        prod = production_fns(recs)
        ref = find_fns_referencing(recs, names)
        sample = recs[0].file if recs else "-"
        print(f"{label:10} {str(d):55} -> {len(recs):4} fns, "
              f"{len(prod):4} production, {len(ref):4} referencing "
              f"| file[0]={sample!r}")
        for loc in ref[:5]:
            print(f"             would revert: {loc.file}::{loc.fn_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
