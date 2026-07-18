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
    component_uids,
    parse_authored_layout,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.layout_chunking import plan_author_chunks
from pipeline_core.domain.private_surface import PrivateSurface, PrivateSurfaceItem
from pipeline_core.domain.scaffold_rewrite import replace_definitions
from pipeline_core.prompts.layout_authoring import build_layout_authoring_prompt


@dataclass(frozen=True, slots=True)
class AuthorLayoutDeps:
    run_model: Callable[[str], str]                 # prompt → reply
    read_skeleton: Callable[[str], str | None]      # rel path → text (None if absent)
    write_skeleton: Callable[[str, str], None]      # rel path, text
    compile_check: Callable[[], tuple[bool, str]]   # (compiles?, error excerpt)
    log: Callable[[str], None]
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
) -> tuple[dict[str, str], bool]:
    """Apply the authored layout (keyed by protocol uid) to the skeleton files
    in memory. Returns (edited files by rel path, all_replaced). Replacements
    are batched per file and occurrence-ordered (`replace_definitions`) so
    same-named items land in their OWN slots; any unlocatable frozen def
    rejects the splice — a dangling frozen def next to an authored one would
    not compile."""
    per_file: dict[str, list[tuple[str, str]]] = {}
    for uid, it in zip(component_uids(items), items, strict=True):
        block = authored.get(uid)
        if block is None:
            return {}, False
        per_file.setdefault(it.file, []).append((it.definition, block.definition))
    edited: dict[str, str] = {}
    for rel, pairs in per_file.items():
        current = deps.read_skeleton(rel)
        if current is None:
            return edited, False
        res = replace_definitions(current, pairs)
        if not res.replaced:
            return edited, False
        edited[rel] = res.text
    return edited, True


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
        def _prompt(chunk_items: tuple[PrivateSurfaceItem, ...],
                    chunk_uids: tuple[str, ...], scheme: str, hint: str,
                    *, _pins: list[BehaviourSpec] = pins) -> str:
            return build_layout_authoring_prompt(
                module_name=module_name, cfg_cluster=surface.cfg_cluster,
                items=chunk_items, public_surface=public_surface,
                layout_pins=_pins, symbol_inventory=symbol_inventory,
                extra_guidance=extra_guidance, authored_scheme=scheme,
                item_uids=chunk_uids) + hint
        committed = False
        reason = "no attempt"
        # snapshot the pristine skeleton files this component touches, so a
        # failed candidate is reverted exactly (no cross-attempt bleed).
        touched = {i.file for i in comp}
        pristine = {f: deps.read_skeleton(f) for f in touched}
        for attempt in range(1, deps.max_attempts + 1):
            hint = _retry_hint(reason) if attempt > 1 else ""
            authored, reason = _author_component(
                comp, surface.cfg_cluster, _prompt, hint, deps)
            if authored is None:
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            edited, ok = _splice_component(comp, authored, deps)
            if not ok:
                reason = "could not locate a frozen definition to replace"
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            for rel, txt in edited.items():
                deps.write_skeleton(rel, txt)
            compiled, errors = deps.compile_check()
            if compiled:
                committed = True
                reason = ""
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                         f"layout compiles (attempt {attempt}).")
                break
            reason = ("authored scaffold did not compile:\n"
                      + errors if errors else "authored scaffold did not compile")
            for rel, orig in pristine.items():
                if orig is not None:
                    deps.write_skeleton(rel, orig)
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                     f"scaffold did not compile (attempt {attempt}) — reverted.")
        results.append(ComponentResult(names, committed, reason))
    return AuthorLayoutResult(surface.cfg_cluster, tuple(results))


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
