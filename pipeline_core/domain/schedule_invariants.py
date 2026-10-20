"""What every fill schedule must satisfy, whatever its inputs.

WHY. The fill schedule (`domain/fill_schedule.py`) was corrected one incident
at a time for a month — 31 commits between 2026-09-01 and 2026-09-28, twelve
reason codes, most with their own grace or lapse — and every correction was
tested on the fill that exposed it. Nothing stated the properties a schedule
must have for ALL inputs, so a correction to one hold could leave a sibling
hold with the same hole. Run `82925-203ed7b8` (2026-09-28, cluster `stable`)
is the instance that named this module: in fills 5 and 6 the per-fn run
holding the one stub 47-48 of the 49 failing tests first stopped at
(`src/error.rs::context`) was deferred `coverage_split`, held by the file and
impl runs of the same stub — and those two were then deferred as re-buys by a
later step of the same function. Every run that could write the stub was
deferred; the fills spent 2 h 10 min on runs whose tests were hollow on it,
and the fill that finally opened the per-fn run moved the score 135 -> 174.
The 2026-09-18 correction had closed the same shape for a hop pair ("a run is
held only by a run this pass has already OPENED"), and the re-buy deferral
was not part of that pass.

WHAT IS CHECKED. One decision per candidate run: opened, or deferred with a
reason (and, for a hold, the runs it waited on). On that:

- ``holder_not_opened`` — a hold names a run that did not open. Waiting for a
  run that never opens is waiting for nothing: a deferred run writes no
  graded row, so no grace clock can release it.
- ``blocker_unwritten`` — a BLOCKER has no opened run that writes it. A
  blocker is a stub a failing test's first failure sits in (the held view,
  `held_stubs`), or a locator the sweep's crash evidence blames that some
  candidate writes. A held test cannot be judged by any route until its stub
  has a body, so a fill that opens no writer of it spends its budget on
  tests that cannot move. A held stub counts as written only by a run that
  aims at it (`Blockers.writes`), and it is reported even when no candidate
  targets it at all: on run `857717-812ee44c` fills 3-5,
  `src/wrapper.rs::source` held three red tests and was in no candidate
  run's targets, and the old intersection of the blockers with the
  candidates' targets kept the check silent.
- ``nothing_opened`` — candidates exist that aim at something and none opened.
- ``deferred_share`` / ``over_budget`` — a deferred run carries a positive
  share, or the opened shares exceed the budget.
- ``slice_unit_starved`` — a function a red test owes this fill (a tier of
  its executed slice that no prompt has asked under the current evidence,
  `candidate_tiers`) has no opened writer. Run `14141-9a904822`
  (2026-10-08): `src/ensure.rs::render`, the one wrong body behind 16 of the
  21 leftovers, went unwritten from fill 3 to fill 8 while every per-test
  predicate over its 19-function slice was satisfied by writing the others.
- ``owner_unrouted`` — a measured coverage owner the driver admitted (or
  aimed at a newly admitted test) and dispatched is a target of NO candidate
  run, opened or deferred. Run `486447-5207101c` (2026-10-07, fill 6):
  `src/ensure.rs::render` was admitted for six failing `ensure!` tests and
  every slice carrying it was hollow or parked, so nothing wrote it and no
  deferral said why. Measured 2026-10-17 over every recorded hit since
  2026-10-12 joined to its sweep's `phase_c.dispatch.rank` batch: 214 of
  214 were owners the rank left past the dispatch cap, 0 were in the batch
  with no candidate. Its input is therefore the owners the sweep owes a
  writer (`escalation_lifetime.measured_owners_owed`): those in the batch,
  and those the rank left out at its rotation bound
  (`dispatch_rank.starved`). An owner past the cap inside the bound is the
  rank's promise, not a routing hole.

These name no test, unit, trait or crate: the inputs are the schedule's own
decisions, the hollow stubs and the blamed locators. The same checker runs in
three places — the property tests over generated inputs, the executor after
every schedule call (`HEALTH ‼ SCHEDULE_INVARIANT`), and the pattern census
over every recorded `phase_c.fill.schedule` event — so a hole is found the
first fill it opens, not after a flat afternoon.

Deliberately NOT checked here: fairness across fills (a unit deferred for k
fills running). That needs the ledger's history, not one decision; the census
measures it before any rule is written for it.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.held_stubs import Blockers
from pipeline_core.domain.models import FnLocator

__all__ = [
    "Decided",
    "ScheduleViolation",
    "blamed_from_event",
    "decided_from_event",
    "due_from_event",
    "decided_from_schedule",
    "hollow_from_event",
    "measured_owners_from_event",
    "violations",
]


@dataclass(frozen=True, slots=True)
class Decided:
    """One candidate run as a schedule decided it."""

    key: str
    label: str
    targets: frozenset[FnLocator]
    opened: bool
    reason: str = ""
    held_by: tuple[str, ...] = ()
    share_minutes: float | None = None
    #: The run is a session: its targets are its editable set.
    session: bool = False
    #: The bare names of the tests the run aims at.
    aimed_tests: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ScheduleViolation:
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.kind}: {self.detail}"


def violations(decided: Sequence[Decided], *,
               hollow: Mapping[str, Collection[FnLocator]] | None,
               crash_blamed: (Mapping[FnLocator, Collection[str]]
                              | Collection[FnLocator]) = (),
               budget_minutes: float = 0.0,
               measured_owners: Collection[FnLocator] = (),
               due: Collection[FnLocator] = (),
               ) -> tuple[ScheduleViolation, ...]:
    """Every invariant the decision set breaks. ``hollow`` is the held view
    (held test -> the stubs its first failure sits in); None means the caller
    does not know it (a recorded event that predates `inputs`), and the
    blocker check then reads blamed locators only."""
    out: list[ScheduleViolation] = []
    opened = [d for d in decided if d.opened]
    deferred = [d for d in decided if not d.opened]
    opened_keys = {d.key for d in opened}
    labels = {d.key: d.label for d in decided}

    for d in deferred:
        missing = [h for h in d.held_by if h not in opened_keys]
        if missing:
            out.append(ScheduleViolation(
                "holder_not_opened",
                f"{d.label} deferred {d.reason or 'held'} behind "
                + ", ".join(labels.get(h, h) for h in missing)
                + ", which did not open"))

    candidates_write: set[FnLocator] = set()
    for d in decided:
        candidates_write |= d.targets
    blk = Blockers.of(hollow, crash_blamed)

    def _writes(d: Decided, u: FnLocator) -> bool:
        return blk.writes(u, targets=d.targets, aimed_tests=d.aimed_tests,
                          session=d.session)

    # A culprit is reported when some candidate writes it; a held stub is
    # reported even when none does (it stops red tests whatever else runs).
    blockers = {u for u in blk.culprits
                if any(_writes(d, u) for d in decided)} | blk.held.units
    written: set[FnLocator] = set()
    for d in opened:
        written |= d.targets
    for b in sorted(blockers, key=lambda u: (u.file, u.fn_name)):
        if any(_writes(d, b) for d in opened):
            continue
        writers = [d for d in deferred if _writes(d, b)]
        out.append(ScheduleViolation(
            "blocker_unwritten",
            f"{b.file}::{b.fn_name} is written by no opened run"
            + ("; its writers were deferred: "
               + ", ".join(f"{d.label} ({d.reason or 'deferred'})"
                           for d in writers)
               if writers else
               "; no candidate run aims at it")))

    for u in sorted(frozenset(due) - written, key=lambda x: (x.file, x.fn_name)):
        writers = [d for d in deferred if u in d.targets]
        out.append(ScheduleViolation(
            "slice_unit_starved",
            f"{u.file}::{u.fn_name} is owed by a red test and no opened run "
            "writes it"
            + ("; its writers were deferred: " + ", ".join(
                f"{d.label} ({d.reason or 'deferred'})" for d in writers)
               if writers else "; no candidate run writes it")))

    for o in sorted(frozenset(measured_owners) - candidates_write,
                    key=lambda u: (u.file, u.fn_name)):
        out.append(ScheduleViolation(
            "owner_unrouted",
            f"{o.file}::{o.fn_name} is a measured coverage owner still on "
            "the frontier, and no candidate run writes it (the dispatch "
            "batch left it past the cap, or a planner dropped it)"))

    if not opened and any(d.reason != "nothing_aimed" for d in deferred):
        out.append(ScheduleViolation(
            "nothing_opened",
            f"{len(deferred)} candidate(s), none opened: "
            + ", ".join(f"{d.label} ({d.reason})" for d in deferred[:6])))

    for d in deferred:
        if d.share_minutes:
            out.append(ScheduleViolation(
                "deferred_share",
                f"{d.label} deferred with share {d.share_minutes:.2f} min"))
    if budget_minutes > 0:
        total = sum(d.share_minutes or 0.0 for d in opened)
        # A recorded event rounds each share to 0.01 min; 32 recorded fills
        # summed to 200.02-200.06 on a 200-minute budget from that alone.
        if total > budget_minutes + 1e-6 + 0.005 * len(opened):
            out.append(ScheduleViolation(
                "over_budget",
                f"opened shares {total:.2f} min exceed the budget "
                f"{budget_minutes:.2f} min"))
    return tuple(out)


def decided_from_schedule(sched: Any) -> list[Decided]:
    """The decisions of a :class:`~fill_schedule.FillSchedule`."""
    out: list[Decided] = []
    for s, opened in ([(s, True) for s in sched.ordered]
                      + [(s, False) for s in sched.deferred]):
        out.append(Decided(
            key=s.run.key, label=s.run.label, targets=s.run.targets,
            opened=opened, reason=s.reason, held_by=tuple(s.held_by),
            share_minutes=s.share_minutes,
            session=str(getattr(s.run.kind, "value", s.run.kind)) == "session",
            aimed_tests=frozenset(s.run.target_tests)))
    return out


def _loc(label: str) -> FnLocator:
    f, _, fn = str(label).partition("::")
    return FnLocator(f, fn)


def _key_of(raw: Mapping[str, Any]) -> str:
    """`RouteRun.key` rebuilt from the run's `to_dict` — the same fields in
    the same format, so a recorded hold's keys resolve."""
    tests = ",".join(sorted(str(t) for t in raw.get("target_tests") or ()))
    files = ",".join(str(f) for f in raw.get("files") or ())
    return (f"{raw.get('kind', '')}:{int(raw.get('seq', 0) or 0)}:"
            f"{tests}:{files}:{raw.get('label', '')}")


