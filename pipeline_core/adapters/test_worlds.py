"""Run one generated test file under one cluster world (adapter).

The measurement `application.test_worlds` gates a generated test by: does the
file build under this world's env, and does every test of its block pass.
Same command the augmentation's own admission runs for the generating world
(`cargo test -p <module> --test <bin> -- --test-threads=1`), judged by the
same rule (`oracle_admission.judge_block`), under the env every cargo of that
cluster gets (`cluster_cargo_env`).

Its own target directory per world: the generating world's build lives in the
workspace target, and a run under another world's flags and toolchain there
would invalidate every fingerprint the next step of the augmentation relies
on.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.oracle_admission import judge_block
from ..domain.test_report import parse_test_output
from .process import run_bounded


def test_passes_in_world(
    project_dir: Path,
    module_name: str,
    test_bin: str,
    block: str,
    cluster: Any,
    *,
    cluster_env: Callable[[Any], dict[str, str]],
    timeout: int,
) -> bool:
    """Whether `tests/<test_bin>.rs` builds under `cluster` and every test of
    `block` is reported passing there. A run that did not finish, or that ran
    none of the block's tests, is not a pass."""
    env = cluster_env(cluster)
    env["CARGO_TARGET_DIR"] = str(
        project_dir / "target" / "worlds" / str(getattr(cluster, "name", "world")))
    run = run_bounded(
        ["cargo", "test", "-p", module_name, "--test", test_bin, "--",
         "--test-threads=1"],
        cwd=project_dir, env=env, timeout=float(timeout))
    if run.failure is not None or run.returncode != 0:
        return False
    output = (run.stdout or "") + (run.stderr or "")
    verdict = judge_block(block, output, run_ok=True)
    # Stricter than the generating world's admission on one point: every
    # declared test must have been REPORTED passing here. A file whose tests
    # this world compiles out (a gate the block itself carries) builds, runs
    # nothing and exits 0; that is not the test passing in this world.
    passed = {n.rsplit("::", 1)[-1] for n in parse_test_output(output).passed}
    return bool(verdict.declared) and verdict.admitted and verdict.declared <= passed


test_passes_in_world.__test__ = False  # type: ignore[attr-defined]
