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

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from pipeline_core.domain.behaviour import BehaviourSpec, FenceDirective
from pipeline_core.domain.installable_fns import fn_decl_shape
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.rust_idents import (
    camelcase_type_names,
    impl_self_type,
    impl_trait_name,
)
from pipeline_core.domain.rust_scan import contains_word

# compile_fail is a NEGATIVE behaviour; ignore is skipped — neither is a RIGHT
# usage example. Everything else demonstrates real API usage.
_EXAMPLE_FENCES = frozenset(
    {
        FenceDirective.RUN,
        FenceDirective.NO_RUN,
        FenceDirective.SHOULD_PANIC,
    }
)


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
    "IDENTITY",
    "TRAIT_OBJECT_IDENTITY",
    "EXACT_FORMAT",
    "BOUNDED_RESOURCE",
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
# Fill-prompt identity instruction: the wrapping MUST t1.5 already renders
# via `_fmt_outcome`, which fill builders never called.
#
# `select_usage_examples` is the wrong payload for that slot — harvest order
# plus a container-type match spends the cap on doctest OTHER (the live
# `construct`/`msg`/`fmt`/`new` blob starts at `doctest::error.rs::311` and
# contains `unimplemented!()`, no `is::<`, no "never a wrapper"). File-first
# already quotes the attributed tests; the 2026-08-26 constructor fills had
# `test_downcast` in the prompt and still wrapped because `_plan_line` had
# no TypeId sentence on first-pass `write:` (`typeid_pin_consequence` fires
# only on escalated + wraps).
# --------------------------------------------------------------------------

# IDENTITY plus the trait-object sibling — the classes whose Then a wrap
# cannot satisfy. Same names `MUST_CLASS_ORDER` leads with.
IDENTITY_MUST_CLASSES: frozenset[str] = frozenset(
    {IDENTITY_CLASS, "TRAIT_OBJECT_IDENTITY"}
)

# Unique Then strings on the instruction, not a dump of every identity spec.
# Three is enough for the wrapping constructors' `&str` / `String` /
# `io::Error` once ranking prefers non-flood hits; raising it re-opens the
# HighLevel / MidLevel / LargeAlignedError parade harvest order produces.
IDENTITY_MUST_CAP = 3

# Last-segment `exercises` names on this fraction of IDENTITY specs are the
# construction-graph dump, not a localizer. Recorded 2026-08-27 stable suite
# (35 TYPE_IDENTITY): `construct` and `new` sit on 35/35, `object_drop` on
# 29/35. A harvest-order cap of 3 then starts at `Box<dyn …>` / `HighLevel`
# and never reaches `test_downcast`'s `&str`. Computed per suite, not a
# denylist of anyhow names.
_IDENTITY_FLOOD_FRACTION = 0.8


def _exercise_leaf_names(spec: BehaviourSpec) -> set[str]:
    """The last `::` segment of each `exercises` id — `construct` from
    `src/error.rs::construct`, not the path prefix and not a mid-path type
    (`Error` from `src/error.rs::new::Error` is a leaf only when it is the
    last segment)."""
    return {ex.split("::")[-1] for ex in spec.exercises if ex}


def _identity_suite(specs: Sequence[BehaviourSpec]) -> list[BehaviourSpec]:
    return [
        s for s in specs
        if s.fence in _EXAMPLE_FENCES
        and s.then is not None
        and behaviour_class_name(s) in IDENTITY_MUST_CLASSES
    ]


def identity_exercise_flood(specs: Sequence[BehaviourSpec]) -> frozenset[str]:
    """Last-segment `exercises` names that appear on most IDENTITY specs.

    Those names are the construction-graph dump (see
    `_IDENTITY_FLOOD_FRACTION`). Ranking a constructor *batch* by how many
    non-flood requested names a spec hits puts `test_downcast`'s `&str` /
    `String` / `io::Error` first; harvest order on the same batch does not.
    """
    ident = _identity_suite(specs)
    if not ident:
        return frozenset()
    counts: Counter[str] = Counter()
    for spec in ident:
        counts.update(_exercise_leaf_names(spec))
    thresh = _IDENTITY_FLOOD_FRACTION * len(ident)
    return frozenset(n for n, k in counts.items() if k >= thresh)


