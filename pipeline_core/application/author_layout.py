"""Layout-authoring use-case — orchestrates one cfg-cluster's
private-layout design between bake and strip.

Per connected component of the private surface: prompt the model, parse the
authored layout, require it to COVER every replaceable item, splice it into the
skeleton scaffold, and accept only if the rewritten scaffold still compiles with
``unimplemented!()`` bodies (the same invariant the baked skeleton holds today).
A component that will not compile after the retry budget is left with its frozen
layout — degraded, never broken (the run proceeds exactly as before layout authoring).

Collaborators (model, skeleton IO, compile check) are injected — this module is
pure orchestration over domain VOs.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from enum import Enum, auto

from pipeline_core.domain.authored_layout import (
    AuthoredItem,
    adopt_component,
    authored_locate_keys,
    carry_frozen_facts,
    component_uids,
    definition_replacements,
    derive_traits,
    layout_regressions,
    parse_authored_layout,
    shared_definitions_text,
    shared_item_ids_agree,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_select import (
    COMPONENT_BEHAVIOUR_CAP,
    IDENTITY_CLASS,
    behaviour_class_name,
    class_rank,
    select_component_behaviours,
)
from pipeline_core.domain.failure_classification import first_diagnostic
from pipeline_core.domain.layout_chunking import plan_author_chunks
from pipeline_core.domain.layout_probe import (
    FIELD_PROBE_MARKERS,
    LayoutGate,
    LayoutProbe,
    build_field_path_probe,
    build_layout_probe,
    install_probe_region,
    strip_probe_region,
)
from pipeline_core.domain.layout_retrigger import (
    dropped_members_in_use,
    is_mechanical_reason,
    members_in_use,
    members_in_use_text,
)
from pipeline_core.domain.oracle_derives import (
    confirmed_by_declaration,
    derive_requirements_text,
    scan_required_derives,
)
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
from pipeline_core.threads import ContextPool

# Item kinds that can carry a `#[derive(...)]`. A trait, a type alias or a fn
# signature whose name a test happens to call is not a derive target, and
# asking for one authors a layout that cannot compile.
_DERIVABLE_KINDS = frozenset({
    SurfaceItemKind.STRUCT, SurfaceItemKind.ENUM, SurfaceItemKind.PUB_FIELDS,
})


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
    # The crate's write-protected tests INCLUDING its `tests/` integration
    # tests, for the derive requirement below. Deliberately separate from
    # `oracle_sources`: that one feeds the member-path compile gate and is
    # scoped to the generated siblings on purpose, and widening it would add
    # constraints to a gate whose rejection costs a whole authoring attempt.
    # The derive scan needs the wider view because the requirement that broke
    # five of eight authorings on 2026-08-11 lives in `tests/baseline.rs` and
    # `tests/integration.rs`, neither of which is a `gen_tests` sibling.
    # Absent ⇒ falls back to `oracle_sources`, i.e. siblings only.
    frozen_test_sources: Callable[[], Mapping[str, str]] | None = None
    max_attempts: int = 3
    # Chunking: components above `single_shot_max` items are authored in
    # chunks (types first, then fns) instead of demanding one 40-block reply.
    single_shot_max: int = 14
    fns_per_chunk: int = 10
    chunk_coverage_retries: int = 2
    # RE-author only (set by the retrigger): reject a replacement that is
    # strictly worse than the definition it replaces — lost derives, a `pub`
    # item made private (`domain.authored_layout.layout_regressions`). At bake
    # time the reference is the frozen original and a redesign may
    # legitimately not carry its every derive, so the initial pass leaves
    # this False.
    guard_replaced_layout: bool = False
    # Components whose attempt-1 authoring (every chunk, with its coverage
    # retries) is asked ahead of their turn, side by side (`_Prefetch`). The
    # prompt reads no tree state, so the replies are the ones the component's
    # turn would get; every compile, calibration, adoption and retry still runs
    # in surface order. 1 asks nothing ahead (the serial pass).
    lanes: int = 1


@dataclass(frozen=True, slots=True)
class ComponentResult:
    item_names: tuple[str, ...]
    authored: bool
    reason: str = ""
    gated: bool = False   # skipped-on-purpose (harvest gap), not a failure
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
    # Non-empty when this component was decided inside ANOTHER world's pass,
    # by the one ask over both worlds' items (`PeerWorld`): that world's name.
    # This world's own pass then makes no call for it.
    asked_with: str = ""


@dataclass(frozen=True)
class PeerWorld:
    """Another world of this launch whose matching component is decided by the
    same ask. Every attempt's reply covers the union of both components' items
    and is compiled in each world; a world that compiles it commits.

    Why: a component the first world could not author was asked again, from
    scratch, by the next world. On run `42057-641e589c` (2026-10-26) the
    34-item `Error` component failed three attempts in `stable` (12 calls,
    ~40 min) and the same three in `nightly` (11 calls, ~54 min); the two
    components differed by three fns. The 3 runs read in full did the same."""

    surface: PrivateSurface
    deps: AuthorLayoutDeps
    select_pins: Callable[[tuple[PrivateSurfaceItem, ...]], list[BehaviourSpec]]
    behaviour_specs: Sequence[BehaviourSpec] = ()


@dataclass
class _PeerSide:
    """One peer world's state for one component during the shared ask."""

    world: str
    deps: AuthorLayoutDeps
    comp: tuple[PrivateSurfaceItem, ...]
    uids: tuple[str, ...]
    gate: LayoutGate
    pristine: dict[str, str | None]
    committed: bool = False
    stopped: bool = False
    tried: bool = False
    reason: str = "no attempt"
    attempt: int = 0
    defs: tuple[tuple[str, str], ...] = ()
    items: tuple[tuple[str, AuthoredItem], ...] = ()
    seen_diags: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _SideChoice:
    """A peer world's component chosen for the shared ask, before anything
    is compiled in that peer."""

    world: str
    comp: tuple[PrivateSurfaceItem, ...]
    uids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AskShape:
    """What one component is asked: the items (its own plus the peers'), the
    worlds label, the prompt builder, and the peer choices that survived the
    union check."""

    ask_comp: tuple[PrivateSurfaceItem, ...]
    label: str
    prompt: Callable[[tuple[PrivateSurfaceItem, ...], tuple[str, ...], str, str], str]
    choices: list[_SideChoice]
    anchors: dict[str, str]


