"""Pins for the crash-localizer orchestration: gdb fast path, ASAN fallback
when gdb sees no native fault (dangling-pointer crashes are stack-layout
dependent and reproduce intermittently), and the "nothing localized" contract
— an empty result must NEVER read as health."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    localize_crashes,
    project_dir_of_tree,
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
    return localize_crashes(deps, sweep, "cargo test", root, lambda _w: {})


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
    localize_crashes(deps, sweep, "cargo test", Path("/w"), lambda _w: {}, max_binaries=2)
    assert len(calls["gdb"]) == 2


def test_a_sanitized_site_supersedes_only_the_gdb_site_of_its_own_binary() -> None:
    """Each test binary is its own process and its own heap. The sanitized
    site for `tests/test_source.rs` replaces gdb's backtrace of THAT binary
    (it carries the origin gdb cannot see) and says nothing about
    `tests/test_convert.rs`, which the sanitizer did not report: that gdb
    site is kept with its own blame.

    Negative control, the pre-fix consolidation: "once one origin is known,
    the other memory-fault backtraces are downstream readers of the same
    corruption" dropped every other binary's site — on run
    `1577437-c93d5c26` that premise turned a sweep with three
    AddressSanitizer reports into one nobody site."""
    sweep = _SWEEP_WITH_CRASH + (
        "    Running tests/test_convert.rs (target/debug/deps/test_convert-333)\n"
        "running 1 test\ntest test_convert ...\n")
    logged: list[str] = []
    deps, _ = _deps(gdb_result=("SIGSEGV",
                                [{"func": "a::fmt", "file": "src/error.rs", "line": 572}]),
                    asan_result=(101, _ASAN_OUT), log=logged.append)
    sites = localize_crashes(deps, sweep, "cargo test", Path("/w"), lambda _w: {})
    by_label = {s.binary_label: s for s in sites}
    assert set(by_label) == {"tests/test_source.rs", "tests/test_convert.rs"}
    assert by_label["tests/test_source.rs"].blamed_locator == FnLocator(
        "src/error.rs", "construct_from_std")
    assert by_label["tests/test_source.rs"].origin_capable, (
        "the sanitized site, not gdb's, stands for the reported binary")
    assert by_label["tests/test_convert.rs"].blamed_locator == FnLocator(
        "src/error.rs", "fmt")
    assert not by_label["tests/test_convert.rs"].origin_capable
    assert any("superseded by the sanitized site of the same binary" in m
               for m in logged)
    assert not any("same corruption read downstream" in m for m in logged), (
        "negative control: the one-heap sentence is gone")


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
    sites = localize_crashes(deps, sweep, "cargo test", Path("/w"), lambda _w: {})
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


_ASAN_SEGV = """\
    Running tests/test_source.rs (target-asan/debug/deps/test_source-999)
running 1 test
test test_io_source ... ==1==ERROR: AddressSanitizer: SEGV on unknown address 0x10
READ of size 8 at 0x10 thread T0
    #0 0x1 in core::fmt::write /rustc/library/core/src/fmt/mod.rs:1
    #1 0x2 in test_source::test_io_source /w/tests/test_source.rs:19:1
