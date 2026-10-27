"""Pins for the t1.5 layout-authoring PHASE (orchestration)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from pipeline_core.application.author_layout import AuthorLayoutDeps
from pipeline_core.application.author_layout_phase import (
    LayoutPhaseDeps,
    make_pin_selector,
    run_layout_phase,
)
from pipeline_core.domain.authored_layout import (
    component_uids,
    item_slot,
    shared_item_ids_agree,
)
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
    classify_private_surface,
)
from pipeline_core.domain.type_header import declaration_text


def _surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


def _suite_with_pin():
    return BehaviourSuite(cfg_cluster="stable", specs=(
        BehaviourSpec(
            behaviour_id="static::repr::0", setup="", when="size_of",
            then=ObservableOutcome.static_assertion(
                StaticAssertKind.SIZE,
                "assert_eq!(size_of::<Error>(), size_of::<ErrorImpl>())"),
            subkind=BehaviourSubKind.OUTCOME),
    ))


def test_pin_selector_matches_component_by_type_name():
    surface = _surface()
    select = make_pin_selector(_suite_with_pin(), surface)
    comp = surface.components[0]
    assert select(comp), "the ErrorImpl component should receive the size pin"


def test_pin_selector_empty_for_unrelated_suite():
    surface = _surface()
    select = make_pin_selector(BehaviourSuite(cfg_cluster="stable"), surface)
    assert select(surface.components[0]) == []


def _suite_pinning_public_type_only():
    # the real anyhow shape: `size_of::<Error>()` names ONLY the public type,
    # never the private `ErrorImpl` it feeds.
    return BehaviourSuite(cfg_cluster="stable", specs=(
        BehaviourSpec(
            behaviour_id="static::test_repr.rs::0", setup="", when="size_of",
            then=ObservableOutcome.static_assertion(
                StaticAssertKind.SIZE,
                "assert_eq!(mem::size_of::<Error>(), mem::size_of::<usize>());"),
            subkind=BehaviourSubKind.OUTCOME),
    ))


def test_pin_selector_matches_via_public_type_reachability():
    surface = _surface()
    select = make_pin_selector(_suite_pinning_public_type_only(), surface)
    comp = surface.components[0]
    assert select(comp), (
        "a size_of pin naming only the PUBLIC type must still reach the "
        "private component that public type's definition references")


def test_pin_selector_does_not_leak_across_unrelated_public_types():
    # a second public type with its OWN unrelated private component: a pin on
    # `Other` must not feed `ErrorImpl`'s component.
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
        TypeRecord(file="src/lib.rs", kind="struct", name="Other", is_pub=True,
                   definition="pub struct Other { inner: Box<OtherImpl> }"),
        TypeRecord(file="src/other.rs", kind="struct", name="OtherImpl", is_pub=False,
                   definition="struct OtherImpl { x: u8 }"),
    ]
    surface = classify_private_surface(types, [], cfg_cluster="stable")
    suite = BehaviourSuite(cfg_cluster="stable", specs=(
        BehaviourSpec(
            behaviour_id="static::test_other.rs::0", setup="", when="size_of",
            then=ObservableOutcome.static_assertion(
                StaticAssertKind.SIZE,
                "assert_eq!(mem::size_of::<Other>(), mem::size_of::<usize>());"),
            subkind=BehaviourSubKind.OUTCOME),
    ))
    select = make_pin_selector(suite, surface)
    error_impl_comp = next(c for c in surface.components
                            if any(i.name == "ErrorImpl" for i in c))
    assert select(error_impl_comp) == [], (
        "a pin on `Other` must not leak onto ErrorImpl's unrelated component")


def test_autotrait_pin_harvested_from_a_test_file_reaches_the_component():
    # The whole chain, from the bytes a crate's test file actually holds:
    # harvest → selector → component. The generic-helper idiom puts the bound
    # on one line and the type on the next, and a pin read from the first line
    # alone carries `Send` and no subject — it matches no component's items and
    # no frozen-public name, so it routes nowhere and gates nothing.
    #
    # On anyhow 2026-08-06 that cost the whole run: all four autotrait pins
    # selected into zero components, t1.5 authored an `Error` that is neither
    # Send nor Sync (it satisfies the size pins, which DID route), and every
    # test binary in both clusters failed to build on E0277 — with the frontier
    # then narrowed to the single fn the compiler implicated and task2a filling
    # 0 of 118 stubs before it declared the cluster exhausted.
    from pipeline_core.application.extract_behaviours import (
        harvest_behaviour_suite,
    )
    test_src = (
        "#[test]\n"
        "fn test_send() {\n"
        "    fn assert_send<T: Send>() {}\n"
        "    assert_send::<Error>();\n"
        "}\n"
    )
    surface = _surface()
    suite = harvest_behaviour_suite(
        (), cfg_cluster="stable",
        test_sources=[("tests/test_autotrait.rs", test_src)])
    select = make_pin_selector(suite, surface)
    comp = next(c for c in surface.components
                if any(i.name == "ErrorImpl" for i in c))
    assert select(comp), (
        "an autotrait pin applied to the public `Error` must reach the private "
        "component that `Error` references")


class _Recorder:
    def __init__(self, suite, reply, compiles):
        self.suite = suite
        self.reply = reply
        self.compiles = compiles
        self.files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}
        self.logs: list[str] = []
        self.recomputed: list[str] = []
        self.prompts: list[str] = []

    def author_deps_for(self, project_dir, cluster):
        def run_model(p):
            self.prompts.append(p)
            return self.reply
        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: self.files.get(rel),
            write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
            compile_check=lambda: (self.compiles, "" if self.compiles
                                   else "error[E0308]: mismatched types"),
            log=self.logs.append, max_attempts=1)

    def deps(self):
        return LayoutPhaseDeps(
            log=self.logs.append,
            load_surface=lambda pd, c: _surface(),
            load_suite=lambda pd, c: self.suite,
            public_surface_text=lambda pd, c: "pub struct Error;",
            symbol_inventory_text=lambda pd, c: "Box",
            author_deps_for=self.author_deps_for,
            recompute_leaf_order=lambda pd, c: self.recomputed.append(c))


_GOOD = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
         'struct ErrorImpl { inner: Box<dyn StdError> }</LAYOUT_ITEM>')


def test_phase_authors_and_recomputes_leaf_order():
    r = _Recorder(_suite_with_pin(), _GOOD, True)
    out = run_layout_phase(Path("/x"), "anyhow", ["stable"], r.deps())
    assert out["stable"].all_authored
    assert r.recomputed == ["stable"], "leaf order recomputed over rewritten scaffold"
    assert "Box<dyn StdError>" in r.files["src/error.rs"]


def test_phase_gates_when_no_pin_and_skips_recompute():
    # layout-sensitive component + empty suite → gated, scaffold untouched, no recompute.
    r = _Recorder(BehaviourSuite(cfg_cluster="stable"), _GOOD, True)
    out = run_layout_phase(Path("/x"), "anyhow", ["stable"], r.deps())
    assert out["stable"].gated
    assert r.recomputed == []
    assert r.prompts == []
    assert r.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"


def test_phase_noop_on_empty_surface():
    r = _Recorder(_suite_with_pin(), _GOOD, True)
    deps = r.deps()
    deps = LayoutPhaseDeps(
        log=r.logs.append,
        load_surface=lambda pd, c: classify_private_surface([], [], cfg_cluster=c),
        load_suite=deps.load_suite, public_surface_text=deps.public_surface_text,
        symbol_inventory_text=deps.symbol_inventory_text,
        author_deps_for=deps.author_deps_for, recompute_leaf_order=deps.recompute_leaf_order)
    out = run_layout_phase(Path("/x"), "anyhow", ["stable"], deps)
    assert out == {}
    assert r.prompts == []


def test_phase_warns_when_the_baked_freeze_scope_differs_from_the_run():
    """The scope is baked into the surface AND into the elided API artefact, on
    live source that is gone by t1.5 — so a config change can only be applied by
    re-baking, and must be said out loud rather than half-applied."""
    r = _Recorder(_suite_with_pin(), _GOOD, True)
    deps = dataclasses.replace(r.deps(), author_private_fields=True)
    run_layout_phase(Path("/x"), "anyhow", ["stable"], deps)
    warning = next(m for m in r.logs if "WARNING" in m)
    assert "author_private_fields=False" in warning
    assert "restart.sh --y" in warning


def test_phase_silent_when_the_scope_matches():
    r = _Recorder(_suite_with_pin(), _GOOD, True)
    run_layout_phase(Path("/x"), "anyhow", ["stable"], r.deps())
    assert not [m for m in r.logs if "WARNING" in m]


def test_registry_carries_one_layout_across_clusters(tmp_path):
    """The SECOND cluster must adopt the first's definitions, not author its
    own — a crate has one definition per private type, and the second cluster
    is seeded with the first's filled bodies."""
    from pipeline_core.application.author_layout_phase import (
        LayoutPhaseDeps,
        run_layout_phase,
    )
    from pipeline_core.domain.authored_layout import AuthoredItem

    prompts: list[str] = []
    saved: dict[str, dict[str, AuthoredItem]] = {}
    files = {"stable": {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"},
             "nightly": {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}}
    reply = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
             'struct ErrorImpl { inner: Box<dyn StdError> }</LAYOUT_ITEM>')

    def _author_deps_for(_pd, cluster):
        def run_model(p):
            prompts.append(p)
            return reply
        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: files[cluster].get(rel),
            write_skeleton=lambda rel, txt: files[cluster].__setitem__(rel, txt),
            compile_check=lambda: (True, ""), log=lambda _m: None)

    deps = LayoutPhaseDeps(
        log=lambda _m: None,
        load_surface=lambda _pd, c: dataclasses.replace(_surface(), cfg_cluster=c),
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_author_deps_for,
        recompute_leaf_order=lambda _pd, _c: None,
        save_registry=lambda pd, reg: saved.setdefault("reg", dict(reg)) and pd)
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)

    assert len(prompts) == 1                       # nightly never called the model
    assert res["nightly"].components[0].adopted
    assert files["nightly"]["src/error.rs"] == files["stable"]["src/error.rs"]
    assert saved["reg"]["ErrorImpl"].definition.startswith("struct ErrorImpl {")


