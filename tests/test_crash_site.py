"""Pins for crash-site domain logic: frame filtering, blame preference
(origin over access), evidence rendering, ASAN report parsing."""

from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.crash_site import (
    asan_stack_excerpt,
    bare_fn_name,
    callee_subjects,
    chain_derived_site,
    crash_evidence_by_locator,
    crate_file_for_test_file,
    destruction_path_subjects,
    origin_stack_text,
    parse_asan_report,
    placeholder_pointer_subjects,
    read_destruction_fault,
    read_placeholder_pointer_fault,
    relativize_frame_files,
    site_from_frames,
    vanish_is_stub_confounded,
    vanished_subject_fns,
)
from pipeline_core.domain.language_driver import (
    GenericLanguageDriver,
    using_driver,
)
from pipeline_core.domain.models import FnLocator


def test_bare_fn_name_strips_generics_and_paths() -> None:
    assert bare_fn_name("anyhow::error::ErrorImpl<()>::error") == "error"
    assert bare_fn_name("anyhow::error::{impl#6}::drop") == "drop"
    assert bare_fn_name("<anyhow::Error>::construct_from_std::<test_source::TestError>") \
        == "construct_from_std"
    assert bare_fn_name("plain_fn") == "plain_fn"


def test_site_filters_to_crate_frames_and_blames_innermost() -> None:
    frames = [
        {"func": "??", "file": "", "line": 0},                      # garbage pc
        {"func": "anyhow::error::{impl#6}::drop",
         "file": "src/error.rs", "line": 612},
        {"func": "core::ptr::drop_in_place<anyhow::Error>",
         "file": "library/core/src/ptr/mod.rs", "line": 805},      # std — dropped
        {"func": "test_source::test_io_source",
         "file": "tests/test_source.rs", "line": 55},              # not src/ — dropped
    ]
    site = site_from_frames("tests/test_source.rs", "test_io_source",
                            "SIGSEGV", frames)
    assert [f.fn_name for f in site.frames] == ["drop"]
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")
    ev = site.render_evidence()
    assert "SIGSEGV" in ev and "test_io_source" in ev
    assert "src/error.rs:612" in ev


def test_origin_frames_take_blame_precedence() -> None:
    access = [{"func": "a::deref", "file": "src/error.rs", "line": 560}]
    origin = [{"func": "a::construct_from_std", "file": "src/error.rs", "line": 142}]
    site = site_from_frames("tests/x.rs", None, "stack-use-after-return",
                            access, origin_raw_frames=origin)
    assert site.blamed_locator == FnLocator("src/error.rs", "construct_from_std")
    ev = site.render_evidence()
    assert "CREATED by" in ev and "construct_from_std" in ev


def test_evidence_by_locator_covers_access_and_origin() -> None:
    access = [{"func": "a::deref", "file": "src/error.rs", "line": 560}]
    origin = [{"func": "a::construct_from_std", "file": "src/error.rs", "line": 142}]
    site = site_from_frames("tests/x.rs", None, "sig", access,
                            origin_raw_frames=origin)
    m = crash_evidence_by_locator((site,))
    assert FnLocator("src/error.rs", "deref") in m
    assert FnLocator("src/error.rs", "construct_from_std") in m


_ASAN = """\
test test_io_source ... ==123==ERROR: AddressSanitizer: stack-use-after-return on address 0x7f at pc 0x55 bp 0x70 sp 0x70
READ of size 8 at 0x7f thread T2
    #0 0x55a in <anyhow::Error as core::ops::deref::Deref>::deref /w/src/error.rs:560:27
    #1 0x55b in test_source::test_io_source /w/tests/test_source.rs:55:5
Address 0x7f is located in stack of thread T2 at offset 32 in frame
    #0 0x55c in <anyhow::Error>::construct_from_std::<E> /w/src/error.rs:142
  This frame has 2 object(s):
    [32, 96) 'vtable'
"""


def test_parse_asan_report_splits_access_and_origin() -> None:
    parsed = parse_asan_report(_ASAN)
    assert parsed is not None
    kind, access, origin = parsed
    assert kind == "AddressSanitizer: stack-use-after-return"
    assert str(access[0]["func"]).endswith("::deref") and access[0]["line"] == 560
    assert origin and "construct_from_std" in str(origin[0]["func"])
    assert parse_asan_report("all tests passed") is None


def test_relativize_frame_files() -> None:
    frames = [{"func": "f", "file": "/w/src/error.rs", "line": 1},
              {"func": "g", "file": "library/std/x.rs", "line": 2}]
    out = relativize_frame_files(frames, "/w")
    assert out[0]["file"] == "src/error.rs"
    assert out[1]["file"] == "library/std/x.rs"


def test_asan_to_site_end_to_end() -> None:
    kind, access, origin = parse_asan_report(_ASAN)  # type: ignore[misc]
    access = relativize_frame_files(access, "/w")
    origin = relativize_frame_files(origin, "/w")
    site = site_from_frames("tests/test_source.rs", "test_io_source", kind,
                            access, origin_raw_frames=origin)
    assert site.blamed_locator == FnLocator("src/error.rs", "construct_from_std")
    assert [f.fn_name for f in site.frames] == ["deref"]


def test_file_fill_prompt_carries_crash_evidence() -> None:
    # The whole-file prompt renders a crashing body as part of the file and
    # asks for mutually consistent siblings — without the evidence the model
    # reads the unsound scheme as the house idiom and copies it.
    from pipeline_core.prompts.fix_prompts import (
        BlockFillPlan,
        build_file_fill_prompt,
    )

    kw = {"file_rel": "src/error.rs", "file_text": "fn a() {}",
          "fill_plan": [BlockFillPlan("impl Error", ("construct_from_adhoc",))],
          "tests_block": "", "blueprint": "", "notes": ""}
    without = build_file_fill_prompt(**kw)
    assert "CRASH_EVIDENCE" not in without

    with_ev = build_file_fill_prompt(
        **kw, crash_evidence="stack-use-after-return in construct_from_std")
    assert "<CRASH_EVIDENCE>" in with_ev
    assert "construct_from_std" in with_ev
    # The builder frames the evidence and asserts no mechanism of its own:
    # the "do not copy that scheme" instruction is the invalid-memory
    # renderer's, and a raw string carries none.
    assert "do NOT copy that pattern" not in with_ev


def test_vanish_is_stub_confounded() -> None:
    provide = FnLocator("src/error.rs", "provide")
    obj_bt = FnLocator("src/error.rs", "object_backtrace")
    display = FnLocator("src/error.rs", "display")   # filled
    fmt = FnLocator("src/error.rs", "fmt")           # filled
    rmap = {"test_provide": {provide, obj_bt, display, fmt}}
    # 2 of the 4 reached fns are still stubs -> >=half -> confounded.
    assert vanish_is_stub_confounded(
        {"test_provide"}, rmap, [provide, obj_bt]) is True
    # only 1 of 4 still stubbed -> a filled-body crash is plausible, localize.
    assert vanish_is_stub_confounded({"test_provide"}, rmap, [provide]) is False
    # module-qualified vanished name resolves via its bare tail.
    assert vanish_is_stub_confounded(
        {"error::test_provide"}, rmap, [provide, obj_bt]) is True
    # no reverse-map entry -> no suspects -> never suppress (localizer runs).
    assert vanish_is_stub_confounded(
        {"test_unknown"}, rmap, [provide, obj_bt]) is False


def test_origin_capable_detector_that_finds_no_origin_blames_nobody() -> None:
    # The live 07-31 defect: ASAN reported `heap-use-after-free` in
    # tests/test_downcast.rs, its free stack symbolized entirely outside the
    # crate, every origin frame was filtered out, and `blamed` fell back to the
    # innermost READER — `src/error.rs::downcast`. Every sweep of every run
    # then told the model to "fix the pointer/lifetime scheme in `downcast`",
    # a correct fn, at ~4 GPU-hours per iteration. The module's own docstring
    # says that frame is routinely innocent, which is why it escalates to ASAN.
    access = [{"func": "anyhow::error::downcast", "file": "src/error.rs",
               "line": 482}]
    site = site_from_frames(
        "tests/test_downcast.rs", "test_downcast",
        "AddressSanitizer: heap-use-after-free", access,
        origin_raw_frames=[],          # free stack had no in-crate frame
        origin_capable=True)
    assert site.origin_unattributed is True
    assert site.blamed is None and site.blamed_locator is None
    # The access path is still evidence and is still rendered...
    ev = site.render_evidence()
    assert "src/error.rs:482" in ev
    # ...but nothing directs the model at the reader.
    assert "Fix the pointer/lifetime scheme" not in ev
    assert "UNKNOWN" in ev

    # An origin that DOES survive the filter convicts, exactly as before.
    with_origin = site_from_frames(
        "tests/test_downcast.rs", "test_downcast",
        "AddressSanitizer: heap-use-after-free", access,
        origin_raw_frames=[{"func": "anyhow::error::object_drop",
                            "file": "src/error.rs", "line": 700}],
        origin_capable=True)
    assert with_origin.origin_unattributed is False
    assert with_origin.blamed_locator == FnLocator("src/error.rs",
                                                   "object_drop")


def test_a_detector_that_cannot_report_origins_still_blames_its_frame() -> None:
    # gdb walks the faulting thread and nothing else, so its lack of origin
    # frames is not evidence of anything. On a host with no sanitizer that
    # backtrace is the only evidence there is — weak blame beats none.
    site = site_from_frames(
        "tests/test_source.rs", "test_io_source", "SIGSEGV",
        [{"func": "anyhow::error::drop", "file": "src/error.rs", "line": 612}])
    assert site.origin_capable is False
    assert site.origin_unattributed is False
    assert site.blamed_locator == FnLocator("src/error.rs", "drop")


