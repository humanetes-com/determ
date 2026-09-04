#!/usr/bin/env python3
"""Replay the ledger re-seed on a scratch copy of a cluster tree, once with the
stub scan's RAW per-site list as `wanted` and once with it collapsed to units.

WHY THIS EXISTS
---------------
Run `2973-66e50ad9` (2026-09-04, cluster `stable`) resumed a tree that built and
scored 25/125. Sixteen seconds after process start the cluster-entry re-seed
printed `7 rejected (did not build); 11 cargo check(s)` and the next three
sweeps read `pass=0 fail=127 … src/context.rs:35: error: unexpected closing
delimiter`. The seven rejected keys were `src/context.rs::ext_context` twice,
`src/ensure.rs::render` once and `src/error.rs::from` four times: the stub scan
returned one locator per extractor RECORD (per impl site), the re-seed handed
each copy to the splicer, and two identical body spans applied to one text
corrupted the file. The bisect's rollback re-derived spans by name over the
text it just wrecked, so the file stayed broken and every later file was
rejected for that file's reason.

This script computes that fixture on the real inputs — the real ledger under
`logs/body_ledger/<project>`, the real extractor binary, the real writer, the
real gate, the real parse validator and a real `cargo check --lib` — on a
scratch copy of the best tree, so the claim "the seven rejections were
manufactured by the multiplicity" is a measurement, and so the fix is judged by
the same command: on the pre-fix code the two arms differed (see below); after
the fix (`StubInventory`, the `TreeTxn` re-seed) they must be identical.

USAGE
-----
    .venv/bin/python scripts/reseed_multiplicity_fixture.py [--project anyhow]
        [--cluster stable] [--tree best|filled] [--workdir DIR]

Preconditions: the extractor release binary is built
(`tools/skeleton_extractor/target/release/skeleton-extractor`), the workspace
`tmp/<project>` holds `.best_tree/<cluster>/src` (or `.skeleton-filled/<cluster>`
for `--tree filled`), `.skeleton/<cluster>` and `.skeleton-filled/<cluster>/
Cargo.toml`. Runs `cargo check` a handful of times in a scratch crate with its
own CARGO_TARGET_DIR (first check is a full dependency build, later ones
cached); no GPU, no model, nothing in the workspace is touched. `--workdir`
defaults to a fresh temporary directory, printed at the end and left in place.

HOW TO INTERPRET THE OUTPUT
---------------------------
Section A — for each of the best tree, the filled tree and the skeleton:
`N stub site(s) in M unit(s)` from `scan_stub_inventory` with the crate root
passed exactly as the loop passes it, then every unit that has more than one
site and how many. Sites minus units is the multiplicity every pre-fix
`N of M stub(s)` count in the run log was inflated by.

Section B — two arms on a fresh scratch copy of the same tree:
  RAW_per_site    `wanted` = one locator per SITE (what the pre-fix scan handed
                  `_reseed_site`; built here from the inventory's sites)
  CTRL_per_unit   `wanted` = the inventory's units (what the loop passes now)
For each arm: whether the untouched tree builds (must be True or the arm
measures nothing), the `[phase-c/ledger]` bisect lines, the outcome line the
loop would print, the seeded and rejected keys, the skip reasons, and AFTER:
which `src/*.rs` files the extractor can no longer parse and whether the tree
builds, plus the cargo-check count.

Decision rule. Pre-fix (2026-09-04, before the transactional re-seed): RAW
showed the seven repeated rejected keys, `src/context.rs` unparseable and
`tree builds = False`, while CTRL seeded those same three units with one cargo
check and `tree builds = True` — the defect was the multiplicity, not the
bodies. Post-fix both arms must print the same seeded set, the same single
cargo check count and `unparseable files = none`, because `reseed()` now
collapses `wanted` to units and the writer takes a mapping. If CTRL rejects a
unit, that body genuinely does not build on this tree and the ledger row is
the thing to read next. If the two arms ever differ again, the collapse has
regressed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.body_ledger_store import default_root, load_entries  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.fn_reseed import (  # noqa: E402
    current_bodies,
    current_signatures,
    splice_bodies,
)
from pipeline_core.adapters.oracle_fingerprint import cluster_oracle_fp  # noqa: E402
from pipeline_core.application.reconstruct.body_reseed import TreeWriter, reseed  # noqa: E402
from pipeline_core.application.reconstruct.stub_scan import scan_stub_inventory  # noqa: E402
from pipeline_core.domain.models import FnLocator  # noqa: E402

BIN = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"


def _stage_src(work: Path, name: str, src_from: Path) -> Path:
    crate = work / name
    if crate.exists():
        shutil.rmtree(crate)
    (crate / "src").mkdir(parents=True)
    for p in src_from.rglob("*.rs"):
        rel = p.relative_to(src_from)
        (crate / "src" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, crate / "src" / rel)
    return crate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--tree", choices=("best", "filled"), default="best",
                    help="which tree the two arms replay on (default: the best tree)")
    ap.add_argument("--workdir", type=Path, default=None)
    args = ap.parse_args()

    ws = ROOT / "tmp" / args.project
    if not BIN.exists():
        print(f"extractor binary missing: {BIN} — run `bash pre_run.sh` first", file=sys.stderr)
        return 2
    work = args.workdir or Path(tempfile.mkdtemp(prefix="reseed_fixture_"))
    work.mkdir(parents=True, exist_ok=True)
    ext = CliExtractor(BIN)

    def enum(d: Path) -> list[dict[str, object]] | None:
        recs = ext.enumerate_fns(d)
        return None if recs is None else [r.to_dict() for r in recs]

    trees = [
        ("best tree", ws / ".best_tree" / args.cluster / "src"),
        ("filled tree", ws / ".skeleton-filled" / args.cluster / "src"),
        ("skeleton", ws / ".skeleton" / args.cluster / "src"),
    ]
    print("=== A: stub-scan multiplicity (crate root passed, as the loop does) ===")
    for label, src in trees:
        if not src.is_dir():
            print(f"{label}: {src} missing — skipped")
            continue
        crate = _stage_src(work, "scan_" + label.replace(" ", "_"), src)
        inv = scan_stub_inventory(crate, enumerate_fns=enum)
        multi = [(f"{u.file}::{u.fn_name}", len(sites))
                 for u, sites in inv.by_unit.items() if len(sites) > 1]
        print(f"{label:12s} {src.relative_to(ws)}: {inv.render_count()} "
              f"(+{inv.site_count - inv.unit_count} phantom under a per-site count)")
        print("   multi-site units:", ", ".join(f"{k} x{n}" for k, n in multi) or "none")

    print()
    replay_src = (ws / ".best_tree" / args.cluster / "src" if args.tree == "best"
                  else ws / ".skeleton-filled" / args.cluster / "src")
    print(f"=== B: the re-seed replayed on {replay_src.relative_to(ws)} ===")
    manifest = ws / ".skeleton-filled" / args.cluster / "Cargo.toml"
    lock = ws / ".skeleton-filled" / args.cluster / "Cargo.lock"
    if not (replay_src.is_dir() and manifest.exists()):
        print("replay inputs missing — nothing to measure", file=sys.stderr)
        return 2
    entries = load_entries(default_root(), args.project, cluster=args.cluster)
    oracle_fp = cluster_oracle_fp(ws / ".skeleton-filled" / args.cluster, ws, args.cluster)
    print(f"ledger rows (cluster {args.cluster}): {len(entries)}; oracle fp of the live "
          f"filled tree: {oracle_fp}")
    writer = TreeWriter(splice=splice_bodies, signatures=current_signatures,
                        bodies=current_bodies)
    target_dir = work / "cargo_target"

    def arm(name: str, per_site: bool) -> None:
        crate = _stage_src(work, name, replay_src)
        shutil.copy2(manifest, crate / "Cargo.toml")
        if lock.exists():
            shutil.copy2(lock, crate / "Cargo.lock")
        env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir))
        builds = [0]

        def check() -> tuple[bool, str]:
            builds[0] += 1
            p = subprocess.run(["cargo", "check", "--lib", "--quiet"], cwd=crate, env=env,
                               capture_output=True, text=True, timeout=600)
            return p.returncode == 0, p.stderr[:400]

        ok0, err0 = check()
        print(f"[{name}] untouched tree builds: {ok0} {'' if ok0 else err0[:200]}")
        inv = scan_stub_inventory(crate, enumerate_fns=enum)
        wanted: list[FnLocator] = ([s.locator for s in inv.sites] if per_site
                                   else inv.unit_list())
        print(f"[{name}] wanted: {len(wanted)} element(s) ({len(set(wanted))} distinct)")
        log: Callable[[str], None] = print
        out = reseed(site=name, entries=entries, wanted=wanted, project=args.project,
                     cluster=args.cluster, src_dir=crate,
                     skeleton_root=ws / ".skeleton" / args.cluster,
                     writer=writer, oracle_fp=oracle_fp, check_builds=check,
                     validate_parse=ext.parse_check, log=log)
        print(f"[{name}] outcome: {out.render()}")
        print(f"[{name}] seeded:   {[f'{k.file}::{k.fn_name}' for k in out.seeded]}")
        print(f"[{name}] rejected: {[f'{k.file}::{k.fn_name}' for k in out.rejected]}")
        print(f"[{name}] skipped by reason: {dict(Counter(why for _k, why in out.skipped))}")
        bad = [str(p.relative_to(crate)) for p in sorted((crate / "src").rglob("*.rs"))
               if not ext.parse_check(p)]
        okf, errf = check()
        first = errf.strip().splitlines()[0] if errf.strip() else "?"
        print(f"[{name}] AFTER: unparseable files = {bad or 'none'}; tree builds = {okf}"
              + ("" if okf else f"; first stderr: {first}"))
        print(f"[{name}] cargo checks run by this arm (incl. before/after): {builds[0]}")

    arm("RAW_per_site", per_site=True)
    print()
    arm("CTRL_per_unit", per_site=False)
    print(f"\nscratch left in place: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
