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


class _Cargo:
    """Scripted `cargo check`: returns each queued rc in turn."""

    def __init__(self, rcs: list[int]) -> None:
        self.rcs = rcs
        self.calls = 0

    def env(self) -> dict[str, str]:
        return {}

    def run_check_diag(self, *_a: Any, **_k: Any) -> tuple[int, str, list[Any]]:
        rc = self.rcs[min(self.calls, len(self.rcs) - 1)]
        self.calls += 1
        return rc, ("error[E0560]: struct has no field named `x`"
                    if rc else ""), []


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
    # `plan_layout_reconciliation` is imported into the module under test; the
    # scripted plan is injected by patching it there in the test body.
    return gen


def _run(monkeypatch, rcs: list[int], tmp_path: Path) -> tuple[_Tel, Path]:
    import pipeline_core.application.generators.task2_generator as mod

    src = tmp_path / "filled" / "src"
    src.mkdir(parents=True)
    target = src / "error.rs"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    monkeypatch.setattr(
        mod, "plan_layout_reconciliation",
        lambda *_a, **_k: ({"error.rs": ["def"]}, []))
    tel = _Tel([])
    gen = _gen(_Cargo(rcs), tel, "error.rs")
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
