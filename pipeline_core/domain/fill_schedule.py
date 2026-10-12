"""The fill phase as a SCHEDULE of route runs, not a fixed sequence.

WHY. One TDD fill used to walk five routes in a hard-coded order — slice
sessions, formatter pairs, file-first, per-impl, per-fn — each with its own
preconditions and no knowledge of the others' payoff. Every time one route
starved another, the fix shipped as a new precondition on one route (the
session's site join, the packing of over-union leftovers, then a withhold of
every session while any leftover test was hollow). Run `334902-2774708c`
(2026-09-05/06) is the instance that named the class: of 10h13m, 9h53m went
to the stub routes on a stub tree — five crash-only keeps and two batch
ratchet reverts on one file — and the 20-minute session that opened at
05:23 UTC on 18 format tests produced the only verdict movement after pass
74 (+14). The withhold written against that fill would have skipped the
session and left the routes that burned the night untouched: a gate on a
count no route was working to clear.

WHAT THIS MODULE DECIDES, and from which inputs. Given the candidate runs of
every route (what each would write, which tests it aims at, which of its
targets are still hollow), the hollow relation between leftover tests and
stubs, the route ledger (what each identical run bought before), and a fill
budget in minutes, :func:`schedule` returns the order the runs execute in
and the share of the budget each may claim. Three rules, all generic — no
test, unit or crate name is an input:

- blockers first — a run that fills a stub which makes some leftover test
  hollow goes ahead of runs that fill no such stub (a hollow test cannot be
  judged by ANY route until its stub is filled; a runnable test's session can
  go later in the same fill). This is the producer-before-consumer rule the
  sweep applies when it defers blocked consumers, one level up. Within that,
  a run whose own tests are hollow because of a stub another run holds goes
  after that run. A hop pair is the same kind of blocker for every other run
  that shares its leftover tests or members: the pair is the paying
  two-file transaction, and a never-served file / impl / per-fn of units
  those tests merely execute through is not a new try, it is a split of
  that transaction — while the pair's grace lasts (``unpaid_grace``) (run ``675471-0f3105f2``, 2026-09-10: the pair sat at
  served rank 2 while a never-served 26-body per-fn of coverage locators
  opened first and classified every landing unreachable). Those blocked
  runs are deferred this fill (reason ``pair_wait``), not queued after
  the pair with a share (run ``12120-78b991d3``, 2026-09-11: the pair
  opened first and two coverage sessions, a per-fn, a file and an impl
  of the same leftovers still sat in ordered). A leftover-aiming run
  that writes coverage locators whose files are not files of the
  attributed members it holds waits behind the attributed write of
  those leftovers (reason ``coverage_split``).
- never-served before re-buys — a run with no ledger row on its identity (the
  route, the targets, the targets' bodies, the oracle, and for a session the
  tests it aims at) ranks ahead of one whose last identical try moved no
  verdict, which ranks ahead of one whose last identical try was reverted. A
  run whose last identical try PAID keeps the never-served rank. The service
  order applies the same rule to units. Novelty does not outrank a hop pair
  that aims at the same leftovers.
- budget follows payoff — shares are weights over the fill budget; a zero
  weight (a re-buy) is DEFERRED rather than run, unless every candidate is a
  re-buy or the run has been deferred `readmit_after` times already (the
  bounded re-admission the exhausted set already gets at the unit level).
  A run that cashed on its last turn may extend past its share into the
  fill's unspent budget; a run that produced nothing in its share stops
  claiming new units. No unit, turn or session in flight is interrupted.

`budget_minutes = 0` means ORDER ONLY: no shares, no weight-zero deferral.
pair_wait, coverage-split, kind_cap and nothing_aimed (a run that fills no
stub, aims at no failing test and writes no crash-blamed locator) still
defer at budget 0.

Two rules hold over all of the above, whatever the inputs, and are checked
on every decision (`domain/schedule_invariants.py`): a run waits only on runs
that OPEN in the same schedule — a run's own deferrals (nothing aimed, a
re-buy under a budget) are decided before it can hold anyone — and no
deferral leaves a blocker (a stub a leftover test is hollow on, or a
crash-blamed locator) that some candidate writes with no opened writer: the
first deferred writer in order opens for it. :func:`admit_ranked` is where
both are decided, once.

Deliberately not here: any route's own preconditions (what a session may
edit, which blocks the impl route covers), the keep contract applied to a
run's landed change set, or the process-level fill-phase stop — those are
the routes', the gate's and the graceful-stop clock's respectively.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pipeline_core.domain import unit_clock
from pipeline_core.domain.fill_class import FillOutcomeClass
from pipeline_core.domain.held_stubs import Blockers
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.models import FnLocator, FnRecord

__all__ = [
    "DEFAULT_UNPAID_GRACE",
    "TODAY_ORDER",
    "UnitEvidence",
    "FillBudget",
    "RankedRun",
    "FillSchedule",
    "RouteKind",
    "RouteLedgerRow",
    "RouteRun",
    "RunClock",
    "RunOutcome",
    "ScheduledRun",
    "admit_ranked",
    "bodies_sha_of",
    "call_price",
    "carve_rebuys",
    "condensed_order",
    "unit_evidence_from",
    "consecutive_unpaid",
    "rebuy_identity",
    "run_blocks",
    "schedule",
    "unpaid_pair_fills",
    "unpaid_pair_buys",
    "withhold_landed",
]


class RouteKind(StrEnum):
    SESSION = "session"
    PAIR = "pair"
    FILE = "file"
    IMPL = "impl"
    PER_FN = "per-fn"


#: The sweep's crash blame as the schedule takes it: culprit -> the tests
#: whose crash convicted it (`crash_site.blamed_tests`). A bare collection
#: of culprits is accepted and knows no crashed test (`Blockers.of`).
CrashBlame = Mapping[FnLocator, Collection[str]] | Collection[FnLocator]


#: The order the fill walked before the schedule existed. It is the tie-break
#: of last resort, so a schedule with no evidence reproduces the old walk.
TODAY_ORDER: tuple[RouteKind, ...] = (
    RouteKind.SESSION, RouteKind.PAIR, RouteKind.FILE, RouteKind.IMPL,
    RouteKind.PER_FN)


class RunOutcome(StrEnum):
    """What a route run terminated as, for the ledger and the next schedule."""

    CASHED = "cashed"        # verdicts moved (net_pass_delta > 0)
    FLAT = "flat"            # bodies landed or kept, no verdict moved
    REVERTED = "reverted"    # the landed set was restored
    DECLINED = "declined"    # the model answered nothing usable
    DEFERRED = "deferred"    # the schedule did not open it this fill
    STOPPED = "stopped"      # a graceful stop ended it mid-way
    EMPTY = "empty"          # nothing to do when it opened
    #: The run's only landings (if any) came from reasoning-capped replies:
    #: a truncated call is not a measured refusal (the batch routes' rule
    #: for an escalated rewrite), so the row is not a re-buy verdict and
    #: the next schedule ranks the run as never served. Run
    #: `3014175-d98ed09e` (2026-09-08) session #3: turn 0 cut at the cap,
    #: an empty follow-up, `FLAT after 14.3 min (1 call(s))`.
    UNMEASURED = "unmeasured"


@dataclass(frozen=True, slots=True)
class RouteRun:
    """One thing a route would do this fill.

    ``targets`` are the units the run may WRITE; ``target_tests`` the bare
    names of the tests it aims at (a session's targets; the attributed tests
    of a unit route's targets); ``stubs_in_targets`` the targets that are
    hollow bodies on the tree now. ``bodies_sha`` is the identity of the
    targets' bodies at plan time (:func:`bodies_sha_of`) — with the kind, the
    targets, the oracle, and for a session the aimed tests, it is what makes
    two runs "the same try". ``label`` is a display string, not an id:
    packed sessions can share a count-only label while aiming at disjoint
    tests (run ``1092886-60870fdd``, 2026-09-06).
    """

    kind: RouteKind
    label: str
    targets: frozenset[FnLocator]
    target_tests: frozenset[str] = frozenset()
    stubs_in_targets: frozenset[FnLocator] = frozenset()
    files: tuple[str, ...] = ()
    seq: int = 0
    bodies_sha: str = ""
    #: Non-empty when the route writes each target on its own and will run
    #: ANY subset of ``targets`` it is handed: every target with the bare
    #: names of the tests it aims at. The schedule then ranks the run's
    #: units, not the batch (:func:`carve_rebuys`). Empty means the targets
    #: are one transaction (a file, an impl block, a pair, a session).
    unit_tests: tuple[tuple[FnLocator, frozenset[str]], ...] = ()
    #: The ``key`` of the planned run this one was carved from; the route
    #: that planned that run executes this one. Empty for a planned run.
    parent: str = ""

    @property
    def plan_key(self) -> str:
        """The key of the run as its route planned it."""
        return self.parent or self.key

    @property
    def key(self) -> str:
        """Unique per planned run. The display ``label`` is not sufficient:
        two sessions of the same kind can share a count-only label."""
        tests = ",".join(sorted(self.target_tests))
        files = ",".join(self.files)
        return f"{self.kind.value}:{self.seq}:{tests}:{files}:{self.label}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "label": self.label,
            "targets": sorted(f"{f}::{fn}" for f, fn in self.targets),
            "target_tests": sorted(self.target_tests),
            "stubs": sorted(f"{f}::{fn}" for f, fn in self.stubs_in_targets),
            "files": list(self.files), "seq": self.seq,
            "bodies_sha": self.bodies_sha,
            **({"parent": self.parent} if self.parent else {}),
        }


def bodies_sha_of(bodies: Iterable[tuple[FnLocator, str]]) -> str:
    """Identity of a set of bodies: whitespace-folded, keyed by locator.

    The same fold the body ledger applies to one unit's occurrences, so a
    run's identity moves exactly when a unit's would."""
    h = hashlib.sha256()
    for loc, text in sorted(bodies, key=lambda p: (p[0].file, p[0].fn_name)):
        h.update(f"{loc.file}::{loc.fn_name}".encode())
        h.update(b"\0")
        h.update(" ".join((text or "").split()).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def bodies_sha_on_tree(targets: Collection[FnLocator],
                       records: Iterable[FnRecord]) -> str:
    """:func:`bodies_sha_of` over the bodies the extractor read for
    ``targets`` — the identity a run's re-buy detection compares.

    ``records`` are the extractor's records of the tree (``file`` relative to
    the directory it was given, so ``src/x.rs`` when handed the cluster root)
    and ``targets`` the loop's locators; the two are joined after the same
    ``src/`` normalization on both sides. An empty join is not a small
    identity: every run of the same targets would hash alike whatever their
    bodies, and a moved body would read as the same try."""
    if not targets:
        return ""
    wanted = {FnLocator(norm_src_rel(t.file), t.fn_name) for t in targets}
    bodies: dict[FnLocator, list[str]] = {}
    for r in records:
        loc = FnLocator(norm_src_rel(r.file), r.fn_name)
        if loc in wanted:
            bodies.setdefault(loc, []).append(r.body_text or "")
    return bodies_sha_of(
        (loc, "\n".join(texts)) for loc, texts in bodies.items())


def rebuy_identity(kind: RouteKind, targets: Collection[FnLocator],
                   bodies_sha: str, oracle_fp: str, *,
                   target_tests: Collection[str] = ()) -> str:
    """The key under which a run is the SAME try as an earlier one.

    A session's work is the slice, so the aimed tests are part of the
    identity; two sessions that write overlapping hubs for different tests
    are different tries. A file / impl / per-fn run's ``target_tests`` are
    the leftover attributed set and shrink as tests cash — putting those
    names in the identity would make a flat file look never-served every
    fill, so only ``SESSION`` includes them.
    """
    names = ",".join(sorted(f"{f}::{fn}" for f, fn in targets))
    ident = f"{kind.value}|{names}|{bodies_sha}|{oracle_fp}"
    if kind is RouteKind.SESSION:
        ident = f"{ident}|{','.join(sorted(target_tests))}"
    return ident


def _blocking_stubs(run: RouteRun,
                    hollow: Mapping[str, frozenset[FnLocator]]) -> frozenset[FnLocator]:
    """The stubs this run holds that make SOME leftover test hollow."""
    if not run.stubs_in_targets:
        return frozenset()
    all_blocking: set[FnLocator] = set()
    for locs in hollow.values():
        all_blocking |= locs
    return run.stubs_in_targets & frozenset(all_blocking)


def run_blocks(a: RouteRun, b: RouteRun,
               hollow: Mapping[str, frozenset[FnLocator]],
               crash_blamed: Collection[FnLocator] = ()) -> bool:
    """True when `a` must land before `b` can be judged.

    This is the RELATION: it says which runs a pair's transaction covers
    — a two-file transaction written after one of its halves has been
    peeled apart is the split every instance below records. Whether the
    relation still orders and defers is the pair's grace; see
    ``unpaid_grace`` in :func:`schedule`. Past the grace over `b` the pair
    neither defers `b` nor sorts ahead of it: the ledger ranks the two.

    A hop pair is the two-file transaction for its members. Any other run
    that can write either member, or that aims at the same leftover tests,
    waits — unless that other run fills a stub which makes a leftover test
    hollow, which is still blockers-first, or holds a crash-blamed
    locator the pair does not already write (a table fn in a third file
    the two-file pair cannot splice). A blamed locator the pair holds is
    not an exemption: that run is a split of the joint write (run
    ``10959-9c95ea89``, 2026-09-14: file of the recovered slots opened
    first, kept FLAT net +0, pair still crash-reverted). Run
    ``19980-f9ea6adb`` (2026-09-10) opened the overlapping session first,
    wrote both halves plus a hanging companion, peeled the set apart, and
    ``PairRoute`` would have skipped once a session had served either
    member. The same split, one route down: run ``675471-0f3105f2`` opened
    a never-served per-fn of coverage locators (and a never-served file of
    the dispatcher) before the served pair, and every per-fn landing came
    back unreachable. The pre-schedule walk still puts SESSION before
    PAIR; this rule is the schedule's. Restricting the wait to SESSION
    left file / impl / per-fn free to spend the fill on units the hop
    already said cannot pay.

    Otherwise: `b`'s own tests are hollow because of a stub `a` holds and
    `b` does not."""
    if a is b:
        return False
    blamed = frozenset(crash_blamed)
    if (a.kind is RouteKind.PAIR and b.kind is not RouteKind.PAIR
            and (a.targets & b.targets or a.target_tests & b.target_tests)
            and not _blocking_stubs(b, hollow)
            and not ((b.targets & blamed) - a.targets)):
        return True
    if not a.stubs_in_targets or not b.target_tests:
        return False
    needed: set[FnLocator] = set()
    for t in b.target_tests:
        needed |= hollow.get(t, frozenset())
    needed -= b.stubs_in_targets
    return bool(a.stubs_in_targets & frozenset(needed))


@dataclass(frozen=True, slots=True)
class RouteLedgerRow:
    """One route run as the ledger records it — the payoff evidence the next
    schedule ranks on. Persisted as JSON lines under `logs/route_ledger/`."""

    kind: RouteKind
    label: str
    targets: tuple[tuple[str, str], ...]
    bodies_sha: str
    oracle_fp: str
    outcome: RunOutcome
    run_id: str = ""
    iteration: int = 0
    cluster: str = ""
    started: str = ""
    minutes: float = 0.0
    model_calls: int = 0
    net_pass_delta: int = 0
    newly_red: tuple[str, ...] = ()
    newly_crashed: tuple[str, ...] = ()
    kept: tuple[tuple[str, str], ...] = ()
    reverted: tuple[tuple[str, str], ...] = ()
    target_tests: tuple[str, ...] = ()
    classes: Mapping[str, int] = field(default_factory=dict)
    share_minutes: float | None = None
    reason: str = ""
    #: Bare names of the tests the run's keeps turned green. None is a row
    #: that recorded no per-test reading — every row written before
    #: 2026-09-15, and rows that opened no judged try — which is NOT the
    #: same claim as ``()`` ("kept, and paid none of its tests").
    newly_green: tuple[str, ...] | None = None
    #: The reverted units the revert did not narrow to: restored only
    #: because the transaction they rode in was restored as a whole
    #: (`FillAttempts.note_reverted(collateral=True)`). Empty on every row
    #: written before 2026-09-23, which reads as "every revert implicated
    #: its unit", the reading those rows were ranked on.
    collateral: tuple[tuple[str, str], ...] = ()
    #: Write sets a reply of this run named as one (the target and the
    #: extras it wrote with it, as sorted pairs; `domain.joint_write`).
    #: Empty on every row written before 2026-09-25 and on every run whose
    #: replies named none.
    joint_sets: tuple[tuple[tuple[str, str], ...], ...] = ()
    #: The units the run actually OPENED (its per-run attempt delta:
    #: dispatched, kept or reverted). ``targets`` is fixed when the schedule
    #: plans the run; the route's own planners (the refill collector, the
    #: stale probe, the landed-this-fill withholding) drop units after that,
    #: so a target the run never opened is a unit filtered inside the run.
    #: None on every row written before 2026-09-27: not recorded, which is
    #: not the same claim as "opened nothing".
    opened: tuple[tuple[str, str], ...] | None = None
    #: Minutes from the session's opening to the end of its first turn (the
    #: baseline suite, turn 0's prompt and deliberation, its check and
    #: judgement). The first turn is where a session's cost is, so the floor
    #: prices it apart from the turns after it (:class:`SessionTurnPrice`).
    #: None on every non-session row, on a session that made no call, and on
    #: every row written before 2026-10-05.
    first_turn_minutes: float | None = None
    #: Targets the run's own planners dropped after the schedule fixed them,
    #: each with the predicate that dropped it: ``(file, fn, reason)``. A
    #: unit in ``targets`` and not in ``opened`` with no entry here was
    #: dropped by a planner that does not say why. Empty on every row written
    #: before 2026-10-07.
    filtered: tuple[tuple[str, str, str], ...] = ()
    #: `file::fn` -> body text captured before this run's revert restored
    #: the tree. Empty on rows that reverted nothing worth replaying, and
    #: on every row written before the joint ask kept bodies.
    candidate_bodies: Mapping[str, str] = field(default_factory=dict)
    #: The union gate repaired this run's gained revert. Absent on older rows.
    joint_repaired: bool = False

    @property
    def identity(self) -> str:
        return rebuy_identity(
            self.kind, [FnLocator(f, fn) for f, fn in self.targets],
            self.bodies_sha, self.oracle_fp,
            target_tests=self.target_tests)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "label": self.label,
            "targets": [list(t) for t in self.targets],
            "bodies_sha": self.bodies_sha, "oracle_fp": self.oracle_fp,
            "outcome": self.outcome.value, "run_id": self.run_id,
            "iteration": self.iteration, "cluster": self.cluster,
            "started": self.started, "minutes": round(self.minutes, 3),
            "model_calls": self.model_calls,
            "net_pass_delta": self.net_pass_delta,
            "newly_red": list(self.newly_red),
            "newly_crashed": list(self.newly_crashed),
            "kept": [list(t) for t in self.kept],
            "reverted": [list(t) for t in self.reverted],
            "target_tests": list(self.target_tests),
            "classes": dict(self.classes),
            "share_minutes": self.share_minutes, "reason": self.reason,
            "newly_green": (list(self.newly_green)
                            if self.newly_green is not None else None),
            "collateral": [list(t) for t in self.collateral],
            "joint_sets": [[list(t) for t in js] for js in self.joint_sets],
            "opened": ([list(t) for t in self.opened]
                       if self.opened is not None else None),
            "first_turn_minutes": (round(self.first_turn_minutes, 3)
                                   if self.first_turn_minutes is not None
                                   else None),
            **({"filtered": [list(t) for t in self.filtered]}
               if self.filtered else {}),
            **({"candidate_bodies": dict(sorted(self.candidate_bodies.items()))}
               if self.candidate_bodies else {}),
            **({"joint_repaired": True} if self.joint_repaired else {}),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RouteLedgerRow | None:
        try:
            kind = RouteKind(str(raw["kind"]))
            outcome = RunOutcome(str(raw["outcome"]))
        except (KeyError, ValueError):
            return None

        def _pairs(key: str) -> tuple[tuple[str, str], ...]:
            return tuple((str(a), str(b)) for a, b in raw.get(key) or ())

        share = raw.get("share_minutes")
        green = raw.get("newly_green")
        first = raw.get("first_turn_minutes")
        return cls(
            kind=kind, label=str(raw.get("label", "")),
            targets=_pairs("targets"), bodies_sha=str(raw.get("bodies_sha", "")),
            oracle_fp=str(raw.get("oracle_fp", "")), outcome=outcome,
            run_id=str(raw.get("run_id", "")),
            iteration=int(raw.get("iteration", 0) or 0),
            cluster=str(raw.get("cluster", "")),
            started=str(raw.get("started", "")),
            minutes=float(raw.get("minutes", 0.0) or 0.0),
            model_calls=int(raw.get("model_calls", 0) or 0),
            net_pass_delta=int(raw.get("net_pass_delta", 0) or 0),
            newly_red=tuple(str(x) for x in raw.get("newly_red") or ()),
            newly_crashed=tuple(str(x) for x in raw.get("newly_crashed") or ()),
            kept=_pairs("kept"), reverted=_pairs("reverted"),
            target_tests=tuple(str(x) for x in raw.get("target_tests") or ()),
            classes={str(k): int(v) for k, v in dict(raw.get("classes") or {}).items()},
            share_minutes=float(share) if share is not None else None,
            reason=str(raw.get("reason", "")),
            newly_green=(tuple(str(x) for x in green)
                         if isinstance(green, list) else None),
            collateral=_pairs("collateral"),
            joint_sets=tuple(
                tuple((str(a), str(b)) for a, b in js)
                for js in raw.get("joint_sets") or () if js),
            opened=(_pairs("opened") if isinstance(raw.get("opened"), list)
                    else None),
            first_turn_minutes=float(first) if first is not None else None,
            filtered=tuple(
                (str(t[0]), str(t[1]), str(t[2]))
                for t in raw.get("filtered") or ()
                if isinstance(t, (list, tuple)) and len(t) == 3),
            candidate_bodies={
                str(k): str(v) for k, v in dict(
                    raw.get("candidate_bodies") or {}).items() if v},
            joint_repaired=bool(raw.get("joint_repaired", False)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


# Rank classes, in the order they dispatch within one blocking level.
#: Graded tries a route's schedule exemptions survive without paying.
#: Sized from the live stable ledger of 2026-09-14: the hop pair's two
#: work keys held 36 and 24 consecutive non-cashing graded rows, so any
#: small bound releases the competing routes, and three leaves room for
#: the retry-with-a-fresh-diagnosis the exemptions were written for (a
#: compile-fail retry plus one re-ask) before the premise expires.
DEFAULT_UNPAID_GRACE = 3

_RANK_NEVER_SERVED = 0
_RANK_PAYING = 1
_RANK_SERVED = 2
_RANK_REBUY_FLAT = 3
_RANK_REBUY_REVERTED = 4

_WEIGHT = {
    _RANK_NEVER_SERVED: 2, _RANK_PAYING: 2, _RANK_SERVED: 1,
    _RANK_REBUY_FLAT: 0, _RANK_REBUY_REVERTED: 0,
}

_REASON = {
    _RANK_NEVER_SERVED: "never_served", _RANK_PAYING: "paying",
    _RANK_SERVED: "served", _RANK_REBUY_FLAT: "rebuy_flat",
    _RANK_REBUY_REVERTED: "rebuy_reverted",
}


@dataclass(frozen=True, slots=True)
class ScheduledRun:
    run: RouteRun
    rank: int
    weight: int
    reason: str
    level: int
    share_minutes: float | None = None
    deferred: bool = False
    blocked_by: tuple[str, ...] = ()
    #: Deferrals this run would have taken and did not, because the run it
    #: waited on spent its grace on the tests they share (``pair_wait``,
    #: ``coverage_split``). Printed on the schedule line so a lapse is a
    #: log fact, not an absence.
    lapsed: tuple[str, ...] = ()
    #: Pairs that block this run and did not hold it because this run must
    #: land before the PAIR can be judged (a stub the pair's tests are
    #: hollow on). Printed apart from ``lapsed``: nothing was spent, the
    #: wait would have made the pair's own verdict unreachable.
    mutual: tuple[str, ...] = ()
    #: Minutes this run's share may not fall below (a session's minimum
    #: turns priced at its measured turn cost, :func:`session_floor`); None =
    #: no floor. `floor_note` says how it was priced, or that it was clipped.
    floor_minutes: float | None = None
    floor_note: str = ""
    #: The runs (keys) a `pair_wait` / `coverage_split` hold waited on, and
    #: the reason the run was ranked with before the hold. A hold is "wait
    #: for X"; the schedule holds a run only behind runs its pass OPENS, but
    #: the executor can still defer X (premise moved, landed this fill,
    #: budget spent), and a run waiting on a run that never opens waits for
    #: nothing. The executor releases it (`execute_fill`) from these.
    held_by: tuple[str, ...] = ()
    prior_reason: str = ""
    #: This run's index in the schedule's topological order, deferred runs
    #: included, so a released run re-enters where the order put it.
    position: int = 0
    #: The blockers (``file::fn``) this run was opened for after a deferral
    #: of its own had left them with no opened writer (stage 3 of
    #: :func:`admit_ranked`). Empty for a run the pass opened.
    blocker_writer: tuple[str, ...] = ()
    #: The owed functions (``file::fn``, :mod:`candidate_tiers`) this run was
    #: opened for after a deferral had left them with no opened writer — a
    #: red test's tier asked under evidence no prompt has carried yet. Empty
    #: for a run the pass opened.
    starved_writer: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.run.to_dict(), "rank": self.rank, "weight": self.weight,
            "floor_minutes": (round(self.floor_minutes, 2)
                              if self.floor_minutes is not None else None),
            "floor_note": self.floor_note,
            "reason": self.reason, "level": self.level,
            "share_minutes": (round(self.share_minutes, 2)
                              if self.share_minutes is not None else None),
            "deferred": self.deferred, "blocked_by": list(self.blocked_by),
            "lapsed": list(self.lapsed), "mutual": list(self.mutual),
            "held_by": list(self.held_by),
            "prior_reason": self.prior_reason, "position": self.position,
            "blocker_writer": list(self.blocker_writer),
            "starved_writer": list(self.starved_writer),
        }


@dataclass(frozen=True, slots=True)
class FillSchedule:
    ordered: tuple[ScheduledRun, ...]
    deferred: tuple[ScheduledRun, ...]
    budget_minutes: float

    @property
    def runs(self) -> tuple[RouteRun, ...]:
        return tuple(s.run for s in self.ordered)

    def render(self) -> str:
        parts: list[str] = []
        for i, s in enumerate(self.ordered, 1):
            stub_n = len(s.run.stubs_in_targets)
            bits = [s.reason]
            if stub_n:
                bits.append(f"{stub_n} stub(s)")
            if s.blocker_writer:
                bits.append("opened as the only writer of "
                            + ", ".join(s.blocker_writer))
            if s.starved_writer:
                bits.append("starved: opened for owed "
                            + ", ".join(s.starved_writer))
            if s.lapsed:
                bits.append("grace spent: " + ", ".join(s.lapsed))
            if s.mutual:
                bits.append("not held, the pair waits on it: "
                            + ", ".join(s.mutual))
            if s.blocked_by:
                bits.append("after " + ", ".join(s.blocked_by))
            if s.floor_note:
                bits.append(s.floor_note)
            share = (f" share {s.share_minutes:.0f}m"
                     if s.share_minutes is not None else "")
            parts.append(f"{i}. {s.run.label} ({'; '.join(bits)}){share}")
        text = "; ".join(parts) if parts else "no run"
        if self.deferred:
            text += " — deferred: " + ", ".join(
                f"{s.run.label} ({s.reason})" for s in self.deferred)
        return text


def _latest_by_identity(rows: Iterable[RouteLedgerRow]) -> dict[str, list[RouteLedgerRow]]:
    """Rows per identity, in ledger order (append-only, so file order is time)."""
    out: dict[str, list[RouteLedgerRow]] = {}
    for r in rows:
        out.setdefault(r.identity, []).append(r)
    return out


def _row_paid(r: RouteLedgerRow) -> bool:
    return r.outcome is RunOutcome.CASHED or r.net_pass_delta > 0


def row_paid_test(r: RouteLedgerRow, test: str, *,
                  aimed_later: bool = False) -> bool:
    """Did this row pay `test` — turn it green — rather than merely pay?

    A row that recorded its green set answers from it. A row that did not
    (written before the set was recorded) has two answers, in order:

    - ``aimed_later``: the SAME process wrote a row aimed at `test` at a
      later iteration. Every row's aimed tests come from the sweep that
      opened its fill, so `test` was failing at a sweep after this row's
      keeps — the row did not leave it green. (A keep that reddens a passing
      test is reverted by the regression gate, so "green at the keep, red at
      the next sweep" is not a path the fill has.)
    - otherwise the row-level fact, which every walk read before: a refusal
      to spend that an unrecorded row could release would be releasing on no
      evidence, the unrecorded-premise convention :func:`unpaid_pair_buys`
      applies to an unwired oracle.

    Run `11358-e5de094e` is the instance: fill 3's pair row cashed +2 aimed
    at three tests, green on two, and the third test — still aimed at by
    every row of iterations 4 and 5 — had its streak restarted on it.
    """
    if r.newly_green is None:
        return False if aimed_later else _row_paid(r)
    leaf = test.rsplit("::", 1)[-1]
    return any(n.rsplit("::", 1)[-1] == leaf for n in r.newly_green)


def _unpaid_streak(rows: Iterable[RouteLedgerRow],
                   match: Callable[[RouteLedgerRow], bool],
                   oracle_fp: str,
                   paid: Callable[[RouteLedgerRow], bool] = _row_paid) -> int:
    """Rows matching `match` that SPENT a fill, newest first, until one paid.

    The ONE walk behind every "has this spend stopped paying" question, so
    a second consumer cannot drift from the first about which rows are a
    try. Every caller is an exemption clock: the lapse of a pair's right to
    zero its competitors (:func:`consecutive_unpaid`), and the two per-test
    lapses (:func:`unpaid_tries_for_test`, :func:`unpaid_attributed_writes`).
    Rows under another oracle are not evidence about this one and are
    skipped, as the re-buy detector already skips them.

    WHICH ROWS COUNT, and why it is not the same set the RANK reads. A
    deferred row and an EMPTY row opened nothing and consumed nothing; a
    STOPPED row was cut by a graceful stop, which is the operator's spend,
    not the route's. Those three neither count nor interrupt the run. An
    UNMEASURED row does count, and that is the one difference from
    :func:`_rank_of`, deliberately: a run whose every reply was cut
    mid-deliberation is not EVIDENCE that the route cannot pay — which is
    the rank's question, and why the rank reads it as never served — but it
    is a fill the route spent while deferring every competing run at share
    zero, which is this clock's question. Skipping it here would make the
    exemption unreachable exactly when the route is failing in the most
    expensive way: run `366282-7c585f74` iter 2 (2026-09-15) spent 49
    minutes on three pair runs, two of them a single truncated call, and it
    was those three graded rows that let the next fill open the file runs
    that then cashed +14. Under a clock that skipped the truncated two, the
    streak would have read 1 of 3 and that fill would have deferred them
    again.

    `paid` is what "paid" means for the question asked: the row paid (a
    route's exemption), or the row paid one test (a per-test exemption,
    :func:`row_paid_test`).
    """
    seen = 0
    for r in reversed(list(rows)):
        if r.oracle_fp != oracle_fp or not match(r):
            continue
        if r.outcome in (RunOutcome.DEFERRED, RunOutcome.EMPTY,
                         RunOutcome.STOPPED):
            continue
        if paid(r):
            break
        seen += 1
    return seen


def unpaid_tries_for_test(
    rows: Iterable[RouteLedgerRow], test: str, oracle_fp: str,
    match: Callable[[RouteLedgerRow], bool] = lambda _r: True,
) -> int:
    """Graded rows aimed at `test` (and matching `match`) since the last
    one that turned `test` green, newest first.

    A row that paid a sibling of `test` is a try of `test` that did not
    pay it: the exemption a route earns over one leftover is spent by
    that leftover's own record, not by what the route did for another.
    """
    rows_list = list(rows)
    # (run, iteration) of every row aimed at `test`, any outcome — a
    # deferred row's aimed tests are that sweep's leftovers too.
    aimed: dict[str, int] = {}
    for r in rows_list:
        if r.run_id and test in r.target_tests:
            aimed[r.run_id] = max(aimed.get(r.run_id, r.iteration), r.iteration)
    return _unpaid_streak(
        rows_list, lambda r: test in r.target_tests and match(r), oracle_fp,
        paid=lambda r: row_paid_test(
            r, test, aimed_later=bool(r.run_id)
            and aimed.get(r.run_id, r.iteration) > r.iteration))


def unpaid_pair_buys(rows: Iterable[RouteLedgerRow], test: str,
                     oracle_fp: str) -> int:
    """How many graded PAIR runs aimed at `test` have returned nothing,
    newest first, stopping at the first that cashed.

    The attribution side's reading of the same evidence the schedule ranks
    on, and the reason it is keyed on the row's `target_tests` rather than
    on :func:`_served_key`: a pair's write set is not its hop. The hop that
    covers a leftover is two cross-file members, while the run the ledger
    records also carries whatever same-file table slots the crash evidence
    joined to it, so a key built from the hop's members matches no row the
    pair ever wrote (live ledger, 2026-09-15: hop `src/error.rs::fmt +
    src/fmt.rs::debug`, rows targeting all four of `context_backtrace`,
    `fmt`, `no_backtrace`, `debug`). The test the run aimed at is the same
    in both, and it is the granularity the question is asked at.

    An empty `oracle_fp` is 0, never a match against rows that also carry
    none. Unlike :func:`consecutive_unpaid`, whose answer only lowers a
    route's rank, this one RELEASES a refusal to spend, and `""` is the
    driver's word for "no oracle is wired" — the same unrecorded-premise
    convention the flat ledger gate uses, pointing the same way: a caller
    that could not read the tree's oracle has observed nothing about
    whether the pair's record describes this tree.
    """
    if not oracle_fp:
        return 0
    return unpaid_tries_for_test(
        rows, test, oracle_fp, match=lambda r: r.kind is RouteKind.PAIR)


def unpaid_pair_fills(rows: Iterable[RouteLedgerRow], test: str,
                      oracle_fp: str) -> int:
    """How many fills in a row, newest first, had a PAIR aimed at `test`
    and did not turn `test` green — whether the pair opened in that fill
    or not.

    ONE clock for the measured-owners withholding. It used to be two —
    graded buys that returned nothing (:func:`unpaid_pair_buys`) and fills
    in which the pair did not open — each compared with the grace on its
    own, and an OPEN reset the second while the first had not reached the
    bound. Run `486447-5207101c` (2026-10-07, cluster `stable`): the
    withholding for `test_if` lapsed at fills 4 and 5 on the unopened
    clock, fill 5 then opened the pair (it cashed on four other tests),
    and fill 6 read `unopened 0`, `unpaid 2/3` and withheld again: six
    fills of the pair as `test_if`'s candidate without paying it, and the
    owner the coverage reading named stayed out. A fill the pair spent
    and a fill it was the candidate and did not open are the same fact
    for the test: a fill went by and the pair did not pay it.

    A fill is one (run id, iteration). It pays `test` when any of its pair
    rows does (:func:`row_paid_test`, with the same-process inference
    :func:`unpaid_tries_for_test` uses for an unrecorded row). A STOPPED
    row is the operator's and neither counts nor interrupts. Other oracles
    are not evidence about this one. An empty `oracle_fp` is 0, as in
    :func:`unpaid_pair_buys`.

    Newest first is read from the rows, not from where they sit in the
    ledger: a process orders by its earliest `started` (its first row
    where none is stamped), a fill within a process by its iteration. A
    walk in insertion order stopped at an older paid fill when the rows
    arrived newest first, and under-counted the unpaid fills after it.
    """
    if not oracle_fp:
        return 0
    rows_list = list(rows)
    aimed: dict[str, int] = {}
    for r in rows_list:
        if r.run_id and test in r.target_tests:
            aimed[r.run_id] = max(aimed.get(r.run_id, r.iteration), r.iteration)
    run_order: dict[str, tuple[str, int]] = {}
    for n, r in enumerate(rows_list):
        first = run_order.get(r.run_id)
        started = r.started or ""
        if first is None:
            run_order[r.run_id] = (started, n)
        elif started and (not first[0] or started < first[0]):
            run_order[r.run_id] = (started, first[1])
    fills: dict[tuple[str, int], bool] = {}
    for r in rows_list:
        if (r.kind is not RouteKind.PAIR or r.oracle_fp != oracle_fp
                or test not in r.target_tests
                or r.outcome is RunOutcome.STOPPED):
            continue
        key = (r.run_id, r.iteration)
        paid = (r.outcome not in (RunOutcome.DEFERRED, RunOutcome.EMPTY)
                and row_paid_test(
                    r, test, aimed_later=bool(r.run_id)
                    and aimed.get(r.run_id, r.iteration) > r.iteration))
        fills[key] = fills.get(key, False) or paid
    newest_first = sorted(
        fills, key=lambda k: (run_order[k[0]], k[1]), reverse=True)
    seen = 0
    for paid in (fills[k] for k in newest_first):
        if paid:
            break
        seen += 1
    return seen


def consecutive_unpaid(rows: Iterable[RouteLedgerRow], run: RouteRun,
                       oracle_fp: str) -> int:
    """How many graded rows this route+targets has run, newest first,
    without paying — stopping at the first row that cashed.

    Keyed on the WORK key (kind + targets), not on
    :func:`rebuy_identity`, and that is the whole point: the identity
    carries ``bodies_sha``, so a run that keeps any body at all comes back
    under a fresh identity and its history restarts. A hop pair that
    writes table slots no test executes keeps those slots on every
    crash-revert, which moved its bodies sha, which is why the live
    schedule called a route with 60 graded rows and 0 cashes
    ``never_served`` on 45 of its 55 buys (2026-09-14, cluster `stable`).

    Rows under another oracle are not evidence about this one and are
    skipped, as the re-buy detector already skips them. Deferred, empty and
    stopped rows consumed no fill and neither count nor interrupt the run;
    a reasoning-capped (UNMEASURED) row consumed one and counts — see
    :func:`_unpaid_streak` for why this clock and the rank differ there.
    """
    key = _served_key(run)
    return _unpaid_streak(rows, lambda r: _served_key_of(r) == key, oracle_fp)


#: Body-ledger grades that say the landing moved the tests it was bought on.
_PAYING_GRADES = frozenset({"advanced", "proven"})


@dataclass(frozen=True, slots=True)
class UnitEvidence:
    """What the body ledger says about each unit's landings — the per-unit
    verdict the schedule ranks units on, so it is never re-derived from a
    route row's run-level outcome.

    WHY. A route row's outcome is about the RUN: FLAT means "no verdict
    moved while this run's keeps were on the tree". Applied to every target
    of the row it convicted units the run never wrote and units whose tests
    could not run. Run `3131-f6b37c1a` (2026-09-23): of the five per-fn
    convictions that deferred the batch holding `src/error.rs::msg` (45 of
    150 failing tests first panic in it), one was a graded body that ran its
    tests and stayed red; `src/fmt.rs::display` was convicted by a row
    (`497151-a6fa205b`) that never kept it and whose only class was
    `unreachable`. Under oracle `1c3b06644b2e` 43 of 86 FLAT route rows
    carried only `unreachable`. The body ledger already grades each landed
    body at the next sweep and already says which grades are evidence
    (`BodyState.UNMEASURED` is "NOT evidence about anything").

    ``grades`` maps (run id, iteration the route row was written at, unit)
    to (body sha landed, grade): the grade is the sweep's non-``landed``
    row with the landing's run id AND body sha, whatever iteration it
    carries — the sweep stamps its own iteration, one later for 107 of 114
    graded rows under that oracle and EARLIER for 5 (a later process judged
    them). None when no sweep graded the landing.

    ``standing_flat`` maps each unit whose ON-TREE body carries a FLAT
    claim whose premise still holds (``body_ledger.flat_standing_unchanged``:
    same body, oracle, convicting set and evidence) to that body's sha. A
    flat grade convicts a unit only through this map: the claim is about a
    moment, and a body a redo or a revert removed from the tree, or a unit
    that would now be told something new, is not the question it answered.
    """

    grades: Mapping[tuple[str, int, FnLocator], tuple[str, str | None]] = field(
        default_factory=dict)
    standing_flat: Mapping[FnLocator, str] = field(default_factory=dict)

    def grade(self, run_id: str, iteration: int,
              unit: FnLocator) -> tuple[str, str | None] | None:
        return self.grades.get((run_id, iteration, unit))

    def stands_flat(self, unit: FnLocator, body_sha: str) -> bool:
        return self.standing_flat.get(unit) == body_sha


def unit_evidence_from(
    entries: Iterable[Any],
    standing_flat: Mapping[FnLocator, str],
    *, oracle_fp: str,
) -> UnitEvidence:
    """Build :class:`UnitEvidence` from body-ledger rows (``LedgerEntry``:
    ``key.file``, ``key.fn_name``, ``run_id``, ``iteration``, ``body_sha``,
    ``state``, ``oracle_fp``), in ledger order. A landing is a ``landed``
    row; its grade is the LAST non-``landed`` row after it with the same
    run id, unit and body sha. Several bodies landed for one unit at one
    iteration: the last one in ledger order stands for the landing (it is
    the body the route row's keep left on the tree)."""
    landed: dict[tuple[str, int, FnLocator], str] = {}
    graded: dict[tuple[str, FnLocator, str], str] = {}
    for e in entries:
        if str(getattr(e, "oracle_fp", "")) != oracle_fp:
            continue
        unit = FnLocator(e.key.file, e.key.fn_name)
        state = str(getattr(e.state, "value", e.state))
        if state == "landed":
            landed[(e.run_id, int(e.iteration), unit)] = e.body_sha
        else:
            graded[(e.run_id, unit, e.body_sha)] = state
    return UnitEvidence(
        grades={k: (sha, graded.get((k[0], k[2], sha)))
                for k, sha in landed.items()},
        standing_flat=dict(standing_flat))


class UnitService:
    """Which units the ledger has served under one oracle, and how.

    Service is a property of the UNIT, not of the batch that carried it. The
    dispatch batch is re-drawn every iteration, so a run keyed by its whole
    target list presented a new key almost every fill and ranked
    ``never_served`` at double weight while every unit in it had been bought
    27+ times (run `450977-5f48b68a`, 2026-09-21: fill 1's `file
    src/error.rs` never_served over 5 targets with prior buys min/median
    27/34; the per-fn batch never_served over 12 with median 27). Built once
    per schedule from the rows that OPENED a try — not deferred, empty,
    stopped or unmeasured (a reasoning-capped run served nothing, the 241
    grade) — under the schedule's oracle."""

    def __init__(self, rows: Iterable[RouteLedgerRow], oracle_fp: str, *,
                 session_min_turns: int = 0,
                 evidence: UnitEvidence | None = None) -> None:
        self._opened: dict[FnLocator, list[RouteLedgerRow]] = {}
        self._deferred: dict[FnLocator, list[RouteLedgerRow]] = {}
        # Every opened row under the oracle in ledger order, and the session
        # rows among the opened and deferred ones: a session's try is keyed
        # by the tests it aims at, not by the units it touched.
        self._opened_rows: list[RouteLedgerRow] = []
        self._sessions: list[RouteLedgerRow] = []
        self._sessions_deferred: list[RouteLedgerRow] = []
        self._min_turns = session_min_turns
        self._evidence = evidence
        rows = list(rows)
        self._joint = joint_sets_of(rows, oracle_fp)
        for r in rows:
            if r.oracle_fp != oracle_fp:
                continue
            bucket = (self._deferred if r.outcome is RunOutcome.DEFERRED
                      else self._opened if r.outcome not in (
                          RunOutcome.EMPTY, RunOutcome.STOPPED,
                          RunOutcome.UNMEASURED)
                      else None)
            if bucket is None:
                continue
            if bucket is self._opened:
                self._opened_rows.append(r)
            if r.kind is RouteKind.SESSION:
                (self._sessions if bucket is self._opened
                 else self._sessions_deferred).append(r)
            for f, fn in r.targets:
                bucket.setdefault(FnLocator(f, fn), []).append(r)

    def served(self, unit: FnLocator) -> bool:
        return unit in self._opened

    @staticmethod
    def _same_route(row: RouteLedgerRow, run: RouteRun) -> bool:
        """The route that would be re-bought: the same kind, and for a
        session the same aimed tests — two slices sharing hub units are
        different questions (`_work_key` keys sessions the same way)."""
        if row.kind is not run.kind:
            return False
        if run.kind is RouteKind.SESSION:
            return frozenset(row.target_tests) == frozenset(run.target_tests)
        return True

    def starved(self, row: RouteLedgerRow) -> bool:
        """A session row that ran out of its share before completing
        ``session_min_turns`` turns. It is not a graded try: the share, not
        the route, ended it. On `450977-5f48b68a` fill 1 the session ran 2
        of 8 turns in 25.8 min against a 20-min share and was rowed
        REVERTED; ranked on that row it would be a zero-weight re-buy, so
        the starved share would convict the route it starved. Zero turns
        configured means no row is starved (the pre-floor reading)."""
        return (row.kind is RouteKind.SESSION and self._min_turns > 0
                and row.model_calls < self._min_turns
                and row.share_minutes is not None
                and row.minutes >= row.share_minutes)

    def _prior_cut(self, row: RouteLedgerRow, test: str) -> bool:
        """The session try on `test` before `row` was cut by its share too."""
        prior = None
        for r in self._sessions:
            if r is row:
                break
            if test in r.target_tests:
                prior = r
        return prior is not None and self.starved(prior)

    def cut_by_share(self, row: RouteLedgerRow) -> bool:
        """`row` is :meth:`starved`, and at least one test it aimed at had not
        been cut by the share on its try before: the share owes a test one
        more look, not an unbounded number.

        A starved row ranks ``served`` because the share, not the route, ended
        it. That holds for a test's first cut. When the session try on the
        test before it was cut too, the share was too small twice, and
        nothing bounded the next one: on `450922-661c5e9f` `test_altdebug`
        was cut after one call at fill 10 (with `test_downcast_low`, 26.8
        min) and fill 11 (with `test_downcast_mid`, 33.2 min), and a third
        slice on it opened at fill 12. Keyed per test, because every fill
        re-packs the leftovers: no two of those three slices aimed at the
        same set. A test cut twice in a row is graded on the outcome its
        latest row recorded."""
        if not self.starved(row):
            return False
        if not row.target_tests:
            return True
        return any(not self._prior_cut(row, t) for t in row.target_tests)

    def session_rebuy(self, run: RouteRun) -> tuple[int, int] | None:
        """(rank, deferrals) when every test `run` aims at was last tried by a
        session that was graded and did not pay it; None otherwise.

        A session's slice is re-packed around its leftovers every fill, so
        a new unit set or a moved body makes a new identity and a new work
        key for the same question. The question is the test. Its latest
        session row decides: graded (not starved, or starved on a test whose
        try before was starved too — :meth:`cut_by_share`), FLAT / REVERTED /
        DECLINED, not paying the test, and no later opened row on any route
        paying it. One test without such a row (never tried by a session,
        owed its look after a first cut, or paid since) leaves the run at
        the rank it already holds. An unopened unit does not: `_rank_of`
        asks this for `never_served` as well as `served`, so a unit no
        route has opened, riding on tests that already have the row, is
        the re-buy. Deferrals are the session deferrals aimed at the test
        since that row, the least over the tests, for the bounded
        re-admission."""
        if run.kind is not RouteKind.SESSION or not run.target_tests:
            return None
        worst = _RANK_PAYING
        deferrals: list[int] = []
        for t in run.target_tests:
            mine = [r for r in self._sessions if t in r.target_tests]
            if not mine:
                return None
            last = mine[-1]
            owed = self.starved(last) and not self._prior_cut(last, t)
            if (owed or row_paid_test(last, t)
                    or last.outcome not in (RunOutcome.FLAT,
                                            RunOutcome.REVERTED,
                                            RunOutcome.DECLINED)):
                return None
            at = next(i for i, r in enumerate(self._opened_rows) if r is last)
            if any(row_paid_test(r, t) for r in self._opened_rows[at + 1:]):
                return None
            worst = max(worst, _RANK_REBUY_REVERTED
                        if last.outcome is RunOutcome.REVERTED
                        else _RANK_REBUY_FLAT)
            deferrals.append(sum(
                1 for d in self._sessions_deferred
                if t in d.target_tests and d.started >= last.started))
        return worst, min(deferrals)

    def rank(self, run: RouteRun, *, grace_left: bool,
             readmit_after: int) -> tuple[int, str]:
        """(rank, reason) for a run the identity ledger has no row for.

        ``never_served`` only when a target has no opened row under this
        oracle on ANY route: an untried unit is a new try whatever rides
        with it. Otherwise each target is classed by its latest row on this
        route — paid: ``paying``; none, starved by its share, or a later try
        on another route paid: ``served``; flat or reverted: that re-buy —
        and the run takes its WORST target's class, so a batch re-drawn
        around units bought and never paid ranks as the re-buy it is. On
        `450977-5f48b68a` fill 1 "best target decides" left the 12-unit
        per-fn re-fill at ``served`` because one unit was new to per-fn
        while the other eleven had been bought flat on it. A re-buy whose
        every re-buy target has been deferred `readmit_after` times since
        its latest try here is re-admitted, as the identity path does."""
        targets = run.targets
        if not targets or any(not self.served(t) for t in targets):
            return _RANK_NEVER_SERVED, _REASON[_RANK_NEVER_SERVED]
        worst = _RANK_PAYING
        rebuy_deferrals: list[int] = []
        for t in targets:
            cls, deferrals = self.target_class(t, run, grace_left=grace_left)
            if deferrals is not None:
                rebuy_deferrals.append(deferrals)
            worst = max(worst, cls)
        if (worst in (_RANK_REBUY_FLAT, _RANK_REBUY_REVERTED)
                and rebuy_deferrals and min(rebuy_deferrals) >= readmit_after):
            return worst, "rebuy_readmitted"
        return worst, _REASON[worst]

    def target_class(self, t: FnLocator, run: RouteRun, *,
                     grace_left: bool) -> tuple[int, int | None]:
        """(class, deferrals) of one target on `run`'s route: the rank its
        latest opened row on this route gives it, and — for a re-buy only —
        how many times this route deferred it since that row (the bounded
        re-admission count). A target never opened on any route is
        never-served.

        A re-buy is the same try again. A unit offered together with every
        partner a reply named it with (`RouteLedgerRow.joint_sets`), when
        its last try on this route wrote it without one of them, is not: that
        write was never tried, so it is `served`. Run `116341-24c76a97`
        (2026-09-25) fill 6: `src/error.rs::context`, reverted alone on the
        file route, was carved into a re-buy piece away from the table fns
        its body installs, and neither piece could write the pair."""
        cls, deferrals = self._target_class(t, run, grace_left=grace_left)
        if (cls in (_RANK_REBUY_FLAT, _RANK_REBUY_REVERTED)
                and self._joint_untried(t, run)):
            return _RANK_SERVED, None
        return cls, deferrals

    def _joint_untried(self, t: FnLocator, run: RouteRun) -> bool:
        """``t`` rides in ``run`` with every partner of a set a reply named
        it with, and its latest try on this route wrote it without one."""
        named = [js for js in self._joint if t in js and js <= run.targets]
        if not named:
            return False
        here = [r for r in self._opened.get(t, ()) if self._same_route(r, run)]
        wrote: set[FnLocator] = set()
        if here:
            last = here[-1]
            wrote = {FnLocator(f, fn) for f, fn in last.targets}
            for js in last.joint_sets:
                wrote |= {FnLocator(f, fn) for f, fn in js}
        return any(not js <= wrote for js in named)

    def _target_class(self, t: FnLocator, run: RouteRun, *,
                      grace_left: bool) -> tuple[int, int | None]:
        if not self.served(t):
            return _RANK_NEVER_SERVED, None
        if self.judges_units(run):
            return self._landing_class(t, run, grace_left=grace_left)
        opened = self._opened[t]
        here_at = [i for i, r in enumerate(opened)
                   if self._same_route(r, run)]
        if (not here_at or self.cut_by_share(opened[here_at[-1]])
                or any(_row_paid(r) for r in opened[here_at[-1] + 1:])):
            # Never tried on this route, starved by its share, or a later
            # try on another route PAID (the verdicts this route last saw
            # moved): served, and this try is new. A later flat keep
            # elsewhere moves the body, not the evidence — counting it
            # made every re-buy "served" on `450977-5f48b68a`'s ledger,
            # where session, file and per-fn touch the same units in turn.
            return _RANK_SERVED, None
        last = opened[here_at[-1]]
        if last.outcome is RunOutcome.CASHED or last.net_pass_delta > 0:
            return _RANK_PAYING, None
        if last.outcome is RunOutcome.REVERTED:
            cls = (_RANK_SERVED if _pair_revert_owes_a_retry(run, last, grace_left)
                   else _RANK_REBUY_REVERTED)
        else:
            cls = _RANK_REBUY_FLAT
        if cls == _RANK_SERVED:
            return cls, None
        return cls, self._deferrals_since(t, run, last)

    def _deferrals_since(self, t: FnLocator, run: RouteRun,
                         last: RouteLedgerRow) -> int:
        return sum(1 for d in self._deferred.get(t, ())
                   if self._same_route(d, run) and d.started >= last.started)

    def judges_units(self, run: RouteRun) -> bool:
        """True when `run`'s units are ranked by their own graded landings
        (:class:`UnitEvidence`) rather than by the route rows' outcomes.
        Every unit-writing route; not a session, whose try is keyed by the
        tests it aims at (the slice), not by the units its transcript
        happened to touch."""
        return self._evidence is not None and run.kind is not RouteKind.SESSION

    def _landing_class(self, t: FnLocator, run: RouteRun, *,
                       grace_left: bool) -> tuple[int, int | None]:
        """The unit's class from its own latest landing on this route.

        - no row on this route KEPT or REVERTED the unit (a target the run
          never claimed is not a try): served;
        - that landing starved, or a later try on another route paid:
          served (the `rank` rules, unchanged);
        - the unit was reverted there, and the revert implicated it: the
          reverted re-buy (a pair keeps its grace, as in `rank`). A revert
          that restored the unit only because its transaction was restored
          whole (``RouteLedgerRow.collateral``) is a verdict on the set; the
          identical set comes back through its identity row (`_rank_of`),
          and a different set is a new try for the unit;
        - the sweep graded the landed body advanced or proven: paying;
        - that body is still on the tree and its FLAT claim still stands
          (:attr:`UnitEvidence.standing_flat`): the flat re-buy;
        - no grade, and the row paid: paying;
        - anything else — unmeasured, never graded, flat but off the tree
          or under moved evidence, a cash that was a sibling's: served.
        """
        ev = self._evidence
        assert ev is not None
        opened = self._opened[t]
        here_at = [i for i, r in enumerate(opened)
                   if self._same_route(r, run) and _landed(r, t)]
        if not here_at:
            return _RANK_SERVED, None
        last = opened[here_at[-1]]
        if self.cut_by_share(last) or any(
                _row_paid(r) for r in opened[here_at[-1] + 1:]):
            return _RANK_SERVED, None
        if (t in _locs(last.reverted) and t not in _locs(last.kept)
                and t not in _locs(last.collateral)):
            if _pair_revert_owes_a_retry(run, last, grace_left):
                return _RANK_SERVED, None
            return _RANK_REBUY_REVERTED, self._deferrals_since(t, run, last)
        g = ev.grade(last.run_id, last.iteration, t)
        if g is not None and g[1] in _PAYING_GRADES:
            return _RANK_PAYING, None
        if g is not None and ev.stands_flat(t, g[0]):
            return _RANK_REBUY_FLAT, self._deferrals_since(t, run, last)
        if (g is None or g[1] is None) and _row_paid(last):
            return _RANK_PAYING, None
        return _RANK_SERVED, None


def _pair_revert_owes_a_retry(run: RouteRun, last: RouteLedgerRow,
                              grace_left: bool) -> bool:
    """True when a pair's revert is owed its retry inside the grace: the
    revert stored a crash diagnosis the next prompt carries. A revert whose
    aimed tests RAN and stayed red (`classes` holds `wrong`) has no such
    diagnosis; it is a measured re-buy on its first row (run
    `450922-661c5e9f` fill 11: the formatter pair, `{wrong: 1}`, reopened
    `never_served` for 20.9 min at net 0). Every rank site asks this one
    rule, so no path can grant the grace the others refuse."""
    return (run.kind is RouteKind.PAIR and grace_left
            and not last.classes.get(FillOutcomeClass.WRONG_BODY.value))


def _locs(pairs: Iterable[tuple[str, str]]) -> frozenset[FnLocator]:
    return frozenset(FnLocator(f, fn) for f, fn in pairs)


def _landed(row: RouteLedgerRow, t: FnLocator) -> bool:
    return t in _locs(row.kept) or t in _locs(row.reverted)


def joint_sets_of(rows: Iterable[RouteLedgerRow], oracle_fp: str
                  ) -> tuple[frozenset[FnLocator], ...]:
    """Every write set a reply named as one, from the ledger rows under
    ``oracle_fp`` (a set named under another oracle was a claim about
    another test suite's tree)."""
    out: dict[frozenset[FnLocator], None] = {}
    for r in rows:
        if r.oracle_fp != oracle_fp:
            continue
        for js in r.joint_sets:
            members = frozenset(FnLocator(f, fn) for f, fn in js)
            if len(members) > 1:
                out[members] = None
    return tuple(out)


def carve_rebuys(run: RouteRun, units: UnitService, *,
                 readmit_after: int) -> tuple[RouteRun, ...]:
    """A run whose route writes each target on its own, split into the
    units that are a new or paying try and the units that are a re-buy.

    "The run takes its worst target's class" is right for a transaction —
    a file or an impl block is re-bought whole — and wrong for a route
    that claims unit by unit: there it lets ONE flat unit convict every
    other unit riding in the batch. Run `3131-f6b37c1a` (2026-09-23,
    cluster `stable`, after a restart from the augmented tree): the per-fn
    batch of 12 stubs was deferred `rebuy_flat` in fills 1 and 2 because
    one target's last per-fn try (run `497151-a6fa205b`) was flat, while
    the other eleven's last per-fn try (run `713017-baa50600`) had PAID —
    among them the stub 45 of the 150 failing tests first panicked in.
    No route opened those eleven; the fills went to 1-stub impl blocks
    whose tests were hollow on them, and the score sat at 29/184.

    A unit is a re-buy when :meth:`UnitService.target_class` says so and
    its own deferrals since that try are fewer than `readmit_after` (the
    same bounded re-admission the whole run gets). Returns `(run,)` when
    the run is not divisible or nothing splits; otherwise the open piece
    first and the re-buy piece second, each carrying `parent=run.key`, its
    own targets, stubs, files and the union of its units' aimed tests. The
    re-buy piece is ranked, deferred and rowed like any run, so each of
    its units accrues its own deferrals and is re-admitted on its own
    count."""
    if not run.unit_tests:
        return (run,)
    rebuy: set[FnLocator] = set()
    for t in run.targets:
        cls, deferrals = units.target_class(t, run, grace_left=True)
        if (cls in (_RANK_REBUY_FLAT, _RANK_REBUY_REVERTED)
                and (deferrals or 0) < readmit_after):
            rebuy.add(t)
    if not rebuy or rebuy == set(run.targets):
        return (run,)
    tests_of = dict(run.unit_tests)

    def _piece(targets: frozenset[FnLocator], what: str) -> RouteRun:
        return replace(
            run, label=f"{run.label} [{len(targets)} {what}]",
            targets=targets,
            stubs_in_targets=run.stubs_in_targets & targets,
            target_tests=frozenset().union(
                *(tests_of.get(t, frozenset()) for t in targets)),
            files=tuple(sorted({t.file for t in targets})),
            unit_tests=tuple((t, tests_of.get(t, frozenset()))
                             for t in sorted(targets,
                                             key=lambda u: (u.file, u.fn_name))),
            bodies_sha="", parent=run.key)

    open_part = frozenset(run.targets) - rebuy
    return (_piece(open_part, "unit(s) to try"),
            _piece(frozenset(rebuy), "re-buy unit(s)"))


def release_held(held: Sequence[ScheduledRun],
                 not_opened: Collection[str]) -> tuple[ScheduledRun, ...]:
    """The held runs every holder of which is known not to open this fill
    (``not_opened``: run keys the executor deferred), released: no longer
    deferred, ranked again with the reason they had before the hold.

    The schedule's pass holds a run only behind a run it OPENS, because a
    hold on a run that never opens is a wait for nothing — no grace clock
    can release it, a deferred run writes no graded row (run
    `3127-6763a5c6`, 2026-09-18: fills 2-6 opened nothing). The pass cannot
    see the executor defer that holder afterwards. Run `116341-24c76a97`
    (2026-09-25) fill 6: both formatter pairs were deferred `premise_moved`
    after the per-fn run's keeps, and the error.rs file and impl runs and
    the 26-unit session they held stayed `pair_wait` behind them — the one
    planned run that held `src/error.rs::context` beside its table fns
    never opened. One rule for both holds (`pair_wait`, `coverage_split`);
    a run with no recorded holder is not a hold and is never released."""
    gone = frozenset(not_opened)
    return tuple(
        replace(h, deferred=False, reason=h.prior_reason or h.reason,
                share_minutes=None)
        for h in held
        if h.held_by and gone.issuperset(h.held_by))


def withhold_landed(run: RouteRun, landed: Collection[FnLocator],
                    ) -> tuple[RouteRun | None, frozenset[FnLocator]]:
    """`run` without the targets an earlier run of THIS fill kept a body
    for: ``(run to open, withheld)``. ``None`` to open means defer the run.

    A kept body is judged at the next sweep and not before: until then no
    reading says whether it paid, so a second route rewriting it in the same
    fill buys an answer to a question nobody has scored, and overwrites the
    body the sweep was about to grade (`body_ledger.classify` then reads the
    first landing `unmeasured`, because it is no longer the body on the
    tree). Run `143676-7dadc500` (2026-09-24, cluster `stable`), iteration 3:
    10 of the per-fn run's 12 targets were bodies the file runs had kept
    earlier that fill; the per-fn run spent 86 min and 23 calls on them and
    moved no verdict, and iteration 4 repeated it for 6 of 12. The per-impl
    route already waited for the sweep after a file-first or pair landing
    (`refill_stamp.blocks_after_file_first`, `pair_fill.
    blocks_after_pair_fill`); the per-fn route asked neither, because each
    rule lived inside the route that happened to follow the one it guarded.
    This answers the question once, for every run the executor opens.

    A route that writes each target on its own (``unit_tests``) is carved
    to the rest, as `carve_rebuys` carves a re-buy; a transaction (a pair, a
    file with no per-unit claims) or a run with nothing left is deferred
    whole. A session is never withheld: its try is keyed by the tests it
    aims at, not by the units its transcript touches
    (:meth:`UnitService.judges_units`), and its own turns are judged by the
    suite as they land. Its keeps still withhold them from later runs."""
    held = frozenset(run.targets) & frozenset(landed)
    if not held or run.kind is RouteKind.SESSION:
        return run, frozenset()
    rest = frozenset(run.targets) - held
    if not run.unit_tests or not rest:
        return None, held
    tests_of = dict(run.unit_tests)
    return replace(
        run, label=f"{run.label} [{len(rest)} not landed this fill]",
        targets=rest,
        stubs_in_targets=run.stubs_in_targets & rest,
        target_tests=frozenset().union(
            *(tests_of.get(t, frozenset()) for t in rest)),
        files=tuple(sorted({t.file for t in rest})),
        unit_tests=tuple((t, tests_of.get(t, frozenset()))
                         for t in sorted(rest, key=lambda u: (u.file, u.fn_name))),
        bodies_sha="", parent=run.plan_key), held


def _rank_of(run: RouteRun, oracle_fp: str,
             history: Mapping[str, list[RouteLedgerRow]],
             units: UnitService,
             *, readmit_after: int, grace_left: bool = True,
             ) -> tuple[int, str, int]:
    """(rank, reason, deferrals) for one run against the ledger.

    `units` answers for a run whose identity has no graded row — the
    "served, but under another identity" class — by the service its
    UNITS have had (:class:`UnitService`)."""
    ident = rebuy_identity(
        run.kind, run.targets, run.bodies_sha, oracle_fp,
        target_tests=run.target_tests)
    rows = history.get(ident) or []
    # Rows that RAN (not deferred, not empty): the latest decides the
    # class. Deferrals since it are counted for the bounded re-admission;
    # an EMPTY row (the route returned before its first dispatch — a file
    # over the file-first fn bound, a pair the refusal gate closed) says
    # nothing about the body and neither hides an earlier verdict nor
    # counts as a deferral.
    deferrals = 0
    last: RouteLedgerRow | None = None
    for r in reversed(rows):
        if r.outcome is RunOutcome.DEFERRED:
            deferrals += 1
            continue
        if r.outcome is RunOutcome.EMPTY:
            continue
        last = r
        break
    if last is None or (units.judges_units(run) and (
            last.outcome is RunOutcome.FLAT
            or (run.unit_tests and last.outcome is not RunOutcome.REVERTED))):
        # With unit evidence, a divisible run has ONE rank path, and an
        # identical run's FLAT row is not a verdict either — its units'
        # graded landings are. Without this the next fill of the same
        # targets and bodies was deferred whole by the row's FLAT and the
        # per-unit verdict was never asked (validation of 2026-09-23 on the
        # `3131-f6b37c1a` batches). A REVERTED identity row stays the gate's
        # verdict on the run, divisible or not: the same targets over the
        # same bodies are the transaction the gate restored, whether its
        # revert implicated each unit or restored the set whole (a
        # collateral revert convicts no unit, so without this the identical
        # file would re-open every fill). An interrupted one stays no
        # evidence.
        rank, reason = units.rank(run, grace_left=grace_left,
                                  readmit_after=readmit_after)
    else:
        rank, reason = _ledger_rank(run, last, grace_left=grace_left,
                                    starved=units.cut_by_share(last))
    if (run.kind is RouteKind.PAIR and not grace_left
            and rank in (_RANK_NEVER_SERVED, _RANK_SERVED)):
        # Past the grace a pair is never "never served": its work key has
        # `unpaid_grace` graded rows at least, and the identity misses them
        # only because it carries the bodies sha, which any kept body moves.
        # Anything the ledger can say beyond that — the identical bodies
        # came back flat, or reverted — it says through `_ledger_rank`,
        # exactly as for every other route: run `3893442-cb547388`
        # (2026-09-19) held 10 graded rows on the debug pair under one
        # oracle, none paying, the last two on one bodies sha, and the
        # pair still opened ahead of four never-served sessions.
        return _RANK_SERVED, "grace_spent", deferrals
    if (run.kind is RouteKind.SESSION
            and rank in (_RANK_SERVED, _RANK_NEVER_SERVED)):
        # A re-packed slice is a new identity and a new work key for the
        # same tests; the tests' own latest session tries decide. An
        # unopened unit is `never_served` from `UnitService.rank` whatever
        # rides with it, which is how a twice-cut test reopened: the slice
        # added one unit no route had opened. The tests still decide.
        # `paying` is left alone, and so is a `never_served` whose tests
        # have no graded unpaid row (`session_rebuy` returns None): a
        # first cut, an untried test, an interrupted try.
        rebuy = units.session_rebuy(run)
        if rebuy is not None:
            rank, deferrals = rebuy
            reason = _REASON[rank]
    if rank in (_RANK_REBUY_FLAT, _RANK_REBUY_REVERTED) \
            and deferrals >= readmit_after:
        return rank, "rebuy_readmitted", deferrals
    return rank, reason, deferrals


def _ledger_rank(run: RouteRun, last: RouteLedgerRow, *,
                 grace_left: bool, starved: bool = False) -> tuple[int, str]:
    """(rank, reason) from the latest graded row on the run's identity."""
    if last.outcome is RunOutcome.CASHED or last.net_pass_delta > 0:
        return _RANK_PAYING, _REASON[_RANK_PAYING]
    if last.outcome in (RunOutcome.STOPPED, RunOutcome.UNMEASURED):
        # Interrupted, or every reply reasoning-capped: not evidence.
        return _RANK_NEVER_SERVED, _REASON[_RANK_NEVER_SERVED]
    if starved:
        # Ended by its share before its minimum turns, the first such cut
        # on its aimed tests (UnitService.cut_by_share): opened, so served,
        # but not a graded try.
        return _RANK_SERVED, _REASON[_RANK_SERVED]
    if (last.outcome is RunOutcome.REVERTED
            and _pair_revert_owes_a_retry(run, last, grace_left)):
        # The pair's revert stores a diagnosis the next prompt is
        # supposed to carry. Ranking it as a zero-weight rebuy defers
        # that retry under a positive budget, so the next fill opens
        # the overlapping session instead and splits the transaction
        # (run 200802-b835274e, 2026-09-10: fill 1 pair reverted, fills
        # 2 and 3 never opened it). A pair FLAT is still a rebuy — the
        # bodies landed and the leftovers stayed red.
        #
        # `grace_left` is that premise, made checkable. "The next prompt
        # carries a new diagnosis" is true of the first retry and false
        # of the fiftieth: once the route has been graded `unpaid_grace`
        # times without paying, the revert is ordinary re-buy evidence
        # and ranks as what it is. Without the bound this branch answers
        # NEVER SERVED about a route the ledger has 60 graded rows for
        # (cluster `stable`, 2026-09-14, none of them cashed), which is
        # the one state the re-buy detector exists to catch.
        return _RANK_NEVER_SERVED, _REASON[_RANK_NEVER_SERVED]
    rank = (_RANK_REBUY_REVERTED if last.outcome is RunOutcome.REVERTED
            else _RANK_REBUY_FLAT)
    return rank, _REASON[rank]


def _work_key(kind: RouteKind, targets: Iterable[FnLocator],
              target_tests: Collection[str]) -> str:
    """Served-under-another-identity key. Sessions include the aimed tests
    so two slices that share hubs are not the same try."""
    names = ",".join(sorted(f"{f}::{fn}" for f, fn in targets))
    base = f"{kind.value}|{names}"
    if kind is RouteKind.SESSION:
        return base + "|" + ",".join(sorted(target_tests))
    return base


def _served_key(run: RouteRun) -> str:
    return _work_key(run.kind, run.targets, run.target_tests)


def _served_key_of(row: RouteLedgerRow) -> str:
    return _work_key(
        row.kind, [FnLocator(f, fn) for f, fn in row.targets],
        row.target_tests)


def _aims_at_nothing(run: RouteRun, blamed: frozenset[FnLocator]) -> bool:
    """True when no verdict can move by construction: the run fills no stub,
    aims at no failing test, and writes no crash-blamed locator. It rewrites
    bodies already on disk that nothing failing is attributed to. One rule
    for every route, so a new route cannot recreate the case by omitting a
    guard in its planner. Run `484518-91681c1a` fill 5 (2026-09-23): two
    formatter pairs whose members had dropped out of the dispatch map
    (`stubs []`, `target_tests []`) opened first and kept 4 bodies flat in
    20 minutes.

    A run with no targets writes nothing and spends no call; the per-fn
    route plans one when every stub it found is cfg-dead, so the pass can
    log and emit that refusal (`phase_c.dispatch.cfg_dead_refused`). It is
    left to the route."""
    return (bool(run.targets) and not run.stubs_in_targets
            and not run.target_tests and not (run.targets & blamed))


def condensed_order(keys: Sequence[str],
                    blockers: Mapping[str, Collection[str]]) -> list[str]:
    """`keys` (given in sort order) in a topological order of `blockers`
    (key -> the keys that must go before it), where a set of keys that
    block each other is placed as ONE node: its position is its best
    member's sort position, and its members keep their sort order.

    Equal to "first key in sort order whose blockers are all placed" on an
    acyclic relation. It differs only on a cycle, and that is the case it
    exists for: the plain walk can place a cycle only once NO key is
    placeable, so every key outside the cycle — however low its sort
    position — goes first. On a stub-heavy tree nearly every run holds a
    stub some other run's tests are hollow on, so the blocks relation is
    one large cycle and the runs outside it are exactly those nothing can
    block (a run aiming at no test). Run `484518-91681c1a` fill 5
    (2026-09-23): the sweep named the first failure of all 141 failing
    tests, the per-fn run holding that stub sorted first (level 0), an
    11-run cycle held it, and the two level-2 formatter pairs — no stub,
    no aimed test — opened ahead of it for 20 flat minutes.
    """
    pos = {k: i for i, k in enumerate(keys)}
    succ: dict[str, list[str]] = {k: [] for k in keys}
    for k in keys:
        for b in blockers.get(k, ()):
            if b in pos and b != k:
                succ[b].append(k)
    for k in keys:
        succ[k].sort(key=pos.__getitem__)

    # Tarjan's strongly connected components, iterative so a large fill
    # cannot hit the recursion limit; roots visited in sort order.
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp_of: dict[str, int] = {}
    comps: list[list[str]] = []
    counter = 0
    for root in keys:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            if i < len(succ[v]):
                work.append((v, i + 1))
                w = succ[v][i]
                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp_of[w] = len(comps)
                    comp.append(w)
                    if w == v:
                        break
                comps.append(sorted(comp, key=pos.__getitem__))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])

    comp_blockers: list[set[int]] = [set() for _ in comps]
    for k in keys:
        for b in blockers.get(k, ()):
            if b in pos and comp_of[b] != comp_of[k]:
                comp_blockers[comp_of[k]].add(comp_of[b])
    pending = sorted(range(len(comps)), key=lambda c: pos[comps[c][0]])
    placed: set[int] = set()
    out: list[str] = []
    while pending:
        pick = next(c for c in pending if not (comp_blockers[c] - placed))
        placed.add(pick)
        pending.remove(pick)
        out.extend(comps[pick])
    return out


