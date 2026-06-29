"""Tests for pipeline_core.application.validate — task output validators."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.validate import (
    Task2CheckerDeps,
    validate_task1_output,
    validate_task2_api_surface_conformance,
    validate_task2_output,
    validate_task4_output,
)
from pipeline_core.domain.models import TypeRecord


def _deps(types: list[TypeRecord] | None = None, fns: list[dict] | None = None,
          api: str = "") -> Task2CheckerDeps:
    return Task2CheckerDeps(
        enumerate_types=lambda _: types,
        enumerate_fns=lambda _: fns,
        load_api_surface=lambda _d, _m: api,
    )


# ---- validate_task1_output -------------------------------------------------

def test_task1_short_content(tmp_path: Path) -> None:
    req = tmp_path / "req.md"
    req.write_text("short", encoding="utf-8")
    ok, reason = validate_task1_output(req, [])
    assert not ok
    assert "too short" in reason


def test_task1_meta_commentary(tmp_path: Path) -> None:
    req = tmp_path / "req.md"
    req.write_text("x" * 600 + "\nThis document describes the crate.", encoding="utf-8")
    ok, reason = validate_task1_output(req, [])
    assert not ok
    assert "meta-commentary" in reason


def test_task1_insufficient_identifier_overlap(tmp_path: Path) -> None:
    req = tmp_path / "req.md"
    req.write_text("x" * 600 + "\nRequestParser responseHandler",  encoding="utf-8")
    ok, reason = validate_task1_output(req, [("t.rs", "completely_unrelated_code qwerty zxcv")])
    assert not ok
    assert "too generic" in reason


def test_task1_ok(tmp_path: Path) -> None:
    req = tmp_path / "req.md"
    # PascalCase / snake_case identifiers — mixed_case_identifiers extracts these
    # (it does NOT extract lowerCamelCase like `parseRequest`).
    shared = "ParseRequest HandleResponse ValidateInput ProcessOutput BuildResult"
    req.write_text("x" * 600 + "\n" + shared, encoding="utf-8")
    test_content = "ParseRequest HandleResponse ValidateInput ProcessOutput BuildResult ExtraFn"
    ok, reason = validate_task1_output(req, [("t.rs", test_content)])
    assert ok
    assert reason == "ok"


def test_task1_missing_file(tmp_path: Path) -> None:
    req = tmp_path / "nonexistent.md"
    ok, reason = validate_task1_output(req, [])
    assert not ok
    assert "too short" in reason


# ---- validate_task4_output -------------------------------------------------

def test_task4_stall() -> None:
    ok, reason = validate_task4_output("same content", "same content", "output")
    assert not ok
    assert "stall" in reason


def test_task4_shrank() -> None:
    ok, reason = validate_task4_output("longer original text", "short", "output")
    assert not ok
    assert "shrank" in reason


def test_task4_no_identifier_overlap() -> None:
    before = "x" * 50
    after = before + "\n\ngeneric update with no identifiers from tests"
    # PascalCase so the test-output idents are non-empty (lowerCamelCase is not
    # extracted) → the overlap check runs and fails on the disjoint update.
    ok, reason = validate_task4_output(before, after, "ParseRequest HandleResponse")
    assert not ok
    assert "no identifiers" in reason


def test_task4_duplicate_paragraph() -> None:
    para = "This is a long paragraph that already exists in genesis. " * 4
    before = para
    after = before + "\n\n" + para
    ok, reason = validate_task4_output(before, after, "")
    assert not ok
    assert "duplicates" in reason


def test_task4_ok() -> None:
    before = "original content"
    after = before + "\n\nFixed parseRequest by correcting handleResponse logic."
    ok, reason = validate_task4_output(before, after, "parseRequest handleResponse")
    assert ok
    assert reason == "ok"


def test_task4_build_failure_skips_identifier_check() -> None:
    before = "x" * 10
    after = before + "\n\nGeneral structural rule: keep module boundaries clean."
    ok, reason = validate_task4_output(before, after, "E0308 mismatched types", failure_type="build")
    assert ok


# ---- validate_task2_output -------------------------------------------------

def test_task2_no_expected_paths(tmp_path: Path) -> None:
    ok, reason = validate_task2_output(tmp_path, [], deps=_deps())
    assert ok


def test_task2_missing_file(tmp_path: Path) -> None:
    ok, reason = validate_task2_output(tmp_path, ["src/lib.rs"], deps=_deps())
    assert not ok
    assert "missing" in reason


def test_task2_tiny_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    src.write_bytes(b"x" * 10)
    ok, reason = validate_task2_output(tmp_path, ["src/lib.rs"], deps=_deps())
    assert not ok
    assert "stub" in reason


def test_task2_placeholder_only_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    # ≥50 bytes so it clears the tiny-file (<50) gate and reaches the
    # placeholder-only check; still all-placeholder after stripping.
    src.write_text("pub fn frobnicate_the_widget_value() { unimplemented!() }",
                   encoding="utf-8")
    ok, reason = validate_task2_output(tmp_path, ["src/lib.rs"], deps=_deps())
    assert not ok
    assert "placeholder" in reason


def test_task2_ok_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "lib.rs"
    src.parent.mkdir()
    src.write_text(
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\npub fn sub(a: i32, b: i32) -> i32 { a - b }",
        encoding="utf-8",
    )
    ok, reason = validate_task2_output(tmp_path, ["src/lib.rs"], deps=_deps())
    assert ok


# ---- validate_task2_api_surface_conformance --------------------------------

def test_api_conformance_no_api_surface(tmp_path: Path) -> None:
    ok, reason = validate_task2_api_surface_conformance(
        tmp_path, "mymod", tmp_path, deps=_deps(api="")
    )
    assert ok
    assert "skipping" in reason


def test_api_conformance_all_present(tmp_path: Path) -> None:
    api = "pub struct Foo {}\npub fn bar() {}"
    types = [TypeRecord(file="src/lib.rs", kind="struct", name="Foo", is_pub=True)]
    fns = [{"fn_name": "bar", "is_pub": True, "file": "src/lib.rs"}]
    src = tmp_path / "src"
    src.mkdir()
    ok, reason = validate_task2_api_surface_conformance(
        tmp_path, "mymod", tmp_path, deps=_deps(types=types, fns=fns, api=api)
    )
    assert ok


def test_api_conformance_drift(tmp_path: Path) -> None:
    api = "pub struct Foo {}\npub struct Bar {}"
    types = [TypeRecord(file="src/lib.rs", kind="struct", name="Foo", is_pub=True)]
    src = tmp_path / "src"
    src.mkdir()
    ok, reason = validate_task2_api_surface_conformance(
        tmp_path, "mymod", tmp_path, deps=_deps(types=types, fns=[], api=api)
    )
    assert not ok
    assert "Bar" in reason


def test_api_conformance_no_deps_skips_check(tmp_path: Path) -> None:
    ok, reason = validate_task2_api_surface_conformance(
        tmp_path, "mymod", tmp_path, deps=None
    )
    assert ok
    assert "skipping" in reason