def test_asan_stack_excerpt_keeps_the_free_stack_the_filter_discards() -> None:
    # Frames outside `src/` never reach a CrashFrame, so who freed the memory
    # was unrecoverable without paying for a second instrumented build. The
    # report was already in hand and was being parsed and dropped.
    report = (
        "Running tests/test_downcast.rs\n"
        "==11==ERROR: AddressSanitizer: heap-use-after-free on address 0xdead\n"
        "READ of size 8 at 0xdead thread T0\n"
        "    #0 0x1 in anyhow::error::downcast src/error.rs:482\n"
        "0xdead is located 0 bytes inside of 16-byte region\n"
        "freed by thread T0 here:\n"
        "    #0 0x2 in free ../asan_malloc_linux.cpp:52\n"
        "    #1 0x3 in __rust_dealloc library/alloc/src/alloc.rs:117\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free\n"
        "Shadow bytes around the buggy address:\n")
    excerpt = asan_stack_excerpt(report)
    assert "freed by thread T0 here:" in excerpt
    assert "__rust_dealloc" in excerpt          # the frame the filter drops
    assert "Shadow bytes" not in excerpt        # noise stays out


def test_the_excerpt_budget_is_per_stack_not_per_report() -> None:
    # A single whole-report cap is spent by whichever stack prints FIRST, and
    # ASAN prints the access stack first — the one stack this function exists
    # to look past. Live on `anyhow/nightly`: a 33-frame access stack against
    # a 40-line cap left `freed by` exactly three frames before the truncation
    # marker and dropped `previously allocated by` entirely, so the decisive
    # evidence survived on ordering luck alone.
    access = "".join(f"    #{i} 0x{i} in noise::f{i} library/core/x.rs:{i}\n"
                     for i in range(60))
    report = (
        "==11==ERROR: AddressSanitizer: heap-use-after-free on address 0xdead\n"
        "READ of size 8 at 0xdead thread T0\n"
        + access +
        "0xdead is located 0 bytes inside of 16-byte region\n"
        "freed by thread T0 here:\n"
        "    #0 0x2 in free ../asan_malloc_linux.cpp:52\n"
        "    #1 0x3 in core::ptr::drop_glue::<RawVec<u8>> library/core/x.rs:825\n"
        "    #2 0x4 in anyhow::error::downcast src/error.rs:482\n"
        "previously allocated by thread T0 here:\n"
        "    #0 0x5 in malloc ../asan_malloc_linux.cpp:67\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free\n")
    excerpt = asan_stack_excerpt(report)
    assert "… (stack truncated)" in excerpt      # the access stack IS capped
    assert "noise::f59" not in excerpt
    # ...and the stacks after it are not starved by that cap.
    assert "drop_glue::<RawVec<u8>>" in excerpt
    assert "anyhow::error::downcast" in excerpt
    assert "previously allocated by thread T0 here:" in excerpt
    assert "in malloc" in excerpt
    # `origin_stack_text` reads the free stack back off the excerpt, so the
    # budget has to leave it intact there too.
    assert "drop_glue::<RawVec<u8>>" in origin_stack_text(excerpt)


def test_asan_stacks_are_read_by_introducer_not_by_position() -> None:
    # The live output on `anyhow/nightly`: a use-after-free report followed by
    # LeakSanitizer's own reports (leak detection runs at exit regardless of
    # halt_on_error). Taking stacks[0]/stacks[1] blindly walked out of the UAF
    # report and into a leak's malloc stack, then labelled it "origin" — which
    # is why `src/error.rs::downcast` was blamed on every sweep since 07-30.
    report = (
        "==11==ERROR: AddressSanitizer: heap-use-after-free on address 0xdead\n"
        "READ of size 8 at 0xdead thread T0\n"
        "    #0 0x1 in anyhow::error::downcast src/error.rs:482\n"
        "0xdead is located 0 bytes inside of 16-byte region\n"
        "freed by thread T0 here:\n"
        "    #0 0x2 in free ../asan_malloc_linux.cpp:52\n"
        "    #1 0x3 in anyhow::error::object_drop src/error.rs:700\n"
        "previously allocated by thread T0 here:\n"
        "    #0 0x4 in malloc ../asan_malloc_linux.cpp:67\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free src/error.rs:482\n"
        # Everything below belongs to a DIFFERENT report and must not leak in.
        "==11==ERROR: LeakSanitizer: detected memory leaks\n"
        "Direct leak of 4 byte(s) in 1 object(s) allocated from:\n"
        "    #0 0x9 in malloc ../asan_malloc_linux.cpp:67\n"
        "    #1 0xa in anyhow::ptr::test_own_clone src/ptr.gen_tests_1.rs:10\n"
        "SUMMARY: AddressSanitizer: 4 byte(s) leaked in 1 allocation(s).\n")
    got = parse_asan_report(report)
    assert got is not None
    kind, access, origin = got
    assert kind == "AddressSanitizer: heap-use-after-free"
    # ACCESS is the reader...
    assert [f["func"] for f in access] == ["anyhow::error::downcast"]
    # ...ORIGIN is the free path, which is the fn that must actually change.
    assert [f["func"] for f in origin][-1] == "anyhow::error::object_drop"
    # The leak's allocation stack never appears in either.
    assert not any("test_own_clone" in str(f["func"])
                   for f in (*access, *origin))

    site = site_from_frames("tests/test_downcast.rs", "test_downcast", kind,
                            access, origin_raw_frames=origin,
                            origin_capable=True)
    assert site.blamed_locator == FnLocator("src/error.rs", "object_drop")


def test_evidence_carries_the_unfiltered_free_stack() -> None:
    # Reproduced from the live archived report. The `src/`-only frame filter
    # keeps ONE frame — `downcast` — and drops the two that say WHAT was
    # freed. `drop_glue::<RawVec<u8>>` under `free` is the whole diagnosis: a
    # String payload's buffer dropped by a vtable call after `ptr::read` moved
    # the value out. Without it the prompt says only "fix the pointer scheme
    # in downcast", which is where the CALL is, not what the defect is.
    report = (
        "==580451==ERROR: AddressSanitizer: heap-use-after-free on address 0x7bb\n"
        "READ of size 6 at 0x7bb thread T2\n"
        "    #0 0x1 in memcmp ../sanitizer_common_interceptors.inc:880:10\n"
        "0x7bb is located 0 bytes inside of 6-byte region\n"
        "freed by thread T2 here:\n"
        "    #0 0x2 in free ../asan_malloc_linux.cpp:51:3\n"
        "    #1 0x3 in core::ptr::drop_glue::<alloc::raw_vec::RawVec<u8>> "
        "library/core/src/ptr/mod.rs:825:1\n"
        "    #2 0x4 in <anyhow::Error>::downcast::<alloc::string::String> "
        "src/error.rs:482:30\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free\n")
    kind, access, origin = parse_asan_report(report)  # type: ignore[misc]
    site = site_from_frames(
        "tests/test_downcast.rs", "test_downcast", kind, access,
        origin_raw_frames=origin, origin_capable=True,
        raw_report=asan_stack_excerpt(report))
    # The access stack is entirely std here, so the crate-frame view is EMPTY —
    # which is why 93.2 read the single `frames` entry as the access frame and
    # concluded the reader was being blamed. It was the ORIGIN frame.
    assert site.frames == ()
    assert [f.fn_name for f in site.origin_frames] == ["downcast"]
    ev = site.render_evidence()
    assert "drop_glue::<alloc::raw_vec::RawVec<u8>>" in ev
    assert "freed by thread T2 here:" in ev


# ---- origin blame vs evidence keys, and the outside-the-unit test ----

def test_blamed_locators_are_the_origin_not_every_frame() -> None:
    """`crash_evidence_by_locator` keys by EVERY frame — access and origin —
    so the innocent caller routing execution into the fault is a key too.
    Live 2026-08-09/10: `ext_context` (src/context.rs) was an access frame of
    the very `src/error.rs::object_drop` fault it was being charged for. The
    blame set must carry the origin alone, or any outside-the-unit test reads
    the blame as inside the caller and never fires."""
    from pipeline_core.domain.crash_site import (
        CrashFrame,
        CrashSite,
        blame_outside_unit,
        blamed_locators,
    )
    access = CrashFrame(file_rel="src/context.rs", line=10,
                        fn_name="ext_context", symbol="anyhow::ext_context")
    origin = CrashFrame(file_rel="src/error.rs", line=99,
                        fn_name="object_drop", symbol="anyhow::object_drop")
    site = CrashSite(binary_label="tests/test_context.rs",
                     test_name="test_downcast_high",
                     signal="AddressSanitizer: heap-use-after-free",
                     frames=(access,), origin_frames=(origin,),
                     raw_report="")
    evidence = crash_evidence_by_locator((site,))
    assert FnLocator("src/context.rs", "ext_context") in evidence  # the trap
    blamed = blamed_locators((site,))
    assert blamed == frozenset({FnLocator("src/error.rs", "object_drop")})
    # The innocent caller's unit is OUTSIDE the blame…
    assert blame_outside_unit(blamed, "src/context.rs",
                              frozenset({"ext_context"}))
    # …the faulty fn's own unit is not…
    assert not blame_outside_unit(blamed, "src/error.rs",
                                  frozenset({"object_drop"}))
    # …and no blame at all exonerates nobody.
    assert not blame_outside_unit(frozenset(), "src/context.rs",
                                  frozenset({"ext_context"}))


# --- the unlocalizable deterministic crasher -------------------------------
# `error::gen_tests_4::test_no_backtrace_fallback` vanished on every sweep of
# run `2219473` (2026-08-10) and every localization returned "no native fault
# under gdb, no ASAN report" — so `blamed_fns` stayed empty and the whole
# crash-evidence machinery (keep, block, prompt section) had nothing to act
# on. `vanished_subject_fns` is the weaker evidence grade for exactly that
# case: subject attribution from the test's own module file, never blame.


