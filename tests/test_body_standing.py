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
                       passed={"t1", "t2"}, deaths={"t3": frozenset()},
                       intact=True)
    # `make` has a passing test but another of its tests died unlocated.
    assert got == {A}
    assert proven_units([B], {"t1": [A]}, passed={"t1"}, deaths={},
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


# ---- where a death is placed decides whom it refutes ----
#
# Runs `3178980-c42f7944` (iter 2: 33 of 394 proven, 18 more left out over
# one aborted test) and `4112620-8a89f05c` (iter 2: 39 of 367, 17 left out
# over two; the one localized death a segfault blamed on `src/error.rs::drop`).
# Any attributed death used to remove the unit. The shapes are invented:
# `read` and `make_a` in `src/lib.rs`.

READ = FnLocator("src/lib.rs", "read")
MAKE_A = FnLocator("src/lib.rs", "make_a")


def _old_rule(attributed, passed, died):
    """The rule this batch replaced: any attributed death removes the unit.
    Kept as the negative control."""
    out = set()
    by_unit: dict = {}
    for t, us in attributed.items():
        for u in us:
            by_unit.setdefault(u, set()).add(t)
    for u, ts in by_unit.items():
        if ts & set(passed) and not ts & set(died):
            out.add(u)
    return out


def _fabricated():
    """`read` was handed the bad address its caller `make_a` made: the
    trace names `make_a` the producer, and the stack also names `read`."""
    from pipeline_core.domain.crash_site import site_from_frames
    return site_from_frames("tests/t.rs", "t_dead", "SIGSEGV", [
        {"func": "crate::read", "file": "src/lib.rs", "line": 2,
         "ptr_args": [[0x8, 8]]},
        {"func": "crate::make_a", "file": "src/lib.rs", "line": 6,
         "ptr_args": []}], fault_addr=0x8)


def _reader_only():
    """The trace stops at `make_a`, the frame that read the bad address:
    no producer is named."""
    from pipeline_core.domain.crash_site import site_from_frames
    return site_from_frames("tests/t.rs", "t_dead", "SIGSEGV", [
        {"func": "crate::make_a", "file": "src/lib.rs", "line": 6,
         "ptr_args": [[0x1000, 8]]}], fault_addr=0x9999_0000)


def _sanitized():
    """A use-after-free whose sanitizer origin is `make_a`, read in `read`."""
    from pipeline_core.domain.crash_site import site_from_frames
    return site_from_frames(
        "tests/t.rs", "t_dead", "AddressSanitizer: heap-use-after-free",
        [{"func": "crate::read", "file": "src/lib.rs", "line": 2}],
        origin_raw_frames=[{"func": "crate::make_a", "file": "src/lib.rs",
                            "line": 6}],
        origin_capable=True)


_ATTR = {"t_pass": [READ], "t_dead": [READ]}


def _places(site=None):
    from pipeline_core.domain.body_standing import death_places
    return death_places({"t_dead"},
                        sites=[site] if site is not None else [])


def test_a_death_placed_in_another_unit_leaves_the_unit_proven() -> None:
    deaths = _places(_fabricated())
    assert deaths == {"t_dead": frozenset({MAKE_A})}
    assert proven_units([], _ATTR, passed={"t_pass"}, deaths=deaths,
                        intact=True) == {READ}
    # Negative control: the old clause left it out.
    assert _old_rule(_ATTR, {"t_pass"}, {"t_dead"}) == set()


def test_a_frame_on_the_stack_that_is_not_the_culprit_does_not_refute() -> None:
    """The stack names `read` too. Placing the death by every crate frame
    (`abort_verdict.crash_units_by_test`) would refute `read` again."""
    from pipeline_core.domain.abort_verdict import crash_units_by_test
    site = _fabricated()
    assert READ in crash_units_by_test([site])["t_dead"]
    assert proven_units([], _ATTR, passed={"t_pass"},
                        deaths=_places(site), intact=True) == {READ}


def test_a_death_placed_in_the_unit_refutes_it() -> None:
    attr = {"t_pass": [MAKE_A], "t_dead": [MAKE_A]}
    assert proven_units([], attr, passed={"t_pass"},
                        deaths=_places(_fabricated()), intact=True) == frozenset()


def test_an_unlocated_death_refutes_every_attributed_unit() -> None:
    assert _places() == {"t_dead": frozenset()}
    assert proven_units([], _ATTR, passed={"t_pass"}, deaths=_places(),
                        intact=True) == frozenset()


def test_a_trace_that_stops_at_the_reader_places_nothing() -> None:
    """The debugger's reader is not the producer (run `14235-bf24f931`
    iter 3, the settlement's own refusal): the death stays unlocated, so
    `read`, which may have made the pointer, stays unproven. Negative
    control: placing it at the raw culprit proves `read`."""
    site = _reader_only()
    assert site.culprit_locators == {MAKE_A} and not site.names_producer
    assert proven_units([], _ATTR, passed={"t_pass"}, deaths=_places(site),
                        intact=True) == frozenset()
    raw = {"t_dead": site.culprit_locators}
    assert proven_units([], _ATTR, passed={"t_pass"}, deaths=raw,
                        intact=True) == {READ}


def test_a_sanitizer_origin_in_another_unit_places_the_death_there() -> None:
    site = _sanitized()
    assert site.names_producer and site.culprit_locators == {MAKE_A}
    assert proven_units([], _ATTR, passed={"t_pass"}, deaths=_places(site),
                        intact=True) == {READ}


def test_a_panicking_death_with_no_site_leaves_every_unit_unproven() -> None:
    """An abort that printed a panic is not a native crash, so the sweep
    hands this function no site. Negative control: placing the death at
    the panic's function proves `read`."""
    deaths = _places()
    assert deaths == {"t_dead": frozenset()}
    assert proven_units([], _ATTR, passed={"t_pass"}, deaths=deaths,
                        intact=True) == frozenset()
    assert proven_units([], _ATTR, passed={"t_pass"},
                        deaths={"t_dead": frozenset({MAKE_A})},
                        intact=True) == {READ}


def test_the_vetoes_split_by_the_same_rule() -> None:
    from pipeline_core.domain.body_standing import death_vetoes
    attr = {"t_pass": [READ, MAKE_A], "t_dead": [READ, MAKE_A]}
    got = death_vetoes(attr, passed={"t_pass"}, deaths=_places(_fabricated()))
    assert set(got.kept) == {READ} and set(got.refused) == {MAKE_A}
    assert got.kept[READ] == (("t_dead", frozenset({MAKE_A})),)


def test_a_qualified_abort_name_meets_a_bare_attribution_key() -> None:
    """An isolated binary prints `module::test`. Attribution keys the leaf.
    The sweep view bares the abort name, and so does the death reading."""
    from pipeline_core.domain.assertion_position import KeyedVerdicts
    from pipeline_core.domain.body_ledger import SweepView
    from pipeline_core.domain.body_standing import death_places
    view = SweepView.of_sweep(
        passed=(), failed=(), vanished=(), intact=True, epoch=0,
        positions={}, aborted=("mod::t_dead",), aborted_silent=("mod::t_quiet",),
        verdicts=KeyedVerdicts())
    assert view.aborted == frozenset({"t_dead"})
    assert view.aborted_silent == frozenset({"t_quiet"})
    deaths = death_places({"mod::t_dead"}, sites=[])
    assert set(deaths) == {"t_dead"}


def test_the_producer_reading_and_the_settlement_agree() -> None:
    """`CrashSite.producer_refusal` is the site half of the settlement's
    `_unactionable`: the same reason, and None exactly when it names one."""
    from pipeline_core.domain.crash_settlement import _unactionable
    for site in (_fabricated(), _reader_only(), _sanitized()):
        got = _unactionable(site, lambda _u: False)
        assert (got is None) == site.names_producer
        if got is not None:
            assert got[0] == site.producer_refusal()
