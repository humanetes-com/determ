#!/usr/bin/env python3
"""Which remaining stubs of a reconstructed tree the end gate proves nothing
can call, with the production reader, offline.

WHY. The end gate of run `3323407-984b07f2` (2026-11-08) exited 1 with every
oracle test green on one stub, `src/context.rs::write_str [impl Write for
Quoted<&mut fmt::Formatter<'_>>]`: its reader then was rustc's `dead_code`
lint, which never reports a trait-impl method. The reader that replaced it
(`application/unreached_stub_reading.read_unreached_stub_proofs`, rule in
`domain/stub_removal`) seeds a fixed point with the lint's dead set, marks
dead every unit (fn item or whole trait impl) whose every MIR use sits in dead
code, a closure counting as its creator, and certifies each stub by a removal
of its unit and the dead units that alone use it: the copy must build and every
surviving fn's MIR must equal the baseline's (deletion blanks, so positions do
not move). This script runs that reader and
the end gate's classifier on any tree, so the gate's verdict is read without
re-running task 3.

USAGE.
    .venv/bin/python scripts/measure_unreached_stubs.py [--tree tmp/anyhow]

  The tree is copied (symlinks followed, `target/` and `.git/` skipped) into a
  temporary directory first, so the reader's build outputs land in the copy
  and the tree is never written. Baked clusters, env and toolchains are the
  production ones (`load_baked_clusters`, `cluster_cargo_env`). Cost: per
  world that compiles a stub, two `cargo check` dead-code runs; where a
  trait impl holds only stubs, also two `cargo rustc -Zunpretty=mir` runs,
  one `cargo rustdoc` JSON run and, per removal group, one
  `cargo check --lib --tests` plus two `cargo rustc -Zunpretty=mir` dumps;
  a few minutes on anyhow, no GPU.

HOW TO INTERPRET THE OUTPUT.
  `[stub-gate/<world>] kept <site>: <why>` — a trait-impl stub a guard kept
  (an outside crate can reach it, the MIR names a call outside dead code, or
  deleting its impl breaks a build or changes another fn's MIR). A free fn or inherent method the lint
  does not report dead has no line: it is simply not PROVED.
  `PROVED <site> — <proof>` — a stub the gate moves out of the blocking list.
  Then the gate's own report (the NOTE / UNREACHED PRIVATE BODY / WARNING
  blocks task 3 prints) and `GATE success|blocked`. `blocked` lists what
  still fails the run; on-theory for a tree whose oracle is green is
  `success`, with every blocking stub explained by a `kept` line.
"""
from __future__ import annotations

import argparse
import functools
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.augment_store import FileAugmentStore  # noqa: E402
from pipeline_core.adapters.cargo import cluster_cargo_env  # noqa: E402
from pipeline_core.adapters.dead_code import measure_world_dead_code  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.fn_reachability import measure_world_reachability  # noqa: E402
from pipeline_core.adapters.structure import CliStructureReader, load_baked_clusters  # noqa: E402
from pipeline_core.adapters.stub_removal import measure_world_removal  # noqa: E402
from pipeline_core.application.orchestrator import (  # noqa: E402
    classify_remaining_stubs,
    report_stub_verdict,
)
from pipeline_core.application.reconstruct.stub_scan import scan_stub_inventory  # noqa: E402
from pipeline_core.application.unreached_stub_reading import (  # noqa: E402
    UnreachedStubDeps,
    read_unreached_stub_proofs,
)
from pipeline_core.domain.cfg import CfgSet  # noqa: E402
from pipeline_core.domain.fn_execution import site_label  # noqa: E402

_EXTRACTOR = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", type=Path, default=ROOT / "tmp" / "anyhow")
    args = ap.parse_args()
    src = args.tree.resolve()
    scratch = Path(tempfile.mkdtemp(prefix="unreached_stubs_"))
    tree = scratch / "tree"
    try:
        shutil.copytree(src, tree, symlinks=False,
                        ignore=shutil.ignore_patterns("target", ".git"))
        clusters = load_baked_clusters(tree)
        st = CliStructureReader(CliExtractor(_EXTRACTOR))
        deps = UnreachedStubDeps(
            enumerate_fns=st.enumerate_fns, enumerate_mods=st.enumerate_mods,
            measure_dead=functools.partial(
                measure_world_dead_code, cluster_env=cluster_cargo_env, timeout=1800),
            read_compiler_view=functools.partial(
                measure_world_reachability, cluster_env=cluster_cargo_env,
                timeout=1800, test_profile_mir=True),
            measure_removal=functools.partial(
                measure_world_removal, cluster_env=cluster_cargo_env, timeout=1800),
            log=print)
        proofs = read_unreached_stub_proofs(tree, clusters, deps)
        for site, proof in sorted(proofs.items(), key=lambda kv: site_label(kv[0])):
            print(f"PROVED {site_label(site)} — {proof}")
        cfg_sets = [CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false)
                    for c in clusters]
        stubs = scan_stub_inventory(tree, enumerate_fns=st.enumerate_fns).site_preds()
        # The gate's third filter reads augmentation's per-cluster records
        # (`.cluster_augmented/*.fns.json`, copied with the tree), so the
        # verdict below is the one task 3 prints, less the reference-hollow
        # bucket (that needs the original crate measured).
        records = FileAugmentStore().all_fn_records(tree)
        verdict = classify_remaining_stubs(stubs, cfg_sets, (), records, proofs)
        ok = report_stub_verdict(verdict, print, len(cfg_sets))
        print("GATE " + ("success" if ok else "blocked"))
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
