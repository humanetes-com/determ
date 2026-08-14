"""Pins for crash-site domain logic: frame filtering, blame preference
(origin over access), evidence rendering, ASAN report parsing."""

from __future__ import annotations

from pipeline_core.domain.crash_site import (
    asan_stack_excerpt,
    bare_fn_name,
    crash_evidence_by_locator,
    origin_stack_text,
    parse_asan_report,
    relativize_frame_files,
    site_from_frames,
    vanish_is_stub_confounded,
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
    assert "do NOT copy that pattern" in with_ev


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
