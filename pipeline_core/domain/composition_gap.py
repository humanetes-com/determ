"""Composition-coverage gap finder (F10 synthesis tier / M7) — pure domain.

The user's lego argument: unit tests under-determine how units COMPOSE. Harvested
behaviours cover the compositions the library's own tests/examples happen to
exercise; the GAP is the units no harvested behaviour touches. Synthesis targets
exactly that gap — it authors new public-API usage programs for the uncovered
units (never for well-covered ones, which would just burn tokens re-teaching).

Coverage is by ``exercises`` (the unit ids a behaviour touches). Pure set logic;
the caller supplies the full unit inventory and the harvested suite.
"""
from __future__ import annotations

from pipeline_core.domain.behaviour import BehaviourSpec


def covered_units(specs: tuple[BehaviourSpec, ...]) -> frozenset[str]:
    """Every unit id some harvested behaviour already exercises."""
    out: set[str] = set()
    for s in specs:
        out |= s.exercises
    return frozenset(out)


def uncovered_units(
    all_unit_ids: frozenset[str], specs: tuple[BehaviourSpec, ...],
) -> frozenset[str]:
    """Units no harvested behaviour exercises — the composition gap synthesis
    should target. Empty when the harvest already covers everything."""
    return all_unit_ids - covered_units(specs)


def synthesis_targets(
    all_unit_ids: frozenset[str], specs: tuple[BehaviourSpec, ...],
    *, max_targets: int = 12,
) -> tuple[str, ...]:
    """A deterministic, capped list of uncovered units to synthesize for (sorted;
    a bounded batch keeps the synthesis prompt inside budget)."""
    return tuple(sorted(uncovered_units(all_unit_ids, specs)))[:max_targets]
