"""Probe: how many fns does futility escalation actually name?

Escalation is a HYPOTHESIS ("the accessors are futile — the PRODUCER of the
type is wrong"). A hypothesis that names most of the crate carries no
discriminating power: it dispatches every unit at every failing test, costing
one full fill pass per iteration and telling the model nothing.

Prints, per type name, the producer count under each clause of
`producers_for_types` so the predicate can be judged on real records rather
than on its docstring.

    .venv/bin/python tools/probe_escalation_precision.py <src_dir> [Type ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.structure import CliStructureReader  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src_dir = Path(sys.argv[1]).resolve()
    types = sys.argv[2:] or ["Error"]
    reader = CliStructureReader(CliExtractor(
        Path(__file__).resolve().parent / 'skeleton_extractor' / 'target'
        / 'release' / 'skeleton-extractor'))
    recs = reader.enumerate_fns(src_dir) or []
    print(f"records: {len(recs)} fn(s) under {src_dir}")
    for tn in types:
        ret_only, ctor, assoc = [], [], []
        for r in recs:
            key = (r.get("file", ""), r.get("fn_name", ""))
            if not (key[0] and key[1]):
                continue
            sig = r.get("sig_text", "") or ""
            body = r.get("body_text", "") or ""
            ret = sig.split("->", 1)[1] if "->" in sig else ""
            if tn in ret:
                ret_only.append(key)
            elif f"{tn} {{" in body:
                ctor.append(key)
            elif f"{tn}::" in body:
                assoc.append(key)
        total = len(ret_only) + len(ctor) + len(assoc)
        print(f"\n{tn}: {total} producer(s) of {len(recs)} fn(s) "
              f"({100.0 * total / max(1, len(recs)):.0f}%)")
        print(f"  returns {tn}          : {len(ret_only)}")
        print(f"  builds `{tn} {{`      : {len(ctor)}")
        print(f"  mentions `{tn}::`     : {len(assoc)}  <- use, not production")
        for label, keys in (("assoc-only", assoc),):
            print(f"  {label} sample: "
                  + ", ".join(f"{f}::{fn}" for f, fn in keys[:6]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
