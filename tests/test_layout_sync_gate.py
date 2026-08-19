"""The layout registry is crate-wide and permanent; its reinstall gets a build.

On 2026-08-01 two retriggers fired with `cause=DESIGN_CONFLICT` on rustc errors
that came from rewritten test fixtures. Their re-authored definitions survived
the run, `layout-sync` reinstalled them on the next start, the build broke, and
`pre-pass-heal` reverted 41 fns — 93 passing to 31. The tree can be restored
from a snapshot; the registry cannot.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.generators.task2_generator import (
    StaticSkeletonTask2Generator as _Gen,
)


@dataclass
class _Tel:
    lines: list[str]

    def log(self, m: str) -> None:
        self.lines.append(m)

    def emit(self, *_a: Any, **_k: Any) -> None:
        pass

    def check_stop(self, *_a: Any) -> None:
        pass


@dataclass
class _Diag:
    file: str


class _Cargo:
    """Scripted `cargo check`: returns each queued rc in turn."""

    def __init__(self, rcs: list[int],
                 diags: list[Any] | None = None,
                 implicated: list[Any] | None = None) -> None:
        self.rcs = rcs
        self.calls = 0
        self.diags = diags or []
        self.implicated = implicated or []

    def env(self) -> dict[str, str]:
        return {}

    def run_check_diag(self, *_a: Any, **_k: Any) -> tuple[int, str, list[Any]]:
        rc = self.rcs[min(self.calls, len(self.rcs) - 1)]
        self.calls += 1
        return rc, ("error[E0560]: struct has no field named `x`"
                    if rc else ""), (self.diags if rc else [])

    def implicated_fns_from_check(self, *_a: Any, **_k: Any) -> list[Any]:
        return list(self.implicated)


class _GenTests:
    def is_gen_tests_file(self, name: str) -> bool:
        return ".gen_tests_" in name


class _Cluster:
    name = "stable"
    toolchain = None
    env_extra: dict[str, str] = {}


def _gen(cargo: _Cargo, tel: _Tel, plan_file: str) -> _Gen:
    def _load_surface(_p: Path, _c: str) -> Any:
        return object()

    def _load_records(_p: Path, _c: str) -> list[Any]:
        return []

    def _apply(plan: dict[str, list[Any]], src: Path) -> tuple[int, str]:
        for rel in plan:
            (src / rel).write_text("REINSTALLED\n", encoding="utf-8")
        return len(plan), ""

    gen = _Gen.__new__(_Gen)
    object.__setattr__(gen, "tel", tel)
    object.__setattr__(gen, "cargo", cargo)
    object.__setattr__(gen, "layout_sync", (_load_surface, _load_records, _apply))
    object.__setattr__(gen, "layout_registry", None)
    object.__setattr__(gen, "gentests", _GenTests())
    # `plan_layout_reconciliation` is imported into the module under test; the
    # scripted plan is injected by patching it there in the test body.
    return gen


def _run(monkeypatch, rcs: list[int], tmp_path: Path,
         cargo: _Cargo | None = None) -> tuple[_Tel, Path]:
    import pipeline_core.application.generators.task2_generator as mod

    src = tmp_path / "filled" / "src"
    src.mkdir(parents=True)
    target = src / "error.rs"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    monkeypatch.setattr(
        mod, "plan_layout_reconciliation",
        lambda *_a, **_k: ({"error.rs": ["def"]}, []))
    tel = _Tel([])
    gen = _gen(cargo or _Cargo(rcs), tel, "error.rs")
    gen._sync_layout_into_tree("[tag]", tmp_path, _Cluster(), tmp_path / "filled")
    return tel, target


def test_a_reinstall_that_breaks_a_compiling_tree_is_reverted(monkeypatch, tmp_path):
    tel, target = _run(monkeypatch, [0, 101], tmp_path)
    assert target.read_text() == "ORIGINAL\n", \
        "the registry's definition was left in a tree it cannot build"
    assert any("REVERTED" in m for m in tel.lines)


def test_a_good_reinstall_stands(monkeypatch, tmp_path):
    tel, target = _run(monkeypatch, [0, 0], tmp_path)
    assert target.read_text() == "REINSTALLED\n"
    assert not any("REVERTED" in m for m in tel.lines)


def test_an_already_broken_tree_is_not_held_against_the_reinstall(monkeypatch, tmp_path):
    """Repairing a tree that was already failing is what this step is FOR."""
    tel, target = _run(monkeypatch, [101, 101], tmp_path)
    assert target.read_text() == "REINSTALLED\n"
    assert not any("REVERTED" in m for m in tel.lines)


def test_a_red_path_reinstall_travels_with_its_restub(monkeypatch, tmp_path):
    """The 2026-08-19 wreck: on an already-red tree the reinstall stood ALONE
    — the adopted definitions replaced what bodies elsewhere were written
    against, the wreckage surfaced as errors resolving to no fn, pre-pass-heal
    reverted 0 in 3 rounds, and `nightly` closed with zero scored sweeps (run
    `3579062-476abc3b`). Destroy-then-restub is a pair: the sync itself must
    re-stub what the compiler implicates against its reinstall."""
    from pipeline_core.domain.models import FnLocator

    calls: list[list[Any]] = []

    def _fake_revert(self, tag, implicated, filled_dir, skeleton_dir,
                     project_dir=None, cluster_name=""):
        calls.append(list(implicated))
        return len(implicated)

    monkeypatch.setattr(_Gen, "_revert_implicated", _fake_revert)
    cargo = _Cargo([101, 101, 0],
                   implicated=[FnLocator("src/backtrace.rs", "capture")])
    tel, target = _run(monkeypatch, [], tmp_path, cargo=cargo)
    assert target.read_text() == "REINSTALLED\n", \
        "the registry's definition must stand — the bodies answer to it"
    assert calls and calls[0] == [FnLocator("src/backtrace.rs", "capture")], \
        "the reinstall was not paired with a re-stub of what it broke"
    assert any("re-stubbed" in m for m in tel.lines)
    assert any("compiles after" in m for m in tel.lines)


def test_red_path_errors_outside_src_are_left_to_the_oracle(monkeypatch, tmp_path):
    """Unresolved-import cascades in tests/*.rs are the oracle plus fallout of
    a lib that does not build — healing them repairs nothing (measured: the
    overnight rounds targeted only test files and reverted 0). The sync must
    not touch them, and must say what it left behind."""
    def _boom(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("no src/ target — nothing may be reverted")

    monkeypatch.setattr(_Gen, "_revert_implicated", _boom)
    cargo = _Cargo([101, 101],
                   diags=[_Diag("tests/test_source.rs"),
                          _Diag("tests/test_chain.rs"),
                          _Diag("src/error.gen_tests_1.rs")])
    tel, target = _run(monkeypatch, [], tmp_path, cargo=cargo)
    assert target.read_text() == "REINSTALLED\n"
    assert any("no error lands on repairable src/" in m for m in tel.lines)