def test_a_registry_definition_the_gate_refutes_is_replaced_not_kept(tmp_path):
    """A stored definition that fails the compile+pin gate on the cluster that
    tried it is a measured wrong answer, and the layout the cluster authors in
    its place must take its slot in the registry.

    Keeping it is what `setdefault` did, and on `anyhow` 2026-08-06 it cost a
    whole run. The registry restored from the `task0c-augmented-anyhow` tag held
    `pub struct Error { inner: core::ptr::NonNull<ErrorImpl> }` — authored before
    autotrait pins routed to a component, so nothing had ever gated it on
    `Error: Send`. Both clusters adopted it, both were refused with `error[E0277]:
    NonNull<ErrorImpl> cannot be sent between threads safely`, and both authored a
    correct replacement verified against all 6 pins. The refuted text survived in
    the registry anyway, and `reconcile_layout` — where the registry outranks a
    per-cluster record — reinstalled it over the tree: `pass=0 fail=112`, the
    frontier narrowed to the single fn the diagnostics implicated, 0 of 118 stubs
    filled in 70 minutes. No fill can repair it; the defect is a declaration."""
    from pipeline_core.application.author_layout_phase import (
        LayoutPhaseDeps,
        run_layout_phase,
    )
    from pipeline_core.domain.authored_layout import AuthoredItem

    refuted = "struct ErrorImpl { stale: u8 }"
    authored = "struct ErrorImpl { inner: Box<dyn StdError> }"
    files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}
    saved: dict[str, dict[str, AuthoredItem]] = {}

    def _author_deps_for(_pd, _cluster):
        return AuthorLayoutDeps(
            run_model=lambda _p: (f'<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                                  f'{authored}</LAYOUT_ITEM>'),
            read_skeleton=lambda rel: files.get(rel),
            write_skeleton=lambda rel, txt: files.__setitem__(rel, txt),
            # the gate's verdict, standing in for the pin probe: the stored
            # definition does not hold on this cluster, the authored one does.
            compile_check=lambda: (
                (False, "error[E0277]: cannot be sent between threads safely")
                if refuted in files["src/error.rs"] else (True, "")),
            log=lambda _m: None)

    deps = LayoutPhaseDeps(
        log=lambda _m: None,
        load_surface=lambda _pd, c: dataclasses.replace(_surface(), cfg_cluster=c),
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_author_deps_for,
        recompute_leaf_order=lambda _pd, _c: None,
        load_registry=lambda _pd: {
            "ErrorImpl": AuthoredItem("ErrorImpl", "struct", refuted)},
        save_registry=lambda pd, reg: saved.__setitem__("reg", dict(reg)) or pd)
    res = run_layout_phase(tmp_path, "anyhow", ["stable"], deps)

    comp = res["stable"].components[0]
    assert comp.authored and not comp.adopted
    assert comp.refuted_uids == ("ErrorImpl",), "the gate refused the stored definition"
    assert saved["reg"]["ErrorImpl"].definition == authored, (
        "a definition the gate disproved must not survive in the registry — "
        "reconcile_layout would reinstall it over the tree that just rejected it")


def test_a_refuted_definition_is_dropped_even_when_re_authoring_fails(tmp_path):
    """The disproved definition goes whether or not a replacement arrives.

    Replacing it only on success leaves the whole defect intact on the path that
    reaches it most often: the registry write iterates the items a component
    COMMITTED, and a component that refuted its adoption and then burned all
    three authoring attempts commits nothing. The stored text would survive
    untouched, and `reconcile_layout` installs the registry over the live tree —
    so the cluster that proved the layout wrong gets it written back. Not a
    hypothetical path on `anyhow`: the same `Error`/`ErrorImpl` component that
    refuted its adoption on 2026-08-06 also exhausted 3/3 attempts on
    `could not locate a frozen definition to replace: Error in lib.rs` in the
    same log, and its first attempt after the refutation was rejected for
    marking a field `pub`.

    An empty slot is the honest state: the next cluster authors its own layout
    instead of adopting a measured wrong answer, and a cluster that verified the
    old definition still holds it in its own per-cluster record."""
    from pipeline_core.application.author_layout_phase import (
        LayoutPhaseDeps,
        run_layout_phase,
    )
    from pipeline_core.domain.authored_layout import AuthoredItem

    refuted = "struct ErrorImpl { stale: u8 }"
    files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}
    saved: dict[str, dict[str, AuthoredItem]] = {}

    def _author_deps_for(_pd, _cluster):
        return AuthorLayoutDeps(
            run_model=lambda _p: "the model answers with nothing usable",
            read_skeleton=lambda rel: files.get(rel),
            write_skeleton=lambda rel, txt: files.__setitem__(rel, txt),
            compile_check=lambda: (
                (False, "error[E0277]: cannot be sent between threads safely")
                if refuted in files["src/error.rs"] else (True, "")),
            log=lambda _m: None)

    deps = LayoutPhaseDeps(
        log=lambda _m: None,
        load_surface=lambda _pd, c: dataclasses.replace(_surface(), cfg_cluster=c),
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_author_deps_for,
        recompute_leaf_order=lambda _pd, _c: None,
        load_registry=lambda _pd: {
            "ErrorImpl": AuthoredItem("ErrorImpl", "struct", refuted)},
        save_registry=lambda pd, reg: saved.__setitem__("reg", dict(reg)) or pd)
    res = run_layout_phase(tmp_path, "anyhow", ["stable"], deps)

    comp = res["stable"].components[0]
    assert not comp.authored and comp.refuted_uids == ("ErrorImpl",)
    assert "ErrorImpl" not in saved["reg"], (
        "the gate disproved this definition and nothing replaced it — leaving it "
        "in the registry hands reconcile_layout the text the tree just rejected")