def _identity_hits(spec: BehaviourSpec, names: Collection[str]) -> set[str]:
    """Requested fn names this spec actually localizes to.

    Leaf `exercises` or a whole word in `when`. `given` is deliberately
    omitted: it is the whole test (helper `fn fmt`, `use std::fmt`) and is
    what made Display::fmt match identity probes whose `when` is
    `err.is::<MyErr>()`."""
    wanted = {n for n in names if n}
    hits = _exercise_leaf_names(spec) & wanted
    hits |= {n for n in wanted if contains_word(spec.when, n)}
    return hits


# ---------------------------------------------------------------------------
# Stub-time aiming. `exercises` is line coverage of the original crate
# (`exercises_by_coverage`), so matching on it alone hands the instruction to
# every unit that ran while an identity test ran — Display::fmt for
# MessageError, Drop/Deref, ptr.rs accessors. Computed on the live stable
# frontier 2026-08-28: 55/85 stubbed units and all six wrapper.rs fmt bodies
# received a MUST. Frequency (identity_exercise_flood as a match filter)
# cannot separate "ran during" from "decides TypeId": flood-strict still
# keeps those six fmts and drops `construct`'s solo instruction.
#
# `constructs_type` reads the body, which at first-pass is
# `unimplemented!()`, so the discriminator has to be the signature and the
# container — both on the record at every fill site. Formatting is refused
# as a class (`Display`/`Debug`/`fmt::Result`). Admitted: a name containing
# `downcast`; a TypeId parameter whose return is Option (the downcast
# vtable slot, not the drop slot that shares `target: TypeId`); a fn that
# constructs the identity-subject type (`Error` on this crate — inherent
# `impl Error` returning Self, `From<…> for Error`, or a named `-> Error`
# such as `ext_context` / `render`). Bare `returns Self` is not that class:
# it admits Own::new / Ref::new / Buf::new.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityMustUnit:
    """One fill target as the aiming predicate sees it at stub time."""

    name: str
    container: str = ""
    sig: str = ""


# Production members are ``FnRecord`` (``fn_name``/``sig_text``). The VO
# uses ``name``/``sig``. A bare str is neither — it used to coerce to a
# name-only unit and fail closed silently.
IdentityMustInput = IdentityMustUnit | FnRecord


# Catalog-driver admission: Display/Debug by trait name, or a fmt::Result
# return. The generic driver uses the return-type check alone and never
# reads this set. Kept so LANGUAGE_DRIVER=catalog is the pre-2026-09-09
# path, not a delete.
_FORMAT_TRAITS = frozenset({"Display", "Debug"})

# The type identity `when` texts name (`Error::msg`, `err.is::<&str>()`).
# Not "any Self return" — that is Own/Ref/Buf.
_IDENTITY_SUBJECT = "Error"


def _return_type(sig: str) -> str:
    shape = fn_decl_shape(sig)
    return shape[2] if shape is not None else ""


def catalog_is_formatting(container: str, sig: str) -> bool:
    """Catalog-driver formatting test: the Display/Debug name-set, else
    a `fmt::Result` return (inherent display/debug, Write).

    Production admission goes through :func:`language_driver`. This
    function is the catalog implementation; the generic driver does not
    call it.
    """
    trait = impl_trait_name(container) or ""
    if trait in _FORMAT_TRAITS:
        return True
    return "fmt::Result" in _return_type(sig)


def _is_formatting(container: str, sig: str) -> bool:
    """Bound-driver formatting test (generic: return type; catalog: name-set)."""
    return language_driver().is_formatting(container, sig)


def catalog_constructs_identity_subject(
        name: str, container: str, sig: str) -> bool:
    """Catalog-driver identity constructor: return type or Self is Error.

    Production aiming goes through :func:`language_driver`. This function
    is the catalog table; the generic driver harvests the subject type
    from the suite's ``when`` and does not call it.
    """
    if "downcast" in name or catalog_is_formatting(container, sig):
        return False
    ret = _return_type(sig)
    if ret == _IDENTITY_SUBJECT:
        return True
    self_ty = impl_self_type(container) or ""
    return ret == "Self" and self_ty == _IDENTITY_SUBJECT


def constructs_identity_subject(name: str, container: str, sig: str) -> bool:
    """Catalog table. Production aiming uses language_driver()."""
    return catalog_constructs_identity_subject(name, container, sig)


