"""Per-unit bookkeeping of one fill's attempts — a unit's LAST outcome wins,
and every note carries the route run that made it.

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

A tick window is one stream for the whole fill, and the file and per-impl
routes run their consecutive runs as ONE WAVE of concurrent workers
(`PIPELINE_PARALLELISM=4`), so a run's window holds every sibling's notes
too. Run `1212998-d61fbd2c` iter 1 (2026-09-06 13:55 UTC) ran `file
src/context.rs`, `file src/error.rs` and `file src/ptr.rs` as one wave:
the context.rs run dispatched and reverted its one target, `source`, and
its row read `FLAT … kept 9, reverted 1` — the nine keeps were error.rs's
eight and ptr.rs's one, the row's `model_calls` was the wave's, and the
grade was FLAT rather than REVERTED because a sibling's keep sat in the
window; the next fill ranked the identity `rebuy_flat`. The same fill's
error.rs and ptr.rs rows carried context.rs's revert. Every note therefore
also carries the LABEL of the route run that made it — read from the
run scope the executor binds on the worker that runs the route
(`fill_context.route_scope`), or passed explicitly — and
`since(before, now, label=…)` returns only the units and calls stamped
with that label (an unlabelled note counts for any run, the rule the
keep-log reader applies: a route that could not bind its label is a
single run executing alone). `label=None` keeps the whole-window reading
for a caller that wants the fill's union.

The model-call counter lives here too, under the same lock and the same
label, so a row's `model_calls` is the run's own calls and the file
route's increment is no longer an unguarded read-modify-write on a
closure integer.

`sticky_reverts=True` is the pre-fix rule (a revert mark is never
cleared) and exists only as the negative control of the recorded
fixture; the loop never constructs it.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pipeline_core.domain.fill_context import current_route
from pipeline_core.domain.models import FnLocator

__all__ = [
    "AttemptOutcome",
    "AttemptsDelta",
    "AttemptsSnapshot",
    "FillAttempts",
    "current_run_label",
]


def current_run_label() -> str:
    """The label of the route run executing now; "" outside any run."""
    return current_route()[1]


class AttemptOutcome(StrEnum):
    """How a unit's last attempt in this fill ended."""

    KEPT = "kept"
    REVERTED = "reverted"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class AttemptsSnapshot:
    """The views at one instant: taken at a route run's start and end.
    `tick` is the last note's stamp; `dispatched_at` / `marked_at` hold
    each unit's latest dispatch / outcome stamp, and `dispatched_by` /
    `marked_by` the label of the run that made it, so a later snapshot
    can say which units were touched after this one and by whom. `calls`
    is the fill's model-call count so far, `calls_by_label` its split by
    run label ("" for calls made outside any run)."""

    dispatched: frozenset[FnLocator] = frozenset()
    kept: frozenset[FnLocator] = frozenset()
    reverted: frozenset[FnLocator] = frozenset()
    lost: frozenset[FnLocator] = frozenset()
    tick: int = 0
    dispatched_at: Mapping[FnLocator, int] = field(default_factory=dict)
    marked_at: Mapping[FnLocator, int] = field(default_factory=dict)
    dispatched_by: Mapping[FnLocator, str] = field(default_factory=dict)
    marked_by: Mapping[FnLocator, str] = field(default_factory=dict)
    calls: int = 0
    calls_by_label: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttemptsDelta:
    """What one route run did, by each touched unit's outcome at its end,
    and the model calls it made."""

    dispatched: frozenset[FnLocator]
    kept: frozenset[FnLocator]
    reverted: frozenset[FnLocator]
    lost: frozenset[FnLocator]
    calls: int = 0

    @property
    def empty(self) -> bool:
        """The run touched no unit and made no call: it never opened
        (a file whose fn count exceeds the file-first bound, a pair the
        refusal gate declined before dispatch). Such a run is EMPTY, not
        FLAT — a FLAT row is a re-buy the next schedule deranks, an EMPTY
        row is not evidence."""
        return (not self.dispatched and not self.kept and not self.reverted
                and not self.lost and self.calls == 0)


def _owned(stamp: str, label: str | None) -> bool:
    """The keep-log rule: a label filter takes notes stamped with that
    label or with none; no filter takes every note."""
    return label is None or not stamp or stamp == label


