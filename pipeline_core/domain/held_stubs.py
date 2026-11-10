"""Which still-stubbed function each red test stops in: the one answer.

WHY THIS EXISTS. "Which stub stops this test" was answered in four places,
each from its own evidence, and they disagreed. On run `857717-812ee44c`
(2026-10-10, cluster `stable`) the sweep's own output (`StubHold`: the stub a
failing test's first failure sits in) named 13, 7, 9, 4 and 2 stubs on iters
1 to 5. The schedule's slice reading (every stub on a test's executed coverage
slice) named 47, 24, 19, 10 and 3 for the same sweeps. The slice also holds
stubs run during unwinding, after the first panic (the drop of a value whose
test had already failed), and those stop nothing. The dispatch deferral took
a third answer from attribution (every stub attribution names for the test),
and the owed rows a fourth. Each consumer was right about its own reading,
and the batch, the schedule and the session planner ranked the same stub
differently.

`StubHold` is the observation; this value is its dispatch-keyed view, and it
is the only thing any consumer reads to decide that a test is held or that a
unit holds one:

- the schedule's hollow map (:meth:`hollow`), which orders its levels, its
  pair holds and its blocker cover;
- the session planner, which leaves a held test to the unit routes
  (:meth:`holds`): a verdict behind a stub is not a behaviour verdict;
- the dispatch deferral (:meth:`consumer_deferrals`);
- the dispatch rank, whose `HELD` class is :attr:`units`;
- whether a run WRITES a held unit (:meth:`Blockers.writes`).

Test names are keyed bare (`bare_test_name`) because every consumer compares
against bare names: a run's aimed tests, a session's targets, a slice key.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from pipeline_core.domain.attribution import locator_of
from pipeline_core.domain.models import FnLocator, Frontier
from pipeline_core.domain.oracle_ledger import bare_test_name
from pipeline_core.domain.test_outcome import StubHold

__all__ = ["Blockers", "HeldStubs", "offered_after_deferral"]


@dataclass(frozen=True, slots=True)
class HeldStubs:
    """Per red test (bare name), the still-stubbed units its first failure
    sits in. Empty on a sweep that observed none (a red build, no output)."""

    by_test: Mapping[str, frozenset[FnLocator]] = field(default_factory=dict)

    @staticmethod
    def of(hold: StubHold) -> HeldStubs:
        out: dict[str, set[FnLocator]] = {}
        for test, sites in hold.by_test.items():
            if not sites:
                continue
            out.setdefault(bare_test_name(test), set()).update(
                locator_of(s) for s in sites)
        return HeldStubs({t: frozenset(us) for t, us in out.items()})

    @staticmethod
    def of_map(hollow: Mapping[str, Iterable[FnLocator]] | None) -> HeldStubs:
        """From a map already in this shape (a recorded event, a caller that
        was handed the view across a port)."""
        out: dict[str, set[FnLocator]] = {}
        for test, units in (hollow or {}).items():
            us = set(units)
            if us:
                out.setdefault(bare_test_name(test), set()).update(us)
        return HeldStubs({t: frozenset(us) for t, us in out.items()})

    @property
    def units(self) -> frozenset[FnLocator]:
        """Every unit that holds at least one red test."""
        out: set[FnLocator] = set()
        for us in self.by_test.values():
            out |= us
        return frozenset(out)

    def tests_held_by(self, unit: FnLocator) -> frozenset[str]:
        return frozenset(t for t, us in self.by_test.items() if unit in us)

    def holds(self, test: str) -> bool:
        """True when `test`'s first failure sits in a still-stubbed unit."""
        return bare_test_name(test) in self.by_test

    def hollow(self) -> dict[str, frozenset[FnLocator]]:
        """The schedule's map: held test -> the units it stops in."""
        return dict(self.by_test)

    def consumer_deferrals(
        self, frontier: Frontier,
    ) -> tuple[Frontier, dict[FnLocator, frozenset[FnLocator]]]:
        """Split `frontier` into (active, {deferred unit: its holders}).

        A unit is deferred when it has red tests and every one of them is held
        by a unit other than itself: whatever it is given, those tests stop at
        the other stub first. It is recomputed every sweep, so a deferred unit
        comes back the sweep its holder gets a body. A unit with no tests (a
        stub unioned in with no attribution) and a unit that holds one of its
        own tests stay active."""
        active: Frontier = {}
        deferred: dict[FnLocator, frozenset[FnLocator]] = {}
        for unit, tests in frontier.items():
            holders: set[FnLocator] = set()
            blocked = bool(tests)
            for t in tests:
                hs = self.by_test.get(bare_test_name(t), frozenset())
                if not hs or unit in hs:
                    blocked = False
                    break
                holders |= hs
            if blocked:
                deferred[unit] = frozenset(holders)
            else:
                active[unit] = tests
        return active, deferred


