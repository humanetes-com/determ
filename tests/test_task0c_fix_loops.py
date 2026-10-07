"""Pins for the FixLoop port of task0c's two hand-rolled loops (harness
fix + gen_tests block fix) onto the shared `drive_fix_loop` driver.

The headline property is TERMINATION: the old `while True` harness loop only
exited on identical error signatures, so signatures that kept CHANGING while
never resolving spun forever. On the driver, floor-stall (default 3) and the
runaway backstop bound every path. The escalation ladder (targeted strip →
full strip → git-restore probe) is preserved on exhaustion.

Driven through `task0c_augment_module` with a fakes `Task0cDeps` (the
established pattern); a sentinel exception in `read_state` marks
"harness section completed, augmentation began" for the heal path.
"""
from __future__ import annotations

import json
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
    """Ever-changing, never-resolving signatures bounded by
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


def test_engine_get_history_formats_jsonl(tmp_path: Path) -> None:
    """Relocated `_engine_get_history` reads the per-project JSONL attempt
    log and renders success (committed) vs failure entries, capping the stored
    compile error at deps.FIX_ERR_CAP from the front (tail kept)."""
    from pipeline_core.application.tasks.task0c import (
        TASK0C_ATTEMPTS_LOG,
        _engine_get_history,
    )

    log = tmp_path / TASK0C_ATTEMPTS_LOG
    rows = [
        {"file": "src/a.rs", "attempt": 1, "block_preview": "fn t();",
         "compile_error": "X" * 50 + "TAILERR", "outcome": "compile_error"},
        {"file": "src/a.rs", "attempt": 2, "block_preview": "", "compile_error": "",
         "outcome": "success"},
        {"file": "src/other.rs", "attempt": 1, "block_preview": "z",
         "compile_error": "nope", "outcome": "compile_error"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    deps = SimpleNamespace(FIX_ERR_CAP=10)
    out = _engine_get_history(deps, tmp_path, "src/a.rs")

    assert "Attempt history for `src/a.rs` (2 attempts)" in out
    assert "COMMITTED TO DISK" in out          # success label
    assert "FAILED (compile_error)" in out     # failure label
    assert "TAILERR" in out and "X" * 11 not in out  # err capped to last 10 chars
    assert "src/other.rs" not in out           # filtered by rel_path

    # No log file → empty string.
    assert _engine_get_history(deps, tmp_path / "none", "src/a.rs") == ""


def test_engine_get_history_imported_not_injected() -> None:
    """get_task0c_history is no longer a Task0cDeps field
    (the helper now lives in the module, not injected back)."""
    assert "get_task0c_history" not in Task0cDeps.__dataclass_fields__


def test_no_model_verdict_can_write_a_pragma() -> None:
    """Both writers that let a model's "unreachable" exclude lines from the
    line reading are gone: the stall pass that pragma'd whatever the model
    listed, and the retrospective's targeted pass that pragma'd anyhow's
    `format_err` `else` arm, which `anyhow!("x {var}")` executes. A line leaves
    the reading only by the compiler's proof or the artifact/cfg filters."""
    from pipeline_core.application.tasks import task0c
    assert not hasattr(task0c, "_engine_pragma_pass")
    assert not hasattr(task0c, "_engine_targeted_pragma")
    assert "coverage_ignore_pragma" not in Task0cDeps.__dataclass_fields__


def test_looks_like_fn_decl_matches_old_regex() -> None:
    """The regex-free fn-decl detector matches the old
    re.match(r'\\s*(pub\\s+)?fn\\s+', ln) it replaced, across edge cases."""
    import re as _re

    from pipeline_core.application.tasks.task0c import _looks_like_fn_decl
    old = _re.compile(r'\s*(pub\s+)?fn\s+')
    cases = [
        "fn foo()", "pub fn foo()", "  fn bar() {", "    pub fn baz<T>() {",
        "fn", "pub fn", "fnfoo()", "pubfn foo()", "xpub fn y()", "", "   ",
        "pub  fn  spaced()", "struct S", "// fn comment", "let fn_x = 1;",
        "pub(crate) fn q()",
    ]
    for c in cases:
        assert _looks_like_fn_decl(c) == bool(old.match(c)), c


def test_engine_mutation_pass_persists_hardening(tmp_path: Path, monkeypatch) -> None:
    """Relocated `_engine_mutation_pass` is opt-in (env-gated) and, when on,
    persists generated hardening tests as `tests/{stem}_gen_N.rs`."""
    from pipeline_core.application.tasks.task0c import _engine_mutation_pass

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    f = src_dir / "x.rs"
    f.write_text("pub fn a() {}\n")
    dest = tmp_path / "tests" / "x_gen_1.rs"

    deps = SimpleNamespace(
        mutation_harden_file=lambda pd, mn, cl, rel: "    #[test]\n    fn k() {}\n",
        gen_tests_siblings=lambda src: [],
        gen_tests_path=lambda src, n: src.parent / f"{src.stem}.gen_tests_{n}.rs",
    )

    # Off by default → no work.
    monkeypatch.delenv("TASK0C_MUTATION_HARDEN", raising=False)
    assert _engine_mutation_pass(deps, tmp_path, "x", None, ["src/x.rs"]) == 0
    assert not dest.exists()

    # On → hardens and writes the integration file, not a src sibling.
    monkeypatch.setenv("TASK0C_MUTATION_HARDEN", "1")
    assert _engine_mutation_pass(deps, tmp_path, "x", None, ["src/x.rs"]) == 1
    assert dest.exists() and "fn k()" in dest.read_text()
    assert "use x::*;" in dest.read_text()
    assert not (src_dir / "x.gen_tests_1.rs").exists()


