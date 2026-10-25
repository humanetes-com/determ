"""Walk: the owner-map admission is reachable once the cohort is served.

Run `9872-51b20f30` (2026-09-01, cluster `stable`) held `pass=134 fail=3`
across four sweeps with `never_served=[]` from iter 1 and ZERO
`phase_c.attribution.measured_owners` emissions: the shipped fire demanded
latest-grade unanimity over a cohort whose grade writers could never supply
it (proven-from-siblings mints, masked flats, forever-reverting units), while
the coverage owner map named a measured owner of all three failing tests
that no run had ever dispatched. The service fire keys on the premise the
futility escalation already uses — every cohort unit RETURNED from a fill —
which no grade class can hold.

Replayed on the distilled fixture of that run's sweeps. Reachability only:
the admitted owner reaches a dispatched batch; no claim about the score.
Negative control: the same replay under the unanimity premise (served =
graded) reproduces the recorded zero-emission shape — the owner reaches no
batch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureKind,
    FormatterPair,
)
from pipeline_core.domain.coverage_slice import CoverageSlice, bind
from pipeline_core.domain.fill_schedule import (
    DEFAULT_UNPAID_GRACE,
    RouteKind,
    RouteLedgerRow,
    RunOutcome,
)
from pipeline_core.domain.models import ClusterFillResult, FnLocator, FnSite
from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk

# The three failing tests of the 134/137 sweeps, as the harness names them.
_REDS = ("t135", "t136", "t137")
# Attributed accessors (the walk's stand-ins for the Debug-test accessor
# cohort) and the measured owner the map names on all three tests, which no
# attribution path ever produced.
_ACC = FnLocator("src/a.rs", "fmt")
_ACC2 = FnLocator("src/a.rs", "context")
_OWNER = FnLocator("src/b.rs", "owner_fmt")

_ACC_SITE = FnSite("src/a.rs", "fmt", "impl Debug for Error")
_ACC2_SITE = FnSite("src/a.rs", "context", "impl Error")
_OWNER_SITE = FnSite("src/b.rs", "owner_fmt", "impl Debug for Error")

_OWNERS = {t: frozenset({_ACC_SITE, _ACC2_SITE, _OWNER_SITE}) for t in _REDS}


def _coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
    return CoverageSlice(owners=dict(_OWNERS), failing=frozenset(failing_tests))


def _replay(monkeypatch, tmp_path: Path):
    walk = load_walk("9872-51b20f30")
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(
            walk, monkeypatch, tmp_path, events=events,
            frontier={_ACC: set(_REDS), _ACC2: set(_REDS)},
            fill=_fill, coverage=_coverage)
    finally:
        bind(None)
    return logs, events, batches


def test_a_served_cohort_admits_the_measured_owner_into_a_batch(
        monkeypatch, tmp_path: Path) -> None:
    logs, events, batches = _replay(monkeypatch, tmp_path)
    admits = [d for e, d in events
              if e == "phase_c.attribution.measured_owners"]
    assert admits, "the admission must fire once the cohort has been served"
    assert "src/b.rs::owner_fmt" in admits[0]["added"]
    assert any(_OWNER in b for b in batches), (
        "the admitted owner must reach a dispatched batch")
    assert any("measured-owners:" in ln for ln in logs)
    # The accessors were served (fill-returned) before the fire — the
    # premise is service, not any grade.
    assert batches and _ACC in batches[0] and _OWNER not in batches[0]


def test_negative_control_grade_unanimity_reproduces_the_zero_emission(
        monkeypatch, tmp_path: Path) -> None:
    """The recorded shape: under the unanimity premise (only a graded unit
    counts as served) the cohort holds the fire on every sweep — zero
    admission events, the owner in no batch, exactly the live run's
    `events.jsonl`."""
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    real = _mod.escalate_to_measured_owners

    def _unanimity(**kw: Any):
        kw["served"] = frozenset(kw.get("graded") or frozenset())
        return real(**kw)

    monkeypatch.setattr(_mod, "escalate_to_measured_owners", _unanimity)
    logs, events, batches = _replay(monkeypatch, tmp_path)
    assert not any(e == "phase_c.attribution.measured_owners"
                   for e, _ in events)
    assert all(_OWNER not in b for b in batches)


# ---------------------------------------------------------------------------
# The pair's withholding, and the lapse that makes it a refusal to spend
# rather than a lock. Run `1158170-ce307ee7` (2026-09-15) measured the
# executed slice of its three leftovers at 29 units and dispatched 3 at both
# iterations, because `_paired_tests` withheld every coverage owner for as
# long as the hop existed — a condition no fill can change.
_DISPATCHER = FnSite("src/a.rs", "fmt", "impl Debug for Error")
_IMPLEMENTER = FnSite("src/b.rs", "owner_debug", "impl ErrorImpl")
_PAIR = FormatterPair(dispatcher=_DISPATCHER, implementer=_IMPLEMENTER)
_IMPL_LOC = FnLocator("src/b.rs", "owner_debug")
# The tree's oracle identity. Non-empty on purpose: `""` is the driver's
# word for "no oracle is wired", and the lapse refuses on it because a
# caller that could not read the oracle has observed nothing about whether
# the pair's record describes this tree.
_ORACLE = "walkoracle01"


def _pair_owners() -> dict[str, frozenset[FnSite]]:
    """The coverage map a paired leftover has: both hop members plus the
    spine unit no attribution path names."""
    return {t: frozenset({_ACC_SITE, _ACC2_SITE, _OWNER_SITE,
                          _DISPATCHER, _IMPLEMENTER})
            for t in _REDS}


def _pair_coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
    return CoverageSlice(owners=_pair_owners(),
                         failing=frozenset(failing_tests))


def _attribute_with_pair(*_a: Any, **_k: Any) -> AttributionResult:
    front = {_ACC: set(_REDS), _ACC2: set(_REDS), _IMPL_LOC: set(_REDS)}
    return AttributionResult(dict(front), FailureKind.NO_FRONTIER,
                             dict(front), pairs=frozenset({_PAIR}))


def _pair_row(outcome: RunOutcome, fill: int) -> RouteLedgerRow:
    """One pair row of fill `fill` of an earlier process: the withholding
    clock counts fills, so each graded buy is its own fill."""
    return RouteLedgerRow(
        kind=RouteKind.PAIR,
        label="pair src/a.rs::fmt + src/b.rs::owner_debug",
        targets=(("src/a.rs", "fmt"), ("src/b.rs", "owner_debug")),
        bodies_sha="deadbeef", oracle_fp=_ORACLE, outcome=outcome,
        run_id="earlier-run", iteration=fill,
        target_tests=tuple(_REDS))


def _replay_pair(monkeypatch, tmp_path: Path, rows, *, oracle=_ORACLE):
    walk = load_walk("9872-51b20f30")
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(
            walk, monkeypatch, tmp_path, events=events,
            frontier={_ACC: set(_REDS), _ACC2: set(_REDS)},
            fill=_fill, coverage=_pair_coverage,
            attribute=_attribute_with_pair, route_rows=rows,
            oracle_fp=oracle)
    finally:
        bind(None)
    return logs, events, batches


def test_a_spent_pair_stops_withholding_the_coverage_owners(
        monkeypatch, tmp_path: Path) -> None:
    """A hop pair bought `DEFAULT_UNPAID_GRACE` times under this oracle that
    returned nothing no longer holds the frontier at its own members: the
    measured spine unit is admitted and reaches a dispatched batch, and the
    log says what refuted the premise."""
    rows = [_pair_row(RunOutcome.REVERTED, i + 1)
            for i in range(DEFAULT_UNPAID_GRACE)]
    logs, events, batches = _replay_pair(monkeypatch, tmp_path, rows)
    lapsed = [d for e, d in events
              if e == "phase_c.dispatch.pair_withhold_lapsed"]
    assert lapsed, "the withholding must lapse on the pair's own record"
    assert set(lapsed[0]["tests"]) == set(_REDS)
    assert lapsed[0]["still_withholding"] == []
    assert all(v >= DEFAULT_UNPAID_GRACE
               for v in lapsed[0]["unpaid_fills"].values())
    assert any(_OWNER in b for b in batches), (
        "the admitted coverage owner must reach a dispatched batch")
    assert any("without it turning them green" in ln for ln in logs)


def test_negative_control_an_unspent_pair_still_withholds(
        monkeypatch, tmp_path: Path) -> None:
    """The 675471 case, which must still behave as it did: a pair with
    fewer graded buys than the bound is still the paying fill, the coverage
    spine is admitted nowhere, and no batch holds it. Same walk, same
    coverage map, same hop — only the pair's record differs."""
    rows = [_pair_row(RunOutcome.REVERTED, i + 1)
            for i in range(DEFAULT_UNPAID_GRACE - 1)]
    logs, events, batches = _replay_pair(monkeypatch, tmp_path, rows)
    assert not any(e == "phase_c.dispatch.pair_withhold_lapsed"
                   for e, _ in events)
    assert not any(e == "phase_c.attribution.measured_owners"
                   for e, _ in events)
    assert all(_OWNER not in b for b in batches)
    assert any("whose buy is unspent" in ln for ln in logs)


