"""Pins for the t1.5 layout-pin compile gate (`domain/layout_probe`).

Before this gate the pins were prompt-advisory only: t1.5 accepted any authored
layout that passed `cargo check --lib`, and a wrong-sized private layout was
discovered a whole sweep later as repr failures blamed on fn bodies."""
from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    FenceDirective,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.layout_pins import harvest_layout_pin_behaviours
from pipeline_core.domain.layout_probe import (
    build_field_path_probe,
    build_layout_probe,
)
from pipeline_core.domain.oracle_field_paths import FieldPath


def _pin(kind: StaticAssertKind, expr: str) -> BehaviourSpec:
    return BehaviourSpec(
        behaviour_id=f"static::{expr}", given="", when=expr,
        then=ObservableOutcome.static_assertion(kind, expr),
        provenance=BehaviourProvenance.HARVESTED,
        subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN)


def test_size_pin_becomes_a_const_assertion() -> None:
    p = build_layout_probe([_pin(
        StaticAssertKind.SIZE,
        "assert_eq!(mem::size_of::<Error>(), mem::size_of::<usize>());")])
    assert p.checks_anything
    assert "const _: () = assert!(" in p.source
    # `mem::` is qualified so the probe does not depend on the crate's imports
    # (and works under no_std, where `std::mem` does not exist).
    assert "core::mem::size_of::<Error>()" in p.source
    assert "core::mem::size_of::<usize>()" in p.source
    assert p.skipped == ()


def test_niche_pin_keeps_nested_generics_in_one_operand() -> None:
    # `Result<(), Error>` contains a top-level-looking comma; splitting the
    # macro args naively would compare `size_of::<Result<()` to ` Error>>()`.
    p = build_layout_probe([_pin(
        StaticAssertKind.NICHE,
        "assert_eq!(mem::size_of::<Result<(), Error>>(), "
        "mem::size_of::<usize>());")])
    assert "core::mem::size_of::<Result<(), Error>>()" in p.source
    assert p.gated and not p.skipped


def test_autotrait_pin_is_applied_to_the_pinned_public_types() -> None:
    # The generic helper names the BOUNDS but not the type; the type comes from
    # the component's public surface, not from re-parsing the call line.
    p = build_layout_probe(
        [_pin(StaticAssertKind.AUTOTRAITS,
              "fn assert<E: Unpin + Send + Sync + 'static>() {}")],
        pinned_types=("Error",))
    assert "fn _a<T: Unpin + Send + Sync + 'static>() {}" in p.source
    assert "_a::<Error>();" in p.source


def test_autotrait_macro_form_carries_its_own_type() -> None:
    p = build_layout_probe(
        [_pin(StaticAssertKind.AUTOTRAITS, "assert_impl_all!(Error: Send, Sync);")])
    assert "fn _a<T: Send + Sync>() {}" in p.source
    assert "_a::<Error>();" in p.source


def test_unrenderable_pin_is_reported_not_silently_dropped() -> None:
    # An unchecked pin must be NAMED: silently gating nothing looks identical to
    # gating everything, which is how the advisory-pins gap survived so long.
    p = build_layout_probe([_pin(StaticAssertKind.SIZE, "some_prose_line")])
    assert not p.checks_anything
    assert p.skipped == ("some_prose_line",)


def test_autotrait_pin_without_a_pinned_type_is_skipped() -> None:
    p = build_layout_probe(
        [_pin(StaticAssertKind.AUTOTRAITS, "fn assert<E: Send>() {}")],
        pinned_types=())
    assert not p.checks_anything and len(p.skipped) == 1


def test_real_harvested_repr_test_renders_end_to_end() -> None:
    # The exact shape of the crate's own layout test, through the real
    # harvester — the classifier and the probe must agree on the pin text.
    src = (
        "#[test]\n"
        "fn test_error_size() {\n"
        "    assert_eq!(mem::size_of::<Error>(), mem::size_of::<usize>());\n"
        "}\n")
    pins = harvest_layout_pin_behaviours(src, source_file="tests/test_repr.rs")
    p = build_layout_probe(pins, pinned_types=("Error",))
    assert p.checks_anything
    assert p.source.count("const _: () = assert!(") == 1


