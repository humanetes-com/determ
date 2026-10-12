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


# ---- a stderr line is never a verdict (2026-09-27) ----
#
# The sanitized sweep of run `12616-8345ae1d` joined stdout and stderr with no
# mark, and trybuild's stderr progress line (`test tests/ui/x.rs ... ok`)
# became the in-flight test of a binary that aborted before libtest flushed
# its own line. The fix rests on one fact about cargo: a test process's raw
# stderr reaches cargo's STDERR, never its stdout. The invented crate writes a
# verdict-shaped line (no colour, so the name rule cannot catch it) straight
# to the process's stderr, then aborts inside the test libtest announced.

_PHANTOM_TESTS = """use std::io::Write;

#[test]
fn real_in_flight() {
    std::io::stderr().write_all(b"test phantom_from_stderr ... \\n").unwrap();
    std::io::stderr().flush().unwrap();
    std::process::abort();
}
"""


def _phantom_crate(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    # No doc tests: the aborted binary is the last stdout section, as in the
    # sanitized sweep (`--target` skips doc tests), so nothing closes after it.
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("handles", "phantom")
                                     + "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text("pub fn f() {}\n")
    (root / "tests" / "phantom.rs").write_text(_PHANTOM_TESTS)
    return root


def _shared_capture(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """(stdout, stderr) of the shared-process run, through the real adapter."""
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "0")
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "phantom")
    p = CargoCli().run(_CMD, root, env=env, timeout=600)
    return p.stdout, p.stderr


