"""Layout-authoring use-case (F11 t1.5) — orchestrates one cfg-cluster's
private-layout design between bake and strip.

Per connected component of the private surface: prompt the model, parse the
authored layout, require it to COVER every replaceable item, splice it into the
skeleton scaffold, and accept only if the rewritten scaffold still compiles with
``unimplemented!()`` bodies (the same invariant the baked skeleton holds today).
A component that will not compile after the retry budget is left with its frozen
layout — degraded, never broken (the run proceeds exactly as before F11).

Collaborators (model, skeleton IO, compile check) are injected — this module is
pure orchestration over domain VOs.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from pipeline_core.domain.authored_layout import (
    AuthoredItem,
    authored_locate_keys,
    component_uids,
    parse_authored_layout,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.failure_classification import first_diagnostic
from pipeline_core.domain.layout_chunking import plan_author_chunks
from pipeline_core.domain.layout_probe import LayoutProbe, build_layout_probe
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.scaffold_rewrite import (
    DefReplacement,
    replace_surface_definitions,
)
from pipeline_core.prompts.layout_authoring import build_layout_authoring_prompt


@dataclass(frozen=True, slots=True)
class AuthorLayoutDeps:
    run_model: Callable[[str], str]                 # prompt → reply
    read_skeleton: Callable[[str], str | None]      # rel path → text (None if absent)
    write_skeleton: Callable[[str, str], None]      # rel path, text
    compile_check: Callable[[], tuple[bool, str]]   # (compiles?, error excerpt)
    log: Callable[[str], None]
    # Layout-pin gate: install probe source into the skeleton (None removes it).
    # Absent ⇒ pins stay advisory, exactly as before the gate existed.
    write_pin_probe: Callable[[str | None], None] | None = None
    max_attempts: int = 3
    # M6 chunking: components above `single_shot_max` items are authored in
    # chunks (types first, then fns) instead of demanding one 40-block reply.
    single_shot_max: int = 14
    fns_per_chunk: int = 10
    chunk_coverage_retries: int = 2


@dataclass(frozen=True, slots=True)
class ComponentResult:
    item_names: tuple[str, ...]
    authored: bool
    reason: str = ""
    gated: bool = False   # M13: skipped-on-purpose (harvest gap), not a failure
    # (uid, locate-key) per item when authored: the text a future re-author
    # matches against the LIVE skeleton (the baked frozen defs no longer exist
    # there once a splice landed — locating by them fails every attempt).
    authored_defs: tuple[tuple[str, str], ...] = ()


class LayoutOutcome(Enum):
    """What became of ONE connected component after a t1.5 pass — the shape
    the F11 M3 re-trigger mechanism needs to find "which private types are
    still on the ORIGINAL frozen scaffold" (FROZEN = every retry attempt
    failed to compile; the model never got a layout it could work with)."""

    AUTHORED = auto()   # a model-authored replacement compiles and was spliced
    GATED = auto()      # M13 refused to author blind (harvest gap) — left frozen on purpose
    FROZEN = auto()     # every authoring attempt failed to compile — reverted to the original


def component_outcome(c: ComponentResult) -> LayoutOutcome:
    if c.gated:
        return LayoutOutcome.GATED
    if c.authored:
        return LayoutOutcome.AUTHORED
    return LayoutOutcome.FROZEN


@dataclass(frozen=True, slots=True)
class LayoutComponentRecord:
    """One persisted per-component layout row. `defs` carries the (uid,
    locate-key) pairs of an AUTHORED component — the text that finds each
    authored item in the LIVE skeleton, which a re-author must use instead of
    the baked frozen definitions (those left the tree when the splice landed)."""

    names: frozenset[str]
    outcome: LayoutOutcome
    defs: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorLayoutResult:
    cfg_cluster: str
    components: tuple[ComponentResult, ...] = field(default_factory=tuple)

    @property
    def all_authored(self) -> bool:
        return all(c.authored for c in self.components)

    @property
    def gated(self) -> tuple[ComponentResult, ...]:
        return tuple(c for c in self.components if c.gated)


def _splice_component(
    items: tuple[PrivateSurfaceItem, ...], authored: dict[str, AuthoredItem],
    deps: AuthorLayoutDeps,
) -> tuple[dict[str, str], str]:
    """Apply the authored layout (keyed by protocol uid) to the skeleton files
    in memory. Returns (edited files by rel path, failure reason — "" on
    success). Replacements are batched per file and occurrence-ordered
    (`replace_definitions`) so same-named items land in their OWN slots; any
    unlocatable frozen def rejects the splice — a dangling frozen def next to
    an authored one would not compile. The reason NAMES the failing uid/file:
    a missing block, an unreadable file and a locate miss are three different
    bugs and collapsing them into one message hides which one is happening."""
    per_file: dict[str, list[DefReplacement]] = {}
    per_file_uids: dict[str, list[str]] = {}
    for uid, it in zip(component_uids(items), items, strict=True):
        block = authored.get(uid)
        if block is None:
            return {}, f"authored blocks missing uid {uid}"
        # Fn definitions are enumerated as bare signatures (no vis/`unsafe`
        # prefix, no stub body) — name the fn so the splice swaps the whole
        # item instead of nesting the new def inside the old one.
        fn_name = it.name if it.kind is SurfaceItemKind.FN_SIG else ""
        per_file.setdefault(it.file, []).append(
            DefReplacement(it.definition, block.definition, fn_name))
        per_file_uids.setdefault(it.file, []).append(uid)
    edited: dict[str, str] = {}
    for rel, pairs in per_file.items():
        current = deps.read_skeleton(rel)
        if current is None:
            return edited, f"skeleton file unreadable: {rel}"
        res = replace_surface_definitions(current, pairs)
        if not res.replaced:
            if res.miss_index is not None:
                return edited, ("could not locate a frozen definition to "
                                f"replace: {per_file_uids[rel][res.miss_index]}"
                                f" in {rel}")
            return edited, ("could not locate a frozen definition to replace:"
                            f" overlapping/ambiguous target spans in {rel}")
        edited[rel] = res.text
    return edited, ""


def author_layout(
    surface: PrivateSurface,
    *,
    module_name: str,
    public_surface: str,
    select_pins: Callable[[tuple[PrivateSurfaceItem, ...]], list[BehaviourSpec]],
    symbol_inventory: str,
    deps: AuthorLayoutDeps,
    extra_guidance: str = "",
) -> AuthorLayoutResult:
    """Author every connected component of `surface`. Each component is
    committed to the skeleton only after the rewritten scaffold compiles; a
    failed component is rolled back to its frozen state.

    M13 gate: a component that is LAYOUT-SENSITIVE (its private types feed a
    public type's size/niche) but reaches here with NO layout pins is a harvest
    gap, not a free layout — authoring it blind produces a wrong-sized public
    type that compiles yet fails the repr tests a whole sweep later. Such a
    component is left FROZEN (gated), loud in the log, never authored blind."""
    results: list[ComponentResult] = []
    for comp in surface.components:
        names = tuple(i.name for i in comp)
        pins = select_pins(comp)
        if not pins and surface.component_is_layout_sensitive(comp):
            reason = ("layout-sensitive (feeds a public type's size) but NO "
                      "layout pins harvested — refusing to author blind")
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: GATED — {reason}")
            results.append(ComponentResult(names, False, reason, gated=True))
            continue
        anchors = _public_anchors(surface, comp)
        def _prompt(chunk_items: tuple[PrivateSurfaceItem, ...],
                    chunk_uids: tuple[str, ...], scheme: str, hint: str,
                    *, _pins: list[BehaviourSpec] = pins,
                    _anchors: dict[str, str] = anchors) -> str:
            return build_layout_authoring_prompt(
                module_name=module_name, cfg_cluster=surface.cfg_cluster,
                items=chunk_items, public_surface=public_surface,
                layout_pins=_pins, symbol_inventory=symbol_inventory,
                extra_guidance=extra_guidance, authored_scheme=scheme,
                item_uids=chunk_uids, item_anchors=_anchors) + hint
        committed = False
        reason = "no attempt"
        committed_defs: tuple[tuple[str, str], ...] = ()
        # snapshot the pristine skeleton files this component touches, so a
        # failed candidate is reverted exactly (no cross-attempt bleed).
        touched = {i.file for i in comp}
        pristine = {f: deps.read_skeleton(f) for f in touched}
        probe = _calibrated_pin_probe(surface, comp, pins, deps)
        for attempt in range(1, deps.max_attempts + 1):
            hint = _retry_hint(reason) if attempt > 1 else ""
            authored, reason = _author_component(
                comp, surface.cfg_cluster, _prompt, hint, deps)
            if authored is None:
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            edited, splice_err = _splice_component(comp, authored, deps)
            if splice_err:
                reason = splice_err
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            for rel, txt in edited.items():
                deps.write_skeleton(rel, txt)
            # The pin probe rides along with THIS compile: a layout that no
            # longer satisfies `size_of::<Error>() == size_of::<usize>()` must
            # be rejected here, not discovered a whole sweep later as repr
            # failures blamed on individual fn bodies.
            if probe.checks_anything and deps.write_pin_probe is not None:
                deps.write_pin_probe(probe.source)
            compiled, errors = deps.compile_check()
            if deps.write_pin_probe is not None:
                deps.write_pin_probe(None)
            if compiled:
                committed = True
                reason = ""
                committed_defs = authored_locate_keys(comp, authored)
                pin_note = (f" [{len(probe.gated)} layout pin(s) verified]"
                            if probe.checks_anything else "")
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                         f"layout compiles (attempt {attempt}).{pin_note}")
                break
            reason = ("authored scaffold did not compile:\n"
                      + errors if errors else "authored scaffold did not compile")
            for rel, orig in pristine.items():
                if orig is not None:
                    deps.write_skeleton(rel, orig)
            # Name the first diagnostic: an environmental failure that no
            # authored layout can fix (E0554 = nightly `#![feature]` built on
            # stable) is otherwise indistinguishable in the log from a model
            # that wrote bad code, and reads as N hard components instead of
            # one broken gate.
            diag = first_diagnostic(errors)
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                     f"scaffold did not compile (attempt {attempt}) — reverted"
                     + (" (layout pins were gated this attempt)"
                        if probe.checks_anything else "")
                     + (f": {diag}" if diag else "."))
        results.append(ComponentResult(names, committed, reason,
                                       authored_defs=committed_defs))
    return AuthorLayoutResult(surface.cfg_cluster, tuple(results))


def _public_anchors(
    surface: PrivateSurface, comp: tuple[PrivateSurfaceItem, ...],
) -> dict[str, str]:
    """uid → the frozen PUBLIC type whose layout reaches that item, for the
    items the prompt lists. This is the ONLY disambiguation the item listing
    carries now that the frozen definition is withheld, and it leaks nothing:
    the public type's own definition is already in the prompt's frozen-surface
    section. A name reachable from several public types gets the first by sort
    order — a stable label, not a claim of exclusivity."""
    reach = {name: surface.reachable_from_public(name)
             for name in sorted(surface.public_direct_refs)}
    out: dict[str, str] = {}
    for uid, it in zip(component_uids(comp), comp, strict=True):
        anchor = next((p for p, names in reach.items() if it.name in names), "")
        if anchor:
            out[uid] = anchor
    return out


def _calibrated_pin_probe(
    surface: PrivateSurface,
    comp: tuple[PrivateSurfaceItem, ...],
    pins: list[BehaviourSpec],
    deps: AuthorLayoutDeps,
) -> LayoutProbe:
    """The component's layout pins rendered as a compile-time probe, PROVEN to
    hold on the pristine (frozen) skeleton before it is allowed to reject
    anything.

    The frozen layout is the crate's own, so every harvested pin holds there by
    construction. A probe that fails this calibration is therefore wrong ABOUT
    ITSELF — an expression we rendered badly, a type not in scope at crate root
    — never a real violation. Rejecting authored layouts on such a probe would
    freeze components that were fine, so a failed calibration disables pin
    gating loudly and the gate degrades to exactly its pre-probe behaviour.
    """
    if deps.write_pin_probe is None or not pins:
        return LayoutProbe()
    pinned_types = tuple(sorted(
        name for name in surface.public_direct_refs
        if surface.reachable_from_public(name) & {i.name for i in comp}))
    probe = build_layout_probe(pins, pinned_types=pinned_types)
    names = tuple(i.name for i in comp)
    if probe.skipped:
        deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: "
                 f"{len(probe.skipped)} layout pin(s) NOT compile-gated "
                 f"(could not be rendered): {list(probe.skipped)}")
    if not probe.checks_anything:
        return LayoutProbe(skipped=probe.skipped)
    deps.write_pin_probe(probe.source)
    holds, errors = deps.compile_check()
    deps.write_pin_probe(None)
    if not holds:
        deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: layout-pin probe "
                 "does not hold on the FROZEN scaffold — the probe is wrong, "
                 "not the layout; pin gating disabled for this component. "
                 f"{errors.strip()[:300]}")
        return LayoutProbe(skipped=probe.gated + probe.skipped)
    return probe


def _author_component(
    comp: tuple[PrivateSurfaceItem, ...],
    cfg_cluster: str,
    make_prompt: Callable[
        [tuple[PrivateSurfaceItem, ...], tuple[str, ...], str, str], str],
    attempt_hint: str,
    deps: AuthorLayoutDeps,
) -> tuple[dict[str, AuthoredItem] | None, str]:
    """One full authoring pass over the component's chunk plan. Returns
    (authored blocks by component-scoped uid, "") or (None, reason). Each
    chunk sees the definitions earlier chunks authored (the scheme) and gets
    `chunk_coverage_retries` extra tries when it omits items — a missing item
    re-requests ONE chunk, not the whole component."""
    uids = component_uids(comp)
    chunks = plan_author_chunks(
        comp, single_shot_max=deps.single_shot_max,
        fns_per_chunk=deps.fns_per_chunk)
    authored: dict[str, AuthoredItem] = {}
    scheme_parts: list[str] = []
    for chunk in chunks:
        chunk_items = tuple(comp[i] for i in chunk)
        chunk_uids = tuple(uids[i] for i in chunk)
        want = frozenset(chunk_uids)
        missing = want
        hint = attempt_hint
        for _ in range(1 + deps.chunk_coverage_retries):
            reply = deps.run_model(
                make_prompt(chunk_items, chunk_uids, "\n\n".join(scheme_parts),
                            hint))
            layout = parse_authored_layout(reply)
            for item in layout.items:
                if item.name in want:
                    authored[item.name] = item
            missing = frozenset(u for u in want if u not in authored)
            if not missing:
                break
            hint = _retry_hint(
                f"missing authored items: {', '.join(sorted(missing))}")
        if missing:
            return None, f"missing authored items: {', '.join(sorted(missing))}"
        scheme_parts.extend(
            authored[u].definition for u in chunk_uids)
    return authored, ""


def _retry_hint(reason: str) -> str:
    return ("\n\n=== PREVIOUS ATTEMPT FAILED ===\n"
            f"{reason}\nFix this and re-emit ALL <LAYOUT_ITEM> blocks.\n")