def catalog_identity_must_admits(name: str, container: str, sig: str) -> bool:
    """Catalog-driver identity-MUST admission, including a downcast name.

    Production aiming goes through :func:`language_driver`. The generic
    driver drops the name token and keys on TypeId-plus-Option plus the
    harvested subject type.
    """
    if catalog_is_formatting(container, sig):
        return False
    if "downcast" in name:
        return True
    ret = _return_type(sig)
    if "TypeId" in sig and ret.startswith("Option"):
        return True
    return catalog_constructs_identity_subject(name, container, sig)


def identity_must_admits(name: str, container: str, sig: str) -> bool:
    """Catalog table. Production aiming uses language_driver()."""
    return catalog_identity_must_admits(name, container, sig)


def identity_subject_from_when(when: str) -> str:
    """Last path segment of a ``Type::`` call in ``when``, else ``""``.

    The type identity Thens already name (``Error::msg``, ``Counter::new``).
    Harvested, not hardcoded: a different subject type is a different
    last segment.
    """
    last = ""
    i = 0
    n = len(when)
    while i < n:
        ch = when[i]
        if "A" <= ch <= "Z":
            j = i + 1
            while j < n and (when[j].isalnum() or when[j] == "_"):
                j += 1
            if j + 1 < n and when[j] == ":" and when[j + 1] == ":":
                last = when[i:j]
            i = j
        else:
            i += 1
    return last


def identity_subject_from_specs(specs: Sequence[BehaviourSpec]) -> str:
    """The identity type the suite's ``when`` texts name, most common."""
    counts: Counter[str] = Counter()
    for spec in _identity_suite(specs):
        for text in (spec.when, spec.given):
            name = identity_subject_from_when(text)
            if name:
                counts[name] += 1
                break
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _coerce_unit(item: IdentityMustInput) -> IdentityMustUnit:
    """``IdentityMustUnit`` as-is; ``FnRecord`` via ``fn_name``/``sig_text``.

    A bare name used to become ``IdentityMustUnit(name=str(item))`` with
    empty container/sig and was silently refused by
    :func:`identity_must_admits` (fail-closed, but the checker could not
    see it). Callers that bypass the checker get TypeError, not an empty
    instruction.
    """
    if isinstance(item, IdentityMustUnit):
        return item
    if isinstance(item, FnRecord):
        return IdentityMustUnit(
            name=item.fn_name,
            container=item.container or "",
            sig=item.sig_text or "",
        )
    raise TypeError(
        "identity-must unit needs IdentityMustUnit or FnRecord; "
        f"got {type(item).__name__}")


def _constructor_sibling_leaves(
    ident: Sequence[BehaviourSpec], flood: Collection[str],
) -> set[str]:
    """Non-flood identity leaf names — the localizers a constructor with
    zero `exercises` (live `construct_from_display`) can borrow.

    Flood names are the construction-graph dump (`construct`/`new` on every
    identity spec); matching those spends the cap on harvest-order
    `Box<dyn …>` / `HighLevel`. Non-flood leaves on the recorded and live
    suites rank `&str` / `String` / `io::Error` first.
    """
    leaves: set[str] = set()
    for spec in ident:
        leaves |= _exercise_leaf_names(spec)
    return {n for n in leaves if n and n not in flood}


