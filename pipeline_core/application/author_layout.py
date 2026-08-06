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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto

from pipeline_core.domain.authored_layout import (
    AuthoredItem,
    adopt_component,
    authored_locate_keys,
    component_uids,
    definition_replacements,
    parse_authored_layout,
    shared_definitions_text,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.failure_classification import first_diagnostic
from pipeline_core.domain.layout_chunking import plan_author_chunks
from pipeline_core.domain.layout_probe import (
    LayoutProbe,
    build_field_path_probe,
    build_layout_probe,
)
from pipeline_core.domain.layout_retrigger import dropped_members_in_use
from pipeline_core.domain.oracle_field_paths import (
    FieldPath,
    field_path_requirements_text,
    read_field_paths,
)
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.domain.scaffold_rewrite import (
    replace_surface_definitions,
)
from pipeline_core.domain.test_results import rustc_error_codes
from pipeline_core.domain.type_header import (
    TypeHeader,
    declares_pub_field,
    headers_match,
    parse_type_header,
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
    # The crate's ALREADY-FILLED bodies (the live tree cargo builds), as one
    # blob — the only evidence that can contradict a layout the skeleton
    # compiles, since a skeleton's bodies are all `unimplemented!()`. Supplied
    # by the RE-TRIGGER only: at bake time nothing is filled yet, so there is
    # nothing to contradict and the veto below has no evidence to work from.
    # Absent ⇒ no veto, exactly as before it existed.
    filled_bodies: Callable[[], str] | None = None
    # The crate's WRITE-PROTECTED in-crate tests (the `gen_tests` siblings), rel
    # path → text. They are generated from the ORIGINAL source, where the private
    # fields this phase re-authors were still visible, so they read member paths
    # by name — and no later stage may edit them to match a new layout. Read here
    # so the authoring prompt carries those names and the compile gate enforces
    # them. Absent ⇒ no field-path constraint, exactly as before it existed.
    oracle_sources: Callable[[], Mapping[str, str]] | None = None
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
    # (uid, definition) per committed item — self-describing pairs so a LATER
    # cfg-cluster can adopt this exact layout instead of inventing a second,
    # incompatible one for the same private types (see `adopt_component`).
    # Empty unless committed.
    authored_items: tuple[tuple[str, AuthoredItem], ...] = ()
    adopted: bool = False   # committed from the crate-wide registry, no model call
    # The uids whose registry definitions were tried here and DISPROVED by the
    # compile+pin gate — the gate produced rustc diagnostics against them, so
    # this is a measurement, not a competing opinion. The registry write in
    # `run_layout_phase` drops these before it stores anything: a disproved
    # definition must not stand for a later cluster to adopt, nor for
    # `reconcile_layout` to reinstall over the tree that just rejected it. Empty
    # when the adoption merely could not be INSTALLED (a locate/splice miss or a
    # public-contract rejection) — nothing was compiled then, so nothing was
    # disproved, and replacing the crate's definition on that evidence is how
    # two clusters end up with two layouts for one uid.
    refuted_uids: tuple[str, ...] = ()


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
    filled = deps.filled_bodies() if deps.filled_bodies is not None else ""
    for uid, it in zip(component_uids(items), items, strict=True):
        block = authored.get(uid)
        if it.kind is SurfaceItemKind.PUB_FIELDS:
            if block is None:
                return {}, f"authored blocks missing uid {uid}"
            contract_err = _public_contract_broken(uid, it, block)
            if contract_err:
                return {}, contract_err
        if filled and block is not None:
            evicted = dropped_members_in_use(
                it.definition, block.definition, filled)
            if evicted:
                return {}, _dropped_members_reason(uid, evicted)
    per_file, per_file_uids, plan_err = definition_replacements(items, authored)
    if plan_err:
        return {}, plan_err
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


def _public_contract_broken(
    uid: str, item: PrivateSurfaceItem, block: AuthoredItem,
) -> str:
    """Non-empty reason iff an authored PUB_FIELDS block does more than design
    fields. The compile gate cannot catch either violation: a renamed generic
    or a dropped bound compiles inside the crate and breaks every downstream
    user, and a field turned `pub` compiles while silently WIDENING the API the
    reconstruction is contracted to keep. Rejecting here feeds the reason back
    as a retry hint, so the model gets to correct it."""
    frozen = parse_type_header(item.definition)
    got = parse_type_header(block.definition)
    if not headers_match(frozen, got):
        return (f"authored {uid} changed the frozen public declaration: "
                f"expected `{_head_text(frozen)}`, got `{_head_text(got)}` — "
                "author ONLY the fields, keep the head verbatim")
    if declares_pub_field(block.definition):
        return (f"authored {uid} marks a field `pub`, widening the public API "
                "— every field of this struct must stay private")
    return ""


def _dropped_members_reason(uid: str, evicted: dict[str, int]) -> str:
    """Retry hint for a layout that removed something the crate is using. Names
    every member and its read count: the model has to be able to tell which of
    its choices to undo, and a bare "you dropped a field" leaves it guessing
    across a component that can carry thirty items."""
    worst = sorted(evicted.items(), key=lambda kv: (-kv[1], kv[0]))
    detail = ", ".join(f"`{n}` ({c} read{'s' if c != 1 else ''})"
                       for n, c in worst)
    return (f"authored {uid} drops member(s) this crate's already-written "
            f"bodies still read: {detail}. Those bodies are what the layout "
            "has to be able to hold — a definition they cannot be written "
            "against is not a simpler design, it is an empty one. Keep these "
            "members (the name as well as the storage) and redesign around "
            "them.")


def _head_text(h: TypeHeader | None) -> str:
    if h is None:
        return "(unparseable)"
    return " ".join(p for p in (h.vis, h.kind, h.name + h.generics) if p)


def author_layout(
    surface: PrivateSurface,
    *,
    module_name: str,
    public_surface: str,
    select_pins: Callable[[tuple[PrivateSurfaceItem, ...]], list[BehaviourSpec]],
    symbol_inventory: str,
    deps: AuthorLayoutDeps,
    extra_guidance: str = "",
    registry: Mapping[str, AuthoredItem] | None = None,
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
    oracle = _oracle_sources(deps)
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
        uids = component_uids(comp)
        # snapshot the pristine skeleton files this component touches, so a
        # failed candidate is reverted exactly (no cross-attempt bleed).
        touched = {i.file for i in comp}
        pristine = {f: deps.read_skeleton(f) for f in touched}
        paths = _component_field_paths(comp, oracle)
        probe = _calibrated_pin_probe(surface, comp, pins, deps).merged_with(
            _calibrated_field_path_probe(surface, comp, paths, deps))
        paths_note = field_path_requirements_text(paths)

        # ADOPTION BEFORE AUTHORING. A private type authored by an earlier
        # cfg-cluster is already this crate's definition of that type, and this
        # cluster is seeded with the earlier cluster's filled bodies. Authoring
        # a second layout renames the very fields those bodies read, and the
        # whole cluster stops compiling (`no field lifetime on Ref`). The same
        # compile+pin gate decides it, so an adopted layout is never trusted
        # blind — it just costs no model call.
        adopted = adopt_component(uids, registry or {})
        refuted: tuple[str, ...] = ()
        if adopted is not None:
            ok, why, adopt_errors = _try_commit(comp, adopted, deps, probe, pristine)
            if ok:
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: adopted the "
                         "layout already fixed by an earlier cluster "
                         "(no model call).")
                results.append(ComponentResult(
                    names, True, "",
                    authored_defs=authored_locate_keys(comp, adopted),
                    authored_items=tuple((u, adopted[u]) for u in uids),
                    adopted=True))
                continue
            # Two failures wear one message otherwise, and they call for
            # opposite treatment of the crate-wide registry. WITH diagnostics
            # the stored layout was compiled here and rustc refused it, so it
            # is disproved and must not survive. WITHOUT them nothing was
            # compiled — the splice could not even install it (a frozen def the
            # locator cannot find, a public-contract rejection) — and that is a
            # statement about this skeleton, not about the layout.
            refuted = uids if adopt_errors else ()
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: the crate's "
                     f"existing layout does not hold here ({first_diagnostic(why) or why}) "
                     "— re-authoring; this cluster's layout will DIVERGE and "
                     "cross-cluster body seeding into it will not compile."
                     if adopt_errors else
                     f"  [t1.5] {surface.cfg_cluster}/{names}: the crate's "
                     f"existing layout could not be INSTALLED here ({why}) — "
                     "re-authoring, but the crate-wide definition stands: "
                     "nothing compiled, so nothing about it was disproved.")
        # Partly covered: the model must EXTEND the fixed definitions (adding
        # what this cluster's cfg needs), never redesign them — so it gets their
        # exact current text and an instruction to reproduce it verbatim.
        fixed = [registry[u] for u in uids if registry and u in registry]
        shared = ("\n\nThese definitions are ALREADY FIXED for this crate by "
                  "another cfg-cluster. Reproduce each one EXACTLY as given and "
                  "design only the remaining items around them:\n"
                  + shared_definitions_text(fixed)) if fixed and adopted is None else ""

        def _prompt(chunk_items: tuple[PrivateSurfaceItem, ...],
                    chunk_uids: tuple[str, ...], scheme: str, hint: str,
                    *, _pins: list[BehaviourSpec] = pins,
                    _anchors: dict[str, str] = anchors,
                    _shared: str = shared, _paths: str = paths_note) -> str:
            return build_layout_authoring_prompt(
                module_name=module_name, cfg_cluster=surface.cfg_cluster,
                items=chunk_items, public_surface=public_surface,
                layout_pins=_pins, symbol_inventory=symbol_inventory,
                extra_guidance=extra_guidance + _shared + _paths,
                authored_scheme=scheme,
                item_uids=chunk_uids, item_anchors=_anchors) + hint
        committed = False
        reason = "no attempt"
        committed_defs: tuple[tuple[str, str], ...] = ()
        committed_items: tuple[tuple[str, AuthoredItem], ...] = ()
        for attempt in range(1, deps.max_attempts + 1):
            hint = _retry_hint(reason) if attempt > 1 else ""
            authored, reason = _author_component(
                comp, surface.cfg_cluster, _prompt, hint, deps)
            if authored is None:
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
            ok, reason, errors = _try_commit(comp, authored, deps, probe, pristine)
            if ok:
                committed = True
                committed_defs = authored_locate_keys(comp, authored)
                committed_items = tuple((u, authored[u]) for u in uids
                                       if u in authored)
                pin_note = (f" [{len(probe.gated)} layout pin(s) verified]"
                            if probe.checks_anything else "")
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                         f"layout compiles (attempt {attempt}).{pin_note}")
                break
            if not errors:   # splice/coverage rejection, nothing was written
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                continue
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
        results.append(ComponentResult(
            names, committed, reason, authored_defs=committed_defs,
            authored_items=committed_items, refuted_uids=refuted))
    return AuthorLayoutResult(surface.cfg_cluster, tuple(results))


