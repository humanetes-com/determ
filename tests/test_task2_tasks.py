"""Fakes-driven tests for the TASK-2 orchestration (tasks/task2.py).

task2/task2_inner_loop/task2_fix_loop are thin orchestration over injected collaborators
(generator, manifest loader, generate_fix_loop, error counters). These drive the
build-check retry loop's success / floor-stall / scope-correction / proc-macro branches and
the post-task3 fix-loop's context assembly — no LLM, no cargo.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.application.tasks.task2 import (
    Task2Deps,
    task2_fix_loop,
    task2_inner_loop,
)


class _Gen:
    """Fake Task2 generator: .generate(req) returns a queued/constant value and records reqs."""

    def __init__(self, results: list[bool] | bool) -> None:
        self._results = results
        self.reqs: list[Any] = []

    def generate(self, req: Any) -> bool:
        self.reqs.append(req)
        if isinstance(self._results, bool):
            return self._results
        return self._results[min(len(self.reqs) - 1, len(self._results) - 1)]


def _deps(gen: _Gen, *, fix_loop: Any, **over: Any) -> Task2Deps:
    base = Task2Deps(
        generator_factory=lambda: gen,
        load_manifest=lambda _p: [],
        read=lambda _p: "",
        generate_fix_loop=fix_loop,
        store_prompt_content=lambda s: s,
        is_proc_macro_crate=lambda _t: False,
        count_error_codes=lambda _o: 0,
        build_check_retries=3,
        build_max_stalls=2,
        build_timeout=1,
        test_timeout=1,
        task2_model="m",
        task2_fix_retries=2,
        single_call_char_limit=120_000,
        genesis_cap=3000,
    )
    return replace(base, **over)


_COMMON = dict(  # noqa: C408 — explicit kwargs for the inner-loop call
    genesis_path=Path("g"), functional_req_path=Path("fr"), manifest_path=Path("man"),
    existing_test_files=set(), project_profile="", workspace_structure="",
)


# --------------------------------------------------------------------------- inner loop

def test_inner_loop_build_check_passes(tmp_path: Path) -> None:
    gen = _Gen(True)
    captured: dict[str, Any] = {}

    def fix_loop(**kw: Any) -> tuple[bool, str]:
        captured.update(kw)
        return True, ""

    ok = task2_inner_loop(tmp_path, **_COMMON, build_check_cmd="cargo build",
                          deps=_deps(gen, fix_loop=fix_loop), current_module="m")
    assert ok is True
    assert len(gen.reqs) == 1                       # one generate, then build passed
    assert captured["check_cmd"] == "cargo build"   # fix-loop wired to the build check


def test_inner_loop_no_build_check_returns_after_generate(tmp_path: Path) -> None:
    gen = _Gen(True)
    ok = task2_inner_loop(tmp_path, **_COMMON, build_check_cmd=None,
                          deps=_deps(gen, fix_loop=lambda **k: (True, "")), current_module="m")
    assert ok is True and len(gen.reqs) == 1


def test_inner_loop_floor_stall_escalates(tmp_path: Path) -> None:
    """Build never compiles and the error count never improves → floor-stall breaks
    out early (before exhausting all retries)."""
    gen = _Gen(True)
    deps = _deps(gen, fix_loop=lambda **k: (False, "error[E0001]: boom"),
                 count_error_codes=lambda _o: 5, build_max_stalls=1, build_check_retries=5)
    ok = task2_inner_loop(tmp_path, **_COMMON, build_check_cmd="cargo build",
                          deps=deps, current_module="m")
    assert ok is False
    # attempt1 sets best=5 (stall 0); attempt2 stalls (5 not < 5) → break. Not all 5 used.
    assert len(gen.reqs) == 2


def test_inner_loop_scope_correction_then_abort(tmp_path: Path) -> None:
    """No in-scope files: the first miss injects a scope-correction retry, the second
    miss aborts (covers the scope-error branch + no_files_generated)."""
    gen = _Gen(False)  # generate always reports no files
    ok = task2_inner_loop(tmp_path, **_COMMON, build_check_cmd="cargo build",
                          deps=_deps(gen, fix_loop=lambda **k: (True, ""), build_check_retries=2),
                          current_module="cratea", module_dir_rel="cratea/")
    assert ok is False
    assert len(gen.reqs) == 2                                    # initial + one scope-corrected retry
    assert "SCOPE ERROR" in gen.reqs[1].build_error              # correction fed back in


def test_inner_loop_detects_proc_macro_and_passes_constraint(tmp_path: Path) -> None:
    """A proc-macro Cargo.toml under module_dir_rel injects the top-level constraint
    into the fix loop's extra_context."""
    (tmp_path / "cratea").mkdir()
    (tmp_path / "cratea" / "Cargo.toml").write_text("[lib]\nproc-macro = true\n")
    gen = _Gen(True)
    captured: dict[str, Any] = {}

    def fix_loop(**kw: Any) -> tuple[bool, str]:
        captured.update(kw)
        return True, ""

    ok = task2_inner_loop(tmp_path, **_COMMON, build_check_cmd="cargo build",
                          deps=_deps(gen, fix_loop=fix_loop, is_proc_macro_crate=lambda _t: True),
                          current_module="cratea", module_dir_rel="cratea/")
    assert ok is True
    assert "PROC-MACRO" in captured["extra_context"]