def select_identity_must(
    specs: Sequence[BehaviourSpec],
    *,
    fn_names: Collection[str] = (),
    units: Sequence[IdentityMustInput] = (),
    cap: int = IDENTITY_MUST_CAP,
) -> list[BehaviourSpec]:
    """IDENTITY / TRAIT_OBJECT_IDENTITY specs a fill of these units must
    satisfy, unique Then, at most `cap`.

    `units` is the aimed path: only units :func:`identity_must_admits`
    keeps contribute names. A constructor admitted with no spec hits
    falls back to non-flood identity leaves so a solo
    `construct_from_display` still gets `&str` — admission is not a Then;
    the 229-spec suite names that fn in zero `exercises`. A bare str is
    not a unit (`IdentityMustInput` is `IdentityMustUnit | FnRecord`).

    `fn_names` without `units` is the pre-aiming match (leaf exercises /
    `when` only, no container-type, no stub-time filter). Ranking tests
    and the names-only negative control use it. Production call sites pass
    `units`.

    Order: more non-flood leaf-exercise hits first, `ok=True` before
    `ok=False`, harvest order within that. Dedupes by `(kind, value, ok)`
    so three `MyErr` probes do not spend the cap. Empty when none match.
    """
    unit_list = [_coerce_unit(u) for u in units]
    constructor_miss = False
    if unit_list:
        subject = identity_subject_from_specs(specs)
        drv = language_driver()
        admitted = [u for u in unit_list
                    if drv.identity_must_admits(
                        u.name, u.container, u.sig, subject)]
        names = {u.name for u in admitted if u.name}
        constructor_miss = any(
            drv.constructs_identity_subject(
                u.name, u.container, u.sig, subject)
            for u in admitted)
    else:
        names = {n for n in fn_names if n}
    if cap <= 0:
        return []
    ident = _identity_suite(specs)
    flood = identity_exercise_flood(ident)
    rank_names = names
    matched = [s for s in ident if _identity_hits(s, names)] if names else []
    if not matched and constructor_miss:
        rank_names = _constructor_sibling_leaves(ident, flood)
        matched = [s for s in ident if _identity_hits(s, rank_names)]
    if not matched or not rank_names:
        return []

    def _rank(spec: BehaviourSpec) -> tuple[int, int]:
        specific = _identity_hits(spec, rank_names) - flood
        # then.ok is the wrap-violating MUST; the trait-object probe's
        # `is::<MyErr>() == false` is a different observable.
        then = spec.then
        ok_rank = 0 if (then is not None and then.ok) else 1
        return (-len(specific), ok_rank)

    matched.sort(key=_rank)
    seen: set[tuple[str, str, bool]] = set()
    out: list[BehaviourSpec] = []
    for spec in matched:
        then = spec.then
        if then is None:
            continue
        key = (then.kind.name, then.value, then.ok)
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
        if len(out) >= cap:
            break
    return out


# --------------------------------------------------------------------------
# Capture/guess split: the guess route is an allowlist.
#
# Pinning a behaviour whose Then is an exact value means PREDICTING that
# value from the source. The model guesses, the guess is refused, and the
# observable never reaches the oracle. The capture route runs the program
# and records what it printed, so it produces those without predicting them.
#
# What the guess route can still answer (and is therefore listed to t0c and
# required of its block):
#
#   OPTION_SEMANTICS  `Some`/`None` is a shape, not a rendering
#   LAYOUT_PIN        a compile-time `size_of`/autotrait fact the source states
#   CFG_WORLD         a promise that only holds in one cfg cluster
#   assertion-only    `then is None` — the asserts ARE the contract
#
# Everything else with a Then is capture-owned. OTHER is the classifier's
# junk drawer, not a licence: a kind `behaviour_class_name` cannot name
# (RETURN_RENDERED, NORMALIZED_STDOUT, and any kind added beside them) is
# still an exact value a model would have to predict. Taking the four
# named classes as the predicate made the split a near no-op. Computed on
# the live 230-spec suite (2026-08-27): of 155 pinnable specs the class
# allowlist left 6 — three free-form probe blobs and three RETURN_RENDERED
# values out of `test_downcast_mut` — and those 6 matched 7 of the crate's
# 8 source files, so `require_pin` stayed ON everywhere, demanding a pin on
# a multi-`println!` blob that `behaviours_pinned_by` can never match.
# --------------------------------------------------------------------------
GUESS_ROUTE_CLASSES: frozenset[str] = frozenset(
    {
        "OPTION_SEMANTICS",
        "LAYOUT_PIN",
        "CFG_WORLD",
    }
)


def capture_owned(spec: BehaviourSpec) -> bool:
    """True iff pinning `spec` would require the model to PREDICT an exact
    value rather than reach a code path — the capture route's half of the
    split.

    The guess route is the allowlist (:data:`GUESS_ROUTE_CLASSES`, plus an
    assertion-only spec whose asserts ARE the contract). Every other spec
    that carries a Then is capture-owned, including one whose class is
    OTHER: OTHER is what the classifier could not name, which is where an
    unpredictable value hides from a class-only filter."""
    if spec.then is None:
        return False
    return behaviour_class_name(spec) not in GUESS_ROUTE_CLASSES


def drop_capture_owned(
    specs: Sequence[BehaviourSpec],
    *,
    enabled: bool = True,
) -> tuple[BehaviourSpec, ...]:
    """`specs` without the capture-owned ones, harvest order preserved.

    ``enabled=False`` is the off switch: the whole sequence comes back
    unchanged, which is the pre-split behaviour (every pinnable spec listed to
    the guess route and required of its block)."""
    if not enabled:
        return tuple(specs)
    return tuple(s for s in specs if not capture_owned(s))
