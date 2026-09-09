"""Compose the catalog language driver from the tables that live here.

Domain must not import prompts (layers contract). The catalog
implementations stay in ``sections.trait_contract_for``,
``layout_authoring``'s derive/autotrait rules,
``behaviour_select.catalog_is_formatting``, and the gotchas /
class-prompt tables. This factory injects them into
:class:`CatalogLanguageDriver` so ``LANGUAGE_DRIVER=catalog`` is
the A/B and generic never grows an import edge onto the tables.
"""

from __future__ import annotations

from pipeline_core.domain.behaviour_probe import catalog_class_prompts
from pipeline_core.domain.behaviour_select import catalog_is_formatting
from pipeline_core.domain.enums import LanguageDriverKind
from pipeline_core.domain.language_driver import (
    CatalogLanguageDriver,
    GenericLanguageDriver,
    LanguageDriver,
)
from pipeline_core.prompts.layout_authoring import _AUTOTRAIT_RULE, _DERIVED_TRAIT_RULE
from pipeline_core.prompts.sections import (
    gotchas_for_error,
    gotchas_summary,
    trait_contract_for,
)


def catalog_driver() -> CatalogLanguageDriver:
    """The pre-2026-09-09 tables, wired for an A/B."""
    return CatalogLanguageDriver(
        trait_contract_fn=trait_contract_for,
        is_formatting_fn=catalog_is_formatting,
        derived_trait_rule_text=_DERIVED_TRAIT_RULE,
        autotrait_rule_text=_AUTOTRAIT_RULE,
        gotchas_summary_fn=gotchas_summary,
        gotchas_for_error_fn=gotchas_for_error,
        class_prompts_fn=lambda pin: catalog_class_prompts(pin_env_debug=pin),
    )


def driver_from_kind(kind: LanguageDriverKind) -> LanguageDriver:
    """The driver for an env-coerced kind. Unknown kinds are GENERIC."""
    if kind is LanguageDriverKind.CATALOG:
        return catalog_driver()
    return GenericLanguageDriver()
