"""Which functions no test can execute, proved from rustc's MIR and public index.

`domain.fn_reachability` is pure: it reads the text of `-Zunpretty=mir` and of
rustdoc's JSON for one cluster world. The fixture crate
`tests/fixtures/fn_reachability` carries one case per claim, with its negative
control, and `expected/` holds the two compiler outputs taken from it, so these
tests run with no cargo (the integration test regenerates both and asserts the
same verdicts, which pins the dump's shape).

Each rule that keeps a function testable has a mutation control here: change
the one fact the rule reads and the function is proved, so the rule — not the
fixture's shape — is what keeps it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.fn_execution import site_label
from pipeline_core.domain.fn_reachability import (
    PublicSurface,
    Verdict,
    defined_type_names,
    has_word,
    idents,
    macro_idents,
    parse_mir,
    public_surface,
    read_world,
)
from pipeline_core.domain.models import FnRecord, FnSite

_REPO = Path(__file__).resolve().parents[1]
_FIX = _REPO / "tests" / "fixtures" / "fn_reachability"
_EXPECTED = json.loads((_FIX / "expected" / "verdicts.json").read_text(encoding="utf-8"))
_BIN = _REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"


def _records() -> list[FnRecord]:
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    from pipeline_core.adapters.extractor import CliExtractor
    rows = CliExtractor(_BIN).enumerate_fns(_FIX) or []
    return [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r))
            for r in rows if r.file.startswith("src/")]


def _texts() -> dict[str, str]:
    return {"src/lib.rs": (_FIX / "src" / "lib.rs").read_text(encoding="utf-8")}


def _mir() -> str:
    return (_FIX / "expected" / "mir.txt").read_text(encoding="utf-8")


def _surface() -> PublicSurface:
    surface = public_surface((_FIX / "expected" / "rustdoc.json").read_text(encoding="utf-8"))
    assert surface is not None
    return surface


def _read(*, mir: str | None = None, surface: PublicSurface | None = None,
          texts: dict[str, str] | None = None) -> dict[str, Verdict]:
    got = read_world(_records(), mir_text=_mir() if mir is None else mir,
                     surface=surface or _surface(),
                     file_texts=texts or _texts())
    return {site_label(s): v for s, v in got.items()}


def test_the_fixture_proves_exactly_the_functions_it_was_built_to() -> None:
    got = _read()
    assert sorted(label for label, v in got.items() if v.proof) == sorted(_EXPECTED["flagged"])
    for label, why in _EXPECTED["kept"].items():
        assert not got[label].proof, label
        assert why in got[label].kept, (label, got[label].kept)


def test_the_proof_names_the_facts_that_held() -> None:
    proof = _read()["src/lib.rs::clone [impl Clone for Token]"].proof
    assert "`Token` is not nameable" in proof and "<Token as Clone>::clone" in proof


# --- the mutation controls: one fact changed, one function proved -------------

def test_control_a_call_is_what_keeps_a_clone() -> None:
    mutated = _mir().replace("<Called as Clone>::clone", "<Called as Other>::other")
    assert _read(mir=mutated)["src/lib.rs::clone [impl Clone for Called]"].proof


def test_control_an_unsize_coercion_is_what_keeps_a_vtable_impl() -> None:
    mutated = _mir().replace("PointerCoercion(Unsize", "PointerCoercion(Other")
    got = _read(mir=mutated)
    assert got["src/lib.rs::fmt [impl fmt::Debug for Inner]"].proof
    assert got["src/lib.rs::fmt [impl fmt::Debug for Sq]"].proof


def test_control_a_provided_method_use_is_what_keeps_a_required_one() -> None:
    mutated = _mir().replace("std::fmt::Write>::write_fmt", "std::fmt::Other>::other_fmt")
    assert _read(mir=mutated)["src/lib.rs::write_str [impl fmt::Write for Sink]"].proof


def test_control_a_generic_flow_is_what_keeps_the_cloned_type() -> None:
    mutated = _mir().replace("dup_generic::<Boxed>", "dup_generic::<u32>")
    assert _read(mir=mutated)["src/lib.rs::clone [impl Clone for Boxed]"].proof


def test_control_a_public_signature_is_what_keeps_a_handed_out_type() -> None:
    s = _surface()
    hidden = PublicSurface(s.nameable - {"Leaked"}, s.exposed - {"Leaked"}, s.fn_spans, s.opaque_return)
    assert _read(surface=hidden)["src/lib.rs::clone [impl Clone for Leaked]"].proof


def test_control_the_public_index_is_what_keeps_a_hidden_fn() -> None:
    s = _surface()
    unlisted = PublicSurface(s.nameable, s.exposed, {}, s.opaque_return)
    got = _read(surface=unlisted)
    assert got["src/lib.rs::hidden_api"].proof
    # A `pub` method on a type an outside crate can hold stays testable on its
    # own, whether or not the index lists it.
    assert not got["src/lib.rs::value [impl Api]"].proof


def test_control_a_macro_body_is_what_keeps_a_private_fn() -> None:
    text = _texts()["src/lib.rs"].replace("macro_named()\n    };", "()\n    };")
    assert _read(texts={"src/lib.rs": text})["src/lib.rs::macro_named"].proof


def test_an_opaque_return_in_the_public_surface_disables_every_trait_impl_proof() -> None:
    s = _surface()
    opaque = PublicSurface(s.nameable, s.exposed, s.fn_spans, True)
    got = _read(surface=opaque)
    assert not got["src/lib.rs::clone [impl Clone for Token]"].proof
    assert "impl Trait" in got["src/lib.rs::clone [impl Clone for Token]"].kept
    # An inherent method and a free fn read no trait, so they are still proved.
    assert got["src/lib.rs::dead_helper"].proof


def test_a_record_the_dump_does_not_define_is_never_proved() -> None:
    """A fn compiled out of the dumped world (a cfg the dump was not taken
    under) has no body in the MIR; its absence from every use is vacuous."""
    mutated = "\n".join(
        ln for ln in _mir().splitlines() if "dead_helper" not in ln)
    kept = _read(mir=mutated)["src/lib.rs::dead_helper"]
    assert not kept.proof and "does not define it" in kept.kept


def test_nothing_is_proved_without_a_surface_or_a_dump() -> None:
    assert read_world(_records(), mir_text="", surface=_surface(), file_texts=_texts()) == {}
    assert read_world(_records(), mir_text=_mir(), surface=None, file_texts=_texts()) == {}
    assert public_surface("not json") is None and public_surface("{}") is None


def test_a_site_two_records_share_is_proved_only_when_each_is() -> None:
    """Two cfg twins of one site: one the dump defines and proves, one it does
    not define (compiled out of this world). The site is not proved."""
    recs = _records()
    token = next(r for r in recs if r.fn_name == "clone" and "Token" in r.container)
    twin = FnRecord.from_dict({**token.to_dict(), "sig_byte_start": 0})
    assert record_site(twin) == record_site(token)
    alone = read_world([token], mir_text=_mir(), surface=_surface(), file_texts=_texts())
    both = read_world([token, twin], mir_text=_mir(), surface=_surface(), file_texts=_texts())
    assert alone[record_site(token)].proof
    assert not both[record_site(token)].proof


# --- the small readers --------------------------------------------------------

def test_tokens_and_words() -> None:
    assert idents("<Own<T> as Clone>::clone(_1)") == ["Own", "T", "as", "Clone", "clone", "_1"]
    assert has_word("fn Ref<'a>", "Ref") and not has_word("fn Referee", "Ref")
    assert not has_word("Prefix", "Ref") and has_word("x::Ref::y", "Ref")


def test_types_the_crate_defines_and_names_a_macro_body_spells() -> None:
    src = "struct A; enum B {} pub union C { x: u8 } fn struct_like() {}\n" \
          "macro_rules! m { () => { helper(); } }\nfn other() {}"
    assert defined_type_names([src]) == {"A", "B", "C"}
    spelled = macro_idents([src])
    assert "helper" in spelled and "other" not in spelled


def test_the_dump_splits_into_items_and_skips_allocation_tables() -> None:
    dump = ("// WARNING\nfn a() -> () {\n    bb0: {\n        return;\n    }\n}\n\n"
            "alloc1 (size: 2, align: 1) {\n    00 01\n}\n\n"
            "const K: usize = const 4_usize;\n\nfn b() -> () {\n}\n")
    heads = [b.header for b in parse_mir(dump)]
    assert heads == ["fn a() -> () {", "const K: usize = const 4_usize;", "fn b() -> () {"]


def test_a_site_label_of_the_fixture_is_the_one_the_expectation_uses() -> None:
    assert site_label(FnSite("src/lib.rs", "dead_helper", "")) == "src/lib.rs::dead_helper"
