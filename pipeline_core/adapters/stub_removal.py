"""The removal build: does the tree still build, in one cluster world, with
some items deleted (adapter).

The end gate asks whether anything in the rebuilt crate or its oracle needs a
stub's unit (`domain.stub_removal`): the stub's fn or impl, together with
the dead units that alone use it (its bundle). This adapter answers it the
only way that names no dispatch rule: delete the items on a scratch copy,
run `cargo check --lib --tests` under the cluster's env and toolchain, and
when that builds, dump the library's MIR (`-Zunpretty=mir`, default and test
profile) and require every surviving fn's MIR to equal the baseline copy's
(`domain.stub_removal.mir_unchanged`). `--tests` compiles the library under
`cfg(test)` (its in-crate test modules) and every integration test target,
so an oracle test that needs the item makes the build fail; the MIR
comparison catches what builds and still changed the program (a call that
fell back to another method, a drop the compiler no longer inserts).

`base` is deleted in every build beside the items asked about: the fns the
lint reports dead, so a dead caller does not fail the build for a reason that
is not a use. When the copy with `base` deleted does not build (the lint's
dead set and the build disagree), the builds run without it, which can only
keep more.

The copy follows symlinks (the tree's `tests` is a symlink into the
workspace oracle; a copy that kept the link would let an edit reach the
oracle), and leaves out `target/` and `.git/`. The tree itself is never
written. The check runs in its own target directory under the tree, so the
registry dependencies stay built across readings and the fingerprints the
test sweeps build in are not touched.
"""
from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..domain.stub_removal import ItemKey, delete_ranges, mir_unchanged, removable_subset
from .fn_reachability import lib_crate_name
from .process import run_bounded


def measure_world_removal(
    project_dir: Path,
    cluster: Any,
    bundles: Mapping[ItemKey, frozenset[ItemKey]],
    base: frozenset[ItemKey] = frozenset(),
    *,
    cluster_env: Callable[[Any], dict[str, str]],
    timeout: int = 900,
) -> tuple[frozenset[ItemKey], str]:
    """The keys of `bundles` whose bundle's deletion (with `base`) still
    builds in `cluster` and leaves every surviving fn's MIR as it was, and ""
    — or an empty set and the reason there is no reading (the unmodified copy
    does not build, cargo did not finish, no MIR could be dumped)."""
    if not bundles:
        return frozenset(), ""
    name = str(getattr(cluster, "name", "?"))
    env = cluster_env(cluster)
    env["CARGO_TARGET_DIR"] = str(project_dir / "target" / "stub-removal" / name)
    cmd = ["cargo"]
    toolchain = getattr(cluster, "toolchain", None)
    if toolchain:
        cmd.append(f"+{toolchain}")
    mir_cmds = [[*cmd, "rustc", "--lib", "--", "-Zunpretty=mir"],
                [*cmd, "rustc", "--lib", "--profile", "test", "--", "-Zunpretty=mir"]]
    cmd += ["check", "--lib", "--tests", "--message-format=short"]
    crate = lib_crate_name(project_dir)
    if not crate:
        return frozenset(), "no package name in Cargo.toml"
    mir_env = dict(env)
    mir_env["RUSTC_BOOTSTRAP"] = crate
    mir_env["CARGO_TARGET_DIR"] = str(project_dir / "target" / "stub-removal" / f"{name}-mir")
    scratch = Path(tempfile.mkdtemp(prefix="stub_removal_"))
    copy = scratch / "tree"
    try:
        shutil.copytree(project_dir, copy, symlinks=False,
                        ignore=shutil.ignore_patterns("target", ".git"))
        asked = set(bundles)
        files = {k[0] for b in bundles.values() for k in b} | {k[0] for k in base}
        originals = {rel: (project_dir / rel).read_bytes() for rel in files}
        failure: list[str] = []
        always: set[ItemKey] = set(base)
        baseline: list[str] = []

        def _dumps() -> list[str] | None:
            out: list[str] = []
            for mir_cmd in mir_cmds:
                run = run_bounded(mir_cmd, cwd=copy, env=mir_env, timeout=float(timeout))
                if run.failure is not None:
                    failure.append(f"cargo rustc -Zunpretty=mir did not finish: {run.failure}")
                    return None
                if run.returncode != 0 or not (run.stdout or "").strip():
                    return None
                out.append(run.stdout)
            return out

        def _build(keys: frozenset[ItemKey]) -> bool:
            ranges: dict[str, list[tuple[int, int]]] = {}
            for k in {x for key in keys for x in bundles[key]} | always:
                ranges.setdefault(k[0], []).append((k[1], k[2]))
            for rel, text in originals.items():
                (copy / rel).write_bytes(delete_ranges(text, ranges.get(rel, ())))
            run = run_bounded(cmd, cwd=copy, env=env, timeout=float(timeout))
            if run.failure is not None:
                failure.append(f"cargo check did not finish: {run.failure}")
                return False
            if run.returncode != 0:
                return False
            dumps = _dumps()
            if dumps is None:
                return False
            if not keys:
                baseline[:] = dumps
                return True
            # The program changed although it builds: a call fell back to
            # another candidate, or the compiler stopped inserting one.
            return all(mir_unchanged(b, c) for b, c in zip(baseline, dumps, strict=True))

        if not _build(frozenset()):
            if failure:
                return frozenset(), failure[0]
            always.clear()
            if not _build(frozenset()):
                return frozenset(), (failure[0] if failure else
                                     "the unmodified copy does not build in this world, "
                                     "or its MIR could not be dumped")
        got = removable_subset(sorted(asked), _build)
        if failure:
            return frozenset(), failure[0]
        return got, ""
    except OSError as exc:
        return frozenset(), f"scratch copy failed: {exc}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
