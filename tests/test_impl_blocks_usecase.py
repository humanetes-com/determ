"""Unit tests for the impl-block collection use-case
(`pipeline_core.application.reconstruct.impl_blocks.collect_impl_blocks`), P6.

Fake-injected `enumerate_fns`; a real temp src dir provides the byte ranges.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.impl_blocks import collect_impl_blocks


def test_none_when_extractor_unavailable(tmp_path):
    assert collect_impl_blocks(tmp_path, enumerate_fns=lambda _sd: None) is None


def test_empty_records_yield_no_blocks(tmp_path):
    assert collect_impl_blocks(tmp_path, enumerate_fns=lambda _sd: []) == []


def test_groups_fns_into_impl_block(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    text = "impl Foo {\n    fn a() {}\n    fn b() {}\n}\n"
    (src / "lib.rs").write_text(text, encoding="utf-8")
    a_pos = text.index("fn a")
    b_pos = text.index("fn b")
    recs = [
        {"file": "lib.rs", "sig_byte_start": a_pos, "container": "impl Foo",
         "sig_text": "fn a()", "has_unimplemented_body": False},
        {"file": "lib.rs", "sig_byte_start": b_pos, "container": "impl Foo",
         "sig_text": "fn b()", "has_unimplemented_body": True},
    ]
    blocks = collect_impl_blocks(tmp_path / "src", enumerate_fns=lambda _sd: recs)
    assert blocks is not None and len(blocks) == 1
    blk = blocks[0]
    assert blk.container == "impl Foo"
    assert len(blk.members) == 2
    assert len(blk.stub_members) == 1  # only fn b is unimplemented


def test_free_fn_emitted_with_empty_container(tmp_path):
    # A free fn (no impl body contains its sig, extractor container "") still
    # gets its own block with container="" — its `related_containers` is empty.
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("fn free() {}\n", encoding="utf-8")
    recs = [{"file": "lib.rs", "sig_byte_start": 0, "container": "",
             "sig_text": "fn free()", "has_unimplemented_body": False}]
    blocks = collect_impl_blocks(tmp_path / "src", enumerate_fns=lambda _sd: recs)
    assert blocks is not None and len(blocks) == 1
    assert blocks[0].container == ""
    assert blocks[0].related_containers == ()
    assert len(blocks[0].members) == 1
