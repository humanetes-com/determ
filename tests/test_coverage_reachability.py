"""A fill is refused when no convicting test executes the body — on the records
of the run that named the class.

The fixture is COMPUTED, not written: `tests/fixtures/coverage/
2450551_stable_owners.json` is the output of `scripts/coverage_owner_probe.py`
passes A and C against the filled `stable` tree of run `2450551-9a6a8683`
(2026-08-30), plus the verbatim conviction that run persisted for
`src/lib.rs::Ok`. Every assertion below is a sentence about that run, and the
negative controls are the pre-fix mechanisms failing to catch it:

- the DEAD SET, which is what the loop had before this change, does not contain
  `src/lib.rs::Ok` at all — it is generic and `#[inline]`, so with no caller
  there is no instantiation and no region, and a whole-suite merge cannot
  distinguish that from a body it simply did not sample;
- with the reading unbound (every run before this one), `screen_guidance`
  honours the record and hands the prompt the guidance that made
  `Ok<T>(v)` return `Err(v)`.

The over-refusal control is `src/error.rs::deref_mut`, the unit the same run
was working when this was found: both `deref_mut` tests DO enter it, so the
predicate must decline.
"""
from __future__ import annotations

import json
import pathlib

from pipeline_core.domain.coverage_slice import (
    CoverageSlice,
    bind,
    convictors_never_enter,
    unreached_by,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.retro_gate import (
    CONVICTORS_NEVER_EXECUTE,
    NO_TREE_PREMISE,
    ConvictionEvidence,
    retired_guidance_line,
    screen_guidance,
    screening_unit,
    tree_premise_absent,
)

_FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "coverage"
     / "2450551_stable_owners.json").read_text())

_OK = FnLocator("src/lib.rs", "Ok")
_DEREF_MUT = FnLocator("src/error.rs", "deref_mut")


def _loc(name: str) -> FnLocator:
    file_rel, fn = name.split("::", 1)
    return FnLocator(file_rel, fn)


def _owners() -> dict[str, frozenset[FnLocator]]:
    return {t: frozenset(_loc(n) for n in names)
            for t, names in _FIXTURE["owners"].items()}


def _unbind() -> None:
    bind(None)


# ---------------------------------------------------------------- the claim

def test_trailer_does_not_enter_the_unit_it_convicted() -> None:
    """The plan's sentence: `test_trailer` convicted `src/lib.rs::Ok` and does
    not execute it. 4,550 s over five attempts and two retrospective rounds."""
    owners = _owners()
    assert _FIXTURE["retro_record"]["convicted_by"] == ["test_trailer"]
    entered = owners["test_trailer"]
    assert _OK not in entered
    assert _loc("src/lib.rs::format_err") in entered
    assert _loc("src/lib.rs::must_use") in entered
    assert convictors_never_enter(_OK, ["test_trailer"], owners) == frozenset(
        {"test_trailer"})


def test_no_failing_test_in_the_run_enters_the_unit() -> None:
    """Not one test of nine — so the refusal does not depend on which of the
    run's failing tests attribution happened to pick."""
    owners = _owners()
    assert len(owners) == 9
    assert all(_OK not in entered for entered in owners.values())
    assert convictors_never_enter(
        _OK, sorted(owners), owners) == frozenset(owners)


# ------------------------------------------------------- negative controls

def test_the_dead_set_alone_could_not_have_caught_it() -> None:
    """The pre-fix reading, on the same tree, says nothing about this unit.

    13 bodies are dead and `src/lib.rs::Ok` is not one of them: generic and
    `#[inline]`, never instantiated, so it emits no region at all. Absence is
    not a zero count, which is why the refusal is built on the positive
    evidence of a per-test owner set."""
    dead = set(_FIXTURE["dead_bodies"])
    assert len(dead) == 13
    assert "src/lib.rs::Ok" not in dead


def test_a_legitimately_owned_unit_is_not_refused() -> None:
    """`src/error.rs::deref_mut` — the unit the same run was working when this
    was found. Both deref_mut tests enter it, so the gate must decline."""
    owners = _owners()
    convictors = ["test_deref_mut_through_error_mut",
                  "test_deref_mut_error_targets"]
    assert all(_DEREF_MUT in owners[t] for t in convictors)
    assert convictors_never_enter(_DEREF_MUT, convictors, owners) is None


