"""Pins for the P1 FixLoop port of task0c's two hand-rolled loops (harness
fix + gen_tests block fix) onto the shared `drive_fix_loop` driver.

The headline property is TERMINATION: the old `while True` harness loop only
exited on identical error signatures, so signatures that kept CHANGING while
never resolving spun forever. On the driver, floor-stall (default 3) and the
runaway backstop bound every path. The escalation ladder (targeted strip →
full strip → git-restore probe) is preserved on exhaustion.

Driven through `task0c_augment_module` with a fakes `Task0cDeps` (the
established slice-5b pattern); a sentinel exception in `read_state` marks
"harness section completed, augmentation began" for the heal path.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline_core.application.tasks.task0c import Task0cDeps, task0c_augment_module


class _Stop(Exception):
    """Sentinel: execution passed the harness gate into augmentation."""


class _Run:
    def __init__(self, rc: int = 1):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text("mod gen_tests_1;\n")
    return ws


def _deps(**over: Any) -> Task0cDeps:
    stub: dict[str, Any] = {f: (lambda *a, **k: None) for f in Task0cDeps.__dataclass_fields__}
    stub["get_current_cluster"] = lambda: None
    stub["cargo_env"] = lambda: {}
    stub["match_gen_tests_sibling"] = lambda name: None
    stub["strip_all_gen_tests_artifacts"] = lambda p: 0
    stub["is_test_file"] = lambda p, root: False
    stub["extract_files"] = lambda r: {}
    stub["subprocess"] = SimpleNamespace(run=lambda *a, **k: _Run(1))
    stub.update(over)
    return Task0cDeps(**stub)


def _augmentor(harness: Any) -> Any:
    """harness: callable returning (ok, err) per test_harness_compiles call."""
    return SimpleNamespace(
        is_applicable=lambda ct: True,
        lib_compiles=lambda p, m: (True, ""),
        test_harness_compiles=harness,
    )


def test_harness_fix_changing_signatures_terminates(tmp_path: Path) -> None:
    """THE P1 pin: ever-changing, never-resolving signatures bounded by
    floor-stall instead of looping forever; exhaustion runs the strip ladder
    and returns the blocked verdict."""
    ws = _ws(tmp_path)
    calls = {"ask": 0, "sig": 0}

    def err_sigs(out: str) -> frozenset[str]:
        calls["sig"] += 1
        return frozenset({f"E{calls['sig']:04}"})   # distinct every time, size 1

    def ask(prompt: str, **k: Any) -> str:
        calls["ask"] += 1
        return ""

    deps = _deps(
        test_augmentor_factory=lambda: _augmentor(
            lambda p, m: (False, "error: x\n --> src/lib.rs:1:1")),
        error_signatures_from_output=err_sigs,
        ask=ask,
    )
    res = task0c_augment_module(ws, "m", ws, deps, crate_type="lib")
    assert res == (0.0, 0, False, "harness_broken_after_strip")
    # Floor never improves (constant count 1, fresh sigs) → 1 baseline + 3
    # stalls = 4 LLM passes, then the ladder. The old loop never exited here.
    assert calls["ask"] == 4


def test_harness_fix_identical_signatures_escalates_to_strip(tmp_path: Path) -> None:
    """Identical signatures on consecutive attempts → driver `repeated` exit
    after attempt 2 (the old loop's same-sig escalation), then the ladder."""
    ws = _ws(tmp_path)
    calls = {"ask": 0}

    def ask(prompt: str, **k: Any) -> str:
        calls["ask"] += 1
        return ""

    deps = _deps(
        test_augmentor_factory=lambda: _augmentor(
            lambda p, m: (False, "error: x\n --> src/lib.rs:1:1")),
        error_signatures_from_output=lambda out: frozenset({"E0308 same"}),
        ask=ask,
    )
    res = task0c_augment_module(ws, "m", ws, deps, crate_type="lib")
    assert res == (0.0, 0, False, "harness_broken_after_strip")
    assert calls["ask"] == 2


def test_harness_fix_unidentified_file_stops(tmp_path: Path) -> None:
    """No broken file identifiable from the error → 'stop' outcome, blocked
    verdict, zero prompts (the old early-return path)."""
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text("pub fn f() {}\n")  # no cfg(test), not in err
    calls = {"ask": 0}

    def ask(prompt: str, **k: Any) -> str:
        calls["ask"] += 1
        return ""

    deps = _deps(
        test_augmentor_factory=lambda: _augmentor(
            lambda p, m: (False, "error: y\n --> nothing/matches.rs:1:1")),
        error_signatures_from_output=lambda out: frozenset({"E"}),
        ask=ask,
    )
    res = task0c_augment_module(ws, "m", ws, deps, crate_type="lib")
    assert res == (0.0, 0, False, "harness_broken_file_unidentified")
    assert calls["ask"] == 0


def test_harness_fix_llm_heals_and_augmentation_proceeds(tmp_path: Path) -> None:
    """Broken harness healed by the LLM's file fix on attempt 1: the loop
    accepts, the harness gate opens, and execution proceeds into augmentation
    (marked by the read_state sentinel)."""
    ws = _ws(tmp_path)
    checks = {"n": 0, "ask": 0}

    def harness(p: Path, m: str) -> tuple[bool, str]:
        checks["n"] += 1
        # 1st: broken (enter loop); 2nd: healed (post-LLM recheck);
        # 3rd: precheck before pre-clean.
        return (checks["n"] >= 2), ("" if checks["n"] >= 2 else "error: z\n --> src/lib.rs:1:1")

    def ask(prompt: str, **k: Any) -> str:
        checks["ask"] += 1
        return "<FILE>fixed</FILE>"

    def read_state(p: Path) -> dict[str, Any]:
        raise _Stop()

    deps = _deps(
        test_augmentor_factory=lambda: _augmentor(harness),
        error_signatures_from_output=lambda out: frozenset({"E"}),
        ask=ask,
        extract_files=lambda r: {"src/lib.rs": "pub fn f() {}\n"},
        write=lambda p, c: Path(p).write_text(c),
        derive_gentest_prelude=lambda p: ("", ""),
        prelude_import_names=lambda b: "",
        read_state=read_state,
    )
    with pytest.raises(_Stop):
        task0c_augment_module(ws, "m", ws, deps, crate_type="lib")
    assert checks["ask"] == 1          # one LLM fix pass healed it
    assert checks["n"] == 3            # broken → healed → precheck
