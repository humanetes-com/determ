"""Why a task0c coverage pass ended — measured, not inferred from an absence.

`at_ceiling` used to mean "no file set `_pass_had_work` this pass" (the flag
was set only on a committed block), and the log line it printed asserted
something else entirely: that every uncovered line which remains is inside an
excluded cfg block or is an LLVM artifact. On 2026-08-27, run
`2881435-34981ab0`, 63 `llm.request` events extracted nothing (fenced-reply defect), the
flag stayed false, and both passes over anyhow's `stable` cluster reported a
ceiling. Their own post-filter counts listed the actionable set below. Two
at_ceiling reports are `ModuleAugmentState.CEILING_CONFIRM`, so the cluster was
accepted at 83.9% against the ~98.1% ceiling estimate printed at that same
cluster's entry, and an earlier tree's 97.8%.

What is asserted here is the plan's sentence — a pass with reachable uncovered
lines is never a ceiling, whatever the pipeline did with them — on the recorded
per-file vector, with the pre-fix rule as the negative control.
"""
from __future__ import annotations

from pipeline_core.domain.coverage import coverage_pass_outcome

# logs/pipeline_anyhow.log lines 921-1021 and 1026-1119: post-filter actionable
# uncovered lines for backtrace.rs, ensure.rs, lib.rs, ptr.rs, fmt.rs,
# wrapper.rs, context.rs, error.rs — identical in both passes.
RECORDED_STABLE_PASS = (4, 3, 4, 9, 8, 14, 23, 54)


def _pre_fix_rule(*, actionable_lines: int, had_model_call: bool) -> str:
    """The rule this module replaces, verbatim: `if not _pass_had_work`. It
    never reads the actionable count, which is why it could call that pass a
    ceiling."""
    return "ceiling" if not had_model_call else "progressed"


def test_the_recorded_stable_pass_is_not_a_ceiling() -> None:
    actionable = sum(RECORDED_STABLE_PASS)
    assert actionable == 119, "the recorded per-file vector must not drift"
    assert coverage_pass_outcome(
        actionable_lines=actionable, had_model_call=False) == "no_model_call"


def test_negative_control_the_pre_fix_rule_called_that_pass_a_ceiling() -> None:
    assert _pre_fix_rule(
        actionable_lines=sum(RECORDED_STABLE_PASS), had_model_call=False) == "ceiling"


def test_no_reachable_line_is_the_only_ceiling() -> None:
    # The claim the log line makes: everything left was filtered. True of a pass
    # whose actionable set is empty, whether or not any model call was spent.
    assert coverage_pass_outcome(actionable_lines=0, had_model_call=False) == "ceiling"
    assert coverage_pass_outcome(actionable_lines=0, had_model_call=True) == "ceiling"


def test_one_reachable_line_is_enough_to_deny_the_ceiling() -> None:
    assert coverage_pass_outcome(actionable_lines=1, had_model_call=False) == "no_model_call"
    assert coverage_pass_outcome(actionable_lines=1, had_model_call=True) == "progressed"


def test_a_working_pass_is_neither() -> None:
    assert coverage_pass_outcome(
        actionable_lines=sum(RECORDED_STABLE_PASS), had_model_call=True) == "progressed"