def test_probe_install_is_idempotent_and_fully_removable(tmp_path) -> None:
    # An interrupted run must not leave a half-probe that the next install
    # nests inside itself, and removal must restore lib.rs byte-for-byte.
    from pipeline_core.adapters.skeleton_compile import (
        skeleton_src_dir_for,
        write_pin_probe,
    )
    src = skeleton_src_dir_for(tmp_path, "stable")
    src.mkdir(parents=True)
    original = "pub struct Error { inner: usize }\n"
    (src / "lib.rs").write_text(original, encoding="utf-8")

    write_pin_probe(tmp_path, "stable", "const _: () = assert!(1 == 1);")
    once = (src / "lib.rs").read_text(encoding="utf-8")
    assert "const _: () = assert!(1 == 1);" in once and original.strip() in once

    write_pin_probe(tmp_path, "stable", "const _: () = assert!(2 == 2);")
    twice = (src / "lib.rs").read_text(encoding="utf-8")
    assert twice.count("__LAYOUT_PIN_PROBE_BEGIN__") == 1
    assert "assert!(1 == 1)" not in twice

    write_pin_probe(tmp_path, "stable", None)
    assert (src / "lib.rs").read_text(encoding="utf-8") == original


def test_probe_removal_on_a_crate_without_lib_rs_is_a_noop(tmp_path) -> None:
    from pipeline_core.adapters.skeleton_compile import write_pin_probe
    write_pin_probe(tmp_path, "stable", None)  # must not raise


def test_malformed_pin_text_never_renders_a_broken_probe() -> None:
    # Every one of these is a pin line the classifier could plausibly hand over
    # from odd test source. None may produce Rust — a probe that does not parse
    # would fail calibration and disable gating for the whole component, so
    # returning "unrenderable" here keeps the other pins working.
    for bad in (
        "assert_eq!(mem::size_of::<Error>(",          # macro paren never closes
        "assert_eq!(only_one_argument);",             # no comparison pair
        "assert_eq!(, );",                            # empty operands
        "assert!(no_comparison_here);",               # assert! without `==`
        "assert_eq",                                  # no argument list at all
    ):
        p = build_layout_probe([_pin(StaticAssertKind.SIZE, bad)])
        assert not p.checks_anything, bad
        assert p.skipped == (bad,)


def test_malformed_autotrait_text_is_skipped() -> None:
    for bad in (
        "fn assert() {}",                     # no generic parameter list
        "fn assert<E() {}",                   # generic list never closes
        "fn assert<E>() {}",                  # parameter carries no bounds
        "fn assert<A: Send, B: Sync>() {}",   # ambiguous: two bounded params
        "fn assert<E: >() {}",                # empty bound list
    ):
        p = build_layout_probe([_pin(StaticAssertKind.AUTOTRAITS, bad)],
                               pinned_types=("Error",))
        assert not p.checks_anything, bad


def test_already_qualified_mem_path_is_left_alone() -> None:
    # `std::mem::` / `core::mem::` must not become `std::core::mem::`, and an
    # identifier merely ENDING in `mem` is not a path segment.
    p = build_layout_probe([_pin(
        StaticAssertKind.SIZE,
        "assert_eq!(std::mem::size_of::<Error>(), core::mem::size_of::<usize>());")])
    assert "std::mem::size_of" in p.source
    assert "core::mem::size_of::<usize>()" in p.source
    assert "std::core::mem" not in p.source


def test_bare_assert_with_comparison_renders() -> None:
    p = build_layout_probe([_pin(
        StaticAssertKind.ALIGN,
        "assert!(mem::align_of::<Error>() == 8);")])
    assert "const _: () = assert!(core::mem::align_of::<Error>() == 8);" in p.source


