#!/usr/bin/env python3
"""Why is stub X not in the phase-c frontier?

Replays the exact chain the live run uses — extractor `--enumerate-fns` →
`scan_unimplemented_functions` → `fn_belongs_to_cluster` — against a cluster
tree on disk, and prints the verdict for every stub it finds. A stub that the
extractor reports but the cluster filter drops (or that never reaches the
scan at all) is the answer to "the run says 0 active stubs but the file still
has `unimplemented!()`".

    .venv/bin/python tools/probe_stub_frontier.py [--project anyhow]
                                                  [--cluster stable]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.application.reconstruct import cluster_membership as _m  # noqa: E402
from pipeline_core.application.reconstruct import impl_blocks as _blocks  # noqa: E402
from pipeline_core.application.reconstruct.stub_scan import (  # noqa: E402
    scan_unimplemented_functions,
)
from pipeline_core.domain import cfg as _cfg  # noqa: E402


class _Cluster:
    """Minimal stand-in for the run's cluster object — only the fields the
    membership filter reads."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.name = spec["name"]
        self.cfg_true = list(spec.get("cfg_true") or [])
        self.cfg_false = list(spec.get("cfg_false") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--bin", default="tools/skeleton_extractor/target/release/skeleton-extractor")
    args = ap.parse_args()

    root = Path("tmp") / args.project
    src = root / ".skeleton-filled" / args.cluster / "src"
    spec = next(c for c in json.loads((root / ".skeleton" / "_clusters.json")
                                      .read_text())["clusters"]
                if c["name"] == args.cluster)
    cluster = _Cluster(spec)
    ex = CliExtractor(Path(args.bin))

    def enumerate_fns(sd: Path) -> list[dict[str, Any]] | None:
        recs = ex.enumerate_fns(sd)
        return None if recs is None else [
            {f.name: getattr(r, f.name) for f in dataclasses.fields(r)}
            for r in recs]

    def enumerate_mods(sd: Path) -> Any:
        return ex.enumerate_mods(sd)

    tgt = root / ".skeleton-filled" / args.cluster
    blocks = _blocks.collect_impl_blocks(tgt, enumerate_fns=enumerate_fns)
    kept_blocks = _m.collect_impl_blocks_for_cluster(
        tgt, cluster,
        collect_impl_blocks=lambda d: blocks,
        fn_belongs=lambda d, f, n, c, fn_cfg_predicates=None: (
            _m.fn_belongs_to_cluster(d, f, n, c, fn_cfg_predicates,
                                     mod_records=enumerate_mods)))
    print(f"blocks={len(blocks or [])} cluster_blocks={len(kept_blocks or [])}")
    for b in kept_blocks or []:
        if b.stub_members:
            print(f"  STUBBED BLOCK  {b.file} | container={b.container!r} | "
                  f"{[m.fn_name for m in b.stub_members]}")

    raw = scan_unimplemented_functions(src, enumerate_fns=enumerate_fns)
    print(f"tree={src}  cluster={cluster.name}  scan_stubs={len(raw)}")
    print(f"cfg_true={cluster.cfg_true}  cfg_false={cluster.cfg_false}")
    cfg_set = _cfg.CfgSet.of(set(cluster.cfg_true), set(cluster.cfg_false))
    kept = 0
    for loc in raw:
        ok = _m.fn_belongs_to_cluster(src, loc.file, loc.fn_name, cluster,
                                      mod_records=enumerate_mods)
        kept += ok
        # Re-derive the predicates the verdict came from: a bare KEEP/SKIP
        # cannot distinguish "the fn's own cfg excluded it" from "the backward
        # text-walk captured a PRECEDING item's cfg", which is the failure this
        # probe exists to catch.
        file_pred = _m.file_cfg_predicate(src, loc.file, mod_records=enumerate_mods)
        try:
            text = (src / loc.file).read_text(encoding="utf-8")
        except OSError:
            text = ""
        fn_preds = _m.fn_cfg_predicates(text, loc.fn_name) if text else []
        why = [(p, cfg_set.evaluate(p))
               for p in ([file_pred] if file_pred else []) + fn_preds]
        detail = "  ".join(f"{p!r}->{v}" for p, v in why) or "(ungated)"
        print(f"  {'KEEP' if ok else 'SKIP'}  {loc.file}::{loc.fn_name}  {detail}")
    print(f"cluster-active stubs = {kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
