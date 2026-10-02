"""rustc's dead-code analysis of one tree under one cluster world (adapter).

`cargo check --lib --message-format=json` with `--force-warn dead_code`
appended to the cluster's RUSTFLAGS. `--force-warn` is what makes this a
reading rather than a hope: the trees this pipeline writes open every file
with `#![allow(unused, dead_code, …)]` and the base cargo env carries
`-A warnings`, and a forced lint is reported over both. Measured on the merged
anyhow tree of run `292191-d60a5308` (2026-10-02), cluster `nightly`: twelve
diagnostics, among them the two functions the run ended on.

The check runs in its own target directory. Changing RUSTFLAGS invalidates
every fingerprint in the directory cargo test builds in, and a reading taken
at the end gate must not make the next sweep rebuild the crate.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.dead_code import WorldDeadCode, dead_code_spans
from .process import run_bounded

_FORCE = "--force-warn dead_code"


def measure_world_dead_code(
    project_dir: Path,
    cluster: Any,
    *,
    cluster_env: Callable[[Any], dict[str, str]],
    timeout: int = 900,
) -> WorldDeadCode:
    """The dead-code spans rustc reports for the library at `project_dir`
    under `cluster`, or the reason there is no reading (the tree does not
    build in that world, cargo is missing, the check timed out)."""
    name = str(getattr(cluster, "name", "?"))
    env = cluster_env(cluster)
    env["RUSTFLAGS"] = f"{env.get('RUSTFLAGS', '')} {_FORCE}".strip()
    env["CARGO_TARGET_DIR"] = str(project_dir / "target" / "dead-code" / name)
    cmd = ["cargo"]
    toolchain = getattr(cluster, "toolchain", None)
    if toolchain:
        cmd.append(f"+{toolchain}")
    cmd += ["check", "--lib", "--message-format=json"]
    run = run_bounded(cmd, cwd=project_dir, env=env, timeout=float(timeout))
    if run.failure is not None:
        return WorldDeadCode(name, reason=f"cargo check did not finish: {run.failure}")
    if run.returncode != 0:
        tail = (run.stderr or "").strip().splitlines()[-1:] or [""]
        return WorldDeadCode(
            name, reason=f"cargo check rc={run.returncode}: {tail[0][:200]}")
    return WorldDeadCode(name, spans=dead_code_spans(run.stdout))
