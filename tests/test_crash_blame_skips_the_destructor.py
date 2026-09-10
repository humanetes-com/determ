"""The frame that FREED a resource is not the frame that owns the bug.

THE CLASS. On a use-after-free the `freed by` stack's innermost in-crate frame
is the destructor by construction — freeing is what a destructor is for — and
the defect is always that some other body kept or handed out a second owner.
`CrashSite.blamed` took that innermost origin frame, so every rewrite it aimed
went to a correct body.

THE RECORDED INSTANCE, run `1618155-b60a8c4c` (2026-08-30, cluster `stable`),
report `logs/asan/asan-20260830T031254431377.txt`, whose first error block is
checkpointed under `tests/fixtures/crash/` and driven here through the real
`parse_asan_report` and `relativize_frame_files`. Twelve tests were unmeasurable behind
`heap-use-after-free` in five binaries. The READ stack holds no `src/` frame at
all; the `freed by` stack is `<anyhow::Error as core::ops::drop::Drop>::drop`
at `src/error.rs:706`, called from the crashing test's line 27,
`let boxed = err.into_boxed_dyn_error();`. The allocation is freed by the
conversion and read by the `Box` the conversion returned. `into_boxed_dyn_error`
takes `self` by value and never neutralises it before handing the pointer to
`object_boxed`; `Error::drop` is a correct destructor.

WHAT THE WRONG BLAME COST. The layout retrigger spent 2 of a cap of 2 on that
blame, the cap is per-blame and outlives the process, and the crash-reachability
work then put `drop` at ranks 0-2 with the fault as its instruction — the model
rewrote the body, 14 fns landed, and iteration 2 read `pass=101 improved=False`,
`cashed=0`, same five binaries crashed.

WITHOUT the test source line, skipping the destructor leaves no in-crate frame
and last-resort blame stays `drop` (the recorded walk
`tests/walks/test_walk_mech_budget_lockout.py` pins that empty aim is worse).
WITH the recorded line and the READ stack's `Box<dyn Error + Sync + Send>`
wrapping `ErrorImpl`, blame is `into_boxed_dyn_error`.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from pipeline_core.domain.crash_site import (
    CONVERSION_FAMILY,
    AsanReport,
    CrashFrame,
    CrashSite,
    SourceWindow,
    authorable_frames,
    blamed_locators,
    conversion_family_from_access,
    first_test_file_after_destructor,
    first_test_file_on_access,
    non_destructor_frames,
    parse_asan_report,
    parse_asan_reports,
    prefer_non_destructor,
    recover_owner_from_caller_source,
    relativize_frame_files,
    site_from_frames,
)
from pipeline_core.domain.language_driver import using_driver
from pipeline_core.domain.models import FnLocator
from pipeline_core.prompts.language_catalog import catalog_driver


@pytest.fixture(autouse=True)
def _catalog_driver() -> Iterator[None]:
    """This module pins the catalog destructor skip and conversion family."""
    with using_driver(catalog_driver()):
        yield


#: The run's own report, first error block, byte-for-byte. Driven through the
#: real parser and the real relativizer so this pins the LIVE path and not a
#: transcription of it — the frame lists below were verified equal to what
#: `parse_asan_report` returns for it.
REPORT = (Path(__file__).parent / "fixtures" / "crash"
          / "uaf_into_boxed_dyn_error_1618155_2026-08-30.txt")
#: `.skeleton-filled/<cluster>` is the tree cargo runs against; ASAN prints
#: absolute paths and the extractor's records are repo-relative `src/...`.
WORKSPACE_ROOT = "/home/user/determ/tmp/anyhow/.skeleton-filled/stable"
#: Line 27 of the crashing test, the free stack's next workspace frame.
RECORDED_SOURCE_LINE = "let boxed = err.into_boxed_dyn_error();"


def _parsed_stacks() -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
    parsed = parse_asan_report(REPORT.read_text())
    assert parsed is not None
    kind, access, origin = parsed
    return (
        kind,
        relativize_frame_files(access, WORKSPACE_ROOT),
        relativize_frame_files(origin, WORKSPACE_ROOT),
    )


def _parsed_site(*, source_line: str | None = None) -> CrashSite:
    kind, access, origin = _parsed_stacks()
    loc = (first_test_file_after_destructor(origin)
           if source_line is not None else None)
    return site_from_frames(
        "tests/behaviour_stable_gen_1.rs",
        "behaviour_probe_stable_trait_object_identity_0", kind, access,
        origin_raw_frames=origin, raw_report=REPORT.read_text(),
        origin_capable=True, source_line=source_line, source_loc=loc)


def _site(origin: list[dict[str, object]]) -> CrashSite:
    """A site with the recorded report's shape — an access stack with no
    in-crate frame — and a substituted origin stack."""
    return site_from_frames(
        "tests/behaviour_stable_gen_1.rs",
        "behaviour_probe_stable_trait_object_identity_0",
        "AddressSanitizer: heap-use-after-free", [],
        origin_raw_frames=origin,
        raw_report="heap-use-after-free", origin_capable=True)


def test_the_recorded_report_puts_only_the_destructor_in_crate() -> None:
    """The fixture's premise: `src/error.rs::drop` is the ONLY in-crate frame
    either stack has, which is why it was blamed and why nothing else could be."""
    site = _parsed_site()
    assert [f.fn_name for f in site.frames] == []
    assert [(f.file_rel, f.fn_name, f.line) for f in site.origin_frames] == [
        ("src/error.rs", "drop", 706)]


def test_the_destructor_was_blamed_before_this() -> None:
    """Negative control: the pre-fix rule takes the innermost origin frame, and
    that is the destructor. This is the blame five runs were aimed at."""
    site = _parsed_site()
    pre_fix = authorable_frames(site.origin_frames)
    assert pre_fix and pre_fix[0].fn_name == "drop", (
        "the pre-fix selection must still reproduce the wrong blame")


def test_a_destructor_alone_is_still_the_blame_of_last_resort() -> None:
    """Without a source line the sanitized site still blames `drop`.

    The caller's frame is a test file and the conversion was inlined, so
    skipping the destructor leaves nothing in-crate to promote. Discarding
    it would leave the aim empty — which is what the recorded walk
    `tests/walks/test_walk_mech_budget_lockout.py` convicts. So last-resort
    blame stays on `drop` here. Naming `into_boxed_dyn_error` is the
    source-line test below, not this one.
    """
    site = _parsed_site()
    assert site.culprit_origin_frames == site.origin_frames
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")
    assert site.origin_unattributed is False


def test_the_recorded_origin_next_workspace_frame_is_the_test_line() -> None:
    """The plan's fixture: after the destructor, the free stack points at
    `tests/behaviour_stable_gen_1.rs:27`."""
    _, _, origin = _parsed_stacks()
    assert first_test_file_after_destructor(origin) == (
        "tests/behaviour_stable_gen_1.rs", 27)


def test_the_recorded_report_blames_the_conversion_the_test_line_names() -> None:
    """The plan's sentence: on this report, with the recorded source line,
    blamed is `into_boxed_dyn_error`, not `drop`."""
    without = _parsed_site()
    assert without.blamed_locator == FnLocator("src/error.rs", "drop"), (
        "negative control: omitting the source line is the pre-recovery blame")

    site = _parsed_site(source_line=RECORDED_SOURCE_LINE)
    assert site.blamed is not None
    assert site.blamed.fn_name == "into_boxed_dyn_error"
    assert site.blamed.file_rel == "src/error.rs"
    assert site.origin_unattributed is False
    text = site.render_evidence()
    assert "src/error.rs:0" not in text
    assert "named by tests/behaviour_stable_gen_1.rs:27" in text


def test_the_origin_shape_licenses_recovery_when_the_read_marks_are_absent() -> None:
    """The family is licensed by EITHER stack. Same recorded origin
    (`Error::drop` then the test line) and source line, an access stack
    with neither `ErrorImpl` nor `Box<dyn Error>`: the origin's
    destructor-then-caller shape licenses the family and blame is the
    conversion. This used to be the "Rule 2" negative control — the READ
    marks were the only licence — and that reading left the iter-4
    `test_downcast` use-after-free of run `1577437-c93d5c26` (READ through
    `memcmp`, no mark) on `drop`. The real negative control is an origin
    with NO in-crate destructor — the Arc double-drop — pinned by
    `test_the_arc_double_drop_report_still_recovers_nothing` below."""
    _, _, origin = _parsed_stacks()
    access = [{"func": "core::ptr::drop_glue::<alloc::sync::Arc<bool>>",
               "file": "/rustc/x/library/core/src/ptr/mod.rs", "line": 1}]
    assert conversion_family_from_access(access) == frozenset(), (
        "the READ marks alone license nothing here")
    site = site_from_frames(
        "tests/behaviour_stable_gen_1.rs",
        "behaviour_probe_stable_trait_object_identity_0",
        "AddressSanitizer: heap-use-after-free", access,
        origin_raw_frames=origin, origin_capable=True,
        source_line=RECORDED_SOURCE_LINE)
    assert site.blamed_locator == FnLocator("src/error.rs", "into_boxed_dyn_error")


def test_recovery_stays_off_when_the_line_names_no_conversion() -> None:
    """A READ stack that matches the type chain still cannot promote a fn
    the test line does not call."""
    site = _parsed_site(source_line="let _ = err.downcast_ref::<MyErr>();")
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")


def test_a_src_caller_behind_the_destructor_is_left_for_the_skip() -> None:
    """When the next frame is still in `src/`, source-line recovery does
    not fire — `prefer_non_destructor` already promotes that caller."""
    origin = [
        {"func": "<anyhow::Error as core::ops::drop::Drop>::drop",
         "file": "src/error.rs", "line": 706},
        {"func": "anyhow::Error::reallocate_into_boxed_dyn_error_without_backtrace",
         "file": "src/error.rs", "line": 610},
    ]
    assert first_test_file_after_destructor(origin) is None
    site = _site(origin=origin)
    assert site.blamed is not None
    assert site.blamed.fn_name == "reallocate_into_boxed_dyn_error_without_backtrace"


def test_a_real_culprit_behind_the_destructor_is_still_blamed() -> None:
    """The skip walks outward; it does not disable origin blame. An in-crate
    frame that is not a destructor still convicts."""
    frames = [
        {"func": "<anyhow::Error as core::ops::drop::Drop>::drop",
         "file": "src/error.rs", "line": 706},
        {"func": "anyhow::Error::into_boxed_dyn_error",
         "file": "src/error.rs", "line": 512},
    ]
    site = _site(origin=frames)
    assert site.blamed is not None
    assert site.blamed.fn_name == "into_boxed_dyn_error"
    assert site.origin_unattributed is False


def test_drop_glue_and_drop_in_place_are_the_same_event() -> None:
    """The destructor inlined away is still the destructor."""
    frames = (
        CrashFrame(file_rel="src/error.rs", line=1, fn_name="drop_glue",
                   symbol="core::ptr::drop_glue::<anyhow::error::ErrorImpl<E>>"),
        CrashFrame(file_rel="src/error.rs", line=2, fn_name="drop_in_place",
                   symbol="core::ptr::drop_in_place::<anyhow::Error>"),
        CrashFrame(file_rel="src/error.rs", line=3, fn_name="object_boxed",
                   symbol="anyhow::error::object_boxed"),
    )
    assert [f.fn_name for f in non_destructor_frames(frames)] == ["object_boxed"]


def test_a_gdb_site_blames_the_caller_the_destructor_hid() -> None:
    """The live sweep this filter first ran on, run `1790133-2181e509`
    (2026-08-30 05:20:18). The ASAN site correctly blamed nobody, and two
    SIGSEGV siblings built from GDB frames still blamed `drop` — a gdb symbol
    carries no `as core::ops::drop::Drop>` marker, so a mark-only test misses
    it. Their access stack held the caller one frame out the whole time."""
    frames = [
        {"func": "drop", "file": "src/error.rs", "line": 705},
        {"func": "reallocate_into_boxed_dyn_error_without_backtrace",
         "file": "src/error.rs", "line": 610},
    ]
    # A native backtrace is not origin-capable, so blame falls to the access
    # stack — which is correct, and is exactly where the destructor sat.
    site = site_from_frames(
        "tests/error_gen_5.rs",
        "test_reallocate_into_boxed_dyn_error_without_backtrace_msg",
        "SIGSEGV", frames, origin_raw_frames=[], raw_report="",
        origin_capable=False)

    pre_fix = authorable_frames(site.frames)
    assert pre_fix[0].fn_name == "drop", (
        "negative control: the recorded event reads blamed: "
        '["src/error.rs", "drop"] off this stack')

    assert site.blamed is not None
    assert site.blamed.fn_name == "reallocate_into_boxed_dyn_error_without_backtrace"


def test_a_bare_drop_symbol_is_still_a_destructor() -> None:
    """gdb prints the function, not the trait impl that owns it."""
    frames = (
        CrashFrame(file_rel="src/error.rs", line=705, fn_name="drop",
                   symbol="drop"),
        CrashFrame(file_rel="src/error.rs", line=610, fn_name="convert",
                   symbol="convert"),
    )
    assert [f.fn_name for f in non_destructor_frames(frames)] == ["convert"]


def test_object_drop_is_a_destructor_the_same_way_the_crate_names_it() -> None:
    """destruction_path_subjects already treats a drop token as a destructor.
    Blame skip must, or object_drop is promoted over the conversion."""
    frames = (
        CrashFrame(file_rel="src/error.rs", line=1, fn_name="object_drop",
                   symbol="anyhow::error::object_drop"),
        CrashFrame(file_rel="src/error.rs", line=2,
                   fn_name="into_boxed_dyn_error",
                   symbol="anyhow::Error::into_boxed_dyn_error"),
    )
    assert [f.fn_name for f in non_destructor_frames(frames)] == [
        "into_boxed_dyn_error"]
    alone = (
        CrashFrame(file_rel="src/error.rs", line=1, fn_name="object_drop",
                   symbol="anyhow::error::object_drop"),
    )
    assert prefer_non_destructor(alone) == alone


def test_a_from_wrapper_source_line_names_the_conversion() -> None:
    """Live convert-test stack, run 1869206-7fd52754, report
    asan-20260830T083211573812.txt: freed-by is Error::drop then
    From<Error> for Box<dyn Error> at src/error.rs:955 whose body is
    error.into_boxed_dyn_error(). Preferring from aims the wrapper.
    The READ stack is Box<dyn Error> wrapping ErrorImpl, without Send+Sync
    — the auto-trait window was requiring one From impl's traits and
    missed this one."""
    origin = [
        {"func": "<anyhow::Error as core::ops::drop::Drop>::drop",
         "file": "src/error.rs", "line": 704},
        {"func": ("<alloc::boxed::Box<dyn core::error::Error> as "
                  "core::convert::From<anyhow::Error>>::from"),
         "file": "src/error.rs", "line": 955},
    ]
    access = [
        {"func": ("core::ptr::drop_glue::<anyhow::error::ErrorImpl<"
                  "test_convert::drop::DetectDrop>>"),
         "file": "/rustc/x/library/core/src/ptr/mod.rs", "line": 825},
        {"func": ("core::ptr::drop_glue::<alloc::boxed::Box<"
                  "dyn core::error::Error>>"),
         "file": "/rustc/x/library/core/src/ptr/mod.rs", "line": 825},
    ]
    loc = ("src/error.rs", 955)
    assert first_test_file_after_destructor(origin) == loc
    without = site_from_frames(
        "tests/test_convert.rs", "test_convert",
        "AddressSanitizer: heap-use-after-free", access,
        origin_raw_frames=origin, origin_capable=True)
    assert without.blamed_locator == FnLocator("src/error.rs", "from"), (
        "negative control: omitting the source line prefers from")
    site = site_from_frames(
        "tests/test_convert.rs", "test_convert",
        "AddressSanitizer: heap-use-after-free", access,
        origin_raw_frames=origin, origin_capable=True,
        source_line="        error.into_boxed_dyn_error()",
        source_loc=loc)
    assert site.blamed is not None
    assert site.blamed.fn_name == "into_boxed_dyn_error"
    text = site.render_evidence()
    assert "src/error.rs:0" not in text
    assert "named by src/error.rs:955" in text


# Live UAFs of run `1212998-d61fbd2c` (2026-09-06). Each ACCESS stack has
# ONE of the two types the old AND-gate required, so recovery stayed off
# and `phase_c.crash.localized` blamed `src/error.rs::drop` four sweeps.
# First error block of each report, driven through the real parser.
_REPORT_ERRORIMPL = (Path(__file__).parent / "fixtures" / "crash"
                     / "uaf_errorimpl_without_generics_1212998_2026-09-06.txt")
_REPORT_BOX = (Path(__file__).parent / "fixtures" / "crash"
               / "uaf_box_dyn_error_without_errorimpl_1212998_2026-09-06.txt")
# Computed from first_test_file_after_destructor on those reports, then
# the line at that loc in `.skeleton-filled/stable`.
_SOURCE_ERRORIMPL = "    let boxed = err.into_boxed_dyn_error();"
_SOURCE_BOX = "    let boxed_back = err.into_boxed_dyn_error();"


def _live_stacks(
    report: Path,
) -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
    parsed = parse_asan_report(report.read_text())
    assert parsed is not None
    kind, access, origin = parsed
    return (
        kind,
        relativize_frame_files(access, WORKSPACE_ROOT),
        relativize_frame_files(origin, WORKSPACE_ROOT),
    )


def _site_from_live_report(
    report: Path, *, source_line: str | None,
) -> CrashSite:
    kind, access, origin = _live_stacks(report)
    loc = (first_test_file_after_destructor(origin)
           if source_line is not None else None)
    return site_from_frames(
        "tests/error_gen_1.rs", None, kind, access,
        origin_raw_frames=origin, raw_report=report.read_text(),
        origin_capable=True, source_line=source_line, source_loc=loc)


def test_an_errorimpl_access_without_the_generic_mark_recovers_the_conversion() -> None:
    """Report 1: ACCESS is `<ErrorImpl>::error` (no `<E>`), no Box. With
    the source line, blamed is `into_boxed_dyn_error`. Without it, drop."""
    _, _, origin = _live_stacks(_REPORT_ERRORIMPL)
    assert first_test_file_after_destructor(origin) == (
        "tests/error_gen_1.rs", 33)
    without = _site_from_live_report(_REPORT_ERRORIMPL, source_line=None)
    assert without.blamed_locator == FnLocator("src/error.rs", "drop"), (
        "negative control: omitting the source line is last-resort drop")
    site = _site_from_live_report(
        _REPORT_ERRORIMPL, source_line=_SOURCE_ERRORIMPL)
    assert site.blamed_locator == FnLocator(
        "src/error.rs", "into_boxed_dyn_error")


def test_a_box_dyn_error_access_without_errorimpl_recovers_the_conversion() -> None:
    """Report 2: ACCESS is `drop_glue::<Box<dyn Error + Sync + Send>>`, no
    ErrorImpl. Same pair of assertions as report 1."""
    _, _, origin = _live_stacks(_REPORT_BOX)
    assert first_test_file_after_destructor(origin) == (
        "tests/error_gen_1.rs", 49)
    without = _site_from_live_report(_REPORT_BOX, source_line=None)
    assert without.blamed_locator == FnLocator("src/error.rs", "drop"), (
        "negative control: omitting the source line is last-resort drop")
    site = _site_from_live_report(_REPORT_BOX, source_line=_SOURCE_BOX)
    assert site.blamed_locator == FnLocator(
        "src/error.rs", "into_boxed_dyn_error")


# ---- run 1577437-c93d5c26 (2026-09-06): the family is licensed by the ORIGIN
# shape too, and an empty origin is recovered from the ACCESS side ----------
# Live archives `logs/asan/asan-20260906T221545829298.txt` (iter 4, two
# reports) and `…T204443251879.txt` (iter 2, three reports), distilled under
# `tests/fixtures/crash/` and driven through the real multi-report parser.
# The crashing tests' source lines are copied here from the filled tree at
# the archives' time; the tests never read the tree.
_SWEEP_ITER4 = (Path(__file__).parent / "fixtures" / "crash"
                / "sweep_two_reports_segv_and_uaf_1577437_2026-09-06_iter4.txt")
_SWEEP_ITER2 = (Path(__file__).parent / "fixtures" / "crash"
                / "sweep_three_reports_1577437_2026-09-06_iter2.txt")
# tests/test_downcast.rs:21 and tests/test_context.rs:124 (origin caller lines).
_LINE_TEST_DOWNCAST_21 = "        bail_fmt().unwrap_err().downcast::<String>().unwrap(),"
_LINE_TEST_CONTEXT_124 = "    let err = err.downcast::<HighLevel>().unwrap();"
# tests/error_gen_3.rs 29-33: the enclosing test fn up to the SEGV's access line.
_WINDOW_ERROR_GEN_3 = SourceWindow(
    file_rel="tests/error_gen_3.rs", first_line=29, lines=(
        "fn test_reallocate_into_boxed_dyn_error_without_backtrace_preserves_std_error_trait() {",
        "    use std::error::Error as StdError;",
        '    let err = Error::new(std::io::Error::new(std::io::ErrorKind::Other, "io"));',
        "    let boxed = err.reallocate_into_boxed_dyn_error_without_backtrace();",
        '    assert_eq!(boxed.to_string(), "io");',
    ))


def _report(fixture: Path, label: str) -> AsanReport:
    (rep,) = [r for r in parse_asan_reports(fixture.read_text()) if r.label == label]
    return AsanReport(
        label=rep.label, kind=rep.kind,
        access=relativize_frame_files(rep.access, WORKSPACE_ROOT),
        origin=relativize_frame_files(rep.origin, WORKSPACE_ROOT),
        text=rep.text, origin_stack_lines=rep.origin_stack_lines)


def test_a_uaf_read_without_either_type_mark_recovers_downcast_from_the_origin_shape() -> None:
    """Iter-4 report 2 (`tests/test_downcast.rs`): READ is `memcmp → <str as
    PartialEq>::eq → test line 19` — no `ErrorImpl`, no `Box<dyn Error>`;
    origin is `free → Error::drop src/error.rs:630 → test line 21`, and
    line 21 calls `downcast::<String>()`. Blame is `downcast`, named by
    `tests/test_downcast.rs:21`. Pre-fix (family licensed by the READ marks
    only): `src/error.rs::drop`."""
    rep = _report(_SWEEP_ITER4, "tests/test_downcast.rs")
    assert conversion_family_from_access(rep.access) == frozenset(), (
        "the READ stack carries neither mark — the pre-fix licence is off")
    loc = first_test_file_after_destructor(rep.origin)
    assert loc == ("tests/test_downcast.rs", 21)
    without = site_from_frames(
        rep.label, "test_downcast", rep.kind, rep.access,
        origin_raw_frames=rep.origin, origin_capable=True)
    assert without.blamed_locator == FnLocator("src/error.rs", "drop"), (
        "negative control: without the source line blame is the destructor")
    site = site_from_frames(
        rep.label, "test_downcast", rep.kind, rep.access,
        origin_raw_frames=rep.origin, origin_capable=True,
        source_line=_LINE_TEST_DOWNCAST_21, source_loc=loc)
    assert site.blamed is not None
    assert site.blamed.fn_name == "downcast"
    assert site.blamed.file_rel == "src/error.rs"
    assert site.blamed.named_by == ("tests/test_downcast.rs", 21)
    assert site.origin_unattributed is False
    assert "named by tests/test_downcast.rs:21" in site.render_evidence()


def test_the_test_context_uaf_recovers_downcast_through_the_turbofish() -> None:
    """Iter-2 report 2 (`tests/test_context.rs`): READ is
    `<anyhow::error::ErrorImpl>::error` (the READ mark licenses the family),
    origin `Error::drop src/error.rs:627 → tests/test_context.rs:124`, and
    line 124 is `err.downcast::<HighLevel>().unwrap()`. Pre-fix the regex
    required `downcast(` and `downcast` was not in the family: `drop`."""
    rep = _report(_SWEEP_ITER2, "tests/test_context.rs")
    assert conversion_family_from_access(rep.access)
    loc = first_test_file_after_destructor(rep.origin)
    assert loc == ("tests/test_context.rs", 124)
    without = site_from_frames(
        rep.label, "test_downcast_high", rep.kind, rep.access,
        origin_raw_frames=rep.origin, origin_capable=True,
        source_line="    let _ = err.downcast_ref::<HighLevel>();", source_loc=loc)
    assert without.blamed_locator == FnLocator("src/error.rs", "drop"), (
        "negative control: a line naming no family member leaves the destructor")
    site = site_from_frames(
        rep.label, "test_downcast_high", rep.kind, rep.access,
        origin_raw_frames=rep.origin, origin_capable=True,
        source_line=_LINE_TEST_CONTEXT_124, source_loc=loc)
    assert site.blamed is not None
    assert site.blamed.fn_name == "downcast"
    assert site.blamed.named_by == ("tests/test_context.rs", 124)


def test_a_segv_with_no_origin_stack_recovers_the_conversion_from_the_access_window() -> None:
    """Iter-4 report 1 (`tests/error_gen_3.rs`): no free/alloc stack at all.
    The READ stack is std's `Box<dyn Error>::to_string` under the test's
    line 33. With the enclosing fn's window (lines 29-33) the unique family
    name is `reallocate_into_boxed_dyn_error_without_backtrace` at line 32;
    the frame's file is the crate file a `tests/<stem>_gen_<N>.rs` test was
    written for. Pre-fix: origin recovery has nothing to walk, the site is
    `origin_unattributed`, blame is NOBODY."""
    rep = _report(_SWEEP_ITER4, "tests/error_gen_3.rs")
    assert rep.origin == [] and rep.origin_stack_lines == 0
    assert first_test_file_after_destructor(rep.origin) is None
    assert conversion_family_from_access(rep.access), (
        "`Box<dyn core::error::Error + Sync + Send>` on the READ stack")
    assert first_test_file_on_access(rep.access) == ("tests/error_gen_3.rs", 33)
    nobody = site_from_frames(
        rep.label, None, rep.kind, rep.access,
        origin_raw_frames=rep.origin, origin_capable=True)
    assert nobody.blamed_locator is None and nobody.origin_unattributed, (
        "negative control: the shipped path had nothing to read")
    site = site_from_frames(
        rep.label, None, rep.kind, rep.access,
        origin_raw_frames=rep.origin, origin_capable=True,
        source_window=_WINDOW_ERROR_GEN_3)
    assert site.blamed is not None
    assert site.blamed.fn_name == "reallocate_into_boxed_dyn_error_without_backtrace"
    assert site.blamed.file_rel == "src/error.rs"
    assert site.blamed.named_by == ("tests/error_gen_3.rs", 32)
    assert site.origin_unattributed is False
    assert "named by tests/error_gen_3.rs:32" in site.render_evidence()
    assert "src/error.rs:0" not in site.render_evidence()


def test_the_family_is_convicted_across_both_live_sites_and_drop_never_is() -> None:
    """`blamed_locators` over the two iter-4 sites (recovered
    `reallocate_…` and `downcast`) holds every family member at
    `src/error.rs` and not `drop`, even though `drop` is a frame on the
    UAF's origin stack."""
    segv = _report(_SWEEP_ITER4, "tests/error_gen_3.rs")
    uaf = _report(_SWEEP_ITER4, "tests/test_downcast.rs")
    sites = (
        site_from_frames(segv.label, None, segv.kind, segv.access,
                         origin_raw_frames=segv.origin, origin_capable=True,
                         source_window=_WINDOW_ERROR_GEN_3),
        site_from_frames(uaf.label, "test_downcast", uaf.kind, uaf.access,
                         origin_raw_frames=uaf.origin, origin_capable=True,
                         source_line=_LINE_TEST_DOWNCAST_21,
                         source_loc=("tests/test_downcast.rs", 21)),
    )
    assert [f.fn_name for f in sites[1].origin_frames] == ["downcast", "drop"], (
        "the destructor stays on the evidence frames")
    got = blamed_locators(sites)
    assert got == frozenset(FnLocator("src/error.rs", n) for n in CONVERSION_FAMILY)
    assert FnLocator("src/error.rs", "drop") not in got


