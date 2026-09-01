"""The blamed type's crate-visible API, appended to a retry's error feedback.

A privacy/resolution compile error names the exact type the model reached
into wrongly, and the thing the model is missing is that type's visible
surface — not a bigger window. Measured on run `358833-aa15d93c`
(2026-09-01), cluster `stable`: four `E0616`s and one `E0599` on the same
`src/fmt.rs::ErrorImpl` block, every attempt reaching for
`ErrorImpl.backtrace` by field or by method-on-`dyn`, while the one attempt
that happened to see all of `src/error.rs` (the budgets-off last resort)
immediately used the `pub(crate) unsafe fn backtrace` accessor and
compiled. The fix-loop feedback carried the diagnostic line but nothing the
diagnostic implies; this module renders that missing half from the type
records the fill already loaded — deterministic, bounded, no new pull
channel and no budget change.

Only `E0616` (private field) and `E0599` (no method/variant found) qualify:
both are "you reached for a surface this type does not expose here", and
both print the blamed type in a fixed diagnostic shape this parser reads.
Other codes name traits, bounds or lifetimes, where a method inventory is
noise.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from pipeline_core.domain.models import TypeRecord

# The two diagnostic shapes that name the blamed type:
#   error[E0616]: field `backtrace` of struct `ErrorImpl<E>` is private
#   error[E0599]: no method named `backtrace` found for struct `ErrorImpl`
#   error[E0599]: no method named `x` found for reference `&dyn StdError`
_BLAMED_TYPE = re.compile(
    r"error\[E0(?:616|599)\][^\n]*?"
    r"(?:struct|enum|union|reference|type|trait object)\s+"
    r"`(?P<ty>[^`]+)`")

# Inventory bound: an inherent-impl-heavy type (anyhow's `ErrorImpl` carries
# well under this) renders whole; a pathological hundred-method type is
# truncated with a count so the section stays a section.
_MAX_METHODS = 40


def _base_name(ty: str) -> str:
    """`crate::error::ErrorImpl<E>` / `&dyn StdError + Send` -> `ErrorImpl`
    / `StdError`: strip reference/pointer sigils, `dyn`/`impl` keywords,
    trait-object `+` bounds, the path and the generics — the extractor keys
    type records on the bare ident.

    A path whose first segment is `std`/`core`/`alloc` yields "": the live
    tell was `error[E0599]: no method named `backtrace` found for reference
    `&dyn core::error::Error + Send + Sync`` (run `358833-aa15d93c`), whose
    LEAF collides with the crate's own `Error` struct — injecting the crate
    type's API for a std trait object would answer a question the error
    never asked."""
    t = ty.strip().lstrip("&*").strip()
    for kw in ("mut ", "dyn ", "impl "):
        if t.startswith(kw):
            t = t[len(kw):].strip()
    t = t.split("+", 1)[0].strip()
    t = t.split("<", 1)[0].strip()
    if "::" in t and t.split("::", 1)[0] in ("std", "core", "alloc"):
        return ""
    return t.rsplit("::", 1)[-1].strip()


def blamed_type_names(excerpt: str) -> tuple[str, ...]:
    """The distinct base type names an `E0616`/`E0599` in `excerpt` blames,
    in first-blame order."""
    return tuple(dict.fromkeys(
        _base_name(m.group("ty")) for m in _BLAMED_TYPE.finditer(excerpt)
        if _base_name(m.group("ty"))))


def _vis(is_pub: bool, is_exactly_pub: bool) -> str:
    if is_exactly_pub:
        return "pub "
    if is_pub:
        return "pub(crate) "
    return ""


def blamed_type_api_section(
    excerpt: str, type_records: Sequence[TypeRecord] | Iterable[TypeRecord],
) -> str:
    """The appendix for a retry prompt's error-feedback slot, or "".

    For each type an `E0616`/`E0599` in `excerpt` blames that the extractor
    has a record for, render its fn inventory — name, signature and
    visibility, exactly what the diagnostic implies the model is missing.
    Restricted visibilities (`pub(super)`, `pub(in …)`) render as
    `pub(crate)`: from inside the crate the distinction the model needs is
    only "callable here" versus the `pub`/private poles. Types the record
    set does not know are skipped, so an std or third-party blame appends
    nothing."""
    names = blamed_type_names(excerpt)
    if not names:
        return ""
    by_name = {r.name: r for r in type_records}
    parts: list[str] = []
    for name in names:
        rec = by_name.get(name)
        if rec is None or not rec.methods:
            continue
        lines = [f"{_vis(m.is_pub, m.is_exactly_pub)}{m.sig}"
                 for m in rec.methods[:_MAX_METHODS] if m.sig]
        if not lines:
            continue
        omitted = len(rec.methods) - _MAX_METHODS
        if omitted > 0:
            lines.append(f"… and {omitted} more")
        parts.append(
            f"The error above blames `{name}`. Its fns, as the crate "
            "declares them — reach the data through one of these instead "
            "of a field or method the type does not expose here:\n"
            + "\n".join(f"  {ln}" for ln in lines))
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


def with_blamed_type_api(
    excerpt: str, type_records: Sequence[TypeRecord] | Iterable[TypeRecord],
) -> str:
    """`excerpt` with the blamed types' inventories appended — the form the
    retry prompt's feedback slot takes. The raw excerpt is unchanged for
    every other consumer (signature history, retrospectives, event
    previews); only the prompt sees the appendix."""
    if not excerpt:
        return excerpt
    section = blamed_type_api_section(excerpt, type_records)
    return excerpt + section if section else excerpt
