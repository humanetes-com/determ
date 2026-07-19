"""Body→container routing by canonical impl-header identity (debt #43).

Pins the run-187469 failure: a multi-line where-clause TARGET container must
match the model's single-line ``block`` attribute instead of dropping every
returned body."""
from __future__ import annotations

from pipeline_core.domain.impl_routing import (
    impl_block_id,
    norm_src_rel,
    refill_authorized_names,
    route_fn_bodies,
)
from pipeline_core.domain.models import FnRecord, ImplBlock
from pipeline_core.domain.rust_idents import canon_container


def test_impl_block_id_stable_across_renderings() -> None:
    # F5: the same impl rendered with vs without a newline where-clause must
    # produce the SAME id, or a related-block routing-table lookup misses and
    # the whole reply is dropped (response_unused on impl<E> From<E> for Error).
    verbatim = "impl<E> From<E> for Error\nwhere\n    E: StdError,"
    single = "impl<E> From<E> for Error"
    assert impl_block_id("src/error.rs", verbatim) == impl_block_id(
        "src/error.rs", single)
    # a genuinely different impl still gets a different id.
    assert impl_block_id("src/error.rs", "impl Display for Error") != impl_block_id(
        "src/error.rs", single)

_OWN_TARGET = "impl<T> Own<T>\nwhere\n    T: ?Sized,"


def _fn(name: str, block: str) -> dict[str, str]:
    return {"fn_name": name, "body": "1", "block": block}


def test_single_line_attr_matches_multiline_where_target() -> None:
    r = route_fn_bodies([_fn("boxed", "impl<T> Own<T>")],
                        target=_OWN_TARGET, related=[])
    assert [rb.container for rb in r.routed] == [_OWN_TARGET]
    assert r.dropped == ()
    assert r.dropped_names == ()


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
    assert r.dropped_names == ("stray",)
    assert r.dropped[0].block_attr == "impl Unrelated for Thing"


def test_dropped_body_carries_the_rejected_block_not_a_name_collision() -> None:
    # I1: two bodies with the SAME fn name in different impls, only one matching
    # the target. The dropped VO must carry the OTHER's real block header — a
    # {fn_name: fn_data} re-lookup would collapse them and mis-report the drop.
    target = "impl Bool for &bool"
    other = "impl Bool for bool"
    r = route_fn_bodies(
        [_fn("not", target), _fn("not", other)],
        target=target, related=[])
    assert [rb.container for rb in r.routed] == [target]
    assert r.dropped_names == ("not",)
    assert r.dropped[0].block_attr == other
    assert r.dropped[0].attr_canon == canon_container(other)


def test_canonical_related_routes_in_listed_order() -> None:
    rel = ["impl<A> Wrap<A>\nwhere\n    A: Debug,", "impl Wrap<A>"]
    r = route_fn_bodies([_fn("wrap", "impl<A> Wrap<A>")],
                        target="impl Other", related=rel)
    assert [rb.container for rb in r.routed] == [rel[0]]


def test_canon_container_reexport_stays() -> None:
    from pipeline_core.adapters.retros import canon_container as via_adapter
    assert via_adapter is canon_container


def test_html_escaped_block_attr_routes(tmp_path=None) -> None:
    """Run-346122 field bug: the model emits XML-safe attribute values
    (`block="impl&lt;T, E&gt; Context&lt;T, E&gt; for Result&lt;T, E&gt;"`);
    the parser must decode them so routing sees real angle brackets."""
    from pipeline_core.domain.llm_output import extract_fn_bodies
    resp = ('<FN name="context" '
            'block="impl&lt;T, E&gt; Context&lt;T, E&gt; for Result&lt;T, E&gt;">\n'
            'self.map_err(|e| e.ext_context(ctx))\n</FN>')
    parsed = extract_fn_bodies(resp)
    assert parsed[0]["block"] == "impl<T, E> Context<T, E> for Result<T, E>"
    r = route_fn_bodies(
        parsed,
        target="impl<T, E> Context<T, E> for Result<T, E>\nwhere\n    E: StdError,",
        related=[])
    assert len(r.routed) == 1 and r.dropped_names == ()


def test_escaped_pull_arg_decodes() -> None:
    from pipeline_core.domain.context_pull import (
        ContextPull,
        PullKind,
        extract_context_pull,
    )
    assert extract_context_pull(
        '<REQUEST kind="type_def" arg="Own&lt;T&gt;"/>') == ContextPull(
        PullKind.TYPE_DEF, "Own<T>")


def _block(file: str, container: str, refill: tuple[str, ...] = ()) -> ImplBlock:
    return ImplBlock(
        file=file, container=container,
        members=tuple(FnRecord(fn_name=n) for n in refill),
        refill_members=tuple(FnRecord(fn_name=n) for n in refill))


def test_refill_authorization_spans_the_file_not_one_block() -> None:
    # Live loss (run 601947): the crash-blamed `construct_from_std` is a refill
    # member of `impl Error`, but the model answered it while filling
    # `impl Display for Error`. Authorization read off the block being filled
    # refused the splice as `target_not_stub` and dropped the correction.
    blocks = [
        _block("src/error.rs", "impl Error", refill=("construct_from_std",)),
        _block("src/error.rs", "impl Display for Error"),
        _block("src/ptr.rs", "impl<T> Own<T>", refill=("boxed",)),
    ]
    names = refill_authorized_names(blocks, "src/error.rs")
    assert names == frozenset({"construct_from_std"})
    # a refill target in ANOTHER file stays unauthorized here.
    assert refill_authorized_names(blocks, "src/ptr.rs") == frozenset({"boxed"})
    # no refill members anywhere in the file ⇒ nothing may overwrite a non-stub.
    assert refill_authorized_names(
        [_block("src/fmt.rs", "impl Debug for Error")], "src/fmt.rs") == frozenset()


def test_refill_authorization_matches_across_src_prefix_forms() -> None:
    # Block records and extractor records disagree on the `src/` prefix; an
    # exact-string compare would silently authorize nothing.
    blocks = [_block("error.rs", "impl Error", refill=("construct_from_std",))]
    assert refill_authorized_names(blocks, "src/error.rs") == frozenset(
        {"construct_from_std"})
    assert norm_src_rel("src/error.rs") == norm_src_rel("error.rs") == "error.rs"
    # a same-named file in another directory must NOT collide.
    assert norm_src_rel("src/a/mod.rs") != norm_src_rel("src/b/mod.rs")
