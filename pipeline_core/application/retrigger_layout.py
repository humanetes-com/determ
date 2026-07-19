"""Layout re-trigger ACTION (F11 M3) — the TDD driver's escape hatch when a
cluster's per-fn fix loop is stalled by a private layout it can't work with.

`domain/layout_retrigger.py` decides WHETHER to retrigger; this module does
it: narrow the cluster's private surface down to ONE component (frozen OR
previously authored), ask the
model for a fresh layout attempt (independent LLM sample — a real chance at a
structurally different, simpler design, since the model is free to invent its
own scheme per the reconstruction-target rule), and on success surgically
revert every already-filled fn that references the component's types so the
per-fn loop re-fills them under the new layout. Everything else in the
cluster — fills unrelated to this component — is left untouched.

Capped per (cluster, component) via a disk-persisted attempt counter (mirrors
the `RETRO_RESET_MAX_ATTEMPTS` shape) so a component that can't be
re-authored within the cap degrades to today's behaviour: exhausted, left
frozen, the TDD driver's existing stall-exit stands.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    ComponentResult,
    LayoutComponentRecord,
    LayoutOutcome,
    author_layout,
)
from pipeline_core.application.author_layout_phase import make_pin_selector
from pipeline_core.domain.authored_layout import (
    component_uids,
    derive_locate_keys,
)
from pipeline_core.domain.behaviour import BehaviourSuite
from pipeline_core.domain.layout_retrigger import (
    CrashEvidence,
    LayoutRetriggerCause,
    classify_crash_retrigger,
    classify_layout_retrigger,
    crash_evidence_confounded,
    find_fns_referencing,
    should_retrigger,
)
from pipeline_core.domain.models import FnLocator, FnRecord, TypeRecord
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
    load_layout_records: Callable[[Path, str], list[LayoutComponentRecord]]
    # Upsert one component's outcome row after a re-author — a success MUST
    # flip FROZEN→AUTHORED (and store fresh locate-keys) or the next trigger
    # re-fires on a component whose frozen defs no longer exist in the tree.
    update_layout_outcome: Callable[[Path, str, ComponentResult], object]
    # Parsed records over the CLUSTER'S SKELETON scaffold (the tree the
    # splice edits) — the backfill source for AUTHORED rows persisted before
    # locate-keys existed.
    enumerate_skeleton_fns: Callable[[Path, str], list[FnRecord] | None]
    enumerate_skeleton_types: Callable[[Path, str], list[TypeRecord] | None]
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


def _with_stored_defs(
    narrowed: PrivateSurface, defs: dict[str, str],
) -> PrivateSurface | None:
    """Swap each narrowed item's BAKED frozen definition for the stored
    locate-key of the def now living in the skeleton (an AUTHORED component's
    frozen defs are long gone — splicing against them fails every attempt).
    None when any item lacks a stored key: authoring would burn an attempt on
    a guaranteed locate failure. Keys are looked up by the same deterministic
    per-component uids the original splice used."""
    key_by_item: dict[int, str] = {}
    for comp in narrowed.components:
        for uid, it in zip(component_uids(comp), comp, strict=True):
            key = defs.get(uid)
            if key is None:
                return None
            key_by_item[id(it)] = key
    return PrivateSurface(
        cfg_cluster=narrowed.cfg_cluster,
        replaceable=tuple(
            replace(i, definition=key_by_item[id(i)])
            if id(i) in key_by_item else i
            for i in narrowed.replaceable),
        frozen_public=narrowed.frozen_public,
        public_referenced=narrowed.public_referenced,
        public_direct_refs=narrowed.public_direct_refs)


def _backfill_stored_defs(
    project_dir: Path, cfg_cluster: str, names: frozenset[str],
    narrowed: PrivateSurface, deps: RetriggerLayoutDeps,
) -> PrivateSurface | None:
    """Derive an AUTHORED component's missing locate-keys from the scaffold
    itself (AST-extracted, verbatim) and persist them, migrating a store row
    written before locate-keys existed. None when derivation is incomplete —
    the caller skips instead of burning an attempt on a locate failure."""
    fn_records = deps.enumerate_skeleton_fns(project_dir, cfg_cluster)
    type_records = deps.enumerate_skeleton_types(project_dir, cfg_cluster)
    if fn_records is None or type_records is None:
        return None
    all_keys: list[tuple[str, str]] = []
    for comp in narrowed.components:
        keys = derive_locate_keys(comp, fn_records, type_records)
        if keys is None:
            return None
        all_keys.extend(keys)
    deps.update_layout_outcome(
        project_dir, cfg_cluster,
        ComponentResult(tuple(sorted(names)), authored=True,
                        authored_defs=tuple(all_keys)))
    deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: backfilled "
             f"{len(all_keys)} locate-key(s) from the scaffold (store row "
             "predates locate-keys).")
    return _with_stored_defs(narrowed, dict(all_keys))


def retrigger_layout(
    project_dir: Path, module_name: str, cfg_cluster: str,
    filled_src_dir: Path, error_text: str,
    deps: RetriggerLayoutDeps,
    crash_evidence: CrashEvidence | None = None,
) -> RetriggerResult:
    """Try to re-author whichever non-gated component's evidence is implicated.
    Returns fired=False when no frozen component matches (the caller's
    existing stall-exit behaviour should proceed unchanged).

    Two evidence channels select a component: compile-error text naming its
    private types (causes A/B, `classify_layout_retrigger`), or — when
    `crash_evidence` is given — runtime-crash implication (cause C,
    `classify_crash_retrigger`: a vanished test's reverse-map target is or
    references a component member; no error text exists in that class).

    Both FROZEN and AUTHORED components are eligible: an authored layout can
    compile yet still be the wall (cause B's body-vs-layout exhaustion, cause
    C's runtime crash). An AUTHORED component's baked frozen defs left the
    tree when its splice landed, so its re-author locates the LIVE items via
    the store's per-uid locate-keys; a row without them (pre-locate-key
    store) is skipped loudly instead of burning an attempt on a guaranteed
    locate failure. GATED stays excluded — authoring blind is still refused."""
    records = deps.load_layout_records(project_dir, cfg_cluster)
    candidates = [r for r in records if r.outcome is not LayoutOutcome.GATED]
    if not candidates:
        return RetriggerResult(fired=False, reason="no re-authorable component recorded")

    attempts = deps.load_attempts(project_dir, cfg_cluster)
    starv_cause, _evidence = classify_starvation("", "", error_text)
    _live_records: list[FnRecord] | None = None

    for rec in candidates:
        names = rec.names
        if crash_evidence is not None:
            if _live_records is None:
                _live_records = deps.enumerate_fns(filled_src_dir) or []
            cause = classify_crash_retrigger(crash_evidence, _live_records, names)
            if (should_retrigger(cause)
                    and crash_evidence_confounded(_live_records, names)):
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    "crash evidence confounded — most of the component's own "
                    "fns are still stubs (stub panics abort binaries too); "
                    "letting fills run before spending an attempt.")
                continue
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

        surface = deps.load_surface(project_dir, cfg_cluster)
        narrowed = PrivateSurface(
            cfg_cluster=cfg_cluster,
            replaceable=tuple(i for i in surface.replaceable if i.name in names),
            frozen_public=surface.frozen_public,
            public_referenced=surface.public_referenced,
            public_direct_refs=surface.public_direct_refs)
        if rec.outcome is LayoutOutcome.AUTHORED:
            with_defs = _with_stored_defs(narrowed, dict(rec.defs))
            if with_defs is None:
                with_defs = _backfill_stored_defs(
                    project_dir, cfg_cluster, names, narrowed, deps)
            if with_defs is None:
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    "AUTHORED component has no stored locate-keys and they "
                    "could not be derived from the scaffold — cannot "
                    "re-author safely; skipping.")
                continue
            narrowed = with_defs

        attempts[key] = attempts.get(key, 0) + 1
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        deps.git_commit(
            project_dir,
            f"pre-layout-retrigger checkpoint — {cfg_cluster}/{sorted(names)} "
            f"attempt {attempts[key]}")

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
            # The cap bounds MODEL attempts. A failure whose reason is splice/
            # locate infrastructure (the model's output was never evaluated
            # against the tree) bought no evidence — refund the attempt so a
            # mechanical bug can never exhaust the budget (observed: the mega
            # component's cap spent on "could not locate a frozen definition"
            # before locate-keys were persisted).
            _mech_markers = ("could not locate a frozen definition",
                             "skeleton file unreadable",
                             "authored blocks missing uid")
            _reasons = [c.reason for c in res.components if not c.authored]
            if _reasons and all(any(m in r for m in _mech_markers)
                                for r in _reasons):
                attempts[key] = max(0, attempts[key] - 1)
                deps.save_attempts(project_dir, cfg_cluster, attempts)
                deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                         "re-author died in splice/locate infrastructure — "
                         "attempt refunded (mechanical failure, no model "
                         "evidence).")
                return RetriggerResult(fired=True, succeeded=False,
                                       cause=cause.name,
                                       reason="mechanical splice failure "
                                              "(attempt refunded)")
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-author "
                     f"attempt {attempts[key]} still did not compile.")
            return RetriggerResult(fired=True, succeeded=False, cause=cause.name,
                                   reason="re-authoring failed again")

        merged_defs = tuple(d for c in res.components for d in c.authored_defs)
        deps.update_layout_outcome(
            project_dir, cfg_cluster,
            ComponentResult(tuple(sorted(names)), authored=True,
                            authored_defs=merged_defs))
        fn_records = deps.enumerate_fns(filled_src_dir) or []
        referencing = find_fns_referencing(fn_records, names)
        reverted = deps.revert_fns(referencing, filled_src_dir)
        deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-authored "
                 f"successfully (attempt {attempts[key]}, cause={cause.name}); "
                 f"reverted {reverted} referencing fn(s) to re-fill.")
        return RetriggerResult(fired=True, succeeded=True, reverted_count=reverted,
                               cause=cause.name)

    return RetriggerResult(fired=False, reason="no re-authorable component's evidence matched")
