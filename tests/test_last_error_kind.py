"""What produced a LAST_ERROR excerpt, checked AT THE CONTRACT THAT WRITES IT.

The defect these tests exist for: the prompt builder decided whether the
previous body had COMPILED by grepping the rendered excerpt for
``binaries crashed``, and `domain.test_results.render_regression_detail`
writes ``test binaries [...] CRASH before reporting results``. Two modules,
one prose string, no test joining them — so the reader answered False on
every crash the renderer could produce, and a body that compiled and
aborted five binaries reached the model as "produced these errors — do not
resubmit the same approach", still carrying the joint-write sentence that
tells a pair the implementer is unentered because the dispatcher does not
call it yet. Run `681204-b695ab3f` (2026-09-14) launched on the fix that
reads that predicate and its own first pair prompt still carried both.

So the negative control here is not a hand-written string: it is the OLD
predicate run over the CURRENT renderer's output, branch by branch. A
test whose fixture is a string containing the phrase the predicate wants
is a second copy of the same guess and fails on nothing.
"""
from __future__ import annotations

import pytest

from pipeline_core.domain.last_error import (
    LastErrorKind,
    instruction_for,
    kind_from_record,
    kind_from_text,
    kind_of_compiled_run,
    last_error_prefix as domain_prefix,
    resolve_kind,
)
from pipeline_core.domain.pair_fill import pair_last_error
from pipeline_core.domain.test_results import (
    RegressionComparison,
    regression_detail_kind,
    render_regression_detail,
)
from pipeline_core.prompts.per_impl import (
    last_error_is_compiling_crash,
    last_error_prefix,
    pair_joint_write_paragraph,
)

UNENTERED_ARM = "they do not enter it because the dispatcher does not yet call"
COMPILE_WORDING = "produced these errors"


def old_predicate(last_error: str) -> bool:
    """The reader as it shipped on 2026-09-14, verbatim. The control."""
    text = (last_error or "").lower()
    if not text:
        return False
    if "error[e" in text and "panicked at" not in text:
        return False
    return ("panicked at" in text
            or "addresssanitizer" in text
            or "binaries crashed" in text)


def comparison(**kw: object) -> RegressionComparison:
    base: dict[str, object] = {
        "newly_red": (), "newly_crashed": (), "excluded_crashed": (),
        "not_run": (), "measured": 8, "base_pass": 30, "cur_pass": 30,
        "base_executed": 30, "cur_executed": 30, "crashed_all_red": (),
        "crash_lost_pass": 0, "newly_hung": (), "hung_tests": ()}
    base.update(kw)
    return RegressionComparison(**base)  # type: ignore[arg-type]


CRASH = comparison(newly_crashed=("tests/a.rs", "tests/b.rs"),
                   crash_lost_pass=30, cur_pass=0)
HANG = comparison(newly_hung=("tests/a.rs",), hung_tests=("t_loop",))
RED = comparison(newly_red=("t_one", "t_two"), cur_pass=28)
EXEC_DROP = comparison(cur_executed=20)
PASS_DROP = comparison(cur_pass=20)

RAN = [(CRASH, LastErrorKind.CRASHED), (HANG, LastErrorKind.HUNG),
       (RED, LastErrorKind.REDDENED)]
ALL = RAN + [(EXEC_DROP, LastErrorKind.LOST_TESTS),
             (PASS_DROP, LastErrorKind.LOST_TESTS)]


@pytest.mark.parametrize("cmp_,expected", ALL)
def test_the_renderer_and_the_kind_agree_branch_for_branch(
        cmp_: RegressionComparison, expected: LastErrorKind) -> None:
    """The contract: a comparison that RENDERS a given head must CLASSIFY
    as that head's kind, and the text fallback must read the renderer's
    own output back to the same answer. This is what was missing — the
    two sides were free to drift and did."""
    assert regression_detail_kind(cmp_) is expected
    text = render_regression_detail(cmp_)
    assert kind_from_text(text) is expected


@pytest.mark.parametrize("cmp_,expected", RAN)
def test_the_old_predicate_was_false_on_every_head_the_renderer_writes(
        cmp_: RegressionComparison, expected: LastErrorKind) -> None:
    """The negative control, and the size of the defect: the shipped
    reader answered False on the producer's own text for every failure
    class that RAN, so the crash wording could never be selected from a
    rendered excerpt. The current reader answers True on the same text
    and on the carried kind."""
    text = render_regression_detail(cmp_)
    assert old_predicate(text) is False
    assert last_error_is_compiling_crash(text) is True
    assert last_error_is_compiling_crash(text, expected) is True


def test_a_body_rustc_rejected_keeps_the_errors_wording() -> None:
    """The control in the other direction: a compile excerpt must not be
    told it compiled, and must keep the unentered-arm sentence, which is
    still the right first-write instruction."""
    excerpt = "error[E0308]: mismatched types\n  expected Ref found *mut"
    assert kind_from_text(excerpt) is LastErrorKind.COMPILE
    assert last_error_is_compiling_crash(excerpt, LastErrorKind.COMPILE) is False
    assert COMPILE_WORDING in last_error_prefix(excerpt, LastErrorKind.COMPILE)
    assert UNENTERED_ARM in pair_joint_write_paragraph(
        excerpt, LastErrorKind.COMPILE)
    assert domain_prefix(excerpt, LastErrorKind.COMPILE) == last_error_prefix(
        excerpt, LastErrorKind.COMPILE)


