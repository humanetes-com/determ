"""Pins for propagating a re-authored layout into the LIVE (filled) tree.

The skeleton and the filled tree hold the same type definitions but different fn
BODIES. These pin that a plan built against the skeleton still lands on a tree
whose bodies were filled long ago — the exact drift that made a successful
re-author invisible to every compile.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.layout_tree_splice import apply_layout_to_tree
from pipeline_core.domain.scaffold_rewrite import DefReplacement

_OLD_TYPE = "pub struct Error {\n    ptr: NonNull<ErrorImpl>,\n}"
_NEW_TYPE = ("pub struct Error {\n    ptr: NonNull<ErrorImpl>,\n}\n\n"
             "unsafe impl Send for Error {}\nunsafe impl Sync for Error {}")


def _tree(tmp_path: Path, body: str) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        _OLD_TYPE + "\n\nimpl Error {\n    fn erase(self) -> u8 " + body + "\n}\n")
    return src


def test_type_definition_lands_on_a_tree_whose_bodies_are_filled(tmp_path: Path):
    src = _tree(tmp_path, "{ self.ptr.as_ptr() as u8 }")
    n, err = apply_layout_to_tree(
        {"lib.rs": [DefReplacement(_OLD_TYPE, _NEW_TYPE)]}, src)
    assert (n, err) == (1, "")
    text = (src / "lib.rs").read_text()
    assert "unsafe impl Send for Error {}" in text
    # the filled body is untouched — only the definition was swapped
    assert "self.ptr.as_ptr() as u8" in text


def test_fn_item_is_located_by_signature_despite_a_filled_body(tmp_path: Path):
    # A fn's locate key is its SIGNATURE precisely so it survives body fills;
    # `fn_name` expands the match to the whole item, body included.
    src = _tree(tmp_path, "{ self.ptr.as_ptr() as u8 }")
    n, err = apply_layout_to_tree(
        {"lib.rs": [DefReplacement("fn erase(self) -> u8",
                                   "fn erase(self) -> u16 { 0 }", "erase")]}, src)
    assert (n, err) == (1, "")
    text = (src / "lib.rs").read_text()
    assert "fn erase(self) -> u16 { 0 }" in text
    assert "as_ptr" not in text


def test_a_miss_writes_nothing_at_all(tmp_path: Path):
    # All-or-nothing: a tree holding half the new definitions compiles as
    # neither layout, and the caller cannot tell that from a partial success.
    src = _tree(tmp_path, "{ 0 }")
    before = (src / "lib.rs").read_text()
    n, err = apply_layout_to_tree({
        "lib.rs": [DefReplacement(_OLD_TYPE, _NEW_TYPE)],
        "missing.rs": [DefReplacement(_OLD_TYPE, _NEW_TYPE)],
    }, src)
    assert n == 0 and "unreadable" in err
    assert (src / "lib.rs").read_text() == before


def test_unlocatable_definition_is_reported_not_silently_skipped(tmp_path: Path):
    src = _tree(tmp_path, "{ 0 }")
    n, err = apply_layout_to_tree(
        {"lib.rs": [DefReplacement("struct Absent {}", "struct Absent { x: u8 }")]},
        src)
    assert n == 0 and "could not locate a definition in lib.rs" in err


def test_empty_plan_is_an_error_not_a_silent_success(tmp_path: Path):
    # A component that produced no replacements means the plan builder failed;
    # reporting 0 files written as success would hide it.
    assert apply_layout_to_tree({}, tmp_path)[1] == "empty replacement plan"
