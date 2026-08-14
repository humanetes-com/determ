"""Pins for the crash-localizer orchestration: gdb fast path, ASAN fallback
when gdb sees no native fault (dangling-pointer crashes are stack-layout
dependent and reproduce intermittently), and the "nothing localized" contract
— an empty result must NEVER read as health."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    localize_crashes,
)
from pipeline_core.domain.models import FnLocator

_SWEEP_WITH_CRASH = """\
    Running tests/test_a.rs (target/debug/deps/test_a-111)
running 2 tests
test alpha ... ok
test beta ... ok
test result: ok. 2 passed; 0 failed; 0 ignored
    Running tests/test_source.rs (target/debug/deps/test_source-222)
running 3 tests
test gamma ... ok
test test_io_source ...
"""

_SWEEP_CLEAN = """\
    Running tests/test_a.rs (target/debug/deps/test_a-111)
running 1 test
test alpha ... ok
test result: ok. 1 passed; 0 failed; 0 ignored
"""

_ASAN_OUT = """\
    Running tests/test_source.rs (target-asan/debug/deps/test_source-999)
running 3 tests
test test_io_source ... ==7==ERROR: AddressSanitizer: stack-use-after-return on address 0x7f
READ of size 8 at 0x7f thread T2
    #0 0x55a in <anyhow::Error as core::ops::deref::Deref>::deref /w/src/error.rs:560:27
    #1 0x55b in test_source::test_io_source /w/tests/test_source.rs:55:5
Address 0x7f is located in stack of thread T2 at offset 32 in frame
    #0 0x55c in <anyhow::Error>::construct_from_std::<E> /w/src/error.rs:142
