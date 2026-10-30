"""The contradiction screen convicts sentences, not records, and a negated
removal is a preservation.

Run `3808231-81b847f7` (2026-10-29, cluster `stable`, fill 3): the pair
prompt over a Debug dispatcher and its implementer went out with no
retrospective guidance. The stored diagnosis (below, verbatim) opened with
the fix the reply then missed, and its fourth sentence said "so the first
cause is not dropped and the “Caused by” output contains ... “outer ctx”,
“level 2”". The removal predicate read `dropped` as a removal whose object
held the pinned words, and the screen retired the whole record on that one
sentence. The negative controls are the pre-fix readings: the negated
sentence convicted, and one convicted sentence emptying the record."""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.retro_gate import (
    CONTRADICTS_TARGET,
    DENIES_DEFINED_NAME,
    ConvictionEvidence,
    GuidanceScreening,
    bind_crate_names,
    contradicting_sentences,
    guidance_sentences,
    prescription_removes_pinned_observable,
    retired_guidance_line,
    screen_guidance,
    without_sentences,
)

# Verbatim, prompt `4a268d0b…` (the retrospective reply the fill-3 screen
# retired).
RETIRED_LIVE = (
    "Stop using `self.inner.by_ref()` followed by `deref()` to reach the "
    "inner `ErrorImpl`; obtain a proper reference with `self.inner.as_ref()` "
    "– e.g. `let this_ref = self.inner.as_ref();` – and drive the alternate "
    "Debug arm via the unsafe primitives `Self::error`/`Self::chain`/"
    "`Self::backtrace` on that `NonNull` rather than dereferencing through a "
    "`by_ref` reference. In `ErrorImpl::debug` do not consume the chain with "
    "`let _ = chain_iter.next();` before measuring/listing it; build "
    "`causes_len` and the enumeration from the un-consumed "
    "`Self::chain(this)` iterator so the first cause is not dropped and the "
    "“Caused by” output contains the expected messages such as “outer ctx”, "
    "“level 2”, etc.")
# Verbatim, the pins line of the retrospective prompt `889b8a0b…`.
PINS_LIVE = (
    'assert_eq!(EXPECTED_ALTDEBUG_F, format!("{:#?}", …)), '
    'assert_eq!(EXPECTED_ALTDEBUG_G, format!("{:#?}", …)), '
    'assert_eq!(EXPECTED_ALTDEBUG_H, format!("{:#?}", …)) "anyhow debug '
    'cycle" "base msg" "boxed error" "debug boxed roundtrip" "dynamic '
    'message" "hello from MessageError" "inner io" "level 1" "level 2" '
    '"missing value" "outer context" "outer ctx" "simple message".')

# An invented shape, no crate symbol: one sentence keeps a pinned literal,
# one removes it.
PINS = '"Caused by:", "outer ctx"'
MIXED = ("Route the alternate arm through `Self::inner_of(this)` and format "
         "that value. Drop the “Caused by:” block from the plain arm.")

LAYOUT = "pub struct Holder { inner: u8 }\n"
EVIDENCE = ConvictionEvidence(frozenset({"t_alt"}))


def _record(guidance: str) -> dict[str, Any]:
    return {"category": "WRONG_DEREF_PATTERN", "guidance": guidance,
            "retrospective_count": 1, "error_count": 0,
            "convicted_by": ["t_alt"], "blocked_on": ["helper"]}


def _screen(rec: dict[str, Any], pins: str = PINS) -> GuidanceScreening:
    return screen_guidance(rec, LAYOUT + "fn helper() { unimplemented!() }\n",
                           evidence=EVIDENCE, cluster="stable", pins=pins)


# ---------------------------------------------------------------------------
# Negation.

def test_a_negated_removal_is_a_preservation() -> None:
    for kept in ("so the first cause is not dropped and the “Caused by:” "
                 "output contains “outer ctx”",
                 "don't drop the “Caused by:” block",
                 "the “outer ctx” line must not be removed"):
        assert not prescription_removes_pinned_observable(kept, PINS), kept


def test_markers_that_carry_their_own_negation_stay_removals() -> None:
    for removal in ("must not emit the “Caused by:” block",
                    "never print “outer ctx”",
                    "no longer contains “outer ctx”",
                    "drop the “Caused by:” block",
                    "Do not touch the import but drop the “Caused by:” block"):
        assert prescription_removes_pinned_observable(removal, PINS), removal


