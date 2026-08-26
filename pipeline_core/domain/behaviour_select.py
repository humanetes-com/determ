"""Behaviour selection for prompts (F10 Use-B, M5) — pure domain.

The few-shot channel: given the behaviours harvested for a cluster, pick the ones
RELEVANT to the fn being filled and render them as concrete RIGHT usage examples
(same archetype as ``_RUST_GOTCHAS``). Relevance is a token match — a harvested
behaviour is relevant to a unit when its setup/action code names the fn or its
container type. ``exercises`` is the intended localizer, but harvested specs
carry it empty until the return-type/construction scan fills it, so we also match
on the code text so the channel works from day one.

Budget-gated: whole specs are dropped when the char budget would overflow — a
spec is never truncated mid-body (a half example teaches the wrong shape).
"""
from __future__ import annotations

from collections.abc import Collection, Sequence

from pipeline_core.domain.behaviour import BehaviourSpec, FenceDirective
from pipeline_core.domain.rust_idents import camelcase_type_names
from pipeline_core.domain.rust_scan import contains_word

# compile_fail is a NEGATIVE behaviour; ignore is skipped — neither is a RIGHT
# usage example. Everything else demonstrates real API usage.
_EXAMPLE_FENCES = frozenset({
    FenceDirective.RUN, FenceDirective.NO_RUN, FenceDirective.SHOULD_PANIC,
})


def _spec_code(spec: BehaviourSpec) -> str:
    return f"{spec.given}\n{spec.when}".strip()


def is_relevant(spec: BehaviourSpec, *, fn_name: str, container: str) -> bool:
    """True iff `spec` demonstrates the fn under fill — by ``exercises`` id, or
    (fallback) because its code names the fn or its container type."""
    if spec.fence not in _EXAMPLE_FENCES:
        return False
    if fn_name and fn_name in spec.exercises:
        return True
    code = _spec_code(spec)
    if fn_name and contains_word(code, fn_name):
        return True
    container_types = camelcase_type_names(container)
    return bool(container_types and (container_types & camelcase_type_names(code)))


def select_usage_examples(
    specs: tuple[BehaviourSpec, ...],
    *,
    fn_name: str,
    container: str,
    char_budget: int,
    max_examples: int = 3,
) -> str:
    """Render the relevant behaviours as a ``<USAGE_EXAMPLES>`` body, or "" when
    none match / budget is zero. Deterministic: harvested order, whole specs
    only, capped by count and chars."""
    if char_budget <= 0:
        return ""
    picked: list[str] = []
    used = 0
    for spec in specs:
        if len(picked) >= max_examples:
            break
        if not is_relevant(spec, fn_name=fn_name, container=container):
            continue
        code = _spec_code(spec)
        if not code:
            continue
        block = f"// {spec.source_ref or spec.behaviour_id}\n{code}"
        if used + len(block) + 2 > char_budget and picked:
            break
        picked.append(block)
        used += len(block) + 2
    return "\n\n".join(picked)


# --------------------------------------------------------------------------
# Layout-authoring channel (t1.5): which RUNTIME behaviours a component's
# representation must make expressible.
#
# The authoring prompt used to carry only the STATIC_ASSERTION pins
# (`size_of`/autotraits/niche) and nothing about runtime observables, so a
# layout that wrapped a payload AND keyed the wrapper's own TypeId compiled,
# passed every pin, and could not satisfy `Error::msg("x").is::<&str>()` — the
# identity behaviour the harvested suite already held (measured on the filled
# `stable` tree, 2026-08-26). Selection below is the same token match as the
# few-shot channel above, widened from one fn to a whole component.
# --------------------------------------------------------------------------

# The behaviour classes whose outcome a REPRESENTATION decides, in the order the
# prompt lists them: identity (what a downcast finds) is the class the wrapping
# defect violates; exact text next; resource/option semantics after. Any other
# class (layout pins, cfg-world, unclassified usage) ranks after these.
MUST_CLASS_ORDER: tuple[str, ...] = (
    "IDENTITY", "TRAIT_OBJECT_IDENTITY", "EXACT_FORMAT", "BOUNDED_RESOURCE",
    "OPTION_SEMANTICS",
)