def test_vanished_subject_fns_narrows_to_the_module_file() -> None:
    from pipeline_core.domain.crash_site import vanished_subject_fns
    from pipeline_core.domain.models import FnLocator
    rmap = {"test_no_backtrace_fallback": [
        ("src/error.rs", "no_backtrace"),
        ("src/context.rs", "ext_context"),   # reachable, wrong module
    ]}
    out = vanished_subject_fns(
        frozenset({"error::gen_tests_4::test_no_backtrace_fallback"}), rmap)
    assert out == {FnLocator("src/error.rs", "no_backtrace"):
                   frozenset({"error::gen_tests_4::test_no_backtrace_fallback"})}


def test_bare_test_name_anchors_nothing() -> None:
    from pipeline_core.domain.crash_site import vanished_subject_fns
    rmap = {"test_downcast": [("src/error.rs", "downcast")]}
    assert vanished_subject_fns(frozenset({"test_downcast"}), rmap) == {}


def test_too_wide_a_subject_set_contributes_nothing() -> None:
    from pipeline_core.domain.crash_site import vanished_subject_fns
    rmap = {"test_x": [("src/error.rs", f"fn{i}") for i in range(4)]}
    assert vanished_subject_fns(
        frozenset({"error::gen_tests_1::test_x"}), rmap) == {}


def test_unknown_test_contributes_nothing() -> None:
    from pipeline_core.domain.crash_site import vanished_subject_fns
    assert vanished_subject_fns(
        frozenset({"error::gen_tests_1::test_y"}), {}) == {}


def test_crash_signal_hint_extracts_symptom_lines() -> None:
    from pipeline_core.domain.crash_site import crash_signal_hint
    out = ("running 3 tests\n"
           "test a ... ok\n"
           "error: test failed. process didn't exit successfully: "
           "`t/lib-1` (signal: 6, SIGABRT: process abort signal)\n"
           "some unrelated line\n")
    hint = crash_signal_hint(out)
    assert "SIGABRT" in hint
    assert "unrelated" not in hint


def test_crash_signal_hint_empty_when_no_symptoms() -> None:
    from pipeline_core.domain.crash_site import crash_signal_hint
    assert crash_signal_hint("running 2 tests\ntest a ... ok\n") == ""


# --- chain-derived blame: the frame-blind crash class -------------------------
# The deterministic `test_downcast` heap-use-after-free (run `635043-5f7c61f5`,
# 2026-08-14, `logs/asan/asan-20260814T025642115609.txt` report at line 3877):
# access, freed-by and allocated-by stacks all resolve to std/test frames,
# because an Arc refcount error planted in the crate's construct/vtable code
# surfaces later, in std's `Arc::drop` at the test's own drop sites. No frame
# parse can name crate code that is in no stack, so the blame is synthesized
# from the crashed tests' construction chain instead — marked as such.


def test_chain_derived_site_is_marked_and_convicts_nobody() -> None:
    from pipeline_core.domain.crash_site import (
        PROVENANCE_CHAIN,
        blamed_locators,
        chain_derived_site,
    )
    site = chain_derived_site(
        "tests/test_downcast.rs", "test_drop",
        "AddressSanitizer: heap-use-after-free",
        [FnLocator("src/error.rs", "construct_from_std"),
         FnLocator("src/error.rs", "new")],
        derived_from_tests=["test_drop", "test_downcast_high"],
        raw_report="freed by thread T0 here:\n    #0 free asan.cpp:52")
    assert site.provenance == PROVENANCE_CHAIN
    assert site.origin_unattributed is True
    # An inference must never acquire a frame's authority: `blamed`/
    # `blamed_locator` stay None, so the conviction/crash-block/immunity set
    # (`blamed_locators`) never contains a chain-derived blame.
    assert site.blamed is None and site.blamed_locator is None
    assert blamed_locators((site,)) == frozenset()


def test_chain_evidence_keys_by_the_chain_and_says_what_it_is() -> None:
    from pipeline_core.domain.crash_site import chain_derived_site
    site = chain_derived_site(
        "tests/test_downcast.rs", "test_drop",
        "AddressSanitizer: heap-use-after-free",
        [FnLocator("src/error.rs", "construct_from_std"),
         FnLocator("src/error.rs", "new")],
        derived_from_tests=["test_drop"],
        raw_report="freed by thread T0 here:\n    #0 free asan.cpp:52")
    ev = crash_evidence_by_locator((site,))
    assert set(ev) == {FnLocator("src/error.rs", "construct_from_std"),
                       FnLocator("src/error.rs", "new")}
    text = ev[FnLocator("src/error.rs", "new")]
    # The rendered evidence declares its own grade, names the deriving tests,
    # and carries the detector's raw report — the only record of WHAT was
    # freed when every frame is std's.
    assert "CHAIN-DERIVED" in text
    assert "test_drop" in text
    assert "freed by thread T0" in text
    assert "no such section" not in text


# --- a generated test sibling is an access path, never blame ------------------
# The in-crate filter keeps `src/…` frames, and the pipeline's own generated
# tests live at `src/<stem>.gen_tests_<N>.rs` — so the crashing test's own body
# passes it. Run `263217-d41a5bb5` (2026-08-15) is the measured case: the whole
# ASAN report held exactly two frames, `test_reallocate_into_boxed_dyn_error_
# without_backtrace` at `src/error.gen_tests_4.rs:14` and its `{closure#0}` at
# `:11`. That report's origin was unattributed so nothing was blamed; a gdb
# backtrace of the same shape is not origin-capable and used to fall back to
# the innermost access frame, making the crashing test its own culprit.

def _gen_tests_frames() -> list[dict[str, object]]:
    return [
        {"file": "src/error.gen_tests_4.rs", "line": 14,
         "func": "test_reallocate_into_boxed_dyn_error_without_backtrace"},
        {"file": "src/error.gen_tests_4.rs", "line": 11, "func": "{closure#0}"},
    ]


def test_a_gdb_backtrace_of_only_generated_test_frames_blames_nobody() -> None:
    """The pre-fix fallback made the crashing test its own culprit. Nothing in
    the crate declares that name, so the layout aim graded NONE against every
    component and `blame_outside_unit` read True for every real unit — which
    crash-blocks a block against a file fingerprint the fill phase never
    changes. Blaming nobody is the honest answer, and it is the one that lets
    the chain-derived channel run."""
    from pipeline_core.domain.crash_site import blame_outside_unit, blamed_locators
    site = site_from_frames(
        "unittests src/lib.rs", "error::gen_tests_4::test_x",
        "SIGSEGV", _gen_tests_frames())          # gdb: not origin-capable
    assert site.frames, "the frames are still evidence and must survive"
    assert site.blamed is None
    assert blamed_locators((site,)) == frozenset()
    # The consequence that made this expensive: with the test frame as blame,
    # every real unit reads as exonerated and gets crash-blocked.
    assert not blame_outside_unit(
        blamed_locators((site,)), "src/error.rs", frozenset({"construct"}))
    # NEGATIVE CONTROL — the pre-fix rule, verbatim: `origin_frames[0]` if any,
    # else `frames[0]`. On this site that is the generated test fn.
    pre_fix = site.origin_frames[0] if site.origin_frames else site.frames[0]
    assert pre_fix.file_rel == "src/error.gen_tests_4.rs"
    assert blame_outside_unit(
        frozenset({FnLocator(pre_fix.file_rel, pre_fix.fn_name)}),
        "src/error.rs", frozenset({"construct"}))


def test_an_origin_stack_of_only_generated_frames_reads_as_unattributed() -> None:
    """`origin_unattributed` is the chain channel's precondition — "a detector
    that CAN report an origin looked and placed no frame in-crate". An origin
    stack that resolves only into the crashing test's own generated file has
    attributed the fault to nothing answerable, so it must read the same way;
    otherwise the strongest evidence channel stands down for a blame no fill
    can act on."""
    site = site_from_frames(
        "unittests src/lib.rs", "error::gen_tests_4::test_x",
        "AddressSanitizer: heap-use-after-free", _gen_tests_frames(),
        origin_raw_frames=_gen_tests_frames(), origin_capable=True)
    assert site.origin_unattributed
    assert site.blamed is None
    # Pre-fix: origin_frames non-empty → NOT unattributed, and blamed was the
    # generated test fn — the chain synthesis never armed.
    assert site.origin_frames
    # A real crate origin still decides, unchanged.
    real = site_from_frames(
        "unittests src/lib.rs", "error::gen_tests_4::test_x",
        "AddressSanitizer: heap-use-after-free", _gen_tests_frames(),
        origin_raw_frames=[{"file": "src/error.rs", "line": 9,
                            "func": "construct"}],
        origin_capable=True)
    assert not real.origin_unattributed
    assert real.blamed is not None and real.blamed.fn_name == "construct"


def test_a_mixed_stack_blames_the_innermost_authorable_frame() -> None:
    """Not a wholesale drop of the frames: the generated ones stay in the site
    (they are the access path the prompt renders), and the blame skips past
    them to the innermost frame that names a body a fill can rewrite."""
    site = site_from_frames(
        "unittests src/lib.rs", "error::gen_tests_4::test_x", "SIGSEGV",
        _gen_tests_frames() + [{"file": "src/error.rs", "line": 40,
                                "func": "object_drop"}])
    assert len(site.frames) == 3
    assert site.blamed is not None and site.blamed.fn_name == "object_drop"
    text = site.render_evidence()
    assert "src/error.gen_tests_4.rs:14" in text      # access path kept
    assert "`object_drop`" in text


def test_a_test_only_crash_says_it_names_no_fn_to_change() -> None:
    """With no authorable frame and no origin claim, the rendered evidence
    would otherwise list frames and give no instruction at all."""
    site = site_from_frames(
        "unittests src/lib.rs", "error::gen_tests_4::test_x",
        "SIGSEGV", _gen_tests_frames())
    text = site.render_evidence()
    assert "names no fn to change" in text
    assert "Fix the pointer/lifetime scheme" not in text