def _writes_any(run: RouteRun, blockers: Blockers,
                units: Collection[FnLocator]) -> bool:
    """Whether `run` writes any of `units`, by the one blocker predicate
    (`Blockers.writes`)."""
    session = run.kind is RouteKind.SESSION
    return any(blockers.writes(u, targets=run.targets,
                               aimed_tests=run.target_tests, session=session)
               for u in units)


def _level(run: RouteRun, blockers: Blockers) -> int:
    """The blockers-first tier. 0: the run writes a locator the sweep's
    crash evidence names (a frame-sited crash, or the stub failing tests
    first panic in); 1: it writes a stub some leftover test is held on;
    2: the rest. "Writes" is :meth:`Blockers.writes`: a session that only
    carries the blocker in its editable set, for another test's slice, does
    not write it.

    One tier for 0 and 1 said nothing about WHICH blocker: on a stub-heavy
    tree every run holds some stub a hollow test runs through, so the tier
    was everyone and the fixed route order broke the tie. Run
    `220812-8d346f9c` fill 1 (2026-09-23): the sweep named
    `src/error.rs::construct` as the first panic of 128 of 141 failing
    tests, and the per-fn run holding it opened at slot 8 of 9, behind
    seven file and impl runs of 22 minutes each, none holding it — with the
    fill ending at its first premise move.

    Target membership was the test until 2026-10-11. Run
    `2157238-210637e6` fills 2 and 3: every session carried a held stub or
    a crash culprit as a passenger, every run read tier 0, and the route
    order opened four sessions (110 minutes, net +1) ahead of the file,
    impl and per-fn runs that aimed at those blockers."""
    if _writes_any(run, blockers, blockers.culprits):
        return 0
    if _writes_any(run, blockers, blockers.held.units):
        return 1
    return 2