def _try_commit(
    items: tuple[PrivateSurfaceItem, ...], authored: dict[str, AuthoredItem],
    deps: AuthorLayoutDeps, probe: LayoutProbe, pristine: dict[str, str | None],
) -> tuple[bool, str, str]:
    """Splice one candidate layout into the skeleton and KEEP it only if the
    rewritten scaffold still compiles with the component's layout pins probed.
    Any failure rolls the touched files back to `pristine`.

    Returns (committed, reason, compiler_errors). `compiler_errors` is empty for
    a splice/coverage rejection — the caller needs the two apart, since nothing
    was written in that case and there is no diagnostic to name.

    One helper for BOTH candidate sources (the model's reply and a layout
    adopted from an earlier cfg-cluster): a second copy of the accept/rollback
    sequence is how one path ends up skipping the pin probe or leaking a failed
    candidate into the tree.
    """
    edited, splice_err = _splice_component(items, authored, deps)
    if splice_err:
        return False, splice_err, ""
    for rel, txt in edited.items():
        deps.write_skeleton(rel, txt)
    # The pin probe rides along with THIS compile: a layout that no longer
    # satisfies `size_of::<Error>() == size_of::<usize>()` must be rejected
    # here, not discovered a whole sweep later as repr failures blamed on
    # individual fn bodies.
    if probe.checks_anything and deps.write_pin_probe is not None:
        deps.write_pin_probe(probe.source)
    compiled, errors = deps.compile_check()
    if deps.write_pin_probe is not None:
        deps.write_pin_probe(None)
    if compiled:
        return True, "", ""
    for rel, orig in pristine.items():
        if orig is not None:
            deps.write_skeleton(rel, orig)
    return False, ("authored scaffold did not compile:\n" + errors
                   if errors else "authored scaffold did not compile"), errors


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