class _Prefetch:
    """Attempt-1 authoring of components ahead of their turn, side by side.

    A component's prompt is built from inputs no commit changes (`_shape`), so
    its attempt-1 replies can be asked before the components ahead of it have
    compiled. Each component is rehearsed whole (`_author_component`, every
    chunk and coverage retry, with the real model and a silent log) on a lane,
    recording prompt -> replies. At its turn the component's attempt 1 asks
    through `replaying`, which hands back a recorded reply for a prompt it
    recorded and asks the model for any other. So the pass at any width is the
    serial pass: a reply is only ever used for the exact prompt it answered,
    and every compile, calibration, adoption and retry stays in surface order
    on the calling thread. `lanes <= 1` asks nothing ahead."""

    def __init__(self, lanes: int) -> None:
        self.enabled = lanes > 1
        self._pool = (ContextPool(max_workers=lanes, thread_name_prefix="layout-ahead")
                      if self.enabled else None)
        self._runs: dict[int, Future[dict[str, list[str]]]] = {}
        self.asked = 0
        self.used = 0

    def submit(self, key: int, shape: _AskShape, deps: AuthorLayoutDeps) -> None:
        if self._pool is None:
            return

        def _rehearse() -> dict[str, list[str]]:
            rec: dict[str, list[str]] = {}

            def _ask(prompt: str) -> str:
                reply = deps.run_model(prompt)
                rec.setdefault(prompt, []).append(reply)
                return reply

            _author_component(shape.ask_comp, shape.label, shape.prompt, "",
                              replace(deps, run_model=_ask, log=lambda _m: None))
            return rec

        self._runs[key] = self._pool.submit(_rehearse)

    def replaying(self, key: int, deps: AuthorLayoutDeps) -> AuthorLayoutDeps:
        fut = self._runs.pop(key, None)
        if fut is None:
            return deps
        try:
            rec = fut.result()
        except Exception as e:
            deps.log(f"  [t1.5] asked-ahead authoring failed ({type(e).__name__}: {e}); "
                     "this component asks at its turn.")
            return deps
        self.asked += sum(len(v) for v in rec.values())

        def _ask(prompt: str) -> str:
            got = rec.get(prompt)
            if got:
                self.used += 1
                return got.pop(0)
            return deps.run_model(prompt)

        return replace(deps, run_model=_ask)

    def close(self, deps: AuthorLayoutDeps) -> None:
        if self._pool is None:
            return
        for fut in self._runs.values():
            fut.cancel()
        self._pool.shutdown(wait=True, cancel_futures=True)
        unused = sum(1 for f in self._runs.values() if not f.cancelled())
        if self.asked or unused:
            deps.log(f"  [t1.5] asked ahead: {self.used} of {self.asked} reply(ies) used at "
                     f"their component's turn"
                     + (f"; {unused} component(s) asked ahead never reached their turn"
                        if unused else ""))