def test_engine_log_attempt_writes_jsonl_and_type_signals(tmp_path: Path) -> None:
    """Relocated `_engine_log_attempt` appends a JSONL record and, on a
    compile_error, feeds blamed type names to the injected type_signals_write."""
    from pipeline_core.application.tasks.task0c import (
        TASK0C_ATTEMPTS_LOG,
        _engine_log_attempt,
    )

    signals: list = []
    deps = SimpleNamespace(
        condense_rust_to_signatures=lambda block, filename: "fn t();",
        type_signals_write=lambda pd, rel, name, difficulty, pattern: signals.append((name, pattern)),
    )
    _engine_log_attempt(
        deps, tmp_path, "src/a.rs", 1, "mod gen_tests_1 {}",
        "expected type `MyType`, found `Other`", "compile_error",
    )
    rows = (tmp_path / TASK0C_ATTEMPTS_LOG).read_text().splitlines()
    assert len(rows) == 1
    rec = json.loads(rows[0])
    assert rec["file"] == "src/a.rs" and rec["outcome"] == "compile_error"
    assert any(name == "MyType" for name, _ in signals)   # type signal recorded


def test_engine_extract_existing_tests() -> None:
    """Relocated pure helper extracts `mod tests` but not `mod gen_tests`."""
    from pipeline_core.application.tasks.task0c import _extract_existing_tests

    src = "pub fn f() {}\n#[cfg(test)]\nmod tests {\n    #[test] fn t() {}\n}\n"
    assert _extract_existing_tests(src).startswith("#[cfg(test)]\nmod tests")
    assert _extract_existing_tests("#[cfg(test)]\nmod gen_tests_1 {}") == ""
    assert _extract_existing_tests("pub fn f() {}") == ""


def test_engine_build_prompt_sections_assembles(tmp_path: Path) -> None:
    """Relocated `_engine_build_prompt_sections` assembles the section dict,
    strips <think> from the strategy, and surfaces the existing inline tests."""
    from pipeline_core.application.tasks.task0c import _engine_build_prompt_sections

    guards: list = []
    deps = SimpleNamespace(
        TASK0C_PROMPT_CHAR_CAP=100_000,
        guard=lambda *a: guards.append(a),
    )
    existing = ("pub fn f() {}\n#[cfg(test)]\nmod tests {\n"
                "    #[test] fn t() { f(); }\n}\n")
    secs = _engine_build_prompt_sections(
        deps,
        rel_path="src/x.rs",
        existing_text=existing,
        fn_bodies="pub fn f() {}  // !! UNCOVERED\n",
        pub_symbols="pub fn f();",
        cargo_toml_content="[package]\nname='x'",
        integration_tests=[],
        sibling_files=[],
        coverage_strategy="<think>reasoning here</think>\nUse builder pattern.",
        attempt_history="",
        project_context="a small crate",
    )
    assert set(secs) >= {"cargo", "full_source", "fn_bodies", "symbols",
                         "strategy", "examples", "context"}
    assert "reasoning here" not in secs["strategy"]   # <think> stripped
    assert "Use builder pattern." in secs["strategy"]
    assert "mod tests" in secs["examples"]            # existing inline tests surfaced
    assert "a small crate" in secs["context"]


def test_engine_build_prompt_sections_budget_truncation() -> None:
    """Tight budget exercises the source/fn truncation + drop paths and
    both _guard calls (source_cap + budget-dropped)."""
    from pipeline_core.application.tasks.task0c import _engine_build_prompt_sections

    guards: list = []
    deps = SimpleNamespace(
        TASK0C_PROMPT_CHAR_CAP=10_000,   # budget = 10000 - 8000 = 2000
        guard=lambda *a: guards.append(a[0]),
    )
    big_src = "// header\n" + "let x = 1;\n" * 200          # > 50% budget → capped
    big_fns = ("pub fn f() {}\n\n// --- next function ---\n\n" * 60)  # > 40% → truncated
    secs = _engine_build_prompt_sections(
        deps,
        rel_path="src/x.rs",
        existing_text=big_src,
        fn_bodies=big_fns,
        pub_symbols="pub fn f();",
        cargo_toml_content="",
        integration_tests=[("tests/a.rs", "x" * 5000)],
        sibling_files=[("src/y.rs", "y" * 5000)],
        coverage_strategy="strat " * 500,
        attempt_history="hist " * 500,
        project_context="ctx " * 500,
        old_block="old " * 500,
        what_worked="ww " * 500,
        retrospective="retro " * 500,
    )
    assert "source truncated" in secs["full_source"]
    assert "fn_bodies truncated" in secs["fn_bodies"]
    assert "task0c/source_cap" in guards and "task0c/budget" in guards