def decided_from_event(data: Mapping[str, Any]) -> list[Decided]:
    """The decisions a recorded `phase_c.fill.schedule` event carries."""
    out: list[Decided] = []
    for raw, opened in ([(r, True) for r in data.get("runs") or ()]
                        + [(r, False) for r in data.get("deferred") or ()]):
        share = raw.get("share_minutes")
        out.append(Decided(
            key=_key_of(raw), label=str(raw.get("label", "")),
            targets=frozenset(_loc(t) for t in raw.get("targets") or ()),
            opened=opened, reason=str(raw.get("reason", "")),
            held_by=tuple(str(h) for h in raw.get("held_by") or ()),
            share_minutes=float(share) if share is not None else None,
            session=str(raw.get("kind", "")) == "session",
            aimed_tests=frozenset(
                str(t) for t in raw.get("target_tests") or ())))
    return out


def hollow_from_event(data: Mapping[str, Any]
                      ) -> dict[str, frozenset[FnLocator]] | None:
    """The hollow map the event's inputs recorded (held test -> stubs), or
    None when the event predates recorded inputs."""
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping) or "hollow" not in inputs:
        return None
    return {str(t): frozenset(_loc(x) for x in locs or ())
            for t, locs in dict(inputs.get("hollow") or {}).items()}


def due_from_event(data: Mapping[str, Any]) -> frozenset[FnLocator]:
    """The owed functions a recorded schedule event's inputs carry; empty
    for an event that predates them."""
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping):
        return frozenset()
    return frozenset(_loc(x) for x in inputs.get("due") or ())


def measured_owners_from_event(data: Mapping[str, Any]) -> frozenset[FnLocator]:
    """The admitted measured owners a recorded event carries (empty for an
    event that predates the field)."""
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping):
        return frozenset()
    return frozenset(_loc(x) for x in inputs.get("measured_owners") or ())


def blamed_from_event(
    data: Mapping[str, Any],
) -> dict[FnLocator, frozenset[str]]:
    """The crash blame a recorded event carries: culprit -> the tests whose
    crash convicted it. An event that predates `crash_blamed_tests` gives
    each culprit no test (no session is its writer, `Blockers.of`)."""
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping):
        return {}
    tests = inputs.get("crash_blamed_tests") or {}
    return {_loc(x): frozenset(tests.get(x) or ())
            for x in inputs.get("crash_blamed") or ()}


