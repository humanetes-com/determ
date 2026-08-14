"""The lever vocabulary — every reaction the pipeline can take to a stall,
as a typed registry naming the mechanism that owns it.

WHY.  The 2026-08-14 pattern census (`scripts/pattern_census.py`, 34 anyhow
runs) showed that the pipeline's stall reactions exist but are each welded to
one bespoke trigger: the signature memory reacts to a re-fired component, the
re-base to a retrigger's re-stub, the keep-and-defer to a blocked U2 batch.
A new instance of a known class therefore waits for a new mechanism instead
of reusing an existing reaction (frame-blind blame waited nine days; the
identical chain-test escalation recurred across 11 runs).  The planned
diagnosis step — detector fires, evidence pack goes to the local model, the
verdict names a lever — needs this enumeration as its ACTION SPACE: a model
can only select from levers that exist, and a selected lever must be
executable by the mechanism that already owns it, under that mechanism's own
guards.

WHAT THIS IS NOT.  No behaviour change: nothing dispatches through this
registry yet, and existing mechanisms keep calling their own reactions
directly.  The registry is the typed surface — names, owners, and what
evidence each lever demands — validated mechanically by
`tests/test_levers.py`, which imports every owner and asserts the symbol
exists, so a refactor that moves a mechanism breaks the registry loudly
instead of silently orphaning a lever the diagnosis step would later select.

Each lever's `evidence` field states what a verdict selecting it must cite —
the mechanical-validation contract: a citation that cannot be checked against
the tree, the log, or a store is grounds to reject the verdict outright.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Lever:
    """One reaction the pipeline already owns.

    `owner_module`/`owner_symbol` locate the mechanism (import-checked by the
    registry test); `summary` is what pulling the lever does; `evidence` is
    what a diagnosis verdict must cite for the selection to be valid."""
    name: str
    owner_module: str
    owner_symbol: str
    summary: str
    evidence: str


LEVERS: tuple[Lever, ...] = (
    Lever(
        name="rebase_baseline",
        owner_module="pipeline_core.domain.pass_highwater",
        owner_symbol="PassHighWater",
        summary="Re-base a progress high-water after a deliberate "
                "destruction, so retry gates compare against the tree that "
                "exists rather than the one the destruction removed.",
        evidence="The destruction event (re-stub / restore / merge) and the "
                 "stale baseline value, both from this run's log.",
    ),
    Lever(
        name="readmit_exhausted",
        owner_module="pipeline_core.domain.destruction_epoch",
        owner_symbol="readmission_boundary",
        summary="Release persisted fully-exhausted flags so convicted fns "
                "re-enter the frontier for one informed attempt.",
        evidence="The progress or epoch movement since conviction "
                 "(readmission_warranted's own inputs).",
    ),
    Lever(
        name="retire_record",
        owner_module="pipeline_core.domain.retro_gate",
        owner_symbol="admissible_guidance",
        summary="Withhold a stored retro record from prompts because its "
                "premise no longer holds (hollow tree, cleared blocker, "
                "unfounded impossibility, layout drift).",
        evidence="The record key and the premise check that fails against "
                 "the tree on disk (a blocker that filled, a member that "
                 "exists).",
    ),
    Lever(
        name="refute_aim",
        owner_module="pipeline_core.domain.layout_retrigger",
        owner_symbol="crash_component_refuted",
        summary="Mark a component's re-author as spent against a crash "
                "signature so the same aim is never repeated on unchanged "
                "evidence.",
        evidence="The crash signature and the landed re-author that failed "
                 "to clear it (both in the attempts store / log).",
    ),
    Lever(
        name="aim_component",
        owner_module="pipeline_core.domain.layout_retrigger",
        owner_symbol="crash_candidate_grade",
        summary="Choose the next component for a layout re-author from crash "
                "evidence (observed frame > chain-derived > registry order).",
        evidence="The blame the aim rests on, with its provenance grade.",
    ),
    Lever(
        name="synthesize_blame",
        owner_module="pipeline_core.domain.crash_site",
        owner_symbol="chain_derived_site",
        summary="For a deterministic frame-blind crash, synthesize "
                "provenance-marked blame from the crashed tests' "
                "construction chain (prompts, frontier, exhaustion void — "
                "never conviction).",
        evidence="The determinism proof (reproduced shrink) and the "
                 "origin-capable detector's empty in-crate frame set.",
    ),
    Lever(
        name="widen_attribution",
        owner_module="pipeline_core.application.reconstruct.attribution",
        owner_symbol="escalate_frontier_to_producers",
        summary="Escalate a failing test's frontier past futile accessors to "
                "producers and the trait-impl traversal steps they dispatch "
                "through.",
        evidence="The futile accessors (served rewrites, still failing) and "
                 "the type/traversal derivation.",
    ),
    Lever(
        name="keep_and_defer",
        owner_module="pipeline_core.domain.dispatch",
        owner_symbol="sibling_blocked_u2_failures",
        summary="Keep a compiling body whose tests are blocked by "
                "still-stubbed siblings and defer judgement instead of "
                "burning fix-loop calls it cannot pay for.",
        evidence="Every failing attributed test's still-stubbed sibling, "
                 "from the pass-start tree scan.",
    ),
    Lever(
        name="close_walk",
        owner_module="pipeline_core.domain.layout_retrigger",
        owner_symbol="settle_crash_payoff",
        summary="Settle a retrigger walk's payoff and close it after "
                "consecutive zero-payoff fires, letting the stall exit "
                "stand instead of spending further authoring calls on "
                "already-failed evidence.",
        evidence="The zero-payoff fires (signature reproduced after each "
                 "landed re-author).",
    ),
    Lever(
        name="stop_with_diagnosis",
        owner_module="pipeline_core.domain.zero_payoff",
        owner_symbol="zero_payoff_streak",
        summary="Exit the run early with the evidence pack written out — a "
                "distilled-walk input instead of hours of zero-payoff "
                "sweeps. The fallback lever when no other lever's evidence "
                "requirement can be met.",
        evidence="The zero-payoff streak itself (targets + flat payoff "
                 "vector).",
    ),
)


def validate_registry() -> list[str]:
    """Import every lever's owner and check the symbol exists.  Returns the
    problems (empty = valid).  Called by the registry test; cheap enough to
    call from any future dispatcher before trusting a verdict."""
    problems: list[str] = []
    seen: set[str] = set()
    for lever in LEVERS:
        if lever.name in seen:
            problems.append(f"duplicate lever name: {lever.name}")
        seen.add(lever.name)
        try:
            mod = importlib.import_module(lever.owner_module)
        except Exception as exc:  # pragma: no cover - import failure IS the finding
            problems.append(f"{lever.name}: cannot import "
                            f"{lever.owner_module}: {exc}")
            continue
        if not hasattr(mod, lever.owner_symbol):
            problems.append(f"{lever.name}: {lever.owner_module} has no "
                            f"symbol {lever.owner_symbol}")
    return problems