def test_negative_control_no_ledger_at_all_still_withholds(
        monkeypatch, tmp_path: Path) -> None:
    """A caller that can read no ledger has observed nothing about the
    pair, so the withholding stands — the same convention the flat premise
    uses for an unrecorded fingerprint. This is also the pre-lapse code."""
    _logs, events, batches = _replay_pair(monkeypatch, tmp_path, None)
    assert not any(e == "phase_c.dispatch.pair_withhold_lapsed"
                   for e, _ in events)
    assert all(_OWNER not in b for b in batches)


def test_negative_control_an_unwired_oracle_still_withholds(
        monkeypatch, tmp_path: Path) -> None:
    """The same spent ledger that lapses above, read by a driver with no
    oracle wired: the rows may describe another tree entirely, so nothing
    is released. `""` is unrecorded here exactly as it is on a flat row."""
    rows = [_pair_row(RunOutcome.REVERTED, i + 1)
            for i in range(DEFAULT_UNPAID_GRACE)]
    _logs, events, batches = _replay_pair(
        monkeypatch, tmp_path, rows, oracle=None)
    assert not any(e == "phase_c.dispatch.pair_withhold_lapsed"
                   for e, _ in events)
    assert all(_OWNER not in b for b in batches)


# ---------------------------------------------------------------------------
# The cohort an exhausted unit leaves behind. Run `3040-51eab48c`
# (2026-10-02, cluster `stable`, pass 183 at iters 3-5): the one leftover's
# only attributed unit was skipped as exhausted at iter 3 while four stubs
# kept the frontier non-empty. The admission built each test's cohort from
# the units still dispatchable, so the cohort was empty, the test was read
# as unattributed, and the owners admitted the iteration before were gone
# mid-pass until iter 4 spent a no-other-move re-admission to get them back.
# Replayed on the 9872 sweeps (flat, three reds) with an invented shape: one
# unit holds two reds and exhausts after its first try, two more hold the
# third red, and a batch of two leaves one of them to keep the frontier
# non-empty at the iteration the first is skipped.
_HOLD = FnLocator("src/c.rs", "other")
_HOLD2 = FnLocator("src/c.rs", "another")
_HOLD_SITE = FnSite("src/c.rs", "other", "impl Other")
_SPLIT_OWNERS = {
    "t135": frozenset({_ACC_SITE, _OWNER_SITE}),
    "t136": frozenset({_ACC_SITE, _OWNER_SITE}),
    "t137": frozenset({_HOLD_SITE}),
}
_READMIT = "no actionable frontier and no other move"


