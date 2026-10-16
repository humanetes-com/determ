"""Which filled bodies a prompt may present as the crate's own code.

Run `1057870-b427a3a2` (2026-10-15): a constructor kept while its tests could
not run called an accessor on `NonNull::dangling()`, and every later prompt of
its file showed that call, in the idioms section and in the file view, as the
crate's code. The reading (`body_standing`) is bound per sweep; the shapes
here are invented.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from pipeline_core.domain import body_standing
from pipeline_core.domain.body_standing import (
    UNVERIFIED_MARK,
    is_proven_text,
    mark_unverified,
    proven_units,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.prompt_holdings import FileView, file_block

A = FnLocator("src/a.rs", "good")
B = FnLocator("src/a.rs", "make")

_FILE = """impl X {
    fn good(&self) -> u32 {
        self.n // the count
    }

    fn make() -> X {
        build(NonNull::dangling())
    }

    fn todo() -> u32 {
        ::core::unimplemented!()
    }
}
"""


@pytest.fixture(autouse=True)
def _unbind() -> Iterator[None]:
    yield
    body_standing.bind(None)


def _bind_good_proven() -> None:
    body_standing.bind(
        {A: ["\n        self.n\n    "]},
        known=[A, B, FnLocator("src/a.rs", "todo")])


def test_a_passing_attributed_test_proves_its_units_on_an_intact_sweep() -> None:
    got = proven_units([], {"t1": [A], "t2": [B], "t3": [B]},
                       passed={"t1", "t2"}, died={"t3"}, intact=True)
    # `make` has a passing test but another of its tests died with it.
    assert got == {A}
    assert proven_units([B], {"t1": [A]}, passed={"t1"}, died=(),
                        intact=False) == {B}


def test_the_file_view_marks_only_a_known_unproven_filled_body() -> None:
    _bind_good_proven()
    out = mark_unverified("src/a.rs", _FILE)
    lines = out.splitlines()
    i = lines.index("    fn make() -> X {")
    assert lines[i - 1] == f"    {UNVERIFIED_MARK}"
    assert out.count(UNVERIFIED_MARK) == 1   # not `good` (proven), not the stub


def test_every_prompt_file_block_carries_the_mark() -> None:
    _bind_good_proven()
    assert UNVERIFIED_MARK in file_block("src/a.rs", _FILE, view=FileView.WHOLE)


def test_a_file_the_reading_never_saw_is_left_alone() -> None:
    _bind_good_proven()
    assert mark_unverified("tests/t.rs", _FILE) == _FILE
    assert is_proven_text(FnLocator("tests/t.rs", "make"), "x") is None


def test_a_rewritten_body_is_not_the_proven_one() -> None:
    _bind_good_proven()
    assert is_proven_text(A, "self.n") is True
    assert is_proven_text(A, "self.n + 1") is False


def test_negative_control_an_unbound_reading_marks_nothing() -> None:
    assert mark_unverified("src/a.rs", _FILE) == _FILE
    assert is_proven_text(B, "build(NonNull::dangling())") is None