"""


def _deps(gdb_result=None, asan_result=None, *, log=None):
    calls: dict[str, list] = {"gdb": [], "asan": []}

    def _gdb(exe, cwd, env, **kw):
        calls["gdb"].append(Path(exe))
        return gdb_result

    def _asan(cmd, cwd, env, **kw):
        calls["asan"].append(cmd)
        return asan_result

    deps = CrashLocalizerDeps(
        gdb_localize=_gdb,
        asan_sweep=(None if asan_result == "no-adapter" else _asan),
        log=(log if log is not None else (lambda _m: None)))
    return deps, calls


def _run(deps, sweep=_SWEEP_WITH_CRASH, root=Path("/w")):
    return localize_crashes(deps, sweep, "cargo test", root, {})


def test_no_crashed_binary_short_circuits() -> None:
    deps, calls = _deps(gdb_result=("SIGSEGV", []))
    assert _run(deps, sweep=_SWEEP_CLEAN) == ()
    assert calls["gdb"] == []  # never re-runs a healthy suite


def test_gdb_fast_path_localizes_only_the_crashed_binary() -> None:
    frames = [
        {"func": "??", "file": "", "line": 0},
        {"func": "anyhow::error::{impl#6}::drop", "file": "src/error.rs", "line": 612},
    ]
    deps, calls = _deps(gdb_result=("SIGSEGV", frames), asan_result="no-adapter")
    sites = _run(deps)
    assert len(sites) == 1
    assert [p.name for p in calls["gdb"]] == ["test_source-222"]
    site = sites[0]
    assert site.binary_label == "tests/test_source.rs"
    assert site.test_name == "test_io_source"   # the in-flight (dangling) test
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")
    assert calls["asan"] == []  # no sanitizer adapter available


def test_gdb_relative_exe_path_resolved_against_workspace() -> None:
    deps, calls = _deps(gdb_result=("SIGSEGV",
                                    [{"func": "a::f", "file": "src/x.rs", "line": 1}]),
                        asan_result="no-adapter")
    _run(deps, root=Path("/ws"))
    assert calls["gdb"] == [Path("/ws/target/debug/deps/test_source-222")]


def test_gdb_miss_falls_back_to_asan_and_blames_origin_frame() -> None:
    # gdb saw no native fault (layout-dependent crash) → ASAN, which reports
    # the ORIGIN stack: the fn that created the dead memory is the one to fix.
    logged: list[str] = []
    deps, calls = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT),
                        log=logged.append)
    sites = _run(deps)
    assert len(sites) == 1
    site = sites[0]
    assert site.blamed_locator == FnLocator("src/error.rs", "construct_from_std")
    assert [f.fn_name for f in site.frames] == ["deref"]     # faulting access
    assert site.binary_label == "tests/test_source.rs"
    assert site.test_name == "test_io_source"
    assert calls["asan"] == ["cargo test"]
    assert any("ASAN" in m for m in logged)


def test_asan_paths_relativized_to_workspace() -> None:
    deps, _ = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT))
    (site,) = _run(deps, root=Path("/w"))
    assert all(f.file_rel.startswith("src/") for f in site.all_frames)


def test_no_asan_adapter_means_gdb_only() -> None:
    deps, calls = _deps(gdb_result=None, asan_result="no-adapter")
    assert _run(deps) == ()
    assert calls["asan"] == []


def test_asan_without_report_returns_nothing() -> None:
    # ASAN ran but produced no error (crash not reproducible under it):
    # empty result — the caller must treat it as "unlocalized", not health.
    deps, _ = _deps(gdb_result=None, asan_result=(0, "all tests passed"))
    assert _run(deps) == ()


def test_gdb_frames_outside_crate_yield_no_site() -> None:
    # A backtrace with only std/harness frames localizes nothing in OUR code;
    # returning a frameless site would blame an empty locator.
    deps, _ = _deps(gdb_result=("SIGSEGV", [
        {"func": "std::rt::lang_start", "file": "library/std/src/rt.rs", "line": 1}]))
    assert _run(deps) == ()


def test_memory_fault_escalates_to_asan_even_when_gdb_hit() -> None:
    # THE correctness rule: for a memory fault the faulting frame is usually
    # an innocent READER of memory some other fn fabricated. gdb blames the
    # reader (`drop`); only ASAN reports the origin (`construct_from_std`),
    # so its site must supersede — otherwise the model is handed correct code
    # to "fix" and the crash survives.
    gdb_frames = [{"func": "anyhow::error::{impl#6}::drop",
                   "file": "src/error.rs", "line": 612}]
    deps, calls = _deps(gdb_result=("SIGSEGV", gdb_frames),
                        asan_result=(101, _ASAN_OUT))
    (site,) = _run(deps)
    assert calls["asan"] == ["cargo test"]
    assert site.blamed_locator == FnLocator("src/error.rs", "construct_from_std")


def test_non_memory_signal_does_not_escalate() -> None:
    # An OOM kill has no "origin of invalid memory" to find — the backtrace is
    # the whole story, so the expensive sanitized sweep is not run.
    deps, calls = _deps(gdb_result=("SIGKILL",
                                    [{"func": "a::f", "file": "src/x.rs", "line": 1}]),
                        asan_result=(101, _ASAN_OUT))
    (site,) = _run(deps)
    assert calls["asan"] == []
    assert site.blamed_locator == FnLocator("src/x.rs", "f")


def test_gdb_site_survives_when_sanitizer_finds_nothing() -> None:
    # ASAN could not reproduce (or no nightly): the weaker gdb evidence is
    # still evidence and must not be thrown away.
    deps, _ = _deps(gdb_result=("SIGSEGV",
                                [{"func": "a::drop", "file": "src/error.rs", "line": 612}]),
                    asan_result=(0, "all tests passed"))
    (site,) = _run(deps)
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")


def test_max_binaries_caps_gdb_reruns() -> None:
    sweep = _SWEEP_WITH_CRASH
    for i in range(4):
        sweep += (f"    Running tests/t{i}.rs (target/debug/deps/t{i}-{i})\n"
                  f"running 1 test\ntest c{i} ...\n")
    deps, calls = _deps(gdb_result=("SIGSEGV",
                                    [{"func": "a::f", "file": "src/x.rs", "line": 1}]),
                        asan_result="no-adapter")
    localize_crashes(deps, sweep, "cargo test", Path("/w"), {}, max_binaries=2)
    assert len(calls["gdb"]) == 2


def test_origin_evidence_suppresses_downstream_reader_blame() -> None:
    # One dangling pointer kills every binary that dereferences it. Once ASAN
    # names the origin, the other memory-fault backtraces are readers of that
    # same corruption — blaming their faulting frames would hand the model
    # correct fns to "fix" (live: `fmt` and `as_ref` blamed alongside the real
    # culprit `construct_from_std`).
    sweep = _SWEEP_WITH_CRASH + (
        "    Running tests/test_convert.rs (target/debug/deps/test_convert-333)\n"
        "running 1 test\ntest test_convert ...\n")
    logged: list[str] = []
    deps, _ = _deps(gdb_result=("SIGSEGV",
                                [{"func": "a::fmt", "file": "src/error.rs", "line": 572}]),
                    asan_result=(101, _ASAN_OUT), log=logged.append)
    sites = localize_crashes(deps, sweep, "cargo test", Path("/w"), {})
    assert len(sites) == 1
    assert sites[0].blamed_locator == FnLocator("src/error.rs", "construct_from_std")
    assert any("not blamed" in m for m in logged)


def test_non_memory_crash_survives_alongside_origin_site() -> None:
    # A different failure class is independent evidence, not a downstream
    # reader — it keeps its own blame.
    sweep = _SWEEP_WITH_CRASH + (
        "    Running tests/test_hang.rs (target/debug/deps/test_hang-444)\n"
        "running 1 test\ntest solo ...\n")

    def _gdb(exe, cwd, env, **kw):
        if "test_hang" in str(exe):
            return ("SIGKILL", [{"func": "a::spin", "file": "src/x.rs", "line": 9}])
        return None

    deps = CrashLocalizerDeps(
        gdb_localize=_gdb,
        asan_sweep=lambda *_a, **_k: (101, _ASAN_OUT),
        log=lambda _m: None)
    sites = localize_crashes(deps, sweep, "cargo test", Path("/w"), {})
    blamed = {s.blamed_locator for s in sites}
    assert FnLocator("src/error.rs", "construct_from_std") in blamed
    assert FnLocator("src/x.rs", "spin") in blamed


_ASAN_OUT_NO_CRATE_FRAME = """\
    Running tests/test_downcast.rs (target-asan/debug/deps/test_downcast-555)