def _run_is_unmeasurable(
    run: RouteRun,
    unmeasurable_tests: Collection[str],
    crash_blamed: Mapping[FnLocator, Collection[str]] | Collection[FnLocator],
) -> bool:
    """True when every aimed test is one whose binary did not complete,
    and the run writes no crash-blamed locator that can complete it
    (:meth:`Blockers.writes`: a session carrying a culprit for another
    test's slice is not its writer).

    Empty ``target_tests`` is not this class: the run named no test, so it
    cannot be "every name is unmeasurable." Empty ``unmeasurable_tests``
    (the default) preserves every existing rank. A crash-blamed target
    keeps today's rank even when the aimed tests are all unmeasurable —
    that unit is the one whose body can make the binary complete.
    """
    if not run.target_tests or not unmeasurable_tests:
        return False
    if crash_blamed:
        blockers = Blockers.of({}, crash_blamed)
        if _writes_any(run, blockers, blockers.culprits):
            return False
    return run.target_tests <= frozenset(unmeasurable_tests)


run_is_unmeasurable = _run_is_unmeasurable


def _is_coverage_split(
    run: RouteRun,
    attributed: Collection[FnLocator],
) -> bool:
    """True when this leftover-aiming run writes coverage locators that
    are not the attributed write of those leftovers.

    Empty ``attributed`` is not this class — every recorded fill fixture
    that does not pass the snapshot keeps today's order. A run that names
    no leftover test is not this class. Same-file extras on an attributed
    member are that file's installable complement, not a split; extras
    whose file is not a file of the attributed members this run holds
    (a session that packed the coverage spine) are.
    """
    return _targets_split(run.targets, run.target_tests, attributed)


