"""Layout re-trigger ACTION (F11 M3) — the TDD driver's escape hatch when a
cluster's per-fn fix loop is stalled by a private layout it can't work with.

`domain/layout_retrigger.py` decides WHETHER to retrigger; this module does
it: narrow the cluster's private surface down to ONE frozen component, ask the
model for a fresh layout attempt (independent LLM sample — a real chance at a
structurally different, simpler design, since the model is free to invent its
own scheme per the reconstruction-target rule), and on success surgically
revert every already-filled fn that references the component's types so the
per-fn loop re-fills them under the new layout. Everything else in the
cluster — fills unrelated to this component — is left untouched.

Capped per (cluster, component) via a disk-persisted attempt counter (mirrors
the debt-#63 `RETRO_RESET_MAX_ATTEMPTS` shape) so a component that can't be
re-authored within the cap degrades to today's behaviour: exhausted, left
frozen, the TDD driver's existing stall-exit stands.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    LayoutOutcome,
    author_layout,
)
from pipeline_core.application.author_layout_phase import make_pin_selector
from pipeline_core.domain.behaviour import BehaviourSuite
from pipeline_core.domain.layout_retrigger import (
    CrashEvidence,
    LayoutRetriggerCause,
    classify_crash_retrigger,
    classify_layout_retrigger,
    find_fns_referencing,
    should_retrigger,
)
from pipeline_core.domain.models import FnLocator, FnRecord
from pipeline_core.domain.private_surface import PrivateSurface
from pipeline_core.domain.starvation import classify_starvation
from pipeline_core.prompts.layout_authoring import CRASH_GUIDANCE

DEFAULT_MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class RetriggerResult:
    fired: bool
    succeeded: bool = False
    reverted_count: int = 0
    cause: str = ""
    reason: str = ""


@dataclass(frozen=True)
class RetriggerLayoutDeps:
    log: Callable[[str], None]
    load_surface: Callable[[Path, str], PrivateSurface]
    load_layout_outcomes: Callable[[Path, str], list[tuple[frozenset[str], LayoutOutcome]]]
    load_suite: Callable[[Path, str], BehaviourSuite]
    public_surface_text: Callable[[Path, str], str]
    symbol_inventory_text: Callable[[Path, str], str]
    author_deps_for: Callable[[Path, str], AuthorLayoutDeps]
    # Enumerate fns over the LIVE (already-filled) tree — used to find which
    # fns reference a just-reauthored component's types.
    enumerate_fns: Callable[[Path], list[FnRecord] | None]
    # Revert each located fn's body to `unimplemented!()` in the live tree;
    # returns how many were actually changed (already-stub fns don't count).
    revert_fns: Callable[[list[FnLocator], Path], int]
    git_commit: Callable[[Path, str], bool]
    load_attempts: Callable[[Path, str], dict[str, int]]
    save_attempts: Callable[[Path, str, dict[str, int]], Path]
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


def _component_key(names: frozenset[str]) -> str:
    return "|".join(sorted(names))


def retrigger_layout(
    project_dir: Path, module_name: str, cfg_cluster: str,
    filled_src_dir: Path, error_text: str,
    deps: RetriggerLayoutDeps,
    crash_evidence: CrashEvidence | None = None,
) -> RetriggerResult:
    """Try to re-author whichever FROZEN component's errors are implicated.
    Returns fired=False when no frozen component matches (the caller's
    existing stall-exit behaviour should proceed unchanged).

    Two evidence channels select a component: compile-error text naming its
    private types (causes A/B, `classify_layout_retrigger`), or — when
    `crash_evidence` is given — runtime-crash implication (cause C,
    `classify_crash_retrigger`: a vanished test's reverse-map target is or
    references a component member; no error text exists in that class)."""
    outcomes = deps.load_layout_outcomes(project_dir, cfg_cluster)
    frozen = [names for names, outcome in outcomes if outcome is LayoutOutcome.FROZEN]
    if not frozen:
        return RetriggerResult(fired=False, reason="no frozen component recorded")

    attempts = deps.load_attempts(project_dir, cfg_cluster)
    starv_cause, _evidence = classify_starvation("", "", error_text)
    _live_records: list[FnRecord] | None = None

    for names in frozen:
        if crash_evidence is not None:
            if _live_records is None:
                _live_records = deps.enumerate_fns(filled_src_dir) or []
            cause = classify_crash_retrigger(crash_evidence, _live_records, names)
        else:
            cause = classify_layout_retrigger(
                starvation_cause=starv_cause, fills_exhausted=True,
                error_text=error_text, private_type_names=names)
        if not should_retrigger(cause):
            continue

        key = _component_key(names)
        if attempts.get(key, 0) >= deps.max_attempts:
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: cap "
                     f"({deps.max_attempts}) reached — not retriggering again.")
            continue

        attempts[key] = attempts.get(key, 0) + 1
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        deps.git_commit(
            project_dir,
            f"pre-layout-retrigger checkpoint — {cfg_cluster}/{sorted(names)} "
            f"attempt {attempts[key]}")

        surface = deps.load_surface(project_dir, cfg_cluster)
        narrowed = PrivateSurface(
            cfg_cluster=cfg_cluster,
            replaceable=tuple(i for i in surface.replaceable if i.name in names),
            frozen_public=surface.frozen_public,
            public_referenced=surface.public_referenced,
            public_direct_refs=surface.public_direct_refs)
        suite = deps.load_suite(project_dir, cfg_cluster)
        select_pins = make_pin_selector(suite, surface)  # full surface: correct reachability
        res = author_layout(
            narrowed, module_name=module_name,
            public_surface=deps.public_surface_text(project_dir, cfg_cluster),
            select_pins=select_pins,
            symbol_inventory=deps.symbol_inventory_text(project_dir, cfg_cluster),
            deps=deps.author_deps_for(project_dir, cfg_cluster),
            extra_guidance=(CRASH_GUIDANCE
                            if cause is LayoutRetriggerCause.RUNTIME_CRASH
                            else ""))

        if not res.all_authored:
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-author "
                     f"attempt {attempts[key]} still did not compile.")
            return RetriggerResult(fired=True, succeeded=False, cause=cause.name,
                                   reason="re-authoring failed again")

        fn_records = deps.enumerate_fns(filled_src_dir) or []
        referencing = find_fns_referencing(fn_records, names)
        reverted = deps.revert_fns(referencing, filled_src_dir)
        deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-authored "
                 f"successfully (attempt {attempts[key]}, cause={cause.name}); "
                 f"reverted {reverted} referencing fn(s) to re-fill.")
        return RetriggerResult(fired=True, succeeded=True, reverted_count=reverted,
                               cause=cause.name)

    return RetriggerResult(fired=False, reason="no frozen component's errors matched")
