"""Stub-time construction recipes for Error constructors the typed producer
cannot serve.

``producer_bypasses`` keys on a measured escalated producer AND on
``constructs_type`` of the current body. Peel restores ``unimplemented!()``,
the parked-accessor deferral never escalates, and ``construct_from_boxed``
was never a bypass target. Run ``1383168-eb151d09`` (2026-08-29/30):
``producer_bypass_offered`` empty, both units E0277 then peeled, fill
classes ``cashed=0 compile=3``. The instruction has to attach from the
signature, including when the body is a stub or a failed-attempt comment.

Builders that already have ``(name, container)`` call
:func:`construction_recipe`; they do not wait on escalation or body text.
Empty string when the unit is not one of the two shapes below — a forgotten
kwarg cannot omit a recipe the builder computes itself.

The two recipes, each the shape upstream writes and rustc accepts on this
skeleton:

- ``Error::context`` — ``self`` is already this crate's erased Error.
  Construct ``ContextError { context, error: self }`` and install the
  CHAIN table. ``construct_from_context`` needs ``E: StdError`` which
  Error does not implement on this tree; ``ext_context`` is a private
  module and the Error impl of that trait delegates back into this fn.
- ``construct_from_boxed`` — the argument is unsized. Wrap ``BoxedError``
  (a sized StdError this crate already declares) and install
  ``object_downcast`` on the box payload TypeId, never
  ``construct_from_std`` the box (the E0277 every attempt of this unit
  has emitted). ``from_boxed`` has a similar signature and must NOT
  match: it should call this producer, not wrap.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pipeline_core.domain.rust_idents import impl_self_type

# Named so tests pin the PLAN's sentences, not a paraphrase of a helper.
CHAIN_TABLE_RECIPE = (
    "BUILD THE CHAIN TABLE: this fn's `self` is already this crate's erased "
    "Error, not a typed StdError. Construct `ContextError { context, error: self }` "
    "and install `context_chain_downcast` / `context_chain_drop_rest` on that "
    "value in this body. Do not call `construct_from_context` — its `E: StdError` "
    "bound is unsatisfiable on `Error` (this tree has no `impl StdError for Error`). "
    "Do not call `ext_context` — that module is private, and the `Error` impl of "
    "that trait delegates back into this fn. Use the chain fns; rewrite them only "
    "if this prompt lists them as writable."
)

WRAP_BOX_RECIPE = (
    "WRAP THE BOX: the argument is `Box<dyn StdError + Send + Sync>`, which is "
    "unsized and cannot be passed to `construct_from_std` or `construct`. Wrap it "
    "in `BoxedError` — a sized `StdError` this crate already declares — and build "
    "the vtable the same way `construct_from_adhoc` / `construct_from_display` do, "
    "with `object_downcast` on `Box<dyn StdError + Send + Sync>` (the payload "
    "TypeId a user downcasts to), not `BoxedError`. Call "
    "`construct(BoxedError(error), vtable, backtrace)`. Do not transmute the box. "
    "Do not call `construct_from_std` on the box — that is the E0277 this unit "
    "has emitted on every attempt."
)


def catalog_construction_recipe(name: str, container: str) -> str:
    """Catalog-driver recipes keyed on two crate names.

    Production builders go through :func:`language_driver`. This
    function is the catalog table; the generic driver keys on signature
    shape and does not call it.
    """
    if name == "construct_from_boxed":
        return WRAP_BOX_RECIPE
    if name == "context" and impl_self_type(container) == "Error":
        return CHAIN_TABLE_RECIPE
    return ""


# Tests pin the catalog table under this name (the A/B). Production
# builders use language_driver().construction_recipe.
construction_recipe = catalog_construction_recipe


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
    ``sigs`` is the generic driver's shape input; the catalog driver
    ignores it and keys on name.
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
