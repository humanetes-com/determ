"""Behaviour-oracle report (F10 Use-A / M4) — pure domain.

Running the harvested positive behaviours against the reconstruction yields a
:class:`BehaviourVerdict` per spec. This VO tallies them and, for each MISMATCH,
names the units the failing behaviour EXERCISES — the localizer a mismatch
escalates through (the reconstruction reproduces a compiles-but-wrong observable
a unit test did not catch). Attribution over a large ``exercises`` set is weak by
nature (see M9: bisection is the real localizer); this report just carries the
evidence, it does not claim a single culprit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourVerdict


@dataclass(frozen=True, slots=True)
class BehaviourOutcome:
    """One behaviour's result against the reconstruction."""
    behaviour_id: str
    verdict: BehaviourVerdict
    exercises: frozenset[str] = frozenset()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BehaviourOracleReport:
    """The oracle's verdict over a cluster's positive behaviours. ``holds`` +
    ``mismatches`` widen the pass criterion from 'unit tests pass' to '+
    behaviours hold' without touching the unit-test tally."""
    cfg_cluster: str
    outcomes: tuple[BehaviourOutcome, ...] = field(default_factory=tuple)

    @property
    def holds(self) -> tuple[BehaviourOutcome, ...]:
        return tuple(o for o in self.outcomes if o.verdict is BehaviourVerdict.HOLD)

    @property
    def mismatches(self) -> tuple[BehaviourOutcome, ...]:
        return tuple(o for o in self.outcomes
                     if o.verdict is BehaviourVerdict.MISMATCH)

    @property
    def all_hold(self) -> bool:
        return all(o.verdict is BehaviourVerdict.HOLD for o in self.outcomes)

    @property
    def suspect_units(self) -> frozenset[str]:
        """Union of the units every MISMATCH exercises — the escalation frontier
        the behaviour oracle contributes (weak localizer; see M9)."""
        out: set[str] = set()
        for o in self.mismatches:
            out |= o.exercises
        return frozenset(out)


def summarize(spec: BehaviourSpec, verdict: BehaviourVerdict,
              detail: str = "") -> BehaviourOutcome:
    """Fold one run into a :class:`BehaviourOutcome`, carrying the spec's
    ``exercises`` set as the mismatch localizer."""
    return BehaviourOutcome(
        behaviour_id=spec.behaviour_id, verdict=verdict,
        exercises=spec.exercises, detail=detail)