class LayoutOutcome(Enum):
    """What became of ONE connected component after a t1.5 pass — the shape
    the layout re-trigger mechanism needs to find "which private types are
    still on the ORIGINAL frozen scaffold" (FROZEN = every retry attempt
    failed to compile; the model never got a layout it could work with)."""

    AUTHORED = auto()   # a model-authored replacement compiles and was spliced
    GATED = auto()      # refused to author blind (harvest gap) — left frozen on purpose
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
    # world → component uids → what the shared ask decided there. Read by
    # that world's own pass (`author_layout(handled=...)`).
    peer_results: Mapping[str, Mapping[frozenset[str], ComponentResult]] = field(
        default_factory=dict)

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
    # `authored` is already carried (`carry_frozen_facts`, applied once in
    # `_try_commit`), so every check below reads the text that is spliced
    # AND stored.
    if deps.guard_replaced_layout:
        for uid, it in zip(component_uids(items), items, strict=True):
            block = authored.get(uid)
            if block is None:
                continue
            regressions = layout_regressions(it.definition, block.definition)
            if regressions:
                return {}, (
                    f"authored {uid} is strictly worse than the layout it "
                    "replaces: " + "; ".join(regressions) + " — the current "
                    "layout compiled and was accepted with these; keep every "
                    "derive and the visibility it carries")
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
    behaviour_specs: Sequence[BehaviourSpec] = (),
    peers: Sequence[PeerWorld] = (),
    handled: Mapping[frozenset[str], ComponentResult] | None = None,
) -> AuthorLayoutResult:
    """Author every connected component of `surface`. Each component is
    committed to the skeleton only after the rewritten scaffold compiles; a
    failed component is rolled back to its frozen state.

    missing-pin gate: a component that is LAYOUT-SENSITIVE (its private types feed a
    public type's size/niche) but reaches here with NO layout pins is a harvest
    gap, not a free layout — authoring it blind produces a wrong-sized public
    type that compiles yet fails the repr tests a whole sweep later. Such a
    component is left FROZEN (gated), loud in the log, never authored blind.

    `behaviour_specs` is the cluster's whole behaviour suite — the same list
    `select_pins` draws the STATIC_ASSERTION pins from. Per component the
    RUNTIME behaviours its representation must make expressible are
    selected from it (`select_component_behaviours`) and put in the prompt;
    the pins alone let a layout that wraps a payload and keys the wrapper's
    own TypeId pass every gate and fail `Error::msg("x").is::<&str>()`.
    Empty (the default) leaves the prompt exactly as it was.

    `peers` are the worlds whose pass comes after this one. A component this
    world authors is asked once over its items and each peer's matching
    component's items (`PeerWorld`). The union is refused when its ids would
    rename an item, or when a shared id denotes a different declaration in
    another world. When this world commits, a peer that was not compiled
    against that reply is restored and judged on it; a peer that still does
    not commit keeps the path it always had (adopt, or extend around the
    fixed definitions). When no attempt commits here, a peer that committed
    an earlier reply keeps that reply, and a peer that did not is recorded
    as failed and is not asked again. `handled` is what an earlier
    world's pass decided for this world's components."""
    results: list[ComponentResult] = []
    peer_results: dict[str, dict[frozenset[str], ComponentResult]] = {
        p.surface.cfg_cluster: {} for p in peers}
    claimed: dict[str, set[frozenset[str]]] = {p.surface.cfg_cluster: set() for p in peers}
    oracle = _oracle_sources(deps)
    frozen_tests = _frozen_test_sources(deps) or oracle
    # Read ONCE for the whole surface, not per component: on the retrigger path
    # this is a lambda over an already-materialised string, but on the bake path
    # it enumerates the filled tree, and there is one tree per call. Empty at
    # t1.5 (nothing is filled yet), which is what makes the requirements note
    # below cost the initial authoring nothing.
    filled_now = deps.filled_bodies() if deps.filled_bodies is not None else ""

    def _settle(idx: int, comp: tuple[PrivateSurfaceItem, ...]) -> ComponentResult | None:
        """The component's turn: every skeleton read, calibration, adoption,
        commit and retry, in surface order, as the serial pass ran them. Its
        attempt-1 authoring replies come from `prefetch` when they were asked
        ahead under the same prompts; any prompt not asked ahead is asked
        here."""
        names = tuple(i.name for i in comp)
        done = handled.get(frozenset(component_uids(comp))) if handled else None
        if done is not None:
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: "
                     + (f"authored by the one ask with {done.asked_with}, compiled "
                        "here (no model call)." if done.authored else
                        f"left frozen — the one ask with {done.asked_with} found no "
                        "layout that compiles here; not asked again: "
                        f"{first_diagnostic(done.reason) or done.reason}"))
            return done
        pins = select_pins(comp)
        if not pins and surface.component_is_layout_sensitive(comp):
            reason = ("layout-sensitive (feeds a public type's size) but NO "
                      "layout pins harvested — refusing to author blind")
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: GATED — {reason}")
            return ComponentResult(names, False, reason, gated=True)
        uids = component_uids(comp)
        # snapshot the pristine skeleton files this component touches, so a
        # failed candidate is reverted exactly (no cross-attempt bleed).
        touched = {i.file for i in comp}
        pristine = {f: deps.read_skeleton(f) for f in touched}
        # Two probes, two authorities, two homes — one compile per attempt. The
        # pins are calibrated by having to HOLD on the frozen scaffold and ride
        # in the crate root; the member paths are calibrated by their diagnostic
        # and ride in the file that declares each root type.
        paths = _component_field_paths(comp, oracle)
        pin_probe = _calibrated_pin_probe(surface, comp, pins, deps)
        gate = LayoutGate(
            root=pin_probe,
            in_file=_calibrated_field_path_probes(surface, comp, paths, deps))
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
            tried = _try_commit(comp, adopted, deps, gate, pristine)
            why, adopt_errors = tried.reason, tried.errors
            gate = _gate_after(gate, tried)
            if tried.committed:
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: adopted the "
                         "layout already fixed by an earlier cluster "
                         "(no model call).")
                kept = dict(tried.authored)
                return ComponentResult(
                    names, True, "",
                    authored_defs=authored_locate_keys(comp, kept),
                    authored_items=tuple((u, kept[u]) for u in uids),
                    adopted=True)
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
        choices = _choose_peer_sides(comp, uids, peers, claimed, registry or {})
        shape = _shape(comp, names, pins, uids, choices, claimed,
                       adopted_none=adopted is None, deps=deps)
        sides = [_calibrate_side(c, peers) for c in shape.choices]
        ask_comp, label, _prompt = shape.ask_comp, shape.label, shape.prompt
        committed = False
        reason = "no attempt"
        committed_defs: tuple[tuple[str, str], ...] = ()
        committed_items: tuple[tuple[str, AuthoredItem], ...] = ()
        # Every violation this fire has produced so far, in order. The retry
        # hint carries them ALL: a reply that satisfies only the most recent
        # rejection routinely re-breaks an earlier one.
        violations: list[str] = []

        def _learned(r: str, *, _v: list[str] = violations) -> None:
            if r and r not in _v:
                _v.append(r)

        # Compile diagnostics this component has already produced. A repeat
        # means the retry hint did not move the model off the answer, and the
        # next attempt buys the same one — see `_stop_repeat` below.
        seen_diags: list[str] = []

        def _stop_mechanical(
            r: str, n: int, *, _names: tuple[str, ...] = names,
        ) -> bool:
            """Attempt-invariant failures end the loop at the attempt that
            found them. `('CastTo',)` and `('Mut', 'error_mut')` each spent all
            three attempts on `could not locate a frozen definition to replace:
            … in ptr.rs` on run `669347-cf33eae3` — the reason names the store,
            not the model's answer, so attempts 2 and 3 re-ran a lookup that
            had already reported the definition is not there to replace. The
            fire's own mechanical budget already refuses on this reason, but it
            is consulted per FIRE, and all three attempts happen inside one."""
            if not is_mechanical_reason(r):
                return False
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{_names}: {r} — "
                     f"attempt-invariant, so attempts {n + 1}.."
                     f"{deps.max_attempts} are not spent on it. The splice/"
                     "locate store is what has to change, not the prompt.")
            return True

        for attempt in range(1, deps.max_attempts + 1):
            hint = _retry_hint(violations) if violations else ""
            authored, reason = _author_component(
                ask_comp, label, _prompt, hint,
                prefetch.replaying(idx, deps) if attempt == 1 and not hint else deps)
            if authored is None:
                deps.log(f"  [t1.5] {label}/{names}: {reason} "
                         f"(attempt {attempt})")
                _learned(reason)
                for s in sides:
                    s.reason = reason
                if _stop_mechanical(reason, attempt):
                    break
                continue
            for s in sides:
                _try_peer_side(s, authored, attempt, surface.cfg_cluster, _learned)
            tried = _try_commit(comp, authored, deps, gate, pristine)
            reason, errors = tried.reason, tried.errors
            # Before the success line and before the next attempt: a probe the
            # compile proved unusable must neither be claimed as verified nor
            # reinstalled (and re-compiled) to reach the same verdict again.
            gate = _gate_after(gate, tried)
            if tried.committed:
                committed = True
                kept = dict(tried.authored)
                committed_defs = authored_locate_keys(comp, kept)
                committed_items = tuple((u, kept[u]) for u in uids
                                       if u in kept)
                # The registry stores THIS reply. A peer that compiled an
                # earlier one has not been judged on it; the text its pass
                # records would not be the text cluster start installs.
                for s in sides:
                    if not (s.tried and s.attempt == attempt):
                        _rejudge_peer_on_committed_reply(
                            s, authored, attempt, surface.cfg_cluster, _learned)
                pin_note = _verified_note(gate)
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                         f"layout compiles (attempt {attempt}).{pin_note}")
                break
            if not errors:   # splice/coverage rejection, nothing was written
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {reason} "
                         f"(attempt {attempt})")
                _learned(reason)
                if _stop_mechanical(reason, attempt):
                    break
                continue
            # Name the first diagnostic: an environmental failure that no
            # authored layout can fix (E0554 = nightly `#![feature]` built on
            # stable) is otherwise indistinguishable in the log from a model
            # that wrote bad code, and reads as N hard components instead of
            # one broken gate.
            diag = first_diagnostic(errors)
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: authored "
                     f"scaffold did not compile (attempt {attempt}) — reverted"
                     + (" (layout pins/member paths were gated this attempt)"
                        if gate.gated else "")
                     + (f": {diag}" if diag else "."))
            _learned(reason)
            # The per-impl fill route already stops here and says why —
            # "all N attempt(s) failed with an identical signature — the
            # failure does not depend on what the model wrote; a bigger prompt
            # re-purchases the same answer". This route had no equivalent, so
            # the 34-name construct/vtable component on run `669347-cf33eae3`
            # returned `error[E0255]: the name `Backtrace` is defined multiple
            # times` on attempt 1 AND attempt 3, having been told about it in
            # between. Judged on the DIAGNOSTIC rather than consecutively:
            # attempt 2 there failed a different way (dropped `ptr`), and a
            # strict consecutive test would have let the repeat through.
            if diag and diag in seen_diags:
                deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: attempt "
                         f"{attempt} reproduced a diagnostic this component "
                         "already answered for — the retry hint carried it and "
                         "the model wrote it again, so the remaining attempt(s) "
                         "re-purchase the same scaffold. Stopping.")
                break
            if diag:
                seen_diags.append(diag)
        result = ComponentResult(
            names, committed, reason, authored_defs=committed_defs,
            authored_items=committed_items, refuted_uids=refuted)
        for s in sides:
            # A peer this world's commit leaves uncommitted keeps its own path
            # (adopt, or extend around the fixed definitions): the question it
            # would ask is no longer the one asked here. A peer of a component
            # no attempt committed here has been asked; it is not asked again.
            if s.committed or not committed:
                peer_results[s.world][frozenset(s.uids)] = ComponentResult(
                    tuple(i.name for i in s.comp), s.committed, s.reason,
                    authored_defs=s.defs, authored_items=s.items,
                    adopted=s.committed and committed and _same_text(s.items, committed_items),
                    asked_with=surface.cfg_cluster)
        return result

    def _shape(
        comp: tuple[PrivateSurfaceItem, ...], names: tuple[str, ...],
        pins: list[BehaviourSpec], uids: tuple[str, ...],
        choices: list[_SideChoice], claim: dict[str, set[frozenset[str]]],
        *, adopted_none: bool, deps: AuthorLayoutDeps,
    ) -> _AskShape:
        """What this component is asked, from the surface, the suite, the
        oracle, the registry and the peers' surfaces only: nothing here reads
        or writes the skeleton, so the same component asked ahead of its turn
        (`_Prefetch`) builds the same prompt its turn builds. `claim` is the
        peers' claimed components, from which a refused union is released."""
        anchors = _public_anchors(surface, comp)
        matched = select_component_behaviours(
            behaviour_specs, component_names=names,
            public_types=_pinned_public_types(surface, comp), cap=None)
        behaviours = matched[:COMPONENT_BEHAVIOUR_CAP]
        if behaviour_specs:
            omitted = _omitted_behaviour_classes(matched, behaviours)
            extra = (f"; cap omitted {omitted} of {len(matched)}"
                     if omitted else "")
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: "
                     f"{len(behaviours)} behaviour(s) in the authoring "
                     f"prompt ({_behaviour_breakdown(behaviours)}){extra}")
            # The cap ranks MUST-classes first, so it truncates the tail —
            # and the tail is a design choice only while IDENTITY survives
            # it. An identity behaviour dropped by the cap is the ONE loss
            # this section cannot absorb: the wrap-the-payload-and-key-the-
            # wrapper layout satisfies every pin and every format string and
            # is refuted only by an identity Then. Computed on the live
            # stable Error/vtable component (34 names, 92 matched, cap 40):
            # identity 24 all kept. A component that ever exceeds the cap in
            # identity alone needs COMPONENT_BEHAVIOUR_CAP raised, not a
            # quieter log — so say so at the point it happens.
            if (_dropped_ident := _dropped_in_class(
                    matched, behaviours, IDENTITY_CLASS)) > 0:
                deps.log(f"  [t1.5] ⚠️  {surface.cfg_cluster}/{names}: the "
                         f"behaviour cap ({COMPONENT_BEHAVIOUR_CAP}) dropped "
                         f"{_dropped_ident} IDENTITY behaviour(s) — the class "
                         f"that refutes a payload-wrapping layout. Raise "
                         f"COMPONENT_BEHAVIOUR_CAP for this component.")
        paths = _component_field_paths(comp, oracle)
        paths_note = field_path_requirements_text(paths)
        # Third note, same slot and same rationale as the two around it: the
        # member-eviction veto can only reject, and the rejection budget is
        # three. Sourced from the bodies rather than from the tests, so it says
        # what `dropped_members_in_use` will measure the reply against.
        members_note = members_in_use_text({
            i.name: members_in_use(i.definition, filled_now) for i in comp
        }) if filled_now else ""
        # Same shape as the member paths above and for the same reason: the
        # tests are the specification, the prompt does not contain them, and a
        # trait the tests reach for is otherwise left to inference. A general
        # rule was tried first and is not enough on a large component — the
        # hardcrate authors ~100 items and dropped `Default` on `Counter`
        # anyway, where the microcrate with a handful of items did not.
        # Restricted to the types this component actually declares, so the note
        # names items the reply is expected to emit.
        derives_note = _confirmed_derives_note(surface.cfg_cluster, names, comp, frozen_tests, deps)
        peer_only: dict[str, PrivateSurfaceItem] = {}
        for s in choices:
            for u, i in zip(s.uids, s.comp, strict=True):
                if u not in uids:
                    peer_only.setdefault(u, i)
        ask_comp = comp + tuple(peer_only.values())
        # The reply is keyed by the ids the union's own numbering gives; each
        # world reads it by its own. The ordinal strings must agree, and a
        # shared ordinal must be the same declaration: duplicate names are
        # numbered by position in each component, so a different order still
        # produces the same ids and would splice the block into the other item.
        renames = bool(choices) and component_uids(ask_comp) != uids + tuple(peer_only)
        disagrees = bool(choices) and not renames and not shared_item_ids_agree(
            [(uids, comp)] + [(s.uids, s.comp) for s in choices])
        if renames or disagrees:
            detail = ("the union of the worlds' items renames an item id"
                      if renames else
                      "a shared item id denotes a different item in the other world")
            deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {detail} "
                     "— asked for this world alone.")
            for s in choices:
                claim[s.world].discard(frozenset(s.uids))
            choices, ask_comp = [], comp
        label = "+".join([surface.cfg_cluster] + [s.world for s in choices])
        if choices:
            union_specs = list({b.behaviour_id: b for b in (
                list(behaviour_specs) + [b for p in peers for b in p.behaviour_specs])}.values())
            pins = list({b.behaviour_id: b for b in (
                pins + [b for s, p in _side_peers(choices, peers) for b in p.select_pins(s.comp)])
                         }.values())
            behaviours = select_component_behaviours(
                union_specs, component_names=tuple(i.name for i in ask_comp),
                public_types=_pinned_public_types(surface, comp),
                cap=None)[:COMPONENT_BEHAVIOUR_CAP]
            for s, p in _side_peers(choices, peers):
                for u, a in _public_anchors(p.surface, s.comp).items():
                    anchors.setdefault(u, a)
            paths_note = field_path_requirements_text(_component_field_paths(ask_comp, oracle))
            derives_note = _confirmed_derives_note(label, names, ask_comp, frozen_tests, deps)
            deps.log(f"  [t1.5] {label}/{names}: one ask over "
                     f"{len(ask_comp)} item(s) for {len(choices) + 1} world(s); each "
                     "attempt's reply is compiled in every world.")
        # Partly covered: the model must EXTEND the fixed definitions (adding
        # what this cluster's cfg needs), never redesign them — so it gets their
        # exact current text and an instruction to reproduce it verbatim.
        fixed = [registry[u] for u in component_uids(ask_comp) if registry and u in registry]
        shared = ("\n\nThese definitions are ALREADY FIXED for this crate by "
                  "another cfg-cluster. Reproduce each one EXACTLY as given and "
                  "design only the remaining items around them:\n"
                  + shared_definitions_text(fixed)) if fixed and adopted_none else ""

        def _prompt(chunk_items: tuple[PrivateSurfaceItem, ...],
                    chunk_uids: tuple[str, ...], scheme: str, hint: str,
                    *, _pins: list[BehaviourSpec] = pins,
                    _behaviours: list[BehaviourSpec] = behaviours,
                    _anchors: dict[str, str] = anchors,
                    _shared: str = shared, _paths: str = paths_note,
                    _derives: str = derives_note,
                    _members: str = members_note,
                    _label: str = label) -> str:
            return build_layout_authoring_prompt(
                module_name=module_name, cfg_cluster=_label,
                items=chunk_items, public_surface=public_surface,
                layout_pins=_pins, symbol_inventory=symbol_inventory,
                extra_guidance=(extra_guidance + _shared + _paths + _derives
                                + _members),
                authored_scheme=scheme,
                item_uids=chunk_uids, item_anchors=_anchors,
                behaviours=_behaviours) + hint
        return _AskShape(ask_comp, label, _prompt, list(choices), anchors)

    comps = list(surface.components)
    prefetch = _Prefetch(deps.lanes)
    planned_until = 0

    def _plan_ahead(start: int) -> int:
        """Ask ahead, in surface order from `start`, every component whose
        turn will ask: up to the first that may be adopted (whether that
        commits decides which peer components it claims, so what follows it
        is planned after its turn). Returns where planning stopped."""
        claim = {w: set(v) for w, v in claimed.items()}
        quiet = replace(deps, log=lambda _m: None)
        for j in range(start, len(comps)):
            comp = comps[j]
            if handled and handled.get(frozenset(component_uids(comp))) is not None:
                continue
            pins = select_pins(comp)
            if not pins and surface.component_is_layout_sensitive(comp):
                continue
            uids = component_uids(comp)
            if adopt_component(uids, registry or {}) is not None:
                return j
            choices = _choose_peer_sides(comp, uids, peers, claim, registry or {})
            shape = _shape(comp, tuple(i.name for i in comp), pins, uids, choices, claim,
                           adopted_none=True, deps=quiet)
            prefetch.submit(j, shape, deps)
        return len(comps)

    try:
        for idx, comp in enumerate(comps):
            if prefetch.enabled and idx >= planned_until:
                planned_until = max(idx + 1, _plan_ahead(idx))
            result = _settle(idx, comp)
            if result is not None:
                results.append(result)
    finally:
        prefetch.close(deps)
    return AuthorLayoutResult(surface.cfg_cluster, tuple(results),
                              peer_results=peer_results)