# Trimmed verbatim from `logs/asan/asan-20260815T215316630991.txt` — the report
# run `1064759-0f23fb68` produced on every one of its seven sweeps, and blamed
# NOBODY on every one. Frames dropped only from the middles of the two stacks;
# the header, the drop frames and the test frames are as ASAN printed them.
_DOUBLE_DROP = """\
==1122932==ERROR: AddressSanitizer: heap-use-after-free on address 0x7bb97a9e1c60 at pc 0x55d76d1779da bp 0x7b89783fe240 sp 0x7b89783fe238
WRITE of size 8 at 0x7bb97a9e1c60 thread T5
    #0 0x55d76d1779d9 in core::sync::atomic::atomic_sub::<usize, usize> /rustc/9eb3be/library/core/src/sync/atomic.rs:3956:24
    #2 0x55d76d1779d9 in <alloc::sync::Arc<core::sync::atomic::Atomic<bool>> as core::ops::drop::Drop>::drop /rustc/9eb3be/library/alloc/src/sync.rs:2831:32
    #4 0x55d76d1772f9 in core::ptr::drop_glue::<test_downcast::drop::Flag> /rustc/9eb3be/library/core/src/ptr/mod.rs:825:1
    #5 0x55d76d172ee4 in test_downcast::test_drop /w/tmp/anyhow/.skeleton-filled/stable/tests/test_downcast.rs:85:1
0x7bb97a9e1c60 is located 0 bytes inside of 16-byte region
freed by thread T5 here:
    #0 0x55d76d141cb6 in free /rustc/llvm/src/llvm-project/compiler-rt/lib/asan/asan_malloc_linux.cpp:51:3
    #4 0x55d76d176134 in <alloc::alloc::Global as core::alloc::Allocator>::deallocate /rustc/9eb3be/library/alloc/src/alloc.rs:462:23
    #5 0x55d76d177a0f in <alloc::sync::Arc<core::sync::atomic::Atomic<bool>> as core::ops::drop::Drop>::drop /rustc/9eb3be/library/alloc/src/sync.rs:2874:18
    #6 0x55d76d173003 in test_downcast::test_drop /w/tmp/anyhow/.skeleton-filled/stable/tests/test_downcast.rs:83:5
previously allocated by thread T5 here:
    #0 0x55d76d142a3e in malloc /rustc/llvm/src/llvm-project/compiler-rt/lib/asan/asan_malloc_linux.cpp:69:3
    #3 0x55d76d1728a4 in test_downcast::drop::Flag::new /w/tmp/anyhow/.skeleton-filled/stable/tests/test_downcast.rs:20:9
SUMMARY: AddressSanitizer: heap-use-after-free /rustc/9eb3be/library/core/src/sync/atomic.rs:3956:24
"""


def test_a_double_drop_is_read_from_the_stacks_the_frame_filter_discards() -> None:
    """Neither stack holds a `src/` frame, so `blamed` is None and always will
    be: the fault is a MISSING `ManuallyDrop`, and an absence has no frame.
    The two stacks still say exactly what happened."""
    fault = read_destruction_fault(_DOUBLE_DROP)
    assert fault is not None
    assert fault.freed_type == "alloc::sync::Arc<core::sync::atomic::Atomic<bool>>"
    assert fault.free_site == "tests/test_downcast.rs:83"
    assert fault.access_site == "tests/test_downcast.rs:85"
    text = fault.render()
    assert "DOUBLE DROP" in text and "dropped twice" in text
    # The negative control for the whole channel: the localizer's own verdict
    # on this identical report, which is what the run acted on for 7h48m.
    site = site_from_frames(
        "tests/test_downcast.rs", "test_drop",
        "AddressSanitizer: heap-use-after-free", [],
        origin_raw_frames=[], origin_capable=True, raw_report=_DOUBLE_DROP)
    assert site.blamed is None and site.origin_unattributed


def test_a_use_after_return_is_not_a_double_drop() -> None:
    """A different fault class with a different remedy — a stack lifetime, not
    an ownership count. Returning a reading here would put a false statement
    of fact into a prompt."""
    assert read_destruction_fault(_ASAN) is None


def test_a_use_after_free_that_is_not_two_drops_reads_nothing() -> None:
    """Only when BOTH stacks run through a drop. A free from an ordinary
    deallocation path is an ownership bug of some other shape."""
    single = _DOUBLE_DROP.replace(
        "    #5 0x55d76d177a0f in <alloc::sync::Arc<core::sync::atomic::Atomic"
        "<bool>> as core::ops::drop::Drop>::drop /rustc/9eb3be/library/alloc/"
        "src/sync.rs:2874:18\n", "")
    assert read_destruction_fault(single) is None


def test_a_chain_site_carries_the_double_drop_into_its_evidence() -> None:
    """The reading is derived at construction, so no caller can forget it."""
    site = chain_derived_site(
        "tests/test_downcast.rs", "test_drop",
        "AddressSanitizer: heap-use-after-free",
        [FnLocator("src/error.rs", "object_drop_front")],
        derived_from_tests=["test_drop"], raw_report=_DOUBLE_DROP)
    text = site.render_evidence()
    assert "DOUBLE DROP" in text
    assert "alloc::sync::Arc<core::sync::atomic::Atomic<bool>>" in text
    assert "tests/test_downcast.rs:83" in text


_FNS = [
    {"file": "src/error.rs", "fn_name": "object_drop", "body_text": "Box::from_raw(p)"},
    {"file": "src/error.rs", "fn_name": "object_drop_front",
     "body_text": "let _ = Box::from_raw(e.ptr.as_ptr() as *mut ErrorImpl<E>);"},
    {"file": "src/error.rs", "fn_name": "context_chain_drop_rest", "body_text": "{}"},
    {"file": "src/error.rs", "fn_name": "drop", "body_text": "unsafe { (vtable(self).object_drop)(self.inner) }"},
    {"file": "src/error.rs", "fn_name": "downcast", "body_text": "let val = ptr::read(r);"},
    {"file": "src/context.rs", "fn_name": "context", "body_text": "Error::from_context(c, e)"},
]


def test_destruction_subjects_are_the_bodies_that_transfer_ownership() -> None:
    """The set the construction chain cannot reach, read from the bodies:
    `object_drop` and `object_drop_front` hold `from_raw`, `downcast` holds
    `ptr::read`. A fn NAMED for dropping whose body holds no primitive
    (`context_chain_drop_rest`, `Error::drop`) is not a subject: the name is
    a language convention, and the pipeline does not read one."""
    subs = destruction_path_subjects(_FNS)
    assert subs == frozenset({
        FnLocator("src/error.rs", "object_drop"),
        FnLocator("src/error.rs", "object_drop_front"),
        FnLocator("src/error.rs", "downcast"),
    })


def test_a_subject_set_wider_than_the_cap_contributes_nothing() -> None:
    """`vanished_subject_fns`' precedent: a subject that names half the crate
    names none of it."""
    wide = [{"file": "src/a.rs", "fn_name": f"drop_{i}",
             "body_text": "Box::from_raw(p)"} for i in range(13)]
    assert destruction_path_subjects(wide) == frozenset()
    assert len(destruction_path_subjects(wide, max_subjects=13)) == 13


def test_the_subject_channel_anchors_on_the_inventory_not_a_module_path() -> None:
    """The channel had never fired once since it shipped 2026-08-10. Its only
    anchor was `test.split("::")`, and its only production caller passes
    `SweepOracle.vanished` — bare by construction (`reconcile_sweep` builds it
    through `bare_test_name`, and the bare form is load-bearing for the
    duplicate-name dedup that set's docstring defends). A green unit test over
    an input the caller never produces is the shape this was."""
    rmap = {"test_x": {("src/error.rs", "construct")},
            "test_y": {("src/ptr.rs", "new")}}
    bare = frozenset({"test_x"})
    # Pre-fix: a bare name anchors nothing, so the channel contributes nothing.
    assert vanished_subject_fns(bare, rmap) == {}
    # With the inventory's file index the anchor is the crate file the test's
    # generated sibling belongs to.
    out = vanished_subject_fns(
        bare, rmap, test_files={"test_x": "src/error.gen_tests_4.rs"})
    assert out == {FnLocator("src/error.rs", "construct"): frozenset({"test_x"})}


def test_an_integration_test_anchors_nothing() -> None:
    """`tests/test_downcast.rs` answers for the crate's public surface, not for
    one file — a subject that wide is not a subject."""
    assert crate_file_for_test_file("tests/test_downcast.rs") is None
    assert crate_file_for_test_file("src/error.gen_tests_4.rs") == "src/error.rs"
    assert crate_file_for_test_file("src/error.rs") == "src/error.rs"
    assert vanished_subject_fns(
        frozenset({"test_x"}), {"test_x": {("src/error.rs", "construct")}},
        test_files={"test_x": "tests/test_downcast.rs"}) == {}


def test_a_subject_may_never_be_a_test_file() -> None:
    """Measured on the live anyhow cluster tree 2026-08-16: enumerating 323
    fns, the drop-name rule matches `tests/drop/mod.rs::drop`,
    `tests/test_downcast.rs::test_drop` and `tests/test_repr.rs::test_drop`
    beside the five real ones. No fill writes a test, so such a subject lands
    in the frontier and buys a dispatch that cannot change anything — the
    defect 173.1 shipped `authorable_frames` for, arriving through records
    instead of frames."""
    held = "Box::from_raw(p)"
    recs = [
        {"file": "src/error.rs", "fn_name": "object_drop_front", "body_text": held},
        {"file": "tests/drop/mod.rs", "fn_name": "drop", "body_text": held},
        {"file": "tests/test_downcast.rs", "fn_name": "test_drop", "body_text": held},
        {"file": "src/error.gen_tests_4.rs", "fn_name": "drop", "body_text": held},
    ]
    assert destruction_path_subjects(recs) == frozenset({
        FnLocator("src/error.rs", "object_drop_front")})


