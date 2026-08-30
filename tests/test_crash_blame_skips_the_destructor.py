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

from pathlib import Path

from pipeline_core.domain.crash_site import (
    CrashFrame,
    CrashSite,
    authorable_frames,
    first_test_file_after_destructor,
    non_destructor_frames,
    parse_asan_report,
    prefer_non_destructor,
    relativize_frame_files,
    site_from_frames,
)
from pipeline_core.domain.models import FnLocator

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


def test_recovery_stays_off_without_the_box_errorimpl_type_chain() -> None:
    """Rule 2: an Arc double-drop must not recover. Same source line, an
    access stack that is not `Box<dyn Error>` wrapping `ErrorImpl`, last
    resort stays `drop`."""
    _, _, origin = _parsed_stacks()
    site = site_from_frames(
        "tests/behaviour_stable_gen_1.rs",
        "behaviour_probe_stable_trait_object_identity_0",
        "AddressSanitizer: heap-use-after-free",
        [{"func": "core::ptr::drop_glue::<alloc::sync::Arc<bool>>",
          "file": "/rustc/x/library/core/src/ptr/mod.rs", "line": 1}],
        origin_raw_frames=origin, origin_capable=True,
        source_line=RECORDED_SOURCE_LINE)
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")


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
