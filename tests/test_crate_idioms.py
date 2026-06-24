"""Characterization for the relocated `<CRATE_IDIOMS>` miner (P6). Pure over
ImplBlock VOs — pins the Self:: constructor-family axis and the empty cases."""
from __future__ import annotations

from pipeline_core.domain.models import ImplBlock
from pipeline_core.prompts.crate_idioms import (
    extract_crate_idioms,
    extract_filled_fn_bodies,
)


def _block(file: str, members: list[dict]) -> ImplBlock:
    return ImplBlock.from_dict({"file": file, "container": "impl Foo", "members": members})


def test_filled_bodies_skip_stubs_and_free_fns() -> None:
    blk = _block("src/a.rs", [
        {"fn_name": "done", "body_text": "{ 1 + 1 }"},
        {"fn_name": "stub", "body_text": "{ unimplemented!() }",
         "has_unimplemented_body": True},
    ])
    free = ImplBlock.from_dict({"file": "src/a.rs", "container": "",
                                "members": [{"fn_name": "f", "body_text": "{ 0 }"}]})
    bodies = extract_filled_fn_bodies([blk, free], "src/a.rs")
    names = {b["fn_name"] for b in bodies}
    assert "done" in names and "stub" not in names  # stub dropped
    assert "f" not in names  # free fn (no container) dropped


def test_self_constructor_family_surfaces() -> None:
    blk = _block("src/a.rs", [
        {"fn_name": "a", "body_text": "{ Self::wrap(x) }"},
        {"fn_name": "b", "body_text": "{ Self::wrap(y) }"},
    ])
    out = extract_crate_idioms([blk], "src/a.rs")
    assert "Self:: family" in out and "Self::wrap" in out


def test_empty_when_no_filled_fns() -> None:
    assert extract_crate_idioms([], "src/a.rs") == ""
