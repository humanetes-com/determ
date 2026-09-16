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
  that transaction (run ``675471-0f3105f2``, 2026-09-10: the pair sat at
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
pair_wait, coverage-split and kind_cap still defer at budget 0.

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
    "DEFAULT_UNPAID_GRACE",
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
    "consecutive_unpaid",
    "hollow_by_test",
    "rebuy_identity",
    "run_blocks",
    "schedule",
    "unpaid_pair_buys",
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
               hollow: Mapping[str, frozenset[FnLocator]],
               crash_blamed: Collection[FnLocator] = ()) -> bool:
    """True when `a` must land before `b` can be judged.

    This is ORDER: it says the pair goes first, and that claim has no
    expiry — a two-file transaction written after one of its halves has
    been peeled apart is the split every instance below records. What
    DOES expire is the share-0 deferral built on top of it; see
    ``unpaid_grace`` in :func:`schedule`. Read the two together: past the
    grace the pair still sorts first here, and the runs behind it stop
    being zeroed.

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

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.run.to_dict(), "rank": self.rank, "weight": self.weight,
            "reason": self.reason, "level": self.level,
            "share_minutes": (round(self.share_minutes, 2)
                              if self.share_minutes is not None else None),
            "deferred": self.deferred, "blocked_by": list(self.blocked_by),
            "lapsed": list(self.lapsed),
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
            if s.lapsed:
                bits.append("grace spent: " + ", ".join(s.lapsed))
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


def _rank_of(run: RouteRun, oracle_fp: str,
             history: Mapping[str, list[RouteLedgerRow]],
             served_targets: Collection[str],
             *, readmit_after: int, grace_left: bool = True,
             ) -> tuple[int, str, int]:
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
    if run.kind is RouteKind.PAIR and not grace_left:
        # Past the grace: SERVED — honest about a history the ledger has
        # dozens of rows for, and still a non-zero weight. Not
        # never-served, which is what the exemption below was answering;
        # not a zero-weight re-buy either, because a DEFERRED pair is how
        # the two-file transaction gets split, which is the defect that
        # exemption was written against. What lapses instead is the
        # share-0 deferral of every competing run — see `unpaid_grace`.
        # Before `last is None`, because the identity carries the bodies
        # sha and a pair that keeps any body at all arrives with no row
        # on its own identity: that is the state this bound exists for.
        return _RANK_SERVED, "grace_spent", deferrals
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
    if (last.outcome is RunOutcome.REVERTED and run.kind is RouteKind.PAIR
            and grace_left):
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
    attributed: Collection[FnLocator] = (),
    unpaid_grace: int = DEFAULT_UNPAID_GRACE,
) -> FillSchedule:
    """Order the candidate runs and allot the budget. Pure; deterministic.

    Level 0 holds every run that fills a stub which makes a leftover test
    hollow; level 1 the rest. Within a level a coverage-split (see
    ``attributed``) sorts after an attributed write — that bit precedes
    ledger rank, then the caller's `today_order`, then the run's own
    sequence number. A run goes after every run that :func:`run_blocks`
    it (a stable topological order; a cycle falls back to that sort for
    its members). A hop-pair-blocked run is then deferred this fill
    (reason ``pair_wait``), including at ``budget_minutes=0``.

    ``unmeasurable_tests`` / ``crash_blamed`` are optional; empty (the
    default) leaves every rank as the ledger would. A run whose every
    aimed test is unmeasurable and whose targets miss the crash-blamed
    set ranks as a flat re-buy (weight 0) even with no ledger row, so a
    positive budget defers it. ``budget_minutes=0`` still runs weight-zero
    (order only), except pair_wait / coverage-split / kind_cap which
    defer at budget 0 the way kind_cap already did.

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
    and it stops deferring the runs it blocks — it still sorts first, they
    queue behind it with a share instead of zero. ``0`` disables the
    bound and restores the unbounded exemptions. This is the difference
    between "the pair goes first", which the pair_wait instances below
    establish, and "nothing else goes at all", which none of them does:
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
    attr = frozenset(attributed)
    split_of: dict[str, bool] = {}
    blamed = frozenset(crash_blamed)

    # Routes whose schedule exemptions have lapsed: graded `unpaid_grace`
    # times in a row, same targets, same oracle, never paying. They keep
    # their sort position and lose the right to rank as never-tried and
    # to defer their competitors at share 0.
    spent = frozenset(
        _served_key(r) for r in runs
        if unpaid_grace > 0
        and consecutive_unpaid(rows_list, r, oracle_fp) >= unpaid_grace)

    ranked: list[tuple[RouteRun, int, str, int, int]] = []
    for run in runs:
        rank, reason, deferrals = _rank_of(
            run, oracle_fp, history, served_targets,
            readmit_after=readmit_after,
            grace_left=_served_key(run) not in spent)
        if _run_is_unmeasurable(run, unmeasurable_tests, crash_blamed):
            rank, reason = _RANK_REBUY_FLAT, _REASON[_RANK_REBUY_FLAT]
        level = 0 if (_blocking_stubs(run, hollow)
                      or (run.targets & blamed)) else 1
        split_of[run.key] = _is_coverage_split(run, attr)
        ranked.append((run, rank, reason, deferrals, level))

    def _sort_key(item: tuple[RouteRun, int, str, int, int]
                  ) -> tuple[int, int, int, int, int]:
        run, rank, _reason, _d, level = item
        return (level, int(split_of.get(run.key, False)), rank,
                order_index.get(run.kind, len(order_index)), run.seq)

    ranked.sort(key=_sort_key)

    # Stable topological order over ALL runs: at each step the first run in
    # sort order whose every blocker is already placed. A cycle (two runs
    # each holding a stub the other's tests need) falls back to sort order.
    blockers_of: dict[str, tuple[str, ...]] = {
        it[0].key: tuple(
            other[0].label for other in ranked
            if other[0] is not it[0]
            and run_blocks(other[0], it[0], hollow, blamed))
        for it in ranked}
    blocker_keys: dict[str, set[str]] = {
        it[0].key: {
            other[0].key for other in ranked
            if other[0] is not it[0]
            and run_blocks(other[0], it[0], hollow, blamed)}
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

    # Pair-wait: a hop pair is this fill of its leftovers. A run it
    # blocks is deferred (reason pair_wait, share 0), not queued with a
    # share — the 12120-78b991d3 tail. Same path kind_cap already uses;
    # also at budget 0 so order-only still defers the tail. Resolve by
    # kind PAIR on the candidate runs, not by matching blocked_by labels.
    # A stub run that hollows a leftover still opens (blockers-first;
    # run_blocks already refuses to wait that run).
    #
    # A pair that has SPENT its grace keeps its place in the order above
    # (run_blocks is unchanged, so the two-file transaction is still
    # written before anything that could peel it) and stops zeroing the
    # rest: they queue behind it with a share. The instances this rule
    # was written for all establish which run goes FIRST; none of them
    # establishes a share of zero for every competitor for as long as the
    # pair keeps failing. On 2026-09-14 that difference was the cluster's
    # whole fill phase — the stable ledger held 36 consecutive graded,
    # never-paying rows for this pair's targets while the schedule opened
    # it and nothing else in 20 straight fills.
    waiting: list[ScheduledRun] = []
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

    pair_runs = [it[0] for it in ranked if it[0].kind is RouteKind.PAIR]
    if pair_runs:
        kept_pair: list[ScheduledRun] = []
        for s in scheduled:
            blockers = [p for p in pair_runs
                        if run_blocks(p, s.run, hollow, blamed)]
            holding = [
                p for p in blockers
                if _served_key(p) not in spent
                and not ((shared := p.target_tests & s.run.target_tests)
                         and all(_test_spent("pair", t) for t in shared))]
            if holding:
                waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                       reason="pair_wait"))
                continue
            kept_pair.append(replace(s, lapsed=("pair_wait",))
                             if blockers else s)
        scheduled = kept_pair

    # Coverage-split deferral: a leftover-aiming run that writes coverage
    # locators not in the attributed snapshot waits when a non-split write
    # of the same leftovers is still a candidate. Empty attributed skips
    # this entirely.
    if attr:
        kept_split: list[ScheduledRun] = []
        non_split = [s for s in scheduled if not split_of.get(s.run.key, False)]
        for s in scheduled:
            if not split_of.get(s.run.key, False):
                kept_split.append(s)
                continue
            shared = frozenset().union(*(
                s.run.target_tests & o.run.target_tests for o in non_split))
            if not shared:
                kept_split.append(s)
                continue
            if not all(_test_spent("attributed", t) for t in shared):
                waiting.append(replace(s, deferred=True, share_minutes=0.0,
                                       reason="coverage_split"))
                continue
            kept_split.append(replace(
                s, lapsed=tuple(dict.fromkeys((*s.lapsed, "coverage_split")))))
        scheduled = kept_split

    # The per-kind cap, applied in schedule order so the runs the order put
    # first are the ones that open; the rest are deferred whatever the
    # budget says and are never-served candidates of the next fill.
    capped: list[ScheduledRun] = list(waiting)
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
