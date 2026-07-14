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
    AuthoredLayout,
    layout_coverage,
    parse_authored_layout,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.private_surface import PrivateSurface, PrivateSurfaceItem
from pipeline_core.domain.scaffold_rewrite import replace_definition
from pipeline_core.prompts.layout_authoring import build_layout_authoring_prompt


@dataclass(frozen=True, slots=True)
class AuthorLayoutDeps:
    run_model: Callable[[str], str]                 # prompt → reply
    read_skeleton: Callable[[str], str | None]      # rel path → text (None if absent)
    write_skeleton: Callable[[str, str], None]      # rel path, text
    compile_ok: Callable[[], bool]                  # scaffold compiles with stubs?
    log: Callable[[str], None]
    max_attempts: int = 3


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
    items: tuple[PrivateSurfaceItem, ...], layout: AuthoredLayout,
    deps: AuthorLayoutDeps,
) -> tuple[dict[str, str], bool]:
    """Apply the authored layout to the skeleton files in memory. Returns
    (edited files by rel path, all_replaced). Every item must be located in its
    home file (whitespace-tolerant) or the splice is rejected — a dangling
    frozen def next to an authored one would not compile."""
    edited: dict[str, str] = {}
    for it in items:
        authored = layout.item(it.name)
        if authored is None:
            return edited, False
        current = edited.get(it.file)
        if current is None:
            current = deps.read_skeleton(it.file)
            if current is None:
                return edited, False
        res = replace_definition(current, it.definition, authored.definition)
        if not res.replaced:
            return edited, False
        edited[it.file] = res.text
    return edited, True


def author_layout(
    surface: PrivateSurface,
    *,
    module_name: str,
    public_surface: str,
    select_pins: Callable[[tuple[PrivateSurfaceItem, ...]], list[BehaviourSpec]],
    symbol_inventory: str,
    deps: AuthorLayoutDeps,
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
        prompt = build_layout_authoring_prompt(
            module_name=module_name, cfg_cluster=surface.cfg_cluster,
            items=comp, public_surface=public_surface,
            layout_pins=pins, symbol_inventory=symbol_inventory)
        committed = False
        reason = "no attempt"
        # snapshot the pristine skeleton files this component touches, so a
        # failed candidate is reverted exactly (no cross-attempt bleed).
        touched = {i.file for i in comp}
        pristine = {f: deps.read_skeleton(f) for f in touched}
        for attempt in range(1, deps.max_attempts + 1):
            reply = deps.run_model(prompt if attempt == 1
                                   else prompt + _retry_hint(reason))
            layout = parse_authored_layout(reply)
            missing, _ = layout_coverage(comp, layout)
            if missing:
                reason = f"missing authored items: {', '.join(sorted(missing))}"
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            edited, ok = _splice_component(comp, layout, deps)
            if not ok:
                reason = "could not locate a frozen definition to replace"
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            for rel, txt in edited.items():
                deps.write_skeleton(rel, txt)
            if deps.compile_ok():
                committed = True
                reason = ""
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                         f"layout compiles (attempt {attempt}).")
                break
            reason = "authored scaffold did not compile"
            for rel, orig in pristine.items():
                if orig is not None:
                    deps.write_skeleton(rel, orig)
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                     f"(attempt {attempt}) — reverted.")
        results.append(ComponentResult(names, committed, reason))
    return AuthorLayoutResult(surface.cfg_cluster, tuple(results))


def _retry_hint(reason: str) -> str:
    return ("\n\n=== PREVIOUS ATTEMPT FAILED ===\n"
            f"{reason}\nFix this and re-emit ALL <LAYOUT_ITEM> blocks.\n")