def test_an_adoption_that_never_compiled_does_not_unseat_the_registry(tmp_path):
    """Only a COMPILED refusal disproves a stored definition.

    `_try_commit` reports two different failures through one return: rustc said
    no, and the splice could not install the candidate at all (a frozen def the
    locator cannot find, a public-contract rejection, a member eviction). The
    second produces no diagnostic and no compile — it is a statement about this
    skeleton, not about the layout — and treating it as a refutation would let a
    cluster overwrite the crate's definition with a fresh design on no evidence,
    which is the two-layouts-for-one-uid state the registry exists to prevent.

    Here the stored definition drops a field the crate's filled bodies still
    read, so the eviction veto rejects the splice before any compile: the
    cluster authors its own layout for its own tree, and the crate-wide
    definition stands."""
    from pipeline_core.application.author_layout_phase import (
        LayoutPhaseDeps,
        run_layout_phase,
    )
    from pipeline_core.domain.authored_layout import AuthoredItem

    stored = "struct ErrorImpl { data: u8 }"          # drops `vtable`
    authored = "struct ErrorImpl { vtable: u8, data: Box<dyn StdError> }"
    frozen = "struct ErrorImpl { vtable: u8, data: u8 }"
    files = {"src/error.rs": frozen + "\n"}
    saved: dict[str, dict[str, AuthoredItem]] = {}

    def _surface_two_fields():
        types = [
            TypeRecord(file="src/lib.rs", kind="struct", name="Error",
                       is_pub=True,
                       definition="pub struct Error { inner: Box<ErrorImpl> }"),
            TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl",
                       is_pub=False, definition=frozen),
        ]
        return classify_private_surface(types, [], cfg_cluster="stable")

    def _author_deps_for(_pd, _cluster):
        return AuthorLayoutDeps(
            run_model=lambda _p: (f'<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                                  f'{authored}</LAYOUT_ITEM>'),
            read_skeleton=lambda rel: files.get(rel),
            write_skeleton=lambda rel, txt: files.__setitem__(rel, txt),
            compile_check=lambda: (True, ""),
            # a filled body reads the field the stored definition drops
            filled_bodies=lambda: "fn vtable(&self) -> u8 { self.vtable }",
            log=lambda _m: None)

    deps = LayoutPhaseDeps(
        log=lambda _m: None,
        load_surface=lambda _pd, c: dataclasses.replace(
            _surface_two_fields(), cfg_cluster=c),
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_author_deps_for,
        recompute_leaf_order=lambda _pd, _c: None,
        load_registry=lambda _pd: {
            "ErrorImpl": AuthoredItem("ErrorImpl", "struct", stored)},
        save_registry=lambda pd, reg: saved.__setitem__("reg", dict(reg)) or pd)
    res = run_layout_phase(tmp_path, "anyhow", ["stable"], deps)

    comp = res["stable"].components[0]
    assert comp.authored and not comp.adopted
    assert comp.refuted_uids == (), (
        "nothing compiled, so nothing was disproved — this must not read as a "
        "refutation")
    assert saved["reg"]["ErrorImpl"].definition == stored, (
        "a definition that was never compiled here keeps its slot; replacing it "
        "gives the crate two layouts for one uid, which is what the registry "
        "exists to prevent")


# --- one ask over the worlds that author a component -------------------------

def _world_surface(world: str, extra: str):
    """`Error` → `ErrorImpl` → a world-specific helper type. The two worlds'
    components share `ErrorImpl` and differ by one item, the shape of run
    `42057-641e589c` (stable and nightly's 34/33-item component differed by
    three fns)."""
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition=f"struct ErrorImpl {{ h: {extra} }}"),
        TypeRecord(file="src/error.rs", kind="struct", name=extra, is_pub=False,
                   definition=f"struct {extra} {{ x: u8 }}"),
    ]
    return classify_private_surface(types, [], cfg_cluster=world)