def _targets_split(targets: Collection[FnLocator],
                   target_tests: Collection[str],
                   attributed: Collection[FnLocator]) -> bool:
    """`_is_coverage_split` over bare targets, so a ledger row can be asked
    the same question a candidate run is."""
    if not attributed or not target_tests:
        return False
    attr = frozenset(attributed)
    tset = frozenset(targets)
    extra = tset - attr
    if not extra:
        return False
    attributed_here = tset & attr
    if not attributed_here:
        return True
    attributed_files = {loc.file for loc in attributed_here}
    return any(loc.file not in attributed_files for loc in extra)


def unpaid_attributed_writes(rows: Iterable[RouteLedgerRow], test: str,
                             attributed: Collection[FnLocator],
                             oracle_fp: str) -> int:
    """Graded non-split writes aimed at `test` since one turned it green.

    "Non-split" is judged against the attributed set the schedule holds
    NOW (:func:`_targets_split` on the row's targets), so a row is counted
    as the attributed write of `test` exactly when a run with those
    targets would be one today. This is the record a coverage-split's
    deferral stands on: the attributed write goes first because it is the
    write the leftover convicts, and after `unpaid_grace` of them in a row
    have not moved the leftover, that premise has been measured false.
    """
    attr = frozenset(attributed)
    return unpaid_tries_for_test(
        rows, test, oracle_fp,
        match=lambda r: not _targets_split(
            [FnLocator(f, fn) for f, fn in r.targets], r.target_tests, attr))


