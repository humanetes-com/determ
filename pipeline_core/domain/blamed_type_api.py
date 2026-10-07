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


_E0277 = "error[E0277]"
_SIZE_OF = re.compile(
    r"the size for values of type `([^`]+)` cannot be known")
_BOUND_UNSAT = re.compile(
    r"the trait bound `([^`]+)` is not satisfied")
_TRAIT_MISSING = re.compile(
    r"the trait `([^`]+)` is not implemented")
# A rustc gutter line `    |     E: StdError + Send + Sync + 'static`.
# The identifier before the colon is the parameter; the rest is the bound
# this diagnostic required, which may spell the trait differently from
# the `dyn` type in the error header.
_BOUND_LINE = re.compile(
    r"\|\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*([^,\n]+)")
_AUTO_TRAITS = frozenset({
    "Send", "Sync", "Unpin", "Sized", "UnwindSafe", "RefUnwindSafe", "Copy",
})


def _tighten(ty: str) -> str:
    """Whitespace, `'static` and one layer of parentheses off a type the
    compiler or a field spelled."""
    t = re.sub(r"\s+", "", ty.strip())
    t = t.replace("+'static", "").replace("+'_", "")
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1]
    return t


def _dyn_shape(ty: str) -> tuple[str, frozenset[str]] | None:
    """`(principal trait leaf, auto-trait leaves)` of a `dyn` type, or
    None when it is not a single-principal trait object."""
    t = _tighten(ty)
    if t.startswith("Box<") and t.endswith(">"):
        t = t[4:-1]
    if t.startswith("dyn"):
        t = t[3:]
    else:
        return None
    principal = ""
    autos: set[str] = set()
    for part in t.split("+"):
        leaf = part.rsplit("::", 1)[-1]
        if not leaf or leaf.startswith("'"):
            continue
        if leaf in _AUTO_TRAITS:
            autos.add(leaf)
        elif principal:
            return None
        else:
            principal = leaf
    if not principal:
        return None
    return principal, frozenset(autos)


def _e0277_blocks(excerpt: str) -> list[str]:
    parts = re.split(r"(?=error\[E0277\])", excerpt or "")
    return [p for p in parts if p.startswith(_E0277)]


def _alt_traits(block: str, principal: str) -> frozenset[str]:
    """Trait leaves this one diagnostic uses for the same obligation.

    The compiler prints `core::error::Error` and the source bound on the
    same error prints the crate's name for that trait. Both leaves are
    the obligation. No name is special: they are whatever this block
    wrote."""
    found = {principal}
    for m in _TRAIT_MISSING.finditer(block):
        leaf = m.group(1).rsplit("::", 1)[-1].split("<", 1)[0]
        if leaf and leaf not in _AUTO_TRAITS:
            found.add(leaf)
    for m in _BOUND_LINE.finditer(block):
        for part in m.group(1).split("+"):
            leaf = part.strip().rsplit("::", 1)[-1].split("<", 1)[0]
            if leaf and leaf not in _AUTO_TRAITS and leaf.isidentifier():
                found.add(leaf)
    return frozenset(found)


def _subject_of(block: str) -> tuple[str, frozenset[str]] | None:
    """`(subject type as spelled, traits the holder must implement)`.

    The size form's subject is the unsized type. The bound form's subject
    is the type that failed the trait. Traits are the obligation's leaves,
    not `Sized` alone: a size error's useful obligation is the bound the
    note says the callee required."""
    size = _SIZE_OF.search(block)
    bound = _BOUND_UNSAT.search(block)
    if size:
        subject = size.group(1).strip()
        shape = _dyn_shape(subject)
        traits = _alt_traits(block, shape[0]) if shape else frozenset()
        traits = frozenset(t for t in traits if t not in _AUTO_TRAITS)
        return subject, traits
    if bound:
        body = bound.group(1)
        if ":" not in body:
            return None
        subject, trait = body.split(":", 1)
        leaf = trait.strip().rsplit("::", 1)[-1].split("<", 1)[0]
        wanted: set[str] = {leaf} if leaf and leaf not in _AUTO_TRAITS else set()
        shape = _dyn_shape(subject.strip())
        # Every spelling this block uses for the obligation: rustc prints
        # `core::error::Error`, the callee's bound note prints the crate's
        # alias (`E: StdError`), and a crate impl is recorded under the
        # alias. Run `486447-5207101c`: `impl<C> StdError for
        # ContextError<C, Error>` was the answer to `Error: core::error::Error`.
        wanted |= set(_alt_traits(block, shape[0] if shape else leaf))
        return subject.strip(), frozenset(
            t for t in wanted if t not in _AUTO_TRAITS)
    return None


def _field_holds(field_ty: str, subject: str, alts: frozenset[str]) -> bool:
    """The field is the subject, or `Box` of it, under either spelling
    this diagnostic used for the trait object."""
    field = _tighten(field_ty)
    sub = _tighten(subject)
    if field == sub or field == f"Box<{sub}>":
        return True
    shape = _dyn_shape(sub)
    if shape is None:
        return False
    _leaf, autos = shape
    inner = field[4:-1] if field.startswith("Box<") and field.endswith(">") else ""
    got = _dyn_shape(inner) if inner else None
    if got is None:
        return False
    return got[0] in alts and got[1] == autos


def _implements(rec: TypeRecord, traits: frozenset[str]) -> bool:
    if not traits:
        return True
    if rec.trait_impls is None:
        return False
    for impl in rec.trait_impls:
        leaf = impl.trait_path.rsplit("::", 1)[-1].split("<", 1)[0]
        if leaf in traits:
            return True
    return False


