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
        ("bystander", "unmeasurable")]


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


# ---- a correct reader handed a fabricated pointer (2026-10-15) ----
#
# Run `1057870-b427a3a2`: an accessor that reads the first field of the
# pointee it is handed was convicted four attempts running, because the
# constructor above it handed it `NonNull::dangling()`. gdb names the
# faulting frame only; the localizer now reads the fault address and each
# frame's pointer arguments (`traced_origin_index`) and names the frame that
# produced the address. The invented crate has both shapes: `make` hands a
# dangling pointer to the correct `read_tag`; `make_far` hands a valid
# pointer to `read_far`, which computes an address far outside it.

_TRACE_LIB = """use core::ptr::NonNull;

static N: u32 = 7;

#[repr(C)]
pub struct Item {
    pub tag: &'static u32,
    pub n: u32,
}

pub fn read_tag(p: NonNull<Item>) -> u32 {
    unsafe { *(*p.as_ptr()).tag }
}

pub fn make() -> u32 {
    read_tag(NonNull::dangling())
}

pub fn read_far(p: NonNull<Item>) -> u32 {
    unsafe { *((p.as_ptr() as *const u8).wrapping_add(1usize << 44) as *const u32) }
}

pub fn make_far() -> u32 {
    let it = Item { tag: &N, n: 1 };
    read_far(NonNull::from(&it))
}
"""

_TRACE_TESTS = """#[test]
fn handed_dangling() {
    assert_eq!(trace::make(), 7);
}

#[test]
fn computed_far() {
    assert_eq!(trace::make_far(), 7);
}
"""


def _trace_sites(root: Path, monkeypatch: pytest.MonkeyPatch,
                 gdb_localize):
    from pipeline_core.application.reconstruct.crash_localizer import (
        CrashLocalizerDeps,
        localize_crashes,
    )
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("handles", "trace")
                                     + "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text(_TRACE_LIB)
    (root / "tests" / "trace.rs").write_text(_TRACE_TESTS)
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "trace")
    env["RUST_BACKTRACE"] = "0"
    _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    logs: list[str] = []
    # One crashed binary per isolated test: `max_binaries` covers both.
    sites = localize_crashes(
        CrashLocalizerDeps(gdb_localize=gdb_localize, asan_sweep=None,
                           log=logs.append),
        out, _CMD, root, env, max_binaries=4)
    return {s.test_name: s for s in sites}, logs


