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