def test_kind_of_compiled_run_is_reddened_unless_aborted_or_hung() -> None:
    assert kind_of_compiled_run() is LastErrorKind.REDDENED
    assert kind_of_compiled_run(aborted=True) is LastErrorKind.CRASHED
    assert kind_of_compiled_run(hung=True) is LastErrorKind.HUNG
    assert kind_of_compiled_run(aborted=True, hung=True) is LastErrorKind.HUNG
    assert UNENTERED_ARM in pair_joint_write_paragraph("", None)


@pytest.mark.parametrize("cmp_,kind", RAN)
def test_a_body_that_ran_and_faulted_loses_the_unentered_arm_sentence(
        cmp_: RegressionComparison, kind: LastErrorKind) -> None:
    text = render_regression_detail(cmp_)
    assert COMPILE_WORDING not in last_error_prefix(text, kind)
    assert UNENTERED_ARM not in pair_joint_write_paragraph(text, kind)


def test_a_hang_is_never_described_as_a_panic() -> None:
    """A run that never returned has no panic and no stack. Ordering the
    model away from "the call the stack shows reached the panic" would be
    describing evidence the run did not produce."""
    text = render_regression_detail(HANG)
    prefix = last_error_prefix(text, LastErrorKind.HUNG)
    assert "never returned" in prefix
    assert "panic" not in prefix
    assert "panic" not in pair_joint_write_paragraph(text, LastErrorKind.HUNG)
    crash_prefix = last_error_prefix(
        render_regression_detail(CRASH), LastErrorKind.CRASHED)
    assert "panicked" in crash_prefix


def test_a_run_that_only_lost_tests_names_no_faulting_call() -> None:
    """It compiled, so it is not a compile excerpt; but nothing in the
    evidence names a call, so the prompt must not order one away."""
    kind = regression_detail_kind(EXEC_DROP)
    assert kind.compiled is True
    assert kind.has_faulting_call is False
    text = render_regression_detail(EXEC_DROP)
    assert last_error_is_compiling_crash(text, kind) is False


def test_the_carried_kind_outranks_what_the_prose_says() -> None:
    """A record that knows what happened is not second-guessed by a text
    heuristic — that inversion is the whole defect."""
    misleading = "error[E0433]: failed to resolve"
    assert kind_from_text(misleading) is LastErrorKind.COMPILE
    assert resolve_kind(LastErrorKind.CRASHED, misleading) is LastErrorKind.CRASHED
    assert last_error_is_compiling_crash(misleading, LastErrorKind.CRASHED) is True


def test_an_excerpt_with_no_provenance_falls_back_to_its_text() -> None:
    assert resolve_kind(None, render_regression_detail(CRASH)) is LastErrorKind.CRASHED
    assert resolve_kind(LastErrorKind.UNKNOWN, "") is LastErrorKind.UNKNOWN
    assert kind_from_record({}) is None
    assert kind_from_record({"last_error_kind": "crashed"}) is LastErrorKind.CRASHED
    assert kind_from_record({"last_error_kind": "not-a-kind"}) is None
    assert kind_from_record(None) is None


def test_a_truncated_crash_preview_reaches_the_prompt_with_its_instruction() -> None:
    """The stored preview is cut to a fixed budget and the head's length
    grows with the number of binaries it names, so the directive at the
    end is what a wide crash loses. Negative control: the raw truncation
    carries no ask at all."""
    wide = comparison(
        newly_crashed=("tests/error_gen_2.rs", "tests/lib_gen_1.rs",
                       "tests/ptr_gen_1.rs", "tests/test_fmt.rs",
                       "tests/wrapper_gen_2.rs"),
        crash_lost_pass=30, cur_pass=0)
    full = render_regression_detail(wide)
    ask = instruction_for(LastErrorKind.CRASHED)
    stored = full[:240]
    assert ask in full
    assert ask not in stored                      # the negative control

    out = pair_last_error("", stored, "", kind=LastErrorKind.CRASHED)
    assert ask in out.text
    assert out.kind is LastErrorKind.CRASHED

    # A full-length excerpt is not grown, and the ask is never doubled.
    kept = pair_last_error("", full, "", kind=LastErrorKind.CRASHED)
    assert kept.text == full.strip()
    assert kept.text.count(ask) == 1


def test_the_pair_last_error_still_prefers_the_crash_over_a_stale_rustc_line() -> None:
    """Unchanged behaviour, re-pinned now that the function also returns a
    kind: a compiling crash's rejected approach is the crashing body, and
    a prior compile excerpt is a different body rustc already rejected."""
    preview = render_regression_detail(CRASH)
    body = '<FN name="f" block="impl A">unsafe { g() }</FN>'
    out = pair_last_error("error[E0308]: mismatched types", preview, body)
    assert "E0308" not in out.text
    assert "unsafe { g() }" in out.text
    assert out.kind is LastErrorKind.CRASHED

    only_compile = pair_last_error("error[E0308]: mismatched types", "", "")
    assert only_compile.kind is LastErrorKind.COMPILE
    assert only_compile.text == "error[E0308]: mismatched types"

    nothing = pair_last_error("", "", "")
    assert nothing.text == ""
    assert nothing.kind is LastErrorKind.UNKNOWN
