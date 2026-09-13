"""A stored retrospective may not prescribe removing what the unit's own
tests pin.

Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): the paired write of
`src/error.rs::fmt` + `src/fmt.rs::debug` was reverted for reddening ten
previously-passing siblings; the retrospective written from that revert
prescribed dropping the "Caused by" chain and the backtrace emission — the
two substrings the pair's own convicting tests assert in
`format!("{:?}", err)` — and, honoured on the next pass, steered the reply
into the same ten-sibling regression. The fixtures below are that record's
guidance verbatim and the pins of its convicting tests; the negative
controls are the pre-fix paths (no stamp, no pins), which honour it."""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.refill_evidence import pinned_observables
from pipeline_core.domain.retro_gate import (
    CONTRADICTS_TARGET,
    GUIDANCE_FROM_HOLLOW_TREE,
    JOINT_BODY_SHA,
    JOINT_MEMBERS,
    BlockedOnStubs,
    ConvictionEvidence,
    GuidanceScreening,
    RetroProvenance,
    admissible_guidance,
    guidance_sentences,
    pinned_words,
    prescription_removes_pinned_observable,
    removal_sentence,
    retired_guidance_line,
    screen_guidance,
    stamp_conviction,
)

# Verbatim from `.phase_c_logs/retrospectives.json`, record
# `src/error.rs::*pair*::src/error.rs::fmt+src/fmt.rs::debug` (rounds=4).
PAIR_GUIDANCE = (
    "In the Debug impl for Error in src/error.rs::fmt, drop the manual "
    "`self.chain().collect()` / numbered “Caused by” formatting and the "
    "trailing `core::fmt::Debug::fmt(bt, formatter)` backtrace emission in "
    "the non-alternate arm and replace the whole else block with a simple "
    "Display delegation, e.g. `std::fmt::Display::fmt(self, formatter)?`; "
    "keep the alternate arm `unsafe { ErrorImpl::debug(self.inner.by_ref(), "
    "formatter) }` unchanged, and leave src/fmt.rs::debug as alternate → "
    "`core::fmt::Debug::fmt(err, f)` else → `core::fmt::Display::fmt(err, f)` "
    "with no backtrace or manual cause-chain handling."
)
# Verbatim from the sibling record `src/fmt.rs::*impl*::ErrorImpl` (rounds=1).
SIBLING_GUIDANCE = (
    "Keep the Debug delegation only for the alternate formatter and make the "
    "normal Debug arm a pure Display forward. In the `if f.alternate()` arm "
    "obtain `err` with `unsafe { Self::error(this) }` and call "
    "`core::fmt::Debug::fmt(err, f)` and finish with `Ok(())`; in the `else` "
    "arm obtain `err` once with `unsafe { Self::error(this) }`, do "
    "`core::fmt::Display::fmt(err, f)?; Ok(())` and remove the `while let "
    "Some(e) = source` “Caused by” loop and the `Self::backtrace(this)` / "
    "`format!(\"{:?}\", bt)` printing entirely from the non-alternate path."
)
# The head of an earlier round of the same pair record, as the run log's
# honoured line quoted it — the negated-verb phrasing of the same removal.
EARLIER_ROUND_HEAD = ("In the `debug` impl the non-alternate arm must not "
                      "emit a `Debug` backtrace and must not mention causes.")

# What the pair's two convicting tests assert on `format!("{:?}", err)`
# (`starts_with("outer")`, `contains("\n\nCaused by:")`, `contains("inner")`,
# `contains("Stack backtrace")`), rendered the way `pinned_observables`
# renders a pinned literal: the quoted source token verbatim, escapes and
# all, comma-joined.
PINS = '"outer", "\\n\\nCaused by:", "inner", "Stack backtrace"'
# The same observables written as the assertion forms the tests use. Only
# the quoted literals are read either way.
PINS_AS_FORMS = ('format!("{:?}", err) starts_with("outer") '
                 'contains("\\n\\nCaused by:") contains("Stack backtrace") '
                 'contains("inner")')

KEEPS_THE_OBSERVABLE = (
    "Gate the backtrace section on `status() == BacktraceStatus::Captured` "
    "and write `Stack backtrace:` before it")
REMOVES_SOMETHING_ELSE = "Remove the unused `use core::fmt::Write` import"

