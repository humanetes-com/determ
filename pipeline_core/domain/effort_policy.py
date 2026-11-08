"""Which reasoning level one model call is sent at, decided once per call.

WHY. Until 2026-11-07 the level was one process-wide value at the maximum
for every call (Qwen3.8 `reasoning_effort=xhigh`, Muse `reasoning_strength=
high`), so a judge call of the behaviour book and a stuck task 2a function
reasoned alike. On run `336559-eb0222a5` the book's prediction calls used
2.7x Muse's completion tokens per call at the same decode rate: the wall of
t0d was reasoning length.

The level is decided here, from three inputs, by one rule:

- the policy's default (`LLM_EFFORT`, ``medium`` when unset);
- per-phase levels (`LLM_EFFORT_PHASES`, ``phase=level`` pairs, comma
  separated); a call's phase path is its innermost open phase step and that
  step's parents, and the first configured phase on the path wins, so
  ``t0d.book=max`` beats ``t0d=medium`` for a book call;
- an escalation the caller holds (a task 2a run whose owed targets owe a
  maximum try, `candidate_tiers.AskLedger.level_for`), which wins over both.

Pure: callers pass the env mapping and the phase path in. The process-wide
binding and the escalation scope live in `pipeline_core.effort`.

Nothing here names a crate, file, test or symbol.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from .model_profile import EffortLevel

__all__ = ["EffortPolicy", "effort_for", "parse_level"]


def parse_level(raw: str, *, name: str) -> EffortLevel:
    """One level word (`medium` / `max`). Anything else raises: a typo
    silently falling back to the default would run a whole phase at a level
    nobody chose, and the only symptom is a reading that does not compare."""
    v = raw.strip().lower()
    try:
        return EffortLevel(v)
    except ValueError:
        raise ValueError(
            f"{name}={raw!r}: expected one of "
            f"{', '.join(lv.value for lv in EffortLevel)}") from None


@dataclass(frozen=True, slots=True)
class EffortPolicy:
    """The configured levels: a default and per-phase overrides."""

    default: EffortLevel = EffortLevel.MEDIUM
    phases: tuple[tuple[str, EffortLevel], ...] = field(default=())

    @classmethod
    def from_env(cls, env: Mapping[str, str], *,
                 known_phases: Collection[str] = ()) -> EffortPolicy:
        """`LLM_EFFORT` and `LLM_EFFORT_PHASES`. A phase id not in
        `known_phases` (when given) raises, for the same reason as
        :func:`parse_level`."""
        raw_default = (env.get("LLM_EFFORT") or "").strip()
        default = (parse_level(raw_default, name="LLM_EFFORT")
                   if raw_default else EffortLevel.MEDIUM)
        pairs: list[tuple[str, EffortLevel]] = []
        for item in (env.get("LLM_EFFORT_PHASES") or "").split(","):
            item = item.strip()
            if not item:
                continue
            phase, eq, level = item.partition("=")
            phase = phase.strip()
            if not eq or not phase:
                raise ValueError(
                    f"LLM_EFFORT_PHASES item {item!r}: expected phase=level")
            if known_phases and phase not in known_phases:
                raise ValueError(
                    f"LLM_EFFORT_PHASES names unknown phase {phase!r}")
            pairs.append((phase, parse_level(level, name=f"LLM_EFFORT_PHASES[{phase}]")))
        return cls(default=default, phases=tuple(pairs))

    def describe(self) -> str:
        """The startup / cluster-entry form: ``effort=medium`` plus each
        configured phase."""
        tail = "".join(f", {p}={lv.value}" for p, lv in self.phases)
        return f"effort={self.default.value}{tail}"


def effort_for(phase_path: Sequence[str], escalated: EffortLevel | None,
               policy: EffortPolicy) -> EffortLevel:
    """The level of one call. `phase_path` is innermost first (the open
    step, then its parents); `escalated` is the caller's escalation scope,
    None when it holds none."""
    if escalated is not None:
        return escalated
    by_phase = dict(policy.phases)
    for phase in phase_path:
        if phase in by_phase:
            return by_phase[phase]
    return policy.default
