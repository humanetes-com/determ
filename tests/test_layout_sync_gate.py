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

    def cluster_env(self, cluster: Any) -> dict[str, str]:
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
    src.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# The registry can be the stale side, and repeated green-tree rollbacks are
# the measurement that says so. 2026-08-19, run entry 19:38:42: 7 authored
# definitions reinstalled into 4 live files and rolled back on `E0308
# src/error.rs:220` (ErrorImpl field-set mismatch) plus an E0277 — repeating
# at EVERY entry until registry and tree re-converge. The green path's cost
# is two cargo checks; the RED path's is not: kept-by-rule on a
# red-at-entry tree, the divergent registry's reinstall hands
# `_restub_after_reinstall` the live fills to re-stub toward a layout the
# green entries already proved unbuildable.
# ---------------------------------------------------------------------------


def _streak_file(tmp_path: Path) -> Path:
    return tmp_path / ".skeleton" / "_layout_sync_streak_stable.json"


def test_repeated_green_rollbacks_stop_the_readoption(monkeypatch, tmp_path):
    # Two consecutive green-tree rollbacks: the registry is measured stale.
    for _ in range(2):
        tel, target = _run(monkeypatch, [0, 101], tmp_path)
        assert target.read_text() == "ORIGINAL\n"
    assert _streak_file(tmp_path).exists()
    # Third entry: the sync must not even TRY — no cargo check, no apply.
    cargo = _Cargo([0, 101])
    tel, target = _run(monkeypatch, [], tmp_path, cargo=cargo)
    assert cargo.calls == 0, "a stale registry must not buy two more checks"
    assert target.read_text() == "ORIGINAL\n"
    assert any("NOT re-adopting the registry" in m for m in tel.lines)


def test_a_landed_reinstall_clears_the_streak(monkeypatch, tmp_path):
    tel, _ = _run(monkeypatch, [0, 101], tmp_path)     # one rollback
    tel, target = _run(monkeypatch, [0, 0], tmp_path)  # then it LANDS
    assert target.read_text() == "REINSTALLED\n"
    # The streak restarted from zero: one more rollback is below the
    # threshold, so the entry after it still re-adopts.
    tel, _ = _run(monkeypatch, [0, 101], tmp_path)
    cargo = _Cargo([0, 0])
    tel, target = _run(monkeypatch, [], tmp_path, cargo=cargo)
    assert cargo.calls > 0 and target.read_text() == "REINSTALLED\n"
    assert not any("NOT re-adopting" in m for m in tel.lines)


def test_a_stale_registry_never_reaches_the_red_path_restub(monkeypatch, tmp_path):
    """THE HAZARD THE GUARD EXISTS FOR. On a red-at-entry tree the reinstall
    is kept by rule and its re-stub wave follows — correct when the tree's
    bodies answer to the registry, a whole-run cost when the green entries
    already measured the registry unbuildable. With the streak standing, a
    red entry must skip the reinstall entirely: no adoption, no re-stub."""
    for _ in range(2):
        _run(monkeypatch, [0, 101], tmp_path)

    def _boom(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("stale registry: the re-stub wave must not fire")

    monkeypatch.setattr(_Gen, "_restub_after_reinstall", _boom)
    cargo = _Cargo([101, 101, 0])
    tel, target = _run(monkeypatch, [], tmp_path, cargo=cargo)
    assert target.read_text() == "ORIGINAL\n", \
        "a stale registry was re-adopted on a red tree"
    assert any("NOT re-adopting the registry" in m for m in tel.lines)


def test_convergence_clears_the_streak(monkeypatch, tmp_path):
    import json as _json

    import pipeline_core.application.generators.task2_generator as mod
    for _ in range(2):
        _run(monkeypatch, [0, 101], tmp_path)
    assert _json.loads(_streak_file(tmp_path).read_text())["green_reverts"] == 2
    # An entry whose plan is EMPTY means registry and tree agree again — the
    # divergence the streak measured is over.
    tel = _Tel([])
    gen = _gen(_Cargo([0, 0]), tel, "error.rs")
    monkeypatch.setattr(mod, "plan_layout_reconciliation",
                        lambda *_a, **_k: ({}, []))
    gen._sync_layout_into_tree("[tag]", tmp_path, _Cluster(), tmp_path / "filled")
    assert _json.loads(_streak_file(tmp_path).read_text())["green_reverts"] == 0


def test_staleness_threshold_zero_disables_the_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("LAYOUT_SYNC_STALE_AFTER", "0")
    for _ in range(3):
        _run(monkeypatch, [0, 101], tmp_path)
    cargo = _Cargo([0, 0])
    tel, target = _run(monkeypatch, [], tmp_path, cargo=cargo)
    assert target.read_text() == "REINSTALLED\n"
    assert not any("NOT re-adopting" in m for m in tel.lines)


def test_reinstall_streak_state_is_total():
    from pipeline_core.domain.layout_sync_staleness import ReinstallStreak

    assert ReinstallStreak.from_dict(None) == ReinstallStreak()
    assert ReinstallStreak.from_dict({"green_reverts": "x"}) == ReinstallStreak()
    assert ReinstallStreak.from_dict({"green_reverts": -3}) == ReinstallStreak()
    s = ReinstallStreak.from_dict(
        ReinstallStreak().reverted_on_green().reverted_on_green().to_dict())
    assert s.green_reverts == 2 and s.stale() and not s.stale(threshold=0)
    assert not ReinstallStreak(1).stale()
    assert s.cleared().green_reverts == 0