@dataclass(frozen=True, slots=True)
class Blockers:
    """What the fill must write before red tests can move, and the one
    answer to "does this run write that blocker".

    Two kinds of blocker: a held stub (:class:`HeldStubs`, a red test's
    first failure sits in it) and a crash culprit (the sweep's crash blame,
    `crash_site.blamed_tests`: culprit -> the tests whose crash convicted
    it). Every consumer of "this run writes a blocker" reads
    :meth:`writes`: the schedule's blockers-first tier (`_level`), its
    blocker cover (`_cover_blockers`), the unmeasurable demotion's crash
    exemption (`_run_is_unmeasurable`) and the invariant check
    (`schedule_invariants`). Run `2157238-210637e6` (2026-10-11) is why
    there is one: the cover asked the aim question and the tier did not, so
    every fill-2 session ranked with the blockers on a passenger stub or
    culprit and opened ahead of the unit routes that aimed at them."""

    held: HeldStubs = field(default_factory=HeldStubs)
    blamed: Mapping[FnLocator, frozenset[str]] = field(default_factory=dict)

    @staticmethod
    def of(hollow: Mapping[str, Iterable[FnLocator]] | None,
           crash_blamed: Mapping[FnLocator, Iterable[str]]
           | Collection[FnLocator] = ()) -> Blockers:
        """From the schedule's inputs. `crash_blamed` is the culprit ->
        tests mapping; a bare collection of culprits (a caller or a record
        that carries no tests) knows no test that crashed there, so no
        session is its writer."""
        if isinstance(crash_blamed, Mapping):
            blamed = {u: frozenset(bare_test_name(t) for t in ts)
                      for u, ts in crash_blamed.items()}
        else:
            blamed = {u: frozenset() for u in crash_blamed}
        return Blockers(HeldStubs.of_map(hollow), blamed)

    @property
    def culprits(self) -> frozenset[FnLocator]:
        return frozenset(self.blamed)

    def tests_stopped_by(self, unit: FnLocator) -> frozenset[str]:
        """The red tests `unit` stops: held in it, or crashed on it."""
        return self.held.tests_held_by(unit) | self.blamed.get(unit, frozenset())

    @property
    def units(self) -> frozenset[FnLocator]:
        """Every blocker: the held stubs and the crash culprits."""
        return self.held.units | self.culprits

    def writes(self, unit: FnLocator, *, targets: Collection[FnLocator],
               aimed_tests: Collection[str], session: bool) -> bool:
        """Whether a run with these targets and aimed tests writes `unit`.

        A unit route writes what it targets. A session's targets are its
        editable set, and the model is asked to make the session's tests
        pass. It writes a blocker only when one of those tests is stopped by
        it: held there (the session planner leaves held tests to the unit
        routes, `slice_session.plan_slices`, so this is a session that
        should not exist), or crashed there. Otherwise the blocker rides in
        another test's slice and nothing in the prompt asks for its body."""
        if unit not in targets:
            return False
        if not session or unit not in self.units:
            return True
        aimed = {bare_test_name(t) for t in aimed_tests}
        stopped = self.held.tests_held_by(unit) | self.blamed.get(
            unit, frozenset())
        return bool(aimed & stopped)


def offered_after_deferral(
    before: Frontier, active: Frontier, offered: Collection[FnLocator],
) -> tuple[Frontier, frozenset[FnLocator]]:
    """`active` (the result of :meth:`HeldStubs.consumer_deferrals` over
    `before`) with every unit of `offered` that the deferral removed put back
    with the tests it had before the split, and the units put back.

    `offered` is what this sweep must offer: the held stubs and the stubs
    the failing tests panicked in while they unwound
    (`test_outcome.StubHold.unwinding`). The deferral is a first-failure
    rule: it defers a unit whose every test first-fails in another stub. A
    stub that only appears while tests unwind is exactly such a unit, since
    its tests are the ones that first fail elsewhere. So without this step,
    the unit that ends their aborts is never ranked on a sweep where the
    fill that measured it could not write it (the share refusal of run
    `630503-f5ca6540`, fill 2, line 2604, found in review 2026-11-09)."""
    out: Frontier = dict(active)
    back: set[FnLocator] = set()
    for unit in offered:
        if unit in before and unit not in out:
            out[unit] = before[unit]
            back.add(unit)
    return out, frozenset(back)