def _split_coverage(_pd: Path, _cl: str, failing_tests=frozenset(), **_k):
    return CoverageSlice(owners=dict(_SPLIT_OWNERS),
                         failing=frozenset(failing_tests))


def _replay_exhausted(monkeypatch, tmp_path: Path):
    walk = load_walk("9872-51b20f30")
    events: list[tuple[str, dict]] = []
    batches: list[tuple[FnLocator, ...]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        batches.append(keys)
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        logs = replay(
            walk, monkeypatch, tmp_path, events=events,
            frontier={_ACC: {"t135", "t136"}, _HOLD: {"t137"},
                      _HOLD2: {"t137"}},
            fill=_fill, coverage=_split_coverage,
            refill_max="1", dispatch_cap="2", max_stalls="0")
    finally:
        bind(None)
    return logs, events, batches


def _first(logs: list[str], needle: str) -> int:
    return next((i for i, ln in enumerate(logs) if needle in ln), len(logs))


def test_an_exhausted_cohort_admits_its_owners_before_any_readmission(
        monkeypatch, tmp_path: Path) -> None:
    """The plan's sentence: at the iteration a leftover's last attributed
    unit is skipped as exhausted, its measured owners are admitted and no
    no-other-move re-admission is spent first."""
    logs, events, batches = _replay_exhausted(monkeypatch, tmp_path)
    assert batches and _ACC in batches[0] and _OWNER not in batches[0]
    admits = [d for e, d in events
              if e == "phase_c.attribution.measured_owners"]
    assert admits, "the exhausted unit counts as done; its owners are admitted"
    assert "src/b.rs::owner_fmt" in admits[0]["added"]
    assert _first(logs, "measured-owners:") < _first(logs, _READMIT)
    assert any(_OWNER in b for b in batches)


def test_negative_control_the_dispatchable_cohort_spends_a_readmission_first(
        monkeypatch, tmp_path: Path) -> None:
    """The pre-fix cohort (units still dispatchable): the exhausted unit's
    tests have an empty cohort, the owner is admitted nowhere while the
    other unit holds the frontier, and the loop's first move for those
    tests is the re-admission of the exhausted unit."""
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    real = _mod.attributed_cohorts
    monkeypatch.setattr(
        _mod, "attributed_cohorts",
        lambda _frontier, eligible: real({}, eligible))
    # The pre-fix loop also had no sight: today a seen test's empty cohort
    # counts as served, which admits the owner by a second route. Not taken
    # restores the whole pre-fix shape.
    real_sight = _mod.sight_reading
    monkeypatch.setattr(
        _mod, "sight_reading",
        lambda owed, obs, taken=True: real_sight(owed, obs, taken=False))
    logs, events, batches = _replay_exhausted(monkeypatch, tmp_path)
    assert _first(logs, _READMIT) < len(logs), (
        "the recorded shape: a re-admission is spent")
    assert _first(logs, _READMIT) < _first(
        logs, "unit(s) admitted from the owner map")


def test_an_admitted_owner_reaches_the_fill_as_coverage_not_as_escalated(
        monkeypatch, tmp_path: Path) -> None:
    """Run `3076-051d385a` (2026-10-13): the admission wrote its owners into
    the escalation's set, so the fill graded every one of them ESCALATED
    ("the defect is in what this fn constructs") on no futility measurement.
    The plan's sentence through the real driver: the batch that carries the
    admitted owner hands it to the fill as a measured owner, and in neither
    escalated set. Negative control, run on the pre-change tree
    (`git archive 92e5f30`): there the same fill call carries the owner in
    `escalated_keys` and `escalated_measured`."""
    walk = load_walk("9872-51b20f30")
    events: list[tuple[str, dict]] = []
    calls: list[dict[str, Any]] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        calls.append(kw)
        keys = tuple(kw.get("tdd_frontier_keys") or ())
        return ClusterFillResult(attempts=1, dispatched_fns=keys)

    try:
        replay(walk, monkeypatch, tmp_path, events=events,
               frontier={_ACC: set(_REDS), _ACC2: set(_REDS)},
               fill=_fill, coverage=_coverage)
    finally:
        bind(None)
    with_owner = [kw for kw in calls
                  if _OWNER in (kw.get("tdd_frontier_keys") or ())]
    assert with_owner, "the admitted owner must reach a fill"
    kw = with_owner[0]
    assert _OWNER not in (kw.get("escalated_keys") or frozenset())
    assert _OWNER not in (kw.get("escalated_measured") or frozenset())
    assert _OWNER in (kw.get("measured_owner_keys") or frozenset())