def _side_peers[S: (_PeerSide, _SideChoice)](
    sides: Sequence[S], peers: Sequence[PeerWorld],
) -> list[tuple[S, PeerWorld]]:
    by_world = {p.surface.cfg_cluster: p for p in peers}
    return [(s, by_world[s.world]) for s in sides]


def _same_text(
    a: tuple[tuple[str, AuthoredItem], ...], b: tuple[tuple[str, AuthoredItem], ...],
) -> bool:
    """True when every uid both carry has the same committed definition."""
    other = dict(b)
    return all(other[u].definition == it.definition for u, it in a if u in other)


def _choose_peer_sides(
    comp: tuple[PrivateSurfaceItem, ...],
    uids: tuple[str, ...],
    peers: Sequence[PeerWorld],
    claimed: dict[str, set[frozenset[str]]],
    registry: Mapping[str, AuthoredItem],
) -> list[_SideChoice]:
    """Each peer's component that shares the most item ids with `comp`, when
    that component would be authored in its own pass: not gated there for a
    harvest gap, and not fully covered by the registry (adoption comes first
    there, as it does here). A peer component is claimed once. Reads only the
    peers' surfaces and pins: the calibration that compiles in the peer's
    skeleton is `_calibrate_side`, at the component's turn."""
    sides: list[_SideChoice] = []
    mine = set(uids)
    for p in peers:
        world = p.surface.cfg_cluster
        best: tuple[int, tuple[PrivateSurfaceItem, ...]] | None = None
        for cand in p.surface.components:
            ids = frozenset(component_uids(cand))
            overlap = len(mine & ids)
            if overlap and ids not in claimed[world] and (best is None or overlap > best[0]):
                best = (overlap, cand)
        if best is None:
            continue
        pc = best[1]
        pu = component_uids(pc)
        ppins = p.select_pins(pc)
        if not ppins and p.surface.component_is_layout_sensitive(pc):
            continue
        if adopt_component(pu, registry) is not None:
            continue
        claimed[world].add(frozenset(pu))
        sides.append(_SideChoice(world, pc, pu))
    return sides


