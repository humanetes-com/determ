"""The port every fill route answers to, so the fill can be scheduled.

Before this port the five routes — slice sessions, formatter pairs,
file-first, per-impl, per-fn — were a fixed sequence of closures inside one
function and one method, each knowing its own preconditions and nothing
about the others' payoff; the order could only be changed by adding a
precondition to a route. A route that implements :class:`FillRoute` says
what it WOULD do this fill (`plan`) as :class:`RouteRun` values the
schedule can order, and does one of them (`run`) under a :class:`RunClock`
that gates its next claim.

`RouteRunReport` is what a route knows about its own run; the executor
(`fill_scheduler.execute_fill`) completes it into a `RouteLedgerRow` with
the wall clock and the suite reading it measures around the run, so every
route's payoff is read by the same instrument.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pipeline_core.domain.fill_attempts import AttemptsDelta, AttemptsSnapshot
from pipeline_core.domain.fill_class import FillClassHistogram
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunClock,
)
from pipeline_core.domain.models import ClusterFillResult, FnLocator

__all__ = [
    "FillRoute",
    "PreparedFill",
    "RouteLedger",
    "RouteRunReport",
    "RouteSnap",
]


@dataclass(frozen=True, slots=True)
class RouteSnap:
    """The fill's shared counters at a run's start, so the run's own
    landings can be read off the difference at its end: the attempt
    ledger (units and calls, each stamped with the run that made it) and
    the classes this run's thread has classified so far. A route adapter
    takes one from the fill context when its run opens and hands it back
    for the report; it never reads inside."""

    units: AttemptsSnapshot
    classes: FillClassHistogram


@dataclass(frozen=True, slots=True)
class RouteRunReport:
    """What a route reports about one of its runs."""

    model_calls: int = 0
    kept: frozenset[FnLocator] = frozenset()
    reverted: frozenset[FnLocator] = frozenset()
    dispatched: frozenset[FnLocator] = frozenset()
    classes: Mapping[str, int] = field(default_factory=dict)
    #: The route found nothing to do when it opened (the targets were
    #: served by an earlier run, or the plan was stale).
    empty: bool = False
    #: The route declined every target (empty bodies), nothing landed.
    declined: bool = False
    #: A graceful stop ended the run before it finished.
    stopped: bool = False
    #: The route's own verdict-delta reading, when it has one (a session's
    #: last judged turn); None means "let the executor's reading decide".
    own_delta: int | None = None
    #: The run measured nothing it can be graded on: its only landings (if
    #: any) came from reasoning-capped replies. A truncated call is not a
    #: measured refusal, so the ledger grades such a run UNMEASURED rather
    #: than FLAT and the next schedule ranks it as never served.
    unmeasured: bool = False

    @classmethod
    def from_delta(cls, delta: AttemptsDelta,
                   classes: Mapping[str, int]) -> RouteRunReport:
        """The report of a run read off its own attempt delta (the units
        and calls stamped with the run's label — `FillAttempts.since`) and
        its own class histogram delta. A delta that touched nothing and
        made no call is an EMPTY run: the route returned before its first
        dispatch (run `2011677`, 2026-09-07 10:26 UTC: `file src/error.rs`
        skipped for exceeding the file-first fn bound was rowed `FLAT …
        0 call(s), kept 1` off a sibling's dispatch in the same tick
        window, so the identity would have re-entered as a flat re-buy).
        `declined` is a run whose only classified outcome was the model
        declining every target and that kept nothing."""
        declined_only = bool(classes) and set(classes) == {"declined"}
        return cls(
            model_calls=delta.calls,
            kept=delta.kept, reverted=delta.reverted,
            dispatched=delta.dispatched, classes=dict(classes),
            empty=delta.empty,
            declined=declined_only and not delta.kept)


@runtime_checkable
class FillRoute(Protocol):
    """One fill route as the schedule sees it."""

    @property
    def kind(self) -> RouteKind: ...

    @property
    def parallel(self) -> bool:
        """True when CONSECUTIVE runs of this route in the schedule may
        execute concurrently (they touch disjoint files and their shared
        state is already serialised by the splice and counter locks): the
        file-first and per-impl routes. False for the session (one tree,
        one transcript) and the pair route."""
        ...

    def plan(self) -> Sequence[RouteRun]:
        """The runs this route would do on this fill's batch, in the route's
        own order. Computed once at fill start; a run whose targets an
        earlier run served is `empty` when it opens."""
        ...

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        """Execute one planned run. Must poll `clock.may_claim()` before
        every unit / turn / session it opens and never interrupt one in
        flight; must return, never raise, on a route-internal failure."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedFill:
    """What `prepare_impl_fill` hands the engine: the four impl-path routes
    over one shared fill context, the hollow relation of this fill's
    leftover tests, and the result the context yields once every run has
    executed."""

    routes: tuple[FillRoute, ...]
    hollow: Mapping[str, frozenset[FnLocator]]
    finish: Callable[[], ClusterFillResult]
    #: Body identity for a set of targets on the tree as it stands NOW
    #: (re-read per call, so a run planned after another landed keys on the
    #: new bodies).
    bodies_sha: Callable[[frozenset[FnLocator]], str]
    #: How many bodies the four routes have landed so far this fill.
    landed: Callable[[], int] = lambda: 0
    #: Other-file table slots a compiling pair crash-revert recovered.
    #: The engine unions them into crash_blamed so per-fn and the
    #: schedule can open a file the pair cannot write. Same-file slots
    #: join the pair write set instead and must not be crash-blamed
    #: (the CRASHED / faulting-frame grade rewrote those slots, compiled,
    #: and reddened ten already-passing tests on run ``100178`` fill 1).
    #: Empty (the default) is every fill with no third-file slot.
    crash_callees: frozenset[FnLocator] = frozenset()


@runtime_checkable
class RouteLedger(Protocol):
    """Where route rows live (`adapters/route_ledger_store.py`)."""

    def load(self, project: str, cluster: str) -> list[RouteLedgerRow]: ...

    def append(self, project: str, rows: Sequence[RouteLedgerRow]) -> int: ...
