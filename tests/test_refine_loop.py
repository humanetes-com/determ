"""Unit tests for the refine-loop primitive (the loop-unification work).

The loop POLICY (stall detection, retro-on-stall, budget, meta-awareness rendering) is pure and
deterministic — driven by a scripted fake strategy, no model or cargo. This is the net the engine
loops are migrated against.
"""

from __future__ import annotations

from pipeline_core.application.refine_loop import run_refine_loop
from pipeline_core.domain.refine import (
    Attempt,
    CompilingSalvage,
    FixProgress,
    FloorStall,
    LoopState,
    RetroCandidate,
    is_retro_improvement,
)


class _Scripted:
    """A RefineStrategy whose attempts return a scripted metric sequence."""

    def __init__(self, attempt_metrics: list[float], *, target: float) -> None:
        self._metrics = list(attempt_metrics)
        self._target = target
        self._idx = 0
        self.contexts: list[str] = []   # state.context() each attempt saw
        self.retro_count = 0

    def measure(self) -> float:
        return 0.0  # nothing covered before the first attempt

    def is_done(self, metric: float) -> bool:
        return metric >= self._target

    def retrospect(self, state: LoopState) -> str:
        self.retro_count += 1
        return f"retro@{state.iteration}"

    def attempt(self, state: LoopState) -> Attempt:
        self.contexts.append(state.context())
        m = self._metrics[self._idx]
        self._idx += 1
        return Attempt(metric=m)


def test_improves_until_done() -> None:
    s = _Scripted([50, 100], target=100)
    state = run_refine_loop(s, max_stalls=3)
    assert [a.metric for a in state.attempts] == [50, 100]
    assert [a.improved for a in state.attempts] == [True, True]
    assert state.best_metric == 100 and state.stalls == 0
    assert s.retro_count == 0   # never stalled → no retrospectives


def test_stalls_out_after_budget() -> None:
    # improve once, then flatline: stop after max_stalls consecutive non-improving attempts.
    s = _Scripted([50, 50, 50, 50], target=100)
    state = run_refine_loop(s, max_stalls=2)
    assert len(state.attempts) == 3                       # 1 improving + 2 stalls, then stop
    assert [a.improved for a in state.attempts] == [True, False, False]
    assert state.stalls == 2
    assert s.retro_count == 1                             # retro fires once (before the 3rd attempt)


def test_meta_awareness_context() -> None:
    s = _Scripted([50, 50, 50], target=100)
    run_refine_loop(s, max_stalls=2)
    assert s.contexts[0] == ""                            # first attempt: no history
    # third attempt sees iteration, stall count, the trajectory, and the latest retrospective.
    ctx = s.contexts[2]
    assert "attempt 3" in ctx
    assert "consecutive" in ctx
    assert "#1" in ctx and "#2" in ctx                    # trajectory of prior attempts
    assert "retro@3" in ctx                               # pending retrospective surfaced


def test_already_done_runs_nothing() -> None:
    class _Done(_Scripted):
        def measure(self) -> float:
            return 100.0
    s = _Done([], target=100)
    state = run_refine_loop(s)
    assert state.attempts == []                           # done before any attempt


def test_driver_assigns_iteration_and_recomputes_improved() -> None:
    # Strategy returns improved=True wrongly; the driver overrides it from the running best.
    class _Liar(_Scripted):
        def attempt(self, state: LoopState) -> Attempt:
            self.contexts.append(state.context())
            m = self._metrics[self._idx]
            self._idx += 1
            return Attempt(metric=m, improved=True, iteration=999)   # lies
    s = _Liar([10, 5], target=100)
    state = run_refine_loop(s, max_stalls=1)
    assert [a.iteration for a in state.attempts] == [1, 2]           # driver-assigned, not 999
    assert [a.improved for a in state.attempts] == [True, False]     # 5 < 10 → not improved


# --- FloorStall — pins the legacy best/stall/prev_sig semantics ---


def test_floorstall_strict_decrease_resets_stall():
    g = FloorStall()
    assert g.observe(5) is True          # first observation sets the floor
    assert (g.best, g.stalls) == (5, 0)
    assert g.observe(5) is False         # equal count = stall (strict decrease only)
    assert (g.best, g.stalls) == (5, 1)
    assert g.observe(7) is False         # regression = stall, floor unchanged
    assert (g.best, g.stalls) == (5, 2)
    assert g.observe(3) is True          # improvement resets
    assert (g.best, g.stalls) == (3, 0)


def test_floorstall_stalled_threshold():
    g = FloorStall()
    g.observe(2)
    g.observe(2)
    assert not g.stalled(2)
    g.observe(2)
    assert g.stalled(2)


