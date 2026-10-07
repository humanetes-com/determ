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
- ``blocker_unwritten`` — some candidate writes a BLOCKER (a stub a failing
  test is hollow on, or a locator the sweep's crash evidence blames) and no
  opened run writes it. A hollow test cannot be judged by any route until its
  stub has a body, so a fill that opens none of the blocker's writers spends
  its budget on tests that cannot move.
- ``nothing_opened`` — candidates exist that aim at something and none opened.
- ``deferred_share`` / ``over_budget`` — a deferred run carries a positive
  share, or the opened shares exceed the budget.
- ``owner_unrouted`` — a measured coverage owner the driver admitted (or
  aimed at a newly admitted test) and dispatched is a target of NO candidate
  run, opened or deferred. Run `486447-5207101c` (2026-10-07, fill 6):
  `src/ensure.rs::render` was admitted for six failing `ensure!` tests and
  every slice carrying it was hollow or parked, so nothing wrote it and no
  deferral said why.

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

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.models import FnLocator

__all__ = [
    "Decided",
    "ScheduleViolation",
    "all_blocking",
    "blamed_from_event",
    "decided_from_event",
    "decided_from_schedule",
    "hollow_stubs_from_event",
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


@dataclass(frozen=True, slots=True)
class ScheduleViolation:
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.kind}: {self.detail}"


def violations(decided: Sequence[Decided], *,
               hollow_stubs: Collection[FnLocator] | None,
               crash_blamed: Collection[FnLocator] = (),
               budget_minutes: float = 0.0,
               measured_owners: Collection[FnLocator] = (),
               ) -> tuple[ScheduleViolation, ...]:
    """Every invariant the decision set breaks. ``hollow_stubs`` None means
    the caller does not know which stubs hold hollow tests (a recorded event
    that predates `inputs`); the blocker check then reads blamed locators
    only."""
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
    blockers = set(frozenset(crash_blamed) & candidates_write)
    if hollow_stubs is not None:
        blockers |= frozenset(hollow_stubs) & candidates_write
    written: set[FnLocator] = set()
    for d in opened:
        written |= d.targets
    for b in sorted(blockers - written, key=lambda u: (u.file, u.fn_name)):
        writers = [d for d in deferred if b in d.targets]
        out.append(ScheduleViolation(
            "blocker_unwritten",
            f"{b.file}::{b.fn_name} is written by no opened run; its "
            "writers were deferred: "
            + ", ".join(f"{d.label} ({d.reason or 'deferred'})"
                        for d in writers)))

    for o in sorted(frozenset(measured_owners) - candidates_write,
                    key=lambda u: (u.file, u.fn_name)):
        out.append(ScheduleViolation(
            "owner_unrouted",
            f"{o.file}::{o.fn_name} is a measured coverage owner still on "
            "the frontier, and no candidate run writes it"))

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
            share_minutes=s.share_minutes))
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
            share_minutes=float(share) if share is not None else None))
    return out


def hollow_stubs_from_event(data: Mapping[str, Any]
                            ) -> frozenset[FnLocator] | None:
    """The stubs the event's recorded hollow map names, or None when the
    event predates recorded inputs."""
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping) or "hollow" not in inputs:
        return None
    stubs: set[FnLocator] = set()
    for locs in dict(inputs.get("hollow") or {}).values():
        stubs.update(_loc(x) for x in locs or ())
    return frozenset(stubs)


def measured_owners_from_event(data: Mapping[str, Any]) -> frozenset[FnLocator]:
    """The admitted measured owners a recorded event carries (empty for an
    event that predates the field)."""
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping):
        return frozenset()
    return frozenset(_loc(x) for x in inputs.get("measured_owners") or ())


def blamed_from_event(data: Mapping[str, Any]) -> frozenset[FnLocator]:
    inputs = data.get("inputs")
    if not isinstance(inputs, Mapping):
        return frozenset()
    return frozenset(_loc(x) for x in inputs.get("crash_blamed") or ())


def all_blocking(hollow: Mapping[str, Iterable[FnLocator]]
                 ) -> frozenset[FnLocator]:
    """The stubs any leftover test is hollow on."""
    out: set[FnLocator] = set()
    for locs in hollow.values():
        out.update(locs)
    return frozenset(out)