# The sweep text of run `1408670-3c074ece`, iter 5, trimmed to the lines the
# reader is asked about. Verbatim: this is the whole of what
# `tests/test_downcast.rs` printed before SIGABRT, and the localizer read the
# same bytes and blamed `downcast_ref` — which is the frame, and not the fault.
_MISALIGNED = """\
     Running tests/test_downcast.rs (target-asan/x86_64-unknown-linux-gnu/debug/deps/test_downcast-de1f45a040e1a1ee)

thread 'test_drop' panicked at src/error.rs:440:34:
misaligned pointer dereference: address must be a multiple of 0x8 but is 0x1
thread caused non-unwinding panic. aborting.
error: test failed, to rerun pass `--test test_downcast`
"""


def test_a_placeholder_pointer_is_read_from_the_alignment_check() -> None:
    """`0x1` is `NonNull::<()>::dangling()`. The fn that dereferenced it is in
    the panic line; the fn that WROTE it is not, and is what has to change."""
    fault = read_placeholder_pointer_fault(_MISALIGNED)
    assert fault is not None
    assert (fault.address, fault.required_align) == (1, 8)
    assert fault.deref_site == "src/error.rs:440"
    text = fault.render()
    assert "PLACEHOLDER POINTER" in text
    assert "src/error.rs:440" in text
    assert "FABRICATED" in text


def test_a_genuinely_misaligned_heap_pointer_is_not_a_placeholder() -> None:
    """The discriminator is arithmetic and it must bite: an address at or above
    the alignment it violates is a real allocation read at a bad offset, a
    different fault with a different remedy."""
    assert read_placeholder_pointer_fault(
        _MISALIGNED.replace("but is 0x1", "but is 0x55eca2d1c009")) is None
    assert read_placeholder_pointer_fault(
        _MISALIGNED.replace("multiple of 0x8", "multiple of 0x1")) is None


def test_an_ordinary_panic_reads_no_placeholder() -> None:
    assert read_placeholder_pointer_fault(
        "thread 'test_x' panicked at src/error.rs:12:5:\n"
        "assertion `left == right` failed\n") is None
    assert read_placeholder_pointer_fault("") is None


_SENTINEL_FNS = [
    # The three defective bodies on the live tree, verbatim in shape.
    {"file": "src/error.rs", "fn_name": "object_downcast",
     "body_text": "if TypeId::of::<E>() == target {\n"
                  "    Some(unsafe { Ref::from_raw(core::ptr::NonNull::dangling()) })\n"
                  "} else { None }"},
    {"file": "src/error.rs", "fn_name": "context_downcast",
     "body_text": "Some(unsafe { Ref::from_raw(core::ptr::NonNull::dangling()) })"},
    # The correct idiom: a sentinel written INTO a slot in exchange for the
    # value moved out of it. Four of the live tree's seven occurrences.
    {"file": "src/error.rs", "fn_name": "downcast",
     "body_text": "let inner = core::mem::replace(&mut self.inner, "
                  "Own { ptr: core::ptr::NonNull::dangling() });"},
    {"file": "src/error.rs", "fn_name": "drop",
     "body_text": "let inner = core::mem::replace(&mut self.inner, "
                  "Own { ptr: core::ptr::NonNull::dangling() });"},
    {"file": "src/error.rs", "fn_name": "vtable", "body_text": "unsafe { &*e.vtable }"},
]


def test_placeholder_subjects_are_the_fns_that_hand_a_sentinel_out() -> None:
    """Measured against `tmp/anyhow/.skeleton-filled/stable` with the real
    extractor on 2026-08-16: seven `NonNull::dangling()` occurrences over 323
    fns, four of them `mem::replace` arguments, and the three survivors are
    exactly the crate's `*_downcast` family — every one of them the body that
    aborts `tests/test_downcast.rs`, and none of them dispatched once in the
    six-hour run that read 79/101 on every sweep."""
    subs = placeholder_pointer_subjects(_SENTINEL_FNS)
    assert subs == frozenset({
        FnLocator("src/error.rs", "object_downcast"),
        FnLocator("src/error.rs", "context_downcast")})


def test_the_exchange_idiom_is_excluded_by_use_not_by_name() -> None:
    """`downcast` holds the sentinel and is correct; `object_downcast` holds
    the same sentinel and is the fault. Nothing about the fn NAME separates
    them — only what the occurrence is an argument to."""
    only_correct = [f for f in _SENTINEL_FNS
                    if f["fn_name"] in ("downcast", "drop", "vtable")]
    assert placeholder_pointer_subjects(only_correct) == frozenset()


def test_placeholder_subjects_obey_the_cap_and_the_authorable_rule() -> None:
    wide = [{"file": "src/a.rs", "fn_name": f"f{i}",
             "body_text": "Some(NonNull::dangling())"} for i in range(13)]
    assert placeholder_pointer_subjects(wide) == frozenset()
    assert len(placeholder_pointer_subjects(wide, max_subjects=13)) == 13
    assert placeholder_pointer_subjects([
        {"file": "tests/test_downcast.rs", "fn_name": "test_drop",
         "body_text": "let p = NonNull::dangling();"}]) == frozenset()


_CALLEE_FNS = [
    # `downcast_ref`'s live body, which is where the fault is READ.
    {"file": "src/error.rs", "fn_name": "downcast_ref",
     "body_text": "let ref_impl = self.inner.by_ref();\n"
                  "let vtbl = unsafe { crate::error::vtable(self.inner.ptr) };\n"
                  "if let Some(down) = unsafe { (vtbl.object_downcast)(ref_impl, target) } {\n"
                  "    let ptr = down.as_ptr() as *const E;\n"
                  "    return Some(unsafe { &*ptr });\n"
                  "}\nNone"},
    {"file": "src/error.rs", "fn_name": "object_downcast", "body_text": ""},
    {"file": "src/error.rs", "fn_name": "vtable", "body_text": ""},
    {"file": "src/ptr.rs", "fn_name": "by_ref", "body_text": ""},
    {"file": "src/ptr.rs", "fn_name": "as_ptr", "body_text": ""},
    # A crate fn the body never names — the rule must not reach it.
    {"file": "src/chain.rs", "fn_name": "next", "body_text": ""},
    # A module whose name is also a fn name elsewhere. The body says
    # `crate::error::vtable`, and `error` there is a path qualifier.
    {"file": "src/lib.rs", "fn_name": "error", "body_text": ""},
]


def test_callee_subjects_reach_the_producer_the_frame_cannot_name() -> None:
    """The measurement the fix exists for. Blame was `src/error.rs::
    downcast_ref` on all five sweeps of run `1408670-3c074ece`; the fault was
    `object_downcast`, called on the line above the one that faulted, and it
    was never dispatched. Note the call shape: `(vtbl.object_downcast)(…)` puts
    a `)` after the name, so a `(`-only rule misses exactly this fn."""
    subs = callee_subjects(FnLocator("src/error.rs", "downcast_ref"), _CALLEE_FNS)
    assert subs == frozenset({
        FnLocator("src/error.rs", "object_downcast"),
        FnLocator("src/error.rs", "vtable"),
        FnLocator("src/ptr.rs", "by_ref"),
        FnLocator("src/ptr.rs", "as_ptr")})
    # The module qualifier is not a call, and the unmentioned fn is not one.
    assert FnLocator("src/lib.rs", "error") not in subs
    assert FnLocator("src/chain.rs", "next") not in subs


def test_callee_subjects_of_an_unknown_blame_are_empty() -> None:
    assert callee_subjects(
        FnLocator("src/nowhere.rs", "missing"), _CALLEE_FNS) == frozenset()


def test_callee_subjects_obey_the_cap() -> None:
    """A blamed fn that names half the crate names none of it — the same rule
    `destruction_path_subjects` and `vanished_subject_fns` are capped by."""
    body = " ".join(f"f{i}()" for i in range(13))
    recs = [{"file": "src/a.rs", "fn_name": "caller", "body_text": body}]
    recs += [{"file": "src/a.rs", "fn_name": f"f{i}", "body_text": ""}
             for i in range(13)]
    blamed = FnLocator("src/a.rs", "caller")
    assert callee_subjects(blamed, recs) == frozenset()
    assert len(callee_subjects(blamed, recs, max_subjects=13)) == 13


# ---- one AddressSanitizer report per crashed binary --------------------------
# The ASAN sweep is one process per test binary: `halt_on_error=1` stops THAT
# process, cargo starts the next. The archives of run `1577437-c93d5c26`
# (2026-09-06), distilled under `tests/fixtures/crash/` to the lines the
# parsers read, hold 2 (iter 4) and 3 (iter 2) AddressSanitizer reports each,
# with LeakSanitizer reports between them.

_FIXTURES = Path(__file__).parent / "fixtures" / "crash"
_SWEEP_ITER4 = _FIXTURES / "sweep_two_reports_segv_and_uaf_1577437_2026-09-06_iter4.txt"
_SWEEP_ITER2 = _FIXTURES / "sweep_three_reports_1577437_2026-09-06_iter2.txt"


