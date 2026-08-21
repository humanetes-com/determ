"""The flat-score guard as a fold — stage 2's conversion and its control.

THE RUN THIS IS ABOUT. `1920662-40f711b2` (2026-08-16 -> 08-17, 9h48m, 191
model calls, net -46 tests). Its first three judged sweeps read 79/101, 83/106,
83/106 and no pass closed, so at 00:25:43Z the live guard printed

    the flat-score guard is ARMED and WITHHELD — 83/106 has been read 2 time(s)
    (limit 2) but all of them inside traversal 0, and a stop needs one
    completed traversal behind the repeat

and the run spent another five hours. The recorded readings are quoted here as
literals rather than distilled into a walk fixture because there are seven of
them and they are the whole input: `phase_c.tdd.iter` for that run, cluster
`stable`, `(pass_count, oracle_size, pass_completed)`.

WHAT IS BEING ASSERTED: that the DECISION became reachable. The fold over that
same stream stops at the third sweep; the pre-fix rule injected into the same
stream reproduces the withholding. Neither says the run would have reached any
particular score — a walk may assert reachability, never the score.
"""
from __future__ import annotations

from pipeline_core.domain.flat_score import flat_score
from pipeline_core.domain.loop_facts import PassClosed, SweepMeasured

# (iter, pass_count, oracle_size, pass_completed, judged) as recorded.
# Iteration 4 is the crash retrigger's `continue`: it commits no batch and
# closes no pass, which is exactly how `_traversals` stayed parked at 0.
_RECORDED = [
    (1, 79, 101, False, True),
    (2, 83, 106, False, True),
    (3, 83, 106, False, True),
    (4, 83, 106, False, False),
    (5, 25, 105, False, True),
    (6, 33, 105, False, True),
    (7, 33, 105, False, True),
]


def _facts(upto: int) -> list:
    out: list = []
    for it, p, o, closed, judged in _RECORDED:
        if it > upto:
            break
        out.append(SweepMeasured(iter=it, cluster="stable", pass_count=p,
                                 fail_count=o - p, oracle_size=o,
                                 judged=judged))
        if closed:
            out.append(PassClosed(iter=it, cluster="stable", cohort_size=43,
                                  attempted=43))
    return out


def test_the_fold_stops_where_the_live_run_withheld() -> None:
    """THE FIX. Two readings of the same number are the whole rule."""
    d = flat_score(_facts(3), window=5, limit=2)
    assert d is not None
    assert d.stop, f"expected STOP at iter 3, got {d}"
    assert str(d.reading) == "83/106" and d.repeats == 2
    assert d.traversals == 0
    assert "83/106@t0 83/106@t0" in d.render()


def test_negative_control_the_traversal_precondition_reproduces_the_withhold() -> None:
    """The pre-fix rule, injected into the same recorded stream. It must
    reproduce the live WITHHELD or the test above has proved nothing — and the
    withheld state has to be REPORTED, because a guard that is silent when it
    does not trip cannot be told apart from one with nothing to say (that
    silence is what let `1692702-94e6163a` spend 3h36m)."""
    d = flat_score(_facts(3), window=5, limit=2, require_traversal=True)
    assert d is not None
    assert not d.stop
    assert d.withheld_by_traversal
    assert d.repeats == 2 and d.first_traversal == 0 and d.traversals == 0


def test_a_closed_pass_releases_the_precondition() -> None:
    """The precondition is not nonsense — it is unsound as a PRECONDITION
    because it waits on a quantity the loop is not obliged to produce. Give the
    stream one closed pass between the two identical readings and it releases."""
    facts = [
        SweepMeasured(iter=1, cluster="stable", pass_count=83, fail_count=23,
                      oracle_size=106),
        PassClosed(iter=1, cluster="stable", cohort_size=43, attempted=43),
        SweepMeasured(iter=2, cluster="stable", pass_count=83, fail_count=23,
                      oracle_size=106),
    ]
    d = flat_score(facts, window=5, limit=2, require_traversal=True)
    assert d is not None and d.stop and not d.withheld_by_traversal


def test_the_newest_reading_has_to_be_the_repeated_one() -> None:
    """A window holding `79, 66, 79, 80` contains a repeat, but the sweep about
    to dispatch read 80: the loop moved, and stopping there would be reading a
    stale entry as the present."""
    readings = [(79, 101), (66, 101), (79, 101), (80, 101)]
    facts = [SweepMeasured(iter=i, cluster="stable", pass_count=p,
                           fail_count=o - p, oracle_size=o)
             for i, (p, o) in enumerate(readings, start=1)]
    d = flat_score(facts, window=5, limit=2)
    assert d is not None and not d.stop and str(d.reading) == "80/101"


def test_an_unjudged_sweep_is_a_gap_and_not_a_reading() -> None:
    """A grace sweep after a deliberate re-stub, a compile-broken sweep and an
    unmeasured restore are the ABSENCE of a measurement. They must not enter
    the window — and the count of them has to reach the stop line, because how
    far apart the two identical readings were is the one thing an operator
    needs to judge the stop by hand."""
    facts = [
        SweepMeasured(iter=1, cluster="stable", pass_count=83, fail_count=23,
                      oracle_size=106),
        SweepMeasured(iter=2, cluster="stable", pass_count=0, fail_count=0,
                      oracle_size=0, judged=False),
        SweepMeasured(iter=3, cluster="stable", pass_count=0, fail_count=0,
                      oracle_size=0, judged=False),
        SweepMeasured(iter=4, cluster="stable", pass_count=83, fail_count=23,
                      oracle_size=106),
    ]
    d = flat_score(facts, window=5, limit=2)
    assert d is not None and d.stop
    assert d.repeats == 2, "the excused sweeps must not be readings"
    assert d.gap == 2, "…and the stop line has to be able to say there were two"


def test_the_knobs_disable_it_exactly_as_documented() -> None:
    """`REPEATS < 2` disables the guard (a repeat count of 1 would fire on the
    first sweep, before any dispatch has been judged) and a window smaller than
    the repeat count can never hold one. `CLAUDE.md` documents both."""
    facts = _facts(3)
    assert not flat_score(facts, window=5, limit=1).stop
    assert not flat_score(facts, window=1, limit=2).stop
    assert flat_score([], window=5, limit=2) is None, (
        "no judged sweep is not the same fact as a reading that does not repeat")