def test_the_localizer_names_the_frame_that_produced_the_address(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from pipeline_core.adapters.gdb_backtrace import localize_crashed_binary
    assert shutil.which("gdb"), "premise: the crash localizer needs gdb"
    by_test, logs = _trace_sites(tmp_path / "t", monkeypatch,
                                 localize_crashed_binary)
    handed = by_test.get("handed_dangling")
    far = by_test.get("computed_far")
    assert handed is not None and far is not None, (by_test, logs)
    # Premise of the rule: the faulting frame is the reader in both.
    assert handed.frames[0].fn_name == "read_tag", handed.frames
    assert far.frames[0].fn_name == "read_far", far.frames
    # The reader was handed the dangling address: its producer is named.
    assert handed.origin_traced
    assert handed.blamed is not None and handed.blamed.fn_name == "make"
    # The reader computed the address itself: the reader is named.
    assert far.blamed is not None and far.blamed.fn_name == "read_far"


def test_negative_control_without_the_fault_address_the_reader_is_blamed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.adapters.gdb_backtrace import localize_crashed_binary

    def _two_tuple(*a, **kw):
        got = localize_crashed_binary(*a, **kw)
        return None if got is None else (got[0], got[1])
    by_test, logs = _trace_sites(tmp_path / "t", monkeypatch, _two_tuple)
    handed = by_test.get("handed_dangling")
    assert handed is not None, (by_test, logs)
    assert not handed.origin_traced
    assert handed.blamed is not None and handed.blamed.fn_name == "read_tag"


# ---- the settlement end to end (2026-10-16) ----
#
# The same run's lock, with the real runner, debugger, extractor and the
# shared U2 gate: `read` (the write) is a correct reader; `make_a` and
# `make_b` are kept callers that hand it `NonNull::dangling()`, in two test
# binaries; only `make_a`'s test is aimed at the write, so `make_b` shows up
# only when the settler re-runs the whole suite. Before the write `read` is a
# stub and both callers' tests stop there.

_SETTLE_LIB = """use core::ptr::NonNull;

static N: u32 = 7;

#[repr(C)]
pub struct Item {
    pub tag: &'static u32,
}

pub fn read(p: NonNull<Item>) -> u32 {
    BODY
}

pub fn make_a() -> u32 {
    read(NonNull::dangling())
}

pub fn make_b() -> u32 {
    read(NonNull::dangling())
}

pub fn good() -> u32 {
    let it = Item { tag: &N };
    read(NonNull::from(&it))
}
"""

_SETTLE_A = """#[test]
fn a_made() {
    assert_eq!(settle::make_a(), 7);
}

#[test]
fn a_good() {
    assert_eq!(settle::good(), 7);
}
"""

_SETTLE_B = """#[test]
fn b_made() {
    assert_eq!(settle::make_b(), 7);
}
"""

_EXTRACTOR = (Path(__file__).resolve().parents[2] / "tools"
              / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")


def _settle_crate(root: Path, body: str) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("handles", "settle")
                                     + "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text(_SETTLE_LIB.replace("BODY", body))
    (root / "tests" / "a.rs").write_text(_SETTLE_A)
    (root / "tests" / "b.rs").write_text(_SETTLE_B)


def _settle_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                 bind_settler: bool):
    from pipeline_core.adapters.extractor import CliExtractor
    from pipeline_core.adapters.gdb_backtrace import localize_crashed_binary
    from pipeline_core.adapters.structure import CliStructureReader
    from pipeline_core.application.reconstruct import crash_settler
    from pipeline_core.application.reconstruct.cargo_runner import u2_test_gate
    from pipeline_core.application.reconstruct.crash_localizer import (
        CrashLocalizerDeps,
        localize_crashes,
    )
    from pipeline_core.domain.models import FnSite
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"
    root = tmp_path / "s"
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "settle")
    env["RUST_BACKTRACE"] = "0"

    def _run(cmd: str, cwd: Path, env_: dict[str, str], timeout: int = 600):
        return run_cargo(CargoCli(), cmd, cwd, env_, timeout=timeout)
    _settle_crate(root, "unimplemented!()")
    baseline = TestStateReading(parse_binary_results(
        _run(_CMD, root, env)[1]))
    (root / "src" / "lib.rs").write_text(
        _SETTLE_LIB.replace("BODY", "unsafe { *(*p.as_ptr()).tag }"))
    recorded: list = []
    logs: list[str] = []
    settler = crash_settler.CrashSettler(crash_settler.CrashSettlerDeps(
        localize=lambda cap, excl: localize_crashes(
            CrashLocalizerDeps(gdb_localize=localize_crashed_binary,
                               asan_sweep=None, log=logs.append),
            cap, _CMD, root, env, exclude=excl, max_binaries=4),
        run_suite=lambda: _run(_CMD, root, env)[1],
        is_proven=lambda _u: False, src_dir=root,
        record_settled=lambda locs, _s: recorded.append(sorted(locs)),
        log=logs.append))
    crash_settler.bind(settler if bind_settler else None)
    try:
        verdict = u2_test_gate(
            _run, {"a_made", "a_good"},
            frozenset({FnSite("src/lib.rs", "read", "")}), _CMD, root, env,
            structure=CliStructureReader(CliExtractor(_EXTRACTOR)),
            enabled=True, timeout=600, err_cap=4000,
            guard=lambda *_: None, baseline=baseline)
    finally:
        crash_settler.bind(None)
    return verdict, (root / "src" / "lib.rs").read_text(), recorded, logs


