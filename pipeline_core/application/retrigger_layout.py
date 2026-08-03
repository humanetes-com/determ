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
    unlocatable_private_fns,
)
from pipeline_core.domain.behaviour import BehaviourSuite
from pipeline_core.domain.layout_retrigger import (
    DEFAULT_MAX_MECHANICAL,
    DEFAULT_MAX_ZERO_PAYOFF,
    PAYOFF_HIGH_KEY,
    CompileEvidence,
    CrashEvidence,
    LayoutRetriggerCause,
    classify_compile_retrigger,
    classify_crash_retrigger,
    classify_layout_retrigger,
    crash_evidence_confounded,
    filled_body_text,
    find_fns_referencing,
    inflight_key,
    mark_retrigger_pending,
    mechanical_budget_spent,
    mechanical_key,
    payoff_budget_spent,
    reconcile_interrupted_attempts,
    settle_retrigger_payoff,
    should_retrigger,
)
from pipeline_core.domain.models import FnLocator, FnRecord, TypeRecord
from pipeline_core.domain.private_surface import (
    PrivateSurface,
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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    # Crate-wide layout registry upsert. A re-author is a deliberate redesign of
    # THIS CRATE's layout, so the new definitions must become the ones later
    # cfg-clusters adopt — otherwise a cluster authored after the re-trigger
    # adopts the superseded layout and its seeded bodies do not compile.
    # Optional: omitted ⇒ the registry keeps the pre-trigger definitions.
    upsert_layout_registry: Callable[
        [Path, tuple[tuple[str, AuthoredItem], ...]], object] | None = None


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
    if not candidates:
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
    starv_cause, _evidence = classify_starvation("", "", error_text)
    _live_records: list[FnRecord] | None = None

    for rec in candidates:
        names = rec.names
        if compile_evidence is not None:
            cause = classify_compile_retrigger(compile_evidence, names)
        elif crash_evidence is not None:
            if _live_records is None:
                _live_records = deps.enumerate_fns(filled_crate_dir) or []
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
        if mechanical_budget_spent(attempts, key):
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     f"{DEFAULT_MAX_MECHANICAL} consecutive mechanical failure(s) "
                     "already refunded here — the model attempt is not what is "
                     "failing, so another authoring call would burn on the same "
                     "bug; not retriggering until it is fixed.")
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
            if with_defs is None:
                deps.log(
                    f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                    "AUTHORED component has no stored locate-keys and they "
                    "could not be derived from the scaffold — cannot "
                    "re-author safely; skipping.")
                continue
            narrowed = with_defs

        attempts[key] = attempts.get(key, 0) + 1
        # Charged AND marked in the same write. The mark is what makes the
        # charge reversible: everything below can be cut short by a kill, and
        # without a durable "this attempt never reached an outcome" record the
        # next process reads a spent attempt it cannot distinguish from one the
        # model actually failed. See `reconcile_interrupted_attempts`.
        attempts[inflight_key(key)] = attempts[key]
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
        # …and the same property is what makes this the right place to open the
        # payoff claim: what a fire bought is measured at the NEXT decision
        # point, against the pass_count it fired at, and only a fire that
        # reached an outcome the model was answerable for may be asked. A
        # refunded attempt is excluded on the rule its refund already states —
        # no model evidence was bought, so no budget may be spent.
        if plateau_walk and not outcome.refunded:
            attempts = mark_retrigger_pending(attempts, best_pass)
        deps.save_attempts(project_dir, cfg_cluster, attempts)
        return outcome

    return RetriggerResult(fired=False, reason="no component matched the evidence")


def _settle_interrupted(
    project_dir: Path, cfg_cluster: str, deps: RetriggerLayoutDeps,
) -> dict[str, int]:
    """Load the attempts store with every interrupted attempt refunded.

    Read at the top of every sweep, so a restart's stolen attempt is repaid
    before any cap is consulted — and repaid ONCE, because the reconciliation
    persists the cleared marker."""
    attempts = deps.load_attempts(project_dir, cfg_cluster)
    settled, refunded = reconcile_interrupted_attempts(attempts)
    if not refunded:
        return attempts
    deps.save_attempts(project_dir, cfg_cluster, settled)
    for component in refunded:
        deps.log(
            f"  [layout-retrigger] {cfg_cluster}/{component.split('|')}: an "
            "attempt was charged and never reached an outcome — the process "
            "that opened it did not survive the authoring call. No model "
            "evidence was bought, so the attempt is refunded and counted as a "
            "mechanical failure.")
    return settled


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
    res = author_layout(
        narrowed, module_name=module_name,
        public_surface=deps.public_surface_text(project_dir, cfg_cluster),
        select_pins=select_pins,
        symbol_inventory=deps.symbol_inventory_text(project_dir, cfg_cluster),
        deps=replace(deps.author_deps_for(project_dir, cfg_cluster),
                     filled_bodies=lambda: filled),
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
            attempts[mechanical_key(key)] = (
                attempts.get(mechanical_key(key), 0) + 1)
            deps.save_attempts(project_dir, cfg_cluster, attempts)
            deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: "
                     "re-author died in splice/locate infrastructure — "
                     "attempt refunded (mechanical failure, no model "
                     "evidence).")
            return RetriggerResult(fired=True, succeeded=False,
                                   cause=cause.name, refunded=True,
                                   reason="mechanical splice failure "
                                          "(attempt refunded)")
        # Name the reason rather than asserting one. "Still did not compile" is
        # wrong for every rejection the accept path makes on grounds OTHER than
        # rustc — a broken public contract, or a layout that drops members the
        # filled bodies read — and a log that mis-attributes those reads as the
        # model being unable to write compiling Rust.
        why = next((c.reason for c in res.components if not c.authored), "")
        deps.log(f"  [layout-retrigger] {cfg_cluster}/{sorted(names)}: re-author "
                 f"attempt {attempts[key]} was not accepted"
                 + (f": {why}" if why else "."))
        return RetriggerResult(fired=True, succeeded=False, cause=cause.name,
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
    merged_defs = tuple(d for c in res.components for d in c.authored_defs)
    if apply_err:
        # The model DID produce a layout that compiles; only the propagation
        # failed. That is mechanical, so the attempt is refunded on the same
        # rule as a splice/locate miss — a pipeline bug must never be able to
        # exhaust the budget. The refund is counted, though: an identical
        # mechanical failure re-fires on the very next sweep, and each
        # re-fire spends another authoring call on a bug the model cannot
        # affect.
        attempts[key] = max(0, attempts[key] - 1)
        attempts[mechanical_key(key)] = (
            attempts.get(mechanical_key(key), 0) + 1)
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
    deps.save_attempts(project_dir, cfg_cluster, attempts)
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
    referencing = find_fns_referencing(fn_records or [], names)
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
