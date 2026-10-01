"""Use-case edges: compositions of two public items, and which of them an
example already asserts.

A feature of the behaviour book is ONE public item, and a probe aimed at an
item varies that item's inputs. The differences the 2026-08-25 comparison
recorded were not of that kind: each item was right alone on the inputs its
test wrote, and wrong (or simply never observed) in combination — a value
built one way, then asked something. An EDGE is that combination: a public
item whose result type is another public item's receiver or argument type.

Types are read from the declarations the glossary carries
(`GlossaryEntry.signature`), by name. Two spellings never name a type and
are resolved from the entry's own qualifier: `Self` in a declaration of
`Type::f` is `Type`, and a `self` receiver (`self`, `&self`, `&mut self`) of
`Type::f` takes a `Type`. Without that a constructor declared
`fn new(..) -> Self` connects to nothing and every method is unreachable,
which is every edge of a crate built around one type.

ASSERTED, not co-named. An edge is asserted when one example's observed
expression (`when`) calls the consumer and its code names the producer. An
example that merely calls both and observes something else leaves the
composition's result unobserved: the recorded silent divergences were
properties of items the original tests already call without asserting what
they return.

Known limits, stated so the reading is not over-read: a macro has no
declaration and is no producer here; a method of a public trait takes its
implementor, which the declaration does not name, so it is a consumer only
of types its other parameters name; a result reached through a trait-object
view (`&dyn Error`) is not an edge of this shape.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from pipeline_core.domain.behaviour import BehaviourSpec, GlossaryEntry, example_code
from pipeline_core.domain.rust_scan import contains_word

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RECEIVER = re.compile(r"^\s*(?:&\s*(?:'\w+\s+)?)?(?:mut\s+)?self\b")
_TYPE_KINDS = frozenset({"struct", "enum", "type"})


@dataclass(frozen=True, slots=True, order=True)
class UseCaseEdge:
    """`producer`'s result is a `via`, which `consumer` takes."""

    consumer: str
    producer: str
    via: str


def _split_signature(sig: str) -> tuple[str, str]:
    """`(parameter list, result type)` of a fn declaration, each "" when
    absent. The parameter list is the first parenthesised group at angle
    depth 0 (generic bounds may hold parentheses: `F: Fn(A) -> B`)."""
    depth = 0
    start = -1
    for i, ch in enumerate(sig):
        if ch == "<":
            depth += 1
        elif ch == ">" and i and sig[i - 1] != "-":
            depth = max(0, depth - 1)
        elif ch == "(" and depth == 0:
            start = i
            break
    if start < 0:
        return "", ""
    level = 0
    end = len(sig)
    for i in range(start, len(sig)):
        if sig[i] == "(":
            level += 1
        elif sig[i] == ")":
            level -= 1
            if level == 0:
                end = i
                break
    rest = sig[end + 1:]
    arrow = rest.find("->")
    result = rest[arrow + 2:] if arrow >= 0 else ""
    where = re.search(r"\bwhere\b", result)
    return sig[start + 1:end], (result[:where.start()] if where else result).strip(" {;")


def _qualifier(entry: GlossaryEntry, types: Collection[str]) -> str:
    """The type `Self` means in `entry`'s declaration, "" when the entry is
    a free fn or its qualifier is not one of the crate's public types (a
    trait's method: the implementor is not named)."""
    head = entry.name.split("::", 1)[0] if "::" in entry.name else ""
    return head if head in types else ""


def _named(text: str, types: Collection[str], self_type: str) -> set[str]:
    out = set()
    for tok in _IDENT.findall(text):
        if tok == "Self" and self_type:
            out.add(self_type)
        elif tok in types:
            out.add(tok)
    return out


def result_types(entry: GlossaryEntry, types: Collection[str]) -> set[str]:
    """The crate's public types `entry` returns (inside any wrapper)."""
    _params, result = _split_signature(entry.signature)
    return _named(result, types, _qualifier(entry, types))


def input_types(entry: GlossaryEntry, types: Collection[str]) -> set[str]:
    """The crate's public types `entry` takes: its receiver's, and any its
    parameters name."""
    params, _result = _split_signature(entry.signature)
    own = _qualifier(entry, types)
    out = _named(params, types, own)
    if own and _RECEIVER.match(params):
        out.add(own)
    return out


def use_case_edges(glossary: Sequence[GlossaryEntry]) -> tuple[UseCaseEdge, ...]:
    """Every edge of the public API, sorted (consumer, producer, type)."""
    types = {g.name for g in glossary if g.kind in _TYPE_KINDS}
    fns = [g for g in glossary if g.kind == "fn" and g.signature]
    makes = {g.name: result_types(g, types) for g in fns}
    takes = {g.name: input_types(g, types) for g in fns}
    out = {UseCaseEdge(c.name, p.name, t)
           for p in fns for c in fns if p.name != c.name
           for t in makes[p.name] & takes[c.name]}
    return tuple(sorted(out))


def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


def asserted_edges(edges: Iterable[UseCaseEdge],
                   specs: Iterable[BehaviourSpec]) -> frozenset[UseCaseEdge]:
    """The edges some example ASSERTS: its `when` calls the consumer and its
    code names the producer."""
    observed = [(s.when, example_code(s)) for s in specs if s.then is not None]
    return frozenset(
        e for e in edges
        if any(contains_word(when, _leaf(e.consumer)) and contains_word(code, _leaf(e.producer))
               for when, code in observed))


def edges_to_ask(glossary: Sequence[GlossaryEntry], specs: Sequence[BehaviourSpec], *,
                 max_edges: int) -> tuple[tuple[UseCaseEdge, ...], int, int]:
    """`(edges to probe, asserted count, edge count)`. The edges to probe
    are unasserted ones, taken a round at a time: each round gives every
    consumer that still has one its next edge, from the producer chosen
    least so far. Consumers no example observes at all come first (their
    result is nowhere in the oracle). Capped at `max_edges`. A walk in name
    order would spend the whole cap on the first consumer's producers, or
    build every value with the alphabetically first constructor."""
    edges = use_case_edges(glossary)
    done = asserted_edges(edges, specs)
    whens = [s.when for s in specs if s.then is not None]
    unobserved = {e.consumer for e in edges
                  if not any(contains_word(w, _leaf(e.consumer)) for w in whens)}
    open_by: dict[str, list[UseCaseEdge]] = {}
    for e in edges:
        if e not in done:
            open_by.setdefault(e.consumer, []).append(e)
    consumers = sorted(open_by, key=lambda c: (c not in unobserved, c))
    used: dict[str, int] = {}
    out: list[UseCaseEdge] = []
    while len(out) < max_edges and any(open_by.values()):
        for c in consumers:
            if not open_by[c] or len(out) >= max_edges:
                continue
            pick = min(open_by[c], key=lambda e: (used.get(e.producer, 0), e.producer, e.via))
            open_by[c].remove(pick)
            used[pick.producer] = used.get(pick.producer, 0) + 1
            out.append(pick)
    return tuple(out), len(done), len(edges)