SUMMARY: AddressSanitizer: SEGV
"""

_TRACED_GDB = (
    "SIGSEGV",
    [
        {"func": "crate::read", "file": "src/lib.rs", "line": 3,
         "ptr_args": [[0x8, 16]]},
        {"func": "crate::make", "file": "src/lib.rs", "line": 9,
         "ptr_args": []},
    ],
    0x8,
)


def test_a_printed_origin_supersedes_a_traced_site() -> None:
    """An invalid-memory fault runs the sanitizer even after the debugger
    traced an origin. A report that printed a free stack replaces that
    trace for the same binary."""
    logged: list[str] = []
    deps, calls = _deps(gdb_result=_TRACED_GDB, asan_result=(101, _ASAN_OUT),
                        log=logged.append)
    (site,) = _run(deps)
    assert calls["asan"] == ["cargo test"]
    assert site.origin_stack_lines > 0
    assert site.origin_capable
    assert site.blamed_locator == FnLocator("src/error.rs", "construct_from_std")
    assert any("superseded by the sanitized site of the same binary" in m
               for m in logged)


def test_a_report_with_no_origin_leaves_the_traced_site_standing() -> None:
    """A SEGV that printed no free stack does not refute the trace."""
    logged: list[str] = []
    deps, calls = _deps(gdb_result=_TRACED_GDB, asan_result=(101, _ASAN_SEGV),
                        log=logged.append)
    (site,) = _run(deps)
    assert calls["asan"] == ["cargo test"]
    assert site.origin_traced
    assert not site.origin_capable
    assert site.blamed_locator == FnLocator("src/lib.rs", "make")
    assert any("printed no origin stack" in m for m in logged)


def test_a_report_with_no_origin_supersedes_an_untraced_site() -> None:
    """The trace is what stands, not any debugger site: an untraced
    SIGSEGV names only its innermost frame, so the sanitized site of the
    same binary replaces it even when that report printed no origin.
    Negative control: the traced frames of the previous test keep gdb."""
    untraced = ("SIGSEGV", [
        {"func": "crate::read", "file": "src/lib.rs", "line": 3},
        {"func": "crate::make", "file": "src/lib.rs", "line": 9}])
    deps, calls = _deps(gdb_result=untraced, asan_result=(101, _ASAN_SEGV))
    (site,) = _run(deps)
    assert calls["asan"] == ["cargo test"]
    assert site.origin_capable and not site.origin_traced
    traced_deps, _ = _deps(gdb_result=_TRACED_GDB, asan_result=(101, _ASAN_SEGV))
    (traced,) = _run(traced_deps)
    assert traced.origin_traced and not traced.origin_capable


def test_a_segv_the_debugger_did_not_report_is_kept() -> None:
    deps, _ = _deps(gdb_result=None, asan_result=(101, _ASAN_SEGV))
    (site,) = _run(deps)
    assert site.origin_capable
    assert site.origin_stack_lines == 0
    assert site.binary_label == "tests/test_source.rs"


_ASAN_TEST_ONLY_ORIGIN = """\
    Running tests/test_source.rs (target-asan/debug/deps/test_source-999)
running 3 tests
test test_io_source ... ==7==ERROR: AddressSanitizer: heap-use-after-free on address 0x7f
READ of size 8 at 0x7f thread T2
    #0 0x55a in <anyhow::Error as core::ops::deref::Deref>::deref /w/src/error.rs:560:27
freed by thread T2 here:
    #0 0x55b in free /rustc/llvm/compiler-rt/lib/asan/asan_malloc_linux.cpp:51:3
    #1 0x55c in <alloc::sync::Arc<T> as core::ops::drop::Drop>::drop /rustc/xyz/library/alloc/src/sync.rs:2874:18
    #2 0x55d in test_source::test_io_source /w/tests/test_source.rs:83:5
"""

_ASAN_RAW_ADDRESS_ORIGIN = """\
    Running tests/test_source.rs (target-asan/debug/deps/test_source-999)
running 3 tests
test test_io_source ... ==7==ERROR: AddressSanitizer: heap-use-after-free on address 0x7f
READ of size 8 at 0x7f thread T2
    #0 0x55a in <anyhow::Error as core::ops::deref::Deref>::deref /w/src/error.rs:560:27
freed by thread T2 here:
    #0 0x55b (/w/target-asan/debug/deps/test_source-999+0x1234)
    #1 0x55c (/w/target-asan/debug/deps/test_source-999+0x2222)
