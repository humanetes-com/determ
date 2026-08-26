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

That last sentence held for ONE component and not for the cluster: the loop
below `continue`s past a capped component to the next candidate, and with 16
non-gated components recorded (`_layout_outcomes_nightly.json`: 17 rows, one
GATED) the stall-exit could be deferred 32 times. `anyhow`'s `nightly` cluster
spent 2026-08-02 in the resulting period-4 cycle. The
CLUSTER-wide payoff budget (`settle_retrigger_payoff`, `payoff_budget_spent`)
is what now makes the claim true: consecutive fires that leave the pass_count
where they found it stop the walk and hand the cluster back to its plateau
exit. It bounds THAT walk only — the plateau route, whose evidence is the same
error text every sweep. A build failure and a crashed binary are events, they
carry their own streaks, and a cluster that cannot build cannot ever move the
pass_count this budget clears itself on.
"""
from __future__ import annotations

import os
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
from pipeline_core.application.reconcile_layout import live_definition
from pipeline_core.domain.authored_layout import (
    AuthoredItem,
    component_uids,
    definition_replacements,
    derive_locate_keys,
    unlocatable_items,
    unlocatable_private_fns,
)
from pipeline_core.domain.behaviour import BehaviourSuite
from pipeline_core.domain.layout_retrigger import (
    DEFAULT_MAX_CRASH_ROUNDS,
    DEFAULT_MAX_MECHANICAL,
    DEFAULT_MAX_REJECTIONS,
    DEFAULT_MAX_ZERO_PAYOFF,
    INFLIGHT_KEY_PREFIX,
    PAYOFF_HIGH_KEY,
    BlameGrade,
    CompileEvidence,
    CrashEvidence,
    LayoutRetriggerCause,
    charge_crash_round,
    charge_mechanical_failure,
    charge_rejection,
    classify_compile_retrigger,
    classify_layout_retrigger,
    code_build_stamp,
    crash_blame_signature,
    crash_candidate_grade,
    crash_component_refutation,
    crash_evidence_confounded,
    crash_payoff_budget_spent,
    crash_round_budget_spent,
    crash_rounds_spent,
    crash_signature,
    filled_body_text,
    find_fns_referencing,
    inflight_key,
    inflight_pid_key,
    is_mechanical_reason,
    mark_retrigger_pending,
    mechanical_budget_spent,
    mechanical_build_key,
    mechanical_key,
    mechanical_streak_is_stale,
    payoff_budget_spent,
    reconcile_interrupted_attempts,
    record_crash_fire,
    record_crash_scope,
    rejected_key,
    rejection_budget_spent,
    rejection_build_key,
    rejection_streak_is_stale,
    settle_crash_payoff,
    settle_retrigger_payoff,
    should_retrigger,
    top_blame_tier,
)
from pipeline_core.domain.models import FnLocator, FnRecord, TypeRecord
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.scaffold_rewrite import DefReplacement
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
    # The attempt was charged and then GIVEN BACK: it died in splice/locate
    # infrastructure, so the model's output was never evaluated against the
    # tree. Typed rather than re-derived from `reason`, because every budget in
    # this module has to answer the same question — "did this attempt buy
    # model evidence?" — and a substring of a log sentence is not an answer.
    refunded: bool = False
    # The model's output was evaluated and REFUSED — validation or rustc.
    # Distinct from `refunded` (evidence WAS bought) and from a landed
    # re-author (nothing was destroyed): a rejection moves to the rejection
    # budget and must not open a payoff claim, since a fire that changed
    # nothing can only ever settle against the zero-payoff cap.
    rejected: bool = False


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
    # Enumerate fns over the LIVE (already-filled) tree, rooted at the dir
    # passed in — used to find which fns reference a just-reauthored
    # component's types (crate root, matching the reverts), and to derive that
    # tree's own fn locate keys for the propagation splice (src dir, matching
    # the private surface's paths). Record `file`s are relative to that dir, so
    # the two callers must not share one enumeration.
    enumerate_fns: Callable[[Path], list[FnRecord] | None]
    # Read one file of the live tree (src dir, rel path) — the propagation
    # splice slices type declarations from the source rather than trusting an
    # extractor's re-rendered text.
    read_tree_file: Callable[[Path, str], str | None]
    # Revert each located fn's body to `unimplemented!()` in the live tree;
    # returns how many were actually changed (already-stub fns don't count).
    revert_fns: Callable[[list[FnLocator], Path], int]
    # Apply the re-authored definitions to the LIVE tree, returning
    # (files_written, reason). NOT optional: `author_layout` splices into the
    # SKELETON, but the tree cargo compiles is seeded from it once and owned
    # thereafter, so a type definition changed only in the skeleton never
    # reaches the build. Without this the whole re-author is invisible — the
    # referencing fns below get reverted and re-fill against the very layout
    # the re-author replaced.
    apply_layout_to_tree: Callable[
        [dict[str, list[DefReplacement]], Path], tuple[int, str]]
    git_commit: Callable[[Path, str], bool]
    load_attempts: Callable[[Path, str], dict[str, int]]
    save_attempts: Callable[[Path, str, dict[str, int]], Path]
    # The rejection checklist, persisted BESIDE the attempts store: every
    # constraint a rejected re-author violated, by component key. The next
    # fire's authoring prompt opens with all of them — without this, fire 2
    # re-learned fire 1's constraints from scratch (live: renamed struct ->
    # pub field -> dropped member, then the same ring again). Cleared by an
    # accepted re-author. Optional so bare harnesses keep working; None
    # disables only the CROSS-fire carry, never the within-fire checklist.
    load_rejection_notes: Callable[[Path, str], dict[str, list[str]]] | None = None
    save_rejection_notes: Callable[
        [Path, str, dict[str, list[str]]], object] | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    # Crate-wide layout registry upsert. A re-author is a deliberate redesign of
    # THIS CRATE's layout, so the new definitions must become the ones later
    # cfg-clusters adopt — otherwise a cluster authored after the re-trigger
    # adopts the superseded layout and its seeded bodies do not compile.
    # Optional: omitted ⇒ the registry keeps the pre-trigger definitions.
    upsert_layout_registry: Callable[
        [Path, tuple[tuple[str, AuthoredItem], ...]], object] | None = None
    # The pipeline build asking. A mechanical-failure streak is a claim about
    # code ("the authoring call does not reach an outcome here"), and the only
    # path that lowers `mech:` is the accepted re-author the cap itself refuses
    # to reach — so without a build stamp the streak is permanent for every
    # future run. Optional: None keeps the streak unstamped, which is the
    # pre-stamp behaviour.
    code_id: str | None = None
    # The pids whose runs ended on a DELIBERATE external stop, from the death
    # record. Read once per sweep, only to settle interrupted attempts: an
    # operator restart during an authoring call is not evidence about the
    # authoring call. Optional: None ⇒ every interrupted attempt is charged,
    # the behaviour before the death record was consulted.
    deliberate_stop_pids: Callable[[], frozenset[int]] | None = None


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


def _unlocatable(
    surface: PrivateSurface, project_dir: Path, cfg_cluster: str,
    deps: RetriggerLayoutDeps,
) -> tuple[str, ...]:
    """Item names whose locate-key is not in the SKELETON file the splice will
    read. Asked before the model call, against the same reader `author_layout`
    splices through, so the answer describes the splice that is about to
    happen rather than a guess about it."""
    read = deps.author_deps_for(project_dir, cfg_cluster).read_skeleton
    return unlocatable_items(surface.replaceable, read)


def _component_touches(
    surface: PrivateSurface, item: PrivateSurfaceItem,
    names: frozenset[str],
) -> bool:
    """Whether `item`'s connected component contains any of `names`.

    The unit of authoring is the COMPONENT, not the item: its members are
    spliced in one batch and rejected in one batch, so dropping a single
    unlocatable item would leave its siblings authoring against a component the
    model was told is whole. Drop the component or keep it."""
    return any(any(i.name in names for i in comp)
               for comp in surface.components if item in comp)


def _scaffold_keys(
    project_dir: Path, cfg_cluster: str, narrowed: PrivateSurface,
    deps: RetriggerLayoutDeps,
) -> tuple[tuple[str, str], ...] | None:
    """(uid, locate-key) per item, SLICED from the skeleton for declarations
    and taken from the parsed signature for fns. None when any item cannot be
    read out of the file.

    Why the slice rather than `TypeRecord.definition`: syn re-renders a
    declaration with its own wrapping and trailing commas, so an extractor
    rendering is not the bytes on disk for any item wide enough to wrap —
    `application/reconcile_layout.live_definition` says so, and the live tree's
    own propagation plan has always taken this route. Deriving the SKELETON's
    keys from the extractor instead is what produced a repaired key for
    `ErrorVTable` that still could not be located: measured 2026-08-16 at
    iteration 1 of the relaunch, where the backfill reported 34 keys and one of
    them, the struct whose body carries `#[cfg(...)]` fields, matched nothing.
    """
    fns = deps.enumerate_skeleton_fns(project_dir, cfg_cluster)
    if fns is None:
        return None
    read = deps.author_deps_for(project_dir, cfg_cluster).read_skeleton
    keys: list[tuple[str, str]] = []
    for comp in narrowed.components:
        fn_items = tuple(i for i in comp if i.kind is SurfaceItemKind.FN_SIG)
        fn_keys = derive_locate_keys(fn_items, fns, []) if fn_items else ()
        if fn_keys is None:
            return None
        pending = list(fn_keys)
        for uid, it in zip(component_uids(comp), comp, strict=True):
            if it.kind is SurfaceItemKind.FN_SIG:
                keys.append((uid, pending.pop(0)[1]))
                continue
            text = read(it.file)
            sliced = None if text is None else live_definition(text, it)
            if sliced is None:
                return None
            keys.append((uid, sliced))
    return tuple(keys)


def _verified_defs(
    project_dir: Path, cfg_cluster: str, narrowed: PrivateSurface,
    defs: tuple[tuple[str, str], ...], deps: RetriggerLayoutDeps,
) -> tuple[tuple[str, str], ...]:
    """`defs` if every key can be found in the skeleton right now, else the
    scaffold's own rendering of the same items.

    The write side of the same defect. A locate-key is stored as the text the
    model AUTHORED, on the assumption that what was authored is what landed —
    and the two rows that broke cluster `stable` (see `unlocatable_items`) are
    what that assumption looks like when it fails. Verifying costs one read of
    a file the splice just wrote; storing a key that cannot be found makes
    every future fire of this component die attempt-invariantly."""
    probe = _with_stored_defs(narrowed, dict(defs))
    if probe is None:
        return defs
    missing = _unlocatable(probe, project_dir, cfg_cluster, deps)
    if not missing:
        return defs
    fns = deps.enumerate_skeleton_fns(project_dir, cfg_cluster)
    types = deps.enumerate_skeleton_types(project_dir, cfg_cluster)
    derived: list[tuple[str, str]] = []
    if fns is not None and types is not None:
        for comp in narrowed.components:
            keys = derive_locate_keys(comp, fns, types)
            if keys is None:
                derived = []
                break
            derived.extend(keys)
    deps.log(f"  [layout-retrigger] {cfg_cluster}: {len(missing)} authored "
             f"locate-key(s) ({', '.join(missing[:6])}) cannot be found in the "
             "skeleton they were just spliced into — "
             + ("storing the scaffold's own rendering instead, so the next "
                "fire can locate them." if derived else
                "and the scaffold cannot render them either; the stored keys "
                "are known-stale and the next fire will have to re-derive."))
    return tuple(derived) if derived else defs


def _backfill_stored_defs(
    project_dir: Path, cfg_cluster: str, names: frozenset[str],
    narrowed: PrivateSurface, deps: RetriggerLayoutDeps,
) -> PrivateSurface | None:
    """Derive an AUTHORED component's locate-keys from the scaffold itself and
    persist them — the migration for a store row written before locate-keys
    existed, and the repair for a row whose keys the file has outgrown. None
    when derivation is incomplete: the caller skips or narrows rather than
    burning an attempt on a locate failure.

    Declarations are SLICED from the skeleton (`_scaffold_keys`); the extractor
    rendering is the fallback for whatever the slicer cannot find, because a
    re-rendered declaration is not the bytes on disk and matching it locates
    nothing."""
    all_keys_t = _scaffold_keys(project_dir, cfg_cluster, narrowed, deps)
    if all_keys_t is not None:
        all_keys = list(all_keys_t)
    else:
        fn_records = deps.enumerate_skeleton_fns(project_dir, cfg_cluster)
        type_records = deps.enumerate_skeleton_types(project_dir, cfg_cluster)
        if fn_records is None or type_records is None:
            return None
        all_keys = []
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


def _live_tree_plan(
    narrowed: PrivateSurface, results: tuple[ComponentResult, ...],
    live_src_dir: Path, deps: RetriggerLayoutDeps,
) -> tuple[dict[str, list[DefReplacement]], str]:
    """`(per-file replacements, reason)` that rewrites the LIVE tree's current
    definitions into the just-authored ones.

    `live_src_dir` is the crate's `src/`, NOT the crate root: an extractor
    record's `file` is relative to the directory that was enumerated, and the
    private surface was enumerated from the skeleton's `src/` (`error.rs`),
    while everything else in this module — the cargo commands, the referencing-fn
    enumeration, the reverts — is keyed to the crate root (`src/error.rs`).
    Resolving a surface path against the crate root finds nothing at all, and
    finds it silently: every item reads as "not in the live tree".

    The locate keys come from the LIVE TREE itself, not from the skeleton
    splice's: the skeleton is edited first and separately, so reusing its keys
    would silently assume the two trees still agree — the assumption that breaks
    the moment a propagation fails once. Deriving per tree makes the step
    idempotent and self-healing instead. Type declarations are SLICED from the
    file (an extractor renders them back re-wrapped, with trailing commas the
    source never had); only fn keys, which are verbatim signatures, come from
    parsed records."""
    live_fns = deps.enumerate_fns(live_src_dir)
    if live_fns is None:
        return {}, "live tree could not be parsed (extractor unavailable)"
    keys: list[tuple[str, str]] = []
    for comp in narrowed.components:
        # Derived for the WHOLE component in one call, then consumed in order:
        # the occurrence stepping that keeps two same-named private fns off one
        # span lives inside derive_locate_keys and resets per call, so a
        # per-item call silently binds both to the first match.
        fn_items = tuple(i for i in comp if i.kind is SurfaceItemKind.FN_SIG)
        fn_keys = derive_locate_keys(fn_items, live_fns, []) if fn_items else ()
        if fn_keys is None:
            missing = unlocatable_private_fns(fn_items, live_fns)
            return {}, (f"fn {', '.join(missing) or '?'} is not in the live "
                        "tree as a private item")
        pending = list(fn_keys)
        for uid, it in zip(component_uids(comp), comp, strict=True):
            if it.kind is SurfaceItemKind.FN_SIG:
                keys.append((uid, pending.pop(0)[1]))
                continue
            text = deps.read_tree_file(live_src_dir, it.file)
            live_key = None if text is None else live_definition(text, it)
            if live_key is None:
                return {}, f"{it.name} could not be located in {it.file}"
            keys.append((uid, live_key))
    live_surface = _with_stored_defs(narrowed, dict(keys))
    if live_surface is None:
        return {}, "live-tree locate keys are incomplete"
    plan: dict[str, list[DefReplacement]] = {}
    if len(live_surface.components) != len(results):
        # Never zip past a mismatch: the splice is all-or-nothing precisely
        # because a tree holding some new definitions and some old ones
        # compiles as neither layout, and a truncated pairing builds exactly
        # that plan while reporting success.
        return {}, (f"{len(live_surface.components)} live component(s) but "
                    f"{len(results)} authored result(s)")
    for comp, cres in zip(live_surface.components, results, strict=True):
        per_file, _uids, err = definition_replacements(
            comp, dict(cres.authored_items))
        if err:
            return {}, err
        for rel, reps in per_file.items():
            plan.setdefault(rel, []).extend(reps)
    return plan, ""


def retrigger_layout(
    project_dir: Path, module_name: str, cfg_cluster: str,
    filled_crate_dir: Path, error_text: str,
    deps: RetriggerLayoutDeps,
    crash_evidence: CrashEvidence | None = None,
    compile_evidence: CompileEvidence | None = None,
    best_pass: int | None = None,
) -> RetriggerResult:
    """Try to re-author whichever non-gated component's evidence is implicated.
    Returns fired=False when no frozen component matches (the caller's
    existing stall-exit behaviour should proceed unchanged).

    Three evidence channels select a component: compile-error text from a fill
    attempt naming its private types (causes A/B, `classify_layout_retrigger`);
    `crash_evidence`, runtime-crash implication (cause C,
    `classify_crash_retrigger`: a vanished test's reverse-map target is or
    references a component member; no error text exists in that class); or
    `compile_evidence`, a cluster-wide build failure whose rustc digest names
    the component's private types while the lib itself compiled (cause A,
    `classify_compile_retrigger` — no fill attempt and no test run exists to
    classify, and the confounding a stub could introduce cannot apply: an
    `unimplemented!()` body type-checks, so no stub can break a build).

    Both FROZEN and AUTHORED components are eligible: an authored layout can
    compile yet still be the wall (cause B's body-vs-layout exhaustion, cause
    C's runtime crash). An AUTHORED component's baked frozen defs left the
    tree when its splice landed, so its re-author locates the LIVE items via
    the store's per-uid locate-keys; a row without them (pre-locate-key
    store) is skipped loudly instead of burning an attempt on a guaranteed
    locate failure. GATED stays excluded — authoring blind is still refused."""
    records = deps.load_layout_records(project_dir, cfg_cluster)
    candidates = [r for r in records if r.outcome is not LayoutOutcome.GATED]
    non_gated = len(candidates)
    if not candidates:
        # Both `fired=False` returns in this function used to be SILENT, and
        # silence at this gate is indistinguishable from a walk that was
        # never called: run `263217-d41a5bb5` (2026-08-15) printed zero
        # `[layout-retrigger]` lines in 308 minutes while its crash aim was
        # graded out of every candidate. Every other exit here logs; these
        # two now do too.
        deps.log(
            f"  [layout-retrigger] {cfg_cluster}: no re-authorable component "
            f"recorded — {len(records)} layout record(s) loaded, "
            f"{len(records) - non_gated} of them GATED. Nothing to aim at; "
            "the caller's own exit stands.")
        return RetriggerResult(fired=False, reason="no re-authorable component recorded")

    attempts = _settle_interrupted(project_dir, cfg_cluster, deps)
    # The payoff budget bounds the PLATEAU walk and nothing else. Its premise
    # is that the evidence does not change between fires, and that is a
    # property of this route: a stalled cluster hands the same exhausted-fill
    # error text to `classify_layout_retrigger` sweep after sweep. A build
    # failure (cause A) and a crashed binary (cause C) are events — each
    # already carries its own consecutive-sweep streak, and cause A exists
    # precisely to act on the FIRST broken sweep rather than wait out a
    # stall-shaped budget. Charging or refusing them here would also be a
    # one-way door: a cluster whose test binaries do not build reports no
    # verdict for any test, so `best_pass` — the only thing that clears this
    # streak — cannot move again.
    plateau_walk = compile_evidence is None and crash_evidence is None
    if plateau_walk:
        _settled = settle_retrigger_payoff(attempts, best_pass)
        if _settled != attempts:
            attempts = _settled
            deps.save_attempts(project_dir, cfg_cluster, attempts)
        if payoff_budget_spent(attempts):
            deps.log(
                f"  [layout-retrigger] {cfg_cluster}: the last "
                f"{DEFAULT_MAX_ZERO_PAYOFF} re-author(s) left the cluster's "
                f"best pass_count at {attempts.get(PAYOFF_HIGH_KEY)} — "
                "re-authoring a further component is aimed by the same "
                "unchanged evidence, so the walk stops here and the stall "
                "exit stands. Clears itself if any sweep beats that count.")
            return RetriggerResult(
                fired=False, reason="cluster payoff budget spent")
    # Cause C settles in its own currency: signature reproduction. The plateau
    # exemption above rests on "a crash is a new event with its own streak",
    # which is true of the FIRST fire and false of every fire after a landed
    # re-author the crash then survived — run `58819-ee78a1d1` (2026-08-13)
    # re-picked `Quoted` on registry order two iterations after its landed
    # re-author changed nothing, and oscillated 79/74/79/74 for ~2.1h a cycle.
    _crash_sig: str | None = None
    # The blame/marker half of the signature, without the vanished set. It is
    # what a refutation's SCOPE is filed under, so that a crash the loop has
    # partially REPAIRED still matches the refusal it earned — see
    # `crash_component_refutation`.
    _crash_blame_sig: str | None = None
    _crash_vanished: frozenset[str] = frozenset()
    if crash_evidence is not None:
        _crash_sig = crash_signature(crash_evidence)
        _crash_blame_sig = crash_blame_signature(crash_evidence)
        _crash_vanished = frozenset(crash_evidence.vanished)
        _settled = settle_crash_payoff(attempts, _crash_sig)
        if _settled != attempts:
            attempts = _settled
            deps.save_attempts(project_dir, cfg_cluster, attempts)
        if crash_payoff_budget_spent(attempts):
            deps.log(
                f"  [layout-retrigger] {cfg_cluster}: the last "
                f"{DEFAULT_MAX_ZERO_PAYOFF} landed cause-C re-author(s) each "
                "left this exact crash reproducing — a further component is "
                "aimed by evidence that has already failed, so the walk stops "
                "here and the stall exit stands. A changed crash or a new "
                "localizer blame changes the signature and clears it.")
            return RetriggerResult(
                fired=False, reason="cause-C signature payoff budget spent")
    starv_cause, _evidence = classify_starvation("", "", error_text)
    _live_records: list[FnRecord] | None = None

    # Cause C aims at ONE component and the registry order it is walked in is
    # not evidence. Grade every candidate against the observed fault FIRST, keep
    # only the strongest tier, and let the walk below run inside it: a component
    # that merely spells a name the faulting fn also spells can then never be
    # reached while a component that DECLARES the faulting fn exists, whatever
    # their registry indices are.
    _blame_tier = BlameGrade.NONE
    if crash_evidence is not None:
        _live_records = deps.enumerate_fns(filled_crate_dir) or []
        _graded = [(rec, crash_candidate_grade(
            crash_evidence, _live_records, rec.names)) for rec in candidates]
        _blame_tier = top_blame_tier(g for _, g in _graded)
        candidates = [rec for rec, g in _graded if g is _blame_tier
                      and g is not BlameGrade.NONE]
        if _blame_tier is BlameGrade.OWNS:
            deps.log(
                f"  [layout-retrigger] {cfg_cluster}: the observed fault is "
                f"inside code {len(candidates)} component(s) declare — "
                f"{'; '.join(sorted('|'.join(sorted(r.names)) for r in candidates))[:200]}"
                " — so no component that merely references the faulting fn is "
                "a candidate this sweep.")

    # Candidates the evidence DID implicate and a budget (or the confound
    # check) then refused. Counted apart from `candidates` because the two
    # answers this function can end on are different facts: "the evidence
    # named nobody" and "it named someone who may not be re-authored again"
    # — the second is the OWNS tier's exit reason at every tier, and reporting
    # it as the first is the same misreading the declined line was added for.
    _refused = 0
    for rec in candidates:
        names = rec.names
        if compile_evidence is not None:
            cause = classify_compile_retrigger(compile_evidence, names)
        elif crash_evidence is not None:
            # Graded above; every survivor is at the top tier by construction.
            assert _live_records is not None
            cause = LayoutRetriggerCause.RUNTIME_CRASH
            if crash_evidence_confounded(_live_records, names):
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    "crash evidence confounded — most of the component's own "
                    "fns are still stubs (stub panics abort binaries too); "
                    "letting fills run before spending an attempt.")
                _refused += 1
                continue
        else:
            cause = classify_layout_retrigger(
                starvation_cause=starv_cause, fills_exhausted=True,
                error_text=error_text, private_type_names=names)
        if not should_retrigger(cause):
            continue

        key = _component_key(names)
        if _crash_sig is not None:
            _refutation = crash_component_refutation(
                attempts, _crash_sig, key, blame_sig=_crash_blame_sig,
                vanished=_crash_vanished)
            if _refutation.refuted and _refutation.needs_scope \
                    and _crash_blame_sig and _crash_vanished:
                # BACKFILL. This refusal is being made against the crash in
                # front of us, so that crash IS a valid scope for it — and
                # writing it now is what makes the NEXT shrink match instead of
                # re-opening the aim. Stores written before the scope existed
                # hold only the hash-exact row; without this they would stay on
                # the old behaviour until the next landed fire, which is the one
                # event this is trying to prevent.
                attempts = record_crash_scope(
                    attempts, _crash_blame_sig, key, _crash_vanished)
                deps.save_attempts(project_dir, cfg_cluster, attempts)
            if _refutation.refuted:
                if _refutation.fixed:
                    # THE SILENT VERSION OF THIS LINE COST A NIGHT. Run
                    # `1920662-40f711b2` refused this component at iteration 1
                    # and fired at iterations 2 and 4 on a vanished set that had
                    # only SHRUNK, because a shrunken set hashes differently and
                    # the lookup was hash-exact. The refusal has to be able to
                    # say "same crash, minus what we fixed" out loud, or the
                    # next reader reconstructs it from a log diff again.
                    deps.log(
                        f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                        "refuted for a SUPERSET signature — this component's "
                        "re-author was already spent on a crash whose vanished "
                        f"set was {sorted(_refutation.scope)}, and "
                        f"{len(_refutation.fixed)} of those "
                        f"{len(_refutation.scope)} test(s) have since been "
                        f"fixed ({sorted(_refutation.fixed)}). That is the same "
                        "crash minus the repair, not a new one, so the aim does "
                        "NOT re-open. A vanished test outside that set, or a "
                        "new localizer blame, still does.")
                else:
                    deps.log(
                        f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                        "this component's re-author was already spent on this "
                        "exact crash and the crash survived it — refuted for "
                        "this signature, skipping. A changed crash or a new "
                        "localizer blame re-opens the aim.")
                _refused += 1
                continue
            if _crash_blame_sig is not None and crash_round_budget_spent(
                    attempts, _crash_blame_sig, key):
                # A REJECTED ROUND IS NOT FREE. The refutation above counts
                # landed fires only, correctly — a rejected scaffold destroyed
                # nothing and proves nothing about the crash. But nothing
                # recorded that the signature had already been taken to the
                # model and come back empty, so on run `1920662-40f711b2` one
                # signature bought a 55-minute, ~14-call authoring round at
                # iteration 2 (three attempts, all rejected at the compile gate)
                # and then re-fired at iteration 4 on the identical vanished set
                # — that one landed, reverted 54 bodies and cost 58 tests.
                #
                # KEYED ON THE BLAME SIGNATURE, not the vanished-inclusive one.
                # The refutation two blocks up spends this file arguing that a
                # landed fill SHRINKS the vanished set, which changes
                # `crash_signature`, which must not re-open a settled question —
                # and a budget keyed on the full signature has exactly that
                # hole: fix one of N vanished tests and the `sigr:` row is not
                # found, so the same blame buys a fresh cap of rounds, up to
                # once per distinct subset. The run above happened to repeat an
                # identical vanished set, so the hash-exact key covered the
                # instance and not the class. What honestly re-opens the budget
                # is what the log promises: a new localizer blame (`blame_sig`
                # moves) — not the loop repairing part of the crash. Rows
                # written under the old key are unreachable after this change;
                # that is one fresh budget per component at upgrade, bounded by
                # the destruction cap as ever.
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    f"{crash_rounds_spent(attempts, _crash_blame_sig, key)} "
                    f"authoring round(s) of a cap of {DEFAULT_MAX_CRASH_ROUNDS} "
                    "have already been spent on this crash BLAME by this "
                    "component, landed or rejected. A rejected round destroys "
                    "nothing and is therefore not charged to the destruction "
                    "cap — but it is not free either, and this blame has "
                    "had its GPU. A new localizer blame opens a fresh budget; "
                    "a vanished set the loop shrank by working does not.")
                _refused += 1
                continue
        if attempts.get(key, 0) >= deps.max_attempts:
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: cap "
                     f"({deps.max_attempts}) reached — not retriggering again.")
            _refused += 1
            continue
        _mech_stamp = code_build_stamp(deps.code_id)
        if attempts.get(mechanical_key(key), 0) >= DEFAULT_MAX_MECHANICAL \
                and mechanical_streak_is_stale(attempts, key, _mech_stamp):
            # A release is a decision too. Silent, this reads in the log as a
            # component that was never at the cap — and the cap being reached
            # by two operator SIGTERMs is exactly the history a reader needs
            # (`anyhow` 2026-08-15, runs `462635-c8369188`/`496645-9b9de7e1`).
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     f"{attempts.get(mechanical_key(key), 0)} mechanical "
                     "failure(s) on file were earned by a different build of "
                     "the pipeline — that streak says nothing about the code "
                     "asking now, so it is NOT refusing this aim.")
        if mechanical_budget_spent(attempts, key, code_stamp=_mech_stamp):
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     f"{DEFAULT_MAX_MECHANICAL} consecutive mechanical failure(s) "
                     "already refunded here — the model attempt is not what is "
                     "failing, so another authoring call would burn on the same "
                     "bug; not retriggering until it is fixed.")
            _refused += 1
            continue
        if attempts.get(rejected_key(key), 0) >= DEFAULT_MAX_REJECTIONS \
                and rejection_streak_is_stale(attempts, key, _mech_stamp):
            # Released, and said out loud for `mechanical_streak_is_stale`'s
            # reason: silent, this reads as a component that was never at the
            # cap, and the history that it WAS is what a reader needs.
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     f"{attempts.get(rejected_key(key), 0)} rejection(s) on "
                     "file were earned under a different authoring contract or "
                     "a different build of the pipeline — only an ACCEPTED "
                     "re-author can clear that counter and this refusal sits "
                     "upstream of the only path that accepts one, so the "
                     "streak is NOT refusing this aim.")
        if rejection_budget_spent(attempts, key, code_stamp=_mech_stamp):
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     f"{DEFAULT_MAX_REJECTIONS} consecutive re-author(s) "
                     "REJECTED at validation — RED, this refusal is "
                     "attempt-invariant: the model keeps violating the frozen "
                     "contract and only an ACCEPTED re-author clears the "
                     "streak. The rejection budget refused (the destruction "
                     "cap counts landed re-authors only). Not retriggering.")
            _refused += 1
            continue

        surface = deps.load_surface(project_dir, cfg_cluster)
        narrowed = PrivateSurface(
            cfg_cluster=cfg_cluster,
            replaceable=tuple(i for i in surface.replaceable if i.name in names),
            frozen_public=surface.frozen_public,
            public_referenced=surface.public_referenced,
            public_direct_refs=surface.public_direct_refs,
            author_private_fields=surface.author_private_fields)
        if rec.outcome is LayoutOutcome.AUTHORED:
            with_defs = _with_stored_defs(narrowed, dict(rec.defs))
            if with_defs is None:
                with_defs = _backfill_stored_defs(
                    project_dir, cfg_cluster, names, narrowed, deps)
            else:
                # A key that is PRESENT and WRONG, which the backfill above
                # cannot see: it fires only on a MISSING key, and these rows
                # have one. The splice would reject with `could not locate a
                # frozen definition`, `is_mechanical_reason` would correctly
                # call that attempt-invariant, and the whole fire would die —
                # AFTER the model had authored every other component in it.
                # Re-derive from the scaffold, which is the file the splice is
                # about to read, and let the verification below judge it.
                _stale = _unlocatable(with_defs, project_dir, cfg_cluster, deps)
                if _stale:
                    deps.log(
                        f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                        f"{len(_stale)} stored locate-key(s) are NOT in the "
                        "skeleton they would be spliced into "
                        f"({', '.join(_stale[:6])}) — the stored text is what "
                        "the model AUTHORED and that file has been rewritten "
                        "since. Re-deriving them from the scaffold before "
                        "spending an authoring call.")
                    repaired = _backfill_stored_defs(
                        project_dir, cfg_cluster, names, narrowed, deps)
                    with_defs = repaired if repaired is not None else with_defs
            if with_defs is None:
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    "AUTHORED component has no stored locate-keys and they "
                    "could not be derived from the scaffold — cannot "
                    "re-author safely; skipping.")
                _refused += 1
                continue
            # WHATEVER SURVIVES THE REPAIR DECIDES THE SCOPE, not the fire. A
            # component whose key still cannot be found is dropped from this
            # aim and left on its current definition; the rest are authored.
            # The measured alternative is the whole point of this block: on
            # 2026-08-16 `('CastTo',)` and `('Mut','error_mut')` — neither
            # named by the crash — discarded a 31-name `Error` component that
            # had already authored a layout the skeleton COMPILED, twice in one
            # run, at 50m41s/14 calls and 20m57s/7 calls.
            still = frozenset(_unlocatable(with_defs, project_dir,
                                           cfg_cluster, deps))
            if still:
                kept = tuple(i for i in with_defs.replaceable
                             if not _component_touches(with_defs, i, still))
                # THE DROP MUST NEVER REMOVE THE COMPONENT THE FAULT NAMED.
                # A landed re-author REVERTS every body that references the
                # component, so a fire that drops the aimed component and
                # authors the remainder destroys filled work in exchange for a
                # layout change to something the evidence never implicated.
                # Measured 2026-08-16, first relaunch after this entry shipped:
                # `ErrorVTable` could not be located, its component is the
                # 31-name `Error` one, and the drop kept 3 of 34 items —
                # `('CastTo',)` and `('Mut','error_mut')`, neither named by the
                # crash — then reverted 79 bodies and took the cluster 79 -> 25.
                # An unlocatable AIMED component is a reason to fix the store,
                # never a reason to fire at what is left.
                _dropped_comps = [c for c in with_defs.components
                                  if any(i.name in still for i in c)]
                _implicated = [
                    c for c in _dropped_comps
                    if crash_evidence is not None and _live_records is not None
                    and crash_candidate_grade(
                        crash_evidence, _live_records,
                        frozenset(i.name for i in c)) is not BlameGrade.NONE]
                if _implicated:
                    deps.log(
                        f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                        f"{len(still)} item(s) cannot be located "
                        f"({', '.join(sorted(still)[:6])}) and their "
                        "component(s) are the ones the fault implicates — NOT "
                        "firing at the remainder. A re-author reverts every "
                        "body that references this component, and buying that "
                        "destruction with a layout change the evidence never "
                        "asked for is the worst outcome available here.")
                    _refused += 1
                    continue
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    f"{len(still)} item(s) cannot be located even after "
                    f"re-deriving ({', '.join(sorted(still)[:6])}) — DROPPING "
                    "their component(s) from this aim and authoring the rest "
                    f"({len(kept)} of {len(with_defs.replaceable)} item(s)). "
                    "A component the fault never named must not be able to "
                    "discard one it did.")
                if not kept:
                    deps.log(
                        f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                        "nothing left to author after the drop — skipping "
                        "without spending a call.")
                    _refused += 1
                    continue
                with_defs = replace(with_defs, replaceable=kept)
            narrowed = with_defs

        attempts[key] = attempts.get(key, 0) + 1
        # Charged AND marked in the same write. The mark is what makes the
        # charge reversible: everything below can be cut short by a kill, and
        # without a durable "this attempt never reached an outcome" record the
        # next process reads a spent attempt it cannot distinguish from one the
        # model actually failed. See `reconcile_interrupted_attempts`.
        attempts[inflight_key(key)] = attempts[key]
        # …and WHICH process is holding it, so the next run can ask the death
        # record how this one ended instead of guessing from "the most recent
        # death" — a guess that is wrong whenever a run dies before it ever
        # reaches the retrigger.
        attempts[inflight_pid_key(key)] = os.getpid()
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        deps.git_commit(
            project_dir,
            f"pre-layout-retrigger checkpoint — {cfg_cluster}/{sorted(names)} "
            f"attempt {attempts[key]}")

        outcome = _run_layout_attempt(
            project_dir=project_dir, module_name=module_name,
            cfg_cluster=cfg_cluster, filled_crate_dir=filled_crate_dir,
            names=names, key=key, narrowed=narrowed, surface=surface,
            cause=cause, attempts=attempts, deps=deps)
        # Reached only when the attempt produced an OUTCOME — a kill or an
        # exception leaves the marker standing, which is the whole point, so
        # this must stay the single place that clears it. Any future exit added
        # to `_run_layout_attempt` is settled here for free.
        attempts.pop(inflight_key(key), None)
        attempts.pop(inflight_pid_key(key), None)
        # …and the same property is what makes this the right place to open the
        # payoff claim: what a fire bought is measured at the NEXT decision
        # point, against the pass_count it fired at, and only a fire that
        # reached an outcome the model was answerable for may be asked. A
        # refunded attempt is excluded on the rule its refund already states —
        # no model evidence was bought, so no budget may be spent. A REJECTED
        # one is excluded too: it changed nothing in the tree, so the claim it
        # opens can only ever settle against the cluster's zero-payoff cap.
        if plateau_walk and not outcome.refunded and not outcome.rejected:
            attempts = mark_retrigger_pending(attempts, best_pass)
        # THE AUTHORING ROUND, charged here rather than at the open. The
        # destruction cap is charged before the call and refunded after,
        # because a process killed mid-authoring must not be able to retry for
        # free; this budget cannot use that shape, because the refund would
        # have to survive a kill and `reconcile_interrupted_attempts` has no way
        # to know WHICH crash signature the dead attempt was aimed at. Charging
        # on the outcome gives the same rule with no reconciliation: a REJECTED
        # round is charged (the model was asked, answered, and its answer failed
        # the compile gate — that is exactly the spend this bounds), a REFUNDED
        # one is not (no model evidence was bought), and a kill charges nothing,
        # which the per-component mechanical cap already bounds at 2.
        # Booked against the BLAME signature, matching the refusal's key: the
        # vanished-inclusive signature moves whenever the loop repairs part of
        # the crash, and a budget whose key the debtor can change by working is
        # re-bought once per distinct vanished subset.
        if _crash_blame_sig is not None and not outcome.refunded:
            attempts = charge_crash_round(attempts, _crash_blame_sig, key)
        # A LANDED cause-C fire is booked against its crash signature: the
        # component is refuted for this exact crash the moment it is seen
        # again, and the fire's payoff claim settles on signature
        # reproduction (`settle_crash_payoff`). Landed only — a rejection or
        # refund changed nothing in the tree and proves nothing about the
        # crash.
        if _crash_sig is not None and outcome.succeeded:
            attempts = record_crash_fire(
                attempts, _crash_sig, key,
                blame_sig=_crash_blame_sig, vanished=_crash_vanished)
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        return outcome

    if _blame_tier is BlameGrade.OWNS:
        # Every component the fault was observed INSIDE is unavailable — capped,
        # confounded, or already failing mechanically. That is the answer, and
        # the walk ends on it. Falling through to a weaker candidate here is the
        # defect this tier exists to remove: the sweep spends its budget on a
        # type the fault never touched and mass-reverts every body that spells
        # its name, which is progress deleted for evidence nobody has. The
        # caller's fill pass still runs, and the blamed fn is already in its
        # frontier carrying the ASAN report — a body fault gets a body retry.
        deps.log(
            f"  [layout-retrigger] {cfg_cluster}: the fault's own component(s) "
            "cannot be re-authored again this run — NOT re-authoring a "
            "component the fault merely references; the blamed fn stays in the "
            "fill frontier with its crash report instead. With no localizer "
            "blame, the crash's tests stay counted as failing — an ACCEPTED, "
            "bounded loss until an origin-path blame or a changed crash "
            "re-opens the aim.")
        return RetriggerResult(
            fired=False, reason="the faulting component is not re-authorable")
    # The DECLINED walk. Every per-candidate refusal above logs from inside
    # the loop, so an empty candidate list — the state a NONE tier produces —
    # took this exit having printed nothing at all. On run `263217-d41a5bb5`
    # the line below would have read `tier NONE, 0 of 16 candidate(s); crash
    # evidence: 2 blamed frame(s), 67 chain-blamed, 36 suspect(s), 19
    # vanished` at iteration 1, which is the whole diagnosis: blame that
    # implicates nobody, standing in front of a chain that implicates the
    # construct/vtable component.
    _route = ("compile" if compile_evidence is not None else
              "crash" if crash_evidence is not None else "plateau")
    _shape = ""
    if crash_evidence is not None:
        _shape = (f"; crash evidence: {len(crash_evidence.blamed_fns)} blamed "
                  f"frame(s), {len(crash_evidence.chain_blamed)} chain-blamed, "
                  f"{len(crash_evidence.suspect_fns)} suspect(s), "
                  f"{len(crash_evidence.vanished)} vanished")
    if _refused:
        # Below the OWNS tier the walk ends here on refusals too, and "no
        # component matched" is then false in the direction that matters: the
        # evidence named someone, a budget refused them, and each refusal
        # printed its own line above. Say which of the two happened, so the
        # next reader does not go looking for missing evidence when the answer
        # is a spent budget (or the reverse).
        deps.log(
            f"  [layout-retrigger] {cfg_cluster}: {_refused} candidate(s) the "
            f"evidence implicated were all refused above ({_route} route, "
            f"blame tier {_blame_tier.name}, {len(candidates)} of {non_gated} "
            f"non-GATED record(s) at that tier){_shape}. Nothing re-authored, "
            "no attempt charged.")
        return RetriggerResult(
            fired=False, reason="every matching component was refused")
    deps.log(
        f"  [layout-retrigger] {cfg_cluster}: no component matched the "
        f"evidence ({_route} route, blame tier {_blame_tier.name}) — "
        f"{len(candidates)} of {non_gated} non-GATED record(s) reached the "
        f"walk{_shape}. Nothing re-authored, no attempt charged.")
    return RetriggerResult(fired=False, reason="no component matched the evidence")


def _settle_interrupted(
    project_dir: Path, cfg_cluster: str, deps: RetriggerLayoutDeps,
) -> dict[str, int]:
    """Load the attempts store with every interrupted attempt refunded.

    Read at the top of every sweep, so a restart's stolen attempt is repaid
    before any cap is consulted — and repaid ONCE, because the reconciliation
    persists the cleared marker.

    Whether the refund also costs a MECHANICAL failure depends on how the
    process that opened it died, which the death record already knows: the
    charge bounds an authoring call that kills its own process, and an operator
    stop is not that. Both readings are logged by name — reconstructing which
    one a store row came from cost a session on 2026-08-15."""
    attempts = deps.load_attempts(project_dir, cfg_cluster)
    _stopped = (frozenset() if deps.deliberate_stop_pids is None
                else deps.deliberate_stop_pids())
    _openers = {k[len(INFLIGHT_KEY_PREFIX):]: attempts.get(inflight_pid_key(
        k[len(INFLIGHT_KEY_PREFIX):]))
        for k in attempts if k.startswith(INFLIGHT_KEY_PREFIX)}
    settled, refunded = reconcile_interrupted_attempts(
        attempts, stopped_pids=_stopped,
        code_stamp=code_build_stamp(deps.code_id))
    if not refunded:
        return attempts
    deps.save_attempts(project_dir, cfg_cluster, settled)
    for component in refunded:
        _pid = _openers.get(component)
        _spared = _pid is not None and _pid in _stopped
        deps.log(
            f"  [layout-retrigger] {cfg_cluster}/{component.split('|')}: an "
            "attempt was charged and never reached an outcome — the process "
            f"that opened it (pid {_pid if _pid is not None else 'unrecorded'})"
            " did not survive the authoring call. No model evidence was "
            "bought, so the attempt is refunded"
            + (" and NOT counted as a mechanical failure: the death record "
               "says that process was stopped deliberately, which is evidence "
               "about the operator and none about this component."
               if _spared else
               " and counted as a mechanical failure (no death record says "
               "that process was stopped deliberately)."))
    return settled


def _is_mechanical(reason: str) -> bool:
    """Whether a failure reason describes the splice/locate infrastructure
    rather than anything the model wrote. Readers, one definition: the attempt
    refund below, the checklist filter in `_remember_rejections` — they
    disagreed while the markers lived inside the refund branch, and the
    checklist ended up holding entries no reply can satisfy — and the authoring
    retry loop in `author_layout`, which is why the markers now live in
    `domain/layout_retrigger`."""
    return is_mechanical_reason(reason)


def _constraint_key(reason: str) -> str:
    """The identity of a CONSTRAINT, as opposed to the identity of one
    rejection's text. Digits are dropped because the reason embeds a live
    measurement: `drops member(s) ... \\`ptr\\` (25 reads)` and the same
    sentence at `(29 reads)` are one constraint restated after four more
    bodies landed, and exact-text dedup stored both (observed in
    `_layout_retrigger_rejections_stable.json`, 2026-08-14). With a bounded
    list of 12 that is how a single stubborn constraint evicts every other one
    the component has learned."""
    return "".join(c for c in reason if not c.isdigit())


def _remember_rejections(
    project_dir: Path, cfg_cluster: str, key: str,
    reasons: list[str], deps: RetriggerLayoutDeps,
) -> None:
    """Append this rejection's violated constraints to the component's
    persisted checklist (deduplicated by CONSTRAINT, newest last, bounded so a
    pathological reason string cannot grow the prompt without limit).

    Mechanical reasons are dropped rather than stored. They reach here whenever
    ONE component of a mixed batch failed mechanically while another was really
    rejected — the refund branch tests `all()`, so the batch falls through to
    the rejection path carrying both — and the prompt presents this list under
    "each line is a constraint an earlier reply violated; your reply must
    satisfy ALL of them at once". `could not locate a frozen definition to
    replace: CastTo in ptr.rs` is not satisfiable by any reply."""
    if deps.load_rejection_notes is None or deps.save_rejection_notes is None:
        return
    notes = deps.load_rejection_notes(project_dir, cfg_cluster)
    have = [r for r in (notes.get(key) or []) if not _is_mechanical(r)]
    for r in reasons:
        if not r or _is_mechanical(r):
            continue
        # Newest text wins the slot: the counts in it describe the tree as it
        # is now, and the older restatement is the same constraint measured
        # against fewer bodies.
        have = [h for h in have if _constraint_key(h) != _constraint_key(r)]
        have.append(r)
    notes[key] = have[-12:]
    deps.save_rejection_notes(project_dir, cfg_cluster, notes)


def _run_layout_attempt(
    *,
    project_dir: Path, module_name: str, cfg_cluster: str,
    filled_crate_dir: Path, names: frozenset[str], key: str,
    narrowed: PrivateSurface, surface: PrivateSurface,
    cause: LayoutRetriggerCause, attempts: dict[str, int],
    deps: RetriggerLayoutDeps,
) -> RetriggerResult:
    """One CHARGED re-authoring attempt: author, propagate, revert the fns that
    read the old shape. Mutates `attempts` in place for the refunds it decides;
    the caller owns the in-flight marker that makes returning from here mean
    'this attempt was measured'."""
    suite = deps.load_suite(project_dir, cfg_cluster)
    select_pins = make_pin_selector(suite, surface)  # full surface: correct reachability
    # The bodies the live tree holds RIGHT NOW, read BEFORE the re-author. They
    # are the only evidence that can contradict a layout the skeleton compiles,
    # and in a moment they are the very bodies this attempt reverts — so they
    # have to be captured first or the check has nothing left to read.
    #
    # A second enumeration on purpose: the one below runs after the splice has
    # moved every byte offset in this one, and its records are what locate the
    # fns to revert. Sharing one would revert against stale spans.
    filled = filled_body_text(deps.enumerate_fns(filled_crate_dir) or [])
    guidance = (CRASH_GUIDANCE
                if cause is LayoutRetriggerCause.RUNTIME_CRASH else "")
    # Constraints earlier FIRES violated open this fire's prompt. The
    # within-fire retry hint accumulates only inside one authoring call;
    # without this carry, each new fire re-learned the same frozen-contract
    # constraints from scratch (live 2026-08-09/10: renamed struct -> pub
    # field -> dropped member, then the same ring again a fire later).
    prior = (deps.load_rejection_notes(project_dir, cfg_cluster)
             if deps.load_rejection_notes is not None else {})
    checklist = prior.get(key) or []
    if checklist:
        guidance += (
            "\n\nEARLIER RE-AUTHOR ATTEMPTS OF THIS COMPONENT WERE REJECTED."
            " Each line below is a constraint an earlier reply violated;"
            " your reply must satisfy ALL of them at once:\n"
            + "\n".join(f"- {r}" for r in checklist) + "\n")
    res = author_layout(
        narrowed, module_name=module_name,
        public_surface=deps.public_surface_text(project_dir, cfg_cluster),
        select_pins=select_pins,
        symbol_inventory=deps.symbol_inventory_text(project_dir, cfg_cluster),
        deps=replace(deps.author_deps_for(project_dir, cfg_cluster),
                     filled_bodies=lambda: filled,
                     # A re-author answers to the layout it replaces: reject a
                     # replacement that drops its derives or its visibility
                     # (observed non-monotone on 2026-08-11: attempt 1 carried
                     # `#[derive(Debug, Default, Clone, PartialEq, Eq)]`, a
                     # later attempt none, another a private struct).
                     guard_replaced_layout=True),
        extra_guidance=guidance,
        behaviour_specs=suite.specs)

    if not res.all_authored:
        # The cap bounds MODEL attempts. A failure whose reason is splice/
        # locate infrastructure (the model's output was never evaluated
        # against the tree) bought no evidence — refund the attempt so a
        # mechanical bug can never exhaust the budget (observed: the mega
        # component's cap spent on "could not locate a frozen definition"
        # before locate-keys were persisted).
        _reasons = [c.reason for c in res.components if not c.authored]
        if _reasons and all(_is_mechanical(r) for r in _reasons):
            attempts[key] = max(0, attempts[key] - 1)
            charge_mechanical_failure(
                attempts, key, code_build_stamp(deps.code_id))
            deps.save_attempts(project_dir, cfg_cluster, attempts)
            # THE REFUND IS NOT A REPORT, and for two days it read like one.
            # `is_mechanical_reason` (attempts 2..3 not spent) and this refund
            # (the cap not charged) are both correct and both make the failure
            # QUIET: the fire still costs a full authoring round of the biggest
            # component in the crate, lands nothing, and says only "refunded".
            # Run `1692702-94e6163a` (2026-08-16) died this way twice —
            # 50m41s/14 model calls and 20m57s/7 — with the 31-name `Error`
            # component's layout COMPILING both times before it was discarded.
            # A streak is the sentence a reader needs, so it is printed here.
            _mech_streak = attempts.get(mechanical_key(key), 0)
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     "re-author died in splice/locate infrastructure — "
                     "attempt refunded (mechanical failure, no model "
                     "evidence)."
                     + (f" NEVER LANDED: {_mech_streak} consecutive mechanical "
                        "death(s) of this component, each one an authoring "
                        "round spent for nothing. The refund protects the "
                        "budget; it does not make the fire free."
                        if _mech_streak > 1 else "")
                     + f" Reason: {_reasons[0] if _reasons else '?'}")
            return RetriggerResult(fired=True, succeeded=False,
                                   cause=cause.name, refunded=True,
                                   reason="mechanical splice failure "
                                          "(attempt refunded)")
        # Name the reason rather than asserting one. "Still did not compile" is
        # wrong for every rejection the accept path makes on grounds OTHER than
        # rustc — a broken public contract, or a layout that drops members the
        # filled bodies read — and a log that mis-attributes those reads as the
        # model being unable to write compiling Rust.
        #
        # A rejection destroys nothing (nothing was applied, nothing reverted),
        # so it is moved off the destruction cap and onto its own consecutive
        # budget — the cap counts LANDED re-authors only. Two rejections
        # charged against the cap of 2 are how the 34-name component lost its
        # only structural escape on 2026-08-09/10 while ASAN kept blaming
        # `object_drop` every iteration.
        attempts[key] = max(0, attempts[key] - 1)
        # A streak carried over from an older constraint set is retired here,
        # at the first rejection earned under the current one, rather than
        # silently continued — otherwise this rejection reads as "the third
        # time it was told" when it is the first.
        _stale = charge_rejection(attempts, key, code_build_stamp(deps.code_id))
        if _stale:
            deps.log(
                f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                f"{_stale} earlier rejection(s) retired — they were earned "
                "under an authoring prompt or a pipeline build that is not the "
                "one asking; the budget restarts.")
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        why = next((c.reason for c in res.components if not c.authored), "")
        _remember_rejections(project_dir, cfg_cluster, key, _reasons, deps)
        deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-author "
                 f"was not accepted (rejection "
                 f"{attempts[rejected_key(key)]}/{DEFAULT_MAX_REJECTIONS}, "
                 "destruction cap not charged)"
                 + (f": {why}" if why else "."))
        return RetriggerResult(fired=True, succeeded=False, cause=cause.name,
                               rejected=True,
                               reason=why or "re-authoring failed again")

    # Land the new definitions in the tree that is actually built. Its keys
    # are derived from the LIVE TREE's own parsed records, never reused from
    # the skeleton splice: the two trees hold the same definitions only
    # until one of them is edited alone, and that is exactly the state this
    # step exists to repair. Done before the revert so the re-fills below
    # see the new shape.
    # ONE derivation of the src base, shared by the plan and the splice that
    # applies it: a private-surface item's `file` is relative to the src dir
    # it was enumerated from, while `filled_crate_dir` is the crate root that
    # cargo runs in. Deriving it twice is how the two drift apart.
    live_src_dir = filled_crate_dir / "src"
    plan, plan_err = _live_tree_plan(
        narrowed, res.components, live_src_dir, deps)
    written, apply_err = (
        (0, plan_err) if plan_err
        else deps.apply_layout_to_tree(plan, live_src_dir))
    merged_defs = _verified_defs(
        project_dir, cfg_cluster, narrowed,
        tuple(d for c in res.components for d in c.authored_defs), deps)
    if apply_err:
        # The model DID produce a layout that compiles; only the propagation
        # failed. That is mechanical, so the attempt is refunded on the same
        # rule as a splice/locate miss — a pipeline bug must never be able to
        # exhaust the budget. The refund is counted, though: an identical
        # mechanical failure re-fires on the very next sweep, and each
        # re-fire spends another authoring call on a bug the model cannot
        # affect.
        attempts[key] = max(0, attempts[key] - 1)
        charge_mechanical_failure(
            attempts, key, code_build_stamp(deps.code_id))
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        # The SKELETON now holds the authored definitions, so the store must
        # record them: it is what the seed-time layout reconciliation
        # reinstalls from (repairing this exact failure with no model call),
        # and what the next attempt locates by. Left on the old keys, both
        # fall back to re-deriving from an extractor rendering that is not
        # the bytes on disk.
        deps.update_layout_outcome(
            project_dir, cfg_cluster,
            ComponentResult(tuple(sorted(names)), authored=True,
                            authored_defs=merged_defs))
        deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: the "
                 "re-authored layout compiled in the skeleton but could NOT "
                 f"be applied to the live tree ({apply_err}) — the build "
                 "still sees the old layout; not reverting any fn, attempt "
                 "refunded (mechanical failure, no model evidence).")
        return RetriggerResult(fired=True, succeeded=False, cause=cause.name,
                               refunded=True,
                               reason=f"live-tree splice failed: {apply_err}")

    attempts.pop(mechanical_key(key), None)   # consecutive-failure counter
    attempts.pop(mechanical_build_key(key), None)   # …and the build it named
    attempts.pop(rejected_key(key), None)     # an ACCEPTED re-author clears it
    attempts.pop(rejection_build_key(key), None)    # …and the build it named
    deps.save_attempts(project_dir, cfg_cluster, attempts)
    if deps.load_rejection_notes is not None \
            and deps.save_rejection_notes is not None:
        _notes = deps.load_rejection_notes(project_dir, cfg_cluster)
        if _notes.pop(key, None) is not None:
            deps.save_rejection_notes(project_dir, cfg_cluster, _notes)
    if deps.upsert_layout_registry is not None:
        deps.upsert_layout_registry(
            project_dir,
            tuple(i for c in res.components for i in c.authored_items))
    deps.update_layout_outcome(
        project_dir, cfg_cluster,
        ComponentResult(tuple(sorted(names)), authored=True,
                        authored_defs=merged_defs))
    # `reverted` alone cannot be read: revert_fns skips fns already stubbed,
    # so 0 means "scope empty", "scope all stubs" or "nothing enumerated"
    # — three different states, one of which is a silent failure. Report the
    # enumeration and the scope beside it so a zero is attributable.
    fn_records = deps.enumerate_fns(filled_crate_dir)
    # THE SCOPE IS WHAT CHANGED, not what the fire was named after. A revert
    # exists to re-fill bodies written against a layout that just moved, so a
    # name whose definition this attempt did not touch has no business in it:
    # the fire's `names` is the whole store row, and after the locate-key drop
    # above it can be much wider than the set actually authored. On 2026-08-16
    # the two were 34 names against 3, and reverting on the wide one cost 79
    # filled bodies for a re-author of `CastTo` and `Mut`.
    _authored_names = frozenset(
        n for c in res.components if c.authored for n in c.item_names) or names
    referencing = find_fns_referencing(fn_records or [], _authored_names)
    reverted = deps.revert_fns(referencing, filled_crate_dir)
    scope = ("live tree could not be parsed (extractor unavailable) — "
             "revert scope is empty for that reason, not because no fn "
             "references the component" if fn_records is None else
             f"{len(referencing)} of {len(fn_records)} enumerated fn(s) "
             f"reference it, {reverted} had a body to revert")
    deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-authored "
             f"successfully (attempt {attempts[key]}, cause={cause.name}); "
             f"applied to {written} live file(s); "
             f"reverted {reverted} referencing fn(s) to re-fill — {scope}.")
    return RetriggerResult(fired=True, succeeded=True, reverted_count=reverted,
                           cause=cause.name)
