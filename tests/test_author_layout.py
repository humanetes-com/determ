"""Pins for the F11 layout-authoring use-case (t1.5 orchestration)."""
from __future__ import annotations

import dataclasses

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    ComponentResult,
    LayoutOutcome,
    author_layout,
    component_outcome,
)
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.models import FieldRecord, TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface


def _surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


def _pin():
    return BehaviourSpec(
        behaviour_id="static::repr::0", given="", when="size_of::<Error>()",
        then=ObservableOutcome.static_assertion(
            StaticAssertKind.SIZE, "assert_eq!(size_of::<Error>(), size_of::<usize>())"),
        subkind=BehaviourSubKind.OUTCOME,
    )


def _has_pins(comp):
    return [_pin()]


class _Deps:
    def __init__(self, reply, compile_results):
        self.reply = reply
        self.compile_results = list(compile_results)
        self.files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}
        self.logs: list[str] = []
        self.prompts: list[str] = []

    def deps(self):
        def run_model(p):
            self.prompts.append(p)
            return self.reply
        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: self.files.get(rel),
            write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
            compile_check=lambda: (self.compile_results.pop(0),
                                   "error[E0308]: mismatched types"),
            log=self.logs.append, max_attempts=3)


_GOOD = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
         'struct ErrorImpl { inner: Box<dyn StdError> }</LAYOUT_ITEM>')