_UNION_REPLY = (
    '<LAYOUT_ITEM name="ErrorImpl" kind="struct">struct ErrorImpl { h: Box<u8> }</LAYOUT_ITEM>'
    '<LAYOUT_ITEM name="Helper" kind="struct">struct Helper { x: u16 }</LAYOUT_ITEM>'
    '<LAYOUT_ITEM name="NightlyOnly" kind="struct">struct NightlyOnly { y: u16 }</LAYOUT_ITEM>')


def _two_world_phase(compiles: dict[str, bool], *, attempts: int = 3):
    prompts: list[str] = []
    logs: list[str] = []
    files = {"stable": {"src/error.rs": "struct ErrorImpl { h: Helper }\nstruct Helper { x: u8 }\n"},
             "nightly": {"src/error.rs": "struct ErrorImpl { h: NightlyOnly }\n"
                                         "struct NightlyOnly { x: u8 }\n"}}

    refusals = {"stable": 0, "nightly": 0}

    def _compile(cluster):
        if compiles[cluster]:
            return True, ""
        # A different diagnostic each time, so the repeated-diagnostic stop
        # does not end the loop early and every attempt is spent.
        refusals[cluster] += 1
        return False, f"error[E0277]: refusal {refusals[cluster]} in {cluster}"

    def _author_deps_for(_pd, cluster):
        def run_model(p):
            prompts.append(p)
            return _UNION_REPLY
        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: files[cluster].get(rel),
            write_skeleton=lambda rel, txt: files[cluster].__setitem__(rel, txt),
            compile_check=lambda: _compile(cluster),
            log=logs.append, max_attempts=attempts)

    deps = LayoutPhaseDeps(
        log=logs.append,
        load_surface=lambda _pd, c: _world_surface(c, "Helper" if c == "stable" else "NightlyOnly"),
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_author_deps_for,
        recompute_leaf_order=lambda _pd, _c: None)
    return deps, prompts, logs, files