"""


def test_nobody_verdict_names_the_starvation_symbolized_but_test_only() -> None:
    """A NOBODY verdict must say WHICH starvation it is. Here every origin
    frame symbolizes and the only workspace frames are the test's own — the
    shape measured on `asan-20260816T194054947258.txt` (the test's free is
    the legitimate one; the crate's defect is on the access/retention side).
    The verdict line answers that without an archive dig."""
    logged: list[str] = []
    deps, _ = _deps(gdb_result=None, asan_result=(101, _ASAN_TEST_ONLY_ORIGIN),
                    log=logged.append)
    (site,) = _run(deps)
    assert site.origin_unattributed
    nobody = [m for m in logged if "blaming NOBODY" in m]
    assert nobody and "3 of 3 frame(s) symbolized" in nobody[0]
    assert "test-file frame(s) (oracle" in nobody[0]
    assert "access/retention" in nobody[0]


def test_nobody_verdict_names_the_starvation_raw_addresses() -> None:
    """Negative control for the line above: an origin stack of bare addresses
    (no `in SYMBOL FILE:LINE`) parses to zero frames — the verdict must say
    NO frame symbolized and point at the symbolizer, not at the crate."""
    logged: list[str] = []
    deps, _ = _deps(gdb_result=None, asan_result=(101, _ASAN_RAW_ADDRESS_ORIGIN),
                    log=logged.append)
    (site,) = _run(deps)
    assert site.origin_unattributed
    nobody = [m for m in logged if "blaming NOBODY" in m]
    assert nobody and "NO frame symbolized" in nobody[0]
    assert "llvm-symbolizer" in nobody[0]


def test_asan_uaf_blames_the_conversion_named_on_the_test_line(tmp_path: Path) -> None:
    """The plan's sentence at the I/O contract: the localizer reads line 27
    of the crashing test off disk and blame is `into_boxed_dyn_error`.

    Negative control is the same report against a workspace with no test
    file — blame stays `drop`, which is what every existing localizer test
    (workspace `/w`) still asserts.
    """
    fixture = (
        Path(__file__).parent / "fixtures" / "crash"
        / "uaf_into_boxed_dyn_error_1618155_2026-08-30.txt"
    ).read_text()
    live_root = "/home/user/determ/tmp/anyhow/.skeleton-filled/stable"
    report = fixture.replace(live_root, str(tmp_path))
    asan_out = (
        "    Running tests/behaviour_stable_gen_1.rs "
        "(target-asan/debug/deps/behaviour_stable_gen_1-999)\n"
        "running 1 test\n"
        "test behaviour_probe_stable_trait_object_identity_0 ...\n"
        + report
    )

    empty, _ = _deps(gdb_result=None, asan_result=(101, asan_out))
    (without,) = _run(empty, root=tmp_path)
    assert without.blamed_locator == FnLocator("src/error.rs", "drop"), (
        "negative control: no test file on disk, recovery cannot fire")

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "behaviour_stable_gen_1.rs").write_text(
        "\n" * 26 + "let boxed = err.into_boxed_dyn_error();\n")
    deps, _ = _deps(gdb_result=None, asan_result=(101, asan_out))
    (site,) = _run(deps, root=tmp_path)
    assert site.blamed_locator == FnLocator("src/error.rs", "into_boxed_dyn_error")



# ---- one site per crashed binary: the live sweeps of run 1577437-c93d5c26 ----
# `logs/asan/asan-20260906T221545829298.txt` (iter 4) and
# `…T204443251879.txt` (iter 2), distilled to the lines the two parsers read
# (`Running` labels, per-test verdict lines and trailers, every `ERROR:`
# header, the AddressSanitizer stacks, leak reports cut to header + four
# frames + SUMMARY). `parse_asan_reports`, `parse_binary_results` and
# `localize_crashes` were checked to return the same values on the excerpt
# and on the archive before the excerpt was committed.
_FIXTURES = Path(__file__).parent / "fixtures" / "crash"
_SWEEP_ITER4 = _FIXTURES / "sweep_two_reports_segv_and_uaf_1577437_2026-09-06_iter4.txt"
_SWEEP_ITER2 = _FIXTURES / "sweep_three_reports_1577437_2026-09-06_iter2.txt"
_LIVE_ROOT = "/home/user/determ/tmp/anyhow/.skeleton-filled/stable"

# The crashing tests' source, copied from the filled tree at the archive's
# time so the test does not read the tree. Line numbers are the ones the
# stacks cite: `tests/error_gen_3.rs` 29-33, `tests/test_downcast.rs`
# 14-21, `tests/test_context.rs` 121-124.
_ERROR_GEN_3 = "\n" * 28 + """\
fn test_reallocate_into_boxed_dyn_error_without_backtrace_preserves_std_error_trait() {
    use std::error::Error as StdError;
    let err = Error::new(std::io::Error::new(std::io::ErrorKind::Other, "io"));
    let boxed = err.reallocate_into_boxed_dyn_error_without_backtrace();
    assert_eq!(boxed.to_string(), "io");
    assert!(boxed.source().is_none());
}
"""
_TEST_DOWNCAST = "\n" * 13 + """\
fn test_downcast() {
    assert_eq!(
        "oh no!",
        bail_literal().unwrap_err().downcast::<&str>().unwrap(),
    );
    assert_eq!(
        "oh no!",
        bail_fmt().unwrap_err().downcast::<String>().unwrap(),
    );
}
"""
_TEST_CONTEXT = "\n" * 120 + """\
fn test_downcast_high() {
    let (err, dropped) = make_chain();

    let err = err.downcast::<HighLevel>().unwrap();
    assert!(!dropped.high.get());
}
"""


# The anchor file's declarations, which the localizer reads from the tree
# under test (`_declared_reader`) to tell a crate call from std's. Bodies are
# irrelevant to the read: only the fn names it declares are counted.
_SRC_ERROR = """\
impl Error {
    pub fn new<E>(error: E) -> Self { unimplemented!() }
    pub fn downcast<E>(self) -> Result<E, Self> { unimplemented!() }
    pub fn into_boxed_dyn_error(self) -> Box<dyn StdError> { unimplemented!() }
    pub fn reallocate_into_boxed_dyn_error_without_backtrace(self) -> Box<dyn StdError> {
        unimplemented!()
    }
}
impl Drop for Error {
    fn drop(&mut self) { unimplemented!() }
}
"""


def _live_sweep(fixture: Path, root: Path, *, with_tests: bool) -> str:
    text = fixture.read_text().replace(_LIVE_ROOT, str(root))
    if with_tests:
        tests = root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "error_gen_3.rs").write_text(_ERROR_GEN_3)
        (tests / "test_downcast.rs").write_text(_TEST_DOWNCAST)
        (tests / "test_context.rs").write_text(_TEST_CONTEXT)
        src = root / "src"
        src.mkdir(exist_ok=True)
        (src / "error.rs").write_text(_SRC_ERROR)
    return text


def test_two_reports_become_two_sites_neither_of_them_nobody(tmp_path: Path) -> None:
    """The plan's sentence on the iter-4 archive: two AddressSanitizer
    reports, two sites. The SEGV site names
    `reallocate_into_boxed_dyn_error_without_backtrace` by
    `tests/error_gen_3.rs:32`; the use-after-free site names `downcast` by
    `tests/test_downcast.rs:21`. Neither is `origin_unattributed`, so
    chain-derived synthesis has no precondition to arm on.

    Negative control: the same sweep against a workspace with no test
    files. Report 1 is then NOBODY (an empty origin) and report 2 blames
    `drop` — the two verdicts the shipped code produced, minus the first
    of them being the sweep's ONLY site."""
    sweep = _live_sweep(_SWEEP_ITER4, tmp_path, with_tests=False)
    logged: list[str] = []
    deps, calls = _deps(gdb_result=None, asan_result=(1, sweep),
                        log=logged.append)
    before = localize_crashes(deps, sweep, "cargo test", tmp_path, lambda _w: {})
    assert [(s.binary_label, s.blamed_locator) for s in before] == [
        ("tests/error_gen_3.rs", None),
        ("tests/test_downcast.rs", FnLocator("src/error.rs", "drop"))], (
        "negative control: without the test source, report 1 is nobody and "
        "report 2 is the destructor")
    assert before[0].origin_unattributed
    nobody = [m for m in logged if "blaming NOBODY" in m]
    assert len(nobody) == 1
    assert "carries no origin stack (AddressSanitizer: SEGV)" in nobody[0]
    assert "llvm-symbolizer" not in nobody[0], (
        "a SEGV prints no free/alloc stack; that is not a symbolizer fault")

    sweep = _live_sweep(_SWEEP_ITER4, tmp_path, with_tests=True)
    deps, calls = _deps(gdb_result=None, asan_result=(1, sweep))
    sites = localize_crashes(deps, sweep, "cargo test", tmp_path, lambda _w: {})
    assert calls["asan"] == ["cargo test"], "one sanitized sweep, read whole"
    assert len(sites) == 2
    segv, uaf = sites
    assert segv.binary_label == "tests/error_gen_3.rs"
    assert segv.test_name == (
        "test_reallocate_into_boxed_dyn_error_without_backtrace_preserves_std_error_trait")
    assert segv.blamed is not None
    assert segv.blamed.fn_name == "reallocate_into_boxed_dyn_error_without_backtrace"
    assert segv.blamed.file_rel == "src/error.rs"
    assert segv.blamed.named_by == ("tests/error_gen_3.rs", 32)
    assert uaf.binary_label == "tests/test_downcast.rs"
    assert uaf.test_name == "test_downcast"
    assert uaf.blamed is not None
    assert uaf.blamed.fn_name == "downcast"
    assert uaf.blamed.named_by == ("tests/test_downcast.rs", 21)
    assert not any(s.origin_unattributed for s in sites)
    assert "named by tests/error_gen_3.rs:32" in segv.render_evidence()


def test_three_reports_become_three_sites(tmp_path: Path) -> None:
    """The iter-2 archive: `tests/error_gen_3.rs` SEGV, `tests/test_context.rs`
    and `tests/test_downcast.rs` heap-use-after-free — three sites, each
    joined to its own binary's in-flight test by label. The test_context
    site recovers `downcast` from `err.downcast::<HighLevel>().unwrap()` at
    line 124: the turbofish is a call."""
    sweep = _live_sweep(_SWEEP_ITER2, tmp_path, with_tests=True)
    deps, _ = _deps(gdb_result=None, asan_result=(1, sweep))
    sites = localize_crashes(deps, sweep, "cargo test", tmp_path, lambda _w: {})
    assert [(s.binary_label, s.test_name) for s in sites] == [
        ("tests/error_gen_3.rs",
         "test_reallocate_into_boxed_dyn_error_without_backtrace_preserves_std_error_trait"),
        ("tests/test_context.rs", "test_downcast_high"),
        ("tests/test_downcast.rs", "test_downcast")]
    blamed = [s.blamed for s in sites]
    assert all(b is not None for b in blamed)
    assert [b.fn_name for b in blamed if b] == [
        "reallocate_into_boxed_dyn_error_without_backtrace", "downcast", "downcast"]
    assert blamed[1] is not None
    assert blamed[1].named_by == ("tests/test_context.rs", 124)
    assert not any(s.origin_unattributed for s in sites)


def test_a_gdb_site_for_a_binary_the_sanitizer_did_not_report_is_kept(tmp_path: Path) -> None:
    """The iter-4 shape exactly: three binaries crashed, two of them with an
    AddressSanitizer report, and `tests/test_context.rs` aborted on a panic
    in a destructor with no report at all. gdb's untraced SIGSEGV for
    `tests/error_gen_3.rs` is superseded by that label's sanitized site
    even though the SEGV printed no free stack: an untraced debugger site
    names only its innermost frame. The use-after-free replaces gdb for
    its own binary. The SIGABRT for `tests/test_context.rs` survives as the third site — it is
    not "a downstream reader" of a fault in another process."""
    sweep = _live_sweep(_SWEEP_ITER4, tmp_path, with_tests=True)

    def _gdb(exe, cwd, env, **kw):
        if "error_gen_3" in str(exe):
            return ("SIGSEGV", [
                {"func": "drop", "file": "src/error.rs", "line": 630},
                {"func": "reallocate_into_boxed_dyn_error_without_backtrace",
                 "file": "src/error.rs", "line": 610}])
        if "test_context" in str(exe):
            return ("SIGABRT", [
                {"func": "context_drop_rest", "file": "src/error.rs", "line": 900}])
        return None

    deps = CrashLocalizerDeps(gdb_localize=_gdb,
                              asan_sweep=lambda *_a, **_k: (1, sweep),
                              log=lambda _m: None)
    sites = localize_crashes(deps, sweep, "cargo test", tmp_path, lambda _w: {})
    by_label = {s.binary_label: s for s in sites}
    assert set(by_label) == {
        "tests/error_gen_3.rs", "tests/test_downcast.rs", "tests/test_context.rs"}
    segv = by_label["tests/error_gen_3.rs"]
    assert segv.origin_capable, (
        "the sanitized site replaced gdb's untraced site for the same binary")
    assert segv.origin_stack_lines == 0
    assert segv.blamed_locator == FnLocator(
        "src/error.rs", "reallocate_into_boxed_dyn_error_without_backtrace")
    uaf = by_label["tests/test_downcast.rs"]
    assert uaf.origin_capable and uaf.origin_stack_lines > 0
    assert uaf.blamed_locator == FnLocator("src/error.rs", "downcast")
    kept = by_label["tests/test_context.rs"]
    assert not kept.origin_capable and kept.signal == "SIGABRT"
    assert kept.blamed_locator == FnLocator("src/error.rs", "context_drop_rest")


# A repeat of the same crashes reuses the sites when every crashed test has
# a recorded executed set and those bodies are unchanged. The shapes are
# invented: `test_io_source` is the in-flight test of the fixture capture.

_ENTERED_SRC = "fn fabricate() {\n    made\n}\n\nfn borrow() {\n    read\n}\n"
_ENTERED = frozenset({
    FnLocator("src/made.rs", "fabricate"),
    FnLocator("src/made.rs", "borrow"),
})


def _entered_tree(tmp_path: Path, text: str = _ENTERED_SRC) -> None:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "made.rs").write_text(text)


