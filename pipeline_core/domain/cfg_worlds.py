"""A cluster is a build world, not a reconstruction run.

From the behaviour side a crate has one specification: a behaviour is a
property of the crate, and a cluster is a build world (cfg atoms, toolchain,
env) in which a subset of its items and tests is compiled. Every element a
model is asked about therefore splits into a CORE, compiled in every world,
and a RESIDUE, compiled in a proper subset of them. Asking about the core once
per world is the same question asked again.

Measured on run `72627-751534fe` (2026-09-30, anyhow, clusters `stable` and
`nightly`): the two behaviour suites held 227 specs each, identical in every
field but `cfg_cluster`; the two glossaries differed by one name
(`Error::thiserror_provide`, nightly only); the strip bake built the same
behaviour book twice, 10,090 s for stable and 10,639 s for nightly, one call
at a time.

This module is the one partition every per-world consumer reads through.
`partition` places each element in the worlds that compile it; the per-world
stores are written FROM a partition, never built per world, so a new consumer
cannot recreate the double ask by forgetting a call. Where an element's
worlds come from is the caller's evaluation — `worlds_of` for an element
carrying cfg predicates (the one "is this compiled here" context,
`cfg.cluster_cfg_set`), membership in a per-world record for an element that
an earlier per-world evaluation already filtered (a glossary built under each
world's cfg set). No second evaluator.

Generic by construction: it names no crate, test, unit or cfg atom, and never
branches on a cluster's name. A crate whose worlds are cargo features gets
the same partition.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from pipeline_core.domain.cfg import cluster_cfg_set

type Worlds = frozenset[str]


def worlds_of(predicates: Sequence[str], clusters: Sequence[object]) -> Worlds:
    """Names of the clusters that compile an element gated by the
    conjunction `predicates`.

    Evaluated under `cluster_cfg_set(c)`; an undecidable predicate keeps the
    element, the conservative direction every cluster-membership reader
    already takes (`private_surface._cfg_excluded`): wrongly placing an
    element in a world costs one extra example in a prompt, wrongly leaving
    it out loses the element there. `clusters` are `BuildCluster`-shaped
    (`name` plus the fields `cluster_cfg_set` reads)."""
    out: set[str] = set()
    for c in clusters:
        cfg_set = cluster_cfg_set(c)
        if not any(cfg_set.evaluate(p) is False for p in predicates):
            out.add(str(getattr(c, "name", "")))
    return frozenset(out)


def world_mark(worlds: Iterable[str], every: Iterable[str]) -> tuple[str, ...]:
    """The mark an element carries in a prompt or a store: `()` when it is
    compiled in every world (the core needs no mark), else its worlds,
    sorted."""
    ws = frozenset(worlds)
    return () if ws >= frozenset(every) else tuple(sorted(ws))


@dataclass(frozen=True, slots=True)
class Placed[T]:
    """One element and the worlds that compile it."""

    element: T
    worlds: Worlds


@dataclass(frozen=True, slots=True)
class Partition[T]:
    """Elements placed in worlds, in their input order.

    `core` is compiled in every world, `residue[w]` is what only a proper
    subset of worlds compiles (listed under each of them), `nowhere` is an
    element no world compiles (kept so a census can count it, never handed
    to a world)."""

    clusters: tuple[str, ...]
    placed: tuple[Placed[T], ...]

    def is_core(self, p: Placed[T]) -> bool:
        return bool(self.clusters) and p.worlds >= frozenset(self.clusters)

    @property
    def core(self) -> tuple[T, ...]:
        return tuple(p.element for p in self.placed if self.is_core(p))

    @property
    def residue(self) -> dict[str, tuple[T, ...]]:
        return {w: tuple(p.element for p in self.placed
                         if w in p.worlds and not self.is_core(p))
                for w in self.clusters}

    @property
    def nowhere(self) -> tuple[T, ...]:
        return tuple(p.element for p in self.placed if not p.worlds)

    def in_world(self, world: str) -> tuple[T, ...]:
        """Everything `world` compiles, core and residue, in input order."""
        return tuple(p.element for p in self.placed if world in p.worlds)

    def mark(self, p: Placed[T]) -> tuple[str, ...]:
        return world_mark(p.worlds, self.clusters)

    def sizes(self) -> dict[str, int]:
        """`core`, `nowhere`, and one `residue:<world>` count per world."""
        out = {"core": len(self.core), "nowhere": len(self.nowhere)}
        out.update({f"residue:{w}": len(v) for w, v in self.residue.items()})
        return out


def partition[T](
    elements: Iterable[T],
    worlds_of_element: Callable[[T], Iterable[str]],
    clusters: Sequence[str],
) -> Partition[T]:
    """Place every element in the worlds `worlds_of_element` names,
    restricted to `clusters`."""
    names = tuple(sorted(set(clusters)))
    known = frozenset(names)
    return Partition(
        clusters=names,
        placed=tuple(Placed(e, frozenset(worlds_of_element(e)) & known) for e in elements))


def membership(per_world: Mapping[str, Iterable[str]]) -> Callable[[str], Worlds]:
    """`key → the worlds whose record lists it`, for elements a per-world
    evaluation already filtered (a glossary built under each world's cfg
    set). Reads that evaluation instead of re-deriving it."""
    index: dict[str, set[str]] = {}
    for w, keys in per_world.items():
        for k in keys:
            index.setdefault(k, set()).add(w)
    return lambda k: frozenset(index.get(k, ()))


def gate_for_worlds(worlds: Iterable[str], atoms: Mapping[str, str]) -> str:
    """The cfg predicate under which code is compiled in exactly `worlds`,
    rendered from each world's own gate (`atoms`: world → the predicate that
    is true in that world and false in the others).

    Empty when `worlds` is every world — the element is core and carries no
    gate — or when a world in it has no gate of its own (a crate with one
    world). One world gives its atom; several give `any(…)` of theirs, in
    name order. An element in no world is a caller's error: something that
    was never shown to build anywhere must not come out ungated."""
    every = frozenset(atoms)
    ws = frozenset(worlds) & every
    if not ws:
        raise ValueError("gate_for_worlds: no known world to gate to")
    if ws >= every:
        return ""
    preds = [atoms[w] for w in sorted(ws)]
    if not all(preds):
        return ""
    return preds[0] if len(preds) == 1 else "any(" + ", ".join(preds) + ")"