def test_premise_a_test_process_s_raw_stderr_reaches_cargo_s_stderr(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _shared_capture(_phantom_crate(tmp_path / "p"), monkeypatch)
    assert "phantom_from_stderr" in err and "phantom_from_stderr" not in out, (
        "cargo no longer keeps a test process's stderr apart from libtest's "
        "stdout — the capture layout's split has nothing to split on")
    assert "test real_in_flight ..." in out


def test_the_in_flight_test_is_libtest_s_not_the_stderr_line(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain.test_results import join_capture
    out, err = _shared_capture(_phantom_crate(tmp_path / "p"), monkeypatch)
    (b,) = [b for b in parse_binary_results(join_capture(out, err))
            if b.label.endswith("phantom.rs")]
    assert not b.completed and b.dangling == "real_in_flight"
    # Negative control: the sweep's old unmarked join.
    (old,) = [b for b in parse_binary_results(out + "\n" + err)
              if b.label.endswith("phantom.rs")]
    assert old.dangling == "phantom_from_stderr"


# ---- the schedule's crash inputs read a real isolated capture (2026-09-27) ----
#
# Isolation (2026-09-19) rebuilt every aborted binary as a COMPLETED section.
# Two decisions still asked "did a binary not complete" and went dead with no
# signal: the stale-refill probe (run `118933-877c43bd` dropped a culprit of a
# live stack overflow as "crash gone") and the schedule's unmeasurable
# demotion (non-empty in 1 of ~160 fills since). Their unit tests passed
# throughout, because their fixtures were hand-written or pre-isolation
# captures. This section derives every crash input the fill schedule reads
# from the capture the real runner produces, so a change to how the pipeline
# MEASURES cannot silently disable a schedule rule. It carries the `gate`
# marker: `pre_run.sh` runs it and refuses the stamp when it fails.
#
# The invented crate: `ping`/`pong` recurse into each other without a base
# case (the shape of the live ring), `id` is an ordinary body. `overflow`
# dies on a stack overflow, `asserts` fails its own assertion, `passes`
# passes.

_RING_LIB = """#![allow(unconditional_recursion)]
pub fn ping(n: u64) -> u64 {
    let pad = [n; 64];
    pong(pad[(n % 64) as usize] + 1) + pad[1]
}

pub fn pong(n: u64) -> u64 {
    let pad = [n; 64];
    ping(pad[(n % 64) as usize] + 1) + pad[2]
}

pub fn id(n: u64) -> u64 {
    n
}
"""

_RING_TESTS = """#[test]
fn overflow() {
    assert_eq!(ring::ping(1), 0);
}

#[test]
fn asserts() {
    assert_eq!(ring::id(1), 2);
}

#[test]
fn passes() {
    assert_eq!(ring::id(1), 1);
}
"""


def _ring_capture(root: Path, monkeypatch: pytest.MonkeyPatch,
                  isolation: str = "1") -> str:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("handles", "ring")
                                     + "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text(_RING_LIB)
    (root / "tests" / "ring.rs").write_text(_RING_TESTS)
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", isolation)
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "ring")
    env["RUST_BACKTRACE"] = "0"
    _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    return out


def _ring_runs():
    from pipeline_core.domain.fill_schedule import RouteKind, RouteRun
    from pipeline_core.domain.models import FnLocator
    culprit = FnLocator("src/lib.rs", "ping")
    bystander = FnLocator("src/lib.rs", "id")
    return culprit, [
        RouteRun(RouteKind.FILE, "bystander", frozenset({bystander}),
                 target_tests=frozenset({"overflow"}), seq=0),
        RouteRun(RouteKind.PER_FN, "culprit", frozenset({culprit}),
                 target_tests=frozenset({"overflow"}), seq=1),
    ]


@pytest.mark.gate
def test_premise_a_stack_overflow_is_a_silent_abort_under_isolation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (b,) = [b for b in parse_binary_results(
        _ring_capture(tmp_path / "r", monkeypatch)) if b.label.endswith("ring.rs")]
    assert b.completed, "isolation no longer rebuilds an aborted binary"
    assert b.passed == {"passes"} and b.failed == {"overflow", "asserts"}
    assert b.aborted_silent == {"overflow"}, (
        "the runtime's stack-overflow abort is no longer a panic-less abort "
        "of its own process — re-derive what the crash readers key on")


@pytest.mark.gate
def test_every_schedule_crash_input_sees_the_isolated_abort(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract: on one real capture, the crash definition, the
    unmeasurable set, the probe's admission and the schedule agree."""
    from pipeline_core.domain.fill_schedule import schedule
    from pipeline_core.domain.refill_stamp import RefillAdmission, refill_admission
    from pipeline_core.domain.test_results import (
        native_crashes,
        parse_failed_tests,
        unmeasured_failures,
    )
    out = _ring_capture(tmp_path / "r", monkeypatch)
    bins = parse_binary_results(out)
    assert [n for _b, n in native_crashes(bins)] == ["overflow"]
    assert unmeasured_failures(bins, parse_failed_tests(out)) == {"overflow"}
    # The probe: a culprit with no attributed test stays while the crash does.
    assert refill_admission((), crash_blamed=bool(native_crashes(bins))) \
        is RefillAdmission.CRASH_BLAMED
    culprit, runs = _ring_runs()
    s = schedule(runs, [], oracle_fp="o", hollow={}, budget_minutes=60,
                 unmeasurable_tests=unmeasured_failures(
                     bins, parse_failed_tests(out)),
                 crash_blamed=frozenset({culprit}))
    assert [r.run.label for r in s.ordered] == ["culprit"]
    assert [(r.run.label, r.reason) for r in s.deferred] == [
        ("bystander", "rebuy_flat")]


@pytest.mark.gate
def test_negative_control_the_binary_completion_question_is_blind(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # What both dead readers asked, on the same real capture: no binary is
    # incomplete, so "crash present" is False and "no verdict" is empty.
    from pipeline_core.domain.oracle_ledger import bare_test_name
    from pipeline_core.domain.test_results import parse_failed_tests
    out = _ring_capture(tmp_path / "r", monkeypatch)
    bins = parse_binary_results(out)
    assert not any(not b.completed for b in bins)
    verdicted = {bare_test_name(n) for b in bins for n in b.failed}
    assert {bare_test_name(t) for t in parse_failed_tests(out)} <= verdicted


# ---- crash blame from the real localizer (full_pre_run contract) ----
#
# The gate tests above hand the schedule its `crash_blamed` set. This one
# derives it the way the loop does: the real isolated capture, then
# `localize_crashes` with the real gdb adapter, then `blamed_locators`.
# Premise: gdb is installed (the loop's localizer needs it too).

def test_the_localizer_blames_both_members_of_the_recursion(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from pipeline_core.adapters.gdb_backtrace import localize_crashed_binary
    from pipeline_core.application.reconstruct.crash_localizer import (
        CrashLocalizerDeps,
        localize_crashes,
    )
    from pipeline_core.domain.crash_site import blamed_locators
    from pipeline_core.domain.models import FnLocator
    assert shutil.which("gdb"), "premise: the crash localizer needs gdb"
    root = tmp_path / "r"
    out = _ring_capture(root, monkeypatch)
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "ring")
    logs: list[str] = []
    sites = localize_crashes(
        CrashLocalizerDeps(gdb_localize=localize_crashed_binary,
                           asan_sweep=None, log=logs.append),
        out, _CMD, root, env)
    assert sites, "no crash site localized:\n" + "\n".join(logs)
    blamed = blamed_locators(sites)
    assert {FnLocator("src/lib.rs", "ping"),
            FnLocator("src/lib.rs", "pong")} <= blamed, (blamed, logs)


# ---- a body that overflows the stack is a destroyed verdict (2026-10-11) ----
#
# Run `2157238-210637e6` kept five `construct_from_*` bodies that called
# themselves: the attributed tests were red at baseline (stub panic) and red
# after (stack overflow), and the keep read that as non-regression. The fix
# rests on two facts about the real tools: a stack overflow under isolation
# is rebuilt as a FAILED entry (never a missing verdict), and its block
# prints no panic (`aborted_silent`). The invented crate's `build` is the
# fill: a stub, then a body that calls itself.

_REC_LIB = """pub fn build(n: u32) -> u32 {
    BODY
}
"""

_REC_TESTS = """#[test]
fn builds() {
    assert_eq!(handles::build(3), 3);
}
"""


def _rec_crate(root: Path, body: str) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML + "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text(
        "#![allow(unconditional_recursion)]\n" + _REC_LIB.replace("BODY", body))
    (root / "tests" / "builds.rs").write_text(_REC_TESTS)
    return root


class _NoRecords:
    def enumerate_fns(self, _src: Path) -> list[dict[str, object]]:
        return []


def _scoped(root: Path, monkeypatch: pytest.MonkeyPatch, tag: str,
            baseline: TestStateReading | None):
    from pipeline_core.application.reconstruct.cargo_runner import u2_test_gate
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / tag)
    env["RUST_BACKTRACE"] = "0"

    def _run(cmd: str, cwd: Path, env_: dict[str, str], timeout: int = 600):
        return run_cargo(CargoCli(), cmd, cwd, env_, timeout=timeout)
    return u2_test_gate(
        _run, {"builds"}, frozenset(), _CMD, root, env,
        structure=_NoRecords(), enabled=True, timeout=600, err_cap=2000,
        guard=lambda *_: None, baseline=baseline)


def test_premise_a_stack_overflow_under_isolation_is_a_silent_failed_entry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _rec_crate(tmp_path / "rec", "build(n)")
    r = _reading(root, "1", monkeypatch, "rec")
    (b,) = [b for b in r.binaries if b.label.endswith("builds.rs")]
    assert b.completed and b.failed == {"builds"}, (
        "isolation no longer rebuilds a stack overflow as a FAILED entry; "
        "the gate's destroyed-verdict reading must be re-derived")
    assert b.aborted_silent == {"builds"}, (
        "a stack overflow now prints a panic line; it would no longer read "
        "as a native fault")


def test_a_self_calling_fill_is_refused_by_the_gate_and_the_keep(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain.test_report import (
        U2Decision,
        judge_refill,
        parse_test_output,
        self_aborted_tests,
    )
    base = _reading(_rec_crate(tmp_path / "stub", "unimplemented!()"), "1",
                    monkeypatch, "rec-stub")
    assert "builds" in base.failed_names and not base.aborted_names
    v = _scoped(_rec_crate(tmp_path / "rec", "build(n)"), monkeypatch,
                "rec", base)
    assert v.decision is U2Decision.REJECT and v.aborted == {"builds"}
    assert not judge_refill({"builds"}, base, v).non_regression

    # NEGATIVE CONTROL: the destroyed set read as before — missing names
    # only — is empty on the same real capture.
    report = parse_test_output(v.capture)
    missing = frozenset({"builds"} - (report.passed
                                      | {f.test_name for f in report.failures}))
    assert self_aborted_tests(missing, base.judged_names) == frozenset()
