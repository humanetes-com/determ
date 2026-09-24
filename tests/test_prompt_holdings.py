"""What a prompt already holds: the `<FILE>` grammar, the contents index, and
the answer to a request for a file the conversation carries whole.

Measured 2026-09-24 over every fill since 2026-09-01: 302 of 690 context
requests named a file or type the prompt had already rendered; the per-fn view
of a file is the cluster's cfg-filtered file, and the re-sent answer was the
raw file with the inactive arms added back. The contract is typed at the
writer — every fill-prompt file declares `view="whole"` or `view="part"` — so
the writer-contract tests below build each fill prompt and read its blocks
back; a builder that forgets to declare its view fails here, not live.
"""
from __future__ import annotations

import pytest

from pipeline_core.domain.models import FnRecord, ImplBlock
from pipeline_core.domain.prompt_holdings import (
    CONTENTS_OPEN,
    FileView,
    HeldFile,
    changed_since,
    contents_index,
    file_block,
    held_file_answer,
    outline,
    read_holdings,
    render_contents,
    with_contents,
)

_FILE = """use crate::other::Thing;
/// A table of callbacks.
pub struct Table { slot: fn(u8) -> u8 }
impl Table {
    pub fn new() -> Self { unimplemented!() }
    // fn fake() {} in a comment is not an item
    fn get(&self) -> u8 { let s = "}"; 1 }
}
fn free(a: u8) -> u8 { a + 1 }
"""

_MENU = '\nreply <REQUEST kind="KIND" arg="ARG"/> to ask for more.'


def test_file_block_declares_its_view_and_refuses_an_undeclared_one() -> None:
    blk = file_block("src/t.rs", _FILE, view=FileView.WHOLE,
                     attrs=' editable="get (impl<A> Table<A>)"')
    assert blk.startswith('<FILE path="src/t.rs" view="whole" editable="get (impl<A> Table<A>)">\n')
    with pytest.raises(ValueError):
        file_block("src/t.rs", _FILE, view=FileView.UNKNOWN)


def test_read_holdings_reads_views_tests_and_types() -> None:
    text = (file_block("src/t.rs", _FILE, view=FileView.WHOLE,
                       attrs=' editable="get (impl<A> Table<A>)"')
            + "\n" + file_block("src/o.rs", "pub fn o();", view=FileView.PART)
            + '\n<FILE path="src/legacy.rs">\nfn x() {}\n</FILE>'
            + '\n<TEST path="tests/a.rs">\n#[test] fn t() {}\n</TEST>'
            + "\n<TYPES_IN_SCOPE>\n/// doc\n#[repr(C)]\nstruct Table\n"
            + "  fields:\nexternal struct Backtrace\nenum Kind\n</TYPES_IN_SCOPE>")
    h = read_holdings(text)
    assert [(f.path, f.view) for f in h.files] == [
        ("src/t.rs", FileView.WHOLE), ("src/o.rs", FileView.PART),
        ("src/legacy.rs", FileView.UNKNOWN)]
    # A `>` inside a quoted attribute did not end the tag.
    assert h.files[0].text.startswith("use crate::other::Thing;")
    assert h.tests == ("tests/a.rs",)
    assert h.types == ("Table", "Backtrace", "Kind")
    assert h.whole_view("src/t.rs") is h.files[0]
    assert h.whole_view("src/o.rs") is None       # a part is never whole
    assert h.whole_view("src/legacy.rs") is None  # undeclared is never whole


def test_a_whole_view_the_context_guard_cut_reads_as_a_part() -> None:
    cut = _FILE[:40] + "\n[... 9,000 chars trimmed by context-guard to fit x ...]\n" + _FILE[-30:]
    h = read_holdings(file_block("src/t.rs", cut, view=FileView.WHOLE))
    assert h.files[0].view is FileView.PART


def test_the_index_is_only_for_prompts_that_can_request_and_is_idempotent() -> None:
    body = file_block("src/t.rs", _FILE, view=FileView.WHOLE)
    assert contents_index(body) == ""                       # no request menu
    indexed = with_contents(body + _MENU)
    assert indexed.startswith(CONTENTS_OPEN)
    assert "src/t.rs — the whole file, 9 lines" in indexed
    assert with_contents(indexed) == indexed
    # The index carries no tag syntax: reading the indexed prompt back finds
    # exactly the one real block.
    assert [f.path for f in read_holdings(indexed).files] == ["src/t.rs"]


def test_the_index_lists_one_line_per_path_and_a_whole_view_wins() -> None:
    h = read_holdings(file_block("src/t.rs", "fn a();", view=FileView.PART)
                      + file_block("src/t.rs", _FILE, view=FileView.WHOLE))
    idx = render_contents(h)
    assert idx.count("src/t.rs —") == 1 and "the whole file" in idx
    assert render_contents(read_holdings("no blocks here")) == ""


def test_outline_lists_items_with_their_lines_and_skips_comments() -> None:
    got = {it.line: it.header for it in outline(_FILE)}
    assert got[3].startswith("pub struct Table")
    assert got[4] == "impl Table"
    assert got[5] == "pub fn new() -> Self"
    assert got[7] == "fn get(&self) -> u8"
    assert got[9] == "fn free(a: u8) -> u8"
    assert not any("fake" in h for h in got.values())


