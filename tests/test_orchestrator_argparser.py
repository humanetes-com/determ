"""build_arg_parser (P5): the CLI surface extracted from main()."""
from __future__ import annotations

import argparse
from pathlib import Path

from pipeline_core.application.orchestrator import build_arg_parser, run_phase_subcommand


def test_returns_parser_with_defaults() -> None:
    p = build_arg_parser()
    assert isinstance(p, argparse.ArgumentParser)
    ns = p.parse_args([])
    assert ns.project == "agave"
    assert ns.vram_gb == 32.0
    assert ns.clean is False
    assert ns.repo is None


def test_known_flags_parse() -> None:
    ns = build_arg_parser().parse_args(
        ["--project", "anyhow-master", "--clean", "--from-spec", "--audit",
         "--phase-c-only", "--vram-gb", "48"])
    assert ns.project == "anyhow-master"
    assert ns.clean and ns.from_spec and ns.audit and ns.phase_c_only
    assert ns.vram_gb == 48.0


def test_unknown_flag_errors() -> None:
    import pytest
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--definitely-not-a-flag"])


def _handlers(calls: list[str]) -> dict[str, object]:
    return {f: (lambda _pd, _f=f: (calls.append(_f), 7)[1]) for f in
            ("phase_a_only", "phase_b_only", "phase_c_only", "phase_d_only", "skeleton_backend")}


def test_subcommand_none_when_no_flag() -> None:
    calls: list[str] = []
    ns = build_arg_parser().parse_args([])
    assert run_phase_subcommand(ns, Path("/x"), _handlers(calls)) is None  # type: ignore[arg-type]
    assert calls == []


def test_subcommand_runs_selected_handler() -> None:
    calls: list[str] = []
    ns = build_arg_parser().parse_args(["--phase-c-only"])
    rc = run_phase_subcommand(ns, Path("/x"), _handlers(calls))  # type: ignore[arg-type]
    assert rc == 7 and calls == ["phase_c_only"]


def test_subcommand_dispatch_order_a_first() -> None:
    calls: list[str] = []
    ns = build_arg_parser().parse_args(["--phase-a-only", "--phase-d-only"])
    run_phase_subcommand(ns, Path("/x"), _handlers(calls))  # type: ignore[arg-type]
    assert calls == ["phase_a_only"]  # a dispatched before d
