"""Unit tests for the discover-test-command use-case
(`pipeline_core.application.tasks.discover_test_command`), P6. Fake ask/sys_exit.
"""
from __future__ import annotations

import pytest

from pipeline_core.application.tasks.discover_test_command import (
    DiscoverTestCommandDeps,
    discover_test_command,
)


def _deps(answer: str):
    def _exit(_code):
        raise SystemExit(_code)
    return DiscoverTestCommandDeps(
        ask=lambda prompt, model=None: answer, task0_model="m", model_name="M",
        extra_cap=3000, sys_exit=_exit,
    )


def test_extracts_command_from_readme(tmp_path):
    out = discover_test_command("Run `cargo test` to test.", None, _deps("cargo test"))
    assert out == "cargo test"


def test_normalizes_bare_tool():
    assert discover_test_command("readme", None, _deps("npm")) == "npm test"


def test_rust_project_non_cargo_falls_back(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    out = discover_test_command("readme", tmp_path, _deps("pytest"))
    assert out == "cargo test"


def test_empty_response_exits():
    with pytest.raises(SystemExit):
        discover_test_command("readme", None, _deps("not a command at all"))
