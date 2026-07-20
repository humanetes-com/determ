"""Private-layout surface classification — F11's freeze-scope resolver.

The baked skeleton freezes EVERY item's shape verbatim, including a crate's
PRIVATE type-erasure layout (anyhow's ``ErrorImpl``/``ErrorVTable``/``Own``).
That forces the model to reconstruct unsafe vtable code the test oracle never
asks for — a self-inflicted wall, not a capability ceiling. This module names
the boundary the layout-authoring stage (t1.5) redraws: PUBLIC signatures stay
frozen (they ARE the API contract); PRIVATE definitions become model-authored.

An item is PRIVATE-REPLACEABLE iff it is not part of the public surface. The
replaceable items form a reference graph (a private type's def names the other
private types it points at); t1.5 authors one CONNECTED COMPONENT per turn so
the scheme is internally consistent by construction (Rule 9). The graph — and
therefore the component partition — is computed PER cfg-cluster, because a
private layout can differ by cfg (nightly ``ErrorImpl`` carries a backtrace
field stable does not — Hard Rule 8).

Pure domain: consumes already-enumerated TypeRecord/FnRecord, no IO, no regex
(CamelCase reference scan via :func:`rust_idents.camelcase_type_names`).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto

from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.domain.rust_idents import camelcase_type_names
from pipeline_core.domain.rust_name_scope import ModuleScopes, NameResolver


class SurfaceItemKind(Enum):
    STRUCT = auto()
    ENUM = auto()
    TYPE_ALIAS = auto()
    TRAIT = auto()
    FN_SIG = auto()   # a private helper fn whose signature touches the layout


_TYPE_KINDS = {
    "struct": SurfaceItemKind.STRUCT,
    "enum": SurfaceItemKind.ENUM,
    "type": SurfaceItemKind.TYPE_ALIAS,
    "trait": SurfaceItemKind.TRAIT,
}


@dataclass(frozen=True, slots=True)
class PrivateSurfaceItem:
    """One model-authorable private item: its name, its home file, the verbatim
    definition/signature the skeleton froze, and the private type names its
    definition references (the reference-graph edges)."""

    kind: SurfaceItemKind
    name: str
    file: str
    definition: str
    references: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PrivateSurface:
    """The replaceable private layout for ONE cfg-cluster, plus the public item
    names that stay frozen (t1.5 must keep them existing + satisfying their
    pins). ``components`` partitions the replaceable items into connected
    reference-graph components — the t1.5 authoring unit.

    ``public_referenced`` = the private type names a FROZEN-PUBLIC type's
    definition points at (e.g. ``pub struct Error { inner: Own<ErrorImpl> }`` →
    ``Own``). These are the layout-sensitive private types: getting their size
    wrong changes a PUBLIC type's size. The t1.5 missing-pin gate (M13) refuses
    to author a component containing one of these UNLESS the layout pins that
    constrain it were actually harvested — otherwise it authors a wrong-sized
    public type that compiles yet fails the repr tests a whole sweep later."""

    cfg_cluster: str
    replaceable: tuple[PrivateSurfaceItem, ...]
    frozen_public: frozenset[str]
    public_referenced: frozenset[str] = frozenset()
    # Private names that a FROZEN item names together — e.g. an untouched
    # `impl<T> Own<T> { pub fn by_mut(&mut self) -> Mut<T> }` couples `Own` and
    # `Mut` even though neither type's own definition names the other.
    # Re-authoring one without the other breaks that frozen item, so these are
    # component edges exactly like a reference edge.
    coupled: tuple[frozenset[str], ...] = ()
    # Per PUBLIC type name, the private type names ITS OWN definition names
    # directly (not merged across all public types) — the precise seed set a
    # STATIC_ASSERTION pinning that one public type's layout must propagate
    # to. `public_referenced` above is the union of these, used only for the
    # coarse "is this component sensitive at all" gate.
    public_direct_refs: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.replaceable

    @property
    def components(self) -> tuple[tuple[PrivateSurfaceItem, ...], ...]:
        return connected_components(self.replaceable, self.coupled)

    def component_is_layout_sensitive(
        self, component: tuple[PrivateSurfaceItem, ...],
    ) -> bool:
        """True iff any item in `component` is reachable from a public type's
        layout (directly named by a public def, or transitively via the private
        reference graph). Such a component MUST reach t1.5 with layout pins."""
        reachable = _reachable_from(self.public_referenced, self.replaceable)
        return any(it.name in reachable for it in component)

    def reachable_from_public(self, public_name: str) -> frozenset[str]:
        """Private type names reachable from ONE public type's OWN direct
        references — e.g. a ``size_of::<Error>()`` pin never names ``Error``'s
        private fields, so a component matched only by this closure (not by
        its own item names) is still the exact scope that pin constrains."""
        seeds = self.public_direct_refs.get(public_name, frozenset())
        return _reachable_from(seeds, self.replaceable)


def _references(
    definition: str, file: str, resolver: NameResolver,
) -> frozenset[str]:
    """Which private type names this definition points at (graph edges), with
    each bare ident resolved in ITS OWN file's scope — an ident that a `use`
    binds to another crate's item is not an edge however closely it spells a
    private name (`dyn StdError` under `use std::error::Error as StdError`)."""
    return resolver.resolve(file, camelcase_type_names(definition))


def _reachable_from(
    seeds: frozenset[str], items: tuple[PrivateSurfaceItem, ...],
) -> frozenset[str]:
    """The closure of `seeds` under each item's private references — every
    private type a public layout depends on, directly or transitively."""
    by_name = {it.name: it for it in items}
    seen: set[str] = set()
    stack = [s for s in seeds if s in by_name]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        it = by_name.get(n)
        if it is not None:
            stack.extend(r for r in it.references if r not in seen)
    return frozenset(seen)


def classify_private_surface(
    type_records: list[TypeRecord],
    fn_records: list[FnRecord],
    *,
    cfg_cluster: str = "default",
    reachable_files: frozenset[str] | None = None,
    scopes: ModuleScopes | None = None,
) -> PrivateSurface:
    """Partition enumerated items into frozen-public vs private-replaceable.

    Replaceable = every non-``pub`` struct/enum/type-alias/trait, PLUS every
    non-``pub`` free/helper fn whose signature references one of those private
    types (the layout helpers, e.g. anyhow's ``unsafe fn vtable(NonNull<
    ErrorImpl>)``). Public items stay frozen — they are the API contract.

    A type only counts as truly public (frozen) when its OWN visibility is
    exactly ``pub`` (``is_exactly_pub`` — a `pub(crate)` item is NOT part of
    the crate's external contract, however `is_pub` folds it) AND its file is
    externally reachable (``reachable_files``, when supplied — a `pub` item
    sealed behind a private `mod` is equally unreachable). Omitting
    `reachable_files` skips that second check (module-tree data unavailable).

    `scopes` carries the crate's module tree + `use` bindings so a reference is
    an edge only where the ident actually denotes that private item. Omitting
    it resolves names crate-globally by bare identifier, which welds every item
    mentioning a name the crate re-binds elsewhere (see rust_name_scope)."""
    frozen_public: set[str] = set()
    public_types: list[TypeRecord] = []
    private_types: list[TypeRecord] = []
    for t in type_records:
        if not t.name or t.kind not in _TYPE_KINDS:
            continue
        truly_public = (
            t.is_pub and t.is_exactly_pub
            and (reachable_files is None or t.file in reachable_files))
        if truly_public:
            frozen_public.add(t.name)
            public_types.append(t)
        else:
            private_types.append(t)

    scope = scopes or ModuleScopes()
    resolver = scope.resolver((t.name, t.file) for t in private_types)
    public_direct_refs = {
        t.name: _references(t.definition, t.file, resolver) for t in public_types}
    public_referenced = frozenset(
        name for refs in public_direct_refs.values() for name in refs)
    items: list[PrivateSurfaceItem] = []
    for t in private_types:
        items.append(PrivateSurfaceItem(
            kind=_TYPE_KINDS[t.kind], name=t.name, file=t.file,
            definition=t.definition,
            references=_references(t.definition, t.file, resolver),
        ))
    # Private helper fns that touch the private layout become authorable sigs.
    # Only truly-EXTERNAL fns are excluded: `is_pub` alone folds `pub(crate)`
    # in, which would wrongly freeze a `pub(crate)` layout helper instead of
    # offering it for authoring.
    # A trait's `definition` is the WHOLE `trait T { … }` text, method bodies
    # included, so a default method inside a replaceable trait is already being
    # authored as part of its trait. Listing it again as its own item makes two
    # targets whose splice spans NEST, which `replace_surface_definitions`
    # rightly refuses as ambiguous — freezing the entire component. (Impl
    # blocks are NOT containers in this sense: `impl Adhoc { … }` lives outside
    # `pub struct Adhoc;`, so those methods stay independently authorable.)
    trait_owned = {
        f"trait {t.name}" for t in private_types if _TYPE_KINDS[t.kind] is
        SurfaceItemKind.TRAIT}
    for f in fn_records:
        if (f.is_pub and f.is_exactly_pub) or not f.fn_name:
            continue
        if f.container in trait_owned:
            continue
        refs = _references(f.sig_text, f.file, resolver)
        if refs:
            items.append(PrivateSurfaceItem(
                kind=SurfaceItemKind.FN_SIG, name=f.fn_name, file=f.file,
                definition=f.sig_text, references=refs,
            ))
    replaceable_names = frozenset(i.name for i in items)
    return PrivateSurface(
        cfg_cluster=cfg_cluster,
        replaceable=tuple(items),
        frozen_public=frozenset(frozen_public),
        public_referenced=public_referenced,
        public_direct_refs=public_direct_refs,
        coupled=_coupling_groups(
            type_records, fn_records, replaceable_names, resolver),
    )


def _coupling_groups(
    type_records: list[TypeRecord],
    fn_records: list[FnRecord],
    replaceable: frozenset[str],
    resolver: NameResolver,
) -> tuple[frozenset[str], ...]:
    """Replaceable names that some item holds together in one text. A fn is read
    with its CONTAINER (`impl Own<T>`) because that is where the coupling lives:
    `fn by_mut(&mut self) -> Mut<T>` names only `Mut`, yet re-authoring `Own`
    alone breaks it. Frozen and replaceable items alike contribute — the frozen
    ones are the load-bearing case, since nothing else can adapt them."""
    groups: set[frozenset[str]] = set()
    texts: list[tuple[str, str]] = [(t.definition, t.file) for t in type_records]
    texts += [(f"{f.container} {f.sig_text}", f.file) for f in fn_records]
    for text, file in texts:
        named = resolver.resolve(file, camelcase_type_names(text)) & replaceable
        if len(named) > 1:
            groups.add(named)
    return tuple(sorted(groups, key=lambda g: sorted(g)))


def connected_components(
    items: tuple[PrivateSurfaceItem, ...],
    coupled: tuple[frozenset[str], ...] = (),
) -> tuple[tuple[PrivateSurfaceItem, ...], ...]:
    """Group items into connected components of the type-reference graph.

    Edges: an item references another private type by name (directed), plus all
    items sharing a name (a fn sig helper attaches to the type it names), plus
    each `coupled` group (private names a FROZEN item holds together — see
    PrivateSurface.coupled). We use the UNDIRECTED closure — any partition finer
    than the connected component would let two mutually-referencing private
    types be authored inconsistently (the exact divergence F11 exists to
    prevent). Deterministic order: components sorted by first item's (file,
    name)."""
    by_name: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        by_name.setdefault(it.name, []).append(i)

    adj: dict[int, set[int]] = {i: set() for i in range(len(items))}
    for group in coupled:
        idxs = [i for n in group for i in by_name.get(n, ())]
        for i in idxs:
            for j in idxs:
                if i != j:
                    adj[i].add(j)
    for i, it in enumerate(items):
        for ref in it.references:
            for j in by_name.get(ref, ()):
                if j != i:
                    adj[i].add(j)
                    adj[j].add(i)
        # a helper fn sig and the type it shares a name with belong together.
        for j in by_name.get(it.name, ()):
            if j != i:
                adj[i].add(j)
                adj[j].add(i)

    seen: set[int] = set()
    comps: list[tuple[PrivateSurfaceItem, ...]] = []
    for start in range(len(items)):
        if start in seen:
            continue
        stack = [start]
        comp: list[int] = []
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.append(n)
            stack.extend(adj[n] - seen)
        comp.sort()
        comps.append(tuple(items[k] for k in comp))
    comps.sort(key=lambda c: (c[0].file, c[0].name))
    return tuple(comps)
