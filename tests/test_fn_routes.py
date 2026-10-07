"""Which public calls reach a function no test can name, read from rustc's MIR.

`domain.fn_routes` is pure: it reads one world's `-Zunpretty=mir` text and
rustdoc's public index. The fixture crate `tests/fixtures/fn_routes` mirrors the
shapes anyhow hides its error objects behind (a pub type in a private module
built inside a crate fn, an object behind a fn-pointer vtable, an
alternate-gated `dyn` Debug call, a call made inside a closure), each with a
control; `expected/` holds the two compiler outputs taken from it, so these
tests run with no cargo (the integration test regenerates both).

Each route has a mutation control here: change the one fact the walk reads
and the route is gone, so the fact — not the fixture's shape — is what gives it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline_core.domain.fn_reachability import PublicSurface, public_surface
from pipeline_core.domain.fn_routes import (
    ROUTES_VERSION,
    FnRoutes,
    GatedDispatch,
    PublicRoute,
    TypeFact,
    WorldFacts,
    facts_from_json,
    facts_to_json,
    world_facts,
)
from pipeline_core.domain.models import FnRecord, TypeRecord

_REPO = Path(__file__).resolve().parents[1]
_FIX = _REPO / "tests" / "fixtures" / "fn_routes"
_EXPECTED = json.loads((_FIX / "expected" / "routes.json").read_text(encoding="utf-8"))
_BIN = _REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"


def _extracted() -> tuple[list[FnRecord], list[TypeRecord]]:
    if not _BIN.exists():
        pytest.skip("extractor release binary missing")
    from pipeline_core.adapters.extractor import CliExtractor
    ex = CliExtractor(_BIN)
    recs = [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r))
            for r in ex.enumerate_fns(_FIX) or []]
    types = list(ex.enumerate_types(_FIX) or [])
    return ([r for r in recs if r.file.startswith("src/")],
            [t for t in types if t.file.startswith("src/")])


def _mir() -> str:
    return (_FIX / "expected" / "mir.txt").read_text(encoding="utf-8")


def _surface() -> PublicSurface:
    s = public_surface((_FIX / "expected" / "rustdoc.json").read_text(encoding="utf-8"))
    assert s is not None
    return s


def _facts(*, mir: str | None = None) -> WorldFacts:
    recs, types = _extracted()
    return world_facts("stable", mir_text=_mir() if mir is None else mir, surface=_surface(),
                       records=recs, types=types,
                       file_texts={"src/lib.rs": (_FIX / "src" / "lib.rs").read_text(encoding="utf-8")})


def _text(facts: WorldFacts, label: str) -> str:
    return "\n".join(facts.routes[label].lines()) if label in facts.routes else ""


def test_each_target_gets_the_route_the_fixture_was_built_for() -> None:
    facts = _facts()
    for label, parts in _EXPECTED["routes"].items():
        text = _text(facts, label)
        pos = 0
        for part in parts:
            at = text.find(part, pos)
            assert at >= 0, (label, part, text)
            pos = at + len(part)
    for label in _EXPECTED["none"]:
        assert label not in facts.routes, (label, _text(facts, label))
    for label in _EXPECTED["no_gate"]:
        assert "alternate" not in _text(facts, label), label


def test_the_unnameable_types_are_the_private_modules_pub_types() -> None:
    by_name = {t.name: t for t in _facts().unnameable}
    assert set(by_name) == set(_EXPECTED["unnameable"])
    for name, part in _EXPECTED["unnameable"].items():
        assert part in by_name[name].types_line(), by_name[name].types_line()
        assert by_name[name].never_line().startswith(f"NEVER: {name}  #")


def test_the_line_names_what_it_found_and_never_says_only() -> None:
    for fr in _facts().routes.values():
        for line in fr.lines():
            assert "only by" not in line and "only through" not in line


# --- the mutation controls: one fact removed, one route gone -----------------

def test_control_the_aggregate_is_what_routes_a_built_type() -> None:
    mutated = _mir().replace("_2 = DisplayError::<M>(copy _1);", "_2 = const ZeroSized: DisplayError<M>;")
    assert "src/lib.rs::fmt [impl fmt::Debug for DisplayError<M>]" not in _facts(mir=mutated).routes


def test_control_the_vtable_field_read_is_what_routes_a_coerced_type() -> None:
    mutated = _mir().replace("((*_4).1: unsafe fn(*mut ())", "((*_4).9: unsafe fn(*mut ())")
    assert "src/lib.rs::fmt [impl fmt::Debug for ErrorImpl<E>]" not in _facts(mir=mutated).routes
    # The aggregate route of the same type is not substituted for it: a type
    # coerced to a dyn is reached through the dyn.
    assert "src/lib.rs::object_boxed" not in _facts(mir=mutated).routes


def test_a_coerced_route_quotes_the_gate_as_a_built_one_does() -> None:
    """A route of the trait, build or coercion, quotes the crate's `<dyn Trait>`
    call on the formatter-flag true edge: the dispatcher's body holds the
    branch whichever way the value reached the `dyn`. Which test entered the
    function (the boxed `{:?}` in the fixture lcov) is a separate fact."""
    from pipeline_core.domain.reach_probe import target_for
    facts = _facts()
    built = "src/lib.rs::fmt [impl fmt::Debug for DisplayError<M>]"
    coerced = "src/lib.rs::fmt [impl fmt::Debug for ErrorImpl<E>]"
    assert "only when `f.alternate()` is true" in _text(facts, built)
    text = _text(facts, coerced)
    assert "coerces `ErrorImpl` to a `dyn`" in text and "only when `f.alternate()` is true" in text
    asked = target_for(coerced, facts.routes[coerced])
    assert asked is not None and asked.observe == "alt_debug"


def test_control_the_alternate_flag_is_what_gates_the_dispatch() -> None:
    mutated = _mir().replace("Formatter::<'_>::alternate(", "Formatter::<'_>::sign_plus(")
    text = _text(_facts(mir=mutated), "src/lib.rs::fmt [impl fmt::Debug for DisplayError<M>]")
    assert "Error::from_display" in text and "alternate" not in text


def test_control_an_ungated_second_dispatch_keeps_the_gate_line() -> None:
    # The same call on the false branch too. That call is not gated. The call
    # that is still gated keeps its line, and the reach ask still says {:#?}.
    from pipeline_core.domain.reach_probe import target_for
    mir = _mir()
    call = "_0 = <dyn std::error::Error + Send + Sync as Debug>::fmt(copy _3, copy _2)"
    head, _, tail = mir.partition("_10 = core::fmt::rt::Argument::<'_>::new_display")
    mutated = head + call + " -> [return: bb7, unwind continue];\n        _10 = core::fmt::rt::Argument::<'_>::new_display" + tail
    label = "src/lib.rs::fmt [impl fmt::Debug for DisplayError<M>]"
    facts = _facts(mir=mutated)
    text = _text(facts, label)
    assert "Error::from_display" in text and "only when `f.alternate()` is true" in text
    asked = target_for(label, facts.routes[label])
    assert asked is not None and asked.observe == "alt_debug"
    # Display's own calls are not on that edge.
    shown = "src/lib.rs::fmt [impl fmt::Display for DisplayError<M>]"
    assert "alternate" not in _text(facts, shown)
    display = target_for(shown, facts.routes[shown])
    assert display is not None and display.observe == "display"


def test_control_the_wrapping_variant_is_read_from_the_public_body() -> None:
    mutated = _mir().replace("_0 = Result::<T, Error>::Err(move _5);", "_0 = Result::<T, Error>::Ok(move _5);")
    text = _text(_facts(mir=mutated), "src/lib.rs::fmt [impl fmt::Debug for DisplayError<M>]")
    assert "returned as its `Ok`" in text and "`Err`" not in text


def test_control_the_closure_owner_is_what_routes_a_call_inside_a_closure() -> None:
    mutated = _mir().replace("fn lazy::{closure#0}(", "fn orphan::{closure#0}(")
    assert "src/lib.rs::in_closure" not in _facts(mir=mutated).routes


def test_control_an_unresolved_callee_gives_no_edge() -> None:
    mutated = _mir().replace("_0 = middle() ->", "_0 = elsewhere::middle_too() ->")
    facts = _facts(mir=mutated)
    assert "src/lib.rs::middle" not in facts.routes
    assert "src/lib.rs::leaf" not in facts.routes


# --- persistence --------------------------------------------------------------

def test_the_facts_round_trip_through_their_json() -> None:
    facts = _facts()
    got = facts_from_json(facts_to_json([facts]))
    assert got is not None
    version, worlds = got
    assert version == ROUTES_VERSION
    assert worlds == (facts,)


def test_an_unreadable_facts_file_is_none() -> None:
    assert facts_from_json("not json") is None
    assert facts_from_json(json.dumps({"version": "1"})) is None


def test_rendering_without_a_route_is_empty() -> None:
    assert FnRoutes().lines() == []
    fr = FnRoutes((PublicRoute("`f`", ("`g`",), "builds `T`"),),
                  (GatedDispatch("`h`", "<dyn E as Debug>::fmt", "alternate"),))
    assert fr.lines() == [
        "// Reached from the public API (callers in this crate's MIR): `f` → `g` (builds `T`)",
        "// `<dyn E as Debug>::fmt` is called in `h` only when `f.alternate()` is true (`{:#?}`)",
    ]
    assert TypeFact("T", "src/a.rs").types_line().endswith("was found in the MIR")
