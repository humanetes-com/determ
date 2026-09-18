"""build_arg_parser: the CLI surface extracted from main()."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from pipeline_core.application.orchestrator import (
    LockDeps,
    build_arg_parser,
    filter_module_test_files,
    lock_or_complete,
    module_descriptor,
    next_module,
    run_phase_subcommand,
    with_example_usage_signal,
)
from pipeline_core.domain.stub_inventory import StubInventory


def _lock_deps(log: list[str]) -> tuple[LockDeps, list[str]]:
    events: list[str] = []

    def exit_(code: int) -> None:
        raise SystemExit(code)

    deps = LockDeps(
        commit_and_mark=lambda *a, **k: events.append(f"commit_and_mark:{k.get('commit_message')}"),
        set_current=lambda _pd, label: events.append(f"set_current:{label}"),
        lock_module=lambda *a: events.append("lock_module"),
        git_commit=lambda _pd, msg: events.append(f"git_commit:{msg}") or True,
        log=lambda m: log.append(m),
        sys_exit=exit_,
        scan_unimplemented_functions=lambda *a, **k: StubInventory(),
    )
    return deps, events


def test_lock_or_complete_multi_module_locks_no_exit() -> None:
    log: list[str] = []
    deps, events = _lock_deps(log)
    locked: set[str] = set()
    lock_or_complete(
        Path("/p"), "mymod", Path("/p/mymod"), Path("/p/.gen"),
        is_multi_module=True, locked_modules=locked, src_hash="h",
        commit_message="cm", detail="d", set_current_label="scl",
        locked_commit_msg="locked-mymod", success_log="SUCCESS",
        tag="t", complete_commit_msg="complete", deps=deps,
    )
    assert "mymod" in locked
    assert "lock_module" in events
    assert "git_commit:locked-mymod" in events
    assert "SUCCESS" not in log  # success/exit path not taken in multi-module


def test_lock_or_complete_single_module_exits() -> None:
    log: list[str] = []
    deps, events = _lock_deps(log)
    with pytest.raises(SystemExit) as exc:
        lock_or_complete(
            Path("/p"), "mymod", Path("/p"), Path("/p/.gen"),
            is_multi_module=False, locked_modules=set(), src_hash="h",
            commit_message="cm", detail="d", set_current_label="scl",
            locked_commit_msg="locked", success_log="SUCCESS-DONE",
            complete_commit_msg="pipeline: complete", deps=deps,
        )
    assert exc.value.code == 0
    assert "SUCCESS-DONE" in log
    assert "git_commit:pipeline: complete" in events
    assert "lock_module" not in events


def test_lock_or_complete_single_module_no_complete_commit() -> None:
    log: list[str] = []
    deps, events = _lock_deps(log)
    with pytest.raises(SystemExit):
        lock_or_complete(
            Path("/p"), "m", Path("/p"), Path("/p/.gen"),
            is_multi_module=False, locked_modules=set(), src_hash=None,
            commit_message="cm", detail="d", set_current_label="scl",
            locked_commit_msg="locked", success_log="ok",
            deps=deps,  # complete_commit_msg defaults to None
        )
    assert not any(e.startswith("git_commit:") for e in events)  # no completion commit


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


class _FakeGraph:
    def crate_dir(self, m: str) -> Path:
        return Path(f"/ws/{m}")

    def crate_type(self, m: str) -> str:
        return "bin"

    def workspace_deps(self, m: str) -> set[str]:
        return {"dep1"}

    def module_test_command(self, test_command: str, m: str) -> str:
        return f"{test_command} -p {m}"


def test_module_descriptor_single_module_shortcircuits() -> None:
    d = module_descriptor("core", dep_graph=_FakeGraph(), is_multi_module=False,
                          project_dir=Path("/proj"), test_command="cargo test")
    assert d.module_dir == Path("/proj")          # project root, no graph
    assert d.crate_type == "lib"
    assert d.workspace_deps == set()
    assert d.module_test_cmd == "cargo test"
    assert d.module_dir_rel == ""                 # same as project root
    assert d.module_manifest == Path("/proj/.generated_files_core.txt")


def test_module_descriptor_multi_module_uses_graph() -> None:
    d = module_descriptor("alpha", dep_graph=_FakeGraph(), is_multi_module=True,
                          project_dir=Path("/ws"), test_command="cargo test")
    assert d.module_dir == Path("/ws/alpha")
    assert d.crate_type == "bin"
    assert d.workspace_deps == {"dep1"}
    assert d.module_test_cmd == "cargo test -p alpha"
    assert d.module_dir_rel == "alpha/"           # relative + trailing slash


def test_filter_module_test_files() -> None:
    files = [("src/a/x.rs", "A"), ("src/b/y.rs", "B"), ("src/a/z.rs", "C")]
    assert filter_module_test_files(files, "src/a/") == [("src/a/x.rs", "A"), ("src/a/z.rs", "C")]
    assert filter_module_test_files(files, "") == files          # root/single-module → all
    assert filter_module_test_files(files, "src/none/") == []


def test_with_example_usage_signal() -> None:
    base = [("tests/t.rs", "T")]
    assert with_example_usage_signal(base, []) is base          # no examples → unchanged
    out = with_example_usage_signal(base, [("examples/e.rs", "E")])
    assert out[0] == ("tests/t.rs", "T")                        # base preserved, first
    rel, content = out[1]
    assert rel == "examples/e.rs"
    assert content.endswith("E") and "NOT a test" in content    # header prepended


def test_next_module_highest_coverage_unlocked() -> None:
    mods = ["a", "b", "c"]
    cov = {"a": 50.0, "b": 90.0, "c": 70.0}
    assert next_module(mods, set(), cov) == "b"                 # highest
    assert next_module(mods, {"b"}, cov) == "c"                 # b locked → next highest
    assert next_module(mods, {"a", "b", "c"}, cov) is None      # all locked


def test_next_module_missing_coverage_is_zero_and_stable_ties() -> None:
    assert next_module(["x", "y"], set(), {}) == "x"            # all 0.0 → first in order
    assert next_module(["x", "y"], set(), {"y": 0.0, "x": 0.0}) == "x"
