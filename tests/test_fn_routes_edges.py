"""The edge cases of `domain.fn_routes` and `domain.reach_probe` the fixture
crate does not reach: callee spellings the resolver must read or refuse, an
ambiguous callee giving no edge, a public builder being its own route, and the
reach round's observe kinds and stored record. Synthetic MIR, in the shape the
committed fixture dump pins."""
from __future__ import annotations

import json

from pipeline_core.domain.fn_reachability import PublicSurface
from pipeline_core.domain.fn_routes import (
    FnRoutes,
    GatedDispatch,
    MirGraph,
    PublicRoute,
    WorldFacts,
    _aggregate,
    _api_item_name,
    _parse_callee,
    _split_impl,
    _struct_of,
    nameable_api,
    routes_for,
)
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.reach_probe import (
    PROBE_FAILED,
    PROBE_MISSED,
    REACHED,
    ReachOutcome,
    outcomes_from_json,
    outcomes_to_json,
    reach_targets,
    side_hint,
    target_for,
)
from pipeline_core.domain.strategy_render import BEGIN, computed_block, strip_computed

_TYPES = frozenset({"Error", "Wrap"})


def test_the_callee_spellings_the_resolver_reads() -> None:
    t = _parse_callee("<Option<T> as Context<T, Infallible>>::context::<C>", _TYPES)
    assert t is not None and (t.kind, t.self_ty, t.trait, t.name) == ("trait", "Option", "Context", "context")
    i = _parse_callee("error::<impl Error>::construct_from_display::<C>", _TYPES)
    assert i is not None and (i.kind, i.self_ty, i.name) == ("inherent", "Error", "construct_from_display")
    q = _parse_callee("<Error>::msg", _TYPES)
    assert q is not None and (q.kind, q.self_ty, q.name) == ("inherent", "Error", "msg")
    qi = _parse_callee("<impl Wrap>::new", _TYPES)
    assert qi is not None and (qi.kind, qi.self_ty) == ("inherent", "Wrap")
    f = _parse_callee("Box::<ErrorImpl<E>>::new", _TYPES)
    assert f is not None and f.kind == "foreign"
    m = _parse_callee("a::b::helper", _TYPES)
    assert m is not None and (m.kind, m.modules, m.name) == ("free", ("a", "b"), "helper")
    assert _parse_callee("", _TYPES) is None
    assert _parse_callee("<>", _TYPES) is None


def test_impl_headers_struct_types_and_aggregates() -> None:
    assert _split_impl("impl < T > Context < T > for Option < T >") == ("Context < T >", "Option < T >")
    assert _split_impl("impl Error") == ("", "Error")
    assert _struct_of("&mut *const error::VTable<'_>") == "VTable"
    assert _aggregate("_1 = VTable { a: move _2, b: move _3 };") == ("VTable", ["move _2", "move _3"])
    assert _aggregate("_1 = foo(move _2) -> [return: bb1, unwind continue];") is None
    assert _aggregate("_1 = move _2;") is None


def test_the_api_item_a_rendered_line_declares() -> None:
    assert _api_item_name("pub const fn f(x: u8) -> u8") == "f"
    assert _api_item_name("pub struct Wrap< M > { 0: M }") == "Wrap"
    assert _api_item_name("fn private()") == ""
    assert _api_item_name("pub") == ""
    facts = WorldFacts("s", item_names={"src/lib.rs": frozenset({"f"})})
    assert nameable_api("pub const fn f(x: u8) -> u8", "src/lib.rs", facts) == "pub const fn f(x: u8) -> u8"
    # No public index read for the world: the text is kept and says it was
    # not checked; a measured unnameable type of the file is still hidden.
    assert nameable_api("x", "src/lib.rs", WorldFacts("s")) == (
        "x\n// The public index was not read for this world: the items above "
        "are not checked as importable from tests/.")
    from pipeline_core.domain.fn_routes import TypeFact
    hidden = nameable_api("pub struct DisplayError< M >(M);\npub fn g()",
                          "src/wrapper.rs",
                          WorldFacts("s", unnameable=(TypeFact("DisplayError", "src/wrapper.rs"),)))
    assert hidden.splitlines()[0] == "pub fn g()"
    assert "NOT nameable from tests/" in hidden and "`DisplayError`" in hidden
    assert "not checked as importable" in hidden
    # No facts at all: the text as it was.
    assert nameable_api("x", "src/lib.rs", None) == "x"


# --- a synthetic dump ------------------------------------------------------------

_SRC = """pub struct Error;
struct Wrap;
pub fn make() -> Wrap { Wrap }
mod inner {
    pub fn helper() -> u8 { 1 }
}
pub fn api() -> u8 { inner::helper() }
fn twin() -> u8 { 2 }
"""


