"""The evidence premise: what a unit was TOLD, as a comparable identity.

`body_ledger.flat_standing_unchanged`'s first three clauses all describe the
world AROUND a unit, and on a stalled tree none of them moves — so a FLAT
grade scoped to those alone retires a unit that lands a compiling, non-paying
body permanently. These tests pin the fourth channel's own rules; the clause
that reads it is pinned in `test_body_ledger.py` and its wiring into the
driver in `test_tdd_ledger_wiring.py`.

Every assertion here has its control in the same test: the same computation
over the evidence a fill would actually have moved, producing a different
identity, and over evidence nothing observed, producing none.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.domain.evidence_premise import (
    evidence_fingerprint,
    evidence_fingerprints,
    file_context_text,
    retro_evidence_text,
)
from pipeline_core.domain.models import FnLocator

# The three units run `837207-ce5b1647` (2026-09-15) excluded, and the file
# whose bodies moved under them between the grade and the exclusion: iter 1
# landed `src/fmt.rs::debug` (`5279ba6bdc3c25cb`), iter 2 landed
# `src/error.rs::context` (`78f67290fd15864e`) and `src/error.rs::fmt`
# (`834f31f22906674e`), and from iter 3 the sweep read `dispatching 0 of 0`
# against `frontier=3 fn(s)`.
_FMT = FnLocator("src/error.rs", "fmt")
_MSG = FnLocator("src/error.rs", "msg")
_DEBUG = FnLocator("src/fmt.rs", "debug")


def _recs(**bodies: str) -> list[dict[str, Any]]:
    """Extractor records for `src/error.rs`, one per named fn."""
    return [{"file": "src/error.rs", "fn_name": name, "body_text": body,
             "sig_text": f"fn {name}(&self)"}
            for name, body in bodies.items()]


def test_a_sibling_landing_moves_the_file_the_prompt_quotes() -> None:
    """THE CHANNEL THAT ACTUALLY MOVES ON A STALLED TREE. `src/error.rs::fmt`
    was graded flat on the body the loop KEPT, so its own body cannot move
    while the grade stands (`standing_on_tree` pins it). What moved between
    the dispatch that graded it and the iteration that excluded it was the
    file: `context` landed a body in it. The prompt quotes that file — the
    per-fn route annotates the whole view, the per-impl route renders the
    siblings, file-first sends it — so this is a different question.

    NEGATIVE CONTROL: the same file with only `fmt`'s own body re-formatted
    is the same evidence, because a whitespace fold is what the identity is
    taken over."""
    at_grade = _recs(fmt="write!(f, \"{}\", self.0)", msg="None",
                     context="unimplemented!()")
    after_sibling = _recs(fmt="write!(f, \"{}\", self.0)", msg="None",
                          context="Self::construct(c, e)")
    before = evidence_fingerprints([_FMT, _MSG], at_grade)
    after = evidence_fingerprints([_FMT, _MSG], after_sibling)
    assert before[_FMT] and after[_FMT]
    assert before[_FMT] != after[_FMT], (
        "a sibling landing a body is a different prompt for every unit of "
        "the file")
    # ...and for every unit of that file, which is what releases a unit
    # whose own grade is several runs old (`src/error.rs::msg` carried a
    # flat row from run `200802-b835274e`).
    assert before[_MSG] != after[_MSG]
    reformatted = _recs(fmt="write!(f,   \"{}\",\n  self.0)", msg="None",
                        context="unimplemented!()")
    assert evidence_fingerprints([_FMT], reformatted)[_FMT] == before[_FMT]


def test_a_units_evidence_is_scoped_to_its_own_file() -> None:
    """A landing in ANOTHER file is not this unit's prompt. `src/fmt.rs::
    debug` and `src/error.rs::fmt` are the pair the run kept re-writing, and
    they are two prompts: the file each route quotes is the unit's own."""
    recs = _recs(fmt="a", msg="b") + [
        {"file": "src/fmt.rs", "fn_name": "debug", "body_text": "c",
         "sig_text": "fn debug(&self)"}]
    moved = _recs(fmt="a", msg="b") + [
        {"file": "src/fmt.rs", "fn_name": "debug", "body_text": "c2",
         "sig_text": "fn debug(&self)"}]
    before = evidence_fingerprints([_FMT, _DEBUG], recs)
    after = evidence_fingerprints([_FMT, _DEBUG], moved)
    assert before[_DEBUG] != after[_DEBUG]
    assert before[_FMT] == after[_FMT], (
        "src/fmt.rs moving is not a new question for a src/error.rs unit")


def test_the_retro_row_is_evidence_and_so_is_the_crash_excerpt() -> None:
    """The other two channels move a premise on their own.

    `retro` is the `RETROSPECTIVE_GUIDANCE` channel AND the cross-fill
    `LAST_ERROR` one (the pair route builds its next LAST_ERROR from
    `last_compile_error` / `last_error_preview` / `last_failed_body` on the
    same record). On run `837207-ce5b1647` the pair record's
    `retrospective_count` walked 49 -> 61 under four different categories
    while the tree stood still. `crash` is the one channel that arrives
    with no test convicting the unit at all, so it must be able to move a
    premise by itself."""
    recs = _recs(fmt="a")
    base = evidence_fingerprints([_FMT], recs)[_FMT]
    with_retro = evidence_fingerprints(
        [_FMT], recs, retro={_FMT: "round 49: pass the wrapper"})[_FMT]
    next_round = evidence_fingerprints(
        [_FMT], recs, retro={_FMT: "round 50: pass the inner pointer"})[_FMT]
    with_crash = evidence_fingerprints(
        [_FMT], recs, crash={_FMT: "heap-use-after-free in by_ref"})[_FMT]
    assert len({base, with_retro, next_round, with_crash}) == 4
    # A channel's name is part of the identity: the same text arriving as a
    # crash excerpt and as a stored lesson is two different premises.
    assert (evidence_fingerprints([_FMT], recs, retro={_FMT: "x"})[_FMT]
            != evidence_fingerprints([_FMT], recs, crash={_FMT: "x"})[_FMT])


def test_nothing_observed_yields_no_fingerprint() -> None:
    """The load-bearing empty return. A caller that could read none of the
    channels has not observed that the question is unchanged — it has
    observed nothing — and the gate must refuse rather than lock (the
    pass-81 readmission deadlock shape). A unit whose file the records do
    not carry is exactly that caller: it is what an extractor read rooted at
    `src/` looks like, where `file` is bare `error.rs` and joins to nothing.

    NEGATIVE CONTROL: the same unit with any one channel present has an
    identity."""
    assert evidence_fingerprint() == ""
    assert evidence_fingerprints([_FMT], [])[_FMT] == ""
    mis_rooted = [{"file": "error.rs", "fn_name": "fmt", "body_text": "a"}]
    assert evidence_fingerprints([_FMT], mis_rooted)[_FMT] == ""
    assert evidence_fingerprints(
        [_FMT], mis_rooted, retro={_FMT: "a lesson"})[_FMT] != ""
    # A record with no `file` is skipped rather than filed under "".
    assert file_context_text([{"fn_name": "fmt", "body_text": "a"}]) == {}


def test_the_file_context_keeps_every_occurrence_and_no_ordering() -> None:
    """`src/context.rs::source` is two impls, so a file's text is two bodies
    the prompt renders — collapsing them would hide one impl's rewrite. And
    the extractor's enumeration order must not move the identity: the pairs
    are sorted."""
    a = [{"file": "src/context.rs", "fn_name": "source", "body_text": "None"},
         {"file": "src/context.rs", "fn_name": "source",
          "body_text": "Some(&self.0)"}]
    one = [{"file": "src/context.rs", "fn_name": "source",
            "body_text": "None"}]
    assert file_context_text(a) != file_context_text(one)
    assert file_context_text(a) == file_context_text(list(reversed(a)))


def test_the_retro_text_is_order_free_and_survives_an_unserializable_row(
) -> None:
    """A unit carries several store rows — one per container, plus any joint
    record that names it — so the rendering sorts by key and dumps each row
    with sorted fields. `default=str` is why a row holding a value `json`
    cannot encode costs a fingerprint that moves (one re-dispatch) instead
    of an exception on the dispatch path."""
    rows = {"src/error.rs::fmt::DebugforError": {"rounds": 1, "guidance": "A"},
            "src/error.rs::fmt::DisplayforError": {"guidance": "B", "rounds": 2}}
    assert retro_evidence_text(rows) == retro_evidence_text(
        {k: rows[k] for k in reversed(list(rows))})
    assert retro_evidence_text({}) == ""
    odd = retro_evidence_text({"k": {"guidance": object()}})
    assert odd and "guidance" in odd
    # A row that is not a mapping at all renders nothing rather than raising.
    assert retro_evidence_text({"k": "corrupt"}) == ""  # type: ignore[dict-item]


def test_the_retro_text_digests_only_what_a_prompt_can_carry() -> None:
    """Run `1158170-ce307ee7`: 13 retired and 9 withheld retro lines against 5
    honoured, so a fingerprint over whole rows could release a flat unit
    onto a byte-identical prompt. A counter or a category moving is not new
    evidence; the guidance or a LAST_ERROR field is. A hollow-tree lesson is
    never shown. Control: the guidance changing moves the text."""
    base = {"k": {"guidance": "use X", "round": 1, "category": "A"}}
    bumped = {"k": {"guidance": "use X", "round": 2, "category": "B",
                    "retrospective_count": 5}}
    assert retro_evidence_text(base) == retro_evidence_text(bumped)
    hollow = {"k": {"guidance": "use Y", "guidance_from_hollow_tree": True}}
    assert retro_evidence_text(hollow) == ""
    assert retro_evidence_text({"k": {"guidance": "use Z"}}) != \
        retro_evidence_text(base)
    assert "E0277" in retro_evidence_text(
        {"k": {"last_compile_error": "error[E0277]"}})
