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
  after that run.
- never-served before re-buys — a run with no ledger row on its identity (the
  route, the targets, the targets' bodies, the oracle, and for a session the
  tests it aims at) ranks ahead of one whose last identical try moved no
  verdict, which ranks ahead of one whose last identical try was reverted. A
  run whose last identical try PAID keeps the never-served rank. The service
  order applies the same rule to units.
- budget follows payoff — shares are weights over the fill budget; a zero
  weight (a re-buy) is DEFERRED rather than run, unless every candidate is a
  re-buy or the run has been deferred `readmit_after` times already (the
  bounded re-admission the exhausted set already gets at the unit level).
  A run that cashed on its last turn may extend past its share into the
  fill's unspent budget; a run that produced nothing in its share stops
  claiming new units. No unit, turn or session in flight is interrupted.

`budget_minutes = 0` means ORDER ONLY: no shares, nothing deferred.

Deliberately not here: any route's own preconditions (what a session may
edit, which blocks the impl route covers), the keep contract applied to a
run's landed change set, or the process-level fill-phase stop — those are
the routes', the gate's and the graceful-stop clock's respectively.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pipeline_core.domain.models import FnLocator, FnSite

__all__ = [
    "TODAY_ORDER",
    "FillBudget",
    "FillSchedule",
    "RouteKind",
    "RouteLedgerRow",
    "RouteRun",
    "RunClock",
    "RunOutcome",
    "ScheduledRun",
    "bodies_sha_of",
    "hollow_by_test",
    "rebuy_identity",
    "run_blocks",
    "schedule",
]


class RouteKind(StrEnum):
    SESSION = "session"
    PAIR = "pair"
    FILE = "file"
    IMPL = "impl"
    PER_FN = "per-fn"


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


def hollow_by_test(
    slices: Mapping[str, Collection[FnSite]],
    stubs: Collection[FnLocator],
) -> dict[str, frozenset[FnLocator]]:
    """For every leftover test, the still-hollow units on its executed path.

    A test is HOLLOW when any site of its slice shares a dispatch name with a
    stub — the same predicate the session planner drops hollow tests on
    (`slice_session._stub_blocks`), restated here so the schedule and the
    planner cannot disagree about which tests are blocked."""
    stub_set = frozenset(stubs)
    out: dict[str, frozenset[FnLocator]] = {}
    for test, sites in slices.items():
        blocking = frozenset(
            FnLocator(s.file, s.fn_name) for s in sites
            if FnLocator(s.file, s.fn_name) in stub_set)
        if blocking:
            out[test] = blocking
    return out


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
               hollow: Mapping[str, frozenset[FnLocator]]) -> bool:
    """True when `a` must land before `b` can be judged.

    A hop pair is the two-file transaction for its members. A session that
    can write either member (or that aims at the same leftover tests)
    waits: run ``19980-f9ea6adb`` (2026-09-10) opened the overlapping
    session first, wrote both halves plus a hanging companion, peeled the
    set apart, and ``PairRoute`` would have skipped once a session had
    served either member. The pre-schedule walk still puts SESSION before
    PAIR; this rule is the schedule's.

    Otherwise: `b`'s own tests are hollow because of a stub `a` holds and
    `b` does not."""
    if a is b:
        return False
    if (a.kind is RouteKind.PAIR and b.kind is RouteKind.SESSION
            and (a.targets & b.targets or a.target_tests & b.target_tests)):
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
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


# Rank classes, in the order they dispatch within one blocking level.
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

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.run.to_dict(), "rank": self.rank, "weight": self.weight,
            "reason": self.reason, "level": self.level,
            "share_minutes": (round(self.share_minutes, 2)
                              if self.share_minutes is not None else None),
            "deferred": self.deferred, "blocked_by": list(self.blocked_by),
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
            if s.blocked_by:
                bits.append("after " + ", ".join(s.blocked_by))
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


