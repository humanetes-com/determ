"""Which other bodies a unit may write in the same transaction — one answer
for every route that opens a unit.

The question had seven answerers, each added after one run and each keyed on
the shape that fired (one file, a fn-pointer table, a formatter hop, a
crash stack's homonym). Run `12616-8345ae1d` and `59479-cc3afffd`
(2026-09-27) show what that leaves: a three-body recursion in two files
(`src/kind.rs::new [impl Boxed]` -> `from_boxed` -> `construct_from_boxed`
-> back) was opened by a per-fn run, a file run and an impl run, each of
which could write one member, and the only route whose write set spanned
files (the slice session) held both members only after its planner learned
to resolve stack frames by span.

The model this module holds: two bodies are COUPLED when one cannot be made
right without the other, and the evidence for that is one of

- ``CALL_CYCLE`` — the two sit on one cycle of the tree's own call graph
  (`reference_graph`): each reaches the other, so a body that breaks the
  cycle has to be written with the member it stops calling;
- ``OBSERVED_CYCLE`` — the two are members of one repeating period of a
  faulting stack the sweep observed (`crash_site.StackCycle`), resolved to
  the declared bodies by span. This is the same fact for a cycle the source
  does not spell (through a fn pointer or a trait object).

Every route asks :func:`coupled_units`. The sweep's observed cycles are
bound here by the driver (:func:`bind_observed`), the same way the coverage
reading is bound, so a route cannot forget to pass them; the call graph is
computed from the records the route already holds, so it describes the tree
the route will write. A coupling names no crate symbol, no test and no
language construct: a call edge is spelled by a path, a cycle by a stack.

What it deliberately does not cover. The fn-pointer table complement
(`installable_fns.table_complement`) is a slot-fit, not a coupling: it
exists for a fn nothing references yet, which no graph can see, and the
session composes it beside this. The formatter pair is a route, not a write
set. Neither is folded in here.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from pipeline_core.domain.attribution import canon_site
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.reference_graph import ReferenceGraph, reference_graph


class Coupling(Enum):
    CALL_CYCLE = "call_cycle"
    OBSERVED_CYCLE = "observed_cycle"


@dataclass(frozen=True, slots=True)
class CoupledUnit:
    site: FnSite
    coupling: Coupling

    @property
    def reason(self) -> str:
        return {
            Coupling.CALL_CYCLE: "calls into a cycle with it on the tree",
            Coupling.OBSERVED_CYCLE: "repeats with it on a crashed stack",
        }[self.coupling]


_observed: tuple[frozenset[FnSite], ...] = ()


def bind_observed(cycles: Iterable[Collection[FnSite]]) -> None:
    """Install the sweep's observed cycles (each a set of declared bodies).
    Driver-owned, once per sweep; an empty iterable clears."""
    global _observed
    canon = (frozenset(canon_site(x) for x in c) for c in cycles)
    _observed = tuple(c for c in canon if len(c) > 1)


def observed() -> tuple[frozenset[FnSite], ...]:
    return _observed


def coupled_units(
    targets: Collection[FnSite],
    records: Sequence[FnRecord],
    *,
    cap: int,
    graph: ReferenceGraph | None = None,
) -> tuple[CoupledUnit, ...]:
    """The bodies coupled to any of ``targets``, never a target, at most
    ``cap``, call cycles first (the source is the tree the write lands on),
    then observed cycles; stable order within each. ``records`` are the
    cluster's current records; ``graph`` may be passed when the caller
    already built it from those records."""
    if cap <= 0 or not targets:
        return ()
    want = frozenset(canon_site(t) for t in targets)
    g = graph if graph is not None else reference_graph(records)
    out: dict[FnSite, CoupledUnit] = {}
    for t in sorted(want):
        for s in sorted(g.cycle_of(t)):
            if s not in want and s not in out:
                out[s] = CoupledUnit(s, Coupling.CALL_CYCLE)
    for cyc in _observed:
        if not cyc & want:
            continue
        for s in sorted(cyc):
            if s not in want and s not in out:
                out[s] = CoupledUnit(s, Coupling.OBSERVED_CYCLE)
    return tuple(out.values())[:cap]


def records_by_site(
    records: Iterable[FnRecord],
    site_of: Callable[[FnRecord], FnSite],
) -> dict[FnSite, FnRecord]:
    """Index records by site, first record wins (a cfg-duplicate spelling
    of one body resolves to the first the extractor emitted)."""
    out: dict[FnSite, FnRecord] = {}
    for r in records:
        out.setdefault(site_of(r), r)
    return out


def render_coupled(
    items: Sequence[tuple[CoupledUnit, str, str]],
) -> str:
    """The COUPLED_BODIES section of a unit's prompt: each coupled body with
    where it is, why it is coupled, its signature and its CURRENT body
    (``items`` = (coupled unit, signature, body text)). Observation only —
    the section says what the tree does, not what to write; the output
    contract says how an extra is named. "" when there is nothing."""
    if not items:
        return ""
    parts = ["These bodies are coupled to the target on the current tree. "
             "A body that changes the coupling usually has to be written "
             "with them in one change; each may be given as an extra <FN> "
             "block (see Output)."]
    for cu, sig, body in items:
        where = cu.site.file + (f" — {cu.site.container}"
                                if cu.site.container else "")
        parts.append(f"// {where} — {cu.reason}\n{sig} {{\n{body.rstrip()}\n}}")
    return "\n\n".join(parts)
