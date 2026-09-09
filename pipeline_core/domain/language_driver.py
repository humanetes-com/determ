"""Who is the language expert in fill and layout prompts.

The fill model is the language expert. The pipeline's job is the gate:
who may write, what evidence sits in the prompt, whether a two-file
change is one transaction. A catalog of std-trait tutorials, a
Display/Debug name-set, a hardcoded derive list, or a Send/Sync
tutorial is the pipeline taking the language job — the next leftover
of a different construct then needs a sibling of each function.

Two drivers, selected by ``LANGUAGE_DRIVER`` at composition
(``generic`` default, ``catalog`` the pre-2026-09-09 tables for an A/B):

- :class:`GenericLanguageDriver` keys on shape the record already has
  (a ``fmt::Result`` return, a harvested pin, a computed derive list
  injected elsewhere). It does not import or call the catalog tables.
- :class:`CatalogLanguageDriver` is those tables, kept so an A/B can
  turn them back on. The factory
  ``pipeline_core.prompts.language_catalog.catalog_driver`` injects the
  original functions so this module never imports prompts.

Bind once at the composition root (:func:`bind`). The default is
generic so unit tests that never import the pipeline see the
production driver. Domain methods themselves are pure; bind is the
composition seam (same shape as ``prompts.runtime``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pipeline_core.domain.installable_fns import fn_decl_shape


@runtime_checkable
class LanguageDriver(Protocol):
    """Bound language policy for fill and layout prompts."""

    @property
    def name(self) -> str: ...
    def trait_contract(self, container: str) -> str: ...
    def is_formatting(self, container: str, sig: str) -> bool: ...
    def format_follow_default(self) -> tuple[str, ...]: ...
    def derived_trait_rule(self) -> str: ...
    def autotrait_rule(self) -> str: ...


def _return_type(sig: str) -> str:
    shape = fn_decl_shape(sig)
    return shape[2] if shape is not None else ""


@dataclass(frozen=True, slots=True)
class GenericLanguageDriver:
    """The fill model is the language expert; the pipeline is the gate.

    Does not read the catalog tables. A leftover of a different construct
    pays through the same functions.
    """

    name: str = "generic"

    def trait_contract(self, container: str) -> str:
        # The trait name is already in the impl header the prompt quotes.
        return ""

    def is_formatting(self, container: str, sig: str) -> bool:
        # Shape: a fmt::Result return. An impl that returns that type is
        # formatting; an impl whose signature failed to parse is a parser
        # bug, not a reason to name the trait. `container` is accepted so
        # the catalog driver can use it; this path does not read it.
        del container
        return "fmt::Result" in _return_type(sig)

    def format_follow_default(self) -> tuple[str, ...]:
        # The std format method every format-trait dispatcher declares.
        # Production passes `_follow_methods_for_trait(trait)` and does
        # not use this default. The catalog default also followed a
        # second method named for one leftover; this path does not.
        return ("fmt",)

    def derived_trait_rule(self) -> str:
        # `derive_requirements_text` already injects the computed list
        # from the tests. A standard-set paragraph invents derives the
        # tests did not ask for (E0119 on a hand-written impl).
        return ""

    def autotrait_rule(self) -> str:
        # Harvested autotrait pins already sit in LAYOUT-PINNING TESTS.
        return ""


@dataclass(frozen=True, slots=True)
class CatalogLanguageDriver:
    """The pre-2026-09-09 language catalogs, kept for an A/B.

    The tables live in prompts / ``catalog_is_formatting``. This class
    holds injected callables so domain never imports prompts; the
    factory is :func:`pipeline_core.prompts.language_catalog.catalog_driver`.
    Constructing this with empty callables would silently no-op — do not;
    go through the factory.
    """

    trait_contract_fn: Callable[[str], str]
    is_formatting_fn: Callable[[str, str], bool]
    derived_trait_rule_text: str
    autotrait_rule_text: str
    name: str = "catalog"
    format_follow: tuple[str, ...] = ("fmt", "debug")

    def trait_contract(self, container: str) -> str:
        return self.trait_contract_fn(container)

    def is_formatting(self, container: str, sig: str) -> bool:
        return self.is_formatting_fn(container, sig)

    def format_follow_default(self) -> tuple[str, ...]:
        return self.format_follow

    def derived_trait_rule(self) -> str:
        return self.derived_trait_rule_text

    def autotrait_rule(self) -> str:
        return self.autotrait_rule_text


_DRIVER: LanguageDriver = GenericLanguageDriver()


def bind(driver: LanguageDriver) -> None:
    """Bind once at the composition root. Tests restore via :func:`using_driver`."""
    global _DRIVER
    _DRIVER = driver


def language_driver() -> LanguageDriver:
    return _DRIVER


@contextmanager
def using_driver(driver: LanguageDriver) -> Iterator[LanguageDriver]:
    """Bind `driver` for the block and restore the previous driver after."""
    prev = language_driver()
    bind(driver)
    try:
        yield driver
    finally:
        bind(prev)