def _oracle_sources(deps: AuthorLayoutDeps) -> Mapping[str, str]:
    """The write-protected in-crate tests, read ONCE per phase. Never fatal: a
    missing oracle means the field-path constraint is simply not applied, which
    is the behaviour that existed before it did."""
    if deps.oracle_sources is None:
        return {}
    try:
        return deps.oracle_sources()
    except Exception as e:      # noqa: BLE001 — degrade, never fail authoring
        deps.log(f"  [t1.5] could not read the crate's in-crate tests for "
                 f"field-path constraints ({e}) — authoring without them.")
        return {}


def _component_field_paths(
    comp: tuple[PrivateSurfaceItem, ...], oracle: Mapping[str, str],
) -> tuple[FieldPath, ...]:
    """The member paths the oracle reads on THIS component's types.

    Two filters, both about not gating on a misread. The scan only resolves a
    local whose type is stated outright, and only for names this component owns;
    on top of that, a root whose frozen definition is still IN the skeleton must
    actually declare the first hop — if it does not, the binding was resolved to
    the wrong type and the path is dropped rather than imposed. A PUB_FIELDS root
    is exempt because its fields are precisely what the freeze removed: there is
    nothing left to check the name against, and the oracle is the only record of
    it that survived."""
    if not oracle:
        return ()
    by_name = {i.name: i for i in comp}
    out: dict[tuple[str, tuple[str, ...]], FieldPath] = {}
    for rel, src in sorted(oracle.items()):
        for p in read_field_paths(rel, src, types=by_name.keys()):
            item = by_name[p.root_type]
            if (item.kind is not SurfaceItemKind.PUB_FIELDS
                    and not contains_word(item.definition, p.fields[0])):
                continue
            out.setdefault((p.root_type, p.fields), p)
    return tuple(out.values())


