"""Pins the test-environment prompt-context builder at its new home
(pipeline_core/prompts/test_environment.py, 2026-06-29 relocation)."""
from __future__ import annotations

from pipeline_core.prompts.test_environment import describe_test_environment


def test_visible_and_notable_vars() -> None:
    out = describe_test_environment({"RUSTFLAGS": "-C x"}, None, None)
    assert "Test environment" in out
    assert "RUSTFLAGS = '-C x'" in out
    assert "NOT SET in parent process: RUST_BACKTRACE" in out
    assert "cargo test" in out
    # no cluster → no cfg lines
    assert "active cfg predicates" not in out


def test_cluster_cfg_lines() -> None:
    out = describe_test_environment({}, ["feat"], ["ex"])
    assert "active cfg predicates: feat" in out
    assert "excluded cfg predicates: ex" in out


def test_empty_active_cfg_still_emits_section() -> None:
    # an active cluster with empty cfg lists still emits the (none) lines
    out = describe_test_environment({"RUSTFLAGS": "x"}, [], [])
    assert "active cfg predicates: (none)" in out
    assert "excluded cfg predicates: (none)" in out