def test_floorstall_repeated_signature_needs_two_observations():
    # Legacy guard `fix_attempt > 1`: the first observation never counts as
    # repeated, even against the per-impl init value (empty frozenset).
    g = FloorStall()
    g.observe(1, signature=frozenset())
    assert g.repeated is False
    g.observe(1, signature=frozenset())
    assert g.repeated is True


def test_floorstall_repeated_tracks_consecutive_only():
    g = FloorStall()
    g.observe(3, signature=frozenset({"a"}))
    g.observe(3, signature=frozenset({"b"}))
    assert g.repeated is False
    g.observe(3, signature=frozenset({"b"}))
    assert g.repeated is True
    g.observe(2, signature=frozenset({"c"}))
    assert g.repeated is False           # improvement with new sig clears it


def test_floorstall_no_signature_never_repeated():
    # Pass-level gauge passes no signature; repeated must stay False.
    g = FloorStall()
    g.observe(4)
    g.observe(4)
    assert g.repeated is False


def test_retro_improvement_more_own_tests_passing():
    old = RetroCandidate(own_tests_passing=1, compile_error_count=0)
    new = RetroCandidate(own_tests_passing=2, compile_error_count=0)
    assert is_retro_improvement(old, new) is True


def test_retro_improvement_fewer_own_tests_passing_is_not_improvement():
    old = RetroCandidate(own_tests_passing=2, compile_error_count=0)
    new = RetroCandidate(own_tests_passing=1, compile_error_count=0)
    assert is_retro_improvement(old, new) is False


def test_retro_improvement_same_tests_lower_errors_still_counts():
    old = RetroCandidate(own_tests_passing=0, compile_error_count=3)
    new = RetroCandidate(own_tests_passing=0, compile_error_count=1)
    assert is_retro_improvement(old, new) is True


def test_retro_improvement_same_tests_same_errors_is_not_improvement():
    old = RetroCandidate(own_tests_passing=0, compile_error_count=3)
    new = RetroCandidate(own_tests_passing=0, compile_error_count=3)
    assert is_retro_improvement(old, new) is False


def test_retro_improvement_test_count_dominates_error_count():
    # Fewer tests passing but ALSO fewer errors is still not an improvement —
    # the own-test signal is primary and overrides the error-count tiebreak.
    old = RetroCandidate(own_tests_passing=2, compile_error_count=3)
    new = RetroCandidate(own_tests_passing=1, compile_error_count=0)
    assert is_retro_improvement(old, new) is False


def test_parse_retrospective_function_matches_old_regex():
    import re

    from pipeline_core.domain.refine import parse_retrospective_function
    OLD = re.compile(r'FUNCTION\s+([^:]+(?:::[^:]+)*):\s*(?:\([a-d]\)\s*)?(\w+)')

    def old(line):
        m = OLD.match(line)
        return (m.group(1).rstrip(':'), m.group(2)) if m else None

    cases = [
        "FUNCTION AdhocKind::anyhow_kind: (c) UNREACHABLE — reason",
        "FUNCTION foo: TESTABLE",
        "FUNCTION a::b::c: (a) CFG_GATED extra",
        "FUNCTION   spaced  :  NEEDS_INTERNALS",
        "FUNCTION bad:::x KW", "FUNCTION nocolon TESTABLE",
        "NOT a function line", "FUNCTION x: (z) KW",
    ]
    for line in cases:
        assert parse_retrospective_function(line) == old(line), repr(line)


# --- ratchet sweep 2026-07-04 ---------------------------------------------- #

def test_loop_state_latest_retro_fallbacks() -> None:
    st = LoopState()
    assert st.latest_retro() == ""
    st.record(Attempt(metric=1.0, retro="first"))
    st.record(Attempt(metric=2.0))
    assert st.latest_retro() == "first"       # last attempt has none → walk back
    st.pending_retro = "pending"
    assert st.latest_retro() == "pending"


def test_parse_retrospective_categories_map() -> None:
    from pipeline_core.domain.refine import parse_retrospective_categories
    retro = "\n".join([
        "FUNCTION foo: (a) TESTABLE covered by t1",
        "FUNCTION bar::baz: CFG_GATED nightly only",
        "FUNCTION qux: (c) UNREACHABLE dead branch",
        "FUNCTION deep: NEEDS_INTERNALS private state",
        "FUNCTION odd: WEIRD_KEYWORD unknown",
        "not a function line",
        "FUNCTION",
        "FUNCTION : missing name",
    ])
    cats = parse_retrospective_categories(retro)
    assert cats["TESTABLE"] == ["foo"]
    assert cats["CFG_GATED"] == ["bar::baz"]
    assert cats["UNREACHABLE"] == ["qux"]
    assert cats["NEEDS_INTERNALS"] == ["deep"]


# --- FixProgress — the two-tier fix-loop metric ---