def test_changed_since_finds_a_rewritten_body_and_ignores_disk_only_items() -> None:
    view = _FILE.replace("{ a + 1 }", "{ unimplemented!() }")
    # An annotation line outside every body, and an item only the disk has
    # (an inactive cfg arm the view filtered out): neither is a change.
    view = view.replace("fn free", "// >>> TARGET <<<\nfn free")
    disk = _FILE + "#[cfg(off)]\nfn only_on_disk() {}\n"
    assert changed_since(view, disk) == ("fn free(a: u8) -> u8",)
    assert changed_since(_FILE, disk) == ()


def test_the_held_answer_points_outlines_and_carries_rewritten_bodies() -> None:
    view = _FILE.replace("{ a + 1 }", "{ unimplemented!() }")
    ans = held_file_answer("src/t.rs", HeldFile("src/t.rs", FileView.WHOLE, view), _FILE)
    assert "It is not sent again." in ans
    assert "fn free(a: u8) -> u8 { a + 1 }" in ans     # the rewritten body
    assert "L9: fn free(a: u8) -> u8" in ans           # disk line numbers
    assert 'from_line="N"' in ans
    assert "<FILE" not in ans                          # no tag syntax
    fresh = held_file_answer("src/t.rs", HeldFile("src/t.rs", FileView.WHOLE, _FILE), _FILE)
    assert "Nothing in it has changed on disk since." in fresh


# ── the writer contract: every fill prompt that renders a file declares it ──

def _views(prompt: str) -> dict[str, FileView]:
    return {f.path: f.view for f in read_holdings(prompt).files}


def test_per_fn_prompts_render_their_file_whole() -> None:
    from pipeline_core.prompts.context import PerFnFixSections, PerFnSections
    from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt, build_per_fn_prompt
    kw = {"macros": "", "notes": "", "container": "impl Table", "types": "",
          "invariants": "", "siblings": "", "crate_idioms": "", "bound_methods": ""}
    first = build_per_fn_prompt(
        file_text=_FILE, fn_name="new", sig_text="pub fn new() -> Self",
        test_blocks=[], module_name="m", cluster_name="c",
        file_rel="src/t.rs", fr_path=None, sections=PerFnSections(**kw))
    fix = build_per_fn_fix_prompt(
        file_text=_FILE, fn_name="new", sig_text="pub fn new() -> Self",
        test_blocks=[], module_name="m", cluster_name="c",
        file_rel="src/t.rs", prev_body="x", error_excerpt="error[E0425]",
        sections=PerFnFixSections(**kw))
    assert _views(first)["src/t.rs"] is FileView.WHOLE
    assert _views(fix)["src/t.rs"] is FileView.WHOLE


def test_the_file_first_prompt_renders_its_target_whole() -> None:
    from pipeline_core.prompts.fix_prompts import BlockFillPlan, build_file_fill_prompt
    p = build_file_fill_prompt(
        file_rel="src/t.rs", file_text=_FILE,
        fill_plan=[BlockFillPlan("impl Table", ("new",))],
        tests_block="", blueprint="", notes="")
    assert _views(p) == {"src/t.rs": FileView.WHOLE}


def test_the_per_impl_prompt_renders_its_own_file_whole_only_when_given() -> None:
    from pipeline_core.prompts.context import PerImplSections
    from pipeline_core.prompts.per_impl import build_per_impl_prompt
    rec = FnRecord(file="src/t.rs", fn_name="new", container="impl Table",
                   sig_text="pub fn new() -> Self", body_text="unimplemented!()")
    block = ImplBlock(file="src/t.rs", container="impl Table",
                      members=(rec,), stub_members=(rec,))
    kw = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
          "dispatch_family": "", "macros": "", "siblings": "",
          "retrospective_guidance": "", "bound_methods": ""}
    without = build_per_impl_prompt(block, [], [], "m", "c", PerImplSections(**kw))
    with_file = build_per_impl_prompt(
        block, [], [], "m", "c", PerImplSections(**kw, own_file=_FILE))
    # Negative control: the pre-change prompt carried no view of its file.
    assert "src/t.rs" not in _views(without)
    assert _views(with_file)["src/t.rs"] is FileView.WHOLE


def test_the_session_file_is_whole_when_it_fits_and_a_part_when_condensed() -> None:
    from pipeline_core.prompts.slice_session import render_slice_file
    rec = FnRecord(file="src/t.rs", fn_name="new", container="impl Table",
                   sig_text="pub fn new() -> Self", body_text="unimplemented!()")
    whole = render_slice_file("src/t.rs", _FILE, [rec], [rec])
    part = render_slice_file("src/t.rs", _FILE, [rec], [rec], file_chars=10)
    assert _views(whole)["src/t.rs"] is FileView.WHOLE
    assert _views(part)["src/t.rs"] is FileView.PART


def test_the_api_slice_and_the_signature_index_are_parts() -> None:
    from pipeline_core.prompts.repo_spec import _api_slice
    from pipeline_core.prompts.skeleton_index import format_skeleton_index
    api = _api_slice('<FILE path="src/o.rs">\npub fn thing();\n</FILE>',
                     "src/t.rs", {"thing"}, 4000)
    idx = format_skeleton_index([("src/t.rs", _FILE)])
    assert _views(api) == {"src/o.rs": FileView.PART}
    assert _views(idx) == {"src/t.rs": FileView.PART}
