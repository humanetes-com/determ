"""Which impl block does a model-returned FN body belong to? (debt #43)

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

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pipeline_core.domain.identity import ContainerId
from pipeline_core.domain.models import ImplBlock
from pipeline_core.domain.rust_idents import (
    canon_container,
    impl_trait_name,
    trait_def_name,
)


@dataclass(frozen=True, slots=True)
class RoutedBody:
    """One returned FN body resolved to the VERBATIM container it splices into.

    `via_trait_default` records that the body arrived tagged with a trait-IMPL
    header and was resolved to the trait DECLARATION that hosts the member's
    default body — the caller must then treat the signature as trait-owned even
    though the container it splices into is not itself a trait impl."""

    fn_data: dict[str, Any]
    container: str
    via_trait_default: bool = False


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


def route_fn_bodies(
    parsed: list[dict[str, Any]], *, target: str, related: list[str],
    trait_defaults: Sequence[TraitDefaultHost] = (),
) -> BodyRouting:
    """Resolve each parsed FN block to the TARGET or a RELATED container.

    Tiers per body (first hit wins): exact match on TARGET, exact on RELATED
    (listed order), canonical match on TARGET, canonical on RELATED (listed
    order), then the trait-default tier — a body tagged `impl <Trait> for <Ty>`
    that matched no container routes to the declaration of `<Trait>` when that
    trait declares the member. Last because an explicit override always wins
    over the default it would replace. A body matching nothing is dropped."""
    routed: list[RoutedBody] = []
    dropped: list[DroppedBody] = []
    target_canon = canon_container(target)
    related_canon = [(c, canon_container(c)) for c in related]
    for fn_data in parsed:
        block_attr = fn_data.get("block") or ""
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
        fn_name = str(fn_data.get("fn_name", ""))
        trait = impl_trait_name(block_attr)
        host = next((h for h in trait_defaults
                     if h.trait_name == trait and fn_name in h.member_names),
                    None) if trait else None
        if host is not None:
            routed.append(RoutedBody(fn_data, host.container, via_trait_default=True))
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
