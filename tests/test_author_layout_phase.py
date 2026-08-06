"""Pins for the t1.5 layout-authoring PHASE (F11 M2 orchestration)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from pipeline_core.application.author_layout import AuthorLayoutDeps
from pipeline_core.application.author_layout_phase import (
    LayoutPhaseDeps,
    make_pin_selector,
    run_layout_phase,
)
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface


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
            behaviour_id="static::repr::0", given="", when="size_of",
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
            behaviour_id="static::test_repr.rs::0", given="", when="size_of",
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
            behaviour_id="static::test_other.rs::0", given="", when="size_of",
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