#: Session rows the turn price is measured over, newest first.
_TURN_COST_WINDOW = 20


@dataclass(frozen=True, slots=True)
class SessionTurnPrice:
    """What a session's turns cost, in minutes: its first turn, and each turn
    after it.

    The two are different quantities. The first turn carries the session's
    whole opening: the baseline suite, turn 0's prompt (the largest the
    session sends) and the deliberation over it (the longest). A later turn
    answers a diagnostic on a transcript the model has already read. On run
    `450922-661c5e9f` (2026-10-05, cluster stable) first turns ran 1.4–33.2
    min, median 16.1, and later turns mostly 0.0–4.5. A per-call median
    spreads the first turn over turns the session never reaches: fill 9
    reserved a fresh slice 3 × 3.98 = 11.9 min and fill 10 3 × 4.19 = 12.6,
    and six of their seven sessions ran one turn and were cut by the share.

    ``first == later`` is the uniform per-call reading, which is all a ledger
    with no first-turn sample can say."""

    first: float
    later: float

    def floor(self, turns: int) -> float:
        """Minutes for `turns` turns: the first, then `turns - 1` later ones."""
        if turns <= 0:
            return 0.0
        return self.first + (turns - 1) * self.later

    def note(self, turns: int) -> str:
        if turns <= 1 or self.first == self.later:
            return f"floor {turns}x{self.later:.1f}m"
        return f"floor {self.first:.1f}m+{turns - 1}x{self.later:.1f}m"


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _first_turn_sample(r: RouteLedgerRow) -> float | None:
    """The first turn's minutes on one session row. A row that made one call
    is its first turn whole, recorded field or not."""
    if r.first_turn_minutes is not None:
        return r.first_turn_minutes
    if r.model_calls == 1:
        return r.minutes
    return None