# ------------------------------------------------------ conservative on None

def test_an_unmeasured_test_decides_nothing() -> None:
    owners = _owners()
    assert convictors_never_enter(
        _OK, ["test_trailer", "test_never_profiled"], owners) is None


def test_an_empty_owner_set_decides_nothing() -> None:
    """Measured-and-empty is what a CRASHED binary produces — it writes no
    profile — so it may not convict anything."""
    owners = {**_owners(), "test_crashed": frozenset()}
    assert convictors_never_enter(_OK, ["test_crashed"], owners) is None


def test_no_tests_decides_nothing() -> None:
    assert convictors_never_enter(_OK, [], _owners()) is None


def test_unbound_reading_decides_nothing() -> None:
    _unbind()
    assert unreached_by(_OK, ["test_trailer"]) is None


def test_bound_reading_answers() -> None:
    bind(CoverageSlice(owners=_owners()))
    try:
        assert unreached_by(_OK, ["test_trailer"]) == frozenset({"test_trailer"})
        assert unreached_by(_DEREF_MUT,
                            ["test_deref_mut_error_targets"]) is None
    finally:
        _unbind()


# --------------------------------------------- the record already on disk

_FILE_TEXT = """
#[allow(non_snake_case)]
#[inline]
pub fn Ok<T>(value: T) -> Result<T> {
    core::result::Result::Ok::<T, Error>(value)
}
"""


def test_the_persisted_conviction_is_retired_under_the_reading() -> None:
    """The verbatim on-disk record stops steering prompts, with no hand-edit
    of `.phase_c_logs/retrospectives.json`."""
    record = _FIXTURE["retro_record"]
    assert "Err(value)" in record["guidance"]
    assert record["fully_exhausted"] is True
    bind(CoverageSlice(owners=_owners()))
    try:
        verdict = screen_guidance(
            record, _FILE_TEXT, evidence=ConvictionEvidence(frozenset()),
            unit=_OK)
        assert verdict.text == ""
        assert verdict.retired_by == CONVICTORS_NEVER_EXECUTE
        assert retired_guidance_line(
            _FIXTURE["retro_key"], record, verdict)
    finally:
        _unbind()


def test_without_the_reading_a_record_with_named_blockers_is_honoured() -> None:
    """The eighth-rule unbound control: with nonempty `blocked_on` the ninth
    rule does not fire, and without the owner map the eighth does not fire
    either, so the guidance reaches the prompt. The live Ok record has
    `blocked_on: []` and is now the ninth rule's constituency — that case
    is pinned next to the 9933 records, not here."""
    _unbind()
    record = dict(_FIXTURE["retro_record"])
    record["blocked_on"] = ["format_err"]
    verdict = screen_guidance(
        record, _FILE_TEXT,
        evidence=ConvictionEvidence(frozenset()), unit=_OK)
    assert "Err(value)" in verdict.text
    assert verdict.retired_by == ""


def test_a_caller_that_offers_no_unit_never_fires_the_eighth_rule() -> None:
    """The impl route: its unit is a block, the owner map is keyed per fn.
    Empty `blocked_on` still retires as the ninth rule — that rule does
    not read the owner map. A copy with named blockers is the eighth-rule
    control at `unit=None`."""
    bind(CoverageSlice(owners=_owners()))
    try:
        verdict = screen_guidance(
            _FIXTURE["retro_record"], _FILE_TEXT,
            evidence=ConvictionEvidence(frozenset()))
        assert verdict.retired_by != CONVICTORS_NEVER_EXECUTE
        assert verdict.retired_by == NO_TREE_PREMISE
        named = dict(_FIXTURE["retro_record"])
        named["blocked_on"] = ["format_err"]
        kept = screen_guidance(
            named, _FILE_TEXT, evidence=ConvictionEvidence(frozenset()))
        assert kept.retired_by != CONVICTORS_NEVER_EXECUTE
        assert "Err(value)" in kept.text
    finally:
        _unbind()