def _calibrate_side(choice: _SideChoice, peers: Sequence[PeerWorld]) -> _PeerSide:
    """The chosen peer component's gate, calibrated on the peer's skeleton
    as it stands at this component's turn, and its pristine files."""
    p = next(x for x in peers if x.surface.cfg_cluster == choice.world)
    pc = choice.comp
    poracle = _oracle_sources(p.deps)
    gate = LayoutGate(
        root=_calibrated_pin_probe(p.surface, pc, p.select_pins(pc), p.deps),
        in_file=_calibrated_field_path_probes(
            p.surface, pc, _component_field_paths(pc, poracle), p.deps))
    return _PeerSide(choice.world, p.deps, pc, choice.uids, gate,
                     {f: p.deps.read_skeleton(f) for f in {i.file for i in pc}})


def _restore_side(side: _PeerSide) -> None:
    """Put the peer's skeleton back to the snapshot taken before any attempt."""
    for rel, orig in side.pristine.items():
        if orig is not None:
            side.deps.write_skeleton(rel, orig)


def _rejudge_peer_on_committed_reply(
    side: _PeerSide, authored: Mapping[str, AuthoredItem], attempt: int,
    asked_in: str, learned: Callable[[str], None],
) -> None:
    """Compile the reply the asking world is committing, in a peer that was
    not judged on it.

    The peer locks on the first reply that compiles there, and later attempts
    are not compiled in it. The registry then stores the asking world's later
    reply, the peer's pass records the earlier one, and the next cluster
    start installs the registry over that peer. The recorded run
    ``42057-641e589c`` failed every attempt in both worlds and did not take
    this path. Restoring the pristine scaffold first is required: the earlier
    reply is already spliced, and a commit locates the frozen definitions.

    The candidate is the reply before this world's carry. The peer's commit
    carries it against the peer's own frozen definitions.
    """
    _restore_side(side)
    side.committed = False
    side.stopped = False
    _try_peer_side(side, authored, attempt, asked_in, learned)