def test_parse_asan_reports_returns_one_report_per_crashed_binary() -> None:
    """Two reports on the iter-4 archive, three on iter 2, each tagged with
    the `Running` label of the binary whose process printed it, and their
    kinds are the two the archives hold. `parse_asan_report` on the same
    text still returns the first — the SEGV — which is the pre-fix reading,
    now explicitly the first-report wrapper."""
    from pipeline_core.domain.crash_site import parse_asan_reports
    two = parse_asan_reports(_SWEEP_ITER4.read_text())
    assert [(r.label, r.kind) for r in two] == [
        ("tests/error_gen_3.rs", "AddressSanitizer: SEGV"),
        ("tests/test_downcast.rs", "AddressSanitizer: heap-use-after-free")]
    three = parse_asan_reports(_SWEEP_ITER2.read_text())
    assert [(r.label, r.kind) for r in three] == [
        ("tests/error_gen_3.rs", "AddressSanitizer: SEGV"),
        ("tests/test_context.rs", "AddressSanitizer: heap-use-after-free"),
        ("tests/test_downcast.rs", "AddressSanitizer: heap-use-after-free")]
    first = parse_asan_report(_SWEEP_ITER2.read_text())
    assert first is not None and first[0] == "AddressSanitizer: SEGV"
    assert first[2] == [], (
        "negative control: the first report has an empty origin — the "
        "reading that blamed NOBODY and armed chain synthesis")


def test_leak_stacks_appear_in_no_report_and_a_segv_has_no_origin_stack() -> None:
    """Twenty-one LeakSanitizer reports sit between the AddressSanitizer
    reports; none of their `malloc` stacks reaches any report's access or
    origin. A SEGV report has ZERO origin lines (no free/alloc stack was
    printed), which is different from an origin stack whose frames did
    not symbolize."""
    from pipeline_core.domain.crash_site import parse_asan_reports
    text = _SWEEP_ITER2.read_text()
    assert text.count("ERROR: LeakSanitizer:") == 21
    reports = parse_asan_reports(text)
    for r in reports:
        assert not any(str(f["func"]) == "malloc" for f in r.origin), r.label
        assert not any("leak" in str(f["func"]).lower()
                       for f in (*r.access, *r.origin)), r.label
    segv, ctx, dc = reports
    assert segv.origin == [] and segv.origin_stack_lines == 0
    assert ctx.origin_stack_lines == 27 and len(ctx.origin) == 27
    assert dc.origin_stack_lines == 24 and len(dc.origin) == 24
    assert any("as core::ops::drop::Drop>::drop" in str(f["func"])
               for f in ctx.origin)
    assert any("as core::ops::drop::Drop>::drop" in str(f["func"])
               for f in dc.origin)


def test_a_report_without_a_summary_is_closed_by_the_next_report_or_label() -> None:
    from pipeline_core.domain.crash_site import parse_asan_reports
    text = (
        "    Running tests/a.rs (target/debug/deps/a-1)\n"
        "==1==ERROR: AddressSanitizer: SEGV on unknown address 0x4\n"
        "    #0 0x1 in a::f /w/src/a.rs:1\n"
        "    Running tests/b.rs (target/debug/deps/b-2)\n"
        "==2==ERROR: AddressSanitizer: heap-use-after-free on address 0x8\n"
        "    #0 0x2 in b::g /w/src/b.rs:2\n"
        "freed by thread T0 here:\n"
        "    #0 0x3 in free ../asan_malloc_linux.cpp:52\n"
        "    #1 0x4 in b::h /w/src/b.rs:9\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free\n")
    reports = parse_asan_reports(text)
    assert [(r.label, [f["func"] for f in r.access], [f["func"] for f in r.origin])
            for r in reports] == [
        ("tests/a.rs", ["a::f"], []),
        ("tests/b.rs", ["b::g"], ["free", "b::h"])]
    assert parse_asan_reports("all tests passed") == []


# ---- the conversion family: every by-value copy of inner ---------------------

def test_generic_recovers_a_conversion_from_the_asan_line_without_a_name_set() -> None:
    """A crate that does not declare into_boxed_dyn_error still recovers
    the unique call on the ASAN test line, and the blame is that one fn."""
    from pipeline_core.domain.crash_site import (
        CrashFrame,
        CrashSite,
        blamed_locators,
        recover_owner_from_caller_source,
    )
    origin = [
        {"func": "free",
         "file": "/rustc/llvm/compiler-rt/lib/asan/asan_malloc_linux.cpp",
         "line": 51},
        {"func": "<crate::Hold as core::ops::drop::Drop>::drop",
         "file": "src/cell.rs", "line": 10},
        {"func": "cell_gen_1::test_take", "file": "tests/cell_gen_1.rs",
         "line": 21},
    ]
    access = [
        {"func": "memcmp",
         "file": "/rustc/llvm/sanitizer_common_interceptors.inc", "line": 880},
    ]
    line = "    let boxed = err.take_box();"
    with using_driver(GenericLanguageDriver()):
        frame = recover_owner_from_caller_source(
            origin, access, line, named_by=("tests/cell_gen_1.rs", 21))
        assert frame is not None
        assert frame.fn_name == "take_box"
        assert frame.file_rel == "src/cell.rs"
        conv = CrashFrame(file_rel="src/cell.rs", line=0, fn_name="take_box",
                          symbol="take_box",
                          named_by=("tests/cell_gen_1.rs", 21))
        site = CrashSite(binary_label="tests/cell_gen_1.rs", test_name="t",
                         signal="AddressSanitizer: SEGV", frames=(),
                         origin_frames=(conv,), origin_capable=True)
        got = blamed_locators((site,))
    assert got == frozenset({FnLocator("src/cell.rs", "take_box")})
    assert "into_boxed_dyn_error" not in {loc.fn_name for loc in got}


def test_first_test_file_on_access_is_the_innermost_test_frame_with_a_line() -> None:
    from pipeline_core.domain.crash_site import first_test_file_on_access
    access = [
        {"func": "<alloc::string::String as core::fmt::Display>::fmt",
         "file": "/rustc/x/library/alloc/src/string.rs", "line": 2725},
        {"func": "error_gen_3::test_x", "file": "tests/error_gen_3.rs", "line": 33},
        {"func": "error_gen_3::test_x::{closure#0}", "file": "tests/error_gen_3.rs",
         "line": 29},
    ]
    assert first_test_file_on_access(access) == ("tests/error_gen_3.rs", 33)
    assert first_test_file_on_access(access[:1]) is None
    sibling = [{"func": "error::gen_tests_4::test_y",
                "file": "src/error.gen_tests_4.rs", "line": 14}]
    assert first_test_file_on_access(sibling) == ("src/error.gen_tests_4.rs", 14)
    assert first_test_file_on_access(
        [{"func": "anyhow::Error::downcast", "file": "src/error.rs", "line": 5}]
    ) is None, "a crate frame is not a test file"


def test_a_generated_integration_test_anchors_the_file_it_was_written_for() -> None:
    """`tests/<stem>_gen_<N>.rs` is the pipeline's own integration-test
    naming; an upstream `tests/test_downcast.rs` still anchors nothing."""
    assert crate_file_for_test_file("tests/error_gen_3.rs") == "src/error.rs"
    assert crate_file_for_test_file("tests/behaviour_stable_gen_1.rs") == (
        "src/behaviour_stable.rs")
    assert crate_file_for_test_file("tests/test_downcast.rs") is None
    assert crate_file_for_test_file("tests/common/mod.rs") is None


def test_origin_blamed_locators_are_the_recovered_name() -> None:
    """The retrigger reads one locator per site. A recovered conversion
    stays that conversion, and the dispatch set adds nothing beside it at
    the file. A drop site and a chain-derived site are unchanged (drop is
    the origin; chain contributes nothing)."""
    from pipeline_core.domain.crash_site import (
        CrashFrame,
        CrashSite,
        blamed_locators,
        chain_derived_site,
        origin_blamed_locators,
    )
    conv = CrashFrame(file_rel="src/error.rs", line=0,
                      fn_name="reallocate_into_boxed_dyn_error_without_backtrace",
                      symbol="reallocate_into_boxed_dyn_error_without_backtrace",
                      named_by=("tests/error_gen_3.rs", 32))
    site = CrashSite(binary_label="tests/error_gen_3.rs", test_name="t",
                     signal="AddressSanitizer: SEGV", frames=(),
                     origin_frames=(conv,), origin_capable=True)
    origin = origin_blamed_locators((site,))
    assert origin == frozenset({
        FnLocator("src/error.rs",
                  "reallocate_into_boxed_dyn_error_without_backtrace")})
    assert origin == blamed_locators((site,))
    drop = CrashFrame(file_rel="src/error.rs", line=630, fn_name="drop",
                      symbol="<anyhow::Error as core::ops::drop::Drop>::drop")
    drop_site = CrashSite(binary_label="tests/x.rs", test_name="t",
                          signal="AddressSanitizer: heap-use-after-free",
                          frames=(), origin_frames=(drop,), origin_capable=True)
    assert origin_blamed_locators((drop_site,)) == frozenset(
        {FnLocator("src/error.rs", "drop")})
    chain = chain_derived_site(
        "tests/x.rs", "t", "AddressSanitizer: SEGV",
        [FnLocator("src/error.rs", "downcast")])
    assert origin_blamed_locators((chain,)) == frozenset()


# --- homonyms keep their container ------------------------------------------

_TWO_FMTS = """struct Error;
impl fmt::Debug for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        self.debug(f)
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("e")
    }
}
impl A {
    fn new() -> A { A }
}
impl B {
    fn new() -> B { B }
}
impl C {
    fn new() -> C { C }
}
fn caller(e: &Error) -> String {
    format!("{:?}", e)
}
"""
_CONTAINERS = {3: "impl fmt::Debug for Error", 8: "impl fmt::Display for Error",
               13: "impl A", 16: "impl B", 19: "impl C", 21: ""}


