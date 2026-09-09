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
        "PartialEq",
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
