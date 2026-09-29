"""Pins for the behaviour oracle (F10 Use-A / M4)."""
from __future__ import annotations

from pipeline_core.application.run_behaviour_oracle import run_behaviour_oracle
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    BehaviourVerdict,
    FenceDirective,
)
from pipeline_core.domain.behaviour_oracle import BehaviourOracleReport, summarize


def _assert_spec(bid, *, exercises=frozenset()):
    return BehaviourSpec(
        behaviour_id=bid, setup="", when="run();", then=None,
        subkind=BehaviourSubKind.ASSERTION, fence=FenceDirective.RUN,
        exercises=exercises)


def _suite(*specs):
    return BehaviourSuite(cfg_cluster="stable", specs=tuple(specs))


def test_noop_when_unit_tests_not_green():
    rep = run_behaviour_oracle(
        _suite(_assert_spec("a")), unit_tests_green=False,
        run_spec=lambda s: (BehaviourVerdict.HOLD, ""))
    assert rep.outcomes == ()


def test_all_hold():
    rep = run_behaviour_oracle(
        _suite(_assert_spec("a"), _assert_spec("b")), unit_tests_green=True,
        run_spec=lambda s: (BehaviourVerdict.HOLD, ""))
    assert rep.all_hold and len(rep.holds) == 2


def test_mismatch_collects_suspect_units():
    specs = _suite(
        _assert_spec("ok"),
        _assert_spec("bad", exercises=frozenset({"context", "Chain::new"})))

    def run(s):
        return ((BehaviourVerdict.MISMATCH, "Display differed")
                if s.behaviour_id == "bad" else (BehaviourVerdict.HOLD, ""))

    rep = run_behaviour_oracle(specs, unit_tests_green=True, run_spec=run)
    assert not rep.all_hold
    assert len(rep.mismatches) == 1
    assert rep.suspect_units == frozenset({"context", "Chain::new"})


def test_only_positive_specs_run():
    # a compile_fail behaviour is negative → excluded from the positive oracle.
    neg = BehaviourSpec(
        behaviour_id="neg", setup="", when="x", then=None,
        subkind=BehaviourSubKind.ASSERTION, fence=FenceDirective.COMPILE_FAIL)
    ran: list[str] = []
    run_behaviour_oracle(
        _suite(_assert_spec("pos"), neg), unit_tests_green=True,
        run_spec=lambda s: (ran.append(s.behaviour_id) or (BehaviourVerdict.HOLD, "")))
    assert ran == ["pos"]


def test_report_summarize_carries_exercises():
    s = _assert_spec("x", exercises=frozenset({"root_cause"}))
    o = summarize(s, BehaviourVerdict.MISMATCH, "d")
    assert o.exercises == frozenset({"root_cause"}) and o.detail == "d"
    rep = BehaviourOracleReport(cfg_cluster="stable", outcomes=(o,))
    assert rep.suspect_units == frozenset({"root_cause"})
