"""Generic vs catalog language driver — the fill model is the language expert.

Plan: LANGUAGE_DRIVER=generic (default) injects no std-trait tutorial, no
Display/Debug name-set backup, no standard derive set, no Send/Sync
tutorial, and a fmt-only follow default. The catalog implementations stay
on disk; CatalogLanguageDriver is the A/B that calls them. When generic
is bound those functions are not called.

Negative control: the same call sites under CatalogLanguageDriver still
produce the pre-2026-09-09 tables.
"""
from __future__ import annotations

import inspect
from collections.abc import Iterator

import pytest

from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.enums import LanguageDriverKind
from pipeline_core.domain.language_driver import (
    GenericLanguageDriver,
    bind,
    language_driver,
    using_driver,
)
from pipeline_core.domain.models import FnRecord, ImplBlock
from pipeline_core.domain.private_surface import (
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.prompts.context import PerFnSections, PerImplSections
from pipeline_core.prompts.language_catalog import catalog_driver, driver_from_kind
from pipeline_core.prompts.layout_authoring import build_layout_authoring_prompt
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt, build_per_fn_prompt
from pipeline_core.prompts.per_impl import build_per_impl_prompt
from pipeline_core.settings import Settings


@pytest.fixture(autouse=True)
def _generic_driver() -> Iterator[None]:
    bind(GenericLanguageDriver())
    yield
    bind(GenericLanguageDriver())


def _fn_sections(container: str) -> PerFnSections:
    return PerFnSections(
        macros="", notes="", container=container, types="",
        invariants="", siblings="", crate_idioms="", bound_methods="")


def _impl_sections() -> PerImplSections:
    return PerImplSections(
        types="", invariants="", notes="", crate_idioms="",
        dispatch_family="", macros="", siblings="",
        retrospective_guidance="", bound_methods="")


def _layout_prompt(**overrides):
    items = (
        PrivateSurfaceItem(SurfaceItemKind.STRUCT, "Cell", "src/cell.rs",
                           "struct Cell { .. }"),
    )
    kwargs = {
        "module_name": "demo", "cfg_cluster": "stable", "items": items,
        "public_surface": "pub struct Cell { .. }",
        "layout_pins": [], "symbol_inventory": "Box",
    }
    kwargs.update(overrides)
    return build_layout_authoring_prompt(**kwargs)


def test_default_bound_driver_is_generic() -> None:
    assert language_driver().name == "generic"
    assert isinstance(language_driver(), GenericLanguageDriver)


def test_driver_from_kind_selects_catalog_only_for_catalog() -> None:
    assert driver_from_kind(LanguageDriverKind.GENERIC).name == "generic"
    assert driver_from_kind(LanguageDriverKind.CATALOG).name == "catalog"
    # coerce fallback is generic (unknown token must not resurrect the catalog).
    assert LanguageDriverKind.coerce("nope", LanguageDriverKind.GENERIC) is (
        LanguageDriverKind.GENERIC)


def test_settings_language_driver_defaults_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGUAGE_DRIVER", raising=False)
    assert Settings.from_env().language_driver is LanguageDriverKind.GENERIC
    monkeypatch.setenv("LANGUAGE_DRIVER", "catalog")
    assert Settings.from_env().language_driver is LanguageDriverKind.CATALOG
    monkeypatch.setenv("LANGUAGE_DRIVER", "GENERIC")
    assert Settings.from_env().language_driver is LanguageDriverKind.GENERIC
    monkeypatch.setenv("LANGUAGE_DRIVER", "not-a-driver")
    assert Settings.from_env().language_driver is LanguageDriverKind.GENERIC


def test_generic_driver_source_does_not_name_the_catalog() -> None:
    """The generic class body has no import of the tables and no
    Display/Debug/Send/Sync branch — a grep of its source is the
    check that it cannot grow a sibling catalog."""
    src = inspect.getsource(GenericLanguageDriver)
    for banned in (
        "TRAIT_CONTRACTS", "trait_contract_for", "_FORMAT_TRAITS",
        "catalog_is_formatting", "_DERIVED_TRAIT_RULE", "_AUTOTRAIT_RULE",
        "Display", "Debug", "Hash", "Drop", "Send", "Sync", "Default",
        "PartialEq", "ErrorImpl", "construct_from_boxed", "CONVERSION_FAMILY",
        "WRAP_BOX", "CHAIN_TABLE", "ErrorVTable", "ManuallyDrop",
        "BoxedError", "CLASS_PROMPTS", "into_boxed_dyn_error",
        "RUST_GOTCHAS", "ADHOC_VTABLE", "ContextValue", "object_downcast",
        "error_generic_member_access", "ContextError", "cfg_name_vs_feature",
    ):
        assert banned not in src, banned


def test_generic_never_calls_trait_contract_for(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.prompts import sections

    def boom(_container: str) -> str:
        raise AssertionError("catalog trait_contract_for was called")

    monkeypatch.setattr(sections, "trait_contract_for", boom)
    assert language_driver().trait_contract("impl Display for X") == ""
    assert language_driver().trait_contract("impl Hash for K") == ""
    prompt = build_per_fn_prompt(
        file_text="fn fmt() { unimplemented!() }",
        fn_name="fmt",
        sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
        test_blocks=[],
        module_name="demo", cluster_name="stable",
        file_rel="src/x.rs", fr_path=None,
        sections=_fn_sections("impl Display for X"))
    assert "<TRAIT_CONTRACT>" not in prompt


def test_generic_never_calls_catalog_is_formatting(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain import behaviour_select as bs

    def boom(_container: str, _sig: str) -> bool:
        raise AssertionError("catalog_is_formatting was called")

    monkeypatch.setattr(bs, "catalog_is_formatting", boom)
    monkeypatch.setattr(bs, "_FORMAT_TRAITS", frozenset({"should-not-be-read"}))
    drv = language_driver()
    # Write returning fmt::Result is formatting without naming Display/Debug.
    assert drv.is_formatting(
        "impl Write for Buf",
        "fn write_str(&mut self, s: &str) -> fmt::Result")
    # A Display impl whose parsed return is not fmt::Result is not
    # formatting — the trait name is not a backup for a parser miss.
    assert not drv.is_formatting(
        "impl Display for X", "fn fmt(&self, f: &mut Formatter)")
    assert not drv.is_formatting(
        "impl Hash for K", "fn hash<H: Hasher>(&self, state: &mut H)")


def test_generic_never_reads_layout_catalog_rules(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.prompts import layout_authoring as la

    monkeypatch.setattr(la, "_DERIVED_TRAIT_RULE", "SHOULD-NOT-APPEAR-DERIVE")
    monkeypatch.setattr(la, "_AUTOTRAIT_RULE", "SHOULD-NOT-APPEAR-AUTOTRAIT")
    assert language_driver().derived_trait_rule() == ""
    assert language_driver().autotrait_rule() == ""
    p = _layout_prompt()
    assert "SHOULD-NOT-APPEAR-DERIVE" not in p
    assert "SHOULD-NOT-APPEAR-AUTOTRAIT" not in p
    assert "TRAITS THE TESTS CALL MUST BE DERIVED" not in p
    assert "When in doubt" not in p
    assert "#[derive(Debug, Default, Clone, PartialEq, Eq)]" not in p
    assert "AUTO-TRAITS ARE PART OF THE FROZEN PUBLIC API" not in p


def test_generic_layout_prompt_still_surfaces_a_harvested_autotrait_pin() -> None:
    """The pin is the gate; the Send/Sync tutorial is not."""
    pin = BehaviourSpec(
        "p", "", "assert_impl_all!(Handle: Send, Sync)",
        ObservableOutcome.static_assertion(
            StaticAssertKind.AUTOTRAITS,
            "assert_impl_all!(Handle: Send, Sync)"),
        subkind=BehaviourSubKind.OUTCOME)
    p = _layout_prompt(layout_pins=[pin])
    assert "assert_impl_all!(Handle: Send, Sync)" in p
    assert "AUTO-TRAITS ARE PART OF THE FROZEN PUBLIC API" not in p
    assert "unsafe impl Send for Handle {}" not in p


def test_generic_builders_omit_trait_contract_for_known_and_unknown_traits() -> None:
    """Plan: no TRAIT_CONTRACT section for a known std trait or an unknown
    one. Negative control of the catalog: the catalog function still
    returns Display::fmt for the same container (the table is not deleted)."""
    from pipeline_core.prompts.sections import trait_contract_for

    assert "Display::fmt" in trait_contract_for("impl Display for X")
    assert "From<T>::from" in trait_contract_for("impl<T> From<T> for X")
    for container, fn_name, sig in (
        ("impl Display for X", "fmt",
         "fn fmt(&self, f: &mut Formatter) -> fmt::Result"),
        ("impl<T> From<T> for X", "from", "fn from(value: T) -> Self"),
        ("impl UnknownTrait for X", "go", "fn go(&self)"),
        ("impl Hash for K", "hash",
         "fn hash<H: Hasher>(&self, state: &mut H)"),
    ):
        p = build_per_fn_prompt(
            file_text=f"fn {fn_name}() {{ unimplemented!() }}",
            fn_name=fn_name, sig_text=sig, test_blocks=[],
            module_name="demo", cluster_name="stable",
            file_rel="src/x.rs", fr_path=None,
            sections=_fn_sections(container))
        assert "<TRAIT_CONTRACT>" not in p, container
        stub = FnRecord(
            file="src/x.rs", fn_name=fn_name, container=container,
            sig_text=sig, has_unimplemented_body=True)
        impl_p = build_per_impl_prompt(
            ImplBlock(file="src/x.rs", container=container,
                      members=(stub,), stub_members=(stub,)),
            [], [], "demo", "stable", _impl_sections(), fr_path=None)
        assert "<TRAIT_CONTRACT>" not in impl_p, container


def test_generic_fix_prompt_omits_trait_contract() -> None:
    from pipeline_core.prompts.context import PerFnFixSections

    sections = PerFnFixSections(
        macros="", notes="", container="impl Display for X", types="",
        invariants="", siblings="", crate_idioms="", bound_methods="")
    p = build_per_fn_fix_prompt(
        file_text="fn fmt() { unimplemented!() }",
        fn_name="fmt",
        sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
        test_blocks=[],
        module_name="demo", cluster_name="stable",
        file_rel="src/x.rs", prev_body="write!(f, \"x\")",
        error_excerpt="error[E0277]",
        sections=sections, fr_path=None)
    assert "<TRAIT_CONTRACT>" not in p


def test_catalog_driver_is_the_tables() -> None:
    """Negative control: catalog still produces the pre-generic payloads."""
    with using_driver(catalog_driver()):
        drv = language_driver()
        assert drv.name == "catalog"
        assert "Display::fmt" in drv.trait_contract("impl Display for X")
        assert "From<T>::from" in drv.trait_contract("impl<T> From<T> for X")
        assert drv.trait_contract("impl UnknownTrait for X") == ""
        # Display with no parsed fmt::Result return is formatting under
        # the name-set backup — that is the catalog the generic path drops.
        assert drv.is_formatting(
            "impl Display for X", "fn fmt(&self, f: &mut Formatter)")
        assert drv.format_follow_default() == ("fmt", "debug")
        assert "TRAITS THE TESTS CALL MUST BE DERIVED" in drv.derived_trait_rule()
        assert "#[derive(Debug, Default, Clone, PartialEq, Eq)]" in (
            drv.derived_trait_rule())
        assert "AUTO-TRAITS ARE PART OF THE FROZEN PUBLIC API" in (
            drv.autotrait_rule())


def test_catalog_builders_inject_trait_contract() -> None:
    with using_driver(catalog_driver()):
        p = build_per_fn_prompt(
            file_text="fn fmt() { unimplemented!() }",
            fn_name="fmt",
            sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
            test_blocks=[],
            module_name="demo", cluster_name="stable",
            file_rel="src/x.rs", fr_path=None,
            sections=_fn_sections("impl Display for X"))
        assert "<TRAIT_CONTRACT>" in p
        assert "Display::fmt" in p
        stub = FnRecord(
            file="src/x.rs", fn_name="fmt",
            container="impl Display for X",
            sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
            has_unimplemented_body=True)
        impl_p = build_per_impl_prompt(
            ImplBlock(file="src/x.rs", container="impl Display for X",
                      members=(stub,), stub_members=(stub,)),
            [], [], "demo", "stable", _impl_sections(), fr_path=None)
        assert "<TRAIT_CONTRACT>" in impl_p
        assert "Display::fmt" in impl_p


def test_catalog_layout_prompt_still_carries_the_tutorials() -> None:
    with using_driver(catalog_driver()):
        p = _layout_prompt()
        assert "TRAITS THE TESTS CALL MUST BE DERIVED" in p
        assert "#[derive(Debug, Default, Clone, PartialEq, Eq)]" in p
        assert "AUTO-TRAITS ARE PART OF THE FROZEN PUBLIC API" in p
        assert "unsafe impl Send for Handle {}" in p


def test_format_follow_default_generic_is_fmt_only() -> None:
    """Plan: omitted methods follow `fmt` only. Catalog still follows
    fmt|debug (the pre-complement hop). A Qual::debug body is empty
    under generic and named under catalog."""
    from pipeline_core.application.reconstruct.attribution import (
        _format_impl_calls,
    )
    body = "\n        Inner::debug(self.inner.by_ref(), f)\n    "
    assert language_driver().format_follow_default() == ("fmt",)
    assert _format_impl_calls(body) == frozenset()
    with using_driver(catalog_driver()):
        assert language_driver().format_follow_default() == ("fmt", "debug")
        assert _format_impl_calls(body) == {("Inner", "debug")}
    display_body = "\n        Inner::display(self.inner.by_ref(), f)\n    "
    assert _format_impl_calls(display_body, ("fmt", "debug")) == frozenset()


def test_using_driver_restores_the_previous_driver() -> None:
    assert language_driver().name == "generic"
    with using_driver(catalog_driver()):
        assert language_driver().name == "catalog"
    assert language_driver().name == "generic"


# --- generic pays from shape; catalog keeps today's table ---


def test_generic_class_prompts_do_not_name_a_render_or_a_std_trait() -> None:
    """Plan: EXACT_FORMAT scarcity is env-moving, not Debug. TRAIT_OBJECT
    identity does not name a std Error trait object."""
    drv = language_driver()
    on = drv.class_prompts(pin_env_debug=True)
    off = drv.class_prompts(pin_env_debug=False)
    assert on == off
    assert "never Display" not in on["EXACT_FORMAT"]
    assert "Debug" not in on["EXACT_FORMAT"]
    assert "{:?}" not in on["EXACT_FORMAT"]
    assert "std::error::Error" not in on["TRAIT_OBJECT_IDENTITY"]
    assert "dyn " not in on["TRAIT_OBJECT_IDENTITY"]


def test_generic_exact_format_observe_accepts_display_and_covers_env_movers() -> None:
    from pipeline_core.domain.behaviour_probe import (
        CapturedProbe,
        exact_format_ask_rejects,
        probe_coverage_class,
    )
    display = CapturedProbe("u", "let v = crate_x::go();", "v", "display")
    assert exact_format_ask_rejects(display) is None
    assert language_driver().exact_format_observe_ok("display")
    assert not language_driver().exact_format_observe_ok("")

    moved = BehaviourSpec(
        "probe::stable::u::0", "let v = crate_x::go();", "v",
        ObservableOutcome.debug("X { .. }"),
        subkind=BehaviourSubKind.OUTCOME,
        klass=BehaviourClass.EXACT_FORMAT,
        required_env=(("RUST_BACKTRACE", "1"),))
    still = BehaviourSpec(
        "probe::stable::u::1", "let v = crate_x::go();", "v",
        ObservableOutcome.debug("X { .. }"),
        subkind=BehaviourSubKind.OUTCOME,
        klass=BehaviourClass.EXACT_FORMAT)
    assert language_driver().exact_format_probe_covers(moved)
    assert not language_driver().exact_format_probe_covers(still)
    assert probe_coverage_class(moved) == "EXACT_FORMAT"
    assert probe_coverage_class(still) == ""


def test_generic_never_calls_gotchas_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.prompts import sections

    def boom_summary() -> str:
        raise AssertionError("catalog gotchas_summary was called")

    def boom_detail(_excerpt: str) -> str:
        raise AssertionError("catalog gotchas_for_error was called")

    monkeypatch.setattr(sections, "gotchas_summary", boom_summary)
    monkeypatch.setattr(sections, "gotchas_for_error", boom_detail)
    assert language_driver().gotchas_summary() == ""
    assert language_driver().gotchas_for_error("error[E0133]") == ""
    assert language_driver().gotchas_for_error(
        "warning: unexpected `cfg` condition name: `probe_flag`\n"
        "  = note: expected values for `feature` are: `std`") == ""
    # A missing-field note pays the in-scope gate without calling the table.
    gate = language_driver().gotchas_for_error(
        "error[E0609]: no field `inner` on type `Cell`")
    assert "You may not add a new top-level item" in gate
    assert "TYPES_IN_SCOPE" in gate
    prompt = build_per_fn_prompt(
        file_text="fn go() { unimplemented!() }",
        fn_name="go", sig_text="fn go(&self)",
        test_blocks=[], module_name="demo", cluster_name="stable",
        file_rel="src/x.rs", fr_path=None,
        sections=_fn_sections("impl Hash for K"))
    assert "<RUST_GOTCHAS>\n\n</RUST_GOTCHAS>" in prompt or (
        "<RUST_GOTCHAS>\n</RUST_GOTCHAS>" in prompt)


def test_generic_never_calls_catalog_construction_recipe(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline_core.domain import construction_recipe as cr

    def boom(_name: str, _container: str) -> str:
        raise AssertionError("catalog_construction_recipe was called")

    monkeypatch.setattr(cr, "catalog_construction_recipe", boom)
    monkeypatch.setattr(cr, "construction_recipe", boom)
    wrap = language_driver().construction_recipe(
        "from_payload", "impl Cell",
        "fn from_payload(error: Box<dyn StdError + Send + Sync>, "
        "trace: Option<Trace>) -> Self")
    assert "unsized" in wrap
    assert language_driver().construction_recipe(
        "from_boxed", "impl Cell",
        "fn from_boxed(error: Box<dyn StdError + Send + Sync>) -> Self") == ""


def test_generic_construction_recipe_keys_on_sig_shape_not_crate_names() -> None:
    """Plan: unsized Box<dyn plus another arg gets the wrap instruction;
    by-value self returning Self on an inherent impl gets the identity
    instruction; a single Box<dyn arg (already boxed) does not wrap;
    a unit named construct_from_boxed on a crate that does not declare
    BoxedError still gets the wrap from the sig."""
    drv = language_driver()
    wrap = drv.construction_recipe(
        "from_payload", "impl Cell",
        "fn from_payload(error: Box<dyn StdError + Send + Sync>, "
        "trace: Option<Trace>) -> Self")
    assert "unsized" in wrap
    assert "BoxedError" not in wrap
    assert "StdError" not in wrap
    assert "construct_from_std" not in wrap
    already = drv.construction_recipe(
        "from_boxed", "impl Cell",
        "fn from_boxed(error: Box<dyn StdError + Send + Sync>) -> Self")
    assert already == ""
    chain = drv.construction_recipe(
        "and_then", "impl Cell",
        "fn and_then<C>(self, extra: C) -> Self")
    assert "erased identity type" in chain
    assert "ContextError" not in chain
    assert drv.construction_recipe("and_then", "impl Cell") == ""
    assert drv.construction_recipe(
        "construct_from_boxed", "impl Error",
        "fn construct_from_boxed(error: Box<dyn StdError + Send + Sync>, "
        "backtrace: Option<Backtrace>) -> Self")
    trait_self = drv.construction_recipe(
        "context", "impl<T, E> Context<T, E> for Result<T, E>",
        "fn context<C>(self, context: C) -> Self")
    assert trait_self == ""


def test_generic_identity_subject_is_harvested_from_when() -> None:
    """Plan: a Counter suite admits Counter::new and refuses impl Display
    for Counter. An Error-named leftover is not the reason the function
    exists."""
    from pipeline_core.domain.behaviour_select import (
        IdentityMustUnit,
        identity_subject_from_specs,
        identity_subject_from_when,
        select_identity_must,
    )
    from pipeline_core.domain.behaviour_select import (
        identity_must_admits as catalog_admits,
    )
    assert identity_subject_from_when("Counter::new()") == "Counter"
    assert identity_subject_from_when("crate::Error::msg") == "Error"
    pin = BehaviourSpec(
        "p", "", "Counter::new()",
        ObservableOutcome.type_identity("u32", True),
        subkind=BehaviourSubKind.OUTCOME,
        klass=BehaviourClass.IDENTITY)
    assert identity_subject_from_specs((pin,)) == "Counter"
    drv = language_driver()
    assert drv.identity_must_admits(
        "new", "impl Counter", "fn new() -> Self", "Counter")
    assert not drv.identity_must_admits(
        "fmt", "impl Display for Counter",
        "fn fmt(&self, f: &mut Formatter) -> fmt::Result", "Counter")
    assert not drv.constructs_identity_subject(
        "new", "impl Own < T >", "fn new(ptr: NonNull<T>) -> Self", "Counter")
    # Catalog still pins Error constructors (the A/B).
    assert catalog_admits(
        "construct_from_display", "impl Error",
        "fn construct_from_display<M>(error: M) -> Self")
    picked = select_identity_must(
        (pin,), units=(IdentityMustUnit(
            "new", "impl Counter", "fn new() -> Self"),))
    assert [s.then.value for s in picked if s.then is not None] == ["u32"]


def test_generic_vtable_dispatch_is_wrapper_shape_not_a_crate_type() -> None:
    drv = language_driver()
    assert drv.is_vtable_dispatch(
        "unsafe fn slot(this: Hold<Node>) -> u8", "impl Node")
    assert not drv.is_vtable_dispatch(
        "unsafe fn read_at<T: Copy>(p: *const T, i: isize) -> T",
        "impl ReadHelper")
    assert not drv.is_vtable_dispatch(
        "unsafe fn slot<E>(this: Hold<Node>) -> u8 where E: Copy",
        "impl Node")
    assert "ErrorImpl" not in drv.vtable_fast_exit_guidance()
    assert "ErrorVTable" not in drv.vtable_fast_exit_guidance()
    assert "ErrorVTable" not in drv.vtable_category_line()


def test_generic_conversion_license_is_box_dyn_or_a_crate_type() -> None:
    drv = language_driver()
    assert drv.conversion_family() == frozenset()
    assert drv.access_licenses_conversion(
        "drop_glue::<Box<dyn core::error::Error + Send>>")
    assert drv.access_licenses_conversion(
        "<crate::cell::Node>::go", crate_types=("Node",))
    assert not drv.access_licenses_conversion(
        "<crate::cell::Node>::go", crate_types=("Other",))
    assert drv.unique_called_name(
        "    let boxed = err.into_boxed_dyn_error();") == (
        "into_boxed_dyn_error")
    assert drv.unique_called_name(
        "    err.downcast::<String>().unwrap()") == "downcast"
    assert drv.unique_called_name(
        "    let boxed = err.take_box();") == "take_box"
    assert drv.unique_called_name("    let x = 1;") is None
    # Catalog unique-name is family-gated: downcast_ref is not a conversion.
    # Generic recovers the first call; the family exclusion is the A/B.
    assert drv.unique_called_name(
        "    let _ = err.downcast_ref::<MyErr>();") == "downcast_ref"
    with using_driver(catalog_driver()):
        assert language_driver().unique_called_name(
            "    let _ = err.downcast_ref::<MyErr>();") is None
        assert language_driver().unique_called_name(
            "    let boxed = err.take_box();") is None
    must = drv.ownership_transfer_must(("convert",))
    assert "original owner must not drop" in must
    assert "ManuallyDrop" not in must
    assert drv.ownership_transfer_must(()) == ""
    assert not drv.is_destructor_fn_name("object_drop")
    assert not drv.is_destructor_fn_name("drop")


def test_generic_prompt_examples_do_not_name_this_crate() -> None:
    drv = language_driver()
    tags = drv.file_fill_tag_examples()
    assert "ErrorImpl" not in tags
    assert "FUNCTION_NAME" not in tags or "method" in tags
    assert "impl Inner" in tags
    ex = drv.sig_change_example("slot")
    assert "ErrorImpl" not in ex
    assert "Own<T>" in ex
    assert "slot" in ex


def test_catalog_driver_keeps_the_pre_generic_tables() -> None:
    """Negative control: catalog still produces today's table."""
    from pipeline_core.domain.construction_recipe import WRAP_BOX_RECIPE
    from pipeline_core.domain.crash_site import (
        CONVERSION_FAMILY,
        OWNERSHIP_TRANSFER_MUST,
    )
    with using_driver(catalog_driver()):
        drv = language_driver()
        assert "never Display" in drv.class_prompts(pin_env_debug=True)[
            "EXACT_FORMAT"]
        assert "std::error::Error" in drv.class_prompts(pin_env_debug=True)[
            "TRAIT_OBJECT_IDENTITY"]
        assert drv.exact_format_observe_ok("debug")
        assert not drv.exact_format_observe_ok("display")
        assert "E0133" in drv.gotchas_for_error(
            "error[E0133]: call to unsafe function") or (
            "unsafe" in drv.gotchas_for_error(
                "error[E0133]: call to unsafe function").lower())
        assert drv.gotchas_summary()
        assert drv.construction_recipe(
            "construct_from_boxed", "impl Error") == WRAP_BOX_RECIPE
        assert drv.conversion_family() == CONVERSION_FAMILY
        assert drv.is_destructor_fn_name("object_drop")
        assert drv.ownership_transfer_must(("downcast",)) == (
            OWNERSHIP_TRANSFER_MUST)
        assert "ErrorImpl::provide" in drv.file_fill_tag_examples()
        assert "Own<ErrorImpl>" in drv.sig_change_example("provide")
        assert "ErrorVTable" in drv.vtable_fast_exit_guidance()
        assert drv.is_vtable_dispatch(
            "unsafe fn error(this: Ref<ErrorImpl>) -> u8", "impl ErrorImpl")


_CRATE_GOTCHA_RECIPES = (
    "Own<T>", "Ref<T>", "wrapper.as_ptr()", "ErrorImpl<E>",
    "vtable.object_downcast", "anyhow's", "ErrorVTable",
    "impl AsRef<dyn StdError + Send + Sync> for Error",
    "fn(&Error)", "Mut<'a, T>", "Mut::read", "anyhow APIs",
    "Error::msg", "ADHOC_VTABLE", "ContextValue { context",
    "object_", "error_generic_member_access",
)


def test_generic_gotchas_for_error_is_the_in_scope_gate_not_a_crate_recipe() -> None:
    """Plan: rustc saying a name or field is missing pays the pipeline
    gate (TYPES_IN_SCOPE, no new items). A duplicate-impl, signature
    mismatch, unsized-return, or missing-format note pays nothing — the
    excerpt is already in COMPILE_ERRORS and the fill model is the
    language expert. No recipe names a wrapper this crate declares."""
    drv = language_driver()
    cases = (
        ("error[E0609]: no field `inner` on type `Cell`", True),
        ("error[E0560]: struct `Cell` has no field named `inner`", True),
        ("error[E0609]: type `Cell` does not have a field named `inner`", True),
        ("error[E0599]: no method named `foo` found for struct `Hold<Node>`",
         True),
        ("error[E0425]: cannot find value `SHARED` in this scope", True),
        ("error[E0599]: no function or associated item named `new` found "
         "for struct `Pair<A, B>`", True),
        ("error[E0201]: duplicate definitions with name `as_ref`", False),
        ("error[E0053]: method `as_ref` has an incompatible type for trait",
         False),
        ("error[E0277]: the size for values of type `T` cannot be known at "
         "compile time\ndoesn't have a size known at compile-time", False),
        ("error[E0433]: failed to resolve: could not find `format` in `core`",
         False),
        ("error[E0133]: call to unsafe function", False),
        ("warning: unexpected `cfg` condition name: `probe_flag`\n"
         "  = note: expected values for `feature` are: `std`", False),
        ("error_generic_member_access", False),
    )
    for excerpt, gated in cases:
        out = drv.gotchas_for_error(excerpt)
        for mark in _CRATE_GOTCHA_RECIPES:
            assert mark not in out, (excerpt, mark)
        if gated:
            assert "You may not add a new top-level item" in out, excerpt
            assert "TYPES_IN_SCOPE" in out, excerpt
        else:
            assert out == "", excerpt
    assert drv.gotchas_summary() == ""


def test_catalog_gotchas_still_name_this_crate() -> None:
    """Negative control: catalog DETAIL still carries today's crate recipes."""
    with using_driver(catalog_driver()):
        drv = language_driver()
        summary = drv.gotchas_summary()
        assert "Own<T>" in summary
        assert "wrapper.as_ptr()" in summary
        assert "Error::msg" in summary
        field = drv.gotchas_for_error(
            "error[E0609]: no field `inner` on type `Cell`")
        assert "ErrorImpl<E>" in field
        assert "object_" in field
        method = drv.gotchas_for_error(
            "error[E0599]: no method named `foo` found for struct `Hold<Node>`")
        assert "vtable.object_downcast" in method
        assert "anyhow's" in method
        assert "Own<T>" in method
        value = drv.gotchas_for_error(
            "error[E0425]: cannot find value `SHARED` in this scope")
        assert "ADHOC_VTABLE" in value
        assoc = drv.gotchas_for_error(
            "error[E0599]: no function or associated item named `new` found "
            "for struct `Pair<A, B>`")
        assert "ContextValue { context" in assoc
        dup = drv.gotchas_for_error(
            "error[E0201]: duplicate definitions with name `as_ref`")
        assert "impl AsRef<dyn StdError + Send + Sync> for Error" in dup
        sig = drv.gotchas_for_error(
            "error[E0053]: method `as_ref` has an incompatible type for trait")
        assert "fn(&Error)" in sig
        unsized = drv.gotchas_for_error(
            "error[E0277]: the size for values of type `T` cannot be known "
            "at compile time\ndoesn't have a size known at compile-time")
        assert "Mut<'a, T>" in unsized
        fmt = drv.gotchas_for_error(
            "error[E0433]: failed to resolve: could not find `format` in `core`")
        assert "anyhow APIs" in fmt
        assert "Error::msg" in fmt
        cfg = drv.gotchas_for_error(
            "warning: unexpected `cfg` condition name: `probe_flag`\n"
            "  = note: expected values for `feature` are: `std`")
        assert "error_generic_member_access" in cfg
        assert '#[cfg(feature = "error_generic_member_access")]' in cfg


def test_generic_fix_prompt_on_missing_field_carries_the_gate_not_a_crate_field() -> None:
    """The live builder: a Cell missing-field note gets the in-scope gate
    and the Cell type record. Catalog on the same note still teaches
    this crate's private-field class."""
    from pipeline_core.prompts.context import PerFnFixSections

    sections = PerFnFixSections(
        macros="", notes="", container="impl Cell",
        types="struct Cell { head: u8 }",
        invariants="", siblings="", crate_idioms="", bound_methods="")
    kwargs = {
        "file_text": "fn first() { unimplemented!() }",
        "fn_name": "first",
        "sig_text": "fn first(&self) -> u8",
        "test_blocks": [],
        "module_name": "demo", "cluster_name": "stable",
        "file_rel": "src/cell.rs", "prev_body": "self.inner",
        "error_excerpt": "error[E0609]: no field `inner` on type `Cell`",
        "sections": sections, "fr_path": None,
    }
    p = build_per_fn_fix_prompt(**kwargs)
    assert "struct Cell { head: u8 }" in p
    assert "You may not add a new top-level item" in p
    assert "object_" not in p
    assert "ErrorImpl" not in p
    assert "ADHOC_VTABLE" not in p
    with using_driver(catalog_driver()):
        cat = build_per_fn_fix_prompt(**kwargs)
    assert "object_" in cat
    assert "ErrorImpl<E>" in cat