def test_a_component_no_world_compiles_is_asked_once_not_once_per_world(tmp_path):
    deps, prompts, logs, files = _two_world_phase({"stable": False, "nightly": False})
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert len(prompts) == 3, "three attempts in all, not three per world"
    assert "Cluster: stable+nightly" in prompts[0] or "stable+nightly" in prompts[0]
    assert "NightlyOnly" in prompts[0] and "Helper" in prompts[0]
    night = res["nightly"].components[0]
    assert not night.authored and night.asked_with == "stable"
    assert any("not asked again" in m for m in logs)
    assert "NightlyOnly { x: u8 }" in files["nightly"]["src/error.rs"], "rolled back"


def test_negative_control_without_peers_each_world_asks_again(tmp_path, monkeypatch):
    """The pre-change phase, with no peers: the second world asks the same
    question from scratch."""
    import pipeline_core.application.author_layout_phase as phase

    real = phase.author_layout
    monkeypatch.setattr(phase, "author_layout",
                        lambda *a, **k: real(*a, **{**k, "peers": (), "handled": None}))
    deps, prompts, _logs, _files = _two_world_phase({"stable": False, "nightly": False})
    run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert len(prompts) == 6


def test_a_world_that_compiles_the_shared_reply_commits_it(tmp_path):
    """Stable refuses every reply, nightly takes the first: nightly commits in
    stable's pass and makes no call of its own."""
    deps, prompts, _logs, files = _two_world_phase({"stable": False, "nightly": True})
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert len(prompts) == 3
    night = res["nightly"].components[0]
    assert night.authored and night.asked_with == "stable" and not night.adopted
    assert "NightlyOnly { y: u16 }" in files["nightly"]["src/error.rs"]
    assert not res["stable"].components[0].authored