def _later_turn_sample(r: RouteLedgerRow) -> float | None:
    """Minutes per turn after the first on one session row. A row written
    before the first turn was recorded can only give minutes / calls, an
    upper bound (it still holds the first turn)."""
    if r.model_calls < 2:
        return None
    if r.first_turn_minutes is not None:
        return max(0.0, r.minutes - r.first_turn_minutes) / (r.model_calls - 1)
    return r.minutes / r.model_calls


CALL_PRICE_WINDOW = 20


def call_price(rows: Iterable[RouteLedgerRow], kind: RouteKind, oracle_fp: str,
               *, window: int = CALL_PRICE_WINDOW) -> float | None:
    """Minutes one call of `kind` takes on this oracle: the median of
    minutes per call over the newest `window` rows of that kind that made a
    call. None when no such row exists — the clock then reserves the rest of
    the share on a grant (:class:`RunClock`). What a granted claim reserves
    while its unit is in flight."""
    graded = [r for r in rows if r.kind is kind and r.oracle_fp == oracle_fp
              and r.model_calls > 0 and r.minutes > 0]
    if not graded:
        return None
    return float(statistics.median(
        r.minutes / r.model_calls for r in graded[-window:]))


def session_turn_price(rows: Iterable[RouteLedgerRow],
                       run: RouteRun) -> SessionTurnPrice | None:
    """The session's turn price: each half the median of its samples over
    this session's own rows, falling back to the newest `_TURN_COST_WINDOW`
    session rows that give that half a sample; None when no session row
    gives either.

    A session's own rows are the session rows aimed at any of its tests, the
    key the ranking reads a session try by (`UnitService._same_route`). The
    unit set is not part of it: every fill re-packs the leftovers, so a
    work key with units in it is new almost every fill, and the price fell
    to the window, which on `450922-661c5e9f` fill 9 was a first-turn median
    of 11.0 min from older runs while this run's tail first turns ran 13-33.
    The turn-0 prompt is the slice the test executes, so the test's own
    first turns are the price of its next one. A run that aims at no test
    is matched on its work key.

    A share-cut row is priced like any other: its minutes are the turns it
    ran, and a one-call cut row is exactly the first-turn sample this price
    exists to keep. With no first-turn sample anywhere the price is the
    uniform per-call median the floor was priced at before
    (``first == later``). Any oracle: a turn's price does not depend on which
    tests fail."""
    sessions = [r for r in rows if r.kind is RouteKind.SESSION
                and r.model_calls > 0 and r.minutes > 0]
    if run.target_tests:
        own = [r for r in sessions if run.target_tests & set(r.target_tests)]
    else:
        key = _served_key(run)
        own = [r for r in sessions if _served_key_of(r) == key]

    def _half(sample: Callable[[RouteLedgerRow], float | None]) -> float | None:
        mine = [x for x in map(sample, own) if x is not None]
        if mine:
            return _median(mine[-_TURN_COST_WINDOW:])
        pool = [x for x in map(sample, sessions) if x is not None]
        return _median(pool[-_TURN_COST_WINDOW:]) if pool else None

    first = _half(_first_turn_sample)
    later = _half(_later_turn_sample)
    if first is None and later is None:
        return None
    if first is None:
        assert later is not None
        return SessionTurnPrice(first=later, later=later)
    return SessionTurnPrice(first=first, later=first if later is None else later)


def _allot(scheduled: list[ScheduledRun], budget_minutes: float
           ) -> list[ScheduledRun]:
    """Shares by weight with floors honoured: a run whose floor exceeds its
    weighted share takes the floor, and the rest split what remains by
    weight (repeated until no further run is lifted, so the result does not
    depend on order). Floors whose sum exceeds the budget were clipped by
    the caller to budget / floored runs."""
    floored: set[str] = set()
    while True:
        rest = budget_minutes - sum(
            s.floor_minutes or 0.0 for s in scheduled if s.run.key in floored)
        free_w = sum(s.weight for s in scheduled if s.run.key not in floored)
        lifted = False
        for s in scheduled:
            if s.run.key in floored or not s.floor_minutes:
                continue
            share = rest * s.weight / free_w if free_w > 0 else 0.0
            if s.floor_minutes > share:
                floored.add(s.run.key)
                lifted = True
        if not lifted:
            break
    out: list[ScheduledRun] = []
    for s in scheduled:
        if s.run.key in floored:
            out.append(replace(s, share_minutes=s.floor_minutes))
        else:
            out.append(replace(s, share_minutes=(
                rest * s.weight / free_w if free_w > 0 else 0.0)))
    return out


def schedule(
    runs: Sequence[RouteRun],
    rows: Iterable[RouteLedgerRow],
    *,
    oracle_fp: str,
    hollow: Mapping[str, frozenset[FnLocator]],
    budget_minutes: float = 0.0,
    today_order: Sequence[RouteKind] = TODAY_ORDER,
    readmit_after: int = 2,
    unmeasurable_tests: Collection[str] = (),
    crash_blamed: CrashBlame = (),
    max_per_kind: Mapping[RouteKind, int] | None = None,
    attributed: Collection[FnLocator] = (),
    unpaid_grace: int = DEFAULT_UNPAID_GRACE,
    session_min_turns: int = 0,
    bodies_sha: Callable[[frozenset[FnLocator]], str] | None = None,
    unit_evidence: UnitEvidence | None = None,
    due: Collection[FnLocator] = (),
    due_rungs: Mapping[FnLocator, str] | None = None,
) -> FillSchedule:
    """Order the candidate runs and allot the budget. Pure; deterministic.

    ``due`` is what the red tests owe this fill (:mod:`candidate_tiers`):
    functions on a tier no prompt has asked under the current evidence.
    Each must have an opened writer; whatever deferred its writers, the
    cover stage opens the first one (:func:`admit_ranked`).

    A run whose route writes each target on its own (``unit_tests``) is
    ranked by unit: its re-buy units are carved into a run of their own
    (:func:`carve_rebuys`) and the rest open at their own class. The
    carved pieces are stamped with ``bodies_sha`` when given (the
    executor's reader of the targets' bodies), so their ledger rows carry
    the same identity any planned run's would.

    ``unit_evidence`` (:class:`UnitEvidence`, from the body ledger) makes
    every unit-writing run's units ranked by their own graded landings; None
    keeps the route-row reading (a caller with no body ledger).

    Level 0 holds every run that writes a crash-evidenced locator
    (``crash_blamed``), level 1 every other run that fills a stub which
    makes a leftover test hollow, level 2 the rest (:func:`_level`). Within a level a coverage-split (see
    ``attributed``) sorts after an attributed write — that bit precedes
    ledger rank, then the caller's `today_order`, then the run's own
    sequence number. A run goes after every run that :func:`run_blocks`
    it (a stable topological order; a cycle falls back to that sort for
    its members). A hop-pair-blocked run is then deferred this fill
    (reason ``pair_wait``), including at ``budget_minutes=0``. Ordering and
    every deferral are :func:`admit_ranked`'s, which this function calls
    with the ranking it computed: a run's own deferrals come before any
    hold, and a blocker left with no opened writer opens its first deferred
    writer.

    ``unmeasurable_tests`` / ``crash_blamed`` are optional; empty (the
    default) leaves every rank as the ledger would. A run whose every
    aimed test is unmeasurable and whose targets miss the crash-blamed
    set ranks as a flat re-buy (weight 0) even with no ledger row, so a
    positive budget defers it. ``budget_minutes=0`` still runs weight-zero
    (order only), except pair_wait / coverage-split / kind_cap /
    nothing_aimed (:func:`_aims_at_nothing`) which defer at budget 0 the
    way kind_cap already did.

    ``max_per_kind`` caps how many runs of a route kind one fill opens, in
    schedule order: the runs beyond a kind's cap are DEFERRED (reason
    ``kind_cap``, share 0) whatever the budget, and re-enter the next fill
    as never-served candidates. Sized from run `3014175-d98ed09e`'s iter-3
    fill (2026-09-08): eight packed sessions of 14–50 minutes ran back to
    back for 4 h 18 min before the file and per-fn routes opened, so the
    sweep that reads the fill arrived five hours after the previous one.
    A cap of 0 or a missing kind means unbounded.

    ``unpaid_grace`` bounds the schedule's exemptions. A run graded that
    many times in a row on the same targets and oracle without paying has
    SPENT its grace: a reverted hop pair stops ranking as never-served,
    it stops deferring the runs it blocks, and it stops sorting ahead of
    them — from then on the ledger ranks it like any run (a flat re-buy of
    identical bodies is ``rebuy_flat``). ``0`` disables the bound and
    restores the unbounded exemptions. Keeping the order past the grace
    was measured on run ``3893442-cb547388`` (2026-09-19): with the fill
    re-planning at its first premise move, the two pairs opened ahead of
    the never-served session that paid, every fill. The grace is the
    difference between "the pair goes first", which the pair_wait
    instances below establish, and "nothing else goes at all", which none
    of them does:
    on 2026-09-14 the stable cluster's ledger held 60 graded pair rows
    under one oracle, all reverted or flat, while the schedule opened
    that pair and nothing else in 20 consecutive fills. A pair's grace
    over a run that shares its leftover tests is ALSO spent when every
    shared test has had `unpaid_grace` pair tries in a row that did not
    turn it green (:func:`unpaid_tries_for_test`), so a pair that paid a
    sibling test does not re-earn the right to zero the runs of the test
    it did not move.

    ``attributed`` is the locators leftover tests convict before
    measured-owners admission. Empty (the default) skips coverage-split
    marking. A leftover-aiming run whose extra targets live in a file
    none of its attributed members occupy is a coverage-split: it sorts
    after attributed writes and is deferred this fill (reason
    ``coverage_split``) when a non-split run of the same leftovers is a
    candidate. Same-file extras on an attributed member are that file's
    installable complement, not a split. That deferral is bounded by
    `unpaid_grace` exactly as pair_wait is: a split run stops waiting once
    every leftover it shares with the non-split candidates has had
    `unpaid_grace` graded attributed writes in a row that did not turn it
    green (:func:`unpaid_attributed_writes`). Without the bound the rule
    had no release a fill could cause — the attributed write is re-planned
    every fill, so it is always a candidate — and run `11358-e5de094e`
    (2026-09-15) deferred every run holding the 27 measured owners its
    leftovers' own coverage named, three fills running, while the
    leftover whose failing assertion is printed by a unit in a third file
    kept buying the two-file pair. ``unpaid_grace=0`` restores the
    unbounded deferral.

    Service is per UNIT (:class:`UnitService`): a run the identity ledger
    has no row for is ``never_served`` only if a target was never opened
    under this oracle on any route, so re-drawing a batch cannot
    manufacture novelty.

    ``session_min_turns`` prices a session's share in its own unit: an
    opened session's share is at least that many turns at its measured
    price, the first turn and the turns after it priced apart
    (:func:`session_turn_price`), taken from the budget before
    the weighted split; floors that together exceed the budget are clipped
    to budget / sessions (:func:`_floor_clip`) and the schedule line says
    so. The clock that opens the run recomputes that same allotment against
    the time still left (:meth:`FillBudget.opening_shares`), so a floor
    that has not opened yet is not spent by an earlier run, and time an
    earlier run did not use flows forward. A session row that ran out of
    its share before that many turns is not a graded try, once per aimed
    test set (:meth:`UnitService.cut_by_share`). ``0`` restores the plain
    weighted split and the pre-floor grading.

    A session aimed only at tests whose latest session try was graded,
    unpaid and not cut by its share ranks as the re-buy of that try under
    any packing (:meth:`UnitService.session_rebuy`).
    """
    rows_list = list(rows)
    history = _latest_by_identity(rows_list)
    # "Served under another identity" is a row that OPENED the try: a run
    # the schedule deferred, a run that returned before its first dispatch
    # (EMPTY), or a run whose every reply was reasoning-capped
    # (UNMEASURED) served nothing, so its targets under a moved body or
    # oracle are still a new try, not a served one.
    units = UnitService(rows_list, oracle_fp,
                        session_min_turns=session_min_turns,
                        evidence=unit_evidence)
    blockers = Blockers.of(hollow, crash_blamed)

    # Routes whose schedule exemptions have lapsed: graded `unpaid_grace`
    # times in a row, same targets, same oracle, never paying. They keep
    # their sort position and lose the right to rank as never-tried and
    # to defer their competitors at share 0.
    spent = frozenset(
        _served_key(r) for r in runs
        if unpaid_grace > 0
        and consecutive_unpaid(rows_list, r, oracle_fp) >= unpaid_grace)

    carved: list[RouteRun] = []
    for planned in runs:
        for piece in carve_rebuys(planned, units, readmit_after=readmit_after):
            if piece.parent and bodies_sha is not None:
                piece = replace(piece, bodies_sha=bodies_sha(piece.targets))
            carved.append(piece)

    ranked: list[RankedRun] = []
    for run in carved:
        rank, reason, _deferrals = _rank_of(
            run, oracle_fp, history, units,
            readmit_after=readmit_after,
            grace_left=_served_key(run) not in spent)
        if _run_is_unmeasurable(run, unmeasurable_tests, crash_blamed):
            rank, reason = _RANK_REBUY_FLAT, _REASON[_RANK_REBUY_FLAT]
        ranked.append((run, rank, reason, _level(run, blockers)))

    return admit_ranked(
        ranked, rows_list, oracle_fp=oracle_fp, hollow=hollow,
        budget_minutes=budget_minutes, today_order=today_order,
        crash_blamed=crash_blamed, max_per_kind=max_per_kind,
        attributed=attributed, unpaid_grace=unpaid_grace,
        session_min_turns=session_min_turns, due=due, due_rungs=due_rungs)


