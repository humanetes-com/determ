"""Stub-time construction recipes attach by signature, not by escalation.

Plan sentence, computed on the frozen 85-stub frontier of 2026-08-28: the
recipe is non-empty exactly for ``construct_from_boxed`` and ``Error::context``.
``ext_context``, ``Result::context``, ``from_boxed``, wrapper ``fmt``, and
``construct_from_std`` stay empty. A forgotten sections field cannot omit
them — the builders derive the recipe from name + container.

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
    make_failed_attempt_body,
)
from pipeline_core.domain.construction_recipe import (
    CHAIN_TABLE_RECIPE,
    WRAP_BOX_RECIPE,
    construction_recipe,
)
from pipeline_core.domain.models import FnRecord, ImplBlock
from pipeline_core.domain.type_production import constructs_type
from pipeline_core.prompts.context import PerFnSections, PerImplSections
from pipeline_core.prompts.fix_prompts import build_maximal_impl_prompt
from pipeline_core.prompts.per_fn import build_per_fn_prompt
from pipeline_core.prompts.per_impl import build_per_impl_prompt

_FRONTIER_STUBS = (
    Path(__file__).resolve().parent / "fixtures" / "behaviours"
    / "identity_must_frontier_stubs_2026-08-28.json"
)


def test_recipes_on_recorded_frontier_are_exactly_boxed_and_error_context() -> None:
    """The plan's fixture, not a hand-made list of two names."""
    stubs = json.loads(_FRONTIER_STUBS.read_text(encoding="utf-8"))
    assert len(stubs) == 85
    hits = [
        (s["fn_name"], s["container"])
        for s in stubs
        if construction_recipe(s["fn_name"], s["container"])
    ]
    assert hits == [
        ("construct_from_boxed", "impl Error"),
        ("context", "impl Error"),
    ]
    boxed = next(s for s in stubs if s["fn_name"] == "construct_from_boxed")
    ctx = next(s for s in stubs
               if s["fn_name"] == "context" and s["container"] == "impl Error")
    assert construction_recipe(boxed["fn_name"], boxed["container"]) == WRAP_BOX_RECIPE
    assert construction_recipe(ctx["fn_name"], ctx["container"]) == CHAIN_TABLE_RECIPE


def test_recipe_refuses_the_near_misses_the_plan_named() -> None:
    """from_boxed must keep calling construct_from_boxed, not wrap a second
    time. Result::context and ext_context are different self types / names.
    construct_from_std is the call that just failed; wrapper fmt is TypeId
    after a body exists."""
    assert construction_recipe("from_boxed", "impl Error") == ""
    assert construction_recipe(
        "context", "impl<T, E> Context<T, E> for Result<T, E>") == ""
    assert construction_recipe("ext_context", "impl StdError for E") == ""
    assert construction_recipe("ext_context", "impl StdError for Error") == ""
    assert construction_recipe("construct_from_std", "impl Error") == ""
    assert construction_recipe("fmt", "impl Debug for BoxedError") == ""
    assert construction_recipe("context", "") == ""


def test_constructs_type_misses_stubs_which_is_why_bypass_never_fired() -> None:
    """producer_bypasses keys on constructs_type of the current body.
    Peel restores unimplemented!(); a failed-attempt comment quotes the
    refused body but does not construct ContextError either."""
    assert not constructs_type("unimplemented!()", "ContextError")
    peeled = make_failed_attempt_body(
        "crate::context::ext::StdError::ext_context(self, context)",
        "error[E0603]: module `ext` is private",
        3)
    assert not constructs_type(peeled, "ContextError")
    boxed_fail = make_failed_attempt_body(
        "Self::construct_from_std(error, backtrace)",
        "error[E0277]: the size for values of type `dyn StdError + Send + Sync` cannot be known",
        3)
    assert not constructs_type(boxed_fail, "BoxedError")
    assert "did not compile — do not repeat this approach" in boxed_fail


def test_per_fn_and_per_impl_builders_derive_the_recipe_without_a_sections_field() -> None:
    """A forgotten construction_recipe kwarg cannot omit it: the builder
    computes from name + container. msg (no recipe) stays free of both
    sentences so identity-must characterization stays byte-stable."""
    fn_empty = {"macros": "", "notes": "", "container": "impl Error",
                "types": "", "invariants": "", "siblings": "",
                "crate_idioms": "", "bound_methods": ""}
    ctx = build_per_fn_prompt(
        file_text="fn context() { unimplemented!() }",
        fn_name="context",
        sig_text="fn context<C>(self, context: C) -> Self",
        test_blocks=[],
        module_name="anyhow", cluster_name="stable",
        file_rel="src/error.rs", fr_path=None,
        sections=PerFnSections(**fn_empty))
    assert CHAIN_TABLE_RECIPE in ctx
    assert "BUILD THE PER-TYPE TABLE" not in ctx
    sig_at = ctx.index("fn context<C>(self, context: C) -> Self")
    gotchas_at = ctx.index("<RUST_GOTCHAS>")
    assert CHAIN_TABLE_RECIPE in ctx[sig_at:gotchas_at]

    boxed = build_per_fn_prompt(
        file_text="fn construct_from_boxed() { unimplemented!() }",
        fn_name="construct_from_boxed",
        sig_text="fn construct_from_boxed(error: Box<dyn StdError + Send + Sync>) -> Self",
        test_blocks=[],
        module_name="anyhow", cluster_name="stable",
        file_rel="src/error.rs", fr_path=None,
        sections=PerFnSections(**fn_empty))
    assert WRAP_BOX_RECIPE in boxed
    assert "Do not call `construct_from_std`" in boxed

    msg = build_per_fn_prompt(
        file_text="fn msg() { unimplemented!() }",
        fn_name="msg",
        sig_text="fn msg<M>(message: M) -> Self",
        test_blocks=[],
        module_name="anyhow", cluster_name="stable",
        file_rel="src/error.rs", fr_path=None,
        sections=PerFnSections(**fn_empty))
    assert CHAIN_TABLE_RECIPE not in msg
    assert WRAP_BOX_RECIPE not in msg

    stub = FnRecord(
        file="src/error.rs", fn_name="context", container="impl Error",
        sig_text="fn context<C>(self, context: C) -> Self",
        has_unimplemented_body=True)
    empty = {"types": "", "invariants": "", "notes": "", "crate_idioms": "",
             "dispatch_family": "", "macros": "", "siblings": "",
             "retrospective_guidance": "", "bound_methods": ""}
    impl_p = build_per_impl_prompt(
        ImplBlock(file="src/error.rs", container="impl Error",
                  members=(stub,), stub_members=(stub,)),
        [], [], "anyhow", "stable",
        PerImplSections(**empty), fr_path=None)
    stub_at = impl_p.index("Stubs to fill in this response: context")
    impl_gotchas = impl_p.index("<RUST_GOTCHAS>")
    assert CHAIN_TABLE_RECIPE in impl_p[stub_at:impl_gotchas]

    max_p = build_maximal_impl_prompt(
        file_rel="src/error.rs", container="impl Error",
        stub_names=["context", "construct_from_boxed"],
        file_text="fn context(){}", tests_block="", errs="e", notes="")
    file_at = max_p.index("File: src/error.rs")
    rust_at = max_p.index("```rust")
    slot = max_p[file_at:rust_at]
    assert CHAIN_TABLE_RECIPE in slot
    assert WRAP_BOX_RECIPE in slot
