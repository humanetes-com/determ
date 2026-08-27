"""Pins for the F11 layout-authoring use-case (t1.5 orchestration)."""
from __future__ import annotations

import dataclasses

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    ComponentResult,
    LayoutOutcome,
    _calibrated_field_path_probes,
    _component_field_paths,
    _install_field_probes,
    _oracle_sources,
    _strip_field_probes,
    author_layout,
    component_outcome,
)
from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.layout_probe import LayoutProbe, build_field_path_probe
from pipeline_core.domain.models import FieldRecord, TypeRecord
from pipeline_core.domain.oracle_field_paths import FieldPath
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
    classify_private_surface,
)


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


class _SeqDeps(_PubDeps):
    """Replies pop in order — one distinct violation per attempt."""

    def __init__(self, replies, compile_results):
        super().__init__("", compile_results)
        self.replies = list(replies)

    def deps(self):
        base = super().deps()
        return dataclasses.replace(
            base, run_model=lambda p: (self.prompts.append(p),
                                       self.replies.pop(0))[1])


def test_retry_prompts_accumulate_every_violated_constraint():
    """The live failure shape, in one fire: six authoring attempts across two
    2026-08-09/10 fires each fixed the named violation while committing a
    different one (renamed struct -> pub field -> dropped member, then the
    same ring again), because the retry hint carried only the LAST rejection.
    The hint is a cumulative checklist: attempt 3 must still see attempt 1's
    violation."""
    d = _SeqDeps([
        # attempt 1: drops the `?Sized` bound — frozen head changed.
        '<LAYOUT_ITEM name="Error" kind="struct">'
        'pub struct Error<T> { boxed: Box<dyn StdError> }</LAYOUT_ITEM>',
        # attempt 2: head restored, but a field turned `pub`.
        '<LAYOUT_ITEM name="Error" kind="struct">'
        'pub struct Error<T: ?Sized> { pub boxed: Box<dyn StdError> }'
        '</LAYOUT_ITEM>',
        # attempt 3: would be correct — the assertion is about its PROMPT.
        '<LAYOUT_ITEM name="Error" kind="struct">'
        'pub struct Error<T: ?Sized> { boxed: Box<dyn StdError> }'
        '</LAYOUT_ITEM>',
    ], compile_results=(True, True, True))
    res = author_layout(_pub_fields_surface(), module_name="anyhow",
                        public_surface="pub struct Error<T: ?Sized>;",
                        select_pins=_has_pins, symbol_inventory="Box",
                        deps=d.deps())
    assert res.all_authored
    assert len(d.prompts) == 3
    assert "changed the frozen public declaration" in d.prompts[1]
    assert "changed the frozen public declaration" in d.prompts[2], (
        "attempt 3's prompt must still carry attempt 1's violation — "
        "last-only feedback is how the live fires cycled through the same "
        "three violations twice")
    assert "widening the public API" in d.prompts[2]


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


_VTABLE_FROZEN = ("struct ErrorVTable { object_drop: unsafe fn(u8), "
                  "object_ref: unsafe fn(u8) -> u8, "
                  "object_downcast: unsafe fn(u8) -> u8 }")
# the shape of the live incident: seven fn pointers reduced to one
_EMPTIED = ('<LAYOUT_ITEM name="ErrorVTable" kind="struct">'
            'struct ErrorVTable { object_drop: unsafe fn(u8) }</LAYOUT_ITEM>')
# a real redesign of the same type: the slots are traded, not removed
_RESHAPED = ('<LAYOUT_ITEM name="ErrorVTable" kind="struct">'
             'struct ErrorVTable { drop_fn: unsafe fn(u8), ref_fn: unsafe fn(u8) -> u8, '
             'cast_fn: unsafe fn(u8) -> u8 }</LAYOUT_ITEM>')


def _vtable_surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorVTable> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorVTable",
                   is_pub=False, definition=_VTABLE_FROZEN),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


def _reads_the_vtable():
    return ("unsafe { (vtable(p).object_ref)(e) }\n"
            "let f = vtable(p).object_downcast;\n")


def _vtable_deps(reply, compiles=(True, True, True)):
    d = _Deps(reply, list(compiles))
    d.files = {"src/error.rs": _VTABLE_FROZEN + "\n"}
    return d


