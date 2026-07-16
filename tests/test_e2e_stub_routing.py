"""Option C: generate_fix_loop must route 'not implemented' panic output to the
per-fn fixer, NOT the legacy whole-file rewrite, when there are no compile errors.

Reproduces the plateau root cause: _do_fix_per_impl sees empty
_compile_error_frontier (test output has no rustc errors) and falls straight to
_do_fix (whole-file).  The correct behaviour is to detect the unimplemented stubs
via _extract_tdd_frontier and route each through _fix_single_fn / _fix_impl_block.

Observable: _do_fix (whole-file) calls ask() → extract_files(), which requires a
<FILE path="..."> block.  _fix_single_fn also calls ask() → extract_fn_bodies(),
which requires a <FN name="..."> block.  We return only the <FN> form from the
scripted model; the whole-file path produces no splices, the per-fn path does.

Tests here FAIL before the fix, PASS after.
"""
from __future__ import annotations

from pathlib import Path

import pipeline
from tests.harness import diag, install_app_seams, install_scripted_cargo

# ── fixture source ──────────────────────────────────────────────────────────
_STUB_SRC = "pub fn foo() -> u32 { unimplemented!() }\n"

# cargo test output: one 'not implemented' panic, no compile errors
_PANIC_OUTPUT = (
    "running 1 test\n"
    "---- test_foo stdout ----\n"
    "thread 'test_foo' panicked at src/lib.rs:1:24: not implemented\n"
    "test test_foo ... FAILED\n"
    "failures:\n    test_foo\n"
    "test result: FAILED. 0 passed; 1 failed; 0 ignored\n"
)
_PASS_OUTPUT = "test result: ok. 1 passed; 0 failed; 0 ignored\n"

# The model response that only the per-fn path can consume.
# _fix_single_fn uses extract_fn_bodies(<FN ...>) → splice.
# _do_fix (whole-file) uses extract_files(<FILE ...>) → nothing found → no splice.
_PER_FN_RESPONSE = '<FN name="foo" block="any">\n42\n</FN>\n'


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    lib = ws / "src" / "lib.rs"
    lib.write_text(_STUB_SRC, encoding="utf-8")
    return ws, lib


def test_stub_panic_routes_to_per_fn_not_whole_file(monkeypatch, tmp_path):
    """generate_fix_loop must use the per-fn path for 'not implemented' panics
    when compile check is clean — not fall back to the whole-file rewrite.

    Currently FAILS: _do_fix_per_impl hits empty _compile_error_frontier →
    falls to _do_fix → <FN> response not parsed → file unchanged.
    After fix C: _extract_tdd_frontier finds foo → _fix_single_fn called →
    <FN> response spliced → '42' in file.
    """
    ws, lib = _workspace(tmp_path)

    # Outer test run: first call returns panic, second (post-fix) passes.
    # Inner compile check (_fix_single_fn): first call returns error for foo,
    # second call (after splice) is clean.
    install_scripted_cargo(
        monkeypatch,
        check_diag=[
            (101, "error[E0000]: not implemented\n --> src/lib.rs:1:24", [diag("src/lib.rs", 1)]),
            (0, "", []),
        ],
        run=[(101, _PANIC_OUTPUT), (0, _PASS_OUTPUT)],
        frontier=[
            {("src/lib.rs", "foo"): {"<compile>"}},  # pre-fix: foo in frontier
            {},                                        # post-fix: clean
        ],
    )

    # App seams: compile frontier empty (no rustc errors); TDD frontier
    # returns foo as a stub (the routing hook); no impl blocks (foo is free).
    install_app_seams(
        monkeypatch,
        compile_error_frontier=lambda *a, **k: {},
        extract_tdd_frontier=lambda *a, **k: {("src/lib.rs", "foo"): {"test_foo"}},
        collect_impl_blocks=lambda *a, **k: [],
    )

    # Model: return the <FN> form — consumed by per-fn, ignored by whole-file.
    monkeypatch.setattr(pipeline, "_ask_raw", lambda *a, **k: _PER_FN_RESPONSE)

    pipeline.generate_fix_loop(
        project_dir=ws,
        check_cmd="cargo test",
        source_paths=[lib],
        existing_test_files=set(),
    )

    assert "42" in lib.read_text(encoding="utf-8"), (
        "Expected per-fn path to splice '42' into foo; "
        "whole-file path was taken instead (file unchanged or corrupted)."
    )