def _generic_params(generics: str) -> list[str]:
    """`<C, E: Bound>` -> `["C", "E"]` (lifetimes and consts dropped)."""
    text = generics.strip()
    if not (text.startswith("<") and text.endswith(">")):
        return []
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in text[1:-1] + ",":
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            name = cur.split(":", 1)[0].strip()
            if name and not name.startswith("'") and not name.startswith("const "):
                out.append(name)
            cur = ""
            continue
        cur += ch
    return out


def _split_args(text: str) -> list[str]:
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in text + ",":
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        cur += ch
    return [a for a in out if a]


def instantiating_impl(rec: TypeRecord, subject: str,
                       traits: frozenset[str]) -> str:
    """The header of one of ``rec``'s trait impls that implements a wanted
    trait for ``rec`` with a generic parameter bound to ``subject`` — where
    that parameter is the type of one of ``rec``'s fields — or "".

    A holder whose field is generic (`error: E`) holds the subject only in
    the impl that instantiates `E` with it. Run `486447-5207101c`
    (2026-10-07): a session turn wrote `Self::construct_from_context(context,
    self, None)` for `Error::context` three times, each E0277 `Error:
    core::error::Error` not satisfied; the crate's `impl<C> StdError for
    ContextError<C, Error>` is the holder that implements the trait for
    exactly that value, and the field-type match could not see it."""
    if rec.trait_impls is None:
        return ""
    params = _generic_params(rec.generics)
    field_params = {(_tighten(f.ty)) for f in rec.fields if f.ty}
    sub = _tighten(subject)
    for impl in rec.trait_impls:
        leaf = impl.trait_path.rsplit("::", 1)[-1].split("<", 1)[0]
        if traits and leaf not in traits:
            continue
        args = [_tighten(a) for a in _split_args(
            _tighten(impl.self_ty)[len(rec.name) + 1:-1])] if _tighten(
                impl.self_ty).startswith(rec.name + "<") else []
        for param, arg in zip(params, args, strict=False):
            if arg == sub and param in field_params:
                return impl.header or f"impl {impl.trait_path} for {impl.self_ty}"
    return ""


def adapter_types_for_diagnostic(
    excerpt: str, type_records: Sequence[TypeRecord] | Iterable[TypeRecord],
) -> tuple[TypeRecord, ...]:
    """Crate types that hold the E0277 subject (or a `Box` of it) and
    implement the trait the diagnostic says was required.

    `types_referenced_in_text` selects types whose name appears in the
    signature. A wrapper whose name the signature never spells, and whose
    field is the value the compiler rejected, is invisible to that walk.
    Run `371055-1a407b3c`: ten replies passed the unsized trait object by
    value (`construct_from_std`, `Error::new`); the reply that compiled
    wrapped it in the one crate struct that holds `Box` of it and
    implements the trait. That struct's name was in no failing prompt.
    External records are not candidates: the compiler already printed the
    foreign impls."""
    records = [r for r in type_records
               if r.name and not r.external_path and r.kind in ("struct", "enum")]
    found: list[TypeRecord] = []
    seen: set[str] = set()
    for block in _e0277_blocks(excerpt):
        parsed = _subject_of(block)
        if parsed is None:
            continue
        subject, traits = parsed
        shape = _dyn_shape(subject)
        alts = _alt_traits(block, shape[0]) if shape else frozenset()
        for rec in records:
            if rec.name in seen:
                continue
            direct = (any(_field_holds(f.ty, subject, alts)
                          for f in rec.fields if f.ty)
                      and _implements(rec, traits))
            if not direct and not instantiating_impl(rec, subject, traits):
                continue
            seen.add(rec.name)
            found.append(rec)
    return tuple(found)


def adapter_type_section(
    excerpt: str, type_records: Sequence[TypeRecord] | Iterable[TypeRecord],
) -> str:
    """The retry appendix for an `E0277`, or "" when no crate type holds
    the rejected value."""
    recs = adapter_types_for_diagnostic(excerpt, type_records)
    if not recs:
        return ""
    subjects = [p for p in (_subject_of(b) for b in _e0277_blocks(excerpt))
                if p is not None]
    lines = []
    for rec in recs:
        fields = ", ".join(f"{f.name}: {f.ty}" for f in rec.fields if f.ty)
        via = next((h for sub, tr in subjects
                    if (h := instantiating_impl(rec, sub, tr))), "")
        lines.append(
            f"  `{rec.name}` in `{rec.file}` — {fields}"
            + (f" (`{via}`)" if via else ""))
    return (
        "\n\nThe error above rejects a value the compiler could not pass "
        "where a trait (and, for an unsized value, a size) was required. "
        "These crate types hold that value, or a Box of it, in a field — "
        "directly, or through the generic parameter the quoted impl binds to "
        "it — and implement the trait the error names. The list is the "
        "records, not a guess:\n"
        + "\n".join(lines))


def with_blamed_type_api(
    excerpt: str, type_records: Sequence[TypeRecord] | Iterable[TypeRecord],
) -> str:
    """`excerpt` with the retry appendices the diagnostic asks for.

    `E0616`/`E0599` get the blamed type's fn inventory. `E0277` gets the
    crate types that hold the rejected value. Both are empty for every
    other code, and the raw excerpt is unchanged for every consumer that
    does not call this (signature history, retrospectives). Every retry
    slot that shows the compiler's answer calls this, so a new slot cannot
    grow one appendix and forget the other."""
    if not excerpt:
        return excerpt
    records = list(type_records)
    extra = blamed_type_api_section(excerpt, records) + adapter_type_section(
        excerpt, records)
    return excerpt + extra if extra else excerpt