#: One candidate as the ranking stage leaves it: the run, its rank class,
#: the reason it ranked with, and its blockers-first level (:func:`_level`).
RankedRun = tuple[RouteRun, int, str, int]


def admit_ranked(
    ranked: Sequence[RankedRun],
    rows: Iterable[RouteLedgerRow],
    *,
    oracle_fp: str,
    hollow: Mapping[str, frozenset[FnLocator]],
    budget_minutes: float = 0.0,
    today_order: Sequence[RouteKind] = TODAY_ORDER,
    crash_blamed: CrashBlame = (),
    max_per_kind: Mapping[RouteKind, int] | None = None,
    attributed: Collection[FnLocator] = (),
    unpaid_grace: int = DEFAULT_UNPAID_GRACE,
    session_min_turns: int = 0,
    due: Collection[FnLocator] = (),
    due_rungs: Mapping[FnLocator, str] | None = None,
) -> FillSchedule:
    """Order the ranked candidates, decide which open, allot the budget.

    The second half of :func:`schedule`, apart so the decision can be run on
    a recorded ranking (`tests/test_schedule_invariants.py`) and so the
    order it decides in is written once. Three stages, each deciding ONE
    question once:

    1. ORDER — blockers-first level, attributed writes before coverage
       splits, ledger rank, `today_order`, sequence; then a topological
       order over :func:`run_blocks` while a pair's grace lasts.
    2. ADMIT — one pass in that order gives every run exactly one decision:
       deferred for a reason of its own (it aims at nothing; it is a re-buy
       under a budget), waiting on runs this pass has already OPENED
       (``pair_wait``, ``coverage_split``), deferred by the per-kind cap, or
       opened. A run's own reasons are decided BEFORE it can join the opened
       set, so a run that holds another has opened for good.
    3. COVER — a blocker (a stub a leftover test is hollow on, or a
       crash-blamed locator) that some candidate writes and no opened run
       writes opens its first deferred writer in order, whatever deferred it.
       So does a ``due`` function: a red test's owed tier. Every hold here is
       right about one run and none can see that, together, they kept a red
       test's wrong body from every prompt: run `14141-9a904822`
       (2026-10-08) left `src/ensure.rs::render` in no candidate for fills
       3-4, reported-not-routed in 5-6 and deferred `pair_wait` in 7-8 while
       16 of the 21 leftovers failed through it.

    Why stage 2 is one pass with the self-deferrals inside it: the re-buy
    deferral used to run after the holds, so a re-buy could hold a run and
    then be deferred itself. Run `82925-203ed7b8` (2026-09-28, cluster
    `stable`), fills 5 and 6: the per-fn run holding `src/error.rs::context`
    — the first failure of 47-48 of the 49 failing tests — waited
    `coverage_split` on the file and impl runs of the same stub, which were
    then deferred `rebuy_reverted`. Nothing wrote the stub for 2 h 10 min;
    the fill that opened the per-fn run moved 135 -> 174. The same shape had
    been closed for pair_wait on 2026-09-18 (run `3127-6763a5c6`: fills 2-6
    opened nothing) and for executor deferrals on 2026-09-25
    (:func:`release_held`); each correction closed one door.

    Why stage 3 exists: a hollow test cannot be judged by any route until
    its stub has a body, so a fill that opens none of a blocker's writers
    spends its whole budget on tests that cannot move. Every deferral reason
    is a local judgement about one run (it is a re-buy; a pair should go
    first; a session cap) and none of them can see that it removed the last
    writer. The cover opens that writer at the smallest weight (1), after
    the deferral that removed it. The pre-change admission — holds first,
    then a weight-zero deferral — is the negative control in
    `tests/test_schedule_invariants.py`: a flat re-buy holds the other
    writer of the stub and is then deferred. A promoted run opens
    past the per-kind cap; the cap bounds how a fill is shaped, a blocker
    decides whether it can move at all. It holds no one: its waiters were
    decided in stage 2 against runs that opened there.
    """
    rows_list = list(rows)
    order_index = {k: i for i, k in enumerate(today_order)}
    attr = frozenset(attributed)
    blamed = frozenset(crash_blamed)
    split_of = {run.key: _is_coverage_split(run, attr) for run, *_ in ranked}
    # Only a pair's grace is read below (its right to go first and to hold
    # the runs it blocks); pairs are never carved, so the planned and the
    # ranked pair are the same run.
    spent = frozenset(
        _served_key(run) for run, *_ in ranked
        if run.kind is RouteKind.PAIR and unpaid_grace > 0
        and consecutive_unpaid(rows_list, run, oracle_fp) >= unpaid_grace)

    def _sort_key(item: RankedRun) -> tuple[int, int, int, int, int]:
        run, rank, _reason, level = item
        return (level, int(split_of.get(run.key, False)), rank,
                order_index.get(run.kind, len(order_index)), run.seq)

    ranked = sorted(ranked, key=_sort_key)
    _per_test: dict[tuple[str, str], int] = {}

    def _test_spent(kind: str, test: str) -> bool:
        """`unpaid_grace` tries in a row of `kind`'s writes aimed at `test`
        that did not turn it green. Cached per fill."""
        if unpaid_grace <= 0:
            return False
        key = (kind, test)
        if key not in _per_test:
            _per_test[key] = (
                unpaid_tries_for_test(
                    rows_list, test, oracle_fp,
                    match=lambda r: r.kind is RouteKind.PAIR)
                if kind == "pair" else
                unpaid_attributed_writes(rows_list, test, attr, oracle_fp))
        return _per_test[key] >= unpaid_grace

    def _grace_left(p: RouteRun, run: RouteRun) -> bool:
        return (_served_key(p) not in spent
                and not ((shared := p.target_tests & run.target_tests)
                         and all(_test_spent("pair", t) for t in shared)))

    def _orders(a: RouteRun, b: RouteRun) -> bool:
        """`a` goes before `b` in the order. A hop pair's claim to go
        first is part of its grace, not a separate rule: once the pair has
        spent it over `b` (its own graded rows, or `b`'s shared tests'
        pair tries, `unpaid_grace` in a row without paying), `b` is ranked
        against the pair by the ledger like any two runs. Run
        `3893442-cb547388` (2026-09-19) is what the unbounded order cost
        once the fill re-plans at the first premise move: both pairs, past
        their grace and returning the bodies already on disk, opened ahead
        of the never-served session that paid +3, 69 of the fill's 96
        minutes, and the fill after re-planned into the same order."""
        if not run_blocks(a, b, hollow, blamed):
            return False
        return a.kind is not RouteKind.PAIR or _grace_left(a, b)

    # Topological order over the blocks relation, condensed to its strongly
    # connected components (:func:`condensed_order`): runs that block each
    # other form one component, which is placed by its best member's sort
    # key like any single run.
    blockers_of: dict[str, tuple[str, ...]] = {
        it[0].key: tuple(
            other[0].label for other in ranked
            if other[0] is not it[0]
            and _orders(other[0], it[0]))
        for it in ranked}
    blocker_keys: dict[str, set[str]] = {
        it[0].key: {
            other[0].key for other in ranked
            if other[0] is not it[0]
            and _orders(other[0], it[0])}
        for it in ranked}
    by_key = {it[0].key: it for it in ranked}
    ordered_items = [
        (*by_key[k], blockers_of[k])
        for k in condensed_order([it[0].key for it in ranked], blocker_keys)]

    scheduled: list[ScheduledRun] = []
    for pos, (run, rank, reason, level, by) in enumerate(ordered_items):
        weight = 1 if reason == "rebuy_readmitted" else _WEIGHT[rank]
        scheduled.append(ScheduledRun(
            run=run, rank=rank, weight=weight, reason=reason, level=level,
            blocked_by=by, position=pos))

    # Every candidate a re-buy: share the budget equally rather than spin an
    # empty fill (the exhausted set's rule, one level up). Decided before
    # the pass, so the pass defers exactly the runs it will not open.
    if budget_minutes > 0 and not any(
            s.weight > 0 for s in scheduled
            if not _aims_at_nothing(s.run, blamed)):
        scheduled = [
            s if _aims_at_nothing(s.run, blamed)
            else replace(s, weight=1, reason="rebuy_readmitted")
            for s in scheduled]

    # STAGE 2 — ONE pass in schedule order decides every deferral, and a run
    # is held only by a run this pass has already OPENED. Every hold is
    # "wait for X": if X is itself deferred, waiting for it is waiting for
    # nothing, and a deferred run writes no graded row, so no grace clock
    # (`_unpaid_streak` skips deferred rows) can ever release it. Run
    # `3127-6763a5c6` (2026-09-18, cluster `stable`, hollow 142): two hop
    # pairs sharing one dispatcher each held the other and every file, impl
    # and per-fn run; fills 2-6 opened 0 runs. A run's own deferrals (it
    # aims at nothing, it is a re-buy under a budget) are decided first, so
    # a run that reaches the opened set stays there.
    #
    # A pair also does not hold a run that must land before the PAIR can
    # be judged (`run_blocks(run, pair)`): deferring it makes the pair's
    # own verdict unreachable, which is the split pair_wait exists to
    # prevent, in the other direction.
    waiting: list[ScheduledRun] = []

    # A coverage-split waits on a non-split candidate of the same
    # leftovers. The candidates are every non-split run in order; the
    # holders among them are only the ones that open (checked below).
    non_split_runs = [s.run for s in scheduled
                      if not split_of.get(s.run.key, False)]
    opened_runs: list[RouteRun] = []
    opened_per_kind: dict[RouteKind, int] = {}
    kept: list[ScheduledRun] = []
    for s in scheduled:
        run = s.run
        if _aims_at_nothing(run, blamed):
            waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                   reason="nothing_aimed"))
            continue
        if budget_minutes > 0 and s.weight <= 0:
            waiting.append(replace(s, deferred=True, share_minutes=0.0))
            continue
        blocking_pairs = [p for p in opened_runs
                          if p.kind is RouteKind.PAIR
                          and run_blocks(p, run, hollow, blamed)]
        mutual = [p for p in blocking_pairs
                  if run_blocks(run, p, hollow, blamed)]
        with_grace = [p for p in blocking_pairs if _grace_left(p, run)]
        holders = [p for p in with_grace if p not in mutual]
        if holders:
            waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                   reason="pair_wait",
                                   held_by=tuple(p.key for p in holders),
                                   prior_reason=s.reason))
            continue
        if with_grace:
            s = replace(s, mutual=tuple(p.label for p in with_grace))
        elif blocking_pairs:
            s = replace(s, lapsed=("pair_wait",))
        if attr and split_of.get(run.key, False):
            shared = frozenset().union(*(
                run.target_tests & o.target_tests for o in non_split_runs))
            if shared:
                split_holders = [
                    o for o in non_split_runs
                    if o in opened_runs and (run.target_tests & o.target_tests)]
                if split_holders and not all(_test_spent("attributed", t)
                                             for t in shared):
                    waiting.append(replace(
                        s, deferred=True, share_minutes=0.0,
                        reason="coverage_split",
                        held_by=tuple(o.key for o in split_holders),
                        prior_reason=s.reason))
                    continue
                s = replace(s, lapsed=tuple(dict.fromkeys(
                    (*s.lapsed, "coverage_split"))))
        cap = int((max_per_kind or {}).get(run.kind, 0) or 0)
        if cap > 0 and opened_per_kind.get(run.kind, 0) >= cap:
            # Keep the ranked reason: a later cover may open this run as
            # the only writer of a blocker, and that opening is not a cap.
            waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                   reason="kind_cap", prior_reason=s.reason))
            continue
        opened_per_kind[run.kind] = opened_per_kind.get(run.kind, 0) + 1
        opened_runs.append(run)
        kept.append(s)

    # STAGE 3 — every blocker a candidate writes has an opened writer, and
    # so has every function a red test owes this fill.
    kept, waiting = _cover_blockers(
        kept, waiting, Blockers.of(hollow, crash_blamed))
    kept, waiting = _cover_due(kept, waiting, frozenset(due), due_rungs or {})
    scheduled = kept

    if budget_minutes <= 0:
        return FillSchedule(tuple(scheduled), tuple(waiting), 0.0)

    if session_min_turns > 0:
        priced: dict[str, SessionTurnPrice] = {}
        for sr in scheduled:
            if sr.run.kind is RouteKind.SESSION and sr.weight > 0:
                price = session_turn_price(rows_list, sr.run)
                if price is not None:
                    priced[sr.run.key] = price
        floors = {k: p.floor(session_min_turns) for k, p in priced.items()}
        total_floor = sum(floors.values())
        clip = _floor_clip(list(floors.values()), budget_minutes)
        scheduled = [
            replace(sr, floor_minutes=(
                min(floors[sr.run.key], clip)
                if clip is not None else floors[sr.run.key]),
                floor_note=(
                    priced[sr.run.key].note(session_min_turns)
                    + (f" clipped to {clip:.0f}m (floors {total_floor:.0f}m > "
                       f"budget {budget_minutes:.0f}m)" if clip is not None
                       and floors[sr.run.key] > clip
                       else "")))
            if sr.run.key in priced else sr
            for sr in scheduled]

    ordered = _allot(list(scheduled), float(budget_minutes))
    return FillSchedule(tuple(ordered), tuple(waiting), float(budget_minutes))