def _try_peer_side(
    side: _PeerSide, authored: Mapping[str, AuthoredItem], attempt: int,
    asked_in: str, learned: Callable[[str], None],
) -> None:
    """Compile this attempt's reply in one peer world, with the same accept,
    rollback and stop rules the asking world applies to itself.

    Returns immediately once this peer has committed or stopped on a reply.
    A later reply the asking world commits is not left there:
    ``_rejudge_peer_on_committed_reply`` restores the scaffold and clears
    both flags before calling back in, and only for a peer this attempt
    did not already judge.
    """
    if side.committed or side.stopped:
        return
    names = tuple(i.name for i in side.comp)
    mine = {u: authored[u] for u in side.uids if u in authored}
    tried = _try_commit(side.comp, mine, side.deps, side.gate, side.pristine)
    side.gate = _gate_after(side.gate, tried)
    side.tried = True
    side.attempt = attempt
    if tried.committed:
        side.committed = True
        kept = dict(tried.authored)
        side.defs = authored_locate_keys(side.comp, kept)
        side.items = tuple((u, kept[u]) for u in side.uids if u in kept)
        side.reason = ""
        side.deps.log(f"  [t1.5] {side.world}/{names}: authored layout compiles "
                      f"(attempt {attempt}, the one ask with {asked_in}).")
        return
    side.reason = tried.reason
    learned(f"in the {side.world} world: {tried.reason}")
    diag = first_diagnostic(tried.errors) if tried.errors else ""
    side.deps.log(f"  [t1.5] {side.world}/{names}: authored scaffold did not "
                  f"compile (attempt {attempt}, the one ask with {asked_in}) — "
                  f"reverted" + (f": {diag}" if diag else f": {tried.reason}"))
    if (not tried.errors and is_mechanical_reason(tried.reason)) or (
            diag and diag in side.seen_diags):
        side.stopped = True
    elif diag:
        side.seen_diags.append(diag)


def _confirmed_derives_note(
    cfg_cluster: str, names: tuple[str, ...], comp: tuple[PrivateSurfaceItem, ...],
    frozen_tests: Mapping[str, str], deps: AuthorLayoutDeps,
) -> str:
    """The derive requirements the scan reads off the tests AND the declaration
    each item replaces derives (`confirmed_by_declaration`). A row the
    declaration refutes is not asked, and the log names it."""
    derivable = [i for i in comp if i.kind in _DERIVABLE_KINDS]
    rows = scan_required_derives(frozen_tests, [i.name for i in derivable])
    declared: dict[str, set[str]] = {}
    for i in derivable:
        declared.setdefault(i.name, set()).update(derive_traits(i.definition))
    kept, dropped = confirmed_by_declaration(rows, declared)
    if dropped:
        deps.log(f"  [t1.5] {cfg_cluster}/{names}: derive requirement(s) not asked — "
                 "the declaration being replaced does not derive them: "
                 + ", ".join(f"{r.type_name}: {r.trait} ({r.origin})" for r in dropped))
    return derive_requirements_text(kept)


def _gate_after(gate: LayoutGate, tried: CommitAttempt) -> LayoutGate:
    """The gate the NEXT compile (and the success line) may use."""
    return gate.without_member_paths() if tried.probe_dropped else gate


def _verified_note(gate: LayoutGate) -> str:
    """What the accepted layout was actually held to, for the success line. The
    two are counted apart because they answer different questions: a pin is the
    crate's own measured layout, a member path is a name a write-protected test
    reads."""
    verified = []
    if gate.root.gated:
        verified.append(f"{len(gate.root.gated)} layout pin(s) verified")
    paths = len(gate.gated) - len(gate.root.gated)
    if paths:
        verified.append(f"{paths} member path(s) verified")
    return f" [{', '.join(verified)}]" if verified else ""


def _install_field_probes(
    deps: AuthorLayoutDeps, in_file: tuple[tuple[str, LayoutProbe], ...],
) -> None:
    """Write each member-path probe into the file that DECLARES its root type,
    as its own marker region (so the pin probe's region in the same file is left
    alone). A file the caller cannot read is skipped, not created: the probe is
    a gate, and a gate that invents a source file is a defect of its own."""
    for rel, probe in in_file:
        if not probe.checks_anything:
            continue
        current = deps.read_skeleton(rel)
        if current is None:
            continue
        deps.write_skeleton(
            rel, install_probe_region(current, probe.source, FIELD_PROBE_MARKERS))


def _strip_field_probes(
    deps: AuthorLayoutDeps, in_file: tuple[tuple[str, LayoutProbe], ...],
) -> None:
    """Remove every member-path probe region. Runs after the compile on BOTH
    paths — a committed layout must not carry the probe into t2a, and a rejected
    one is restored from `pristine` anyway."""
    for rel, probe in in_file:
        if not probe.checks_anything:
            continue
        current = deps.read_skeleton(rel)
        if current is None:
            continue
        deps.write_skeleton(rel, strip_probe_region(current, FIELD_PROBE_MARKERS))


