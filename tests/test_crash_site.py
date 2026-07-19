"""Pins for crash-site domain logic: frame filtering, blame preference
(origin over access), evidence rendering, ASAN report parsing."""

from __future__ import annotations

from pipeline_core.domain.crash_site import (
    bare_fn_name,
    crash_evidence_by_locator,
    parse_asan_report,
    relativize_frame_files,
    site_from_frames,
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
    from pipeline_core.prompts.fix_prompts import build_file_fill_prompt

    kw = {"file_rel": "src/error.rs", "file_text": "fn a() {}",
          "stub_map": [("impl Error", ["construct_from_adhoc"])],
          "tests_block": "", "blueprint": "", "notes": ""}
    without = build_file_fill_prompt(**kw)
    assert "CRASH_EVIDENCE" not in without

    with_ev = build_file_fill_prompt(
        **kw, crash_evidence="stack-use-after-return in construct_from_std")
    assert "<CRASH_EVIDENCE>" in with_ev
    assert "construct_from_std" in with_ev
    assert "do NOT copy that pattern" in with_ev
