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

from dataclasses import dataclass
from enum import Enum, auto

from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.domain.rust_idents import camelcase_type_names


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
    reference-graph components — the t1.5 authoring unit."""

    cfg_cluster: str
    replaceable: tuple[PrivateSurfaceItem, ...]
    frozen_public: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.replaceable

    @property
    def components(self) -> tuple[tuple[PrivateSurfaceItem, ...], ...]:
        return connected_components(self.replaceable)


def _references(definition: str, private_names: frozenset[str]) -> frozenset[str]:
    """Which private type names this definition points at (graph edges)."""
    return frozenset(camelcase_type_names(definition) & private_names)


def classify_private_surface(
    type_records: list[TypeRecord],
    fn_records: list[FnRecord],
    *,
    cfg_cluster: str = "default",
) -> PrivateSurface:
    """Partition enumerated items into frozen-public vs private-replaceable.

    Replaceable = every non-``pub`` struct/enum/type-alias/trait, PLUS every
    non-``pub`` free/helper fn whose signature references one of those private
    types (the layout helpers, e.g. anyhow's ``unsafe fn vtable(NonNull<
    ErrorImpl>)``). Public items stay frozen — they are the API contract."""
    frozen_public: set[str] = set()
    private_types: list[TypeRecord] = []
    for t in type_records:
        if not t.name or t.kind not in _TYPE_KINDS:
            continue
        if t.is_pub:
            frozen_public.add(t.name)
        else:
            private_types.append(t)

    private_names = frozenset(t.name for t in private_types)
    items: list[PrivateSurfaceItem] = []
    for t in private_types:
        items.append(PrivateSurfaceItem(
            kind=_TYPE_KINDS[t.kind], name=t.name, file=t.file,
            definition=t.definition,
            references=_references(t.definition, private_names),
        ))
    # Private helper fns that touch the private layout become authorable sigs.
    for f in fn_records:
        if f.is_pub or not f.fn_name:
            continue
        refs = _references(f.sig_text, private_names)
        if refs:
            items.append(PrivateSurfaceItem(
                kind=SurfaceItemKind.FN_SIG, name=f.fn_name, file=f.file,
                definition=f.sig_text, references=refs,
            ))
    return PrivateSurface(
        cfg_cluster=cfg_cluster,
        replaceable=tuple(items),
        frozen_public=frozenset(frozen_public),
    )


def connected_components(
    items: tuple[PrivateSurfaceItem, ...],
) -> tuple[tuple[PrivateSurfaceItem, ...], ...]:
    """Group items into connected components of the type-reference graph.

    Edges: an item references another private type by name (directed), plus all
    items sharing a name (a fn sig helper attaches to the type it names). We use
    the UNDIRECTED closure — any partition finer than the connected component
    would let two mutually-referencing private types be authored inconsistently
    (the exact divergence F11 exists to prevent). Deterministic order:
    components sorted by their first item's (file, name)."""
    by_name: dict[str, list[int]] = {}
    for i, it in enumerate(items):
        by_name.setdefault(it.name, []).append(i)

    adj: dict[int, set[int]] = {i: set() for i in range(len(items))}
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