class FillAttempts:
    """Thread-safe; every method takes its own lock. `run_label` supplies
    the label a note is stamped with when the caller passes none — by
    default the run scope bound on the calling thread."""

    def __init__(self, *, sticky_reverts: bool = False,
                 run_label: Callable[[], str] = current_run_label) -> None:
        self._lock = threading.Lock()
        self._tick = 0
        self._dispatched_at: dict[FnLocator, int] = {}
        self._dispatched_by: dict[FnLocator, str] = {}
        self._last: dict[FnLocator, AttemptOutcome] = {}
        self._marked_at: dict[FnLocator, int] = {}
        self._marked_by: dict[FnLocator, str] = {}
        self._calls = 0
        self._calls_by_label: dict[str, int] = {}
        self._sticky = sticky_reverts
        self._run_label = run_label

    def _label(self, label: str | None) -> str:
        return self._run_label() if label is None else label

    def note_dispatched(self, units: Iterable[FnLocator], *,
                        label: str | None = None) -> None:
        stamp = self._label(label)
        with self._lock:
            self._tick += 1
            for u in units:
                self._dispatched_at[u] = self._tick
                self._dispatched_by[u] = stamp
                if self._last.get(u) is AttemptOutcome.LOST:
                    del self._last[u]
                    del self._marked_at[u]
                    del self._marked_by[u]

    def _mark(self, u: FnLocator, outcome: AttemptOutcome, stamp: str) -> None:
        self._last[u] = outcome
        self._marked_at[u] = self._tick
        self._marked_by[u] = stamp

    def note_kept(self, units: Iterable[FnLocator], *,
                  label: str | None = None) -> None:
        stamp = self._label(label)
        with self._lock:
            self._tick += 1
            for u in units:
                if self._sticky and self._last.get(u) is AttemptOutcome.REVERTED:
                    continue
                self._mark(u, AttemptOutcome.KEPT, stamp)

    def note_reverted(self, units: Iterable[FnLocator], *,
                      label: str | None = None) -> None:
        stamp = self._label(label)
        with self._lock:
            self._tick += 1
            for u in units:
                self._mark(u, AttemptOutcome.REVERTED, stamp)

    def note_lost(self, units: Iterable[FnLocator], *,
                  label: str | None = None) -> None:
        stamp = self._label(label)
        with self._lock:
            self._tick += 1
            for u in units:
                if u not in self._last:
                    self._mark(u, AttemptOutcome.LOST, stamp)

    def note_call(self, n: int = 1, *, label: str | None = None) -> None:
        """`n` model calls made by the run executing now."""
        stamp = self._label(label)
        with self._lock:
            self._calls += n
            self._calls_by_label[stamp] = self._calls_by_label.get(stamp, 0) + n

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
                marked_at=dict(self._marked_at),
                dispatched_by=dict(self._dispatched_by),
                marked_by=dict(self._marked_by),
                calls=self._calls,
                calls_by_label=dict(self._calls_by_label))

    @property
    def dispatched(self) -> frozenset[FnLocator]:
        return self.snapshot().dispatched

    @property
    def reverted(self) -> frozenset[FnLocator]:
        return self.snapshot().reverted

    @property
    def lost(self) -> frozenset[FnLocator]:
        return self.snapshot().lost

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    @staticmethod
    def since(before: AttemptsSnapshot, now: AttemptsSnapshot,
              label: str | None = None) -> AttemptsDelta:
        """One run's doing: the units it dispatched or marked after
        `before`, by their outcome at the run's end, and the calls it
        made. With `label`, only the notes stamped with that label (or
        with none) — the run's own doing inside a concurrent wave;
        without it, everything in the window. A dispatched unit no
        attempt judged (the per-fn route records no keep) counts as
        kept, as it always did."""
        dispatched = frozenset(
            u for u, t in now.dispatched_at.items()
            if t > before.tick and _owned(now.dispatched_by.get(u, ""), label))
        touched = dispatched | frozenset(
            u for u, t in now.marked_at.items()
            if t > before.tick and _owned(now.marked_by.get(u, ""), label))
        if label is None:
            calls = now.calls - before.calls
        else:
            calls = sum(
                now.calls_by_label.get(k, 0) - before.calls_by_label.get(k, 0)
                for k in ("", label))
        return AttemptsDelta(
            dispatched=dispatched,
            kept=touched - now.reverted - now.lost,
            reverted=touched & now.reverted,
            lost=touched & now.lost,
            calls=calls)
