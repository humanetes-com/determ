"""Cargo test-command construction (pure).

The one place that renders `cargo [+toolchain] test --no-fail-fast --test stem…`
from a cluster's toolchain + the test-file stems to run — so every scoped test
run (fill engine, task2 roles, task3 retry) issues the identical command shape.
"""
from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline_core.domain.models import BuildCluster


def parse_cmd(cmd: str) -> list[str]:
    """Split a shell command string into a list, falling back to str.split on parse error."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def build_test_cmd(
    test_stems: list[str], cluster: BuildCluster | None,
    *, include_lib: bool = False,
) -> str:
    """Build `cargo test --no-fail-fast [--lib] --test stem1 --test stem2 ...`
    honoring the cluster toolchain.

    `include_lib` adds the crate's own unit-test binary (src-side `#[cfg(test)]`
    mods + the `gen_tests` siblings the reconstruction preserves as its oracle).
    Without it the sweep runs only the `tests/` integration binaries, so those
    preserved tests never execute until task3; the caller that turns it on must
    also count the lib sources in the sweep's expected oracle, or the reported
    total exceeds the denominator and every lib test reads as an over-count."""
    parts = ["cargo"]
    if cluster and cluster.toolchain:
        parts.append(f"+{cluster.toolchain}")
    parts += ["test", "--no-fail-fast"]
    if include_lib:
        parts.append("--lib")
    for s in test_stems:
        parts += ["--test", s]
    return shlex.join(parts)


def expected_target_labels(
    test_stems: list[str], *, include_lib: bool = False,
) -> tuple[str, ...]:
    """The labels cargo prints on `Running <label> (<exe>)` for the targets
    :func:`build_test_cmd` asks for: `unittests src/lib.rs` for `--lib`, then
    `tests/<stem>.rs` per `--test stem`. Rendered beside the command so the
    vocabulary the sweep's build accounting (`test_results.sweep_build`)
    compares the stream against has one owner. Checked against the live
    `phase_c.tdd.iter` event of run `2131920-86818224` (cluster `nightly`,
    iter 2): its 31 stems plus the lib binary are exactly the event's 32
    binary labels."""
    labels: list[str] = ["unittests src/lib.rs"] if include_lib else []
    labels += [f"tests/{s}.rs" for s in test_stems]
    return tuple(labels)