def _rec(name: str, line: int, container: str = "") -> FnRecord:
    off = sum(len(ln) + 1 for ln in _SRC.splitlines()[:line - 1])
    return FnRecord(file="src/lib.rs", fn_name=name, container=container, sig_byte_start=off,
                    is_pub=True, is_exactly_pub=True)


_MIR = """fn make() -> Wrap {
    let mut _0: Wrap;

    bb0: {
        _0 = Wrap;
        return;
    }
}

fn inner::helper() -> u8 {
    let mut _0: u8;

    bb0: {
        _0 = const 1_u8;
        return;
    }
}

fn api() -> u8 {
    let mut _0: u8;

    bb0: {
        _0 = inner::helper() -> [return: bb1, unwind continue];
    }

    bb1: {
        return;
    }
}

fn twin() -> u8 {
    let mut _0: u8;

    bb0: {
        _0 = const 2_u8;
        return;
    }
}

fn twin() -> u8 {
    let mut _0: u8;

    bb0: {
        _0 = const 2_u8;
        return;
    }
}
"""


def _graph(surface_lines: dict[str, tuple[tuple[int, int], ...]]) -> MirGraph:
    recs = [_rec("make", 3), _rec("helper", 5, "mod inner"), _rec("api", 7), _rec("twin", 8)]
    return MirGraph(_MIR, recs, {"src/lib.rs": _SRC},
                    PublicSurface(nameable=frozenset({"Error"}), fn_spans=surface_lines))


def test_a_public_fn_that_builds_the_type_is_its_own_route() -> None:
    g = _graph({"src/lib.rs": ((3, 3), (7, 7))})
    (route,) = g.routes_from([(g.build_sites("Wrap")[0], "builds `Wrap`")])
    assert route == PublicRoute("`make`", (), "builds `Wrap`")


def test_a_mod_fn_renders_with_its_module_and_routes_through_its_caller() -> None:
    g = _graph({"src/lib.rs": ((7, 7),)})
    helper = _rec("helper", 5, "mod inner")
    assert routes_for(g, helper).routes == (PublicRoute("`api`", (), ""),)
    i = g.index_of(__import__("pipeline_core.domain.attribution", fromlist=["record_site"]).record_site(helper))
    assert i is not None and g.render(i) == "`inner::helper`"


def test_a_site_two_blocks_claim_is_named_by_neither() -> None:
    g = _graph({"src/lib.rs": ()})
    from pipeline_core.domain.attribution import record_site
    assert g.index_of(record_site(_rec("twin", 8))) is None
    assert all(fn.record is None for fn in g.fns if fn.name == "twin")
    assert g.render(next(i for i, fn in enumerate(g.fns) if fn.name == "twin")) == "`twin`"


def test_a_public_record_needs_no_route() -> None:
    g = _graph({"src/lib.rs": ((7, 7),)})
    assert routes_for(g, _rec("api", 7)) == FnRoutes()


# --- the reach round ---------------------------------------------------------------

def _routes(*, gated: bool = False, side: str = "") -> FnRoutes:
    return FnRoutes((PublicRoute("`api`", (), "", side),),
                    (GatedDispatch("`d`", "<dyn E as Debug>::fmt", "alternate"),) if gated else ())


def test_each_trait_kind_gets_its_own_render() -> None:
    dbg = target_for("src/a.rs::fmt [impl Debug for W]", _routes(gated=True))
    assert dbg is not None and dbg.observe == "alt_debug" and "{:#?}" in dbg.action
    plain = target_for("src/a.rs::fmt [impl Debug for W]", _routes())
    assert plain is not None and plain.observe == "debug" and "{:?}" in plain.action
    disp = target_for("src/a.rs::fmt [impl Display for W]", _routes())
    assert disp is not None and disp.observe == "display"
    src = target_for("src/a.rs::source [impl StdError for W]", _routes())
    assert src is not None and "`StdError::source`" in src.action
    free = target_for("src/a.rs::helper", _routes())
    assert free is not None and free.action.startswith("render the public call's result")
    assert target_for("src/a.rs::helper", FnRoutes()) is None


def test_the_unwrap_hint_follows_the_routes_side() -> None:
    assert ".unwrap_err()" in side_hint(PublicRoute("p", side="Err"))
    assert ".unwrap()" in side_hint(PublicRoute("p", side="Some"))
    assert ".unwrap()" in side_hint(PublicRoute("p", side="Ok"))
    assert side_hint(PublicRoute("p")) == ""


def test_reach_targets_skip_what_ran_and_need_facts() -> None:
    facts = WorldFacts("s", routes={"src/a.rs::helper": _routes(), "src/a.rs::done": _routes()})
    assert [t.label for t in reach_targets(facts, {"src/a.rs::done"})] == ["src/a.rs::helper"]
    assert reach_targets(None, ()) == ()


