"""Per-unit bookkeeping of one fill's attempts — a unit's LAST outcome wins.

The fill's routes used to keep three monotone sets: `dispatched` (stamped
when a unit is handed to a fill cycle, after the skip gates), `reverted`
(spliced then restored to the pre-fill snapshot: peel, compile, U2,
regression) and `answer_lost` (the model was paid and no routable reply
came back). "Kept" was never recorded; it was DERIVED as
`dispatched − reverted − lost`, and `reverted` only ever grew. That is
wrong the moment one route run retries: run `1577437-c93d5c26` iter 4
(2026-09-06 22:15–22:30 UTC) ran the file-first route on `src/error.rs`
twice in one route run — attempt 1 `reverted (compile failed;
codes=['E0596'])`, attempt 2 `GUARD src/error.rs: re-fill KEPT on
non-regression` then `FILE FILLED 14 fn(s) in one pass` — and the route
row read `REVERTED after 13.9 min (2 call(s), net +0, kept 0, reverted
11)`. Attempt 1's eleven revert marks outlived attempt 2's keep of the
same eleven units, so the row graded a run that landed fourteen bodies
as REVERTED, the next fill's schedule ranked the file `rebuy_reverted`
(weight 0, deferred under any budget), and the same set fed the
landed-change audit's FLAT subtraction and the fill result's
`reverted_fns`.

This object is the one source of truth those readers now share: a
per-unit LAST outcome (`kept`, `reverted`, `lost`), from which the
reverted, lost and kept views are derived. The rules, each a contract a
reader already relied on:

* a keep or a revert is a JUDGMENT of the body on the tree and overwrites
  whatever the unit's previous attempt recorded — a unit is reverted at
  the end of the fill only if its last judged attempt was reverted;
* a lost answer is not a judgment: it is recorded only for a unit no
  attempt has judged yet, so a revert followed by a lost retry stays a
  real attempt (the result build used to subtract `reverted` from
  `answer_lost` for exactly this), and a kept remainder is never
  re-ranked never-served by a later attempt's empty reply;
* a dispatch opens a new attempt and clears a LOST mark — the per-impl
  route already did this for a lost file-first answer ("the released
  fallback running is exactly what makes the unit attempted after all");
  a KEPT or REVERTED mark stands until the new attempt is judged.

`since(before, now)` reads one route run's doing off two snapshots. Every
note carries a tick, so the run's units are exactly those it dispatched
or marked after the `before` snapshot, split by their outcome NOW: a unit
reverted then kept in the same run is kept; a unit an earlier run
dispatched and reverted that this run re-dispatched and reverted again is
reverted (the old difference of two monotone sets saw neither event and
reported nothing for it).

`sticky_reverts=True` is the pre-fix rule (a revert mark is never
cleared) and exists only as the negative control of the recorded
fixture; the loop never constructs it.
"""
from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pipeline_core.domain.models import FnLocator

__all__ = [
    "AttemptOutcome",
    "AttemptsDelta",
    "AttemptsSnapshot",
    "FillAttempts",
]


class AttemptOutcome(StrEnum):
    """How a unit's last attempt in this fill ended."""

    KEPT = "kept"
    REVERTED = "reverted"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class AttemptsSnapshot:
    """The views at one instant: taken at a route run's start and end.
    `tick` is the last note's stamp; `dispatched_at` / `marked_at` hold
    each unit's latest dispatch / outcome stamp, so a later snapshot can
    say which units were touched after this one."""

    dispatched: frozenset[FnLocator] = frozenset()
    kept: frozenset[FnLocator] = frozenset()
    reverted: frozenset[FnLocator] = frozenset()
    lost: frozenset[FnLocator] = frozenset()
    tick: int = 0
    dispatched_at: Mapping[FnLocator, int] = field(default_factory=dict)
    marked_at: Mapping[FnLocator, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttemptsDelta:
    """What one route run did, by each touched unit's outcome at its end."""

    dispatched: frozenset[FnLocator]
    kept: frozenset[FnLocator]
    reverted: frozenset[FnLocator]
    lost: frozenset[FnLocator]


class FillAttempts:
    """Thread-safe; every method takes its own lock."""

    def __init__(self, *, sticky_reverts: bool = False) -> None:
        self._lock = threading.Lock()
        self._tick = 0
        self._dispatched_at: dict[FnLocator, int] = {}
        self._last: dict[FnLocator, AttemptOutcome] = {}
        self._marked_at: dict[FnLocator, int] = {}
        self._sticky = sticky_reverts

    def note_dispatched(self, units: Iterable[FnLocator]) -> None:
        with self._lock:
            self._tick += 1
            for u in units:
                self._dispatched_at[u] = self._tick
                if self._last.get(u) is AttemptOutcome.LOST:
                    del self._last[u]
                    del self._marked_at[u]

    def _mark(self, u: FnLocator, outcome: AttemptOutcome) -> None:
        self._last[u] = outcome
        self._marked_at[u] = self._tick

    def note_kept(self, units: Iterable[FnLocator]) -> None:
        with self._lock:
            self._tick += 1
            for u in units:
                if self._sticky and self._last.get(u) is AttemptOutcome.REVERTED:
                    continue
                self._mark(u, AttemptOutcome.KEPT)

    def note_reverted(self, units: Iterable[FnLocator]) -> None:
        with self._lock:
            self._tick += 1
            for u in units:
                self._mark(u, AttemptOutcome.REVERTED)

    def note_lost(self, units: Iterable[FnLocator]) -> None:
        with self._lock:
            self._tick += 1
            for u in units:
                if u not in self._last:
                    self._mark(u, AttemptOutcome.LOST)

    def _view(self, outcome: AttemptOutcome) -> frozenset[FnLocator]:
        return frozenset(u for u, o in self._last.items() if o is outcome)

    def snapshot(self) -> AttemptsSnapshot:
        with self._lock:
            return AttemptsSnapshot(
                dispatched=frozenset(self._dispatched_at),
                kept=self._view(AttemptOutcome.KEPT),
                reverted=self._view(AttemptOutcome.REVERTED),
                lost=self._view(AttemptOutcome.LOST),
                tick=self._tick,
                dispatched_at=dict(self._dispatched_at),
                marked_at=dict(self._marked_at))

    @property
    def dispatched(self) -> frozenset[FnLocator]:
        return self.snapshot().dispatched

    @property
    def reverted(self) -> frozenset[FnLocator]:
        return self.snapshot().reverted

    @property
    def lost(self) -> frozenset[FnLocator]:
        return self.snapshot().lost

    @staticmethod
    def since(before: AttemptsSnapshot, now: AttemptsSnapshot) -> AttemptsDelta:
        """One run's doing: the units it dispatched or marked after
        `before`, by their outcome at the run's end. A dispatched unit no
        attempt judged (the per-fn route records no keep) counts as kept,
        as it always did."""
        dispatched = frozenset(
            u for u, t in now.dispatched_at.items() if t > before.tick)
        touched = dispatched | frozenset(
            u for u, t in now.marked_at.items() if t > before.tick)
        return AttemptsDelta(
            dispatched=dispatched,
            kept=touched - now.reverted - now.lost,
            reverted=touched & now.reverted,
            lost=touched & now.lost)
