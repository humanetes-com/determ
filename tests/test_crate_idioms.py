"""Characterization for the relocated `<CRATE_IDIOMS>` miner. Pure over
ImplBlock VOs — pins the Self:: constructor-family axis and the empty cases,
and the standing rule: only a body the sweep found proven on the tree is
mined (run `1057870-b427a3a2`, 2026-10-15: a kept, never-measured constructor
calling an accessor on `NonNull::dangling()` was shown to every later prompt
as an idiom to match)."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from pipeline_core.domain import body_standing
from pipeline_core.domain.models import FnLocator, ImplBlock
from pipeline_core.prompts.crate_idioms import (
    extract_crate_idioms,
    extract_filled_fn_bodies,
)


def _block(file: str, members: list[dict]) -> ImplBlock:
    return ImplBlock.from_dict({"file": file, "container": "impl Foo", "members": members})


@pytest.fixture(autouse=True)
def _unbind() -> Iterator[None]:
    yield
    body_standing.bind(None)


def _prove(*units: tuple[str, str, str]) -> None:
    proven: dict[FnLocator, list[str]] = {}
    for file, fn, body in units:
        proven.setdefault(FnLocator(file, fn), []).append(body)
    body_standing.bind(proven)


def test_filled_bodies_skip_stubs_and_other_files() -> None:
    blk = _block("src/a.rs", [
        {"fn_name": "done", "body_text": "{ 1 + 1 }"},
        {"fn_name": "stub", "body_text": "{ unimplemented!() }",
         "has_unimplemented_body": True},
        {"fn_name": "blank", "body_text": "   "},
    ])
    other = _block("src/b.rs", [{"fn_name": "elsewhere", "body_text": "{ 1 }"}])
    _prove(("src/a.rs", "done", "{ 1 + 1 }"), ("src/b.rs", "elsewhere", "{ 1 }"))
    bodies = extract_filled_fn_bodies([blk, other], "src/a.rs")
    names = {b["fn_name"] for b in bodies}
    assert names == {"done"}  # stub (unimplemented) + blank body + other-file all dropped


def test_self_constructor_family_surfaces() -> None:
    blk = _block("src/a.rs", [
        {"fn_name": "a", "body_text": "{ Self::wrap(x) }"},
        {"fn_name": "b", "body_text": "{ Self::wrap(y) }"},
    ])
    _prove(("src/a.rs", "a", "{ Self::wrap(x) }"),
           ("src/a.rs", "b", "{ Self::wrap(y) }"))
    out = extract_crate_idioms([blk], "src/a.rs")
    assert "Self:: family" in out and "Self::wrap" in out


def test_an_unproven_filled_body_is_never_an_idiom() -> None:
    blk = _block("src/a.rs", [
        {"fn_name": "make", "body_text":
            "{ unsafe { Self::build(x, read(NonNull::dangling())) } }"},
        {"fn_name": "a", "body_text": "{ Self::wrap(x) }"},
    ])
    _prove(("src/a.rs", "a", "{ Self::wrap(x) }"))
    assert {b["fn_name"] for b in extract_filled_fn_bodies([blk], "src/a.rs")} \
        == {"a"}
    assert "dangling" not in extract_crate_idioms([blk], "src/a.rs")


def test_a_body_rewritten_since_the_sweep_is_not_vouched_for() -> None:
    blk = _block("src/a.rs", [{"fn_name": "a", "body_text": "{ Self::other(x) }"}])
    _prove(("src/a.rs", "a", "{ Self::wrap(x) }"))
    assert extract_filled_fn_bodies([blk], "src/a.rs") == []


def test_negative_control_no_reading_bound_mines_nothing() -> None:
    # Before the standing rule every filled body was mined; with no sweep
    # reading there is no evidence for any of them.
    blk = _block("src/a.rs", [{"fn_name": "a", "body_text": "{ Self::wrap(x) }"},
                              {"fn_name": "b", "body_text": "{ Self::wrap(y) }"}])
    assert extract_crate_idioms([blk], "src/a.rs") == ""


def test_empty_when_no_filled_fns() -> None:
    assert extract_crate_idioms([], "src/a.rs") == ""