LAYOUT = """
pub struct Error {
    inner: Own<ErrorImpl>,
}
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    backtrace: Option<Backtrace>,
    _object: E,
}
"""
CONVICTING = ConvictionEvidence(frozenset({
    "test_debug_with_captured_backtrace_and_cause",
    "test_debug_with_captured_backtrace_simple"}))
MEMBERS = frozenset({"src/error.rs::fmt", "src/fmt.rs::debug"})
BODY_SHA = "6da1a44d8e31e5dc"
# Four of the ten siblings the reverted write reddened, as the record
# stamps them; the read below is the same transaction, so this list is
# shape, not the rule's input.
REGRESSED = ("debug_for_anyhow_literal", "message_error_debug_with_string",
             "test_debug", "wrapper_test_anyhow_literal_empty")


def _pair_record(guidance: str = PAIR_GUIDANCE) -> dict[str, Any]:
    """The live record's shape: compiled (`error_count` 0), convicted by the
    two backtrace tests, written from a regression, joint members and body
    sha stamped, no stub callees, not exhausted."""
    return {
        "category": "WRONG_TYPE_CHOICE", "guidance": guidance, "signal": "ok",
        "retrospective_count": 4, "error_count": 0, "failing_test_count": 10,
        "fully_exhausted": False,
        "convicted_by": sorted(CONVICTING.tests),
        "regressed": list(REGRESSED),
        "blocked_on": [],
        JOINT_MEMBERS: sorted(MEMBERS),
        JOINT_BODY_SHA: BODY_SHA,
    }


def _same_transaction_read(rec: dict[str, Any], *,
                           pins: str = "") -> GuidanceScreening:
    """The pair route's read of its own record: same member set, same kept
    body sha — the read that exempts `regression_scoped`."""
    return screen_guidance(rec, LAYOUT, evidence=CONVICTING, cluster="stable",
                           joint_members=MEMBERS, joint_body_sha=BODY_SHA,
                           pins=pins)


# ---------------------------------------------------------------------------
# The predicate: text against text.

def test_the_live_pair_guidance_removes_what_its_tests_pin() -> None:
    assert prescription_removes_pinned_observable(PAIR_GUIDANCE, PINS)
    assert prescription_removes_pinned_observable(PAIR_GUIDANCE, PINS_AS_FORMS)


def test_the_live_sibling_guidance_removes_what_its_tests_pin() -> None:
    assert prescription_removes_pinned_observable(SIBLING_GUIDANCE, PINS)
    assert prescription_removes_pinned_observable(SIBLING_GUIDANCE, PINS_AS_FORMS)


def test_the_negated_verb_phrasing_of_the_earlier_round_is_the_same_class() -> None:
    assert prescription_removes_pinned_observable(EARLIER_ROUND_HEAD, PINS)


def test_guidance_that_keeps_the_observable_is_not_a_contradiction() -> None:
    # Names the pinned literal, carries no removal marker in that sentence.
    assert not prescription_removes_pinned_observable(KEEPS_THE_OBSERVABLE, PINS)


def test_removing_something_the_tests_do_not_pin_is_not_a_contradiction() -> None:
    # A removal marker, no pinned word.
    assert not prescription_removes_pinned_observable(REMOVES_SOMETHING_ELSE, PINS)


def test_empty_guidance_or_empty_pins_never_fire() -> None:
    assert not prescription_removes_pinned_observable("", PINS)
    assert not prescription_removes_pinned_observable(PAIR_GUIDANCE, "")
    assert not prescription_removes_pinned_observable("", "")


def test_a_removal_whose_object_is_not_the_pinned_word_does_not_fire() -> None:
    # A crash diagnosis: the removal verb's object is the invalid pointer
    # use; the pinned word sits in the causal tail. Whole-sentence overlap
    # retired this lesson on run `22062-a20ae3a2` rounds 2–6 (pair fill
    # crash-reverted every time; RETROSPECTIVE_GUIDANCE was empty).
    causal = (
        "this removes the invalid pointer-as-value use that caused the "
        "segfault and the out-of-range backtrace formatting")
    assert not prescription_removes_pinned_observable(causal, PINS)
    # The same two clauses with no causal tail still meet — the pinned
    # word is in the object.
    assert prescription_removes_pinned_observable(
        "this removes the invalid pointer-as-value use and the out-of-range "
        "backtrace formatting", PINS)