def test_the_arc_double_drop_report_still_recovers_nothing() -> None:
    """Negative control for the origin licence, on the recorded shape of run
    `1064759-0f23fb68`: the free is the test's own `Arc::drop`, no in-crate
    destructor, so neither licence fires and the caller line is never
    consulted — blame stays nobody."""
    access = [
        {"func": "core::sync::atomic::AtomicUsize::fetch_sub",
         "file": "/rustc/x/library/core/src/sync/atomic.rs", "line": 3060},
        {"func": "test_downcast::test_drop", "file": "tests/test_downcast.rs",
         "line": 85},
    ]
    origin = [
        {"func": "free", "file": "/rustc/llvm/compiler-rt/lib/asan/asan_malloc_linux.cpp",
         "line": 51},
        {"func": "<alloc::sync::Arc<Atomic<bool>> as core::ops::drop::Drop>::drop",
         "file": "/rustc/x/library/alloc/src/sync.rs", "line": 2874},
        {"func": "test_downcast::test_drop", "file": "tests/test_downcast.rs",
         "line": 83},
    ]
    assert recover_owner_from_caller_source(
        origin, access, "    let b = err.into_boxed_dyn_error();",
        named_by=("tests/test_downcast.rs", 83)) is None
    assert recover_owner_from_caller_source(
        origin, access, None, source_window=SourceWindow(
            "tests/test_downcast.rs", 83,
            ("    let b = err.into_boxed_dyn_error();", "    drop(b);"))) is None
    site = site_from_frames(
        "tests/test_downcast.rs", "test_drop",
        "AddressSanitizer: heap-use-after-free", access,
        origin_raw_frames=origin, origin_capable=True,
        source_line="    let b = err.into_boxed_dyn_error();",
        source_loc=("tests/test_downcast.rs", 83))
    assert site.blamed_locator is None and site.origin_unattributed
