"""L1 e2e routing tests — CODE_ARCHITECTURE.md R10.2.

Retires the "●●● await live run" tag on R9.1 (build-failure → BUILD-mode fix path)
and R9.2 (keep/revert decision). These drive `_task3_inline_retry` end-to-end with
a scripted model + scripted cargo: no LLM, no real cargo, deterministic, ms.

The decision under test reacts to cargo error-count, not model quality — exactly
the class the harness exists for. The scripted cargo ties its diagnostic count to
the on-disk file state, so a real `_splice_fn_body` mutation drives the metric.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pipeline
from tests.harness import (
    diag,
    install_scripted_attribution,
    install_scripted_cargo,
    install_scripted_model,
)

# A stub fn: `_apply_per_fn_fixes` only re-fills `unimplemented!()` bodies (the
# realistic build-failure case — a stub that never got a compilable body).
_SRC = "pub fn foo() -> i32 { unimplemented!() }\n"
_FIXED_BODY = "{ 1 }"


def _project(td: str) -> Path:
    p = Path(td)
    (p / "src").mkdir()
    (p / "src" / "lib.rs").write_text(_SRC, encoding="utf-8")
    return p


def test_build_failure_reverts_when_fix_worsens(monkeypatch) -> None:
    """R9.1+R9.2 BUILD mode: zero FAILED tests but a non-compiling tree → BUILD
    mode attributes via diagnostics and re-splices; the splice raises the error
    count (2→3) → the tree is reverted from snapshot."""
    monkeypatch.setenv("TASK3_INLINE_MAX_STALLS", "1")
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        lib = proj / "src" / "lib.rs"

        def check_diag(*_a, **_k):
            worse = "unimplemented" not in lib.read_text(encoding="utf-8")
            n = 3 if worse else 2  # the "fix" makes it strictly worse
            return (101, "error: boom", [diag("src/lib.rs") for _ in range(n)])

        install_scripted_model(monkeypatch, per_fn_body=_FIXED_BODY)
        install_scripted_cargo(monkeypatch, check_diag=check_diag,
                               frontier={("src/lib.rs", "foo"): {"<compile>"}})

        passed, out = pipeline._task3_inline_retry(proj, "cargo test", 1, "")

        assert passed is False
        assert lib.read_text(encoding="utf-8") == _SRC  # reverted from snapshot
        assert out == ""  # build_mode revert returns the initial output


def test_build_failure_keeps_when_fix_compiles(monkeypatch) -> None:
    """R9.1+R9.2 BUILD mode: the re-splice makes the tree COMPILE (2→0 errors) →
    progress, kept (not reverted), task3 re-run signalled via non-initial output."""
    monkeypatch.setenv("TASK3_INLINE_MAX_STALLS", "1")
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        lib = proj / "src" / "lib.rs"

        def check_diag(*_a, **_k):
            fixed = "unimplemented" not in lib.read_text(encoding="utf-8")
            if fixed:
                return (0, "ok", [])  # compiles now
            return (101, "error: boom", [diag("src/lib.rs"), diag("src/lib.rs")])

        install_scripted_model(monkeypatch, per_fn_body=_FIXED_BODY)
        install_scripted_cargo(monkeypatch, check_diag=check_diag,
                               frontier={("src/lib.rs", "foo"): {"<compile>"}})

        passed, out = pipeline._task3_inline_retry(proj, "cargo test", 1, "")

        assert passed is False  # build_mode always re-runs task3, never returns True
        assert "unimplemented" not in lib.read_text(encoding="utf-8")  # kept
        assert out != ""  # progress path returns last_out, not the empty initial


_FAILING = "test result: FAILED. 1 passed; 1 failed\ntest tests::t1 ... FAILED\n"


def test_test_mode_reverts_when_fix_breaks_build(monkeypatch) -> None:
    """R9.2 TEST mode: 1 failing test (tests ran) → fix re-splices but the result
    no longer COMPILES (tests stop running). broke_build = ran→not-ran → a
    non-compiling tree is strictly worse than 1 failing test → revert."""
    # max_stalls=2 so the loop re-checks AFTER the fix and observes the broken tree
    # (with =1 it exits on the pre-fix metric and never sees the regression).
    monkeypatch.setenv("TASK3_INLINE_MAX_STALLS", "2")
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        lib = proj / "src" / "lib.rs"

        def run(*_a, **_k):
            if "unimplemented" not in lib.read_text(encoding="utf-8"):
                return (101, "error[E0277]: boom")  # broke the build, 0 tests run
            return (101, _FAILING)  # still 1 failing test

        install_scripted_model(monkeypatch, per_fn_body=_FIXED_BODY)
        install_scripted_cargo(monkeypatch, check_diag=(0, "", []), run=run)
        install_scripted_attribution(monkeypatch,
                                     {("src/lib.rs", "foo"): {"tests::t1"}})

        passed, out = pipeline._task3_inline_retry(proj, "cargo test", 1, _FAILING)

        assert passed is False
        assert lib.read_text(encoding="utf-8") == _SRC  # reverted from snapshot
        assert out == _FAILING  # revert returns the initial output


# A fn with a WRONG, already-filled (non-stub) body — the real shape of a build
# failure (a stub `unimplemented!()` compiles, so it never causes one).
_SRC_NONSTUB = "pub fn foo() -> i32 { 999 }\n"


def test_build_failure_respices_nonstub_body(monkeypatch) -> None:
    """Regression: `_apply_per_fn_fixes` must re-fill a non-stub body (it now
    passes allow_nonstub=True, like every other fix-loop site). Before the fix
    `_splice_fn_body` refused the non-stub body → 'no_fns_spliced' → no-op."""
    monkeypatch.setenv("TASK3_INLINE_MAX_STALLS", "2")
    with tempfile.TemporaryDirectory() as td:
        proj = _project(td)
        lib = proj / "src" / "lib.rs"
        lib.write_text(_SRC_NONSTUB, encoding="utf-8")

        def check_diag(*_a, **_k):
            fixed = "1" in lib.read_text(encoding="utf-8") and "999" not in lib.read_text(encoding="utf-8")
            if fixed:
                return (0, "ok", [])
            return (101, "error: boom", [diag("src/lib.rs"), diag("src/lib.rs")])

        install_scripted_model(monkeypatch, per_fn_body="{ 1 }")
        install_scripted_cargo(monkeypatch, check_diag=check_diag,
                               frontier={("src/lib.rs", "foo"): {"<compile>"}})

        passed, out = pipeline._task3_inline_retry(proj, "cargo test", 1, "")

        body = lib.read_text(encoding="utf-8")
        assert "999" not in body  # the wrong non-stub body was replaced
        assert "1" in body