# --------------------------------------------------------------------------- fix loop

def test_fix_loop_empty_manifest_short_circuits(tmp_path: Path) -> None:
    deps = _deps(_Gen(True), fix_loop=lambda **k: (True, ""), load_manifest=lambda _p: [])
    ok, out = task2_fix_loop(tmp_path, Path("man"), set(), "TESTOUT", "cargo test",
                             Path("fr"), deps)
    assert ok is False and out == "TESTOUT"  # nothing to fix → returns the input unchanged


def test_fix_loop_assembles_genesis_and_tests(tmp_path: Path) -> None:
    """With a manifest, genesis, and a test file present, the fix loop builds the
    genesis + TESTFILE extra-context and delegates to generate_fix_loop."""
    (tmp_path / "cratea" / "src").mkdir(parents=True)
    (tmp_path / "cratea" / "src" / "lib.rs").write_text("fn x() {}")
    test_rel = "cratea/tests/t.rs"
    (tmp_path / "cratea" / "tests").mkdir(parents=True)
    (tmp_path / test_rel).write_text("#[test] fn t() {}")
    genesis = tmp_path / "genesis.md"
    genesis.write_text("METHODOLOGY: validate inputs")
    fr = tmp_path / "fr.md"
    fr.write_text("FR body")

    captured: dict[str, Any] = {}

    def fix_loop(**kw: Any) -> tuple[bool, str]:
        captured.update(kw)
        return True, "fixed"

    deps = _deps(_Gen(True), fix_loop=fix_loop,
                 load_manifest=lambda _p: ["cratea/src/lib.rs"],
                 read=lambda p: genesis.read_text() if Path(p) == genesis else fr.read_text())
    ok, out = task2_fix_loop(tmp_path, Path("man"), {test_rel}, "TESTOUT",
                             "cargo test", fr, deps, genesis_path=genesis,
                             module_dir_rel="cratea/")
    assert (ok, out) == (True, "fixed")
    extra = captured["extra_context"]
    assert "METHODOLOGY: validate inputs" in extra        # genesis section
    assert f'<TESTFILE path="{test_rel}">' in extra        # test section
    assert captured["check_cmd"] == "cargo test"
    # source_paths excludes the test file, includes the manifest src file.
    assert tmp_path / "cratea/src/lib.rs" in captured["source_paths"]


def test_fix_loop_uses_cluster_check_factory_when_present(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn x() {}")
    sentinel = object()
    captured: dict[str, Any] = {}

    def fix_loop(**kw: Any) -> tuple[bool, str]:
        captured.update(kw)
        return True, ""

    deps = _deps(_Gen(True), fix_loop=fix_loop,
                 load_manifest=lambda _p: ["src/lib.rs"],
                 cluster_check_factory=lambda _pd: sentinel)  # type: ignore[arg-type]
    task2_fix_loop(tmp_path, Path("man"), set(), "out", "cargo test", tmp_path / "fr", deps)
    assert captured["check_fn"] is sentinel
