"""Body→container routing by canonical impl-header identity (debt #43).

Pins the run-187469 failure: a multi-line where-clause TARGET container must
match the model's single-line ``block`` attribute instead of dropping every
returned body."""
from __future__ import annotations

from pipeline_core.domain.impl_routing import (
    containers_in_file,
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


def test_lifetime_rendering_does_not_lose_a_body() -> None:
    # Live loss (run 1170946): the file declares
    # `impl Write for Quoted<&mut fmt::Formatter<'_>>`; the model answered with
    # `Formatter<'_'>` — `'_` reads as an unterminated quote, so it closed it.
    # One `write_str` body dropped as "does not match any impl in this file".
    declared = "impl Write for Quoted<&mut fmt::Formatter<'_>>"
    assert canon_container("impl Write for Quoted<&mut fmt::Formatter<'_'>>") \
        == canon_container(declared)
    # A named lifetime is the same container as an elided one.
    assert canon_container("impl<'a> Write for Quoted<&'a mut Formatter<'a>>") \
        == canon_container("impl Write for Quoted<&mut Formatter<'_>>")
    # `Box<dyn Trait>` defaults to `+ 'static`, so both spellings are one type.
    assert canon_container(
        "impl From<Error> for Box<dyn StdError + Send + Sync + 'static>") \
        == canon_container("impl From<Error> for Box<dyn StdError + Send + Sync>")


def test_distinct_containers_still_differ_after_lifetime_elision() -> None:
    # Elision must not collapse containers that differ in anything but a
    # lifetime — otherwise a body routes into the wrong sibling impl.
    assert canon_container("impl Foo for Bar<'a>") != canon_container(
        "impl Foo for Baz<'a>")
    assert canon_container("impl From<Error> for Box<dyn StdError>") != \
        canon_container("impl From<Error> for Box<dyn StdError + Send>")
    # The real shape elision runs closest to: three sibling impls in one file
    # that differ ONLY in their auto-trait bounds, each carrying `+ 'static`.
    # Collapsing any pair would route a `from` body into the wrong sibling.
    siblings = [
        "impl From<Error> for Box<dyn StdError + Send + Sync + 'static>",
        "impl From<Error> for Box<dyn StdError + Send + 'static>",
        "impl From<Error> for Box<dyn StdError + 'static>",
    ]
    assert len({canon_container(h) for h in siblings}) == 3


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


def test_containers_in_file_sees_impls_that_need_no_filling() -> None:
    # Live loss (run 990528): `impl Deref for Error` et al. are present in
    # src/error.rs but carry no stub, so they are absent from the fill set. The
    # file-first prompt renders the whole file, the model answered for them, and
    # routing dropped every body as "matches no impl in this file".
    fill_set = [_block("src/error.rs", "impl Error", refill=("root_cause",))]
    whole_file = [
        *fill_set,
        _block("src/error.rs", "impl Deref for Error"),
        _block("src/error.rs", "impl<E> ErrorImpl<E>"),
        _block("src/ptr.rs", "impl<T> Own<T>"),
    ]
    assert containers_in_file(fill_set, "src/error.rs") == ("impl Error",)
    assert containers_in_file(whole_file, "src/error.rs") == (
        "impl Error", "impl Deref for Error", "impl<E> ErrorImpl<E>")
    # the widening only ever routes; it must not reach another file.
    assert containers_in_file(whole_file, "src/ptr.rs") == ("impl<T> Own<T>",)


def test_containers_in_file_normalizes_the_src_prefix_and_dedupes() -> None:
    blocks = [
        _block("error.rs", "impl Error"),
        _block("src/error.rs", "impl Error"),
        _block("error.rs", ""),
    ]
    # same impl reached under both prefix forms collapses to one entry, and a
    # free-fn block (empty container) is not a routable header.
    assert containers_in_file(blocks, "src/error.rs") == ("impl Error",)


def test_widened_containers_route_a_body_for_an_unfilled_sibling() -> None:
    # End-to-end of the two halves: the widened vocabulary is what makes an
    # already-complete sibling's body routable instead of dropped.
    whole_file = [
        _block("src/error.rs", "impl Error", refill=("root_cause",)),
        _block("src/error.rs", "impl Deref for Error"),
    ]
    containers = ["impl Error"]
    extra = [c for c in containers_in_file(whole_file, "src/error.rs")
             if c not in containers]
    r = route_fn_bodies(
        [{"fn_name": "deref", "block": "impl Deref for Error", "body": "self.inner"}],
        target=containers[0], related=containers[1:] + extra)
    assert r.dropped == ()
    assert r.routed[0].container == "impl Deref for Error"


_BOXED_CFG = '#[cfg(any(feature = "std", not(anyhow_no_core_error)))]'


def test_attribute_prefixed_block_header_routes_to_the_bare_container() -> None:
    # The model is shown the file text, where the impl's cfg attribute sits on
    # the line above `impl`, so it echoes the attribute as part of the header.
    # The extractor's containers carry no attribute; before the strip, all three
    # of these bodies were dropped and the LLM call wasted.
    containers = ["impl Debug for BoxedError", "impl Display for BoxedError",
                  "impl StdError for BoxedError"]
    parsed = [
        {"fn_name": "fmt", "block": f"{_BOXED_CFG}\nimpl Debug for BoxedError",
         "body": "..."},
        {"fn_name": "fmt", "block": f"{_BOXED_CFG}\nimpl Display for BoxedError",
         "body": "..."},
        {"fn_name": "source", "block": f"{_BOXED_CFG}\nimpl StdError for BoxedError",
         "body": "..."},
    ]
    r = route_fn_bodies(parsed, target=containers[0], related=containers[1:])
    assert r.dropped == ()
    assert [b.container for b in r.routed] == containers


def test_literal_backslash_n_where_clause_is_not_dropped() -> None:
    # The model rendered the line breaks as two-character `\n` escapes inside a
    # single-line attribute. Nothing downstream read those as whitespace, so the
    # where-clause split never fired and the clause stayed in the key.
    escaped = r"impl<T> Write for Indented<'_, T>\nwhere\n    T: Write,"
    r = route_fn_bodies(
        [{"fn_name": "write_str", "block": escaped, "body": "..."}],
        target="impl Write for Indented < '_ , T >", related=[])
    assert r.dropped == ()
    assert r.routed[0].container == "impl Write for Indented < '_ , T >"


def test_attribute_strip_survives_brackets_in_strings_and_nested_comments() -> None:
    # A `]` inside an attribute string must not end the attribute early, and
    # Rust block comments nest.
    assert canon_container('#[doc = "]"]\nimpl Foo for Bar') == "FooforBar"
    assert canon_container("/* a /* b */ c */ impl Foo for Bar") == "FooforBar"
    assert canon_container("/// docs\nimpl Foo for Bar") == "FooforBar"
    assert canon_container("#[a]\n#[b]\nimpl Foo for Bar") == "FooforBar"


def test_plain_headers_are_unchanged_by_the_strip() -> None:
    # Regression guard: the normalisation must not perturb headers that never
    # carried an attribute.
    assert (canon_container("impl<T, E> Context<T, E> for Result<T, E>\nwhere\n    E: X,")
            == canon_container("impl Context < T , E > for Result < T , E >"))