def test_when_the_asking_world_commits_a_peer_that_refused_keeps_its_own_path(tmp_path):
    """Stable commits on attempt 1 and nightly refused that reply: nightly is
    not recorded as failed, it adopts or extends in its own pass as before."""
    deps, prompts, _logs, _files = _two_world_phase({"stable": True, "nightly": False},
                                                    attempts=1)
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert res["stable"].components[0].authored
    night = res["nightly"].components[0]
    assert night.asked_with == "" and len(prompts) == 2, "nightly asked in its own pass"


def test_a_derive_row_the_declaration_refutes_stays_out_of_the_prompt(tmp_path):
    """Run `42057-641e589c`: a test comparing two Strings through
    `io::Error` made the prompt demand `Error: Debug, PartialEq` of a type
    whose declaration derives neither."""
    from pipeline_core.domain.models import FieldRecord

    test_src = ('#[test]\nfn t() {\n    assert_eq!(boxed.downcast_ref::<io::Error>()'
                '.unwrap().to_string(), "io err");\n}\n')
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="#[repr(transparent)]\npub struct Error { inner: Box<ErrorImpl> }",
                   fields=(FieldRecord(name="inner", ty="Box<ErrorImpl>", is_pub=False),)),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="#[derive(PartialEq)]\nstruct ErrorImpl { vtable: u8 }"),
    ]
    surface = classify_private_surface(types, [], cfg_cluster="stable",
                                       author_private_fields=True)
    prompts: list[str] = []
    logs: list[str] = []
    files = {"src/lib.rs": "#[repr(transparent)]\npub struct Error { inner: Box<ErrorImpl> }\n",
             "src/error.rs": "#[derive(PartialEq)]\nstruct ErrorImpl { vtable: u8 }\n"}
    test_src += "#[test]\nfn u() { assert_eq!(a, ErrorImpl { vtable: 1 }); }\n"

    def run_model(p):
        prompts.append(p)
        return ""

    deps = LayoutPhaseDeps(
        log=logs.append, load_surface=lambda _pd, _c: surface,
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "", symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=lambda _pd, _c: AuthorLayoutDeps(
            run_model=run_model, read_skeleton=files.get,
            write_skeleton=files.__setitem__, compile_check=lambda: (False, "x"),
            log=logs.append, max_attempts=1,
            frozen_test_sources=lambda: {"tests/error_gen_1.rs": test_src}),
        recompute_leaf_order=lambda _pd, _c: None)
    run_layout_phase(tmp_path, "anyhow", ["stable"], deps)
    assert prompts and "`Error` must derive" not in prompts[0]
    assert "`ErrorImpl` must derive: PartialEq" in prompts[0], "a confirmed row stays"
    assert any("not asked" in m and "Error: Debug" in m for m in logs)


