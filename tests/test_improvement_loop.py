"""Pins for `_improvement_loop` — an adapter over THE
refine driver (`run_refine_loop`). Encodes the unified contract, including
the two semantics CORRECTED by the port (flagged below): post-fix
re-measurement (no one-iteration lag, no spurious first-iteration stall)
and measuring the final fix of a stalled loop.
"""
from __future__ import annotations

from pipeline_core.application.refine_loop import run_callback_loop


class _Seq:
    """run_check stub: yields (ok, output, metric) tuples; last repeats."""

    def __init__(self, *vals):
        self.vals = list(vals)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.vals.pop(0) if len(self.vals) > 1 else self.vals[0]


def test_success_exit_returns_passing_output():
    check = _Seq((False, "err", 3), (True, "all green", 0))
    fixes = []

    def do_fix(output, history, *, retro):
        fixes.append(retro)
        return {"summary": "f1"}

    ok, out, hist = run_callback_loop(
        name="t", run_check=check, do_fix=do_fix, max_stalls=3)
    assert ok and out == "all green"
    assert check.calls == 2 and len(fixes) == 1 and len(hist) == 1
    assert fixes[0] == ""                      # first attempt: no meta context


def test_stall_budget_is_fix_count_and_measures_last_fix():
    # Constant metric → every attempt stalls; max_stalls=3 → exactly 3 fix
    # attempts. f5 CORRECTED semantics: each fix is re-measured immediately
    # (4 = 1 entry measure + 3 post-fix), so the final fix is not discarded
    # unmeasured, and iteration 1 no longer burns a spurious stall.
    check = _Seq((False, "err", 5))
    fixes = []

    def do_fix(output, history, *, retro):
        fixes.append(1)
        return {"summary": "f"}

    ok, out, hist = run_callback_loop(
        name="t", run_check=check, do_fix=do_fix, max_stalls=3)
    assert not ok
    assert len(fixes) == 3 and len(hist) == 3
    assert check.calls == 4
    assert hist[-1]["post_fix_metric"] == 5    # measured, not a hint


def test_improvement_on_final_stall_iteration_continues():
    # CORRECTED semantics: a fix that improves on what would be the last
    # allowed iteration RESETS the stall budget instead of being discarded.
    # metrics: entry 10; post-fix: 10, 9 (improves at stall limit), 9, 9.
    check = _Seq((False, "e", 10), (False, "e", 10), (False, "e", 9),
                 (False, "e", 9), (False, "e", 9))
    fixes = []

    def do_fix(output, history, *, retro):
        fixes.append(1)
        return {}

    ok, _, hist = run_callback_loop(
        name="t", run_check=check, do_fix=do_fix, max_stalls=2)
    assert not ok
    # f1 stalls (10→10), f2 improves (→9, reset), f3+f4 stall out the budget.
    assert len(fixes) == 4


def test_abort_short_circuits_without_remeasure():
    check = _Seq((False, "err", 3))

    def do_fix(output, history, *, retro):
        return {"abort": True, "reason": "check_timeout"}

    ok, out, hist = run_callback_loop(
        name="t", run_check=check, do_fix=do_fix, max_stalls=3)
    assert not ok and out == "err"
    assert check.calls == 1                    # no post-fix measure on abort
    assert len(hist) == 1 and hist[0]["abort"]


def test_retro_fires_only_when_stalled_and_reaches_do_fix():
    check = _Seq((False, "err", 5))
    retros = []
    guidance_seen = []

    def do_retro(output, history):
        retros.append(1)
        return "try a different approach"

    def do_fix(output, history, *, retro):
        guidance_seen.append(retro)
        return {}

    run_callback_loop(
        name="t", run_check=check, do_fix=do_fix, do_retro=do_retro,
        max_stalls=2)
    # attempt 1: not stalled → no retro; attempt 2: stalled → retro fires
    # and its text reaches the fix via state.context().
    assert len(retros) == 1
    assert guidance_seen[0] == ""
    assert "try a different approach" in guidance_seen[1]


def test_legacy_two_arg_do_fix_still_accepted():
    check = _Seq((False, "err", 3), (True, "ok", 0))

    def do_fix(output, history):
        return {"summary": "legacy"}

    ok, _, hist = run_callback_loop(
        name="t", run_check=check, do_fix=do_fix)
    assert ok and hist[0]["summary"] == "legacy"