def test_a_crash_under_a_correct_reader_settles_on_its_callers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain.test_report import U2Decision
    verdict, lib, recorded, logs = _settle_gate(
        tmp_path, monkeypatch, bind_settler=True)
    # The write stands: its aimed test now stops in a stub outside it.
    assert verdict.decision is U2Decision.ACCEPT_EXTERNAL_ONLY, (
        verdict.detail, logs)
    assert "unsafe { *(*p.as_ptr()).tag }" in lib
    # Both callers went back, the one off the aimed binary's stack included.
    assert lib.count("::core::unimplemented!()") == 2, lib
    assert "read(NonNull::dangling())" not in lib
    assert [str(u.fn_name) for u in recorded[0]] == ["make_a", "make_b"]


def test_negative_control_without_a_settler_the_reader_is_rejected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain.test_report import U2Decision
    verdict, lib, _recorded, _logs = _settle_gate(
        tmp_path, monkeypatch, bind_settler=False)
    assert verdict.decision is U2Decision.REJECT
    assert "a_made" in verdict.aborted
    assert lib.count("read(NonNull::dangling())") == 2


# ---- gdb names a trait-impl method without its trait (2026-10-18) ----------
#
# A native (gdb) crash site blamed a frame named `drop`: gdb prints a Drop
# impl's method as `<crate>::{impl#N}::drop`, which carries none of the
# destructor marks the sanitizer's demangled symbol does, so the skip that
# promotes the caller never fired. The linkage name keeps the trait. The
# invented crate's `release` drops a value whose destructor writes through a
# dangling pointer.

_DROP_LIB = """pub struct Holder {
    p: *mut u8,
}

impl Drop for Holder {
    fn drop(&mut self) {
        unsafe { core::ptr::write_volatile(self.p, 1) }
    }
}

pub fn release(n: usize) -> u8 {
    let h = Holder { p: n as *mut u8 };
    drop(h);
    0
}
"""

_DROP_TESTS = """#[test]
fn releases() {
    assert_eq!(dropper::release(16), 0);
}
"""


def _drop_capture(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, str]]:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("handles", "dropper")
                                     + "\n[lib]\ndoctest = false\n")
    (root / "src" / "lib.rs").write_text(_DROP_LIB)
    (root / "tests" / "releases.rs").write_text(_DROP_TESTS)
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / "dropper")
    env["RUST_BACKTRACE"] = "0"
    _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    return out, env