def _fn_item(sig: str) -> PrivateSurfaceItem:
    return PrivateSurfaceItem(
        SurfaceItemKind.FN_SIG, "get", "src/a.rs", sig, frozenset())


def test_a_type_head_ignores_the_field_block_the_model_writes():
    """Two worlds' ErrorImpl differed by fields on run 42057-641e589c and
    were still one item. The ordinal check cannot see that; the slot can."""
    a = PrivateSurfaceItem(
        SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs",
        "struct ErrorImpl { h: Helper }")
    b = PrivateSurfaceItem(
        SurfaceItemKind.STRUCT, "ErrorImpl", "src/error.rs",
        "struct ErrorImpl { h: NightlyOnly }")
    assert item_slot(a) == item_slot(b)
    assert shared_item_ids_agree([(("ErrorImpl",), (a,)), (("ErrorImpl",), (b,))])
    left = (_fn_item("fn get(a: u8)"), _fn_item("fn get(a: u16)"))
    right = (_fn_item("fn get(a: u16)"), _fn_item("fn get(a: u8)"))
    assert component_uids(left) == component_uids(right) == ("get.1", "get.2")
    assert shared_item_ids_agree(
        [(component_uids(left), left), (component_uids(left), left)])
    assert not shared_item_ids_agree(
        [(component_uids(left), left), (component_uids(right), right)])
    bare = "fn get(a: u8)"
    assert declaration_text(f'#[cfg(not(feature = "std"))] {bare}') == declaration_text(bare)
    assert declaration_text(f"{bare} {{ 1 }}") == declaration_text(f"{bare};")


def _dup_fn_surface(world: str, first: str, second: str) -> PrivateSurface:
    return PrivateSurface(
        cfg_cluster=world,
        replaceable=(_fn_item(first), _fn_item(second)),
        frozen_public=frozenset())


def test_swapped_duplicate_names_are_not_one_ask(tmp_path):
    """The ordinal strings match, which is what the old union check compared,
    and the declarations do not. Each world is asked alone."""
    prompts: list[str] = []
    logs: list[str] = []
    stable = _dup_fn_surface("stable", "fn get(a: u8)", "fn get(a: u16)")
    nightly = _dup_fn_surface("nightly", "fn get(a: u16)", "fn get(a: u8)")
    assert component_uids(stable.components[0]) == component_uids(nightly.components[0])

    def _deps(_pd, _cluster):
        return AuthorLayoutDeps(
            run_model=lambda p: prompts.append(p) or "",
            read_skeleton=lambda _rel: None,
            write_skeleton=lambda _rel, _txt: None,
            compile_check=lambda: (False, "error[E0277]: no"),
            log=logs.append, max_attempts=1, chunk_coverage_retries=0)

    deps = LayoutPhaseDeps(
        log=logs.append,
        load_surface=lambda _pd, c: stable if c == "stable" else nightly,
        load_suite=lambda _pd, c: BehaviourSuite(cfg_cluster=c),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_deps,
        recompute_leaf_order=lambda _pd, _c: None)
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert any("different item in the other world" in m for m in logs)
    assert prompts and all("stable+nightly" not in p for p in prompts)
    assert len(prompts) == 2
    assert res["nightly"].components[0].asked_with == ""


_REPLY_A = (
    '<LAYOUT_ITEM name="ErrorImpl" kind="struct">struct ErrorImpl { h: Box<u8> }</LAYOUT_ITEM>'
    '<LAYOUT_ITEM name="Helper" kind="struct">struct Helper { x: u16 }</LAYOUT_ITEM>'
    '<LAYOUT_ITEM name="NightlyOnly" kind="struct">struct NightlyOnly { y: u16 }</LAYOUT_ITEM>')