def test_fixprogress_compile_tier_dominates_test_tier() -> None:
    # Compiling with many red tests beats not compiling with few errors.
    assert FixProgress(0, 99) < FixProgress(1, 0)
    assert FixProgress(0, 12) < FixProgress(2, 0)


def test_fixprogress_test_count_breaks_ties_within_a_tier() -> None:
    assert FixProgress(0, 3) < FixProgress(0, 4)
    assert FixProgress(2, 0) < FixProgress(3, 0)
    assert not FixProgress(0, 4) < FixProgress(0, 4)


def test_fixprogress_renders_the_tier_it_is_in() -> None:
    assert str(FixProgress(2, 0)) == "2 compile err(s)"
    assert str(FixProgress(0, 12)) == "compiles, 12 failing test(s)"
    assert FixProgress(0, 12).compiles and not FixProgress(1, 0).compiles


def test_floorstall_resets_on_the_tier_crossing() -> None:
    g = FloorStall()
    for _ in range(3):
        g.observe(FixProgress(2, 0))
    assert g.stalls == 2
    assert g.observe(FixProgress(0, 12)) is True   # compiled → improved
    assert g.stalls == 0 and g.best == FixProgress(0, 12)


# --- CompilingSalvage — what an exhausted fix loop leaves in the tree ---

def test_salvage_ignores_attempts_that_never_compiled() -> None:
    s = CompilingSalvage()
    s.offer("broken", FixProgress(3, 0))
    s.offer("also broken", FixProgress(1, 0))
    assert s.best is None


def test_salvage_keeps_the_fewest_failing_tests_not_the_last_attempt() -> None:
    s = CompilingSalvage()
    s.offer("two red", FixProgress(0, 2))
    s.offer("nine red", FixProgress(0, 9))
    assert s.best is not None
    assert s.best.body == "two red"
    assert s.best.metric == FixProgress(0, 2)


def test_salvage_prefers_a_compiling_body_over_any_red_one() -> None:
    s = CompilingSalvage()
    s.offer("one rustc error", FixProgress(1, 0))
    s.offer("compiles, 12 red", FixProgress(0, 12))
    assert s.best is not None and s.best.body == "compiles, 12 red"


def test_salvage_ignores_empty_bodies() -> None:
    s = CompilingSalvage()
    s.offer("   \n", FixProgress(0, 1))
    assert s.best is None


def test_floorstall_rejects_mixed_metric_units() -> None:
    # A gauge fed a raw count on one attempt and a FixProgress on the next
    # measures nothing; the comparison must blow up loudly, not silently rank.
    import pytest
    g = FloorStall()
    g.observe(FixProgress(2, 0))
    with pytest.raises(TypeError):
        g.observe(3)


def test_readmission_warranted_needs_payoff_not_just_activity() -> None:
    # Live on `anyhow/nightly` (07-30 -> 07-31): 51 fills landed, pass stayed
    # at 93 across three run boundaries, and the old gate (fills > 0 alone)
    # re-admitted the fns that had already failed on every one of them. The
    # frontier went 41 -> 73 and iteration cost followed it to 224 min — the
    # loop financing its own growth out of work that produced no evidence.
    from pipeline_core.domain.refine import readmission_warranted as w

    # 51 fills, score unmoved: NOT warranted. This is the case that bled.
    assert w(fills_since_reset=51, best_metric=93, metric_at_last_reset=93) is False
    # Fills AND a score move: warranted — the retry has something new to read.
    assert w(fills_since_reset=51, best_metric=95, metric_at_last_reset=93) is True
    # A score move with no fills cannot happen from this loop's own work; if
    # it does, there is still nothing newly written to retry against.
    assert w(fills_since_reset=0, best_metric=95, metric_at_last_reset=93) is False
    # A REGRESSION never buys a re-admission either.
    assert w(fills_since_reset=8, best_metric=90, metric_at_last_reset=93) is False
    # The sentinel: ANY score beats -1, so an unseeded baseline grants the
    # first re-admission free. That grant is the DEADLOCK BREAKER and is not
    # removable — exhaustion flags persist on disk, so a unit that is itself
    # the blocker could never be re-admitted, its release needing a score move
    # that cannot happen while it stays exhausted.
    assert w(fills_since_reset=3, best_metric=93, metric_at_last_reset=-1) is True
    # What the caller controls is WHEN the sentinel is left in place. Run
    # 589477 iter 2 spent the free grant on 7 units off an iter-1 fill phase
    # `pass_delta` scored `by pair: +0 -0`, frontier 40 -> 73, which is once
    # per PROCESS in a pipeline that dies every few hours. `run_phase_c_tdd`
    # therefore seeds the baseline whenever the code is unchanged since the
    # process that set the flags, and leaves the sentinel only for new code —
    # the same unit `TddFloor` guards on. Seeded, the same iteration withholds.
    assert w(fills_since_reset=3, best_metric=93, metric_at_last_reset=93) is False
