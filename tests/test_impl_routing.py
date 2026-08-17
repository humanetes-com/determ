"""Body→container routing by canonical impl-header identity.

Pins the run-187469 failure: a multi-line where-clause TARGET container
(``impl<T> Own<T>\\nwhere\\n    T: ?Sized,``) must match the model's single-line
``block`` attribute (``impl<T> Own<T>``) instead of dropping every returned
body — 11 drops in one src/ptr.rs call, the whole impl-scope answer wasted."""
from __future__ import annotations

from pipeline_core.domain.impl_routing import (
    containers_in_file,
    impl_block_id,
    joint_overwrite_names,
    members_in_file,
    norm_src_rel,
    refill_authorized_names,
    route_fn_bodies,
    trait_default_hosts,
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


# --- trait-default routing tier -------------------------------------------
# Live loss (run 2879-66905005, anyhow src/kind.rs, every run since 07-20): the
# model returned all three `anyhow_kind` bodies tagged with the trait-IMPL
# headers it read in the file text. Those impls are EMPTY (`impl<T> AdhocKind
# for &T {}`) — the body lives in the trait's default method — so they are not
# fillable containers and all three bodies were dropped as "matches no impl in
# this file", three wasted GPU answers per run.

def _trait_block(container: str, members: tuple[str, ...]) -> ImplBlock:
    return ImplBlock(
        file="src/kind.rs", container=container,
        members=tuple(FnRecord(fn_name=n) for n in members))


_KIND_BLOCKS = [
    _trait_block("trait AdhocKind", ("anyhow_kind",)),
    _trait_block("trait TraitKind", ("anyhow_kind",)),
    _trait_block("trait BoxedKind", ("anyhow_kind",)),
    _block("src/kind.rs", "impl Adhoc"),
    _block("src/other.rs", "trait Elsewhere"),
]


def test_trait_default_hosts_scoped_to_file_and_declarations() -> None:
    hosts = trait_default_hosts(_KIND_BLOCKS, "src/kind.rs")
    assert [h.trait_name for h in hosts] == [
        "AdhocKind", "TraitKind", "BoxedKind"]
    assert hosts[0].container == "trait AdhocKind"
    assert hosts[0].member_names == frozenset({"anyhow_kind"})


def test_body_tagged_with_empty_override_routes_to_its_trait_default() -> None:
    hosts = trait_default_hosts(_KIND_BLOCKS, "src/kind.rs")
    parsed = [
        _fn("anyhow_kind",
            "impl<T> AdhocKind for &T where T: ?Sized + Display + Send"),
        _fn("anyhow_kind", "impl<E> TraitKind for E where E: Into<Error>"),
        _fn("anyhow_kind", "impl BoxedKind for Box<dyn StdError + Send + Sync>"),
    ]
    r = route_fn_bodies(parsed, target="impl Adhoc", related=[],
                        trait_defaults=hosts)
    assert r.dropped == ()
    # Three same-named methods: each must land on the trait it was tagged with.
    assert [rb.container for rb in r.routed] == [
        "trait AdhocKind", "trait TraitKind", "trait BoxedKind"]
    assert all(rb.via_trait_default for rb in r.routed)


def test_abbreviated_impl_tag_routes_to_the_trait_default() -> None:
    """The three headers the live run actually dropped: the model abbreviates
    `impl<T> AdhocKind for &T` down to the trait name alone, so the tag carries
    no `for` and `impl_trait_name` reads it as None."""
    hosts = trait_default_hosts(_KIND_BLOCKS, "src/kind.rs")
    parsed = [
        _fn("anyhow_kind", "impl AdhocKind"),
        _fn("anyhow_kind", "impl TraitKind"),
        _fn("anyhow_kind",
            '#[cfg(any(feature = "std", not(anyhow_no_core_error)))]\\nimpl BoxedKind'),
    ]
    r = route_fn_bodies(parsed, target="impl Adhoc", related=[],
                        trait_defaults=hosts)
    assert r.dropped == ()
    assert [rb.container for rb in r.routed] == [
        "trait AdhocKind", "trait TraitKind", "trait BoxedKind"]
    assert all(rb.via_trait_default for rb in r.routed)


def test_abbreviated_tag_still_requires_membership() -> None:
    # `impl Foo` is an INHERENT impl on a type named Foo in real Rust. Only the
    # membership requirement keeps that reading from being routed as a trait.
    hosts = trait_default_hosts(_KIND_BLOCKS, "src/kind.rs")
    r = route_fn_bodies([_fn("not_a_member", "impl AdhocKind")],
                        target="impl Adhoc", related=[], trait_defaults=hosts)
    assert r.routed == () and len(r.dropped) == 1


def test_trait_default_tier_never_overrides_a_real_container_match() -> None:
    # An impl that DOES declare the member is a genuine override; the default
    # must not swallow it.
    hosts = trait_default_hosts(_KIND_BLOCKS, "src/kind.rs")
    real = "impl<T> AdhocKind for &T"
    r = route_fn_bodies([_fn("anyhow_kind", real)],
                        target=real, related=[], trait_defaults=hosts)
    assert [rb.container for rb in r.routed] == [real]
    assert [rb.via_trait_default for rb in r.routed] == [False]


def test_trait_default_tier_requires_the_trait_to_declare_the_member() -> None:
    # Keying on the trait name alone would let any unknown method land on a
    # same-named trait; membership is what makes the tier safe.
    hosts = trait_default_hosts(_KIND_BLOCKS, "src/kind.rs")
    r = route_fn_bodies([_fn("not_a_member", "impl<T> AdhocKind for &T")],
                        target="impl Adhoc", related=[], trait_defaults=hosts)
    assert r.routed == ()
    assert r.dropped_names == ("not_a_member",)


def test_trait_default_tier_is_off_by_default() -> None:
    # Callers that pass no hosts keep the pre-existing drop behaviour verbatim.
    r = route_fn_bodies([_fn("anyhow_kind", "impl<T> AdhocKind for &T")],
                        target="impl Adhoc", related=[])
    assert r.dropped_names == ("anyhow_kind",)


def test_a_body_naming_no_container_routes_to_the_target() -> None:
    # The prompt asked about ONE container, so a reply that declines to repeat
    # which one is about that one. `block=""` is unproducible by the tag reader
    # (it demands >=1 char), so this tier cannot fire on a NAMED container that
    # simply failed to match — those still drop, as below.
    r = route_fn_bodies([_fn("source", "")],
                        target="impl StdError for E", related=["impl Other"])
    assert [rb.container for rb in r.routed] == ["impl StdError for E"]
    assert [rb.via_trait_default for rb in r.routed] == [False]
    # whitespace-only is the same statement
    r = route_fn_bodies([_fn("source", "   ")], target="impl X", related=[])
    assert [rb.container for rb in r.routed] == ["impl X"]
    # a container that was named and matches nothing is still dropped
    r = route_fn_bodies([_fn("source", "impl Nope")], target="impl X", related=[])
    assert r.dropped_names == ("source",)


# --- member identity: the header is not always a container statement --------
# 2026-08-01, run 328661-13e51032, one src/error.rs file-first reply: the model
# returned 24 bodies and routing discarded 22 of them. Thirteen carried the
# block attribute `// free function` (the file's vtable helpers genuinely have
# no impl to name) and six carried the fn's OWN signature line
# (`pub(crate) fn construct_from_std<E> where E: StdError + Send + Sync +
# 'static`). Among the six was `construct` — the funnel every `construct_from_*`
# calls, and the fn the per-fn route failed twelve times before a joint fill
# solved it in four minutes. Nineteen of the 22 name a fn src/error.rs declares
# exactly once; the other three name fns it does not declare at all.

def _member_block(container: str, members: tuple[tuple[str, str], ...]) -> ImplBlock:
    return ImplBlock(
        file="src/error.rs", container=container,
        members=tuple(FnRecord(fn_name=n, sig_text=s) for n, s in members))


_ERROR_BLOCKS = [
    _member_block("impl Error", (
        ("construct", "unsafe fn construct<E>(error: E, vtable: &'static ErrorVTable) -> Self"),
        ("provide", "fn provide<'a>(&'a self, request: &mut Request<'a>)"),
    )),
    _member_block("", (
        ("object_downcast", "unsafe fn object_downcast<E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>"),
        ("vtable", "unsafe fn vtable(p: NonNull<ErrorImpl>) -> &'static ErrorVTable"),
    )),
    _member_block("impl ErrorImpl", (
        ("provide", "unsafe fn provide<'a>(this: Ref<'a, Self>, request: &mut Request<'a>)"),
    )),
]


def test_members_in_file_includes_the_free_fn_block() -> None:
    # The free-fn block's container is "" — the extractor's own rendering, and
    # precisely the declaration no header can name. Excluding it would leave
    # the tier unable to reach the only case it exists for.
    names = [(m.fn_name, m.container) for m in members_in_file(_ERROR_BLOCKS, "src/error.rs")]
    assert ("object_downcast", "") in names and ("construct", "impl Error") in names
    assert members_in_file(_ERROR_BLOCKS, "src/ptr.rs") == ()


def test_signature_line_as_block_attr_routes_to_the_declaring_impl() -> None:
    # The live loss. `construct` belongs to `impl Error`; tagged with its own
    # signature it matched no container and the body was thrown away.
    mems = members_in_file(_ERROR_BLOCKS, "src/error.rs")
    r = route_fn_bodies(
        [_fn("construct", "unsafe fn construct<E> where E: StdError + Send + Sync + 'static")],
        target="impl ErrorImpl", related=["impl Error"], members=mems)
    assert r.dropped == ()
    assert [(rb.container, rb.via_member_name) for rb in r.routed] == [
        ("impl Error", True)]
    # and it carries the declaration's signature, which is what lets the splice
    # find a free fn later in this same reply.
    assert r.routed[0].sig_text.startswith("unsafe fn construct<E>(error: E")


def test_comment_as_block_attr_reaches_the_free_fn() -> None:
    mems = members_in_file(_ERROR_BLOCKS, "src/error.rs")
    r = route_fn_bodies([_fn("object_downcast", "// free function")],
                        target="impl Error", related=[], members=mems)
    assert r.dropped == ()
    assert r.routed[0].container == ""
    assert r.routed[0].via_member_name is True
    assert "object_downcast" in r.routed[0].sig_text


def test_member_tier_refuses_a_name_the_file_declares_twice() -> None:
    # `provide` is declared by `impl Error` AND `impl ErrorImpl`. With no usable
    # header there is nothing to choose between them, and choosing anyway would
    # splice a body into the wrong one.
    mems = members_in_file(_ERROR_BLOCKS, "src/error.rs")
    r = route_fn_bodies([_fn("provide", "// free function")],
                        target="impl Error", related=[], members=mems)
    assert r.routed == () and r.dropped_names == ("provide",)


def test_member_tier_refuses_a_name_the_file_does_not_declare() -> None:
    # Three of the 22 dropped bodies named fns src/error.rs has never had
    # (`object_drop_rest_simple`); an invented name must stay dropped.
    mems = members_in_file(_ERROR_BLOCKS, "src/error.rs")
    r = route_fn_bodies([_fn("object_drop_rest_simple", "// free function")],
                        target="impl Error", related=[], members=mems)
    assert r.routed == () and r.dropped_names == ("object_drop_rest_simple",)


def test_member_tier_never_fires_on_a_header_that_names_an_impl() -> None:
    # The corruption container scoping exists to prevent: `!self` written for
    # `impl Bool for bool` must never land on the self-less free `fn not(cond)`
    # just because that free fn is the only `not` in the block records — which
    # is exactly what a cfg-gated-out impl looks like from here.
    blocks = [_member_block("", (("not", "fn not(cond: bool) -> bool"),))]
    mems = members_in_file(blocks, "src/error.rs")
    for attr in ("impl Bool for bool",
                 '#[cfg(feature = "std")]\nimpl Bool for bool',
                 "impl<T> Bool for &T",
                 "trait Bool"):
        r = route_fn_bodies([_fn("not", attr)], target="impl Other",
                            related=[], members=mems)
        assert r.routed == (), attr
        assert r.dropped_names == ("not",), attr


def test_inherent_impl_header_reaches_a_uniquely_declared_free_fn() -> None:
    # Run 2370111-1800b8b9 (2026-08-17), src/error.rs group 2/2 attempt 3: the
    # model grouped five free fns under `impl ErrorImpl<ContextError<C, E>>`, a
    # header the crate declares nowhere, and all five were dropped. They are
    # top-level `unsafe fn`s declared exactly once (src/error.rs:508, 519, 529
    # in the authored layout), and attempt 2 of the same run routed the same
    # fns fine when the model tagged them with their own signature lines.
    blocks = [_member_block("", (
        ("context_downcast",
         "unsafe fn context_downcast<C, E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>"),
    ))]
    mems = members_in_file(blocks, "src/error.rs")
    r = route_fn_bodies([_fn("context_downcast", "impl ErrorImpl<ContextError<C, E>>")],
                        target="impl Error", related=[], members=mems)
    assert r.dropped == ()
    assert r.routed[0].container == ""
    assert r.routed[0].via_member_name is True
    assert "context_downcast" in r.routed[0].sig_text


def test_inherent_relaxation_does_not_reach_an_impl_member() -> None:
    # Only a FREE fn is unreachable by any header. A name whose sole
    # declaration lives in an impl still needs a header that resolves, or the
    # model's wrong container claim would silently retarget the body.
    blocks = [_member_block("impl Error", (
        ("construct", "unsafe fn construct<E>(error: E) -> Self"),))]
    mems = members_in_file(blocks, "src/error.rs")
    r = route_fn_bodies([_fn("construct", "impl ErrorImpl<ContextError<C, E>>")],
                        target="impl Other", related=[], members=mems)
    assert r.routed == () and r.dropped_names == ("construct",)


def test_member_tier_is_off_by_default() -> None:
    # Callers that pass no members keep the pre-existing drop behaviour.
    r = route_fn_bodies([_fn("construct", "// free function")],
                        target="impl Error", related=[])
    assert r.dropped_names == ("construct",)


def test_a_resolvable_header_still_outranks_the_member_tier() -> None:
    # The model's own claim about its body wins over an inference from the
    # file: a body tagged with a container that DOES resolve goes there, even
    # when the name is unique elsewhere.
    mems = members_in_file(_ERROR_BLOCKS, "src/error.rs")
    r = route_fn_bodies([_fn("provide", "impl ErrorImpl")],
                        target="impl Error", related=["impl ErrorImpl"], members=mems)
    assert [(rb.container, rb.via_member_name) for rb in r.routed] == [
        ("impl ErrorImpl", False)]


def test_block_header_carrying_its_opening_brace_still_matches() -> None:
    # The model echoes the header off the file text, where `{` opens the block
    # on the header's own line. Both `fmt` bodies for src/wrapper.rs were
    # dropped on 2026-08-01 because `DebugforBoxedError{` != `DebugforBoxedError`.
    tree = "impl Debug for BoxedError"
    model = '#[cfg(any(feature = "std", not(anyhow_no_core_error)))]\nimpl Debug for BoxedError\n{'
    assert canon_container(model) == canon_container(tree)
    r = route_fn_bodies([_fn("fmt", model)], target="impl Display for BoxedError",
                        related=[tree])
    assert [rb.container for rb in r.routed] == [tree]
    # a where-clause header rendered the same way reduces identically
    assert canon_container("impl<T> Write for Indented<'_, T>\nwhere\n    T: Write,\n{") == \
        canon_container("impl<T> Write for Indented<'_, T>")
    # a const-generic self type keeps its braces — they are part of its identity
    assert canon_container("impl Foo for Bar<{ N }>") != canon_container("impl Foo for Bar")


# --- one reply's overwrite authorization -----------------------------------
# The file-first plan IS the authorization set: both are built from
# `refill_members`. On 2026-08-01 src/error.rs' plan named `new`, `root_cause`,
# `downcast`, `downcast_ref`, `downcast_mut` and the trait-impl members — and
# the crash it was handed (`heap-use-after-free` at src/error.rs:474, in
# `downcast`) lives in a scheme `downcast` shares with the free `object_downcast`,
# `object_drop_front` and `vtable`, none of which the plan could overwrite.

def _refill_block(container: str, members: tuple[str, ...],
                  refill: tuple[str, ...] = ()) -> ImplBlock:
    return ImplBlock(
        file="src/error.rs", container=container,
        members=tuple(FnRecord(fn_name=n) for n in members),
        refill_members=tuple(FnRecord(fn_name=n) for n in refill))


_JOINT_BLOCKS = [
    _refill_block("impl Error", ("downcast", "construct"), refill=("downcast",)),
    _refill_block("", ("object_downcast", "vtable")),
    ImplBlock(file="src/ptr.rs", container="impl<T> Own<T>",
              members=(FnRecord(fn_name="boxed"),),
              refill_members=(FnRecord(fn_name="boxed"),)),
]


def test_joint_overwrite_adds_the_siblings_a_reply_co_designed() -> None:
    got = joint_overwrite_names(
        _JOINT_BLOCKS, "src/error.rs",
        ["downcast", "object_downcast", "vtable", "construct"])
    assert got == {"downcast", "object_downcast", "vtable", "construct"}


def test_joint_overwrite_is_inert_without_an_authorized_refill() -> None:
    # A reply that answers only unauthorized names may still overwrite nothing
    # but the file's own refill targets — it is not a correction of anything.
    got = joint_overwrite_names(
        _JOINT_BLOCKS, "src/error.rs", ["object_downcast", "vtable"])
    assert got == refill_authorized_names(_JOINT_BLOCKS, "src/error.rs")
    assert got == {"downcast"}
    # …and a stub-only file (no refill members at all) is untouched.
    stubs = [_refill_block("impl Error", ("downcast", "construct"))]
    assert joint_overwrite_names(stubs, "src/error.rs",
                                 ["downcast", "construct"]) == frozenset()


def test_joint_overwrite_never_leaves_the_file_or_invents_a_name() -> None:
    # `boxed` is a refill target — in ANOTHER file. A name this file does not
    # declare is not a member of it however the reply spells it.
    got = joint_overwrite_names(
        _JOINT_BLOCKS, "src/error.rs",
        ["downcast", "boxed", "object_drop_rest_simple"])
    assert got == {"downcast"}
