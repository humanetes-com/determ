"""Body→container routing by canonical impl-header identity (debt #43).

Pins the run-187469 failure: a multi-line where-clause TARGET container must
match the model's single-line ``block`` attribute instead of dropping every
returned body."""
from __future__ import annotations

from pipeline_core.domain.impl_routing import route_fn_bodies
from pipeline_core.domain.rust_idents import canon_container

_OWN_TARGET = "impl<T> Own<T>\nwhere\n    T: ?Sized,"


def _fn(name: str, block: str) -> dict[str, str]:
    return {"fn_name": name, "body": "1", "block": block}


def test_single_line_attr_matches_multiline_where_target() -> None:
    r = route_fn_bodies([_fn("boxed", "impl<T> Own<T>")],
                        target=_OWN_TARGET, related=[])
    assert [rb.container for rb in r.routed] == [_OWN_TARGET]
    assert r.dropped == ()


def test_token_spaced_rendering_matches_pretty_printed() -> None:
    target = "impl<T, E> Context<T, E> for Result<T, E>\nwhere\n    E: Send,"
    attr = "impl Context < T , E > for Result < T , E >"
    r = route_fn_bodies([_fn("context", attr)], target=target, related=[])
    assert [rb.container for rb in r.routed] == [target]


def test_exact_related_wins_over_canonical_target() -> None:
    related = "impl Own<T>"
    r = route_fn_bodies([_fn("cast", related)],
                        target=_OWN_TARGET, related=[related])
    assert [rb.container for rb in r.routed] == [related]


def test_unmatched_block_attr_is_dropped_by_name() -> None:
    r = route_fn_bodies(
        [_fn("ok", _OWN_TARGET), _fn("stray", "impl Unrelated for Thing")],
        target=_OWN_TARGET, related=[])
    assert [rb.fn_data["fn_name"] for rb in r.routed] == ["ok"]
    assert r.dropped == ("stray",)


def test_canonical_related_routes_in_listed_order() -> None:
    rel = ["impl<A> Wrap<A>\nwhere\n    A: Debug,", "impl Wrap<A>"]
    r = route_fn_bodies([_fn("wrap", "impl<A> Wrap<A>")],
                        target="impl Other", related=rel)
    assert [rb.container for rb in r.routed] == [rel[0]]


def test_canon_container_reexport_stays() -> None:
    from pipeline_core.adapters.retros import canon_container as via_adapter
    assert via_adapter is canon_container
