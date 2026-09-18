"""Bound-provided method inventory.

When a generic parameter is bounded by an in-scope trait in the FROZEN `where`
clause (e.g. `impl<T, E> Trait for Result<T, E> where E: some::BoundTrait`), the
methods of that trait are callable DIRECTLY on values of the parameter. The model
routinely misses this: it reaches for a free constructor (`Type::make_from(v)`)
that imposes a DIFFERENT bound (`E: std::error::Error`) the generic does not
carry, gets `E0277`, and the per-fn loop thrashes.

The skeleton FREEZES trait definitions and `where` clauses (only fn BODIES are
stubbed), so the bound and its provider trait are stable, in-scope facts — not
something the model reconstructs. This module turns those facts into a prompt
section that names the callable methods the bound provides, so the body uses the
bound's own trait method instead of a mis-bound free constructor.

Pure: no I/O, no LLM. The caller resolves which TypeRecords the `where` clause
references (via the existing structured type-reference resolver) and passes them
in; this module filters to provider traits and renders the directive.
"""
from __future__ import annotations

from pipeline_core.domain.models import TypeRecord


def select_bound_provider_traits(
    referenced: list[TypeRecord],
) -> list[TypeRecord]:
    """From the TypeRecords a `where` clause references, keep only the ones that
    are TRAITS with at least one method — those are the records whose methods a
    bounded generic makes callable. Auto/marker traits referenced by the bound
    (`Send`, `Sync`, `Sized`, `'static`) resolve to no in-scope record (or a
    method-less one) and are dropped: they contribute no callable method."""
    return [r for r in referenced if r.kind == "trait" and r.methods]


def format_bound_methods_section(
    impl_where: str, provider_traits: list[TypeRecord],
) -> str:
    """Render the BOUND_METHODS prompt section, or "" when no provider trait
    contributes a method. Lists each in-scope bound trait's method signatures
    (real, in-scope symbols) under a WRONG->RIGHT directive that uses GENERIC
    placeholder names (never crate source names) so the model prefers a
    bound-provided trait method over a free constructor that imposes a different
    bound."""
    if not provider_traits:
        return ""
    lines: list[str] = []
    for tr in provider_traits:
        sigs = "\n".join(f"    {m.sig}" for m in tr.methods if m.sig)
        if sigs:
            lines.append(f"  trait `{tr.name}` provides:\n{sigs}")
    if not lines:
        return ""
    body = "\n".join(lines)
    where_line = (
        f"Your frozen `where` clause is: `{impl_where.strip()}`\n"
        if impl_where.strip() else "")
    return (
        f"{where_line}"
        "A generic parameter bounded by a trait can call THAT trait's methods "
        "directly on its values. PREFER them over free `Type::constructor(...)` "
        "calls, which often require a DIFFERENT bound your generic does NOT "
        "satisfy. The bound is FROZEN — you cannot widen it; route through the "
        "method it already gives you.\n"
        "  WRONG: `Target::make_from(value)`  // needs `E: OtherBound`, but your "
        "`E` is only bounded by the trait below → E0277\n"
        "  RIGHT: `self.map_err(|v| v.bound_method(arg))`  // uses the method the "
        "frozen bound provides\n"
        "Methods your frozen bounds make callable (in-scope, real signatures):\n"
        f"{body}"
    )
