"""A served unit nothing owes, opened before under the same own body and the
same red-test words, leaves the frontier; every unit the climb needs stays.

Run `3076-051d385a` iter 3 (2026-10-13, 148/151): six of twelve batch slots
went to served units nothing owed. Their file-wide evidence fingerprint had
moved for three of them only because sibling bodies landed in the same file;
computed on the iter-2 and iter-3 checkpoints (`91cbe27c3e` against
`9b70ed8410`), `error.rs::drop`, `error.rs::construct_from_std` and
`ptr.rs::cast` have the same own body while their files moved, and the
three red tests' words are identical across the two sweeps.

Asserted, on invented shapes: (1) the per-unit fingerprint does not move on a
sibling's landing and does move on the unit's own body or its tests' words;
the file-wide fingerprint moves on the sibling (the negative control);
(2) only an OTHER, served unit that was asked under its current fingerprint
leaves; a held stub, an owed unit, a crash site and a NEVER-served unit
stay even when `asked` lists them (the climb's layer fill); (3) an empty
fingerprint is never "asked".
"""
from __future__ import annotations

from pipeline_core.domain.candidate_tiers import AskLedger
from pipeline_core.domain.dispatch_rank import (
    Crash,
    Service,
    UnitFacts,
    asked_unchanged,
)
from pipeline_core.domain.evidence_premise import (
    evidence_fingerprints,
    unit_ask_fingerprints,
)
from pipeline_core.domain.models import FnLocator

_U = FnLocator("src/m.rs", "pick")
_SIB = FnLocator("src/m.rs", "other")
_WORDS = {_U: ("thread 'red' (11) panicked at tests/m.rs:4:5:\nassertion failed",)}


def _recs(own: str, sib: str) -> list[dict[str, str]]:
    return [{"file": "src/m.rs", "fn_name": "pick", "body_text": own},
            {"file": "src/m.rs", "fn_name": "other", "body_text": sib}]


def test_the_own_fingerprint_ignores_a_sibling_landing() -> None:
    before = unit_ask_fingerprints([_U], _recs("a()", "b()"), words=_WORDS)[_U]
    after_sib = unit_ask_fingerprints([_U], _recs("a()", "c()"), words=_WORDS)[_U]
    assert before and before == after_sib
    # The unit's own body, or what its red tests say, moves it.
    assert unit_ask_fingerprints([_U], _recs("z()", "b()"), words=_WORDS)[_U] != before
    moved = {_U: ("thread 'red' (11) panicked at tests/m.rs:5:5:\nassertion failed",)}
    assert unit_ask_fingerprints([_U], _recs("a()", "b()"), words=moved)[_U] != before
    # libtest's thread id is noise, not evidence.
    noisy = {_U: ("thread 'red' (99) panicked at tests/m.rs:4:5:\nassertion failed",)}
    assert unit_ask_fingerprints([_U], _recs("a()", "b()"), words=noisy)[_U] == before
    # Negative control: the file-wide identity moves on the sibling.
    assert (evidence_fingerprints([_U], _recs("a()", "b()"))[_U]
            != evidence_fingerprints([_U], _recs("a()", "c()"))[_U])


def _facts() -> dict[str, UnitFacts]:
    return {
        "served_other": UnitFacts(static_index=0, service=Service.SERVED),
        "flat_other": UnitFacts(static_index=1, service=Service.FLAT),
        "held": UnitFacts(static_index=2, service=Service.NEVER, held_tests=1),
        "owed": UnitFacts(static_index=3, service=Service.SERVED, owed_tier=0),
        "crash": UnitFacts(static_index=4, service=Service.SERVED,
                           crash=Crash.BLAMED),
        "never": UnitFacts(static_index=5, service=Service.NEVER),
        "fresh_other": UnitFacts(static_index=6, service=Service.SERVED),
    }


def test_only_served_units_nothing_owes_leave() -> None:
    facts = _facts()
    asked = [k for k in facts if k != "fresh_other"]
    assert asked_unchanged(facts, asked) == {"served_other", "flat_other"}
    # Negative control: nothing asked, nothing leaves (today's rank).
    assert asked_unchanged(facts, []) == frozenset()


def test_an_empty_fingerprint_is_never_asked() -> None:
    led = AskLedger(oracle_fp="o")
    assert not led.record_unit("src/m.rs::pick", "")
    assert not led.unit_asked("src/m.rs::pick", "")
    assert led.record_unit("src/m.rs::pick", "f1")
    again = AskLedger.from_dict(led.to_dict())
    assert again.unit_asked("src/m.rs::pick", "f1")
    assert not again.unit_asked("src/m.rs::pick", "f2")
    # A moved oracle is a different question set.
    assert not again.for_oracle("o2").unit_asked("src/m.rs::pick", "f1")
