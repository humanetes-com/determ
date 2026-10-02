"""One behaviour book for every build world, projected into each world's store.

The book (features, checked rules, glossary) is a statement about the crate's
examples. Built per cluster it asks every core example once per world: on run
`72627-751534fe` (2026-09-30) the strip bake spent 10,090 s on the stable book
and 10,639 s on the nightly one, over 227 examples identical in every field
but `cfg_cluster`, and the two books still disagreed (2 rules kept against 3),
so two fills of one crate read different specifications of the same example.

`union_worlds` places every world's examples through `cfg_worlds.partition`:
an example every world produced with the same content is ONE example in the
union, keyed by its own id; an id whose content differs between worlds is two
observables, each keyed `id@worlds`, so neither world's Then is dropped (the
existing-wins rule of `merge_suites` is a within-world merge and is not this).
`union_glossary` does the same for the vocabulary, marking a name with the
worlds that compile it. `project_book` writes a world's store FROM the one
book: that world's own specs, the features and rules restricted to examples
it compiles, the glossary names it compiles.

Which fields make an example "the same" is the prompt's view of it: the code,
the Then, the asserting helper, the required env, the fence. `cfg_cluster`,
`exercises` and `klass` record where and how a world measured the example,
not what the example is, and are excluded from identity.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSuite,
    Feature,
    GlossaryEntry,
    Rule,
    example_code,
)
from pipeline_core.domain.cfg_worlds import Partition, membership, partition

# Per-world measurement fields of a spec: excluded from example identity.
_WORLD_FIELDS = frozenset({"cfg_cluster", "exercises", "klass"})


def example_identity(spec: BehaviourSpec) -> str:
    """What a prompt shows of `spec`, as one comparable string."""
    d = {k: v for k, v in spec.to_dict().items() if k not in _WORLD_FIELDS and k != "behaviour_id"}
    d["code"] = example_code(spec)
    return json.dumps(d, sort_keys=True, default=str)


@dataclass(frozen=True)
class WorldUnion:
    """Every world's examples as one suite the book is built over.

    `suite.specs` carry union ids; `local_id[uid]` is the id the example has
    in its worlds' stores; `examples` places each union id in its worlds."""

    suite: BehaviourSuite
    local_id: Mapping[str, str]
    examples: Partition[str]

    @property
    def clusters(self) -> tuple[str, ...]:
        return self.examples.clusters

    def marks(self) -> dict[str, tuple[str, ...]]:
        """`union id → worlds` for the examples only some worlds compile —
        the prompt's `BUILT ONLY IN` line. Empty when every example is core."""
        return {p.element: self.examples.mark(p) for p in self.examples.placed
                if not self.examples.is_core(p)}

    def ids_in(self, world: str) -> frozenset[str]:
        return frozenset(self.examples.in_world(world))


def union_worlds(suites: Mapping[str, BehaviourSuite]) -> WorldUnion:
    """The union of every world's suite, one example per distinct content."""
    worlds = sorted(suites)
    variants: dict[str, dict[str, tuple[BehaviourSpec, set[str]]]] = {}
    order: list[str] = []
    for w in worlds:
        for s in suites[w].specs:
            by_content = variants.setdefault(s.behaviour_id, {})
            if not by_content:
                order.append(s.behaviour_id)
            key = example_identity(s)
            spec, ws = by_content.setdefault(key, (s, set()))
            ws.add(w)
    specs: list[BehaviourSpec] = []
    local: dict[str, str] = {}
    placed: dict[str, frozenset[str]] = {}
    for bid in sorted(order):
        by_content = variants[bid]
        for spec, ws in by_content.values():
            uid = bid if len(by_content) == 1 else f"{bid}@{'+'.join(sorted(ws))}"
            specs.append(replace(spec, behaviour_id=uid, rules=()))
            local[uid] = bid
            placed[uid] = frozenset(ws)
    helpers: dict[str, str] = {}
    for w in worlds:
        for k, t in suites[w].helpers:
            helpers.setdefault(k, t)
    first = suites[worlds[0]].cfg_cluster if worlds else "default"
    return WorldUnion(
        suite=BehaviourSuite(cfg_cluster=first, specs=tuple(specs),
                             helpers=tuple(sorted(helpers.items()))),
        local_id=local,
        examples=partition((s.behaviour_id for s in specs), lambda u: placed[u], worlds))


def union_glossary(per_world: Mapping[str, Sequence[GlossaryEntry]]) -> tuple[GlossaryEntry, ...]:
    """Every world's vocabulary once, sorted by name; a name some worlds
    lack carries the worlds that have it. One name's entries from several
    worlds merge field-wise (`GlossaryEntry.merged`)."""
    worlds = sorted(per_world)
    first: dict[str, GlossaryEntry] = {}
    for w in worlds:
        for g in per_world[w]:
            prior = first.get(g.name)
            first[g.name] = g if prior is None else prior.merged(g)
    where = membership({w: [g.name for g in per_world[w]] for w in worlds})
    names = partition(sorted(first), where, worlds)
    return tuple(replace(first[p.element], worlds=names.mark(p)) for p in names.placed)


def _in_world(g: GlossaryEntry, world: str) -> bool:
    return not g.worlds or world in g.worlds


def project_book(book: BehaviourSuite, union: WorldUnion, world: str,
                 suite: BehaviourSuite) -> BehaviourSuite:
    """`world`'s store from the one book: `suite`'s own specs (never the
    union's), each feature and rule restricted to the examples `world`
    compiles and renamed back to their local ids (a rule's `refuted_on`
    too), a feature or rule left with no credited example dropped, a rule's
    aiming terms restricted to the names `world` compiles, and the glossary
    names `world` compiles."""
    here = union.ids_in(world)
    glossary = tuple(g for g in book.glossary if _in_world(g, world))
    names = {g.name for g in glossary}

    def local(ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(union.local_id[i] for i in ids if i in here))

    features: list[Feature] = []
    for f in book.features:
        ex = local(f.example_ids)
        if not ex:
            continue
        rules: list[Rule] = []
        for r in f.rules:
            cited = local(r.example_ids)
            if cited:
                rules.append(replace(r, example_ids=cited, refuted_on=local(r.refuted_on),
                                     terms=tuple(t for t in r.terms if t in names)))
        features.append(replace(f, example_ids=ex, rules=tuple(rules),
                                unaided_ids=local(f.unaided_ids)))
    return BehaviourSuite(
        cfg_cluster=world,
        specs=tuple(replace(s, rules=()) for s in suite.specs),
        helpers=suite.helpers,
        features=tuple(features),
        glossary=glossary,
        book_worlds=union.clusters,
        givens=book.givens)


def book_is_current(suites: Mapping[str, BehaviourSuite]) -> bool:
    """Every store carries a book built for exactly these worlds, together.
    False when a store has no book, or its book was built for another world
    set or for its cluster alone — then the one book has to be built."""
    worlds = tuple(sorted(suites))
    return bool(suites) and all(s.features and s.book_worlds == worlds for s in suites.values())