def test_a_gdb_site_skips_the_destructor_by_its_symbol(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from pipeline_core.adapters.gdb_backtrace import localize_crashed_binary
    from pipeline_core.application.reconstruct.crash_localizer import (
        CrashLocalizerDeps,
        localize_crashes,
    )
    from pipeline_core.domain.models import FnLocator
    assert shutil.which("gdb"), "premise: the crash localizer needs gdb"
    root = tmp_path / "d"
    out, env = _drop_capture(root, monkeypatch)
    raw: list[list[dict[str, object]]] = []

    def _gdb(*a: object, **k: object):
        got = localize_crashed_binary(*a, **k)  # type: ignore[arg-type]
        if got is not None:
            raw.append(got[1])
        return got

    def _no_symbol(*a: object, **k: object):
        got = localize_crashed_binary(*a, **k)  # type: ignore[arg-type]
        if got is None:
            return None
        sig, frames, addr = got
        return sig, [{k_: v for k_, v in f.items() if k_ != "symbol"}
                     for f in frames], addr

    logs: list[str] = []
    sites = localize_crashes(
        CrashLocalizerDeps(gdb_localize=_gdb, asan_sweep=None, log=logs.append),
        out, _CMD, root, env)
    assert sites, "no crash site localized:\n" + "\n".join(logs)
    drop_frames = [f for f in raw[0] if str(f.get("func", "")).endswith("::drop")
                   and "{impl" in str(f.get("func", ""))]
    assert drop_frames and "as core::ops::drop::Drop>::drop" in str(
        drop_frames[0].get("symbol")), (
        "premise: gdb prints the Drop impl's method without its trait and "
        f"the demangled linkage name carries it; frames: {raw[0][:4]}")
    assert sites[0].blamed_locator == FnLocator("src/lib.rs", "release"), (
        sites[0], logs)

    control = localize_crashes(
        CrashLocalizerDeps(gdb_localize=_no_symbol, asan_sweep=None,
                           log=lambda _m: None),
        out, _CMD, root, env)
    assert control and control[0].blamed_locator == FnLocator(
        "src/lib.rs", "drop"), "negative control: the printed name alone blames drop"


# --- the same failure, read from the test run alone ---------------------------
#
# The reference floor excuses a red test the original also fails only when the
# failure is the same (`domain.same_failure`, run `391506-3403a6a7`,
# 2026-10-22). It rests on two behaviours of cargo and libtest that only a
# real run shows: a harness that writes its report straight to the process's
# stderr (as trybuild does) is not in libtest's `---- NAME stdout ----` block,
# and the executable the capture's `Running` line names runs exactly that test
# when given `--exact NAME --test-threads=1`, with the report on its stderr.
# The crate is invented: `case()` decides which case of a two-case report
# mismatches.

_REPORT_LIB = """pub fn case() -> &'static str {
    CASE
}
"""

_REPORT_TEST = """use std::io::Write;

#[test]
fn report() {
    let mut err = std::io::stderr();
    writeln!(err, "case {} ... mismatch", reporter::case()).unwrap();
    panic!("1 of 2 cases failed");
}

#[test]
fn quiet() {}
"""


def _report_crate(root: Path, case: str) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(_CARGO_TOML.replace("handles", "reporter"))
    (root / "src" / "lib.rs").write_text(_REPORT_LIB.replace("CASE", f'"{case}"'))
    (root / "tests" / "report.rs").write_text(_REPORT_TEST)
    return root


def _report_run(root: Path, tag: str) -> tuple[str, dict[str, str]]:
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / tag)
    env["RUST_BACKTRACE"] = "0"
    _rc, out = run_cargo(CargoCli(), "cargo test --offline --no-fail-fast", root,
                         env, timeout=600)
    return out, env


def _signature(root: Path, tag: str):  # type: ignore[no-untyped-def]
    from pipeline_core.application.failure_reading import read_one
    from pipeline_core.domain.world_floor import failures_to_read
    out, env = _report_run(root, tag)
    todo = failures_to_read(None, out)
    assert [f.name for f in todo] == ["report"], out[-2000:]
    return out, read_one(todo[0], cwd=root, env=env, timeout=600, cached=False)


def test_premise_a_harness_report_on_stderr_is_not_in_the_libtest_block(
        tmp_path: Path) -> None:
    from pipeline_core.domain.test_results import split_test_stdout_sections
    out, sig = _signature(_report_crate(tmp_path / "a", "b"), "report-a")
    block = dict(split_test_stdout_sections(out))["report"]
    assert "1 of 2 cases failed" in block
    assert "case b ... mismatch" not in block
    assert sig is not None and "case b ... mismatch" in sig.text
    assert "test quiet" not in sig.text  # the alone run ran exactly one test


def test_the_same_failure_matches_and_a_moved_case_does_not(tmp_path: Path) -> None:
    from pipeline_core.domain.reference_floor import (
        ClusterReference,
        judge_against_reference,
    )
    from pipeline_core.domain.same_failure import FailedReading, ReferenceFailure
    _o, original = _signature(_report_crate(tmp_path / "orig", "b"), "report-orig")
    _s, same = _signature(_report_crate(tmp_path / "same", "b"), "report-same")
    _m, moved = _signature(_report_crate(tmp_path / "moved", "c"), "report-moved")
    pair = ("tests/report.rs", "report")
    ref = ClusterReference(cluster="c",
                           failures={pair: ReferenceFailure(original, original)})

    def judge(sig):  # type: ignore[no-untyped-def]
        return judge_against_reference(
            ref, frozenset(), FailedReading(pairs=frozenset({pair}),
                                            signatures={pair: sig}))
    assert same == original and judge(same).met
    assert moved != original and not judge(moved).met
    # Negative control: the name-only rule excused the moved case too.
    assert not (frozenset({pair}) - (ref.failed | ref.ignored))