def test_happy_path_authors_and_commits():
    d = _Deps(_GOOD, [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="pub struct Error;",
                        select_pins=_has_pins, symbol_inventory="Box", deps=d.deps())
    assert res.all_authored
    assert "Box<dyn StdError>" in d.files["src/error.rs"]


def test_compile_failure_reverts_scaffold():
    # authored def never compiles → skeleton must be restored to frozen state.
    d = _Deps(_GOOD, [False, False, False])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert d.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"


def test_incomplete_coverage_retries_then_gives_up():
    d = _Deps("<LAYOUT_ITEM name=\"Other\" kind=\"struct\">struct Other;</LAYOUT_ITEM>",
              [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert any("missing authored items" in m for m in d.logs)
    # never wrote a partial scaffold.
    assert d.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"


def test_second_attempt_recovers_after_compile_fail():
    d = _Deps(_GOOD, [False, True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored
    assert len(d.prompts) == 2  # retried once
    # the retry prompt carries the compiler diagnostics, not a bare "failed"
    assert "error[E0308]" in d.prompts[1]


def test_m13_gate_refuses_layout_sensitive_component_without_pins():
    # ErrorImpl feeds pub Error's size (public_referenced) but no pin is
    # selected → must be GATED (left frozen), never authored blind.
    d = _Deps(_GOOD, [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=lambda comp: [], symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert res.gated and res.gated[0].item_names == ("ErrorImpl",)
    assert d.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"  # untouched
    assert d.prompts == []  # never even prompted the model
    assert any("GATED" in m for m in d.logs)


def test_component_outcome_authored():
    c = ComponentResult(item_names=("Foo",), authored=True)
    assert component_outcome(c) is LayoutOutcome.AUTHORED


def test_component_outcome_gated_takes_priority_over_authored_flag():
    c = ComponentResult(item_names=("Foo",), authored=False, gated=True)
    assert component_outcome(c) is LayoutOutcome.GATED


def test_component_outcome_frozen_when_neither_authored_nor_gated():
    c = ComponentResult(item_names=("Foo",), authored=False, reason="did not compile")
    assert component_outcome(c) is LayoutOutcome.FROZEN


class _ProbeDeps(_Deps):
    """`_Deps` plus the pin-probe port, recording install/remove order."""

    def __init__(self, reply, compile_results):
        super().__init__(reply, compile_results)
        self.probe_writes: list[str | None] = []

    def deps(self):
        base = super().deps()
        return dataclasses.replace(
            base, write_pin_probe=self.probe_writes.append)


def test_pin_probe_is_calibrated_on_the_frozen_scaffold_first():
    # First compile_check is the CALIBRATION (probe vs the pristine layout);
    # only then is a candidate authored and gated.
    d = _ProbeDeps(_GOOD, [True, True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored
    # install → remove (calibration), install → remove (the gated attempt)
    assert [w is not None for w in d.probe_writes] == [True, False, True, False]
    assert any("layout pin(s) verified" in m for m in d.logs)


def test_probe_that_fails_on_the_frozen_scaffold_disables_pin_gating():
    # The frozen layout is the crate's own, so every harvested pin holds there.
    # A probe that fails calibration is wrong ABOUT ITSELF — it must never be
    # allowed to reject an authored layout (that would freeze good components).
    d = _ProbeDeps(_GOOD, [False, True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored, "calibration failure must not fail the component"
    assert any("probe is wrong" in m for m in d.logs)
    # Calibration installed the probe once; after it failed, NO further install
    # happens — the attempt runs ungated. Removal stays unconditional so a stale
    # probe from an interrupted run cannot survive into a gated compile.
    assert d.probe_writes.count(str(d.probe_writes[0])) <= 1
    assert sum(w is not None for w in d.probe_writes) == 1
    assert d.probe_writes[-1] is None


def test_probe_is_removed_even_when_the_authored_layout_fails_to_compile():
    # A probe left behind would poison every later compile in the run.
    d = _ProbeDeps(_GOOD, [True, False, False, False])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert d.probe_writes[-1] is None


def test_failed_compile_log_names_the_diagnostic():
    """A bare "did not compile" makes one broken gate (e.g. E0554: the cluster's
    toolchain never reached cargo) read as N independently-hard components."""
    d = _Deps(_GOOD, [False, False, False])
    author_layout(_surface(), module_name="anyhow", public_surface="pub struct Error;",
                  select_pins=_has_pins, symbol_inventory="Box", deps=d.deps())
    failed = [ln for ln in d.logs if "did not compile" in ln]
    assert failed
    assert all("error[E0308]: mismatched types" in ln for ln in failed)


# --- PUB_FIELDS: fields are authorable, the declaration head is not ---------

def _pub_fields_surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error<T: ?Sized> { inner: Box<ErrorImpl<T>> }",
                   fields=(FieldRecord(name="inner", ty="Box<ErrorImpl<T>>", is_pub=False),)),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable",
                                    author_private_fields=True)


class _PubDeps(_Deps):
    def __init__(self, reply, compile_results):
        super().__init__(reply, compile_results)
        self.files = {
            "src/lib.rs": "pub struct Error<T: ?Sized> { inner: Box<ErrorImpl<T>> }\n"}


def _author_pub(reply, compile_results=(True,)):
    d = _PubDeps(reply, compile_results)
    res = author_layout(_pub_fields_surface(), module_name="anyhow",
                        public_surface="pub struct Error<T: ?Sized>;",
                        select_pins=_has_pins, symbol_inventory="Box", deps=d.deps())
    return res, d


def test_authored_fields_are_spliced_into_the_public_struct():
    res, d = _author_pub(
        '<LAYOUT_ITEM name="Error" kind="struct">'
        'pub struct Error<T: ?Sized> { boxed: Box<dyn StdError>, note: String }'
        '</LAYOUT_ITEM>')
    assert res.all_authored
    assert "boxed: Box<dyn StdError>" in d.files["src/lib.rs"]


def test_changed_declaration_head_is_rejected_not_spliced():
    # bound dropped: compiles inside the crate, breaks the public contract.
    res, d = _author_pub(
        '<LAYOUT_ITEM name="Error" kind="struct">'
        'pub struct Error<T> { boxed: Box<dyn StdError> }</LAYOUT_ITEM>',
        compile_results=(True, True, True))
    assert not res.all_authored
    assert "changed the frozen public declaration" in res.components[0].reason
    assert d.files["src/lib.rs"].startswith("pub struct Error<T: ?Sized>")


def test_field_made_public_is_rejected():
    res, _ = _author_pub(
        '<LAYOUT_ITEM name="Error" kind="struct">'
        'pub struct Error<T: ?Sized> { pub boxed: Box<dyn StdError> }</LAYOUT_ITEM>',
        compile_results=(True, True, True))
    assert not res.all_authored
    assert "widening the public API" in res.components[0].reason


# ---- cross-cluster adoption: one crate, ONE definition per private type ----

def test_registry_hit_adopts_without_calling_the_model():
    # A later cfg-cluster must reuse the definition an earlier one fixed: it is
    # seeded with that cluster's filled bodies, which read those exact fields.
    from pipeline_core.domain.authored_layout import AuthoredItem
    fixed = AuthoredItem("ErrorImpl", "struct",
                         "struct ErrorImpl { inner: Box<dyn StdError> }")
    d = _Deps("<LAYOUT_ITEM name=\"ErrorImpl\" kind=\"struct\">"
              "struct ErrorImpl { totally_different: u8 }</LAYOUT_ITEM>", [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps(),
                        registry={"ErrorImpl": fixed})
    assert res.all_authored
    assert d.prompts == []                                  # no model call at all
    assert "inner: Box<dyn StdError>" in d.files["src/error.rs"]
    assert res.components[0].adopted
    assert res.components[0].authored_items == (("ErrorImpl", fixed),)


def test_adopted_layout_that_does_not_compile_falls_back_to_authoring():
    # The compile gate rules on an adopted layout exactly as on a fresh one;
    # divergence is the DEGRADED path, and it must be logged as such.
    from pipeline_core.domain.authored_layout import AuthoredItem
    fixed = AuthoredItem("ErrorImpl", "struct", "struct ErrorImpl { nope: Missing }")
    d = _Deps(_GOOD, [False, True])   # adoption fails, then the model's reply compiles
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps(),
                        registry={"ErrorImpl": fixed})
    assert res.all_authored
    assert not res.components[0].adopted
    assert len(d.prompts) == 1
    assert any("DIVERGE" in m for m in d.logs)
    assert "Box<dyn StdError>" in d.files["src/error.rs"]


def test_partial_registry_coverage_puts_fixed_defs_in_the_prompt_verbatim():
    # Component not fully covered → the model EXTENDS the fixed definitions and
    # must see their exact text, rather than redesigning them.
    from pipeline_core.domain.authored_layout import AuthoredItem
    fixed = AuthoredItem("Other", "struct", "struct Other { tag: u32 }")
    d = _Deps(_GOOD, [True])
    author_layout(_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=d.deps(),
                  registry={"Other": fixed})
    assert d.prompts                       # the model WAS asked
    assert "struct Other { tag: u32 }" not in d.prompts[0]   # not this component's uid