@dataclass(frozen=True, slots=True)
class CommitAttempt:
    """What one candidate layout's compile decided, and what decided it.

    `errors` is empty for a splice/coverage rejection — the caller needs that
    apart from a compile failure, since nothing was written and there is no
    diagnostic to name.

    `probe_dropped` says the member-path probe was removed and the candidate
    re-decided without it. Two callers need it and neither can infer it: the
    success line would otherwise print "N member path(s) verified" about a
    compile those paths took no part in, and the attempt loop would reinstall a
    probe already proven unusable — paying another `compile_check` to reach the
    same verdict.
    """

    committed: bool
    reason: str = ""
    errors: str = ""
    probe_dropped: bool = False
    #: The candidate with the crate's frozen facts carried
    #: (`carry_frozen_facts`): the text that was spliced. A committed
    #: layout is stored from this, never from the candidate the caller
    #: passed in, so the registry, the locate keys and the skeleton hold one
    #: text.
    authored: Mapping[str, AuthoredItem] = field(default_factory=dict)


def _try_commit(
    items: tuple[PrivateSurfaceItem, ...], authored: dict[str, AuthoredItem],
    deps: AuthorLayoutDeps, gate: LayoutGate, pristine: dict[str, str | None],
) -> CommitAttempt:
    """Splice one candidate layout into the skeleton and KEEP it only if the
    rewritten scaffold still compiles with the component's layout pins probed.
    Any failure rolls the touched files back to `pristine`.

    One helper for BOTH candidate sources (the model's reply and a layout
    adopted from an earlier cfg-cluster): a second copy of the accept/rollback
    sequence is how one path ends up skipping the pin probe or leaking a failed
    candidate into the tree.
    """
    authored = carry_frozen_facts(items, authored)
    edited, splice_err = _splice_component(items, authored, deps)
    if splice_err:
        return CommitAttempt(False, splice_err, authored=authored)
    for rel, txt in edited.items():
        deps.write_skeleton(rel, txt)
    # The probes ride along with THIS compile: a layout that no longer satisfies
    # `size_of::<Error>() == size_of::<usize>()`, or that renames a member the
    # crate's write-protected tests read, must be rejected here — not discovered
    # a whole sweep later as repr failures blamed on individual fn bodies, or at
    # the first `cargo test` as a test file no stage may edit.
    if gate.root.checks_anything and deps.write_pin_probe is not None:
        deps.write_pin_probe(gate.root.source)
    _install_field_probes(deps, gate.in_file)
    compiled, errors = deps.compile_check()
    if deps.write_pin_probe is not None:
        deps.write_pin_probe(None)
    _strip_field_probes(deps, gate.in_file)
    probe_dropped = False
    # `checks_anything`, not `in_file`: a gate whose member paths were already
    # demoted keeps its entries (they carry what the log must still name), so
    # testing the tuple would re-run this on every later attempt of the same
    # component — a second `compile_check` and a line blaming a probe that was
    # not installed, which is both of the things the demotion exists to stop.
    if (not compiled and any(p.checks_anything for _, p in gate.in_file)
            and set(rustc_error_codes(errors, "E")) == {"E0616"}):
        # The member-path probe reached a hop whose OWN module keeps it private:
        # `err.inner.ptr` needs `ptr` visible where the probe sits, and only the
        # crate's frozen source decides that. Calibration cannot see this coming
        # — on the frozen scaffold the root's fields are elided, so rustc stops
        # at the first hop with E0609 and the deeper hop is never resolved. The
        # probe is wrong about ITSELF, and a probe that no candidate could ever
        # satisfy would freeze a component whose layout was fine, so the
        # candidate is re-decided without it (the region is already stripped).
        probe_dropped = True
        compiled, errors = deps.compile_check()
        deps.log("  [t1.5] the member-path probe reads through a member that is "
                 "private to another module (E0616) — that is the probe's own "
                 "reach, not a layout violation; this candidate was decided "
                 f"without it ({'compiles' if compiled else 'still refused'}).")
    if compiled:
        return CommitAttempt(True, probe_dropped=probe_dropped,
                             authored=authored)
    for rel, orig in pristine.items():
        if orig is not None:
            deps.write_skeleton(rel, orig)
    return CommitAttempt(
        False,
        ("authored scaffold did not compile:\n" + errors
         if errors else "authored scaffold did not compile"),
        errors, probe_dropped, authored)


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


def _pinned_public_types(
    surface: PrivateSurface, comp: tuple[PrivateSurfaceItem, ...],
) -> tuple[str, ...]:
    """The frozen PUBLIC types whose layout reaches this component — the
    types the layout probe pins and the behaviour selection matches on, so
    both read one definition of "reaches"."""
    return tuple(sorted(
        name for name in surface.public_direct_refs
        if surface.reachable_from_public(name) & {i.name for i in comp}))


def _behaviour_breakdown(behaviours: Sequence[BehaviourSpec]) -> str:
    """`identity 1, exact_format 2` — per-class counts for the log line, in
    prompt order (the selection is already MUST-classes first)."""
    counts: dict[str, int] = {}
    for b in behaviours:
        k = behaviour_class_name(b).lower()
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{k} {n}" for k, n in counts.items()) or "none"


def _dropped_in_class(
    matched: Sequence[BehaviourSpec], kept: Sequence[BehaviourSpec],
    class_name: str,
) -> int:
    """How many specs of `class_name` the cap removed. 0 when the class
    survived whole, which is the expected reading."""
    n_m = sum(1 for b in matched if behaviour_class_name(b) == class_name)
    n_k = sum(1 for b in kept if behaviour_class_name(b) == class_name)
    return max(0, n_m - n_k)


def _omitted_behaviour_classes(
    matched: Sequence[BehaviourSpec], kept: Sequence[BehaviourSpec],
) -> str:
    """`exact_format 35, bounded_resource 4, other 13` — classes present in
    the uncapped match and absent from the capped prompt, in MUST order.

    The [t1.5] line used to print only the kept breakdown, so a cap of 40
    on the live Error/vtable component (IDENTITY 24, EXACT_FORMAT 51,
    BOUNDED_RESOURCE 4, OTHER 13) logged `identity 24, exact_format 16`
    and hid that the whole 40-byte-buffer / whitespace-fallback class
    was cut. The prompt already says `(+N more)`; this is the operator
    line that names WHICH classes those N were."""
    m: dict[str, int] = {}
    k: dict[str, int] = {}
    for b in matched:
        n = behaviour_class_name(b)
        m[n] = m.get(n, 0) + 1
    for b in kept:
        n = behaviour_class_name(b)
        k[n] = k.get(n, 0) + 1
    parts = [f"{n.lower()} {m[n] - k.get(n, 0)}"
             for n in sorted(m, key=class_rank)
             if m[n] - k.get(n, 0) > 0]
    return ", ".join(parts)


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
    probe = build_layout_probe(
        pins, pinned_types=_pinned_public_types(surface, comp))
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


