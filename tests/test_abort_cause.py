"""The typed abort cause: what an aborted test's own process announced, read
once and read first by the fault class.

Exposed by run `38934-2999cc94` (2026-09-26): 13 of 14 failing tests died
with the runtime's `has overflowed its stack` line in their own isolated
block, while the fault class was read off the detector's kind string alone.
Through the debugger that kind is `SIGSEGV`, which read as invalid memory:
the lifetime prescription, no cycle read, and a sanitizer rebuild to look
for an origin that does not exist. The block below is the recorded one
(`logs/prompts/0abad3bc…`, `test_backtrace_on_boxed_error`), rebuilt by the
real isolation reader and renderer. Negative controls: the same site with
no abort reading classes as invalid memory and escalates to the sanitizer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    localize_crashes,
)
from pipeline_core.domain.abort_cause import AbortCause, read_abort
from pipeline_core.domain.crash_site import FaultClass, fault_class, site_from_frames
from pipeline_core.domain.test_isolation import (
    read_single_test_run,
    render_isolated_section,
)
from pipeline_core.domain.test_outcome import abort_readings, outcomes_of

_NAME = "test_backtrace_on_boxed_error"
_OVERFLOW_PROCESS = (
    "running 1 test\n"
    f"test {_NAME} ... \n"
    "--- stderr ---\n"
    f"thread '{_NAME}' (618210) has overflowed its stack\n"
    "fatal runtime error: stack overflow, aborting")
_DESTRUCTOR_PROCESS = (
    "running 1 test\n"
    "test t_drop ... \n"
    "--- stderr ---\n"
    "thread 't_drop' panicked at library/core/src/panicking.rs:233:5:\n"
    "panic in a destructor during cleanup\n"
    "thread caused non-unwinding panic. aborting.")
_SILENT_PROCESS = "running 1 test\ntest t_sig ... "


def _capture(*processes: tuple[str, int, str]) -> str:
    tests = [read_single_test_run(n, rc, out, timed_out=False, timeout=60)
             for n, rc, out in processes]
    return (render_isolated_section(tests) + "\n--- stderr ---\n"
            "     Running tests/g.rs (target/debug/deps/g-0123)\n")


_CAP = _capture((_NAME, -6, _OVERFLOW_PROCESS),
                ("t_drop", -6, _DESTRUCTOR_PROCESS),
                ("t_sig", -11, _SILENT_PROCESS))


def test_each_aborted_test_carries_the_cause_its_own_block_states() -> None:
    got = {n: r.cause for n, r in abort_readings(_CAP).items()}
    assert got == {_NAME: AbortCause.STACK_EXHAUSTION,
                   "t_drop": AbortCause.PANIC_IN_PANIC,
                   "t_sig": AbortCause.SIGNAL}
    assert abort_readings(_CAP)["t_sig"].signal == 11
    out = outcomes_of(_CAP)
    assert out[_NAME].aborted and out[_NAME].abort is not None
    assert out[_NAME].abort.cause is AbortCause.STACK_EXHAUSTION


def test_a_block_with_no_announcement_and_no_exit_status_is_unknown() -> None:
    assert read_abort("determ-isolation: test process aborted").cause is (
        AbortCause.UNKNOWN)


def test_the_cause_outranks_the_bare_signal_and_the_sanitizer_outranks_both() -> None:
    over = read_abort(_OVERFLOW_PROCESS + "\n(exit status -6)")
    drop = read_abort(_DESTRUCTOR_PROCESS + "\n(exit status -6)")
    assert fault_class("SIGSEGV", over) is FaultClass.EXHAUSTION
    assert fault_class("SIGABRT", drop) is FaultClass.OTHER
    assert fault_class("AddressSanitizer: heap-use-after-free", drop) is (
        FaultClass.INVALID_MEMORY)
    # Negative control: the signal alone.
    assert fault_class("SIGSEGV") is FaultClass.INVALID_MEMORY
    assert fault_class("SIGABRT") is FaultClass.INVALID_MEMORY


def _frames(n: int) -> list[dict[str, object]]:
    cyc = [("src/a.rs", 10, "a::ring_one"), ("src/b.rs", 20, "b::ring_two")]
    return [{"file": f, "line": ln, "func": fn} for _ in range(n)
            for f, ln, fn in cyc]


def test_a_debugger_overflow_reads_its_cycle_through_the_cause() -> None:
    site = site_from_frames("tests/g.rs", _NAME, "SIGSEGV", _frames(8),
                            abort=abort_readings(_CAP)[_NAME])
    assert site.fault is FaultClass.EXHAUSTION and site.cycle is not None
    bare = site_from_frames("tests/g.rs", _NAME, "SIGSEGV", _frames(8))
    assert bare.fault is FaultClass.INVALID_MEMORY and bare.cycle is None


def _localize(capture: str) -> tuple[Any, list[str]]:
    calls: list[str] = []

    def gdb(exe: Path, root: Path, env: dict[str, str], **k: Any) -> Any:
        return ("SIGSEGV", _frames(8)) if k.get("test_filter") == _NAME else None

    def asan(*a: Any, **k: Any) -> Any:
        calls.append("asan")
        return None

    deps = CrashLocalizerDeps(gdb_localize=gdb, asan_sweep=asan,
                              log=lambda m: None)
    return localize_crashes(deps, capture, "cargo test", Path("/w"), lambda _w: {}), calls


def test_the_localizer_reads_the_overflow_and_skips_the_sanitizer() -> None:
    sites, calls = _localize(_capture((_NAME, -6, _OVERFLOW_PROCESS)))
    mine = [s for s in sites if s.test_name == _NAME]
    assert mine and mine[0].fault is FaultClass.EXHAUSTION
    assert calls == []


def test_negative_control_without_the_block_the_overflow_is_invalid_memory() -> None:
    silent = _capture((_NAME, -6, "running 1 test\n" f"test {_NAME} ... "))
    sites, calls = _localize(silent)
    mine = [s for s in sites if s.test_name == _NAME]
    assert mine and mine[0].fault is FaultClass.INVALID_MEMORY
    assert calls == ["asan"]