def test_screening_unit_is_none_exactly_for_a_block_key() -> None:
    """File-first yields both `*impl*` and per-fn keys. Passing the block
    key as a locator would retire every block record whose convictors
    entered any crate body — `*impl*` is never in an owner set. The live
    Ok record is the per-fn key."""
    assert screening_unit("src/lib.rs", "Ok") == _OK
    assert screening_unit("src/error.rs", "deref_mut") == _DEREF_MUT
    assert screening_unit("src/error.rs", "*impl*") is None
    assert screening_unit("src/lib.rs", "") is None


def test_the_retired_line_names_the_class_and_the_convictor() -> None:
    """Silence would convert the honoured line into nothing. The next
    launch's reading is this line, not the absence of honoured."""
    record = _FIXTURE["retro_record"]
    bind(CoverageSlice(owners=_owners()))
    try:
        verdict = screen_guidance(
            record, _FILE_TEXT, evidence=ConvictionEvidence(frozenset()),
            unit=screening_unit("src/lib.rs", "Ok"))
        line = retired_guidance_line(_FIXTURE["retro_key"], record, verdict)
    finally:
        _unbind()
    assert verdict.retired_by == CONVICTORS_NEVER_EXECUTE
    assert line.startswith("[phase-c/retro-retired] src/lib.rs::Ok::")
    assert CONVICTORS_NEVER_EXECUTE in line
    assert "test_trailer" in line
    assert "never execute this body" in line


# --------------------------------------------- empty blocked_on, ninth rule

_PREMISE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "retro"
     / "9933_empty_premise.json").read_text())
_MSG = FnLocator("src/error.rs", "msg")
_CONTEXT = FnLocator("src/error.rs", "context")
_FILE_OK = "pub fn msg() {}\npub fn context() {}\n"


def test_empty_blocked_on_records_from_9933_are_retired() -> None:
    """The plan's sentence: under the ninth rule, screen_guidance on
    `src/error.rs::msg::Error` and `src/error.rs::context::Error` returns
    empty text and retired_by set. The force_capture / `::<C, Error>`
    sentences cannot enter the next prompt. Bound Ok still retires as
    convictors_never_execute (eighth first)."""
    _unbind()
    for key, loc in (("src/error.rs::msg::Error", _MSG),
                     ("src/error.rs::context::Error", _CONTEXT)):
        rec = _PREMISE[key]
        assert rec["blocked_on"] == []
        assert tree_premise_absent(rec)
        verdict = screen_guidance(
            rec, _FILE_OK, evidence=ConvictionEvidence(frozenset()),
            unit=loc)
        assert verdict.text == ""
        assert verdict.retired_by == NO_TREE_PREMISE
        line = retired_guidance_line(key, rec, verdict)
        assert NO_TREE_PREMISE in line
        assert "no tree premise" in line
    assert "force_capture" in _PREMISE["src/error.rs::msg::Error"]["guidance"]
    bind(CoverageSlice(owners=_owners()))
    try:
        bound = screen_guidance(
            _FIXTURE["retro_record"], _FILE_TEXT,
            evidence=ConvictionEvidence(frozenset()), unit=_OK)
        assert bound.retired_by == CONVICTORS_NEVER_EXECUTE
    finally:
        _unbind()


def test_a_record_with_named_blockers_is_not_the_ninth_rule() -> None:
    """`src/fmt.rs::debug::ErrorImpl` names `display`/`write_str`. The
    ninth rule is not a mute of the three checkable rows."""
    rec = dict(_PREMISE["src/fmt.rs::debug::ErrorImpl"])
    rec.pop("futility_suspended", None)
    assert rec["blocked_on"] == ["display", "write_str"]
    assert not tree_premise_absent(rec)
    verdict = screen_guidance(
        rec, "fn display() { unimplemented!() }\nfn write_str() { unimplemented!() }\n",
        evidence=ConvictionEvidence(frozenset()), cluster="")
    assert verdict.retired_by != NO_TREE_PREMISE
