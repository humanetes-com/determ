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
