"""A unit the last fill's batch held and no route attempted is owed a writer
in the next fill.

On the record (2026-09 to 2026-10-27, `phase_c.charge.undispatched_spared`),
132 unit-iterations were a stub the dispatch batch held on two or more
consecutive fills with no route attempting it (33 on run `3127-6763a5c6`);
the runs that targeted those units were deferred `pair_wait` 101,
`premise_moved` 65, `kind_cap` 29, `coverage_split` 21 and by re-buy
reasons 49 times. Each deferral is right about its own run; none sees that
the unit it left out was left out last fill too. Plan sentence: on the next
fill, the cover opens that unit's first writer whatever deferred it.

NEGATIVE CONTROL: the owed set alone (the code before the change) keeps the
writer deferred. Invented shape, no crate names.
"""
from __future__ import annotations

from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    due_this_fill,
    schedule,
    unrouted_units,
)
from pipeline_core.domain.models import FnLocator

_LEAF = FnLocator("src/b.rs", "leaf")
_HEAD = FnLocator("src/a.rs", "head")
_TAIL = FnLocator("src/c.rs", "tail")


def _pair() -> RouteRun:
    return RouteRun(kind=RouteKind.PAIR, label="pair src/a.rs::head + src/c.rs::tail",
                    targets=frozenset({_HEAD, _TAIL}),
                    target_tests=frozenset({"t_one", "t_two"}),
                    files=("src/a.rs", "src/c.rs"), bodies_sha="pair")


def _file() -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label="file src/b.rs",
                    targets=frozenset({_LEAF}), target_tests=frozenset({"t_one"}),
                    files=("src/b.rs",), bodies_sha="f")


def _rows() -> list[RouteLedgerRow]:
    p = _pair()
    return [RouteLedgerRow(kind=p.kind, label=p.label,
                           targets=tuple(sorted(p.targets)), bodies_sha="old",
                           oracle_fp="o", outcome=RunOutcome.REVERTED,
                           target_tests=tuple(sorted(p.target_tests)))]


def test_a_unit_left_unrouted_last_fill_gets_its_writer_opened() -> None:
    runs = [_file(), _pair()]
    batch = frozenset({_LEAF, _HEAD, _TAIL})
    last = unrouted_units(frozenset({_LEAF}))
    due = due_this_fill(frozenset(), last, batch)
    assert due == {_LEAF}
    sched = schedule(runs, _rows(), oracle_fp="o", hollow={}, due=due)
    opened = {s.run.label for s in sched.ordered}
    assert "file src/b.rs" in opened and _pair().label in opened
    # Control: what the red tests owe alone leaves the file run held.
    today = schedule(runs, _rows(), oracle_fp="o", hollow={},
                     due=due_this_fill(frozenset(), frozenset(), batch))
    assert {s.run.label: s.reason for s in today.deferred} == {
        "file src/b.rs": "pair_wait"}


def test_the_carry_is_one_fill_and_follows_the_dispatch() -> None:
    """A unit the next batch dropped is not carried: the dispatch decided
    again. A cfg-excluded stub is the filter working, and no reading carries
    nothing."""
    assert due_this_fill(frozenset(), frozenset({_LEAF}), frozenset({_HEAD})) == frozenset()
    assert unrouted_units(frozenset({_LEAF, _HEAD}), cfg_inactive={_HEAD}) == {_LEAF}
    assert unrouted_units(None) == frozenset()
    assert due_this_fill(frozenset({_HEAD}), frozenset({_LEAF}),
                         frozenset({_HEAD, _LEAF})) == {_HEAD, _LEAF}