def test_a_removal_and_a_mention_in_different_sentences_do_not_meet() -> None:
    # Sentence-scoped: the removal is about an import, the pinned words sit
    # in the next sentence, which keeps them.
    apart = (REMOVES_SOMETHING_ELSE + ". Then write `Stack backtrace:` before "
             "the frames and keep the `Caused by:` chain.")
    assert not prescription_removes_pinned_observable(apart, PINS)
    # …and the same two clauses in ONE sentence do meet.
    together = (REMOVES_SOMETHING_ELSE + " and the `Stack backtrace:` header "
                "before the frames.")
    assert prescription_removes_pinned_observable(together, PINS)


def test_sentence_boundaries_keep_method_chains_and_abbreviations_whole() -> None:
    # The live guidance holds `drop the` and the pinned words on opposite
    # sides of `self.chain().collect()` and an `e.g.`; a split on every `.`
    # would separate them. Only `;` (before "keep the alternate arm") is a
    # boundary here.
    sentences = guidance_sentences(PAIR_GUIDANCE)
    assert len(sentences) == 2
    assert "drop the manual" in sentences[0]
    assert "Caused by" in sentences[0] and "backtrace emission" in sentences[0]
    assert "e.g. `std::fmt::Display::fmt" in sentences[0]
    assert sentences[1].startswith("keep the alternate arm")
    assert guidance_sentences("first line\nsecond line") == [
        "first line", "second line"]
    assert prescription_removes_pinned_observable(
        "Drop the trailing emission, e.g. the backtrace block.", PINS)


def test_pinned_words_read_only_the_quoted_literals() -> None:
    assert pinned_words(PINS) == {"outer", "caused", "inner", "stack", "backtrace"}
    assert pinned_words(PINS_AS_FORMS) == pinned_words(PINS)
    # A literal carrying a real newline (the unescaped rendering) reads the
    # same as the source token.
    assert pinned_words('"\n\nCaused by:"') == {"caused"}
    # Turbofish and lifetime forms hold no literal; short literals and the
    # format string of a format form identify nothing.
    assert pinned_words(
        "downcast::<Box<dyn StdError + Send + Sync + 'static>>, "
        "is::<&'a str>, format!(\"{:#?}\", …), \"msg\", \"top\"") == frozenset()
    assert not prescription_removes_pinned_observable(
        PAIR_GUIDANCE, "downcast::<&str>, is::<&'a str>")
    # An escaped quote and apostrophes inside one token stay in that token.
    assert pinned_words(
        '"Condition failed: `b\\"hmm\\"[1] == b\'c\'` (109 vs 99)", '
        "downcast::<&str>") == {"condition", "failed"}
    # Stopwords and short words are not observables.
    assert pinned_words('"the error value", "by bt vs"') == frozenset()
    assert pinned_words("") == frozenset()


def test_pins_rendered_by_pinned_observables_feed_the_predicate() -> None:
    # The helper-asserted shape `pinned_observables` reads: the literal is
    # the LAST argument of an assertion call. Rendered by the real function,
    # not hand-written, so the predicate is checked against its output.
    src = '''
#[test]
fn test_debug_with_captured_backtrace_and_cause() {
    let s = format!("{:?}", inner.context("outer"));
    assert!(s.contains("\\n\\nCaused by:"), "\\n\\nCaused by:");
    assert!(s.contains("Stack backtrace"), "Stack backtrace");
}
'''
    pins = pinned_observables(src)
    assert '"Stack backtrace"' in pins and '"\\n\\nCaused by:"' in pins
    assert pinned_words(pins) == {"caused", "stack", "backtrace"}
    assert prescription_removes_pinned_observable(PAIR_GUIDANCE, pins)
    assert prescription_removes_pinned_observable(SIBLING_GUIDANCE, pins)
    assert not prescription_removes_pinned_observable(KEEPS_THE_OBSERVABLE, pins)


def test_removal_sentence_is_the_prescription_not_the_premise() -> None:
    assert removal_sentence(SIBLING_GUIDANCE).startswith("Ok(())` and remove")
    assert removal_sentence(KEEPS_THE_OBSERVABLE) == ""
    assert removal_sentence("") == ""


# ---------------------------------------------------------------------------
# The stamp at write.

