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

A pass also means each declared test passes ALONE, in a process of its own.
The file run is `--test-threads=1`, in declaration order, so a test can pass
only because an earlier test changed process state: run `801510-bb22799f`
(2026-10-20) admitted `tests/fmt_gen_1.rs`, whose
`debug_basic_message_is_preserved` asserts a captured backtrace that only
`debug_backtrace_path_is_exercised`'s `set_var("RUST_LIB_BACKTRACE", "1")`
enables, and task 3's parallel run then read it red or green by thread
scheduling. One test per process is the rule the loop's own runner applies to
an aborted binary.
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
    log: Callable[[str], None] = lambda _m: None,
) -> bool:
    """Whether `tests/<test_bin>.rs` builds under `cluster`, every test of
    `block` is reported passing there, and each of them passes again alone
    in its own process. A run that did not finish, or that ran none of the
    block's tests, is not a pass."""
    env = cluster_env(cluster)
    env["CARGO_TARGET_DIR"] = str(
        project_dir / "target" / "worlds" / str(getattr(cluster, "name", "world")))
    base = ["cargo", "test", "-p", module_name, "--test", test_bin, "--"]
    run = run_bounded([*base, "--test-threads=1"],
                      cwd=project_dir, env=env, timeout=float(timeout))
    if run.failure is not None or run.returncode != 0:
        return False
    output = (run.stdout or "") + (run.stderr or "")
    verdict = judge_block(block, output, run_ok=True)
    # Stricter than the generating world's admission on one point: every
    # declared test must have been REPORTED passing here. A file whose tests
    # this world compiles out (a gate the block itself carries) builds, runs
    # nothing and exits 0; that is not the test passing in this world.
    full = [n for n in parse_test_output(output).passed
            if n.rsplit("::", 1)[-1] in verdict.declared]
    passed = {n.rsplit("::", 1)[-1] for n in full}
    if not (bool(verdict.declared) and verdict.admitted
            and verdict.declared <= passed):
        return False
    alone_failed = [name for name in sorted(full)
                    if not _passes_alone(base, name, project_dir, env, timeout)]
    if alone_failed:
        log(f"  [{module_name}] {test_bin}.rs: "
            f"{', '.join(alone_failed)} fail(s) when run alone in its own "
            f"process in world '{getattr(cluster, 'name', '')}' — it passes "
            "only beside the file's other tests, so it is not a pass.")
        return False
    return True


def _passes_alone(base: list[str], name: str, project_dir: Path,
                  env: dict[str, str], timeout: int) -> bool:
    """One declared test, by its exact path, in a test process of its own:
    reported passing and nothing else run."""
    run = run_bounded([*base, "--exact", name, "--test-threads=1"],
                      cwd=project_dir, env=env, timeout=float(timeout))
    if run.failure is not None or run.returncode != 0:
        return False
    report = parse_test_output((run.stdout or "") + (run.stderr or ""))
    return report.passed == frozenset({name})


test_passes_in_world.__test__ = False  # type: ignore[attr-defined]
