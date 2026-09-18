"""Integration: one test's abort must not erase another test's verdict.

Real cargo, real libtest, the pipeline's own runner (`run_cargo` over
`CargoCli`), the real capture parser and comparison. No model.

The class: libtest runs a binary's tests in one process, so a test whose
unwinding reaches a still-stubbed `Drop` path panics during cleanup, the
runtime aborts, and every other test in that binary loses its verdict —
passing ones included. Readers of the capture then had to guess whose loss it
was, and the guesses grew into a branch cluster that, on run
`3213361-9d582ae7` (2026-09-19), reverted a correct constructor because an
already-red test's own assertion preceded the Drop abort in the same binary.

The crate is invented. `Handle::new` is the "fill"; `Drop for Handle` calls a
stub. Test `a_red_then_drop` builds a Handle and fails its own assertion (the
unwind drops the Handle, the stub panics during cleanup, the process
aborts); `b_passes` never touches a Handle; `c_native_fault` dies on a signal
with no panic. The harness runs on one thread so the shared-process order is
deterministic: the abort in `a_…` kills the process before `b_passes` runs.

Asserted: under isolation the binary completes, `b_passes` keeps its pass,
`a_red_then_drop` is an abort that printed its panics (not a native fault),
`c_native_fault` is a silent abort (the localizer's target), and a baseline
with the constructor stubbed compares to the filled tree with no regression.
Negative control: `PHASE_C_TEST_ISOLATION=0` — the same run loses the
binary's verdicts and the comparison charges the fill.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.domain.test_results import (
    TestStateReading,
    compare_readings,
    parse_binary_results,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "isolated-abort"
_CMD = "cargo test --offline --no-fail-fast -- --test-threads=1"

_CARGO_TOML = """[package]
name = "handles"
version = "0.1.0"
edition = "2021"
"""

_LIB = """pub struct Handle {
    pub id: u32,
}

impl Handle {
    pub fn new(id: u32) -> Handle {
        BODY
    }
}

fn release(_id: u32) {
    unimplemented!()
}

impl Drop for Handle {
    fn drop(&mut self) {
        release(self.id)
    }
}
"""

_TESTS = """use handles::Handle;

#[test]
fn a_red_then_drop() {
    let h = Handle::new(7);
    assert_eq!(h.id, 8, "own assertion");
}

#[test]
fn b_passes() {
    assert_eq!(1 + 1, 2);
}

#[test]
fn c_native_fault() {
    let _h = Handle::new(1);
    std::mem::forget(_h);
    unsafe { std::ptr::null_mut::<u32>().write_volatile(1) };
}
"""


def _crate(root: Path, body: str) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML)
    (root / "src" / "lib.rs").write_text(_LIB.replace("BODY", body))
    (root / "tests" / "handles.rs").write_text(_TESTS)
    return root


def _reading(root: Path, isolation: str, monkeypatch: pytest.MonkeyPatch,
             tag: str) -> TestStateReading:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", isolation)
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / tag)
    env["RUST_BACKTRACE"] = "0"
    _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    return TestStateReading(parse_binary_results(out))


def _handles(r: TestStateReading):
    (b,) = [b for b in r.binaries if b.label.endswith("handles.rs")]
    return b


def test_premise_the_shared_process_loses_the_neighbours_verdict(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _crate(tmp_path / "shared", "Handle { id }")
    b = _handles(_reading(root, "0", monkeypatch, "filled"))
    assert not b.completed, (
        "libtest no longer aborts the binary on a panic during cleanup — the "
        "collateral class this isolation removes may be gone upstream; "
        "re-derive before trusting either path")
    assert "b_passes" not in b.passed


def test_isolation_keeps_every_test_s_own_verdict(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _crate(tmp_path / "iso", "Handle { id }")
    b = _handles(_reading(root, "1", monkeypatch, "filled"))
    assert b.completed
    assert b.passed == {"b_passes"}
    assert b.failed == {"a_red_then_drop", "c_native_fault"}
    assert b.aborted == {"a_red_then_drop", "c_native_fault"}
    assert b.aborted_silent == {"c_native_fault"}


def test_filling_the_constructor_is_not_a_regression_under_isolation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _reading(_crate(tmp_path / "b", "unimplemented!()"), "1",
                    monkeypatch, "stub")
    after = _reading(_crate(tmp_path / "a", "Handle { id }"), "1",
                     monkeypatch, "filled")
    cmp = compare_readings(base, after)
    assert not cmp.regressed, cmp.summary
    assert cmp.newly_crashed == () and cmp.newly_red == frozenset()


def test_negative_control_shared_process_charges_the_fill(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _reading(_crate(tmp_path / "b", "unimplemented!()"), "0",
                    monkeypatch, "stub")
    after = _reading(_crate(tmp_path / "a", "Handle { id }"), "0",
                     monkeypatch, "filled")
    cmp = compare_readings(base, after)
    assert cmp.regressed and cmp.newly_crashed