def _frozen_test_sources(deps: AuthorLayoutDeps) -> Mapping[str, str]:
    """The wider write-protected test set, or {} to fall back to the oracle.

    Degrades exactly like `_oracle_sources`: a read failure must never fail
    authoring — the requirement is an aid to attempt 1, not a gate."""
    if deps.frozen_test_sources is None:
        return {}
    try:
        return deps.frozen_test_sources()
    except Exception as e:      # noqa: BLE001 — degrade, never fail authoring
        deps.log(f"  [t1.5] could not read the crate's frozen tests for derive "
                 f"requirements ({e}) — authoring without them.")
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


def _calibrated_field_path_probes(
    surface: PrivateSurface,
    comp: tuple[PrivateSurfaceItem, ...],
    paths: tuple[FieldPath, ...],
    deps: AuthorLayoutDeps,
) -> tuple[tuple[str, LayoutProbe], ...]:
    """The oracle's member paths as a compile-time gate: one probe per DECLARING
    file, calibrated together in one compile.

    WHERE. A member-path probe borrows a PRIVATE field, and a private field is
    visible only inside its own module and that module's descendants. The probe
    therefore goes in the file that declares the root type, exactly like the
    oracle's own tests, which can read the field only because they are generated
    as a child module of that file (see :class:`LayoutGate`).

    CALIBRATION. The pin probe is calibrated by requiring it to HOLD on the
    frozen scaffold — the crate's own layout satisfies its own pins by
    construction. A member path cannot answer to that authority: the freeze
    DELETED the fields it names, so `no field 'inner' on type 'Error'` is the
    expected reading of a CORRECT probe against an elided struct, and demanding
    it hold would disable the gate exactly where it is needed. The rule is
    therefore by diagnostic:

      compiles                  → the path holds on the frozen layout; gate it
                                  (an authored layout may not take it away).
      fails, ONLY E0609         → the fields were elided; that is what this
                                  component is authoring. Gate it.
      anything else             → E0412 (root not in scope in its own file),
                                  E0107, a parse error: the probe is wrong ABOUT
                                  ITSELF. Drop it, say so.

    A dropped probe costs nothing but the gate; a probe kept on a misrendered path
    would freeze a component whose layout was fine, which is why the fall-through
    is "drop, loudly" rather than "gate anyway". The one case calibration cannot
    reach — a deeper hop that is private to a THIRD module, invisible while the
    root's own fields are still elided — is caught after the fact in
    `_try_commit`, on the same principle.

    The only precondition is having paths. These probes are placed with
    `read_skeleton`/`write_skeleton`, which every caller supplies; gating them on
    the crate-root pin port as well (they used to ride in it) would drop the one
    check standing between an authored layout and a write-protected test that
    cannot compile — silently, for any caller that happens not to install pins."""
    if not paths:
        return ()
    by_name = {i.name: i for i in comp}
    generic_roots = frozenset(
        i.name for i in comp
        if (h := parse_type_header(i.definition)) is not None and h.generics)
    grouped: dict[str, list[FieldPath]] = {}
    for p in paths:
        grouped.setdefault(by_name[p.root_type].file, []).append(p)
    probes = tuple(
        (rel, build_field_path_probe(tuple(ps), generic_roots=generic_roots))
        for rel, ps in sorted(grouped.items()))
    live = tuple((rel, pr) for rel, pr in probes if pr.checks_anything)
    skipped = tuple(e for _, pr in probes for e in pr.skipped)
    names = tuple(i.name for i in comp)
    if skipped:
        deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: {len(skipped)} oracle "
                 "member path(s) NOT compile-gated — a generic root would force "
                 "the probe to invent its type parameters, and a path with no "
                 f"member names nothing to borrow: {list(skipped)}")
    if not live:
        return probes
    _install_field_probes(deps, live)
    holds, errors = deps.compile_check()
    _strip_field_probes(deps, live)
    codes = set(rustc_error_codes(errors, "E"))
    gated = tuple(e for _, pr in live for e in pr.gated)
    if holds or codes == {"E0609"}:
        deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: gating on "
                 f"{len(gated)} member path(s) the crate's own write-protected "
                 f"tests read: {list(gated)} (probed in "
                 f"{[rel for rel, _ in live]})")
        return probes
    deps.log(f"  [t1.5] {surface.cfg_cluster}/{names}: member-path probe is "
             "wrong about itself on the FROZEN scaffold (not a layout "
             f"violation) — path gating disabled for this component. "
             f"codes={sorted(codes)} {errors.strip()[:300]}")
    return tuple((rel, LayoutProbe(skipped=pr.gated + pr.skipped))
                 for rel, pr in probes)


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
            # ADD the coverage miss to whatever the outer attempt already
            # learned — overwriting `attempt_hint` here used to throw away the
            # accumulated violation checklist for the rest of the chunk loop.
            hint = attempt_hint + _retry_hint(
                (f"missing authored items: {', '.join(sorted(missing))}",))
        if missing:
            return None, f"missing authored items: {', '.join(sorted(missing))}"
        scheme_parts.extend(
            authored[u].definition for u in chunk_uids)
    return authored, ""


def _retry_hint(reasons: Sequence[str]) -> str:
    """The retry hint is CUMULATIVE. Carrying only the last rejection let six
    authoring attempts across two fires (2026-08-09/10, the 34-name `Error`
    component) cycle renamed struct -> pub field -> dropped member and back,
    each reply fixing the named violation while recommitting an earlier one."""
    lines = "\n".join(f"- {r}" for r in reasons)
    return ("\n\n=== EVERY PREVIOUS ATTEMPT FAILED — CUMULATIVE CHECKLIST ===\n"
            "Each line below is a constraint one of your earlier replies "
            "violated. Your next reply must satisfy ALL of them at once — "
            "fixing one by re-breaking another restarts the list:\n"
            f"{lines}\n"
            "Fix ALL of the above and re-emit ALL <LAYOUT_ITEM> blocks.\n")