def _cover_blockers(
    opened: list[ScheduledRun], deferred: list[ScheduledRun],
    blockers: Blockers,
) -> tuple[list[ScheduledRun], list[ScheduledRun]]:
    """Open, in schedule order, the first deferred writer of every blocker
    no opened run writes (stage 3 of :func:`admit_ranked`). A blocker is a
    stub a leftover test's first failure sits in (the held view), or a
    crash-blamed locator. A run WRITES a blocker only when it aims at it
    (`Blockers.writes`): a session that has it in its editable set as a
    passenger of another test's slice is not asked for that body. The
    promoted run keeps the reason it
    was ranked with, takes weight at least 1, and names the blockers it
    opened for (`blocker_writer`). A blocker no candidate writes at all is
    left for `schedule_invariants` to name."""
    blocking: set[FnLocator] = set(blockers.units)

    def _writes(s: ScheduledRun, u: FnLocator) -> bool:
        return blockers.writes(u, targets=s.run.targets,
                               aimed_tests=s.run.target_tests,
                               session=s.run.kind is RouteKind.SESSION)

    uncovered = {u for u in blocking if not any(_writes(s, u) for s in opened)}
    if not uncovered:
        return opened, deferred
    promoted: list[ScheduledRun] = []
    still: list[ScheduledRun] = []
    for s in sorted(deferred, key=lambda d: d.position):
        hit = frozenset(u for u in uncovered if _writes(s, u))
        if not hit:
            still.append(s)
            continue
        uncovered -= hit
        promoted.append(replace(
            s, deferred=False, share_minutes=None, held_by=(),
            reason=s.prior_reason or s.reason, prior_reason="",
            weight=max(s.weight, 1),
            blocker_writer=tuple(sorted(
                f"{u.file}::{u.fn_name}" for u in hit))))
    return sorted([*opened, *promoted], key=lambda s: s.position), still


def _cover_due(
    opened: list[ScheduledRun], deferred: list[ScheduledRun],
    due: frozenset[FnLocator],
    due_rungs: Mapping[FnLocator, str] | None = None,
) -> tuple[list[ScheduledRun], list[ScheduledRun]]:
    """Open, in schedule order, the first deferred writer of every owed
    function no opened run writes. The promoted run keeps the reason it was
    ranked with, takes weight at least 1 and names what it opened for
    (`starved_writer`). A function owed on the session rung
    (`due_rungs`) is covered only by a session run: its unit rung is spent,
    and another unit route would ask the question it already asked. A
    function no candidate writes at all is the driver's to put in a run (it
    is placed in the dispatched batch, so the routes plan one);
    `schedule_invariants` names it if none does."""
    if not due:
        return opened, deferred
    rungs = due_rungs or {}

    def needs_session(u: FnLocator) -> bool:
        return rungs.get(u, "unit") == "session"

    def covers(s: ScheduledRun, u: FnLocator) -> bool:
        return u in s.run.targets and (
            not needs_session(u) or s.run.kind == RouteKind.SESSION)

    uncovered = {u for u in due if not any(covers(s, u) for s in opened)}
    if not uncovered:
        return opened, deferred
    promoted: list[ScheduledRun] = []
    still: list[ScheduledRun] = []
    for s in sorted(deferred, key=lambda d: d.position):
        hit = frozenset(u for u in uncovered if covers(s, u))
        if not hit:
            still.append(s)
            continue
        uncovered -= hit
        promoted.append(replace(
            s, deferred=False, share_minutes=None, held_by=(),
            reason=s.prior_reason or s.reason, prior_reason="",
            weight=max(s.weight, 1),
            starved_writer=tuple(sorted(
                f"{u.file}::{u.fn_name}" for u in hit))))
    return sorted([*opened, *promoted], key=lambda s: s.position), still


def _floor_clip(floors: Sequence[float], budget: float) -> float | None:
    """The per-floor cap when the floors do not fit in `budget`. None when
    they do. Every floored run takes the same cap, which is what the
    schedule line reports (`clipped to Nm`)."""
    if not floors or sum(floors) <= budget:
        return None
    return budget / len(floors)


def allot_opening(runs: Sequence[ScheduledRun], budget_minutes: float
                  ) -> list[float]:
    """Each run's share of `budget_minutes`, floors reserved before the
    weighted split. The plan (:func:`schedule`) and the clock
    (:meth:`FillBudget.opening_shares`) both allot through here, so a
    later floor cannot be forgotten by a caller that only has the current
    run's weight."""
    clip = _floor_clip(
        [s.floor_minutes for s in runs if s.floor_minutes], budget_minutes)
    adjusted = [
        replace(s, floor_minutes=min(s.floor_minutes, clip))
        if clip is not None and s.floor_minutes and s.floor_minutes > clip
        else s
        for s in runs
    ]
    return [s.share_minutes or 0.0
            for s in _allot(list(adjusted), float(budget_minutes))]


class FillBudget:
    """The fill's wall clock. `remaining()` is None when unbounded."""

    def __init__(self, budget_minutes: float, *,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._budget = float(budget_minutes)
        self._now = now
        self._started = now()

    @property
    def bounded(self) -> bool:
        return self._budget > 0

    def elapsed(self) -> float:
        return (self._now() - self._started) / 60.0

    def remaining(self) -> float | None:
        if not self.bounded:
            return None
        return max(0.0, self._budget - self.elapsed())

    def opening_shares(self, ahead: Sequence[ScheduledRun]
                       ) -> list[float] | list[None]:
        """Shares for `ahead` (the wave opening now, then every run not yet
        opened), from the time still left. Floors of runs not yet opened are
        reserved; time an earlier run did not use is in `remaining` and
        flows forward. None when the fill is unbounded."""
        rem = self.remaining()
        if rem is None:
            return [None] * len(ahead)
        return allot_opening(ahead, rem)


#: Real seconds a claim waiting on a reserved share sleeps between re-reads
#: of the clock when no sibling returns: the wait ends on a release, or on
#: the share's own minutes, whichever comes first.
_RESERVED_WAIT_POLL_S = 5.0


class RunClock:
    """One run's claim gate. It never interrupts: a unit, a turn or a session
    already claimed runs to completion; the clock only answers whether the
    NEXT claim may be made.

    ``ground_moved`` is the fill's premise gate (`fill_scheduler`), asked
    with the claim's scope: True once a keep in this fill changed what THAT
    claim was planned on — a test's verdict, where it first fails, or
    whether its process dies. Every route asks this clock before each
    claim, so the refusal lands at the next unit boundary of whichever
    route is running (run `3383938-c5d24499`, 2026-09-19: the first keep
    moved all 141 failing tests off `construct`'s stub and the rest of the
    fill ran a plan in which `vtable` had no run). ``scope`` is the run's
    target tests; a claim with no scope stands on the whole ground.
    ``units`` is what the run writes: a moved claim still stands when the
    ground now stops one of its planned tests inside them.

    A granted claim RESERVES one call of the run's kind (``call_price``
    minutes, :func:`call_price`) until its unit returns, and the share check
    counts what is reserved: ``elapsed + outstanding < share``. Elapsed is
    wall time and a grant does not move it, so a parallel wave's workers all
    passed ``elapsed < share`` before any of them had spent a call (run
    `450922-661c5e9f` fill 11: a 0.3-minute per-fn share granted four claims
    and ran 31.9 minutes). The check and the grant are one step under a
    lock. A reservation belongs to the claiming thread: it is released when
    that thread's unit returns (:meth:`release`) or replaced by the thread's
    next claim, since its unit is over by then — so a serial route's turns
    and blocks see exactly the old rule. With no price (no graded row of the
    kind) a grant reserves the rest of the share: one call, no sibling.

    A share that is RESERVED is not a share that is SPENT. When the only
    reason not to grant is other threads' calls in flight, and elapsed is
    still below the share, the claim waits for one of them to return
    (:meth:`release` notifies) and is asked again, premise included. It
    ends granted, refused on premise, or refused ``share`` once elapsed
    reaches the share. Run `2157238-210637e6` (2026-10-11) is the drop this
    replaces: fill 1's per-fn workers each got ``share`` from the first
    worker's no-price reservation and emptied the queue in a millisecond,
    one call in 2.2 of 13.3 minutes and 18 units dropped as "the run's share
    is spent". A thread never waits on itself (asking again ends its own
    reservation), so a serial route cannot wait.

    ``owed`` is the held stubs this run was given to write (the fill's held
    view intersected with the run's targets). A claim that names units, all
    of them owed, is granted once the share is SPENT and the fill still has
    budget left: the share bounds the targets a run may skip, and a held
    stub stops red tests whatever else is written. The exemption is not a
    reservation. While elapsed is still below the share and another thread
    holds one, an owed claim waits with every other claim and is asked
    again, premise included. Same run, fill 2: the share spent on four
    `From` impls left `src/context.rs::context` and `with_context`, holding
    ten red tests, unclaimed. A claim with no units, or with any unit not
    owed, stays on the share."""

    def __init__(self, *, share_minutes: float | None,
                 fill_remaining: Callable[[], float | None],
                 now: Callable[[], float] = time.monotonic,
                 ground_moved: Callable[
                     [frozenset[str] | None, frozenset[FnLocator]], bool,
                 ] | None = None,
                 scope: frozenset[str] | None = None,
                 units: frozenset[FnLocator] = frozenset(),
                 unit_multiple: float = 0.0,
                 call_price: float | None = None,
                 owed: frozenset[FnLocator] = frozenset()) -> None:
        self.share_minutes = share_minutes
        #: Minutes one call of this run's kind takes (:func:`call_price`);
        #: what a granted claim reserves while its unit is in flight.
        self.call_price = call_price
        #: The held stubs this run was given to write (see the class doc).
        self.owed = frozenset(owed)
        self._lock = threading.Lock()
        #: Notified on every release, so a claim waiting on a reserved share
        #: re-asks as soon as a sibling's call returns.
        self._returned = threading.Condition(self._lock)
        self._reserved: dict[int, float] = {}
        #: Each claimed unit's own bound, as a multiple of the share
        #: (`FILL_UNIT_SHARE_MULTIPLE`; :mod:`unit_clock`). 0 = unbounded.
        self.unit_multiple = unit_multiple
        self._fill_remaining = fill_remaining
        self._ground_moved = ground_moved
        #: The tests the run was planned on (`RouteRun.target_tests`).
        self.scope = scope
        #: The units the run writes (`RouteRun.targets`).
        self.units = units
        self._now = now
        self._started = now()
        self.last_delta = 0
        self.claims = 0
        self.refused = 0

    def elapsed(self) -> float:
        return (self._now() - self._started) / 60.0

    def remaining(self) -> float | None:
        if self.share_minutes is None:
            return None
        return max(0.0, self.share_minutes - self.elapsed())

    def note_delta(self, delta: int) -> None:
        """What the run's last landed change set bought (net verdicts)."""
        self.last_delta = int(delta)

    def outstanding(self) -> float:
        """Minutes reserved by granted claims whose unit has not returned."""
        with self._lock:
            return sum(self._reserved.values())

    def release(self) -> None:
        """The calling thread's claimed unit returned: its reservation ends,
        and its minutes are in `elapsed()` from here on."""
        with self._lock:
            self._reserved.pop(threading.get_ident(), None)
            self._returned.notify_all()

    def may_claim(self, tests: Iterable[str] | None = None,
                  units: Iterable[FnLocator] | None = None) -> bool:
        """True when the next claim may be made (:meth:`refusal` is "")."""
        return not self.refusal(tests, units)

    def refusal(self, tests: Iterable[str] | None = None,
                units: Iterable[FnLocator] | None = None) -> str:
        """Make the next claim, or say why not: ``""`` when claimed,
        ``"premise"`` when the ground this claim was planned on has moved,
        ``"share"`` when the share is spent and the last claim bought
        nothing. Budget follows payoff: within the share, always; past a
        spent share while the last claim cashed, or while the claim names
        only owed units, and the fill has budget left. An owed claim does
        not skip the wait: a share another thread has only reserved is not
        spent.
        Never once the ground this claim was planned on has moved:
        ``tests`` when the route names the unit's own tests (a pooled
        per-fn unit), else the run's scope; with neither, any move refuses.

        One answer per call, so a parallel wave's workers each get the
        reason for their own claim. The per-fn route names its unclaimed
        units under each reason: one line for both could not say whether
        run `484518-91681c1a` fill 4 refused `src/ptr.rs::new` because a
        keep had just moved its tests onto it, or because the share ran out."""
        scope = frozenset(tests) if tests is not None else self.scope
        claimed = frozenset(units) if units is not None else self.units
        owed_claim = (units is not None and bool(claimed)
                      and claimed <= self.owed)
        me = threading.get_ident()
        with self._lock:
            # This thread asking again means its previous unit is over.
            if self._reserved.pop(me, None) is not None:
                self._returned.notify_all()
        while True:
            if (self._ground_moved is not None
                    and self._ground_moved(scope, claimed)):
                with self._lock:
                    self.refused += 1
                return "premise"
            with self._lock:
                if self.share_minutes is None:
                    return self._claimed(scope, claimed)
                elapsed = self.elapsed()
                outstanding = sum(self._reserved.values())
                if elapsed + outstanding < self.share_minutes:
                    self._reserved[me] = (
                        self.call_price if self.call_price is not None
                        else max(0.0, self.share_minutes - elapsed))
                    return self._claimed(scope, claimed)
                fill_left = self._fill_remaining()
                fill_open = fill_left is None or fill_left > 0
                if fill_open and self.last_delta > 0:
                    # Budget follows payoff past the share. An owed claim
                    # is not this branch: a reservation is not a spent share.
                    self._reserved[me] = self.call_price or 0.0
                    return self._claimed(scope, claimed)
                if elapsed < self.share_minutes and outstanding > 0:
                    # Reserved, not spent: wait for a sibling's call to
                    # return, then ask again from the premise. An owed
                    # claim waits here too. Bounded by the share's own
                    # minutes; the poll re-reads `now` for a clock that
                    # advances without a release.
                    self._returned.wait(timeout=min(
                        _RESERVED_WAIT_POLL_S,
                        (self.share_minutes - elapsed) * 60.0))
                    continue
                if (fill_open and owed_claim
                        and elapsed >= self.share_minutes):
                    # The share is spent and the fill still has time. A
                    # no-price grant reserves nothing, so the owed units of
                    # one wave start together once the share is actually spent.
                    self._reserved[me] = self.call_price or 0.0
                    return self._claimed(scope, claimed)
                self.refused += 1
                return "share"

    def _claimed(self, scope: frozenset[str] | None,
                 claimed: frozenset[FnLocator]) -> str:
        """Count the claim and start the claiming thread's unit clock: the
        unit may run until ``unit_multiple`` × the share, then ends at its
        next attempt boundary (:mod:`unit_clock`).

        The unit keeps the ground question of THIS claim for each later
        call it buys: on the tests of the names that call asks when the
        route gives them, else on the claim's scope; always on the claim's
        units, which is the standing-in set. The share is not asked again:
        it is a between-units rule, and the unit's own bound is the
        multiple above."""
        self.claims += 1
        gate = self._ground_moved

        def _ground(tests: Iterable[str] | None = None) -> bool:
            assert gate is not None
            return gate(scope if tests is None else frozenset(tests), claimed)

        unit_clock.start(
            self.share_minutes * self.unit_multiple
            if self.share_minutes and self.unit_multiple > 0 else None,
            now=self._now, ground=None if gate is None else _ground)
        return ""