# Fallback grouping from the outcome kind, for a spec that carries no
# classification of its own (or an unclassified `OTHER`). Keyed by the kind's
# NAME so a kind added beside these (`TYPE_IDENTITY`, `ALT_DEBUG_STRING`) is
# grouped the moment it exists, without this module naming the enum member.
_KIND_TO_CLASS: dict[str, str] = {
    "DOWNCAST_OK": "IDENTITY",
    "TYPE_IDENTITY": "IDENTITY",
    "DISPLAY_STRING": "EXACT_FORMAT",
    "DEBUG_STRING": "EXACT_FORMAT",
    "ALT_DEBUG_STRING": "EXACT_FORMAT",
    "CHAIN_SEQUENCE": "EXACT_FORMAT",
    "PANIC_MESSAGE": "EXACT_FORMAT",
    "STATIC_ASSERTION": "LAYOUT_PIN",
}


def behaviour_class_name(spec: BehaviourSpec) -> str:
    """The class a behaviour is grouped under in the authoring prompt.

    A spec's own ``klass`` wins when it says something (`IDENTITY`,
    `EXACT_FORMAT`, …); an absent or `OTHER` classification falls back to the
    outcome kind, which can only sharpen it. An assertion-only spec (no
    ``then``) with no classification is `OTHER`: its assertions are the
    contract and the prompt shows them as usage."""
    klass = getattr(spec, "klass", None)
    name = getattr(klass, "name", None)
    if isinstance(name, str) and name and name != "OTHER":
        return name
    if spec.then is None:
        return "OTHER"
    return _KIND_TO_CLASS.get(spec.then.kind.name, "OTHER")


def class_rank(class_name: str) -> int:
    """Position of a class in the MUST-first order (unlisted classes last)."""
    if class_name in MUST_CLASS_ORDER:
        return MUST_CLASS_ORDER.index(class_name)
    return len(MUST_CLASS_ORDER)


def _exercise_names(spec: BehaviourSpec) -> set[str]:
    """Every name an ``exercises`` id can stand for: the id itself and each of
    its ``::`` segments, so `src/error.rs::new::Error` answers to `new` and to
    `Error` as well as to the full path."""
    out: set[str] = set()
    for ex in spec.exercises:
        out.add(ex)
        out.update(seg for seg in ex.split("::") if seg)
    return out


def _mentions_any(code: str, names: Collection[str]) -> bool:
    return any(n and contains_word(code, n) for n in names)


def select_component_behaviours(
    specs: Sequence[BehaviourSpec],
    *,
    component_names: Collection[str],
    public_types: Collection[str],
    cap: int = 40,
) -> list[BehaviourSpec]:
    """The runtime behaviours a component's representation must make
    expressible, MUST-classes first, at most ``cap``.

    Kept: a spec whose ``exercises`` name a component item (full id or any
    ``::`` segment), or whose setup/action text names a component item or one
    of ``public_types`` (the frozen public types whose layout reaches the
    component) as a whole word. Assertion-only specs (``then`` is None — the
    doc-test's own asserts are the check) are kept on the TEXT match only:
    their exercises are the harvester's guess and an assertion usage that never
    names the component teaches nothing about its representation.

    Dropped: STATIC_ASSERTION specs (the layout pins have their own section and
    their own compile gate), and `compile_fail`/`ignore` fences (a negative or
    skipped behaviour is not a MUST). Order within a class is harvest order, so
    the prompt is deterministic for a given suite."""
    names = [n for n in component_names if n]
    pubs = [n for n in public_types if n]
    picked: list[BehaviourSpec] = []
    for spec in specs:
        if spec.fence not in _EXAMPLE_FENCES:
            continue
        if spec.then is not None and spec.then.kind.name == "STATIC_ASSERTION":
            continue
        code = _spec_code(spec)
        by_text = _mentions_any(code, names) or _mentions_any(code, pubs)
        if spec.then is None:
            if not by_text:
                continue
        elif not (by_text or (_exercise_names(spec) & set(names))):
            continue
        picked.append(spec)
    picked.sort(key=lambda s: class_rank(behaviour_class_name(s)))
    return picked[:cap]