def test_layout_dropping_a_member_the_filled_bodies_read_is_refused():
    """The 2026-08-01 `anyhow`/nightly defect. A re-author is accepted on the
    strength of the SKELETON compiling, and a skeleton's bodies are all
    `unimplemented!()` — none constructs the type, none reads a field — so any
    layout that merely parses compiles there. That day one reduced
    `ErrorVTable` from 7 fn-pointer fields to 1 and replaced `ErrorImpl`'s
    payload with `PhantomData`; it compiled, it cleared the layout pins (`Error`
    is a `NonNull` to `ErrorImpl`, so no repr pin can see through it), and the
    cluster fell from 93 passing tests to a tree in which `construct<E>(error:
    E, ...)` has nowhere to put `error`."""
    d = _vtable_deps(_EMPTIED)
    deps = dataclasses.replace(d.deps(), filled_bodies=_reads_the_vtable)
    res = author_layout(_vtable_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=deps)
    assert not res.all_authored
    # the working tree is left exactly as it was — a refused layout must not be
    # half-landed, which is what made the live incident unrecoverable
    assert d.files["src/error.rs"] == _VTABLE_FROZEN + "\n"


def test_the_refusal_names_the_member_back_to_the_model():
    """A rejection the model cannot act on just burns the retry budget. The
    hint has to name the member and its read count: a component can carry
    thirty items and "you dropped a field" leaves the model guessing."""
    d = _vtable_deps(_EMPTIED)
    deps = dataclasses.replace(d.deps(), filled_bodies=_reads_the_vtable)
    author_layout(_vtable_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=deps)
    assert len(d.prompts) > 1, "a refused layout must be retried"
    assert "`object_ref` (1 read)" in d.prompts[1]
    assert "`object_downcast` (1 read)" in d.prompts[1]
    assert "PREVIOUS ATTEMPT FAILED" in d.prompts[1]


def test_a_redesign_that_trades_slots_rather_than_removing_them_is_accepted():
    """The check must convict the emptied layout and nothing else. A re-trigger
    reverts every fn that references the component so those fns can be rewritten
    against the new shape — "a filled body reads this member" is therefore the
    normal state of a genuine redesign, and vetoing on it alone would refuse
    almost every one, turning the crate's escape hatch into a no-op."""
    d = _vtable_deps(_RESHAPED, compiles=(True,))
    deps = dataclasses.replace(d.deps(), filled_bodies=_reads_the_vtable)
    res = author_layout(_vtable_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=deps)
    assert res.all_authored
    assert "drop_fn" in d.files["src/error.rs"]