def _rank_of(run: RouteRun, oracle_fp: str,
             history: Mapping[str, list[RouteLedgerRow]],
             served_targets: Collection[str],
             *, readmit_after: int) -> tuple[int, str, int]:
    """(rank, reason, deferrals) for one run against the ledger.

    `served_targets` holds the (kind, targets) keys the ledger has a row
    that OPENED for (any outcome but deferred or empty) — the "served,
    but under another identity" class."""
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
    if last is None:
        # A row on the same route and targets under ANOTHER identity (the
        # bodies or the oracle moved since): served, but this try is new.
        if _served_key(run) in served_targets:
            return _RANK_SERVED, _REASON[_RANK_SERVED], deferrals
        return _RANK_NEVER_SERVED, _REASON[_RANK_NEVER_SERVED], deferrals
    if last.outcome is RunOutcome.CASHED or last.net_pass_delta > 0:
        return _RANK_PAYING, _REASON[_RANK_PAYING], deferrals
    if last.outcome in (RunOutcome.STOPPED, RunOutcome.UNMEASURED):
        # Interrupted, or every reply reasoning-capped: not evidence.
        return _RANK_NEVER_SERVED, _REASON[_RANK_NEVER_SERVED], deferrals
    if last.outcome is RunOutcome.REVERTED and run.kind is RouteKind.PAIR:
        # The pair's revert stores a diagnosis the next prompt is
        # supposed to carry. Ranking it as a zero-weight rebuy defers
        # that retry under a positive budget, so the next fill opens
        # the overlapping session instead and splits the transaction
        # (run 200802-b835274e, 2026-09-10: fill 1 pair reverted, fills
        # 2 and 3 never opened it). A pair FLAT is still a rebuy — the
        # bodies landed and the leftovers stayed red.
        return _RANK_NEVER_SERVED, _REASON[_RANK_NEVER_SERVED], deferrals
    rank = (_RANK_REBUY_REVERTED if last.outcome is RunOutcome.REVERTED
            else _RANK_REBUY_FLAT)
    if deferrals >= readmit_after:
        return rank, "rebuy_readmitted", deferrals
    return rank, _REASON[rank], deferrals


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


def _run_is_unmeasurable(
    run: RouteRun,
    unmeasurable_tests: Collection[str],
    crash_blamed: Collection[FnLocator],
) -> bool:
    """True when every aimed test is one whose binary did not complete,
    and no target is the crash-blamed locator that can complete it.

    Empty ``target_tests`` is not this class: the run named no test, so it
    cannot be "every name is unmeasurable." Empty ``unmeasurable_tests``
    (the default) preserves every existing rank. A crash-blamed target
    keeps today's rank even when the aimed tests are all unmeasurable —
    that unit is the one whose body can make the binary complete.
    """
    if not run.target_tests or not unmeasurable_tests:
        return False
    if crash_blamed and run.targets & frozenset(crash_blamed):
        return False
    return run.target_tests <= frozenset(unmeasurable_tests)