def test_stamp_marks_a_lesson_that_removes_a_pinned_observable() -> None:
    prov = RetroProvenance.current("stable", LAYOUT)
    rec = stamp_conviction(_pair_record(), prov, CONVICTING,
                           BlockedOnStubs(frozenset()),
                           regressed=REGRESSED, pins=PINS)
    assert rec[CONTRADICTS_TARGET] is True
    # The rest of the stamp is unchanged by the new argument.
    assert rec["convicted_by"] == sorted(CONVICTING.tests)
    assert rec["regressed"] == sorted(REGRESSED)
    assert rec["blocked_on"] == []
    sibling = stamp_conviction(_pair_record(SIBLING_GUIDANCE), prov,
                               CONVICTING, regressed=REGRESSED, pins=PINS)
    assert sibling[CONTRADICTS_TARGET] is True


def test_stamp_leaves_the_key_absent_for_a_causal_tail_crash_diagnosis() -> None:
    # The rounds 2–6 pair record of run `22062-a20ae3a2`: remove the
    # pointer-as-value use because it garbled the pinned field. Pre-fix
    # the stamp was True and the next prompt dropped the diagnosis.
    causal = (
        "this removes the invalid pointer-as-value use that caused the "
        "segfault and the out-of-range backtrace formatting")
    prov = RetroProvenance.current("stable", LAYOUT)
    rec = stamp_conviction(_pair_record(causal), prov, CONVICTING,
                           BlockedOnStubs(frozenset()),
                           regressed=REGRESSED, pins=PINS)
    assert CONTRADICTS_TARGET not in rec
    verdict = _same_transaction_read(rec, pins=PINS)
    assert verdict.retired_by != CONTRADICTS_TARGET
    assert verdict.text == causal


def test_stamp_leaves_the_key_absent_for_a_lesson_that_keeps_the_observable() -> None:
    prov = RetroProvenance.current("stable", LAYOUT)
    rec = stamp_conviction(_pair_record(KEEPS_THE_OBSERVABLE), prov,
                           CONVICTING, regressed=REGRESSED, pins=PINS)
    assert CONTRADICTS_TARGET not in rec
    other = stamp_conviction(_pair_record(REMOVES_SOMETHING_ELSE), prov,
                             CONVICTING, regressed=REGRESSED, pins=PINS)
    assert CONTRADICTS_TARGET not in other


def test_negative_control_a_stamp_without_pins_leaves_no_mark() -> None:
    # The pre-fix write: every caller before the argument existed. The
    # record is written exactly as before — no key, same other stamps.
    prov = RetroProvenance.current("stable", LAYOUT)
    before = stamp_conviction(_pair_record(), prov, CONVICTING,
                              BlockedOnStubs(frozenset()), regressed=REGRESSED)
    assert CONTRADICTS_TARGET not in before
    assert before["regressed"] == sorted(REGRESSED)
    assert before["convicted_by"] == sorted(CONVICTING.tests)
    assert before[GUIDANCE_FROM_HOLLOW_TREE] is False


def test_a_re_stamp_with_new_guidance_clears_a_stale_mark() -> None:
    # A record is re-stamped with new guidance every round; the mark is a
    # judgement of THIS round's text, never carried over from the last.
    prov = RetroProvenance.current("stable", LAYOUT)
    rec = _pair_record(KEEPS_THE_OBSERVABLE)
    rec[CONTRADICTS_TARGET] = True
    again = stamp_conviction(rec, prov, CONVICTING, regressed=REGRESSED,
                             pins=PINS)
    assert CONTRADICTS_TARGET not in again


# ---------------------------------------------------------------------------
# The screen at prompt-build.

def test_negative_control_the_pre_fix_read_honours_the_live_record() -> None:
    # No stamp, no pins: the same-transaction read passes `regression_scoped`
    # (exempt), the premise rules (a joint body sha is a premise) and the
    # evidence rule (its convictors still convict), and hands the removal to
    # the next prompt whole. This is the read run `3003-93e9a9bf` took.
    verdict = _same_transaction_read(_pair_record())
    assert verdict.retired_by == ""
    assert verdict.text == PAIR_GUIDANCE
    assert admissible_guidance(
        _pair_record(), LAYOUT, evidence=CONVICTING, cluster="stable",
        joint_members=MEMBERS, joint_body_sha=BODY_SHA) == PAIR_GUIDANCE


