"""A cluster's health verdict must be computed by a build that can SEE the
failing targets.

On 2026-08-01 `nightly` stopped with two rustc errors in its TEST binaries and
was still recorded `done — validated`: the verdict ran `cargo check`, which
builds the lib alone. An earlier fix stopped a `baseline_broken` cluster being
checkpointed; the verdict itself still could not observe the failure.
"""
from __future__ import annotations

from pipeline_core.application.generators.task2_generator import (
    StaticSkeletonTask2Generator as _Gen,
)
from pipeline_core.domain.models import BuildCluster


def _cluster(toolchain_needed: bool = False) -> BuildCluster:
    c = BuildCluster(name="nightly", env_extra={}, tarpaulin_exclude_files=[],
                     excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")
    c.requires_nightly = toolchain_needed
    return c


def test_the_verdict_command_builds_every_target() -> None:
    assert _Gen._cluster_check_cmd(
        _cluster(), all_targets=True) == "cargo check --all-targets"


def test_the_inner_loop_command_is_unchanged() -> None:
    """The per-fn fill loop checks after every splice; it must not pay for the
    test binaries on each one."""
    assert _Gen._cluster_check_cmd(_cluster()) == "cargo check"


def test_the_toolchain_still_leads_the_command() -> None:
    assert _Gen._cluster_check_cmd(
        _cluster(True), all_targets=True) == "cargo +nightly check --all-targets"
