"""Stub-time construction recipes attach by signature, not by escalation.

Plan sentence, computed on the frozen 85-stub frontier of 2026-08-28: the
bound driver's recipe is non-empty exactly for the two signature shapes it
keys on (an unsized boxed trait-object argument beside another argument;
a by-value ``self`` returning ``Self`` of an inherent type), and its text
names no type the crate declares. A forgotten sections field cannot omit
it — the builders derive the recipe from the signature.

Negative control: ``constructs_type("unimplemented!()", "ContextError")`` is
False, and a failed-attempt stub body also does not construct ContextError.
That is why ``producer_bypasses`` missed both units on run
``1383168-eb151d09`` (peel restored unimplemented, parked escalation never
dispatched, ``producer_bypass_offered`` empty).
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.application.reconstruct.fill_rules import (
    AttemptFailure,
    make_failed_attempt_body,
)
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.type_production import constructs_type
from pipeline_core.prompts.context import PerFnSections
from pipeline_core.prompts.per_fn import build_per_fn_prompt

_FRONTIER_STUBS = (
    Path(__file__).resolve().parent / "fixtures" / "behaviours"
    / "identity_must_frontier_stubs_2026-08-28.json"
)


def test_generic_recipes_on_recorded_frontier_pay_from_sig_shape() -> None:
    """Plan rewrite, computed on the same 85-stub records: generic keys on
    the sig, not the two names. A unit named construct_from_boxed still
    wraps because the argument is unsized Box<dyn plus another arg; a
    by-value self returning Self on an inherent impl is identity; every
    other stub is empty. Neither recipe names a wrapper this crate
    declares."""
    stubs = json.loads(_FRONTIER_STUBS.read_text(encoding="utf-8"))
    assert len(stubs) == 85
    drv = language_driver()
    assert drv.name == "generic"
    hits = [
        (s["fn_name"], s["container"],
         drv.construction_recipe(s["fn_name"], s["container"], s["sig_text"]))
        for s in stubs
        if drv.construction_recipe(s["fn_name"], s["container"], s["sig_text"])
    ]
    assert [(n, c) for n, c, _ in hits] == [
        ("construct_from_boxed", "impl Error"),
        ("context", "impl Error"),
    ]
    wrap, ident = hits[0][2], hits[1][2]
    assert "unsized" in wrap
    assert "BoxedError" not in wrap
    assert "StdError" not in wrap
    assert "erased identity type" in ident
    assert "ContextError" not in ident
    # from_boxed is not on this frontier; a single Box<dyn arg does not wrap.
    assert drv.construction_recipe(
        "from_boxed", "impl Error",
        "fn from_boxed(error: Box<dyn StdError + Send + Sync>) -> Self") == ""
    # The same name on a crate that does not declare BoxedError still wraps.
    other = drv.construction_recipe(
        "construct_from_boxed", "impl Cell",
        "fn construct_from_boxed(error: Box<dyn StdError + Send + Sync>, "
        "trace: Option<Trace>) -> Self")
    assert "unsized" in other
    assert "BoxedError" not in other


def test_constructs_type_misses_stubs_which_is_why_bypass_never_fired() -> None:
    """producer_bypasses keys on constructs_type of the current body.
    Peel restores unimplemented!(); a failed-attempt comment quotes the
    refused body but does not construct ContextError either."""
    assert not constructs_type("unimplemented!()", "ContextError")
    peeled = make_failed_attempt_body(
        "crate::context::ext::StdError::ext_context(self, context)",
        "error[E0603]: module `ext` is private",
        3, failure=AttemptFailure.COMPILE)
    assert not constructs_type(peeled, "ContextError")
    boxed_fail = make_failed_attempt_body(
        "Self::construct_from_std(error, backtrace)",
        "error[E0277]: the size for values of type `dyn StdError + Send + Sync` cannot be known",
        3, failure=AttemptFailure.COMPILE)
    assert not constructs_type(boxed_fail, "BoxedError")
    assert "did not compile — do not repeat this approach" in boxed_fail


def test_generic_builders_pay_from_sig_on_a_crate_that_does_not_declare_the_wrappers() -> None:
    """Live builders under generic: wrap/identity from the sig, no
    BoxedError / ContextError / catalog recipe text."""
    fn_empty = {"macros": "", "notes": "", "container": "impl Cell",
                "types": "", "invariants": "", "siblings": "",
                "crate_idioms": "", "bound_methods": ""}
    wrap_sig = (
        "fn from_payload(error: Box<dyn StdError + Send + Sync>, "
        "trace: Option<Trace>) -> Self")
    wrap_from_sig = (
        "the argument is unsized; wrap it in a sized type this crate already "
        "declares")
    wrap_p = build_per_fn_prompt(
        file_text="fn from_payload() { unimplemented!() }",
        fn_name="from_payload",
        sig_text=wrap_sig,
        test_blocks=[],
        module_name="demo", cluster_name="stable",
        file_rel="src/cell.rs", fr_path=None,
        sections=PerFnSections(**fn_empty))
    assert wrap_from_sig in wrap_p
    assert "BoxedError" not in wrap_p
    already = build_per_fn_prompt(
        file_text="fn from_boxed() { unimplemented!() }",
        fn_name="from_boxed",
        sig_text="fn from_boxed(error: Box<dyn StdError + Send + Sync>) -> Self",
        test_blocks=[],
        module_name="demo", cluster_name="stable",
        file_rel="src/cell.rs", fr_path=None,
        sections=PerFnSections(**fn_empty))
    # RECURRING_ERRORS names a possibly-unsized generic T; that is not
    # the wrap recipe. from_boxed must not pay wrap-from-sig.
    assert wrap_from_sig not in already
    ident = build_per_fn_prompt(
        file_text="fn and_then() { unimplemented!() }",
        fn_name="and_then",
        sig_text="fn and_then<C>(self, extra: C) -> Self",
        test_blocks=[],
        module_name="demo", cluster_name="stable",
        file_rel="src/cell.rs", fr_path=None,
        sections=PerFnSections(**fn_empty))
    assert "erased identity type" in ident
    assert "ContextError" not in ident


def test_block_recipes_are_unique_per_name_and_follow_the_signature() -> None:
    """The per-impl splice: one recipe per name, in order, a repeated name
    once, a name with no signature none."""
    from pipeline_core.domain.construction_recipe import (
        construction_recipe_prefix,
        construction_recipes_for,
    )
    wrap_sig = ("fn from_payload(error: Box<dyn StdError + Send + Sync>, "
                "trace: Option<Trace>) -> Self")
    ident_sig = "fn and_then<C>(self, extra: C) -> Self"
    out = construction_recipes_for(
        ("from_payload", "and_then", "from_payload", "plain"), "impl Cell",
        {"from_payload": wrap_sig, "and_then": ident_sig,
         "plain": "fn plain(&self) -> u8"})
    parts = out.split("\n\n")
    assert len(parts) == 2
    assert "unsized" in parts[0] and "erased identity type" in parts[1]
    assert construction_recipes_for(("from_payload",), "impl Cell") == ""
    assert construction_recipe_prefix("from_payload", "impl Cell", wrap_sig).endswith("\n\n")
    assert construction_recipe_prefix("plain", "impl Cell", "fn plain(&self) -> u8") == ""