_REPLY_B = _REPLY_A.replace("Box<u8>", "Box<i32>")


def _split_commit_phase(nightly: list[bool] | None = None):
    """Stable refuses reply A and commits reply B. Nightly's compile results
    are per call, in the order the ask compiles that world: the shared
    attempts first, then a rejudge of the reply stable commits."""
    prompts: list[str] = []
    logs: list[str] = []
    files = {
        "stable": {"src/error.rs": "struct ErrorImpl { h: Helper }\nstruct Helper { x: u8 }\n"},
        "nightly": {"src/error.rs": "struct ErrorImpl { h: NightlyOnly }\n"
                                    "struct NightlyOnly { x: u8 }\n"},
    }
    calls = {"stable": 0, "nightly": 0}
    plan = {"stable": [False, True], "nightly": nightly if nightly is not None else [True, False]}

    def _compile(cluster):
        i = calls[cluster]
        calls[cluster] = i + 1
        seq = plan[cluster]
        if i < len(seq) and seq[i]:
            return True, ""
        return False, f"error[E0277]: refusal {i} in {cluster}"

    def _author_deps_for(_pd, cluster):
        def run_model(p):
            prompts.append(p)
            return _REPLY_A if len(prompts) == 1 else _REPLY_B
        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: files[cluster].get(rel),
            write_skeleton=lambda rel, txt: files[cluster].__setitem__(rel, txt),
            compile_check=lambda: _compile(cluster),
            log=logs.append, max_attempts=2)

    deps = LayoutPhaseDeps(
        log=logs.append,
        load_surface=lambda _pd, c: _world_surface(c, "Helper" if c == "stable" else "NightlyOnly"),
        load_suite=lambda _pd, _c: _suite_with_pin(),
        public_surface_text=lambda _pd, _c: "",
        symbol_inventory_text=lambda _pd, _c: "",
        author_deps_for=_author_deps_for,
        recompute_leaf_order=lambda _pd, _c: None)
    return deps, prompts, logs, files


def test_a_peer_that_compiled_an_earlier_attempt_is_judged_on_the_committed_reply(tmp_path):
    """Nightly took reply A. Stable commits reply B. Nightly is compiled
    against B, B fails there, and nightly does not keep A's text."""
    deps, prompts, _logs, files = _split_commit_phase()
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert res["stable"].components[0].authored
    assert "Box<i32>" in files["stable"]["src/error.rs"]
    night = res["nightly"].components[0]
    assert night.asked_with == ""
    assert "Box<u8>" not in files["nightly"]["src/error.rs"]
    assert "NightlyOnly { x: u8 }" in files["nightly"]["src/error.rs"]
    assert len(prompts) >= 3


def test_a_peer_that_accepts_the_committed_reply_keeps_that_reply(tmp_path):
    """The same split, except nightly also compiles B. It keeps B, not A,
    and makes no call of its own."""
    deps, prompts, _logs, files = _split_commit_phase([True, True])
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    night = res["nightly"].components[0]
    assert night.authored and night.asked_with == "stable"
    assert "Box<i32>" in files["nightly"]["src/error.rs"]
    assert "Box<u8>" not in files["nightly"]["src/error.rs"]
    assert len(prompts) == 2


def test_negative_control_without_rejudge_the_peer_keeps_the_earlier_reply(
        tmp_path, monkeypatch):
    """The pre-change commit: the peer stays on the reply it already
    compiled, and the asking world's later reply is what the registry stores."""
    import pipeline_core.application.author_layout as layout

    monkeypatch.setattr(layout, "_rejudge_peer_on_committed_reply", lambda *a, **k: None)
    deps, prompts, _logs, files = _split_commit_phase()
    res = run_layout_phase(tmp_path, "anyhow", ["stable", "nightly"], deps)
    assert res["stable"].components[0].authored
    assert "Box<i32>" in files["stable"]["src/error.rs"]
    night = res["nightly"].components[0]
    assert night.authored and night.asked_with == "stable"
    assert "Box<u8>" in files["nightly"]["src/error.rs"]
    assert len(prompts) == 2