def _span_records(text: str, file: str) -> list:
    """Byte-span records of the invented file, as the extractor emits them
    (signature start through the closing brace)."""
    from pipeline_core.domain.models import FnRecord
    raw = text.encode()
    lines = text.splitlines(keepends=True)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln.encode()))
    out = []
    for line_no, cont in _CONTAINERS.items():
        sig = starts[line_no - 1] + lines[line_no - 1].index("fn ")
        brace = raw.index(b"{", sig)
        depth, i = 0, brace
        while True:
            if raw[i:i + 1] == b"{":
                depth += 1
            elif raw[i:i + 1] == b"}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        name = lines[line_no - 1].split("fn ")[1].split("(")[0]
        out.append(FnRecord(file=file, fn_name=name, sig_byte_start=sig,
                            body_byte_start=brace + 1, body_byte_end=i,
                            container=cont))
    return out


def _uaf(origin_line: int, origin_fn: str):
    from pipeline_core.domain.crash_site import CrashFrame, CrashSite
    access = CrashFrame(file_rel="src/error.rs", line=22, fn_name="caller",
                        symbol="anyhow::caller")
    origin = CrashFrame(file_rel="src/error.rs", line=origin_line,
                        fn_name=origin_fn, symbol=f"anyhow::{origin_fn}")
    return CrashSite(binary_label="tests/test_fmt.rs", test_name="t",
                     signal="AddressSanitizer: heap-use-after-free",
                     frames=(access,), origin_frames=(origin,), raw_report="")


def test_the_two_fmt_homonyms_do_not_share_evidence_or_a_refill_admission() -> None:
    """A fault in `fmt` under `impl Debug for Error`. The
    locator maps key `src/error.rs::fmt`, so the Display `fmt` shared its
    evidence string and its crash refill admission. The frames resolve to
    the Debug body; the Display body carries neither. Negative control: the
    locator map still gives both one key, and dispatch keeps reading it."""
    from pipeline_core.application.reconstruct.cluster_membership import (
        collect_impl_refill_targets,
    )
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.crash_site import (
        blamed_locators,
        crash_evidence_by_locator,
        crash_evidence_for_units,
        crash_sites_of,
    )
    from pipeline_core.domain.models import FnRecord, FnSite, ImplBlock
    from pipeline_core.domain.refill_evidence import Attribution, collect_refill_evidence

    recs = _span_records(_TWO_FMTS, "src/error.rs")
    site = _uaf(4, "fmt")
    fmt = FnLocator("src/error.rs", "fmt")
    debug, display = "impl fmt::Debug for Error", "impl fmt::Display for Error"
    assert list(crash_evidence_by_locator((site,))).count(fmt) == 1
    blamed = blamed_locators((site,))
    assert blamed == {fmt}

    sites = crash_sites_of((site,), recs,
                           lambda rel: _TWO_FMTS if rel == "src/error.rs" else None)
    assert sites.blamed == {FnSite("src/error.rs", "fmt", debug)}
    assert sites.blames(blamed, "src/error.rs", "fmt", debug)
    assert not sites.blames(blamed, "src/error.rs", "fmt", display)
    evidence = crash_evidence_by_locator((site,))
    # The per-fn and impl pastes call evidence_for for the container they
    # are filling. The session paste calls crash_evidence_for_units with
    # the same site map. A session whose only unit is the Display body
    # carries no Debug crash text. Without the site map the locator join
    # still hands that body the one shared string.
    debug_site = FnSite("src/error.rs", "fmt", debug)
    display_site = FnSite("src/error.rs", "fmt", display)
    assert sites.evidence_for(evidence, "src/error.rs", "fmt", debug)
    assert sites.evidence_for(evidence, "src/error.rs", "fmt", display) == ""
    assert crash_evidence_for_units(evidence, [display_site])
    assert crash_evidence_for_units(
        evidence, [display_site], sites) == ""
    assert crash_evidence_for_units(evidence, [debug_site], sites)
    assert crash_evidence_for_units(
        evidence, [display_site, debug_site], sites) == (
            crash_evidence_for_units(evidence, [debug_site], sites))
    assert sites.evidences(evidence.keys(), "src/error.rs", "caller", "")

    def block(cont: str) -> ImplBlock:
        return ImplBlock(file="src/error.rs", container=cont, members=(
            FnRecord(file="src/error.rs", fn_name="fmt", container=cont),))

    frontier = FailureFrontier.of_direct({fmt: set()})
    stamped = collect_impl_refill_targets(
        [block(debug), block(display)], {fmt}, frontier,
        crash_blamed=blamed, crash_sites=sites, file_primary=True)
    assert [len(b.refill_members) for b in stamped] == [1, 0]
    # The locator-only reading (no sites) admits both: the old collapse.
    stamped_old = collect_impl_refill_targets(
        [block(debug), block(display)], {fmt}, frontier,
        crash_blamed=blamed, file_primary=True)
    assert [len(b.refill_members) for b in stamped_old] == [1, 1]
    grades = [collect_refill_evidence(b, stamped_old, frontier, crashed=blamed,
                                      crash_sites=sites)[0].attribution
              for b in stamped_old]
    assert grades[0] is Attribution.CRASHED
    assert grades[1] is not Attribution.CRASHED


def test_three_new_containers_do_not_collapse_to_one_blame() -> None:
    from pipeline_core.domain.crash_site import blamed_locators, crash_sites_of

    recs = _span_records(_TWO_FMTS, "src/error.rs")
    site = _uaf(16, "new")
    blamed = blamed_locators((site,))
    sites = crash_sites_of((site,), recs,
                           lambda rel: _TWO_FMTS if rel == "src/error.rs" else None)
    got = [sites.blames(blamed, "src/error.rs", "new", c)
           for c in ("impl A", "impl B", "impl C")]
    assert got == [False, True, False]
    # A frame no record's span holds is left out, and the locator blame then
    # stands for every container, as before.
    stray = crash_sites_of((_uaf(99, "new"),), recs,
                           lambda rel: _TWO_FMTS if rel == "src/error.rs" else None)
    assert stray.blamed == frozenset()
    assert all(stray.blames(blamed, "src/error.rs", "new", c)
               for c in ("impl A", "impl B", "impl C"))


# ---- the frame that produced a faulting address (2026-10-15) ----
#
# Run `1057870-b427a3a2`: the faulting frame was a correct reader handed
# `NonNull::dangling()` by the frame above it. The shapes below are
# invented; the recorded instance is the real-gdb integration test
# `test_the_localizer_names_the_frame_that_produced_the_address`.

from pipeline_core.domain.crash_site import traced_origin_index  # noqa: E402


def _fr(func: str, file: str, *ptrs: tuple[int, int]) -> dict[str, object]:
    return {"func": func, "file": file, "line": 1,
            "ptr_args": [list(p) for p in ptrs]}


def test_a_handed_address_is_traced_to_the_frame_that_passed_it() -> None:
    frames = [_fr("crate::reader", "src/a.rs", (0x8, 56)),
              _fr("crate::maker", "src/b.rs", (0x5555_0000, 1)),
              _fr("tests::t", "tests/t.rs")]
    assert traced_origin_index(frames, 0x8) == 1
    site = site_from_frames("tests/t.rs", "t", "SIGSEGV", frames,
                            fault_addr=0x8)
    assert site.origin_traced
    assert site.blamed is not None and site.blamed.fn_name == "maker"


def test_an_address_the_reader_computed_names_the_reader() -> None:
    frames = [_fr("crate::reader", "src/a.rs", (0x5555_0000, 16)),
              _fr("crate::maker", "src/b.rs")]
    assert traced_origin_index(frames, 0x1000_0000_0000) == 0


def test_an_address_handed_in_by_the_test_names_nothing_in_the_crate() -> None:
    frames = [_fr("crate::reader", "src/a.rs", (0x8, 56)),
              _fr("tests::t", "tests/t.rs")]
    assert traced_origin_index(frames, 0x8) is None
    site = site_from_frames("tests/t.rs", "t", "SIGSEGV", frames,
                            fault_addr=0x8)
    assert not site.origin_traced
    # Unchanged from before the trace: the faulting frame stands.
    assert site.blamed is not None and site.blamed.fn_name == "reader"


def test_a_library_frame_that_produced_it_names_its_crate_caller() -> None:
    frames = [_fr("core::ptr::read", "/rustc/x/ptr.rs"),
              _fr("crate::caller", "src/a.rs", (0x5555_0000, 8))]
    assert traced_origin_index(frames, 0x7) == 1


def test_no_fault_address_traces_nothing() -> None:
    frames = [_fr("crate::reader", "src/a.rs", (0x8, 56)),
              _fr("crate::maker", "src/b.rs")]
    assert traced_origin_index(frames, None) is None
    site = site_from_frames("tests/t.rs", "t", "SIGSEGV", frames)
    assert site.blamed is not None and site.blamed.fn_name == "reader"


def test_a_stub_abort_is_never_traced() -> None:
    frames = [_fr("crate::reader", "src/a.rs", (0x8, 56)),
              _fr("crate::maker", "src/b.rs")]
    site = site_from_frames("tests/t.rs", "t", "SIGABRT", frames,
                            fault_addr=0x8)
    assert not site.origin_traced


_BOX_ACCESS = [{
    "func": "<alloc::boxed::Box<dyn core::error::Error> as core::fmt::Display>::fmt",
    "file": "/rustc/library/alloc/src/boxed.rs", "line": 1,
}]


def _cell_window(lines: list[str]):
    from pipeline_core.domain.crash_site import SourceWindow
    return SourceWindow(file_rel="tests/cell_gen_1.rs", first_line=1,
                        lines=tuple(lines))


_CELL_DECLARED = frozenset({
    "make", "repack", "wrap", "other", "msg",
    "into_boxed_dyn_error",
    "reallocate_into_boxed_dyn_error_without_backtrace", "downcast"})


def _recover(lines: list[str], declared: frozenset[str] | None = _CELL_DECLARED):
    from pipeline_core.domain.crash_site import recover_owner_from_caller_source
    reader = None if declared is None else (
        lambda rel: declared if rel == "src/cell.rs" else None)
    return recover_owner_from_caller_source(
        [], _BOX_ACCESS, None, source_window=_cell_window(lines),
        declared_in=reader)


def test_the_fault_line_resolves_through_the_name_it_reads() -> None:
    """Two calls in the window. The fault reads `boxed`, bound on the line
    that calls `repack`. The frame is line 0, anchored at the test file's
    crate file. A later `let` shadows an earlier one, so the nearest
    binder names. Two declared calls on the binding line name nobody."""
    lines = [
        "fn test_take() {",
        "    let cell = make();",
        "    let boxed = cell.repack();",
        '    assert_eq!(boxed.to_string(), "io");',
    ]
    with using_driver(GenericLanguageDriver()):
        frame = _recover(lines)
        two_calls = _recover([
            "fn test_take() {",
            "    let boxed = cell.repack(other.wrap());",
            '    assert_eq!(boxed.to_string(), "io");',
        ])
        shadowed = _recover([
            "fn test_take() {",
            "    let boxed = cell.wrap();",
            "    let boxed = boxed.repack();",
            '    assert_eq!(boxed.to_string(), "io");',
        ])
        no_binding = _recover([
            "fn test_take() {",
            "    let a = cell.repack();",
            "    let b = cell.other();",
            "    assert_eq!(1, 2);",
        ])
        one_call = _recover([
            "fn test_take() {",
            "    cell.repack();",
            "    assert_eq!(1, 2);",
        ])
        unreadable = _recover(lines, declared=None)
    assert frame is not None
    assert frame.fn_name == "repack" and frame.line == 0
    assert frame.file_rel == "src/cell.rs"
    assert frame.named_by == ("tests/cell_gen_1.rs", 3)
    assert two_calls is None and no_binding is None
    assert shadowed is not None and shadowed.fn_name == "repack"
    assert shadowed.named_by == ("tests/cell_gen_1.rs", 3)
    assert one_call is not None and one_call.fn_name == "repack"
    # No declared names to count with: the binding rule does not run and
    # the window's unique-name gate sees `make` and `repack`.
    assert unreadable is None


def test_a_faulting_line_that_calls_a_declared_fn_names_nobody() -> None:
    """The recorded shape of `tests/error_gen_1.rs:31-33`
    (`asan-20261015T120750691462.txt`): the fault is on the line that calls
    the conversion, whose receiver a constructor bound one line above. The
    text cannot say whether the fault ran in the call or in its receiver.
    Negative control: when the faulting line's call is not a declared fn,
    the binding is followed and names the constructor — the outcome the
    rule that skipped the faulting line's own call produced on the
    recorded report."""
    lines = [
        "fn test_reallocate_into_boxed_dyn_error_without_backtrace() {",
        '    let err = Error::msg("original");',
        "    let boxed = err.reallocate_into_boxed_dyn_error_without_backtrace();",
    ]
    with using_driver(GenericLanguageDriver()):
        own = _recover(lines)
        control = _recover(lines, declared=frozenset({"msg"}))
    assert own is None
    assert control is not None and control.fn_name == "msg"


def test_an_origin_crate_frame_outranks_the_access_window() -> None:
    """A free stack that ran a crate fn names the body that freed. The
    recorded 2026-10-15 reports carried the conversion on their free stack
    and the window's guess replaced it. A destructor alone is the victim,
    and the window is still read. Negative control: no origin frame, and
    the window names the binder."""
    window = _cell_window([
        "fn test_take() {",
        "    let cell = make();",
        "    let boxed = cell.repack();",
        '    assert_eq!(boxed.to_string(), "io");',
    ])
    reader = lambda rel: _CELL_DECLARED  # noqa: E731
    conv = [{"func": "cell::Cell::wrap", "file": "src/cell.rs", "line": 12}]
    dtor = [{"func": "<cell::Cell as core::ops::drop::Drop>::drop",
             "file": "src/cell.rs", "line": 40}]
    with using_driver(GenericLanguageDriver()):
        named = site_from_frames(
            "tests/cell_gen_1.rs", "t", "AddressSanitizer: heap-use-after-free",
            _BOX_ACCESS, origin_raw_frames=conv, origin_capable=True,
            source_window=window, declared_in=reader)
        victim = site_from_frames(
            "tests/cell_gen_1.rs", "t", "AddressSanitizer: heap-use-after-free",
            _BOX_ACCESS, origin_raw_frames=dtor, origin_capable=True,
            source_window=window, declared_in=reader)
        bare = site_from_frames(
            "tests/cell_gen_1.rs", "t", "AddressSanitizer: SEGV",
            _BOX_ACCESS, origin_capable=True,
            source_window=window, declared_in=reader)
    assert named.blamed is not None and named.blamed.fn_name == "wrap"
    assert victim.blamed is not None and victim.blamed.fn_name == "repack"
    assert bare.blamed is not None and bare.blamed.fn_name == "repack"


def test_a_catalog_window_still_names_the_bound_family_call() -> None:
    """The catalog matcher already names a window with one family call.
    Two family calls, and the fault reads the name one of them bound,
    names that call rather than nobody."""
    one = _recover([
        "fn test_take() {",
        "    let boxed = err.reallocate_into_boxed_dyn_error_without_backtrace();",
        '    assert_eq!(boxed.to_string(), "io");',
    ])
    picked = _recover([
        "fn test_take() {",
        "    let other = err.into_boxed_dyn_error();",
        "    let boxed = err.reallocate_into_boxed_dyn_error_without_backtrace();",
        '    assert_eq!(boxed.to_string(), "io");',
    ])
    both = _recover([
        "fn test_take() {",
        "    let boxed = err.into_boxed_dyn_error(other.downcast());",
        '    assert_eq!(boxed.to_string(), "io");',
    ])
    assert one is not None
    assert one.fn_name == "reallocate_into_boxed_dyn_error_without_backtrace"
    assert picked is not None and picked.fn_name == one.fn_name
    assert picked.named_by == ("tests/cell_gen_1.rs", 3)
    assert both is None


def test_dispatch_holds_the_blamed_origin_and_the_cycle() -> None:
    from pipeline_core.domain.crash_site import (
        CrashFrame,
        CrashSite,
        StackCycle,
        blamed_locators,
        blocker_culprit_tests,
    )
    origin = CrashFrame("src/cell.rs", 4, "into_boxed_dyn_error",
                        "into_boxed_dyn_error")
    site = CrashSite(
        binary_label="tests/cell_gen_1.rs", test_name="t_a",
        signal="AddressSanitizer: heap-use-after-free", frames=(),
        origin_frames=(origin,), origin_capable=True)
    loc = FnLocator("src/cell.rs", "into_boxed_dyn_error")
    assert blamed_locators((site,)) == {loc}
    held = blocker_culprit_tests((site,))
    assert set(held) == {loc} and held[loc] == frozenset({"t_a"})
    left = CrashFrame("src/cell.rs", 1, "left", "left")
    right = CrashFrame("src/cell.rs", 2, "right", "right")
    exhausted = CrashSite(
        binary_label="tests/cell_gen_1.rs", test_name="t_b",
        signal="stack-overflow", frames=(left, right),
        cycle=StackCycle(frames=(left, right, left, right, left, right),
                         repeats=3))
    got = blocker_culprit_tests((exhausted,))
    assert set(got) == {FnLocator("src/cell.rs", "left"),
                        FnLocator("src/cell.rs", "right")}
    with using_driver(GenericLanguageDriver()):
        repack = CrashFrame("src/cell.rs", 4, "repack", "repack")
        one = CrashSite(
            binary_label="tests/cell_gen_1.rs", test_name="t_a",
            signal="AddressSanitizer: heap-use-after-free", frames=(),
            origin_frames=(repack,), origin_capable=True)
        two = CrashSite(
            binary_label="tests/cell_gen_1.rs", test_name="t_c",
            signal="AddressSanitizer: heap-use-after-free", frames=(),
            origin_frames=(repack,), origin_capable=True)
        unnamed = CrashSite(
            binary_label="tests/cell_gen_1.rs", test_name=None,
            signal="AddressSanitizer: heap-use-after-free", frames=(),
            origin_frames=(repack,), origin_capable=True)
        rloc = FnLocator("src/cell.rs", "repack")
        assert blamed_locators((one,)) == frozenset({rloc})
        assert blocker_culprit_tests((one, two))[rloc] == frozenset(
            {"t_a", "t_c"})
        assert blocker_culprit_tests((unnamed,))[rloc] == frozenset()


def test_a_destructor_the_trace_stops_at_names_nobody_and_its_caller_is_blamed() -> None:
    """A debugger stop where the innermost crate frame faults on an address
    no frame was handed: the trace names that frame, which it cannot tell
    from a frame that loaded the pointer. When that frame is a destructor by
    its symbol (gdb's linkage name, `<T as core::ops::drop::Drop>::drop`),
    the trace names nobody and the caller that built the value is blamed.
    Negative control: the same frames with gdb's printed name only
    (`crate::{impl#0}::drop`, no destructor mark) keep the trace on drop."""
    def _frames(with_symbol: bool) -> list[dict[str, object]]:
        drop = {"func": "dropper::{impl#0}::drop", "file": "src/lib.rs",
                "line": 7, "ptr_args": [[0x7000, 8]]}
        if with_symbol:
            drop["symbol"] = "<dropper::Holder as core::ops::drop::Drop>::drop"
        return [drop,
                {"func": "core::ptr::drop_in_place<dropper::Holder>",
                 "file": "/rustc/x/library/core/src/ptr/mod.rs", "line": 1},
                {"func": "dropper::release", "file": "src/lib.rs", "line": 13,
                 "ptr_args": []}]

    site = site_from_frames("tests/releases.rs", "releases", "SIGSEGV",
                            _frames(True), fault_addr=16)
    assert not site.origin_traced and not site.origin_traced_at_reader
    assert site.blamed_locator == FnLocator("src/lib.rs", "release")
    control = site_from_frames("tests/releases.rs", "releases", "SIGSEGV",
                               _frames(False), fault_addr=16)
    assert control.origin_traced_at_reader
    assert control.blamed_locator == FnLocator("src/lib.rs", "drop")