def test_a_stamped_record_is_retired_as_contradicts_target() -> None:
    prov = RetroProvenance.current("stable", LAYOUT)
    rec = stamp_conviction(_pair_record(), prov, CONVICTING,
                           regressed=REGRESSED, pins=PINS)
    verdict = _same_transaction_read(rec)
    assert verdict == GuidanceScreening("", CONTRADICTS_TARGET)
    assert admissible_guidance(
        rec, LAYOUT, evidence=CONVICTING, cluster="stable",
        joint_members=MEMBERS, joint_body_sha=BODY_SHA) == ""


def test_an_unstamped_record_is_judged_live_when_pins_are_passed() -> None:
    # A record stored before the stamp existed is screened by the same
    # predicate against the pins of this prompt-build.
    verdict = _same_transaction_read(_pair_record(), pins=PINS)
    assert verdict == GuidanceScreening("", CONTRADICTS_TARGET)
    assert admissible_guidance(
        _pair_record(), LAYOUT, evidence=CONVICTING, cluster="stable",
        joint_members=MEMBERS, joint_body_sha=BODY_SHA, pins=PINS) == ""
    # Pins the guidance does not remove anything of leave the read as before.
    kept = _same_transaction_read(_pair_record(), pins='"realloc test"')
    assert kept.retired_by == ""
    assert kept.text == PAIR_GUIDANCE


def test_an_unstamped_record_is_judged_live_when_only_observables_are_passed() -> None:
    # The one-argument `contains("…")` shape: pins extraction empty, literals
    # in observables. Production screen sites pass both; this is the live
    # re-judgment that path must perform.
    verdict = screen_guidance(
        _pair_record(), LAYOUT, evidence=CONVICTING, cluster="stable",
        joint_members=MEMBERS, joint_body_sha=BODY_SHA,
        pins="", observables={"Stack backtrace", "\n\nCaused by:"})
    assert verdict == GuidanceScreening("", CONTRADICTS_TARGET)
    assert admissible_guidance(
        _pair_record(), LAYOUT, evidence=CONVICTING, cluster="stable",
        joint_members=MEMBERS, joint_body_sha=BODY_SHA,
        pins="", observables={"Stack backtrace", "\n\nCaused by:"}) == ""


def test_a_lesson_that_keeps_the_observable_passes_the_screen_with_pins() -> None:
    verdict = _same_transaction_read(_pair_record(KEEPS_THE_OBSERVABLE),
                                     pins=PINS)
    assert verdict.retired_by != CONTRADICTS_TARGET
    assert verdict.text == KEEPS_THE_OBSERVABLE


def test_the_rule_is_judged_right_after_hollow_tree() -> None:
    # A hollow-tree record keeps its own class; every later rule yields to
    # the contradiction.
    hollow = _pair_record()
    hollow[GUIDANCE_FROM_HOLLOW_TREE] = True
    assert _same_transaction_read(hollow, pins=PINS).retired_by == "hollow_tree"
    # A per-half read of the same record (no member set) would retire it as
    # `regression_scoped`; with the stamp, the contradiction is named first.
    stamped = _pair_record()
    stamped[CONTRADICTS_TARGET] = True
    per_half = screen_guidance(stamped, LAYOUT, evidence=CONVICTING,
                               cluster="stable")
    assert per_half.retired_by == CONTRADICTS_TARGET
    unstamped = screen_guidance(_pair_record(), LAYOUT, evidence=CONVICTING,
                                cluster="stable")
    assert unstamped.retired_by == "regression_scoped"


def test_retired_line_names_the_class_and_quotes_the_removal_sentence() -> None:
    rec = _pair_record(SIBLING_GUIDANCE)
    rec[CONTRADICTS_TARGET] = True
    verdict = _same_transaction_read(rec)
    line = retired_guidance_line("src/fmt.rs::*impl*::ErrorImpl", rec, verdict)
    assert line.startswith("[phase-c/retro-retired] src/fmt.rs::*impl*::ErrorImpl:")
    assert CONTRADICTS_TARGET in line
    assert "test_debug_with_captured_backtrace_and_cause" in line
    # The sentence that removes, not the record's opening premise.
    assert "and remove the `while let Some(e) = source`" in line
    assert "Keep the Debug delegation only" not in line