def _entered(mapping: dict[str, frozenset | None]):
    def executed(name: str):
        return mapping.get(name)
    return executed


def test_a_repeat_on_unchanged_executed_bodies_skips_the_sanitizer(
        tmp_path: Path) -> None:
    _entered_tree(tmp_path)
    deps, calls = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT))
    executed = _entered({"test_io_source": _ENTERED})
    first = localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    assert calls["asan"] == ["cargo test"]
    assert calls["gdb"]
    second = localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    assert second is first
    assert calls["asan"] == ["cargo test"]
    assert len(calls["gdb"]) == 1


def test_control_a_changed_executed_body_runs_the_sanitizer_again(
        tmp_path: Path) -> None:
    text = (
        "fn fabricate() {\n    made\n}\n\n"
        "fn borrow() {\n    read\n}\n\nfn borrow() {\n    other\n}\n"
    )
    _entered_tree(tmp_path, text)
    deps, calls = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT))
    executed = _entered({"test_io_source": _ENTERED})
    localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    path = tmp_path / "src" / "made.rs"
    path.write_text(text.replace("other", "other2"))
    localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    assert len(calls["asan"]) == 2


def test_control_a_missing_executed_set_runs_the_sanitizer_again(
        tmp_path: Path) -> None:
    _entered_tree(tmp_path)
    deps, calls = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT))
    executed = _entered({})
    localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    assert len(calls["asan"]) == 2


