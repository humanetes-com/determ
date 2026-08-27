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
# The class an identity Then belongs to. Named rather than spelled inline at
# every reader: it is the one class whose loss changes what a layout may be —
# a wrap-the-payload-and-key-the-wrapper representation satisfies every layout
# pin and every format string and is refuted only by an identity outcome.
IDENTITY_CLASS = "IDENTITY"

MUST_CLASS_ORDER: tuple[str, ...] = (
    "IDENTITY", "TRAIT_OBJECT_IDENTITY", "EXACT_FORMAT", "BOUNDED_RESOURCE",
    "OPTION_SEMANTICS",
)

# Cap on runtime behaviours in a component's authoring prompt. Measured on
# the live Error/vtable component (2026-08-27): uncapped 92 is IDENTITY 24,
# EXACT_FORMAT 51, BOUNDED_RESOURCE 4, OTHER 13; 40 keeps all identity plus
# 16 exact-format and drops the entire BOUNDED_RESOURCE class (the
# 40-byte-buffer / whitespace-fallback family). The prompt already says
# `(+N more)`; the [t1.5] log names the omitted classes.
COMPONENT_BEHAVIOUR_CAP = 40

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
    cap: int | None = COMPONENT_BEHAVIOUR_CAP,
) -> list[BehaviourSpec]:
    """The runtime behaviours a component's representation must make
    expressible, MUST-classes first, at most ``cap``.

    ``cap=None`` is the uncapped match (what the [t1.5] log compares
    against so an omitted class is visible). Default is
    :data:`COMPONENT_BEHAVIOUR_CAP`.

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
    if cap is None:
        return picked
    return picked[:cap]


# --------------------------------------------------------------------------
# Capture/guess split: which behaviour classes the t0d capture route owns.
#
# A class is capture-owned when pinning it means PREDICTING an exact value:
# a byte-exact Display/Debug rendering, which concrete type a downcast finds,
# which type a trait object's vtable answers with, a length/size boundary
# string. A model asked for one of those from the source alone guesses, and
# the guess is refused — so those observables never reach the oracle at all.
# The capture route runs the program and records what it actually printed, so
# it produces them without predicting them.
#
# What stays with the guess route: OPTION_SEMANTICS (`Some`/`None` is a shape,
# not a rendering), LAYOUT_PIN (a compile-time `size_of`/autotrait fact the
# source states), CFG_WORLD, and an assertion-only spec whose asserts ARE the
# contract. Note that "OTHER" is NOT a licence: OTHER is the bucket for
# anything the classifier could not name, so the kind is consulted too (see
# CAPTURE_OWNED_KINDS below).
# --------------------------------------------------------------------------
CAPTURE_OWNED_CLASSES: frozenset[str] = frozenset({
    "EXACT_FORMAT", "IDENTITY", "TRAIT_OBJECT_IDENTITY", "BOUNDED_RESOURCE",
})

# The classes above are not the whole predicate, and taking them for it made
# the split a near no-op. `behaviour_class_name` reads a kind it does not know
# as OTHER, and `_KIND_TO_CLASS` above knows neither `RETURN_RENDERED` nor
# `NORMALIZED_STDOUT` — so the two kinds whose Then is MOST obviously an exact
# value the model would have to predict were the two that survived the filter.
# Computed on the live 230-spec suite (2026-08-27): of the 155 pinnable specs,
# the class filter alone left 6 — three free-form probe blobs and three
# `RETURN_RENDERED` values harvested out of `test_downcast_mut` — and those 6
# matched 7 of the crate's 8 source files, so `require_pin` stayed ON
# everywhere, now demanding a pin on a multi-`println!` blob that
# `behaviours_pinned_by` (an exact `then.value` join) can never match. The
# split's whole point is "the guess route is not asked for values it must
# predict"; the predicate is therefore over the KIND as well as the class.
CAPTURE_OWNED_KINDS: frozenset[str] = frozenset({
    "RETURN_RENDERED",      # a rendered return value — an exact string
    "NORMALIZED_STDOUT",    # a captured stdout blob — exact, and multi-observable
    "CHAIN_SEQUENCE",       # the exact chain render, element by element
    "PANIC_MESSAGE",        # the exact payload
})


def capture_owned(spec: BehaviourSpec) -> bool:
    """True iff pinning `spec` would require the model to PREDICT an exact
    value rather than reach a code path — the capture route's half of the
    split.

    Two independent reasons, either sufficient: the class is one of
    :data:`CAPTURE_OWNED_CLASSES` (classified by :func:`behaviour_class_name`,
    so a spec carrying no ``klass`` of its own is grouped by its outcome kind
    first), or the Then's kind is one of :data:`CAPTURE_OWNED_KINDS` — the
    kinds `behaviour_class_name` cannot classify and therefore reports as
    OTHER, which is where an unpredictable value hides from a class-only
    filter."""
    if behaviour_class_name(spec) in CAPTURE_OWNED_CLASSES:
        return True
    return spec.then is not None and spec.then.kind.name in CAPTURE_OWNED_KINDS


def drop_capture_owned(
    specs: Sequence[BehaviourSpec], *, enabled: bool = True,
) -> tuple[BehaviourSpec, ...]:
    """`specs` without the capture-owned ones, harvest order preserved.

    ``enabled=False`` is the off switch: the whole sequence comes back
    unchanged, which is the pre-split behaviour (every pinnable spec listed to
    the guess route and required of its block)."""
    if not enabled:
        return tuple(specs)
    return tuple(s for s in specs if not capture_owned(s))
