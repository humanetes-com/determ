"""The regression gate must PUBLISH the score it measures, on every path.

THE DEFECT. `cargo_runner.test_regression_gate` runs the full cluster test
command through `measure_test_state` on every splice, builds a
`RegressionComparison` carrying `base_pass`, `cur_pass` and `net_pass_delta`,
and then reduced all of it to a regression yes/no. On the ACCEPT path it
returned bare `None`, so the numbers reached nobody. `regression_guard.py`
contained no emit call at all, and every event in the store carrying a
`pass_count` was a sweep-boundary event — 36 in total from 2026-08-28 to
08-30, 17 of them iteration lines.

The consequence, measured on cluster `stable` since 2026-08-14: an iteration
is sweep (0.7 s) → fill (2-4 h) → sweep, 51 of 67 runs lived one or two
sweeps, and so a fill phase's payoff was almost always first measured by the
NEXT run's entry sweep. That is why gains cluster at restarts (across a kill
n=56 total +30; within a run n=35 total +10) with **zero negative deltas
either way** — nothing was reverting good fills; the score simply was not
being written down between sweeps, and killing the run was the only way to
make the loop say a number it already knew.

Each test below drives the real `test_regression_gate` and uses the pre-fix
call — the same gate with no observer — as its negative control.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct import cargo_runner
from pipeline_core.application.reconstruct.fill_reading import (
    fill_reading_fields,
    publish_fill_reading,
    render_fill_reading,
)
from pipeline_core.domain.test_results import RegressionComparison

BASE = """
running 3 tests
test a ... ok
test b ... ok
test c ... FAILED
test result: FAILED. 2 passed; 1 failed; 0 ignored
"""
AFTER_GAIN = """
running 3 tests
test a ... ok
test b ... ok
test c ... ok
test result: ok. 3 passed; 0 failed; 0 ignored
"""


def _gate(after_out: str, observe=None):
    """Drive the real gate: one baseline reading, then one post-fill reading."""
    outs = [BASE, after_out]
    calls = {"n": 0}

    def run_cargo(cmd, cwd, env, timeout=120):
        i = calls["n"]
        calls["n"] += 1
        return 0, outs[min(i, len(outs) - 1)]

    base = cargo_runner.measure_test_state(run_cargo, "cargo test", Path("."), {})
    assert base is not None and base.pass_count == 2
    return cargo_runner.test_regression_gate(
        run_cargo, "cargo test", Path("."), {}, base,
        enabled=True, timeout=120, guard=lambda *a: None, observe=observe)


def test_the_accept_path_published_nothing_before_this() -> None:
    """Negative control: the gate accepts a clean fill by returning bare None,
    which is why a two-to-four-hour fill phase produced no score anywhere."""
    assert _gate(AFTER_GAIN) is None


def test_a_gaining_fill_publishes_its_score_mid_fill() -> None:
    """The plan's sentence: the number the gate measured reaches a sink."""
    seen: list[RegressionComparison] = []
    assert _gate(AFTER_GAIN, observe=seen.append) is None, (
        "publishing must not change the gate's decision")
    assert len(seen) == 1
    cmp = seen[0]
    assert (cmp.base_pass, cmp.cur_pass) == (2, 3)
    assert cmp.net_pass_delta == +1
    assert not cmp.regressed


def test_the_observer_fires_on_the_regressing_path_too() -> None:
    """A score is a score whichever way it moved — a reader watching a fill
    must see the loss as well as the gain, or the trace lies by omission."""
    lost = BASE.replace("test b ... ok", "test b ... FAILED").replace(
        "2 passed; 1 failed", "1 passed; 2 failed")
    seen: list[RegressionComparison] = []
    _gate(lost, observe=seen.append)
    assert len(seen) == 1
    assert seen[0].net_pass_delta == -1
    assert seen[0].regressed


def test_publishing_can_never_break_the_gate() -> None:
    """Telemetry hangs off a decision path that reverts bodies. A formatting
    bug here must not be able to revert one."""
    def boom(_cmp):
        raise RuntimeError("sink exploded")

    seen: list[str] = []
    assert _gate(AFTER_GAIN, observe=lambda c: publish_fill_reading(
        c, emit=lambda *a, **k: boom(c), log=seen.append)) is None
    # The log sink ran before the emit sink raised, and the gate still accepted.
    assert seen and "pass 2 -> 3" in seen[0]


def _cmp(**kw) -> RegressionComparison:
    base = {"newly_red": frozenset(), "newly_crashed": (), "excluded_crashed": (),
            "not_run": (), "measured": ("tests/a.rs",), "base_pass": 2,
            "cur_pass": 3, "base_executed": 3, "cur_executed": 3}
    return RegressionComparison(**{**base, **kw})


def test_the_line_reads_as_a_sweep_line() -> None:
    """The numbers mean what `pass=` means on the iteration line, so they are
    named the same way and can go on one axis without translation."""
    text = render_fill_reading(_cmp())
    assert "pass 2 -> 3 (net +1)" in text
    assert "no regression in the measured scope" in text
    assert fill_reading_fields(_cmp())["pass_count"] == 3
    assert fill_reading_fields(_cmp())["net_pass_delta"] == +1


def test_a_crash_that_cost_passes_is_reported_with_what_it_cost() -> None:
    text = render_fill_reading(_cmp(newly_crashed=("tests/b.rs",),
                                    crash_lost_pass=4))
    assert "newly crashed: ['tests/b.rs'] (was passing 4)" in text
    assert fill_reading_fields(
        _cmp(newly_crashed=("tests/b.rs",), crash_lost_pass=4))["crash_lost_pass"] == 4


def test_baseline_crashed_binaries_are_named_as_excluded_not_lost() -> None:
    """A binary already crashing at baseline can neither convict nor exonerate
    a fill; the line must say so rather than let a reader count it as damage."""
    text = render_fill_reading(_cmp(excluded_crashed=("tests/c.rs",)))
    assert "1 excluded (crashed at baseline)" in text