def test_control_an_empty_executed_set_runs_the_sanitizer_again(
        tmp_path: Path) -> None:
    _entered_tree(tmp_path)
    deps, calls = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT))
    executed = _entered({"test_io_source": frozenset()})
    localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    assert len(calls["asan"]) == 2


def test_a_change_outside_the_executed_set_reuses_the_sites(
        tmp_path: Path) -> None:
    _entered_tree(tmp_path)
    (tmp_path / "src" / "side.rs").write_text("fn side() {\n    a\n}\n")
    deps, calls = _deps(gdb_result=None, asan_result=(101, _ASAN_OUT))
    executed = _entered({"test_io_source": _ENTERED})
    first = localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    (tmp_path / "src" / "side.rs").write_text("fn side() {\n    b\n}\n")
    second = localize_crashes(
        deps, _SWEEP_WITH_CRASH, "cargo test", tmp_path, lambda _w: {},
        executed=executed)
    assert second is first
    assert calls["asan"] == ["cargo test"]


def test_a_filled_cluster_tree_reads_the_project_store(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    tree = project / ".skeleton-filled" / "stable"
    tree.mkdir(parents=True)
    assert project_dir_of_tree(tree) == project
    assert project_dir_of_tree(project) == project


def test_the_arc_double_drop_stays_nobody_with_the_test_source_on_disk(tmp_path: Path) -> None:
    """Negative control for the access-side recovery: the family licence is
    the gate. An Arc double-drop (run `1064759-0f23fb68`) has neither READ
    mark and no in-crate destructor on its free stack, so the test file on
    disk is never read even though it calls a family member."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_downcast.rs").write_text(
        "\n" * 82 + "    let b = err.into_boxed_dyn_error();\n"
        "    drop(flag);\n    assert!(dropped.all());\n")
    out = _ASAN_OUT_NO_CRATE_FRAME.replace("/w/", str(tmp_path) + "/")
    deps, _ = _deps(gdb_result=None, asan_result=(101, out))
    (site,) = _run(deps, root=tmp_path)
    assert site.origin_unattributed
    assert site.blamed_locator is None
