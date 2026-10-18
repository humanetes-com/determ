"""Stub-time construction recipes, spliced into the fill builders.

``producer_bypasses`` keys on a measured escalated producer AND on
``constructs_type`` of the current body. Peel restores ``unimplemented!()``,
the parked-accessor deferral never escalates. Run ``1383168-eb151d09``
(2026-08-29/30): ``producer_bypass_offered`` empty, both units E0277 then
peeled, fill classes ``cashed=0 compile=3``. The instruction has to attach
from the signature, including when the body is a stub or a failed-attempt
comment.

The recipe text is the bound language driver's
(``language_driver().construction_recipe``), keyed on signature shape
alone: an unsized boxed trait-object argument beside another argument, or
a by-value ``self`` returning ``Self`` of an inherent type. The catalog of
two crate-named recipes was removed on 2026-10-17 (git tag
``catalog-driver-last``). These helpers only splice: empty string when the
driver returns empty, so a forgotten kwarg cannot omit a recipe the builder
computes itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def construction_recipe_prefix(name: str, container: str, sig: str = "") -> str:
    """Recipe plus blank line for splicing ahead of the next prompt section.

    Empty when the bound driver returns empty, so a builder that
    always concatenates this keeps byte-identical output for every other
    unit (the characterization tests).
    """
    from pipeline_core.domain.language_driver import language_driver
    text = language_driver().construction_recipe(name, container, sig)
    return f"{text}\n\n" if text else ""


def construction_recipes_for(
    names: Iterable[str], container: str,
    sigs: Mapping[str, str] | None = None,
) -> str:
    """Unique recipes for these names under ``container``, stable order.

    Used by the per-impl / maximal builders whose unit of work is a block.
    ``sigs`` is the driver's shape input; a name with no signature gets no
    recipe.
    """
    from pipeline_core.domain.language_driver import language_driver
    parts: list[str] = []
    seen: set[str] = set()
    table = sigs or {}
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        text = language_driver().construction_recipe(
            name, container, table.get(name, ""))
        if text:
            parts.append(text)
    return "\n\n".join(parts)