running 2 tests
test test_drop ... ==7==ERROR: AddressSanitizer: heap-use-after-free on address 0x50
WRITE of size 8 at 0x50 thread T0
    #0 0x1 in core::sync::atomic::AtomicUsize::fetch_sub /rustc/lib/core/sync/atomic.rs:3060
    #1 0x2 in test_downcast::test_drop /w/tests/test_downcast.rs:85:1
freed by thread T0 here:
    #0 0x3 in free /rustc/asan/asan_malloc_linux.cpp:52
    #1 0x4 in test_downcast::test_drop /w/tests/test_downcast.rs:83:9
SUMMARY: AddressSanitizer: heap-use-after-free /rustc/lib/core/sync/atomic.rs
"""


def test_origin_unattributed_site_survives_with_zero_crate_frames() -> None:
    # The frame-blind crash class (run `635043-5f7c61f5`, 2026-08-14): a
    # refcount error planted in construct/vtable code surfaces in std's
    # `Arc::drop` at the test's own drop sites, so EVERY stack — access, free,
    # allocation — filters to nothing in-crate. The old `all_frames`-only
    # return dropped the site entirely, the driver saw no evidence object,
    # and the chain-derived synthesis it keys on could never arm. The site
    # must survive: it names the binary, the in-flight test and carries the
    # raw report, and "an origin-capable detector looked and attributed
    # nothing" is itself the finding.
    logged: list[str] = []
    deps, _ = _deps(gdb_result=None,
                    asan_result=(101, _ASAN_OUT_NO_CRATE_FRAME),
                    log=logged.append)
    (site,) = _run(deps)
    assert site.origin_unattributed is True
    assert site.all_frames == ()
    assert site.blamed_locator is None       # convicts nobody, as before
    assert site.binary_label == "tests/test_downcast.rs"
    assert site.test_name == "test_drop"
    assert "heap-use-after-free" in site.raw_report
    assert any("blaming NOBODY" in m for m in logged)
