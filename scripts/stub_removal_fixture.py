#!/usr/bin/env python3
"""Does a reconstructed tree still build, in every baked world, with some items deleted?

WHY. The end gate of run `3323407-984b07f2` (2026-11-08) exited 1 on one stub,
`src/context.rs::write_str [impl Write for Quoted<&mut fmt::Formatter<'_>>]`,
with every oracle test green: its "no rebuilt caller" reader is rustc's
`dead_code` lint, which never reports a trait-impl method. The plan reviewed
that day proposes a different reader: delete the stub's smallest removable item
(the fn, or the whole impl block when every method in it is a stub) on a
scratch copy, and call the stub uncalled when the crate and its tests still
build in every world. This script takes that reading on any tree, so the plan's
fixture and its negative control (an item that IS used, whose deletion must
break the build) are computed rather than described.

USAGE.
    .venv/bin/python scripts/stub_removal_fixture.py --tree tmp/anyhow \
        [--item src/context.rs:153-157 ...] [--world stable] [--keep]

  --tree   the crate root holding Cargo.toml and build_configs' baked clusters
           (read with the production `load_baked_clusters`, env from
           `cluster_cargo_env`, toolchain from the cluster).
  --item   FILE:START-END, 1-based inclusive line range to delete; repeatable.
           No --item = the baseline (the unmodified copy), which must build.
  --world  restrict to one baked cluster; default every cluster.
  --keep   leave the scratch copy (path printed) instead of deleting it.
The copy is `cp -rL` semantics (symlinks followed, so the oracle's `tests`
symlink is copied, never shared) and skips `target/`. The tree itself is
never written. Cost: one `cargo check --lib --tests` per world, in its own
target directory under the copy (a cold build, ~1 min on anyhow). No GPU.

HOW TO INTERPRET THE OUTPUT. One line per world:
    world=<name> rc=<n> builds=<yes|no>   then up to 8 `error` lines.
`builds=yes` in every world means nothing in the lib, its in-crate test
modules, or the integration tests needs the deleted items (direct call, a
`dyn` coercion, a generic bound, a macro expansion). A final line
`VERDICT removable-in-every-world` or `VERDICT needed-in: <worlds>`.
A reading is only meaningful beside the baseline (no --item) building
in the same worlds; a baseline that fails means the tree does not build and
the removal reading says nothing. Known blind spot, by construction: a
deletion that lets method resolution fall back to another candidate
(autoderef to a `Deref` target's method of the same name, or a trait
method of the same name in scope) builds yet changed which body a call
reaches; a removable verdict is therefore "no caller unless a fallback
candidate exists", not a proof on its own.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline_core.adapters.cargo import cluster_cargo_env  # noqa: E402
from pipeline_core.adapters.structure import load_baked_clusters  # noqa: E402


def _parse_item(spec: str) -> tuple[str, int, int]:
    path, _, rng = spec.rpartition(":")
    a, _, b = rng.partition("-")
    return path, int(a), int(b or a)


def _delete(copy: Path, items: list[tuple[str, int, int]]) -> None:
    by_file: dict[str, list[tuple[int, int]]] = {}
    for f, a, b in items:
        by_file.setdefault(f, []).append((a, b))
    for f, ranges in by_file.items():
        p = copy / f
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        drop = {i for a, b in ranges for i in range(a - 1, b)}
        p.write_text("".join(x for i, x in enumerate(lines) if i not in drop),
                     encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", required=True, type=Path)
    ap.add_argument("--item", action="append", default=[])
    ap.add_argument("--world")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    tree = args.tree.resolve()
    clusters = [c for c in load_baked_clusters(tree)
                if not args.world or str(c.name) == args.world]
    if not clusters:
        print("no baked cluster", file=sys.stderr)
        return 2
    items = [_parse_item(s) for s in args.item]
    scratch = Path(tempfile.mkdtemp(prefix="stub_removal_"))
    copy = scratch / "tree"
    shutil.copytree(tree, copy, symlinks=False,
                    ignore=shutil.ignore_patterns("target", ".git"))
    for f, a, b in items:
        print(f"delete {f}:{a}-{b}")
        for ln in (copy / f).read_text(encoding="utf-8").splitlines()[a - 1:b]:
            print(f"    | {ln}")
    _delete(copy, items)
    needed: list[str] = []
    for c in clusters:
        env = cluster_cargo_env(c)
        env["CARGO_TARGET_DIR"] = str(scratch / "target" / str(c.name))
        cmd = ["cargo"] + ([f"+{c.toolchain}"] if c.toolchain else []) + [
            "check", "--lib", "--tests", "--message-format=short"]
        run = subprocess.run(cmd, cwd=copy, env=env, capture_output=True,
                             text=True, timeout=1800)
        ok = run.returncode == 0
        print(f"world={c.name} rc={run.returncode} builds={'yes' if ok else 'no'}")
        if not ok:
            needed.append(str(c.name))
            errs = [ln for ln in run.stderr.splitlines() if "error" in ln]
            for ln in errs[:8]:
                print(f"    {ln}")
    if not items:
        print("VERDICT baseline-" + ("builds" if not needed
                                     else "fails-in: " + ", ".join(needed)))
    else:
        print("VERDICT " + ("removable-in-every-world" if not needed
                            else "needed-in: " + ", ".join(needed)))
    if args.keep:
        print(f"copy kept at {copy}")
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