def _calibrated_field_path_probe(
    surface: PrivateSurface,
    comp: tuple[PrivateSurfaceItem, ...],
    paths: tuple[FieldPath, ...],
    deps: AuthorLayoutDeps,
) -> LayoutProbe:
    """The oracle's member paths as a compile-time gate, with the one calibration
    that fits them.

    The pin probe is calibrated by requiring it to HOLD on the frozen scaffold —
    the crate's own layout satisfies its own pins by construction. A field path
    cannot answer to that authority: the freeze DELETED the fields it names, so
    `no field 'inner' on type 'Error'` is the expected reading of a correct probe
    against an elided struct, and demanding it hold would disable the gate exactly
    where it is needed. The rule is therefore by diagnostic:

      compiles                  → the path holds on the frozen layout; gate it
                                  (an authored layout may not take it away).
      fails, ONLY E0609         → the fields were elided; that is what this
                                  component is authoring. Gate it.
      anything else             → E0616 (private field — the probe sits at the
                                  crate root and cannot see a field the oracle's
                                  own module can), E0412, E0107, a parse error:
                                  the probe is wrong ABOUT ITSELF. Drop it, say so.

    A dropped probe costs nothing but the gate; a probe kept on a misrendered path
    would freeze a component whose layout was fine, which is why the fall-through
    is "drop, loudly" rather than "gate anyway"."""
    if deps.write_pin_probe is None or not paths:
        return LayoutProbe()
    generic_roots = frozenset(
        i.name for i in comp
        if (h := parse_type_header(i.definition)) is not None and h.generics)
    probe = build_field_path_probe(paths, generic_roots=generic_roots)
    names = tuple(i.name for i in comp)
    if probe.skipped:
        deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: "
                 f"{len(probe.skipped)} oracle member path(s) NOT compile-gated "
                 f"(generic root — a probe would have to invent its type "
                 f"parameters): {list(probe.skipped)}")
    if not probe.checks_anything:
        return LayoutProbe(skipped=probe.skipped)
    deps.write_pin_probe(probe.source)
    holds, errors = deps.compile_check()
    deps.write_pin_probe(None)
    codes = set(rustc_error_codes(errors, "E"))
    if holds or codes == {"E0609"}:
        deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: gating on "
                 f"{len(probe.gated)} member path(s) the crate's own "
                 f"write-protected tests read: {list(probe.gated)}")
        return probe
    deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: member-path probe is "
             "wrong about itself on the FROZEN scaffold (not a layout "
             f"violation) — path gating disabled for this component. "
             f"codes={sorted(codes)} {errors.strip()[:300]}")
    return LayoutProbe(skipped=probe.gated + probe.skipped)


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
