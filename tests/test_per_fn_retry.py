"""Tests for per_fn_retry use-case (V1 extraction).

Covers:
- fix_one_ref guards (tests/ path, gen_tests file, missing file, no-locate)
- fix_one_ref B2 contract: transport/model/splice errors RAISE (not swallowed)
- apply_per_fn_fixes B2 isolation: one fn's exception skipped, others continue
- apply_per_fn_fixes return count
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline_core.application.reconstruct.per_fn_retry import (
    RetryContext,
    RetryDeps,
    apply_per_fn_fixes,
    fix_one_ref,
)
from pipeline_core.domain.reconstruction import SpliceOutcome


def _ctx(tmp_path: Path) -> RetryContext:
    return RetryContext(
        project_dir=tmp_path,
        src_root=tmp_path / "src",
        cluster_label="stable",
        containers={},
        type_recs=[],
        fr_path=None,
        tests_dir_files=[],
    )


def _deps(**overrides) -> RetryDeps:
    base: dict[str, object] = {
        "locate_fn": lambda text, fn: None,
        "gather_fix_context": MagicMock(return_value=MagicMock(
            container="", types_section="", notes_section="", bound_methods_section="")),
        "build_per_fn_fix_prompt": lambda **kw: "prompt",
        "ask_per_fn_body": lambda prompt, fn, log, **kw: (None, ""),
        "splice_fn_body": lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
        "find_tests_for_fn": lambda tdf, fn, **kw: [],
        "focus_cargo_errors": lambda out, rel_f: "",
        "is_gen_tests_file": lambda name: name.startswith("gen_"),
        "log": lambda msg: None,
    }
    base.update(overrides)
    return RetryDeps(**base)  # type: ignore[arg-type]


# ── fix_one_ref guards ─────────────────────────────────────────────────────────

def test_fix_one_ref_skips_tests_path(tmp_path):
    assert fix_one_ref("tests/foo.rs", "bar", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_skips_gen_tests_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "gen_foo.rs").write_text("fn bar() {}")
    assert fix_one_ref("gen_foo.rs", "bar", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_skips_missing_file(tmp_path):
    assert fix_one_ref("src/nonexistent.rs", "bar", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_returns_false_when_fn_not_located(tmp_path):
    (tmp_path / "src.rs").write_text("fn other() {}")
    assert fix_one_ref("src.rs", "missing_fn", "", _ctx(tmp_path), _deps()) is False


def test_fix_one_ref_returns_false_when_model_returns_none(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")
    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: (None, ""),
    )
    assert fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d) is False


def test_fix_one_ref_returns_true_on_success(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")
    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: ("{ Ok(()) }", ""),
        splice_fn_body=lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
    )
    assert fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d) is True


# ── B2 contract: model/transport errors must RAISE out of fix_one_ref ─────────

def test_fix_one_ref_raises_on_model_error(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")

    def _ask_raise(*a, **kw):
        raise RuntimeError("transport down")

    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=_ask_raise,
    )
    with pytest.raises(RuntimeError, match="transport down"):
        fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d)


def test_fix_one_ref_raises_on_splice_error(tmp_path):
    (tmp_path / "src.rs").write_text("fn bar() { unimplemented!() }")

    def _splice_raise(fp, fn, body, **kw):
        raise ValueError("bad splice")

    d = _deps(
        locate_fn=lambda text, fn: ("fn bar()", 0, 12, 27),
        ask_per_fn_body=lambda *a, **kw: ("{ 42 }", ""),
        splice_fn_body=_splice_raise,
    )
    with pytest.raises(ValueError, match="bad splice"):
        fix_one_ref("src.rs", "bar", "", _ctx(tmp_path), d)


# ── apply_per_fn_fixes B2 isolation ───────────────────────────────────────────

def test_apply_skips_failing_fn_and_continues(tmp_path):
    """fn#1 raises → logged and skipped; fn#2 succeeds → count=1."""
    (tmp_path / "ok.rs").write_text("fn ok_fn() { unimplemented!() }")
    calls: list[str] = []

    def ask(prompt, fn, log, **kw):
        if fn == "boom_fn":
            raise RuntimeError("model exploded")
        calls.append(fn)
        return ("{ 1 }", "")

    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 8, 22),
        ask_per_fn_body=ask,
        splice_fn_body=lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
    )
    ctx = _ctx(tmp_path)
    # boom_fn references missing file (→ False, no raise) to isolate the raise
    # to the ask_per_fn_body path for boom_fn.
    # We need boom_fn to actually reach ask; give it a real file.
    (tmp_path / "boom.rs").write_text("fn boom_fn() { unimplemented!() }")
    count = apply_per_fn_fixes(
        [("boom.rs", "boom_fn"), ("ok.rs", "ok_fn")], "", ctx, d
    )
    assert count == 1
    assert calls == ["ok_fn"]


def test_apply_returns_zero_when_all_raise(tmp_path):
    def ask(*a, **kw):
        raise ConnectionError("offline")

    (tmp_path / "a.rs").write_text("fn a() { unimplemented!() }")
    (tmp_path / "b.rs").write_text("fn b() { unimplemented!() }")
    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 6, 20),
        ask_per_fn_body=ask,
    )
    count = apply_per_fn_fixes(
        [("a.rs", "a"), ("b.rs", "b")], "", _ctx(tmp_path), d
    )
    assert count == 0


def test_apply_returns_correct_count(tmp_path):
    """Two fns both succeed → count=2."""
    for name in ("a.rs", "b.rs"):
        (tmp_path / name).write_text(f"fn {name[0]}() {{ unimplemented!() }}")
    d = _deps(
        locate_fn=lambda text, fn: ("fn x()", 0, 6, 20),
        ask_per_fn_body=lambda *a, **kw: ("{ () }", ""),
        splice_fn_body=lambda fp, fn, body, **kw: SpliceOutcome(True, "ok", 0),
    )
    count = apply_per_fn_fixes(
        [("a.rs", "a"), ("b.rs", "b")], "", _ctx(tmp_path), d
    )
    assert count == 2