def test_bake_time_has_no_filled_bodies_and_so_no_veto():
    """At bake time every body is a stub, so there is no evidence a layout can
    contradict. The gate must stay exactly as it was — the emptied layout is
    still the right answer when nothing has been written against it."""
    d = _vtable_deps(_EMPTIED, compiles=(True,))
    res = author_layout(_vtable_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored


# --- the re-author regression guard ------------------------------------------
# A re-author is a fresh model call; live on 2026-08-11 (arm C, microcrate)
# attempt 1 re-authored `Counter` WITH `#[derive(Debug, Default, Clone,
# PartialEq, Eq)]` and a later attempt on the same item produced no derive at
# all (and another a private struct, `error[E0603]`). Nothing compared the
# replacement against the layout it replaced, so the repair could hand back
# something worse than what it was fixing.

_DERIVED_CURRENT = ("#[derive(Debug, Default, Clone, PartialEq, Eq)]\n"
                    "struct Counter { count: u32 }")

_NO_DERIVE_REPLY = ('<LAYOUT_ITEM name="Counter" kind="struct">'
                    'struct Counter { count: u32 }</LAYOUT_ITEM>')

_KEPT_DERIVE_REPLY = ('<LAYOUT_ITEM name="Counter" kind="struct">'
                      '#[derive(Debug, Default, Clone, PartialEq, Eq)]\n'
                      'struct Counter { count: u32, cap: u32 }</LAYOUT_ITEM>')


def _counter_deps(reply, compiles=(True, True, True)):
    d = _Deps(reply, compiles)
    d.files = {"src/counter.rs": _DERIVED_CURRENT + "\n"}
    return d


def _counter_surface():
    types = [TypeRecord(file="src/counter.rs", kind="struct", name="Counter",
                        is_pub=False, definition=_DERIVED_CURRENT)]
    return classify_private_surface(types, [], cfg_cluster="stable")


def test_reauthor_dropping_a_derive_the_current_layout_carries_is_refused():
    d = _counter_deps(_NO_DERIVE_REPLY)
    deps = dataclasses.replace(d.deps(), guard_replaced_layout=True)
    res = author_layout(_counter_surface(), module_name="m", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=deps)
    assert not res.all_authored
    why = next(c.reason for c in res.components if not c.authored)
    assert "strictly worse" in why and "Default" in why
    # nothing half-landed
    assert d.files["src/counter.rs"] == _DERIVED_CURRENT + "\n"
    # and the retry prompt names the constraint back to the model
    assert len(d.prompts) > 1 and "derive" in d.prompts[1]


def test_reauthor_keeping_the_derives_passes_the_guard():
    d = _counter_deps(_KEPT_DERIVE_REPLY)
    deps = dataclasses.replace(d.deps(), guard_replaced_layout=True)
    res = author_layout(_counter_surface(), module_name="m", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=deps)
    assert res.all_authored


def test_initial_authoring_is_not_held_to_the_frozen_derives():
    """NEGATIVE CONTROL: without the flag (bake time), the same derive-dropping
    reply is accepted exactly as before — the reference there is the frozen
    original and a redesign may legitimately not carry its every derive (the
    test-required ones are computed by `oracle_derives` into the prompt)."""
    d = _counter_deps(_NO_DERIVE_REPLY)
    res = author_layout(_counter_surface(), module_name="m", public_surface="",
                        select_pins=_has_pins, symbol_inventory="",
                        deps=d.deps())
    assert res.all_authored


# --- the oracle's member paths as an authoring constraint ---------------------
# t1.5 elides the private fields of a public struct and asks the model to design
# them. The in-crate augmented tests are generated BEFORE that, from the original
# source where those fields were visible, and are write-protected afterwards — so
# a layout that renames a field they read makes a file no stage may edit
# uncompilable. On `anyhow` (2026-08-06) the authored `Error { ptr: … }` passed
# `cargo check --lib`, which never builds `#[cfg(test)]` code, and the first
# `cargo test` failed `error[E0609]: no field 'inner' on type 'Error'` — 0/56
# unit tests in both clusters, with the only diagnostic pointing into the oracle.

def _item(name, definition, kind=SurfaceItemKind.STRUCT, file="src/error.rs"):
    return PrivateSurfaceItem(kind=kind, name=name, file=file,
                              definition=definition)


def test_an_unreadable_oracle_degrades_instead_of_failing_authoring():
    """The constraint is an improvement on authoring blind, never a new way for
    the phase to die: an oracle that cannot be read leaves the behaviour that
    existed before it did."""
    logs: list[str] = []

    def boom():
        raise OSError("permission denied")

    deps = dataclasses.replace(
        _Deps(_GOOD, [True]).deps(), oracle_sources=boom, log=logs.append)
    assert _oracle_sources(deps) == {}
    assert any("authoring without them" in ln for ln in logs)


def test_no_oracle_dep_means_no_field_path_constraint():
    deps = _Deps(_GOOD, [True]).deps()
    assert deps.oracle_sources is None
    assert _oracle_sources(deps) == {}


def test_field_paths_are_read_for_the_component_that_owns_the_root():
    comp = (_item("Error", "pub struct Error { }", SurfaceItemKind.PUB_FIELDS),)
    oracle = {"src/error.gen_tests_3.rs":
              'let err = Error::msg("x");\nlet _ = err.inner.ptr.as_ptr();\n'}
    paths = _component_field_paths(comp, oracle)
    assert [p.expr for p in paths] == ["Error.inner.ptr"]


def test_a_pub_fields_root_is_exempt_from_the_declares_it_check():
    """The freeze REMOVED this root's fields, so there is nothing left in the
    skeleton to check the oracle's name against — the oracle is the only
    surviving record of what the field was called."""
    comp = (_item("Error", "pub struct Error { }", SurfaceItemKind.PUB_FIELDS),)
    oracle = {"t.rs": 'let e: Error = make();\nlet _ = e.inner;\n'}
    assert [p.expr for p in _component_field_paths(comp, oracle)] == ["Error.inner"]


def test_a_path_whose_frozen_root_does_not_declare_the_first_hop_is_dropped():
    """A still-frozen definition is the authority on its own field names. If it
    does not declare the hop, the oracle's binding was resolved to the wrong
    type — and imposing a wrong path would freeze a component that was fine."""
    comp = (_item("ErrorImpl", "struct ErrorImpl { vtable: u8 }"),)
    oracle = {"t.rs": 'let e: ErrorImpl = make();\nlet _ = e.nonexistent;\n'}
    assert _component_field_paths(comp, oracle) == ()


def test_a_path_the_frozen_root_does_declare_is_kept():
    comp = (_item("ErrorImpl", "struct ErrorImpl { vtable: u8 }"),)
    oracle = {"t.rs": 'let e: ErrorImpl = make();\nlet _ = e.vtable;\n'}
    assert [p.expr for p in _component_field_paths(comp, oracle)] == ["ErrorImpl.vtable"]


def test_the_same_path_read_in_two_files_is_one_constraint():
    comp = (_item("Error", "pub struct Error { }", SurfaceItemKind.PUB_FIELDS),)
    src = 'let e: Error = make();\nlet _ = e.inner;\n'
    paths = _component_field_paths(comp, {"a.rs": src, "b.rs": src})
    assert [p.expr for p in paths] == ["Error.inner"]


def test_no_oracle_means_no_paths():
    comp = (_item("Error", "pub struct Error { }", SurfaceItemKind.PUB_FIELDS),)
    assert _component_field_paths(comp, {}) == ()


class _FieldProbeDeps:
    """Deps for the member-path probe calibration. The skeleton is a dict, so a
    test can see WHICH file the probe was written into and what the tree looked
    like at the moment of each compile; `compile_check` is scripted."""

    def __init__(self, compiles, errors="", files=None):
        self.compiles = compiles
        self.errors = errors
        self.files: dict[str, str] = dict(files or {
            "src/error.rs": "struct ErrorImpl { vtable: u8 }\n",
            "src/lib.rs": "pub struct Error { }\n"})
        self.written: list[str | None] = []          # the crate-root pin slot
        self.at_compile: list[dict[str, str]] = []   # tree snapshot per compile
        self.logs: list[str] = []

    def deps(self):
        def compile_check():
            self.at_compile.append(dict(self.files))
            return self.compiles, self.errors
        return AuthorLayoutDeps(
            run_model=lambda p: "",
            read_skeleton=lambda rel: self.files.get(rel),
            write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
            compile_check=compile_check,
            write_pin_probe=self.written.append,
            log=self.logs.append)


def _probe_for(paths, deps, comp=None):
    surface = _surface()
    comp = comp or (_item("Error", "pub struct Error { }", SurfaceItemKind.PUB_FIELDS,
                          file="src/lib.rs"),)
    return _calibrated_field_path_probes(surface, comp, tuple(paths), deps)


def _gated(in_file):
    return tuple(e for _, p in in_file for e in p.gated)


def _skipped(in_file):
    return tuple(e for _, p in in_file for e in p.skipped)


def test_a_path_probe_that_compiles_on_the_frozen_scaffold_is_gated():
    d = _FieldProbeDeps(compiles=True)
    in_file = _probe_for([FieldPath("Error", ("inner",))], d.deps())
    assert _gated(in_file) == ("Error.inner",)
    assert "_layout_pin_field_path_0" not in d.files["src/lib.rs"], (
        "the probe region must be gone once the check has been made")


def test_the_probe_is_written_into_the_file_that_DECLARES_the_root():
    """A private field is visible to its own module and that module's
    descendants, and nowhere else. Probed from the crate root, a correct layout
    for a type declared in a submodule answers `error[E0616]: field is private`
    — and no attempt could ever satisfy it, because `_public_contract_broken`
    rejects a `pub` field outright. Measured on a two-module crate (2026-08-06):
    crate-root probe, field elided → E0609 (so calibration arms the gate); field
    present and private → E0616 on EVERY candidate. In its own file the same
    probe compiles."""
    d = _FieldProbeDeps(compiles=True)
    comp = (_item("ErrorImpl", "struct ErrorImpl { vtable: u8 }", file="src/error.rs"),)
    in_file = _probe_for([FieldPath("ErrorImpl", ("vtable",))], d.deps(), comp)
    assert [rel for rel, _ in in_file] == ["src/error.rs"]
    assert "&v.vtable" in d.at_compile[0]["src/error.rs"]
    assert "_layout_pin_field_path" not in d.at_compile[0]["src/lib.rs"]
    assert d.written == [], "the crate-root pin slot is not the member-path slot"


def test_roots_declared_in_two_files_get_one_probe_each():
    d = _FieldProbeDeps(compiles=True)
    comp = (_item("Error", "pub struct Error { }", SurfaceItemKind.PUB_FIELDS,
                  file="src/lib.rs"),
            _item("ErrorImpl", "struct ErrorImpl { vtable: u8 }", file="src/error.rs"))
    in_file = _probe_for(
        [FieldPath("Error", ("inner",)), FieldPath("ErrorImpl", ("vtable",))],
        d.deps(), comp)
    assert [rel for rel, _ in in_file] == ["src/error.rs", "src/lib.rs"]
    assert "&v.vtable" in d.at_compile[0]["src/error.rs"]
    assert "&v.inner" in d.at_compile[0]["src/lib.rs"]
    assert len(d.at_compile) == 1, "both files, ONE calibration compile"


def test_only_a_probe_that_checks_something_is_placed():
    """A component's paths can group into a file that renders nothing — every
    root declared there generic. That entry still travels with the gate so its
    skipped paths stay named, and it must not put an empty region in the file."""
    d = _FieldProbeDeps(compiles=True)
    in_file = (("src/error.rs",
                build_field_path_probe([FieldPath("ErrorImpl", ("vtable",))])),
               ("src/lib.rs", LayoutProbe(skipped=("ContextError.context",))))
    deps = d.deps()
    _install_field_probes(deps, in_file)
    assert "_layout_pin_field_path_0" in d.files["src/error.rs"]
    assert d.files["src/lib.rs"] == "pub struct Error { }\n"
    _strip_field_probes(deps, in_file)
    assert "_layout_pin_field_path_0" not in d.files["src/error.rs"]


def test_a_declaring_file_the_skeleton_does_not_have_is_skipped_not_created():
    """The probe is a gate. A gate that writes a source file the crate never had
    would put a `fn` in a module nothing declares, and the compile it then reads
    would be answering a question about the gate."""
    d = _FieldProbeDeps(compiles=True, files={"src/lib.rs": "pub struct Error { }\n"})
    comp = (_item("ErrorImpl", "struct ErrorImpl { vtable: u8 }", file="src/gone.rs"),)
    in_file = _probe_for([FieldPath("ErrorImpl", ("vtable",))], d.deps(), comp)
    assert _gated(in_file) == ("ErrorImpl.vtable",)   # rendered, just never placed
    assert "src/gone.rs" not in d.files


def test_a_path_probe_failing_ONLY_on_E0609_is_still_gated():
    """`no field 'inner' on type 'Error'` against an ELIDED struct is the
    expected reading of a correct probe — the fields are exactly what this
    component is about to author. Requiring the probe to hold first, as the pin
    probe does, would disable the gate precisely where it is needed."""
    d = _FieldProbeDeps(compiles=False,
                   errors="error[E0609]: no field `inner` on type `Error`")
    in_file = _probe_for([FieldPath("Error", ("inner",))], d.deps())
    assert _gated(in_file) == ("Error.inner",)


def test_a_path_probe_wrong_about_itself_disables_gating_loudly():
    """In its own file the probe still resolves the DEEPER hops through types
    other modules own, and `field 'raw' of 'Vtable' is private` is the probe's
    own reach, not a layout violation. Gating on it would reject every correct
    layout, so it is dropped and said out loud."""
    d = _FieldProbeDeps(compiles=False,
                   errors="error[E0616]: field `raw` of `Vtable` is private")
    in_file = _probe_for([FieldPath("Error", ("inner", "raw"))], d.deps())
    assert _gated(in_file) == ()
    assert _skipped(in_file) == ("Error.inner.raw",)
    assert any("wrong about itself" in ln for ln in d.logs)


def test_a_generic_root_is_named_as_skipped_not_guessed_at():
    d = _FieldProbeDeps(compiles=True)
    comp = (_item("ContextError", "struct ContextError<C, E> { context: C, error: E }"),)
    in_file = _probe_for([FieldPath("ContextError", ("context",))], d.deps(), comp)
    assert _gated(in_file) == ()
    assert _skipped(in_file) == ("ContextError.context",)
    assert any("generic root" in ln for ln in d.logs)
    assert d.at_compile == [], "nothing renderable, so nothing is compiled"


def test_no_paths_writes_no_probe():
    d = _FieldProbeDeps(compiles=True)
    assert _probe_for([], d.deps()) == ()
    assert d.at_compile == []


def test_member_path_gating_does_not_depend_on_the_crate_root_pin_port():
    """Member-path probes are placed with `read_skeleton`/`write_skeleton`, which
    every caller supplies. They used to ride in the pin port and were gated on
    it; keeping that guard after the move would drop the one check standing
    between an authored layout and a write-protected test that cannot compile —
    silently, for any caller that happens not to install pins."""
    d = _FieldProbeDeps(compiles=True)
    deps = dataclasses.replace(d.deps(), write_pin_probe=None)
    in_file = _probe_for([FieldPath("Error", ("inner",))], deps)
    assert _gated(in_file) == ("Error.inner",)
    assert "&v.inner" in d.at_compile[0]["src/lib.rs"]


class _ScriptedDeps:
    """`compile_check` answers a scripted (compiles, errors) per call, so a test
    can drive the two calibrations and the attempt compiles apart."""

    def __init__(self, reply, results, oracle):
        self.reply = reply
        self.results = list(results)
        self.oracle = oracle
        self.files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n"}
        self.at_compile: list[dict[str, str]] = []
        self.logs: list[str] = []

    def deps(self):
        def compile_check():
            self.at_compile.append(dict(self.files))
            return self.results.pop(0)
        return AuthorLayoutDeps(
            run_model=lambda p: self.reply,
            read_skeleton=lambda rel: self.files.get(rel),
            write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
            compile_check=compile_check,
            write_pin_probe=lambda src: None,
            oracle_sources=lambda: self.oracle,
            log=self.logs.append, max_attempts=3)


def test_an_E0616_after_authoring_is_the_probe_s_reach_not_a_rejection():
    """The hop calibration cannot see: with the root's own fields elided rustc
    stops at E0609 and never resolves `.raw`, so a member private to a THIRD
    module only shows up once a candidate has put the first hop back. Freezing
    the component there would reject a layout no attempt could ever fix, so the
    candidate is re-decided with the probe removed."""
    oracle = {"src/error.gen_tests_1.rs":
              "let e: ErrorImpl = make();\nlet _ = e.vtable.raw;\n"}
    d = _ScriptedDeps(_GOOD, [
        (True, ""),                                             # pin calibration
        (False, "error[E0609]: no field `raw` on type `u8`"),    # path calibration
        (False, "error[E0616]: field `raw` of `Vtable` is private"),  # attempt
        (True, ""),                                             # re-decided
    ], oracle)
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored
    assert "Box<dyn StdError>" in d.files["src/error.rs"]
    assert "_layout_pin_field_path" not in d.files["src/error.rs"]
    assert any("private to another module" in ln for ln in d.logs)


def test_the_success_line_counts_the_paths_the_compile_did_decide():
    """The other half of the same rule: when the probe rode along with the
    compile that accepted the layout, the log says so and says how many."""
    oracle = {"src/error.gen_tests_1.rs":
              "let e: ErrorImpl = make();\nlet _ = e.vtable;\n"}
    d = _ScriptedDeps(_GOOD, [
        (True, ""),                                          # pin calibration
        (False, "error[E0609]: no field `vtable`"),           # path calibration
        (True, ""),                                           # the attempt
    ], oracle)
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored
    success = [ln for ln in d.logs if "authored layout compiles" in ln]
    assert success == [success[0]]
    assert "1 layout pin(s) verified, 1 member path(s) verified" in success[0]
    assert "_layout_pin_field_path" not in d.files["src/error.rs"]


def test_the_success_line_does_not_claim_paths_the_deciding_compile_never_saw():
    """The run log is the instrument this phase is diagnosed from, and the
    fallback's compile ran with the probe stripped. Printing "1 member path(s)
    verified" three lines under "decided without it" makes the log argue with
    itself about the one question it was there to answer. The pin count stays:
    the pins DID hold on the first compile — that is what makes the code set
    exactly {E0616}."""
    oracle = {"src/error.gen_tests_1.rs":
              "let e: ErrorImpl = make();\nlet _ = e.vtable.raw;\n"}
    d = _ScriptedDeps(_GOOD, [
        (True, ""),
        (False, "error[E0609]: no field `raw` on type `u8`"),
        (False, "error[E0616]: field `raw` of `Vtable` is private"),
        (True, ""),
    ], oracle)
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored
    success = [ln for ln in d.logs if "authored layout compiles" in ln]
    assert success and "member path(s) verified" not in success[0]
    assert "1 layout pin(s) verified" in success[0]


def test_a_probe_proven_unusable_is_not_reinstalled_on_the_next_attempt():
    """Cost, and only cost: the same probe would fail the same way and buy a
    second `compile_check` to reach a verdict already reached. Attempt 2 here
    compiles ONCE, and the tree it compiles carries no probe region."""
    oracle = {"src/error.gen_tests_1.rs":
              "let e: ErrorImpl = make();\nlet _ = e.vtable.raw;\n"}
    d = _ScriptedDeps(_GOOD, [
        (True, ""),                                            # pin calibration
        (False, "error[E0609]: no field `raw` on type `u8`"),   # path calibration
        (False, "error[E0616]: field `raw` of `Vtable` is private"),  # attempt 1
        (False, "error: expected one of `,` or `}`"),           # re-decided: no
        (True, ""),                                             # attempt 2
    ], oracle)
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert res.all_authored
    assert len(d.at_compile) == 5, "attempt 2 compiled once, not twice"
    assert "_layout_pin_field_path" not in d.at_compile[-1]["src/error.rs"]
    assert any("still refused" in ln for ln in d.logs)


def test_an_E0616_that_survives_the_probe_s_removal_is_still_a_rejection():
    """The fallback re-decides, it does not excuse: a candidate that does not
    compile without the probe either is reverted exactly as before."""
    oracle = {"src/error.gen_tests_1.rs":
              "let e: ErrorImpl = make();\nlet _ = e.vtable.raw;\n"}
    d = _ScriptedDeps(_GOOD, [
        (True, ""),
        (False, "error[E0609]: no field `raw` on type `u8`"),
    ] + [(False, "error[E0616]: field `raw` of `Vtable` is private")] * 4, oracle)
    res = author_layout(_surface(), module_name="anyhow", public_surface="",
                        select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert not res.all_authored
    assert d.files["src/error.rs"] == "struct ErrorImpl { vtable: u8 }\n"


def test_the_fallback_fires_once_per_component_not_once_per_attempt():
    """`E0616` keeps arriving after the demotion — the authored layout is bad in
    its own right — but the probe is no longer installed, so the second compile
    and the message that blames the probe must not come back. Six compiles for
    three attempts: two calibrations, then one apiece, with the extra
    re-decision only on attempt 1. Reading the demoted gate as still-probing
    cost a compile per attempt and wrote the same explanation three times, twice
    about a probe region that was not in the tree.

    The E0616s name a DIFFERENT field each time on purpose. This test is about
    compile accounting across three attempts, and the retry loop now stops when
    a diagnostic it already answered for comes back byte-identical — scripting
    one repeated string would end the loop at attempt 2 and quietly stop
    measuring the per-attempt invariant this test exists for. The repeat rule
    has its own test below."""
    oracle = {"src/error.gen_tests_1.rs":
              "let e: ErrorImpl = make();\nlet _ = e.vtable.raw;\n"}
    d = _ScriptedDeps(_GOOD, [
        (True, ""),
        (False, "error[E0609]: no field `raw` on type `u8`"),
    ] + [(False, f"error[E0616]: field `raw{i}` of `Vtable` is private")
         for i in range(4)], oracle)
    author_layout(_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert len(d.at_compile) == 6
    assert len([ln for ln in d.logs if "private to another module" in ln]) == 1
    probed = [i for i, tree in enumerate(d.at_compile)
              if "_layout_pin_field_path" in tree.get("src/error.rs", "")]
    assert probed == [1, 2], (
        "only the path calibration and attempt 1 carry a probe region; every "
        "compile after the demotion runs on a tree without one")


# --- the authoring retry loop stops on failures the next attempt cannot fix ---


def test_locate_failure_ends_the_loop_at_the_attempt_that_found_it():
    """A frozen definition that is not in the file to be replaced is not a
    prompting problem, and three samples of the model cannot make it one.

    Run `669347-cf33eae3` (2026-08-15) spent all three attempts on each of
    `('CastTo',)` and `('Mut', 'error_mut')` logging `could not locate a frozen
    definition to replace: … in ptr.rs` — six authoring calls against a lookup
    that had already reported the definition absent. The fire's mechanical
    budget already refuses on exactly this reason, but it is consulted once per
    FIRE and all three attempts happen underneath it."""
    d = _Deps(_GOOD, [True, True, True])
    # The frozen definition the surface wants replaced is not in the file.
    d.files["src/error.rs"] = "struct Unrelated { x: u8 }\n"
    author_layout(_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert len(d.prompts) == 1, (
        "the locate failure is attempt-invariant, so exactly one authoring "
        f"call may be spent on it (spent {len(d.prompts)})")
    assert any("attempt-invariant" in ln for ln in d.logs), (
        "the stop must say why it stopped — a silent early exit reads as a "
        "component that was never tried")


def test_locate_failure_burned_every_attempt_before_the_stop(monkeypatch):
    """Negative control for the test above: with the mechanical classifier
    blinded, the pre-fix behaviour reproduces — three model calls, one per
    attempt, on a failure none of them can move."""
    monkeypatch.setattr("pipeline_core.application.author_layout"
                        ".is_mechanical_reason", lambda _r: False)
    d = _Deps(_GOOD, [True, True, True])
    d.files["src/error.rs"] = "struct Unrelated { x: u8 }\n"
    author_layout(_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert len(d.prompts) == 3
    assert not any("attempt-invariant" in ln for ln in d.logs)


def test_a_repeated_compile_diagnostic_ends_the_loop():
    """The model was told and wrote the same thing again.

    The 34-name construct/vtable component on run `669347-cf33eae3` returned
    `error[E0255]: the name `Backtrace` is defined multiple times` on attempt 1
    and again on attempt 3, with the retry hint carrying it in between. The
    per-impl fill route already stops on an identical signature and says a
    bigger prompt re-purchases the same answer; this route did not."""
    d = _Deps(_GOOD, [False, False, False])   # same E0308 text every compile
    author_layout(_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert len(d.prompts) == 2, (
        "attempt 1 records the diagnostic, attempt 2 reproduces it and stops; "
        f"attempt 3 re-purchases it (spent {len(d.prompts)})")
    assert any("already answered for" in ln for ln in d.logs)


def test_a_repeated_diagnostic_burned_the_third_attempt_before_the_stop(monkeypatch):
    """Negative control: blind the diagnostic reader and the loop runs all
    three attempts against one unchanging compile error, as it did before."""
    monkeypatch.setattr("pipeline_core.application.author_layout"
                        ".first_diagnostic", lambda _t: "")
    d = _Deps(_GOOD, [False, False, False])
    author_layout(_surface(), module_name="anyhow", public_surface="",
                  select_pins=_has_pins, symbol_inventory="", deps=d.deps())
    assert len(d.prompts) == 3
    assert not any("already answered for" in ln for ln in d.logs)


def test_behaviour_suite_reaches_the_authoring_prompt_and_is_logged():
    """The suite `select_pins` draws its STATIC_ASSERTION pins from is also
    the source of the runtime behaviours: the identity behaviour naming the
    public `Error` (whose layout reaches `ErrorImpl`) lands in the prompt's
    MUST section, the pin does not (it has its own section), and one log line
    per component says how many and of which class. Control: without a suite
    the prompt carries no such section and nothing is logged."""
    identity = BehaviourSpec(
        behaviour_id="b-identity", given='let e = Error::msg("x");',
        when="e.is::<&str>()", then=ObservableOutcome.downcast_ok(True),
        subkind=BehaviourSubKind.OUTCOME)
    unrelated = BehaviourSpec(
        behaviour_id="b-other", given="", when="other()",
        then=ObservableOutcome.display("o"), subkind=BehaviourSubKind.OUTCOME)
    d = _Deps(_GOOD, [True])
    res = author_layout(_surface(), module_name="anyhow", public_surface="pub struct Error;",
                        select_pins=_has_pins, symbol_inventory="Box", deps=d.deps(),
                        behaviour_specs=[_pin(), unrelated, identity])
    assert res.all_authored
    prompt = d.prompts[0]
    header = "=== BEHAVIOURS THIS REPRESENTATION MUST MAKE EXPRESSIBLE (MUST) ==="
    assert header in prompt
    section = prompt[prompt.index(header):prompt.index("=== IN-SCOPE SYMBOL INVENTORY ===")]
    assert "- when `e.is::<&str>()` then the downcast is Some (true)" in section
    assert "other()" not in section
    assert "size_of::<Error>()" not in section
    assert any("('ErrorImpl',): 1 behaviour(s) in the authoring prompt (identity 1)" in m
               for m in d.logs), d.logs
    assert not any("cap omitted" in m for m in d.logs)
    # control
    d2 = _Deps(_GOOD, [True])
    author_layout(_surface(), module_name="anyhow", public_surface="pub struct Error;",
                  select_pins=_has_pins, symbol_inventory="Box", deps=d2.deps())
    assert header not in d2.prompts[0]
    assert not any("behaviour(s) in the authoring prompt" in m for m in d2.logs)


def test_authoring_log_names_classes_the_cap_omitted():
    """Cap 40 on 40 exact-format + 4 bounded-resource matching ErrorImpl
    keeps the exact-format slice and drops the entire BOUNDED_RESOURCE
    class (the 40-byte-buffer / whitespace-fallback family). The prompt
    already says `(+N more)`; the [t1.5] line must name the omitted
    class so an operator reading the kept breakdown can see the cut.
    Control: the existing one-identity suite still logs no omitted
    clause (asserted in test_behaviour_suite_reaches_the_authoring_prompt)."""
    exact = [
        BehaviourSpec(
            behaviour_id=f"fmt-{i}", given="",
            when=f"let e = ErrorImpl; format!(\"{{e}}\") {i}",
            then=ObservableOutcome.display(str(i)),
            subkind=BehaviourSubKind.OUTCOME)
        for i in range(40)]
    bounded = [
        BehaviourSpec(
            behaviour_id=f"buf-{i}", given="",
            when=f"let e = ErrorImpl; e.to_string().len() < {40 + i}",
            then=ObservableOutcome.display("x"),
            subkind=BehaviourSubKind.OUTCOME,
            klass=BehaviourClass.BOUNDED_RESOURCE)
        for i in range(4)]
    d = _Deps(_GOOD, [True])
    author_layout(_surface(), module_name="anyhow", public_surface="pub struct Error;",
                  select_pins=_has_pins, symbol_inventory="Box", deps=d.deps(),
                  behaviour_specs=exact + bounded)
    line = next(m for m in d.logs if "behaviour(s) in the authoring prompt" in m)
    assert "40 behaviour(s) in the authoring prompt (exact_format 40)" in line
    assert "cap omitted bounded_resource 4 of 44" in line
