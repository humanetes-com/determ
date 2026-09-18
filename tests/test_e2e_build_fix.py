"""Offline e2e for `_task3_inline_retry`, retiring the "●●● await live"
tag on BUILD mode and compile-state-aware keep/revert.

Drives the real inline-retry logic with the scripted-cargo + scripted-model
harness (no live LLM, no real cargo). The branches a live run rarely hits —
a tree that fails to COMPILE (zero FAILED test lines) and a fix that makes the
tree STRICTLY WORSE — are forced deterministically here. Asserts the contract
(return tuple + whether the src tree was reverted from snapshot or kept), not
brittle cargo call counts.

BUILD mode  = initial output has no `FAILED` test line ⇒ attribute via compile
              diagnostics, metric = compile-error count.
TEST  mode  = some test FAILED ⇒ attribute via the test reverse-map/classifier,
              metric = failing-test count; revert if the retry broke the build
              or raised the failure count.
"""
from __future__ import annotations

from pathlib import Path

import pipeline
from tests.harness import (  # noqa: I001
    diag,
    install_app_seams,
    install_fake_extractor,
    install_scripted_attribution,
    install_scripted_cargo,
    install_scripted_model,
)

STUB_LIB = """pub fn answer() -> u32 {
    unimplemented!()
}
"""

MARKER = "/*FIXED*/"
FIX_BODY = f"{MARKER} 0"

# --- crafted cargo-test outputs (parsed by _parse_failed_tests/_parse_test_counts) ---
BUILD_ERR = "error[E0277]: mismatched types\n --> src/lib.rs:1:1\n"


def _failed(n: int, passed: int = 0) -> str:
    names = [f"t_{i}" for i in range(n)]
    rows = "".join(f"test {nm} ... FAILED\n" for nm in names)
    flist = "".join(f"    {nm}\n" for nm in names)
    return (f"running {n} test(s)\n{rows}\nfailures:\n{flist}\n"
            f"test result: FAILED. {passed} passed; {n} failed\n")


OK_OUT = "test result: ok. 2 passed; 0 failed\n"


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(STUB_LIB)
    (ws / "tests").mkdir()
    # Keep the per-fn fix context off the extractor binary (absent in CI py).
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[])
    install_app_seams(monkeypatch, fn_container_index=lambda *a, **k: {})
    return ws


def _lib(ws: Path) -> str:
    return (ws / "src" / "lib.rs").read_text()


# ── BUILD mode ────────────────────────────────────────────────────────

def test_build_mode_no_reproducible_diags_bails(monkeypatch, tmp_path):
    """0 FAILED tests AND the compile is clean under this env ⇒ nothing to act
    on: return the untouched initial output, never call the model."""
    ws = _setup(monkeypatch, tmp_path)
    model = install_scripted_model(monkeypatch, per_fn_body=FIX_BODY)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    res = pipeline._task3_inline_retry(ws, "cargo test", 1, BUILD_ERR, cluster=None)
    assert res == (False, BUILD_ERR)
    assert model.calls == 0
    assert MARKER not in _lib(ws)            # untouched


def test_build_mode_worsened_reverts_from_snapshot(monkeypatch, tmp_path):
    """BUILD fix raises the compile-error count above the initial floor ⇒ the
    src tree is reverted from snapshot and the initial output is returned
    (non-compiling-worse is strictly worse)."""
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body=FIX_BODY)
    n = {"i": 0}

    def check_diag(*a, **k):
        n["i"] += 1
        errs = 1 if n["i"] == 1 else 2       # initial=1, every later measure=2
        return (101, BUILD_ERR, [diag("src/lib.rs", code="E0277")] * errs)

    install_scripted_cargo(monkeypatch, check_diag=check_diag,
                           frontier={("src/lib.rs", "answer"): {"<compile>"}})
    res = pipeline._task3_inline_retry(ws, "cargo test", 1, BUILD_ERR, cluster=None)
    assert res == (False, BUILD_ERR)
    assert MARKER not in _lib(ws)            # reverted to the stub
    assert "unimplemented!()" in _lib(ws)


def test_build_mode_progress_keeps_and_reruns_task3(monkeypatch, tmp_path):
    """BUILD fix lowers the compile-error count ⇒ keep the spliced tree and
    return (False, last_out) so TASK 3 re-verifies (no revert)."""
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body=FIX_BODY)
    n = {"i": 0}

    def check_diag(*a, **k):
        n["i"] += 1
        errs = 2 if n["i"] == 1 else 1       # initial=2, every later measure=1
        return (101, BUILD_ERR, [diag("src/lib.rs", code="E0277")] * errs)

    install_scripted_cargo(monkeypatch, check_diag=check_diag,
                           frontier={("src/lib.rs", "answer"): {"<compile>"}})
    passed, _out = pipeline._task3_inline_retry(ws, "cargo test", 1, BUILD_ERR, cluster=None)
    assert passed is False
    assert MARKER in _lib(ws)                # progress kept


# ── TEST mode keep/revert ─────────────────────────────────────────────

def test_test_mode_raised_failures_reverts(monkeypatch, tmp_path):
    """Retry raises the failing-test count above the initial floor ⇒ revert."""
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body=FIX_BODY)
    install_scripted_attribution(monkeypatch,
                                 frontier={("src/lib.rs", "answer"): {"t_0"}})
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []),
                           run=(101, _failed(2)))   # always 2 failing > initial 1
    res = pipeline._task3_inline_retry(ws, "cargo test", 1, _failed(1), cluster=None)
    assert res == (False, _failed(1))
    assert MARKER not in _lib(ws)            # reverted


def test_test_mode_partial_progress_kept(monkeypatch, tmp_path):
    """Retry lowers the failing count but never reaches 0, tree still compiles
    ⇒ keep partial progress (no revert), return (False, last_out)."""
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body=FIX_BODY)
    install_scripted_attribution(monkeypatch,
                                 frontier={("src/lib.rs", "answer"): {"t_0"}})
    one_failed = _failed(1, passed=1)        # 1 passed; 1 failed (< initial 2, still runs)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []),
                           run=(101, one_failed))
    passed, out = pipeline._task3_inline_retry(ws, "cargo test", 1, _failed(2), cluster=None)
    assert passed is False
    assert out == one_failed
    assert MARKER in _lib(ws)                # kept


def test_test_mode_pass_returns_true(monkeypatch, tmp_path):
    """First re-check already passes ⇒ (True, last_out)."""
    ws = _setup(monkeypatch, tmp_path)
    install_scripted_model(monkeypatch, per_fn_body=FIX_BODY)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []),
                           run=(0, OK_OUT))
    res = pipeline._task3_inline_retry(ws, "cargo test", 1, _failed(1), cluster=None)
    assert res == (True, OK_OUT)