def test_non_static_assertion_behaviours_are_ignored() -> None:
    # The pin list is filtered by the caller, but a RUNTIME behaviour slipping
    # in must be skipped silently rather than rendered as a bogus const assert.
    from pipeline_core.domain.behaviour import OutcomeKind
    spec = BehaviourSpec(
        behaviour_id="rt", given="", when="x",
        then=ObservableOutcome(kind=OutcomeKind.DISPLAY_STRING, value="boom"),
        provenance=BehaviourProvenance.HARVESTED,
        subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN)
    p = build_layout_probe([spec])
    assert not p.checks_anything and p.skipped == ()


def test_macro_name_without_an_argument_list_is_skipped() -> None:
    p = build_layout_probe([_pin(StaticAssertKind.SIZE, "assert_eq! [1, 2]")])
    assert not p.checks_anything


def test_balanced_span_rejects_a_start_that_is_not_the_opening_bracket() -> None:
    # Defensive guard: every caller passes a located bracket index, but a
    # future caller passing a stale offset must get -1 (unrenderable) rather
    # than a silently wrong span that splices malformed Rust.
    from pipeline_core.domain.layout_probe import _balanced_span
    assert _balanced_span("size_of::<T>()", 0, "<", ">") == -1
    assert _balanced_span("", 0, "(", ")") == -1
    assert _balanced_span("<T>", 0, "<", ">") == 3


# --- the oracle's member paths as a compile gate ------------------------------
# `cargo check --lib` never builds `#[cfg(test)]` code, so a layout that renames
# a field the crate's own write-protected tests read passes the pin gate and
# fails at the first `cargo test` — on `anyhow` (2026-08-06) with
# `error[E0609]: no field 'inner' on type 'Error'`, taking all 56 unit tests of
# both clusters with it. These probes move that question back to the gate.

def test_field_path_probe_renders_a_borrow_per_path() -> None:
    p = build_field_path_probe([FieldPath("Error", ("inner", "ptr"))])
    assert p.checks_anything
    assert "fn _layout_pin_field_path_0(v: &Error) { let _ = &v.inner.ptr; }" in p.source
    assert p.gated == ("Error.inner.ptr",)
    assert p.skipped == ()


def test_field_path_probe_needs_no_constructor() -> None:
    """A borrow through a reference parameter, so the probe is answerable by a
    skeleton whose every body is `unimplemented!()` — the tree it runs on."""
    src = build_field_path_probe([FieldPath("Error", ("inner",))]).source
    assert "&Error" in src and "Error::" not in src and "new(" not in src


def test_field_path_probe_numbers_each_item_uniquely() -> None:
    p = build_field_path_probe([
        FieldPath("Error", ("inner", "ptr")), FieldPath("Error", ("vtable",))])
    assert "_layout_pin_field_path_0" in p.source
    assert "_layout_pin_field_path_1" in p.source
    assert p.gated == ("Error.inner.ptr", "Error.vtable")


def test_field_path_probe_skips_a_generic_root_instead_of_guessing() -> None:
    """`fn p(v: &ContextError)` on a root declared `ContextError<C, E>` is
    E0107 — the probe would be wrong about itself, and a probe that cannot
    compile for its own reasons must not be allowed to reject a layout."""
    p = build_field_path_probe(
        [FieldPath("ContextError", ("error",))],
        generic_roots=frozenset({"ContextError"}))
    assert not p.checks_anything
    assert p.skipped == ("ContextError.error",)
    assert p.source == ""


def test_field_path_probe_skips_a_rootless_path() -> None:
    p = build_field_path_probe([FieldPath("Error", ())])
    assert not p.checks_anything and p.skipped == ("Error",)


def test_field_path_probe_gates_what_it_can_and_names_what_it_cannot() -> None:
    p = build_field_path_probe(
        [FieldPath("Error", ("inner",)), FieldPath("ContextError", ("error",))],
        generic_roots=frozenset({"ContextError"}))
    assert p.gated == ("Error.inner",)
    assert p.skipped == ("ContextError.error",)


def test_no_paths_renders_no_probe() -> None:
    assert not build_field_path_probe([]).checks_anything