def test_the_reach_record_round_trips_and_refuses_garbage() -> None:
    out = [ReachOutcome("a", REACHED, "id"), ReachOutcome("b", PROBE_MISSED, given="g", when="w",
                                                          observe="debug"),
           ReachOutcome("c", PROBE_FAILED, reason="failed: E0432")]
    assert outcomes_from_json(outcomes_to_json("s", out)) == {o.label: o for o in out}
    assert outcomes_from_json("nope") == {}
    assert outcomes_from_json(json.dumps({"outcomes": [{"label": "x", "outcome": "made_up"}]})) == {}
    assert "did not capture on the original crate: failed: E0432" in out[2].prompt_line()
    assert out[0].prompt_line() == ""


def test_a_computed_part_without_its_end_marker_is_cut_at_its_start() -> None:
    assert strip_computed(f"model\n{BEGIN}\nhalf") == "model\n"
    assert computed_block([WorldFacts("s")]) == ""


def test_a_route_list_past_the_cap_says_it_is_partial() -> None:
    """Review remainder: `MAX_ROUTES` cut the list and the line did not say
    so. Control: a list at the cap reads as before."""
    from pipeline_core.domain.fn_routes import MAX_ROUTES, FnRoutes, PublicRoute

    many = FnRoutes(tuple(PublicRoute(f"pub_{i}") for i in range(MAX_ROUTES + 1)))
    line = many.lines()[0]
    assert f"pub_{MAX_ROUTES - 1}" in line and f"pub_{MAX_ROUTES}" not in line
    assert "more public callers exist" in line
    at_cap = FnRoutes(tuple(PublicRoute(f"pub_{i}") for i in range(MAX_ROUTES)))
    assert "more public callers exist" not in at_cap.lines()[0]


_SRC_WRAP = """pub struct Wrap;
fn make() -> Wrap { Wrap }
mod inner {
    pub fn helper() -> Result<u8, Wrap> { Err(super::make()) }
}
pub fn api() -> Result<u8, Wrap> { inner::helper() }
pub fn api_drop() -> u8 { let _ = inner::helper(); 0 }
"""

_MIR_WRAP = """fn make() -> Wrap {
    let mut _0: Wrap;

    bb0: {
        _0 = Wrap;
        return;
    }
}

fn inner::helper() -> Result<u8, Wrap> {
    let mut _0: Result<u8, Wrap>;
    let mut _1: Wrap;

    bb0: {
        _1 = make() -> [return: bb1, unwind continue];
    }

    bb1: {
        _0 = Result::<u8, Wrap>::Err(move _1);
        return;
    }
}

fn api() -> Result<u8, Wrap> {
    let mut _0: Result<u8, Wrap>;

    bb0: {
        _0 = inner::helper() -> [return: bb1, unwind continue];
    }

    bb1: {
        return;
    }
}

fn api_drop() -> u8 {
    let mut _0: u8;
    let mut _1: Result<u8, Wrap>;

    bb0: {
        _1 = inner::helper() -> [return: bb1, unwind continue];
    }

    bb1: {
        _0 = const 0_u8;
        return;
    }
}
"""


def test_a_wrap_one_hop_inward_is_read_when_the_public_fn_passes_it_through() -> None:
    """Review remainder: `wrapped_as` read only the public fn's own body, so
    `api` returning `inner::helper()`'s `Err(make())` as it is read as
    unwrapped. Control: a public fn that drops the helper's result is not."""
    def rec(name: str, line: int, container: str = "") -> FnRecord:
        off = sum(len(ln) + 1 for ln in _SRC_WRAP.splitlines()[:line - 1])
        return FnRecord(file="src/lib.rs", fn_name=name, container=container,
                        sig_byte_start=off, is_pub=True, is_exactly_pub=True)

    recs = [rec("make", 2), rec("helper", 4, "mod inner"), rec("api", 6), rec("api_drop", 7)]
    g = MirGraph(_MIR_WRAP, recs, {"src/lib.rs": _SRC_WRAP},
                 PublicSurface(nameable=frozenset({"Wrap"}),
                               fn_spans={"src/lib.rs": ((6, 6), (7, 7))}))
    idx = {g.render(i).strip("`").rsplit("::", 1)[-1]: i for i in range(len(g.fns))}
    api, helper, make, drop = idx["api"], idx["helper"], idx["make"], idx["api_drop"]
    assert g.wrapped_as(api, helper) == ""
    assert g.passes_through(api, helper)
    assert g.wrapped_along([api, helper, make]) == "Err"
    assert not g.passes_through(drop, helper)
    assert g.wrapped_along([drop, helper, make]) == ""