def test_the_live_retired_record_convicts_no_sentence() -> None:
    assert contradicting_sentences(RETIRED_LIVE, PINS_LIVE) == ()
    # The next whole-record rule still retires this record: its third
    # sentence mentions "`let _ = chain_iter.next();`" under "do not", and
    # the degenerate-body markers read the text whole. That rule stays
    # whole-record by the plan; the screen now names it, not this one.
    assert _screen(_record(RETIRED_LIVE), PINS_LIVE).retired_by == (
        "test_sacrificing_prescription")


def test_negative_control_the_pre_fix_parse_convicted_the_negated_sentence() -> None:
    # The same sentence with the negation taken out is what the old parse
    # read: a removal of the pinned words.
    last = guidance_sentences(RETIRED_LIVE)[-1]
    assert "is not dropped" in last
    assert prescription_removes_pinned_observable(
        last.replace("is not dropped", "is dropped"), PINS_LIVE)


# ---------------------------------------------------------------------------
# Per sentence.

def test_a_mixed_record_injects_the_sentences_the_rule_does_not_convict() -> None:
    verdict = _screen(_record(MIXED))
    assert verdict.retired_by == ""
    assert verdict.text.startswith(
        "Route the alternate arm through `Self::inner_of(this)`")
    assert "Drop the" not in verdict.text
    assert "1 sentence(s) of this lesson were left out" in verdict.text
    assert verdict.withheld == (
        (CONTRADICTS_TARGET, "Drop the “Caused by:” block from the plain arm"),)
    line = retired_guidance_line("k", _record(MIXED), verdict)
    assert line.startswith("[phase-c/retro-withheld] k:")
    assert "Drop the “Caused by:” block" in line


def test_negative_control_the_pre_fix_screen_emptied_the_mixed_record() -> None:
    # The pre-fix rule: any convicted sentence retires the record.
    assert prescription_removes_pinned_observable(MIXED, PINS)
    assert len(contradicting_sentences(MIXED, PINS)) < len(
        guidance_sentences(MIXED))


def test_a_record_every_sentence_of_which_is_convicted_is_retired() -> None:
    whole = "Drop the “Caused by:” block. Never print “outer ctx”."
    verdict = _screen(_record(whole))
    assert verdict == GuidanceScreening("", CONTRADICTS_TARGET)
    assert retired_guidance_line("k", _record(whole), verdict).startswith(
        "[phase-c/retro-retired] k:")


def test_the_stored_sentences_decide_when_the_build_has_no_pins() -> None:
    rec = _record(MIXED)
    rec[CONTRADICTS_TARGET] = list(contradicting_sentences(MIXED, PINS))
    verdict = _screen(rec, pins="")
    assert "Drop the" not in verdict.text
    assert verdict.text.startswith("Route the alternate arm")
    # A boolean stamp names no sentence and withholds none.
    legacy = _record(MIXED)
    legacy[CONTRADICTS_TARGET] = True
    assert _screen(legacy, pins="").text.startswith(
        "Route the alternate arm through `Self::inner_of(this)` and format "
        "that value. Drop the")


def test_a_denial_withholds_only_its_sentence() -> None:
    bind_crate_names({"inner_of"})
    try:
        text = ("Call `Self::inner_of(this)` for the value. `helper_x` is "
                "fine. The `inner_of` accessor does not exist; write it by hand.")
        verdict = _screen(_record(text), pins="")
        assert verdict.retired_by == ""
        assert (DENIES_DEFINED_NAME, "The `inner_of` accessor does not exist") \
            in verdict.withheld
        assert "does not exist" not in verdict.text
        assert verdict.text.startswith("Call `Self::inner_of(this)`")
        only = _screen(_record("The `inner_of` accessor does not exist."),
                       pins="")
        assert only == GuidanceScreening("", DENIES_DEFINED_NAME)
    finally:
        bind_crate_names(())


def test_a_boundary_inside_a_code_span_is_not_a_sentence_end() -> None:
    text = ("Replace the arm with `let v = unsafe { f(this) }; return g(v);` "
            "so the value is formatted. Drop the “Caused by:” block.")
    assert guidance_sentences(text)[0].endswith("so the value is formatted")
    assert without_sentences(text, guidance_sentences(text)[1:]) == (
        "Replace the arm with `let v = unsafe { f(this) }; return g(v);` "
        "so the value is formatted.")