run_is_unmeasurable = _run_is_unmeasurable


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
    crash_blamed: Collection[FnLocator] = (),
    max_per_kind: Mapping[RouteKind, int] | None = None,
) -> FillSchedule:
    """Order the candidate runs and allot the budget. Pure; deterministic.

    Level 0 holds every run that fills a stub which makes a leftover test
    hollow; level 1 the rest. Within a level a run goes after every run that
    :func:`run_blocks` it (a stable topological order; a cycle falls back to
    rank order for its members). Ties break on rank class, then the
    caller's `today_order`, then the run's own sequence number.

    ``unmeasurable_tests`` / ``crash_blamed`` are optional; empty (the
    default) leaves every rank as the ledger would. A run whose every
    aimed test is unmeasurable and whose targets miss the crash-blamed
    set ranks as a flat re-buy (weight 0) even with no ledger row, so a
    positive budget defers it. ``budget_minutes=0`` still runs weight-zero
    (order only).

    ``max_per_kind`` caps how many runs of a route kind one fill opens, in
    schedule order: the runs beyond a kind's cap are DEFERRED (reason
    ``kind_cap``, share 0) whatever the budget, and re-enter the next fill
    as never-served candidates. Sized from run `3014175-d98ed09e`'s iter-3
    fill (2026-09-08): eight packed sessions of 14–50 minutes ran back to
    back for 4 h 18 min before the file and per-fn routes opened, so the
    sweep that reads the fill arrived five hours after the previous one.
    A cap of 0 or a missing kind means unbounded.
    """
    rows_list = list(rows)
    history = _latest_by_identity(rows_list)
    # "Served under another identity" is a row that OPENED the try: a run
    # the schedule deferred, a run that returned before its first dispatch
    # (EMPTY), or a run whose every reply was reasoning-capped
    # (UNMEASURED) served nothing, so its targets under a moved body or
    # oracle are still a new try, not a served one.
    served_targets = frozenset(
        _served_key_of(r) for r in rows_list
        if r.outcome not in (RunOutcome.DEFERRED, RunOutcome.EMPTY,
                             RunOutcome.UNMEASURED))
    order_index = {k: i for i, k in enumerate(today_order)}

    ranked: list[tuple[RouteRun, int, str, int, int]] = []
    for run in runs:
        rank, reason, deferrals = _rank_of(
            run, oracle_fp, history, served_targets, readmit_after=readmit_after)
        if _run_is_unmeasurable(run, unmeasurable_tests, crash_blamed):
            rank, reason = _RANK_REBUY_FLAT, _REASON[_RANK_REBUY_FLAT]
        level = 0 if _blocking_stubs(run, hollow) else 1
        ranked.append((run, rank, reason, deferrals, level))

    def _sort_key(item: tuple[RouteRun, int, str, int, int]) -> tuple[int, int, int, int]:
        run, rank, _reason, _d, level = item
        return (level, rank, order_index.get(run.kind, len(order_index)), run.seq)

    ranked.sort(key=_sort_key)

    # Stable topological order over ALL runs: at each step the first run in
    # sort order whose every blocker is already placed. A cycle (two runs
    # each holding a stub the other's tests need) falls back to sort order.
    blockers_of: dict[str, tuple[str, ...]] = {
        it[0].key: tuple(
            other[0].label for other in ranked
            if other[0] is not it[0] and run_blocks(other[0], it[0], hollow))
        for it in ranked}
    blocker_keys: dict[str, set[str]] = {
        it[0].key: {
            other[0].key for other in ranked
            if other[0] is not it[0] and run_blocks(other[0], it[0], hollow)}
        for it in ranked}
    ordered_items: list[tuple[RouteRun, int, str, int, int, tuple[str, ...]]] = []
    pending = list(ranked)
    placed: set[str] = set()
    while pending:
        pick = next((it for it in pending
                     if not (blocker_keys[it[0].key] - placed)), None)
        if pick is None:
            pick = pending[0]
        ordered_items.append((*pick, blockers_of[pick[0].key]))
        placed.add(pick[0].key)
        pending.remove(pick)

    scheduled: list[ScheduledRun] = []
    for run, rank, reason, _d, level, by in ordered_items:
        weight = 1 if reason == "rebuy_readmitted" else _WEIGHT[rank]
        scheduled.append(ScheduledRun(
            run=run, rank=rank, weight=weight, reason=reason, level=level,
            blocked_by=by))

    # The per-kind cap, applied in schedule order so the runs the order put
    # first are the ones that open; the rest are deferred whatever the
    # budget says and are never-served candidates of the next fill.
    capped: list[ScheduledRun] = []
    if max_per_kind:
        opened: dict[RouteKind, int] = {}
        kept_sched: list[ScheduledRun] = []
        for s in scheduled:
            cap = int(max_per_kind.get(s.run.kind, 0) or 0)
            if cap > 0 and opened.get(s.run.kind, 0) >= cap:
                capped.append(replace(s, deferred=True, share_minutes=0.0,
                                      reason="kind_cap"))
                continue
            opened[s.run.kind] = opened.get(s.run.kind, 0) + 1
            kept_sched.append(s)
        scheduled = kept_sched

    if budget_minutes <= 0:
        return FillSchedule(tuple(scheduled), tuple(capped), 0.0)

    runnable = [s for s in scheduled if s.weight > 0]
    if not runnable:
        # Every candidate is a re-buy: share the budget equally rather than
        # spin an empty fill (the exhausted set's rule, one level up).
        scheduled = [replace(s, weight=1, reason="rebuy_readmitted")
                     for s in scheduled]
        runnable = list(scheduled)
    total_w = sum(s.weight for s in runnable)
    ordered: list[ScheduledRun] = []
    deferred: list[ScheduledRun] = list(capped)
    for s in scheduled:
        if s.weight <= 0:
            deferred.append(replace(s, deferred=True, share_minutes=0.0))
            continue
        ordered.append(replace(
            s, share_minutes=budget_minutes * s.weight / total_w))
    return FillSchedule(tuple(ordered), tuple(deferred), float(budget_minutes))


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

    def share_for(self, weight: int, remaining_weight: int) -> float | None:
        """The share a run may claim when it opens: the UNSPENT budget over
        the weights of the runs not yet opened, so an early finish flows to
        the next run and an overrun is paid by the ones after it."""
        rem = self.remaining()
        if rem is None:
            return None
        if remaining_weight <= 0 or weight <= 0:
            return 0.0
        return rem * weight / remaining_weight


class RunClock:
    """One run's claim gate. It never interrupts: a unit, a turn or a session
    already claimed runs to completion; the clock only answers whether the
    NEXT claim may be made."""

    def __init__(self, *, share_minutes: float | None,
                 fill_remaining: Callable[[], float | None],
                 now: Callable[[], float] = time.monotonic) -> None:
        self.share_minutes = share_minutes
        self._fill_remaining = fill_remaining
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

    def may_claim(self) -> bool:
        """Budget follows payoff: within the share, always; past it only
        while the last claim cashed and the fill has budget left."""
        if self.share_minutes is None:
            self.claims += 1
            return True
        if self.elapsed() < self.share_minutes:
            self.claims += 1
            return True
        fill_left = self._fill_remaining()
        if self.last_delta > 0 and (fill_left is None or fill_left > 0):
            self.claims += 1
            return True
        self.refused += 1
        return False
