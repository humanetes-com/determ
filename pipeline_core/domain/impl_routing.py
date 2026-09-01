"""Which impl block does a model-returned FN body belong to?

The per-impl fill prompt shows a TARGET block plus RELATED blocks (same trait /
dispatch family); the model tags each returned body with a ``block`` attribute.
The old routing matched that attribute against the container strings LITERALLY
(exact, then a ``split("{")`` header-prefix fallback), so a multi-line container
(``impl<T> Own<T>\nwhere\n    T: ?Sized,``) never matched the model's
single-line rendering (``impl<T> Own<T>``) — every body was dropped and the
whole impl-scope LLM call wasted (anyhow run 187469: 11 drops in src/ptr.rs).

This module names the rule: bodies route by CANONICAL impl-header identity
(:func:`~pipeline_core.domain.rust_idents.canon_container` — generics-list,
where-clause and whitespace insensitive), with exact-string equality preferred
and the TARGET preferred over RELATED at each tier. Pure domain — testable
without the model, cargo, or the tree.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pipeline_core.domain.identity import ContainerId
from pipeline_core.domain.models import ImplBlock
from pipeline_core.domain.rust_idents import (
    canon_container,
    impl_head_name,
    impl_trait_name,
    mod_container_ident,
    names_impl_or_trait,
    repair_truncated_generics,
    trait_def_name,
)


@dataclass(frozen=True, slots=True)
class RoutedBody:
    """One returned FN body resolved to the VERBATIM container it splices into.

    `via_trait_default` records that the body arrived tagged with a trait-IMPL
    header and was resolved to the trait DECLARATION that hosts the member's
    default body — the caller must then treat the signature as trait-owned even
    though the container it splices into is not itself a trait impl.

    `via_member_name` records that no header resolved the body and the file's
    own declaration of that fn name did; `sig_text` then carries that
    declaration's verbatim signature, which is the only disambiguator a FREE
    fn has (its container is the empty string, so container narrowing cannot
    separate it from a same-named impl method)."""

    fn_data: dict[str, Any]
    container: str
    via_trait_default: bool = False
    via_member_name: bool = False
    sig_text: str = ""


@dataclass(frozen=True, slots=True)
class TraitDefaultHost:
    """A trait DECLARATION container and the members whose default body it holds.

    `member_names` is what makes the trait-default tier safe: three traits in
    one file can declare the same method name (anyhow's `AdhocKind`/`TraitKind`/
    `BoxedKind` all declare `anyhow_kind`), so a body only routes here when the
    trait it names actually declares that member."""

    container: str
    trait_name: str
    member_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class FileMember:
    """One fn DECLARATION in the file: which container holds it, and the
    verbatim signature that identifies it when the container cannot.

    `container` is "" for a free fn — that is the extractor's own rendering,
    not a missing value, and it is exactly why `sig_text` is carried."""

    fn_name: str
    container: str
    sig_text: str = ""


@dataclass(frozen=True, slots=True)
class DroppedBody:
    """One returned FN body whose ``block`` attribute matched no container.

    Carries the ACTUAL rejected block header (``block_attr``) and its canonical
    form so the drop diagnostic reports the real reason. Re-looking the name up
    in a ``{fn_name: fn_data}`` dict lied whenever the model returned two bodies
    for the same fn name in different impls: the dict collapsed them and the log
    printed whichever body won the collision, not the one that was dropped."""

    fn_name: str
    block_attr: str
    attr_canon: str


@dataclass(frozen=True, slots=True)
class BodyRouting:
    """The routing decision for one model reply: spliceable bodies in reply
    order, plus the bodies whose ``block`` attribute matched no container."""

    routed: tuple[RoutedBody, ...] = ()
    dropped: tuple[DroppedBody, ...] = ()

    @property
    def dropped_names(self) -> tuple[str, ...]:
        """Fn names of the dropped bodies (order-preserving)."""
        return tuple(d.fn_name for d in self.dropped)


def trait_default_hosts(
    blocks: Sequence[ImplBlock], file_rel: str,
) -> tuple[TraitDefaultHost, ...]:
    """Trait DECLARATIONS in `file_rel` that can host a returned body.

    A trait's default method is written inside `trait T { fn m() {..} }`, so an
    `impl T for Ty {}` that overrides nothing is EMPTY and never appears as a
    fillable container. The model, shown the file, reasonably tags its body with
    the impl header it read — the trait is where that body has to go."""
    want = norm_src_rel(file_rel)
    hosts: list[TraitDefaultHost] = []
    for b in blocks:
        if not b.container or norm_src_rel(b.file) != want:
            continue
        name = trait_def_name(b.container)
        if not name:
            continue
        hosts.append(TraitDefaultHost(
            container=b.container, trait_name=name,
            member_names=frozenset(m.fn_name for m in b.members)))
    return tuple(hosts)


def members_in_file(
    blocks: Sequence[ImplBlock], file_rel: str,
) -> tuple[FileMember, ...]:
    """Every fn DECLARED in `file_rel`, with the container that declares it.

    Feeds :func:`route_fn_bodies`' member-identity tier. Includes the free-fn
    block (container "") on purpose — a free fn is where the impl route has no
    header to match, so it is the case the tier exists for. Callers must pass
    the FULL block list for the same reason :func:`containers_in_file` does: a
    frontier-narrowed list omits declarations, and an omitted declaration reads
    to the tier as "this name is unique in the file" when it is not."""
    want = norm_src_rel(file_rel)
    return tuple(
        FileMember(m.fn_name, b.container, m.sig_text)
        for b in blocks if norm_src_rel(b.file) == want
        for m in b.members
    )


def _member_identity_admits(block_attr: str, decls: list[FileMember]) -> bool:
    """Whether the member-identity tier may bind ``block_attr`` to ``decls``.

    Unique name, not an impl/trait claim → yes (comment or signature tag).
    Inherent ``impl Ty`` and a free fn (empty container) → yes. Inherent
    header whose last path segment equals the ident after ``mod `` → yes
    (``impl crate::__private`` vs extractor ``mod __private``). A trait
    claim, a non-unique name, or an impl-member declaration → no.
    """
    if len(decls) != 1:
        return False
    if not names_impl_or_trait(block_attr):
        return True
    inherent = (not trait_def_name(block_attr)
                and not impl_trait_name(block_attr))
    if not inherent:
        return False
    cont = decls[0].container
    if not cont:
        return True
    head = impl_head_name(block_attr)
    ident = mod_container_ident(cont)
    return head is not None and ident is not None and head == ident


def route_fn_bodies(
    parsed: list[dict[str, Any]], *, target: str, related: list[str],
    trait_defaults: Sequence[TraitDefaultHost] = (),
    members: Sequence[FileMember] = (),
) -> BodyRouting:
    """Resolve each parsed FN block to the TARGET or a RELATED container.

    Tiers per body (first hit wins): exact match on TARGET, exact on RELATED
    (listed order), canonical match on TARGET, canonical on RELATED (listed
    order), then the trait-default tier — a body tagged `impl <Trait> for <Ty>`
    or the abbreviated `impl <Trait>` that matched no container routes to the
    declaration of `<Trait>` when that trait declares the member — the
    membership requirement is what makes the abbreviated form safe, since in
    Rust it otherwise reads as an inherent impl on a TYPE of that name. Then
    the MEMBER-IDENTITY tier (below). A body matching nothing is dropped.

    A body naming NO container at all routes to the TARGET. That is not a
    guess: the prompt asked about one container, so an answer that declines to
    repeat which one is about that one. An empty `block` can only mean the
    attribute was absent — the tag reader demands ≥1 character of it — so this
    tier can never fire on a model that named a container and got it wrong.

    **Member identity** (`members`, from :func:`members_in_file`): when the
    attribute is not a container claim at all — a comment, or the fn's own
    signature line — and `file_rel` declares that fn name EXACTLY ONCE, the
    declaration is the answer and the attribute was never information. This is
    the only tier that can reach a FREE fn, whose container is "" and which no
    header can name; it carries the declaration's `sig_text` because "" cannot
    narrow a same-named impl method at splice time.

    The container claim is load-bearing for an IMPL-MEMBER declaration: without
    it a comment-tagged `fmt` would pick one of four `fmt` declarations at
    random. It is not load-bearing when the file's single declaration of the
    name is a FREE fn, and requiring it there cost five correct bodies a reply.
    Run 2370111-1800b8b9 (2026-08-17, src/error.rs group 2/2, attempt 3)
    returned `context_downcast`, `context_drop_rest`, `context_chain_downcast`,
    `context_chain_drop_rest` and `context_backtrace` all tagged
    `impl ErrorImpl<ContextError<C, E>>` — a header no impl in the crate
    declares, invented to group five free fns that do operate on that
    specialisation. All five are top-level `unsafe fn`s declared exactly once
    (src/error.rs:508, 519, 529 in the authored layout); all five were dropped
    as "matches no impl in this file", and the same model in the same run's
    attempt 2 tagged the same fns with their own signature lines and every one
    of them routed. So the tier also fires on an INHERENT impl header — `impl
    Ty`, no `for`, not a `trait` — when the sole declaration is a free fn,
    AND when that declaration lives in an inline module whose ident equals
    the header's head name. Extractor records those fns with
    ``container = "mod {ident}"``, so the free-fn clause
    (`not decls[0].container`) fails for them: run `451330-7e8a7ca9`
    (2026-08-31) dropped `format_err` tagged `impl __private` (and, in an
    earlier log, `impl crate::__private`) while eleven `free fns`-tagged
    error.rs bodies routed the same iteration. Compare
    :func:`impl_head_name` (last path segment) to
    :func:`mod_container_ident` (ident after ``mod ``), not to the full
    container string — ``__private`` != ``mod __private``.

    Trait claims keep the old strictness, and that is what preserves the
    corruption the gate was written against: `!self`, written for `impl Bool
    for bool`, must never land on lib.rs' self-less free `fn not(cond: impl
    Bool)` merely because the impl is cfg-gated out of this cluster and so
    absent from `blocks`. A trait impl's member is declared BY the trait, so a
    free fn can never be what it meant; an inherent header that matches no
    container in the file carries no such claim. `impl Bool for bool`,
    `impl<T> Bool for &T` and `trait Bool` are all excluded by that test, and
    `impl ErrorImpl<ContextError<C, E>>` is admitted by it. `impl __private`
    against a sole `mod __private` declaration is admitted by the ident
    compare; a trait claim still is not.

    Last because every header that DOES resolve must win: the model's own claim
    about its body outranks an inference from the file."""
    routed: list[RoutedBody] = []
    dropped: list[DroppedBody] = []
    target_canon = canon_container(target)
    related_canon = [(c, canon_container(c)) for c in related]
    for fn_data in parsed:
        block_attr = fn_data.get("block") or ""
        if not block_attr.strip():
            routed.append(RoutedBody(fn_data, target))
            continue
        if block_attr == target:
            routed.append(RoutedBody(fn_data, target))
            continue
        exact = next((c for c in related if block_attr == c), None)
        if exact is not None:
            routed.append(RoutedBody(fn_data, exact))
            continue
        attr_canon = canon_container(block_attr)
        if attr_canon == target_canon:
            routed.append(RoutedBody(fn_data, target))
            continue
        canonical = next((c for c, cc in related_canon if attr_canon == cc), None)
        if canonical is not None:
            routed.append(RoutedBody(fn_data, canonical))
            continue
        # A header whose last generic list the model closed with `)` or `]`
        # (`… for Result<T, E)`, `… ContextError<C, Error]`) names the impl as
        # plainly as a correct one; read it as such rather than dropping the
        # body on the one-character slip. Tried after the exact canonical
        # tiers so a header that matches as written is never re-read.
        repaired = repair_truncated_generics(attr_canon)
        if repaired != attr_canon:
            if repaired == target_canon:
                routed.append(RoutedBody(fn_data, target))
                continue
            canonical = next(
                (c for c, cc in related_canon if repaired == cc), None)
            if canonical is not None:
                routed.append(RoutedBody(fn_data, canonical))
                continue
        fn_name = str(fn_data.get("fn_name", ""))
        trait = impl_trait_name(block_attr) or impl_head_name(block_attr)
        host = next((h for h in trait_defaults
                     if h.trait_name == trait and fn_name in h.member_names),
                    None) if trait else None
        if host is not None:
            routed.append(RoutedBody(fn_data, host.container, via_trait_default=True))
            continue
        # An INHERENT impl header (`impl Ty`, no `for`, not a `trait`) that
        # resolved to nothing is not a claim a free fn can contradict; a TRAIT
        # claim is, because the trait declares the member and a free fn never
        # implements one.
        decls = [m for m in members if m.fn_name == fn_name]
        if _member_identity_admits(block_attr, decls):
            routed.append(RoutedBody(
                fn_data, decls[0].container,
                via_member_name=True, sig_text=decls[0].sig_text))
            continue
        dropped.append(DroppedBody(
            fn_name=fn_name,
            block_attr=block_attr,
            attr_canon=attr_canon,
        ))
    return BodyRouting(routed=tuple(routed), dropped=tuple(dropped))


def impl_block_id(file_rel: str, container: str) -> ContainerId:
    """Stable hashable id for an impl block (routing-table key, frontier-mapping
    key, per-block lock key). The container is reduced to its CANONICAL identity
    (`canon_container`) so the SAME impl rendered two ways — a block's own
    verbatim container vs a `related_containers` entry carrying a newline
    where-clause — produces the SAME id. Keying on the raw string let a related
    block miss the routing table, dropping its whole model reply (F5,
    `response_unused` on `impl<E> From<E> for Error`)."""
    return ContainerId(f"{file_rel}::{canon_container(container)}")


def norm_src_rel(rel: str) -> str:
    """Normalized workspace-relative source path: posix separators, no leading
    `src/`. Extractor records and block records disagree on that prefix, so full
    normalized equality is the only safe comparison — a basename fallback would
    collide two same-named files in different directories (`a/mod.rs` vs
    `b/mod.rs`)."""
    p = PurePosixPath(rel.replace("\\", "/")).as_posix().lstrip("/")
    return p[4:] if p.startswith("src/") else p


def containers_in_file(
    blocks: Sequence[ImplBlock], file_rel: str,
) -> tuple[str, ...]:
    """Every distinct impl container declared in `file_rel`, in block order.

    The file-first route shows the model the WHOLE file, so it can answer for
    an impl that needs no filling — a sibling it (correctly) re-designed along
    with its target. Routing has to recognise those headers or the body is
    dropped as "matches no impl in this file" while the impl is sitting right
    there in the text. Callers must therefore pass the FULL block list, not a
    frontier-narrowed one: narrowing keeps only what still needs filling, which
    is exactly the set that already routes.
    """
    want = norm_src_rel(file_rel)
    seen: dict[str, None] = {}
    for b in blocks:
        if b.container and norm_src_rel(b.file) == want:
            seen.setdefault(b.container, None)
    return tuple(seen)


def refill_authorized_names(
    blocks: Sequence[ImplBlock], file_rel: str,
) -> frozenset[str]:
    """Fn names in `file_rel` whose ALREADY-FILLED body a splice may overwrite.

    Authorization spans the whole FILE, never a single block, because both fill
    routes route bodies file-wide: the file-first route shows the model the
    entire file, and the per-block route widens to `related`/sibling containers.
    A body correctly routed to a sibling container is still refused
    `target_not_stub` if authorization is read off the block being filled — so
    the model's answer for a refill target owned by a DIFFERENT block in the
    same file is discarded, which is exactly the co-designed multi-fn
    correction the refill path exists to make (a vtable built by one fn and
    dispatched through by its siblings cannot be repaired one fn at a time).
    """
    want = norm_src_rel(file_rel)
    return frozenset(
        m.fn_name
        for b in blocks if norm_src_rel(b.file) == want
        for m in b.refill_members
    )


def joint_overwrite_names(
    blocks: Sequence[ImplBlock], file_rel: str, answered: Iterable[str],
) -> frozenset[str]:
    """Fn names ONE file-first reply may overwrite: its authorized re-fill
    targets, plus every other fn of `file_rel` the same reply answered.

    The widening fires only when the reply actually answers a re-fill target.
    A reply that fills stubs alone gets `refill_authorized_names` verbatim, so
    nothing about the stub path changes.

    Why the siblings must come with it: the re-fill plan is built from the
    frontier — the fns a FAILING TEST convicts — and a soundness defect is not
    a property of one fn. On 2026-08-01 src/error.rs' plan named `downcast`,
    `downcast_ref`, `downcast_mut`, `root_cause` and `new` in `impl Error`, and
    nothing else; the crash it was asked to fix is a `heap-use-after-free`
    raised at `downcast` (src/error.rs:474) whose scheme `downcast` shares with
    the free fns `object_downcast`, `object_drop_front` and `vtable` — none of
    them in the plan, and so none of them overwritable. The model answered for
    all of them, correctly, having been handed the ASAN report. Asking for a
    joint property to be repaired one half at a time is why six tests have sat
    behind that binary since 07-30.

    Refusing the siblings does not protect the tree. The file-first route
    compiles, runs the U2 gate and runs the whole-suite regression gate over
    the reply AS ONE, and reverts all of it on any failure — the protection is
    those gates, not the authorization. What the narrow authorization
    guarantees is only that a co-designed correction can never be co-designed.

    Scoped to the FILE for the same reason `refill_authorized_names` is: the
    reply is about one file and routing lands bodies file-wide."""
    want = norm_src_rel(file_rel)
    authorized = refill_authorized_names(blocks, file_rel)
    answered_set = frozenset(answered)
    if not (authorized & answered_set):
        return authorized
    declared = frozenset(
        m.fn_name
        for b in blocks if norm_src_rel(b.file) == want
        for m in b.members
    )
    return authorized | (answered_set & declared)
