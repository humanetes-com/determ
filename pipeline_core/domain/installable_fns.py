"""Which of a file's declared fns an ESCALATED producer could INSTALL.

The loop's unit of work is one fn and its unit of measurement is one test, and
some defects are smaller than neither. `anyhow` erases its error type behind a
hand-built vtable: one fn builds an `ErrorVTable` literal and the fns it names
in that literal are what the dispatch actually runs. Wiring a per-type
`object_downcast` into the vtable while that fn's body is wrong pays nothing;
fixing the body while the vtable still names the generic one pays nothing. The
two live in different units — one inside `impl Error`, one a free fn — which
are never writable in the same prompt, so every intermediate state scores
identically and no per-unit fill can climb it. Measured 2026-08-21: re-splicing
a past tree that had the wiring but not the bodies scored 92 of 105, the same
92, the same 13 failures.

This module names the complement: given a producer the futility escalation has
already convicted, which fns of the same file could that producer install? Not
"dead code" — an unreferenced fn is not always the other half of a split
defect, and this relation is the one the defect actually has:

    an ESCALATED producer constructs a struct literal of type T
      -> T has fn-pointer fields
        -> this file declares fns whose signature matches those field types

On `src/error.rs` that yields `context_downcast`, `context_drop_rest`,
`context_chain_downcast`, `context_chain_drop_rest` and `context_backtrace` —
declared, compiling, and referenced by nothing but their own declaration — with
`object_*` and `no_backtrace` behind them, already wired by the single literal
at `:149`. It needs no notion of "unwired" at all and generalises to any
fn-pointer table.

Deterministic, no LLM, no name heuristics — the same contract
`domain/fill_groups.py` holds itself to.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.models import FnLocator, SlotFit, TypeRecord
from pipeline_core.domain.rust_idents import (
    canon_token_text,
    identifier_tokens,
    impl_self_type,
)
from pipeline_core.domain.rust_scan import names_method_call
from pipeline_core.domain.type_production import constructs_type, is_ident_char


@dataclass(frozen=True, slots=True)
class Installable:
    """One fn a producer could install, and the field that would hold it."""

    fn_name: str
    type_name: str        # the struct whose field would hold it
    field: str            # the field's name
    references: int       # occurrences of the fn's name in the file

    @property
    def unwired(self) -> bool:
        """The name occurs nowhere in the file but its own declaration: no
        table installs it, so no test can execute it, and a route whose
        write set is read off coverage can never contain it."""
        return self.references <= 1


@dataclass(frozen=True, slots=True)
class ProducerBypass:
    """One fn whose body constructs a wrapper the typed producer erases,
    WITHOUT invoking that producer — the call-site half of the split defect.

    `installable_fns` above makes the table's *callees* writable beside the
    producer. This is its complement, and the record shows the complement is
    where the defect survives once the callees are written: run
    `40938-f4e8e94b`'s FILE FILLED 20 carried the twelve installables and the
    six constructors, the model's own thinking diagnosed that `Error::context`
    routes `ContextError` through `Self::new` → `construct_from_std` (so the
    per-type table `construct_from_context` builds is installed on nothing),
    named the fix, then wrote "Error::context is not listed for rewrite" and
    complied. The sweep after moved none of the ten vtable pairs. The
    predicate — constructs W through the generic path while bypassing W's
    typed producer — IS the defect, so the predicate sets the scope: any fn
    of the prompted file, in any impl, whatever file the producer lives in.
    (The producer is `pub(crate)`, so a cross-file bypass CAN invoke it; on
    the live tree the five failing `test_context` downcasts never even reach
    `Error::context` — `tests/test_context.rs::make_chain` builds both wraps
    through `src/context.rs::ext_context`, measured 2026-08-24.)
    """

    fn_name: str
    container: str        # one-line impl header ("" for a free fn)
    wrapper: str          # W — the type the body constructs
    producer: str         # the typed producer the generic path bypasses
    producer_file: str    # where that producer is declared
    same_file: bool       # producer declared in the prompted file itself
    # True iff this fn's impl erases the impl's OWN generic parameter
    # (`impl<E> Trait for E` — the extractor's `impl_self_generic`, decided on
    # the syn AST). This is the fact the instruction split keys on, and it is
    # the ERASED INNER TYPE, not bound coverage: upstream `Error::context`
    # builds the `ContextError<C, Error>` CHAIN vtable inline and never calls
    # the typed producer, even where bounds could be arranged — so a concrete
    # self must NEVER be instructed to call the producer. On the live tree the
    # call-form instruction produced three identical `E0277`s on
    # `Error::context` (run 57025-6ba652f2, 17:45/17:55/18:05 UTC), while the
    # generic `ext_context` in the same reply executed it correctly. False for
    # free fns and anything undecidable — the cautious instruction can cost a
    # missed shortcut, never an instructed compile error.
    erases_generic: bool


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` at bracket depth 0 (`<>`, `()`, `[]`). A fn-pointer type's
    parameter list is full of `Option<Ref<()>>` and `dyn StdError + Send`, so a
    naive `.split(",")` cuts inside a generic argument."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return [p.strip() for p in out if p.strip()]


def _norm(text: str) -> str:
    """Whitespace-collapsed type text — the form the STRUCTURAL scan below
    works on (`unsafe `, `fn `, ` where ` are found as words in it)."""
    return " ".join(text.split())


def _canon(text: str) -> str:
    """The form two type texts are COMPARED in: whitespace collapsed, then
    every space dropped unless it separates two identifier characters.

    Collapsing runs of whitespace is not enough, because the two sides of the
    comparison come from different printers. A field type arrives from the
    extractor's pretty-printer token-spaced — `unsafe fn (Own < ErrorImpl >)`,
    `Option < & Backtrace >` — while a declaration's `sig_text` is the source
    verbatim: `fn object_drop<E>(e: Own<ErrorImpl>)`. Measured on the live tree
    2026-08-22 (run 55484-84c991f1): with whitespace-collapse alone no
    declaration of `src/error.rs` matched any of `ErrorVTable`'s seven fields,
    `installable_fns` returned () on every file-first prompt of five sweeps,
    and the escalated constructors were dispatched without the `context_*`
    bodies their tables name — the split defect this module exists to make
    writable in one prompt stayed split. `dyn StdError + Send` keeps its one
    word-separating space (`dyn StdError`) and loses the rest on both sides.
    The algorithm lives in `rust_idents.canon_token_text` — the peel's
    container identity met the identical class on its first live outing (run
    `264297-dd681686`, 2026-08-24) and both seams now share the one
    mechanism."""
    return canon_token_text(text)


def _matching_paren(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def fn_pointer_shape(ty: str) -> tuple[bool, tuple[str, ...], str] | None:
    """`(is_unsafe, param types, return type)` for a fn-POINTER type, else None.

    `unsafe fn(Own<ErrorImpl>, TypeId)` -> `(True, ("Own<ErrorImpl>", "TypeId"), "")`.
    """
    t = _norm(ty)
    is_unsafe = t.startswith("unsafe ")
    if is_unsafe:
        t = t[len("unsafe "):]
    if not t.startswith("fn"):
        return None
    rest = t[2:].lstrip()
    if not rest.startswith("("):
        return None
    close = _matching_paren(rest, 0)
    if close == -1:
        return None
    params = tuple(_canon(p) for p in _split_top_level(rest[1:close]))
    tail = rest[close + 1:].strip()
    ret = _canon(tail[2:]) if tail.startswith("->") else ""
    return is_unsafe, params, ret


def fn_decl_shape(sig: str) -> tuple[bool, tuple[str, ...], str] | None:
    """The same triple for a fn DECLARATION, with its generic list and its
    parameter NAMES dropped — those are the only differences between
    `unsafe fn object_downcast<E>(e: Ref<ErrorImpl>, target: TypeId) -> Option<Ref<()>>`
    and the field type that would hold it.

    The extractor's `sig_text` starts at `fn` — `unsafe` and the visibility are
    not in it — so `is_unsafe` reads False for every live declaration. That is
    harmless under the directional rule in `_coercible` (a safe-looking fn may
    fill an `unsafe fn` field) and would be wrong only for a SAFE field, which
    no table on this crate has."""
    t = _norm(sig)
    is_unsafe = t.startswith("unsafe ")
    if is_unsafe:
        t = t[len("unsafe "):]
    if not t.startswith("fn "):
        return None
    open_at = -1
    depth = 0
    for i, ch in enumerate(t):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth == 0:
            open_at = i
            break
    if open_at == -1:
        return None
    close = _matching_paren(t, open_at)
    if close == -1:
        return None
    params = []
    for p in _split_top_level(t[open_at + 1:close]):
        # `e: Ref<ErrorImpl>` -> `Ref<ErrorImpl>`; `&self` has no `:` and is
        # kept verbatim, which is correct — a method can never match a plain
        # fn-pointer field.
        params.append(_canon(p.split(":", 1)[1]) if ":" in p else _canon(p))
    tail = t[close + 1:].strip()
    # A `where` clause sits after the return type and belongs to neither.
    ret = (_canon(tail[2:].split(" where ", 1)[0].rstrip("{").strip())
           if tail.startswith("->") else "")
    return is_unsafe, tuple(params), ret



def _coercible(decl: tuple[bool, tuple[str, ...], str],
               field: tuple[bool, tuple[str, ...], str]) -> bool:
    """True iff a fn with `decl`'s shape can be stored in a field of `field`'s
    type.

    Parameters and return must match exactly; safety is DIRECTIONAL, because
    Rust is: a safe fn coerces to an `unsafe fn` pointer, never the reverse.
    That is not a nicety here — upstream `anyhow` installs the SAFE
    `fn no_backtrace(e: Ref<ErrorImpl>) -> Option<&Backtrace>` into
    `object_backtrace: unsafe fn(Ref<ErrorImpl>) -> Option<&Backtrace>`, and an
    exact-equality rule drops it while admitting nothing in exchange."""
    d_unsafe, d_params, d_ret = decl
    f_unsafe, f_params, f_ret = field
    return (len(d_params) == len(f_params)
            and all(_same_type(d, f) for d, f in zip(d_params, f_params, strict=True))
            and _same_type(d_ret, f_ret)
            and (f_unsafe or not d_unsafe))


_PATH_PREFIX = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*::)+")


def _same_type(a: str, b: str) -> bool:
    """Two canonical type texts name the same type, whatever path each
    spells it with. A field type and a declaration come from different
    printers AND different authoring passes: on the live `stable` tree
    (2026-09-24) `ErrorVTable`'s fields read `crate::ptr::Own<crate::error::
    ErrorImpl>` while `object_drop<E>` declares `e: Own<ErrorImpl>`, so an
    exact comparison matched 0 of the 7 declared fns for `object_drop`,
    `object_ref` and `object_backtrace`, and the installable offers and the
    session's table complement never listed `object_drop_front` or
    `context_backtrace`. Paths are dropped segment-wise
    (`core::any::TypeId` -> `TypeId`); two distinct types sharing a last
    segment would compare equal, which the compiler then settles."""
    return a == b or _PATH_PREFIX.sub("", a) == _PATH_PREFIX.sub("", b)

def _generic_list(t: str, at: int) -> tuple[str, int] | None:
    """The `<...>` opening at `at` in `t` and the index just past it."""
    depth = 0
    for i in range(at, len(t)):
        if t[i] == "<":
            depth += 1
        elif t[i] == ">":
            depth -= 1
            if depth == 0:
                return t[at + 1:i], i + 1
    return None


def _decl_type_params(sig: str) -> dict[str, str]:
    """A declaration's own type parameters and the bounds it puts on each,
    inline and in its `where` clause (`fn f<'a, T: A, const N: usize>(..)
    where T: B` -> {"T": "A + B"}). Lifetimes and const generics are not
    types a slot's parameter could bind."""
    t = _norm(sig)
    at = t.find("fn ")
    if at == -1:
        return {}
    lt = t.find("<", at)
    paren = t.find("(", at)
    if lt == -1 or (paren != -1 and paren < lt):
        return {}
    got = _generic_list(t, lt)
    if got is None:
        return {}
    inner, _ = got
    bounds: dict[str, list[str]] = {}
    for part in _split_top_level(inner):
        p = part.strip()
        if not p or p.startswith("'") or p.startswith("const "):
            continue
        name, _, bound = p.partition(":")
        name = name.split("=", 1)[0].strip()
        bounds[name] = [bound.strip()] if bound.strip() else []
    if " where " in t:
        for clause in _split_top_level(t.split(" where ", 1)[1].rstrip("{ ")):
            lhs, _, rhs = clause.partition(":")
            if lhs.strip() in bounds and rhs.strip():
                bounds[lhs.strip()].append(rhs.strip())
    return {n: " + ".join(b) for n, b in bounds.items()}


def _fits(decl: tuple[bool, tuple[str, ...], str],
          field: tuple[bool, tuple[str, ...], str],
          params: Mapping[str, str]) -> dict[str, str] | None:
    """The binding of the declaration's type parameters that stores it in
    the field, or None when there is none.

    `_coercible`, where a position the declaration types with one of its own
    bare type parameters binds that parameter to the field's type there —
    `fn f<T>(x: T)` can be stored as `fn(u8)` by naming `f::<u8>`. A
    parameter binds once: `fn same<T>(a: T, b: T)` does not fit
    `fn(u8, bool)`, and `fn id<T>(x: T) -> T` does not fit `fn(u8)` (no
    return is `()`). Anything else must match as `_coercible` requires."""
    d_unsafe, d_params, d_ret = decl
    f_unsafe, f_params, f_ret = field
    if len(d_params) != len(f_params) or (d_unsafe and not f_unsafe):
        return None
    bound: dict[str, str] = {}

    def one(d: str, f: str) -> bool:
        if d in params:
            if d in bound:
                return _same_type(bound[d], f)
            bound[d] = f
            return True
        return _same_type(d, f)

    pairs = [*zip(d_params, f_params, strict=True), (d_ret or "()", f_ret or "()")]
    return bound if all(one(d, f) for d, f in pairs) else None


def _spelled(record: object) -> str | None:
    """How a table literal names the fn: `name<T, U>` (parameter names, no
    bounds) for a free fn, `SelfType::name<…>` for an associated one; None
    for a trait's declaration (it has no body a table could install)."""
    name = _field(record, "fn_name")
    container = _field(record, "container")
    if not name or container.lstrip().startswith("trait"):
        return None
    sig = _field(record, "sig_text")
    params = [p.strip() for p in _generic_names(sig)]
    gen = f"<{', '.join(params)}>" if params else ""
    if not container:
        return f"{name}{gen}"
    self_ty = impl_self_type(container)
    return f"{self_ty}::{name}{gen}" if self_ty else None


def _generic_names(sig: str) -> list[str]:
    """Every generic parameter name of a declaration, lifetimes and consts
    included, in order — how a turbofish lists them."""
    t = _norm(sig)
    at = t.find("fn ")
    lt = t.find("<", at) if at != -1 else -1
    paren = t.find("(", at) if at != -1 else -1
    if lt == -1 or (paren != -1 and paren < lt):
        return []
    got = _generic_list(t, lt)
    if got is None:
        return []
    out = []
    for part in _split_top_level(got[0]):
        p = part.strip()
        if p.startswith("const "):
            p = p[len("const "):]
        out.append(p.split(":", 1)[0].split("=", 1)[0].strip())
    return [p for p in out if p]


def slot_fits(field_ty: str, fn_records: Sequence[object]
              ) -> tuple[SlotFit, ...] | None:
    """Every crate fn whose declared signature can be stored in a field of
    type `field_ty`, in record order. None when `field_ty` is not a fn
    pointer; () when it is and no crate fn fits.

    Why: of the compile errors the pipeline quoted back to retries from
    2026-09-01 to 2026-09-24, 143 named a fn that does not exist, and they
    were one family — fn-pointer table slots written as if a fn of the
    slot's name existed (`object_drop_rest` 24 times, where the crate's
    fns of that signature are `object_drop_front`, `context_drop_rest`
    and `context_chain_drop_rest`). The prompt showed the table's fields
    and every fn, and never said which fns fit which slot."""
    want = fn_pointer_shape(field_ty)
    if want is None:
        return None
    out: list[SlotFit] = []
    for r in fn_records:
        spelled = _spelled(r)
        if spelled is None:
            continue
        sig = _field(r, "sig_text")
        shape = fn_decl_shape(sig)
        params = _decl_type_params(sig)
        binding = None if shape is None else _fits(shape, want, params)
        if binding is None:
            continue
        # A bounded parameter fits only if the type it binds satisfies the
        # bound, which this comparison cannot decide: the candidate is listed
        # with the condition, never as an unconditional fit.
        requires = "; ".join(f"{ty}: {params[p]}" for p, ty in binding.items()
                             if params.get(p))
        preds = (r.get("cfg_predicates") if isinstance(r, Mapping)
                 else getattr(r, "cfg_predicates", ()))
        out.append(SlotFit(spelled, _field(r, "file"),
                           tuple(str(p) for p in preds or ()), requires))
    return tuple(out)


def attach_slot_fits(type_records: Sequence[TypeRecord],
                     fn_records: Sequence[object]) -> list[TypeRecord]:
    """The records with `FieldRecord.fits` set on every fn-pointer field.
    Test fns are not candidates (a table never installs one)."""
    fns = [r for r in fn_records
           if not (r.get("is_test") if isinstance(r, Mapping)
                   else getattr(r, "is_test", False))]
    out: list[TypeRecord] = []
    for tr in type_records:
        if not tr.fields:
            out.append(tr)
            continue
        fields = tuple(
            replace(f, fits=fits) if (fits := slot_fits(f.ty, fns)) is not None
            else f for f in tr.fields)
        out.append(replace(tr, fields=fields) if fields != tr.fields else tr)
    return out


def offer_producers(
    measured: frozenset[FnLocator],
    batch: frozenset[FnLocator],
) -> frozenset[FnLocator]:
    """Keys `installable_fns` may see.

    The offer asks whether the escalation MEASURED a producer that builds a
    fn-pointer table, not whether this batch's slice drew one. A batch-only
    reading is why a fire that named `construct_from_std` still produced
    zero offers on every iteration that dispatched accessors (run
    `508344-41b52769`, 2026-08-22; two of seven iterations on the prior
    run). An empty measured set falls back to the batch so a caller that
    has not been wired still offers when the slice happens to contain a
    literal-builder.
    """
    return measured or batch


def _field(record: object, name: str) -> str:
    """One field of an extractor fn record, raw dict or typed record alike:
    the file route holds the extractor's dicts, the session route holds
    `FnRecord`s, and both ask the one relation below."""
    if isinstance(record, Mapping):
        return str(record.get(name, "") or "")
    return str(getattr(record, name, "") or "")


def installable_fns(
    fn_records: Sequence[object],
    type_records: Iterable[TypeRecord],
    escalated: frozenset[FnLocator],
    file_rel: str,
    *,
    reference_counts: Mapping[str, int] | None = None,
    cap: int = 12,
) -> tuple[Installable, ...]:
    """Fns of `file_rel` that a producer in `escalated` could install.

    Ordered UNREFERENCED FIRST — a fn nothing in the file names is the most
    likely missing half, and the ranking is what keeps the cap honest when a
    table has more candidates than the prompt has room for. `reference_counts`
    is the LIVE filled tree's count (`rust_idents.fn_reference_counts`), never
    the baked `_leaf_order.json`, whose numbers describe a different tree:
    `context_downcast` reads 1 there and 2 upstream.

    Returns () when nothing in `escalated` belongs to this file, which is the
    gate that keeps this from widening every prompt on every iteration: it
    fires only where the loop has already MEASURED that the writable set is not
    paying.
    """
    esc_names = {k.fn_name for k in escalated if k.file == file_rel}
    if not esc_names:
        return ()
    here = [r for r in fn_records if _field(r, "file") == file_rel]
    # Types an escalated producer builds a literal of.
    built: list[TypeRecord] = []
    for tr in type_records:
        if not tr.name or tr.kind not in ("struct", "enum"):
            continue
        if any(constructs_type(_field(r, "body_text"), tr.name)
               for r in here if _field(r, "fn_name") in esc_names):
            built.append(tr)
    if not built:
        return ()
    counts = reference_counts or {}
    out: list[Installable] = []
    seen: set[str] = set()
    for tr in built:
        wanted = [(f.name, shape) for f in tr.fields
                  if (shape := fn_pointer_shape(f.ty)) is not None]
        if not wanted:
            continue
        for r in here:
            name = _field(r, "fn_name")
            # A member of an impl block is reachable by its own container and
            # is not a free fn the table can name.
            if not name or name in seen or _field(r, "container"):
                continue
            shape = fn_decl_shape(_field(r, "sig_text"))
            if shape is None:
                continue
            for field, want in wanted:
                if _coercible(shape, want):
                    seen.add(name)
                    out.append(Installable(name, tr.name, field,
                                           counts.get(name, 0)))
                    break
    out.sort(key=lambda i: (i.references, i.fn_name))
    return tuple(out[:cap])


def table_complement(
    fn_records: Sequence[object],
    type_records: Iterable[TypeRecord],
    writable: frozenset[FnLocator],
    file_rel: str,
    *,
    reference_counts: Mapping[str, int],
) -> tuple[Installable, ...]:
    """The table slots of `file_rel` a WRITABLE table-builder owns, minus the
    ones already writable: every installable nothing installs yet, and every
    installable a writable builder's own table literal names.

    The same relation as `installable_fns`, keyed on what a route may write
    rather than on what the escalation convicted. A route whose write set is
    read off the failing tests' coverage (the slice session) can contain a
    table-builder but not the slots it fills unless the failing path runs
    them, and two kinds never run there:

    - a fn nothing installs is executed by nothing. Run `195091-72b92f1e`
      (2026-09-21, cluster `stable`): the five-test session's editable
      src/error.rs held the context-wrapping producer and every
      table-builder, the two table fns a correct producer must install were
      each referenced only by their own declaration and were not editable,
      and file-first, which offered both, never had the producer in its
      write list; run `3400-4d0618ba` spent 78 min of per-fn on the producer
      alone.
    - a fn the builder DOES install but that the failing path does not reach
      — a slot called after the step the wrong body breaks, or on the arm the
      test's failure skips. Coverage cannot vouch for it because the defect
      is upstream of it. Run `92187-f71f12bf` (2026-09-22): the four-test
      session held the builder whose literal installs a drop slot, the
      model's first reply rewrote that slot, and the splicer refused it as
      outside the editable set; the next seven turns rewrote accessors and
      the session ended flat. The same table family was refused six times
      across five runs from 2026-09-08.

    A wired slot that only a builder OUTSIDE the write set names is left
    out: this route cannot change what installs it, so coverage still
    decides it. Ordered unwired first, as `installable_fns` ranks.
    """
    here = [r for r in fn_records if _field(r, "file") == file_rel]
    names_writable = {k.fn_name for k in writable if k.file == file_rel}
    builder_bodies = [_field(r, "body_text") for r in here
                      if _field(r, "fn_name") in names_writable]
    out: list[Installable] = []
    for i in installable_fns(
            fn_records, type_records, writable, file_rel,
            reference_counts=reference_counts, cap=len(fn_records) or 1):
        if FnLocator(file_rel, i.fn_name) in writable:
            continue
        if i.unwired or any(
                constructs_type(body, i.type_name)
                and i.fn_name in identifier_tokens(body)
                for body in builder_bodies):
            out.append(i)
    return tuple(out)


def _turbofish_types(body_text: str) -> frozenset[str]:
    """Base identifiers of every ``::<...>`` type argument in ``body_text``.

    ``object_drop::<crate::error::ContextError<C, E>>`` yields ``ContextError``
    (the last path segment before the argument's own generic list). This is how
    a typed producer states WHICH type its table erases, so it is the only
    honest source for W: the table literal's field VALUES are instantiated at
    W, whereas the literal's field types never name it."""
    out: set[str] = set()
    pos = body_text.find("::<")
    while pos != -1:
        i = pos + 2  # at '<'
        depth = 0
        for j in range(i, len(body_text)):
            if body_text[j] == "<":
                depth += 1
            elif body_text[j] == ">":
                depth -= 1
                if depth == 0:
                    for arg in _split_top_level(body_text[i + 1:j]):
                        base = arg.split("<", 1)[0].strip()
                        seg = base.rsplit("::", 1)[-1].strip()
                        if seg and (seg[0].isalpha() or seg[0] == "_"):
                            out.add(seg)
                    break
        pos = body_text.find("::<", pos + 3)
    return frozenset(out)


def _table_type_names(type_records: Iterable[TypeRecord]) -> frozenset[str]:
    """Nominal types with at least one fn-pointer field — the tables."""
    return frozenset(
        tr.name for tr in type_records
        if tr.name and tr.kind in ("struct", "enum")
        and any(fn_pointer_shape(f.ty) is not None for f in tr.fields))


def wrapper_producers(
    fn_records: Sequence[Mapping[str, object]],
    type_records: Iterable[TypeRecord],
    escalated: frozenset[FnLocator],
) -> tuple[tuple[str, str, str], ...]:
    """``(wrapper W, producer fn, producer file)`` for every typed producer in
    the MEASURED escalated set, whatever file it lives in.

    A typed producer is an escalated fn whose body constructs a fn-pointer
    TABLE literal and whose turbofish instantiations name a crate nominal type:
    that type is the wrapper W the table erases. `construct_from_std` is
    correctly NOT one — its instantiations are all the generic parameter `E`,
    which names every type and therefore none (the `type_production`
    discipline). The type check is what keeps the closure from re-admitting
    `From`/`new`, the accessor slice the record already convicted."""
    trs = list(type_records)
    tables = _table_type_names(trs)
    if not tables:
        return ()
    nominal = frozenset(tr.name for tr in trs
                        if tr.name and tr.kind in ("struct", "enum"))
    esc = {(k.file, k.fn_name) for k in escalated}
    out: list[tuple[str, str, str]] = []
    for r in fn_records:
        f, n = str(r.get("file", "")), str(r.get("fn_name", ""))
        if (f, n) not in esc:
            continue
        body = str(r.get("body_text", "") or "")
        if not any(constructs_type(body, t) for t in tables):
            continue
        for w in sorted(_turbofish_types(body) & (nominal - tables)):
            out.append((w, n, f))
    return tuple(sorted(set(out)))


def _is_tuple_struct(tr: TypeRecord) -> bool:
    """The extractor names a tuple struct's fields ``0``, ``1``, … — so its
    construction in a body is a CALL (`MessageError(message)`), never a brace
    literal."""
    return (tr.kind == "struct" and bool(tr.fields)
            and all(f.name.isdigit() for f in tr.fields))


def _skip_turbofish(body: str, p: int) -> int:
    """Index just past a ``::<…>`` starting at `p` (angle-depth aware, so
    ``::<ContextError<C, E>>`` is one unit), or `p` when none starts there."""
    if not body.startswith("::<", p):
        return p
    depth = 0
    for j in range(p + 2, len(body)):
        if body[j] == "<":
            depth += 1
        elif body[j] == ">":
            depth -= 1
            if depth == 0:
                return j + 1
    return p


def calls_unit(
    body: str, caller_self: str, name: str, *,
    callee_self_types: frozenset[str], callee_is_free: bool,
    callee_is_method: bool,
) -> bool:
    """True iff `body` CALLS a fn named `name`, resolved on how the call is
    written rather than on the name occurring as a token:

    - a path call ``Q::name(`` resolves when ``Q`` is the callee's impl
      self-type (``crate::Error::msg(`` → the `msg` on `impl Error`) or
      ``Self`` inside the callee's own impl (`caller_self`); ``Box::new(``
      and ``String::from(`` name std types and resolve to nothing;
    - a method call ``.name(`` resolves to any impl/trait method of that name
      (the pipeline's own unit identity, `FnLocator`, already erases the
      container, so the ambiguity is the loop's, not this function's);
    - a bare ``name(`` resolves to a free fn of that name; the declaration
      ``fn name(`` is not a call.

    A turbofish between the name and the paren (``::<E>(``) is a call too.
    The token test this replaces counted `new`/`from`/`context` as calls of
    the crate's `new`/`from`/`context` in nearly every body they appear in."""
    n = len(body)
    i = 0
    while (k := body.find(name, i)) != -1:
        i = k + 1
        e = k + len(name)
        if (k > 0 and is_ident_char(body[k - 1])) or (
                e < n and is_ident_char(body[e])):
            continue
        p = e
        while p < n and body[p].isspace():
            p += 1
        p2 = _skip_turbofish(body, p)
        if p2 != p:
            p = p2
            while p < n and body[p].isspace():
                p += 1
        if p >= n or body[p] != "(":
            continue
        j = k - 1
        while j >= 0 and body[j].isspace():
            j -= 1
        if j >= 1 and body[j] == ":" and body[j - 1] == ":":
            q_end = j - 2
            q_start = q_end
            while q_start >= 0 and is_ident_char(body[q_start]):
                q_start -= 1
            qual = body[q_start + 1:q_end + 1]
            if qual == "Self":
                qual = caller_self
            if qual and qual in callee_self_types:
                return True
            continue
        if j >= 0 and body[j] == ".":
            if callee_is_method:
                return True
            continue
        if (j >= 1 and body[j - 1:j + 1] == "fn"
                and (j < 2 or not is_ident_char(body[j - 2]))):
            continue  # `fn name(` — a declaration, not a call
        if callee_is_free:
            return True
    return False


def producer_dispatch_ranks(
    escalated: frozenset[FnLocator],
    fn_records: Sequence[Mapping[str, object]],
    type_records: Iterable[TypeRecord],
) -> tuple[frozenset[FnLocator], frozenset[FnLocator]]:
    """`(builders, callees)` over the escalated set, for the ordering of the
    escalated cohort (`dispatch_batch.promote_producers`) — pure over records,
    no LLM.

    `builders` are the escalated units whose body CONSTRUCTS a crate nominal
    type: a fn-pointer table literal (`ErrorVTable { … }`), the funnel's
    payload literal (`ErrorImpl { … }`), or a wrapper's tuple constructor
    (`MessageError(message)`). That is the escalation's own subject — "the
    defect is in what this fn constructs" — and it is a ROLE, read off the
    body's shape, not a name and not the current body's correctness. It has
    to be: on the stable 98-tree the Defect-1 wrong body,
    `construct_from_adhoc` = `Self::construct_from_std(MessageError(message),
    bt)`, builds no table precisely because it is wrong, yet it constructs
    `MessageError` and so ranks with the table-builders it should be one of.
    The first draft ranked "constructs a table, or is named `construct`", and
    a walk of run `95878-aaf5bf1d`'s 27-unit cohort (2026-08-25) classified
    exactly the two wrong constructors as non-builders: `construct_from_adhoc`
    landed at slot 12 and `construct_from_display` at slot 13 — outside the
    first batch the ordering exists to put them in.

    `callees` are the escalated units another escalated unit CALLS, resolved
    by `calls_unit` (path call on the callee's self-type or `Self`, method
    call, or bare free-fn call). The first draft tested identifier-token
    presence, and `new` / `from` / `context` occur as tokens in nearly every
    body (`Box::new`, `String::from`, the `context` field), so the same walk
    promoted the delegators the ordering exists to demote — `error.rs::new`,
    `kind.rs::new`, `from`, `from_boxed`, `context.rs::context` — into slots
    6–11 as "callees", ahead of the constructors."""
    trs = list(type_records)
    nominal = [tr for tr in trs if tr.name and tr.kind in ("struct", "enum")]
    esc = {(k.file, k.fn_name) for k in escalated}
    recs: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in fn_records:
        key = (str(r.get("file", "")), str(r.get("fn_name", "")))
        if key in esc:
            recs.setdefault(key, []).append((
                str(r.get("container", "") or ""),
                str(r.get("body_text", "") or "")))
    builders: set[FnLocator] = set()
    for (f, n), items in recs.items():
        if any(constructs_type(body, tr.name, tuple_struct=_is_tuple_struct(tr))
               for _, body in items for tr in nominal):
            builders.add(FnLocator(f, n))
    shapes: dict[tuple[str, str], tuple[frozenset[str], bool, bool]] = {}
    for key, items in recs.items():
        selfs = frozenset(
            s for c, _ in items if c and (s := impl_self_type(c)) is not None)
        shapes[key] = (selfs, any(not c for c, _ in items),
                       any(bool(c) for c, _ in items))
    callees: set[FnLocator] = set()
    for (f, n), items in recs.items():
        for container, body in items:
            caller_self = (impl_self_type(container) or "") if container else ""
            for (f2, n2), (selfs, free, method) in shapes.items():
                if (f2, n2) == (f, n) or FnLocator(f2, n2) in callees:
                    continue
                if calls_unit(body, caller_self, n2, callee_self_types=selfs,
                              callee_is_free=free, callee_is_method=method):
                    callees.add(FnLocator(f2, n2))
    return frozenset(builders), frozenset(callees)


def producer_bypasses(
    fn_records: Sequence[Mapping[str, object]],
    type_records: Iterable[TypeRecord],
    escalated: frozenset[FnLocator],
    file_rel: str,
    *,
    # Same bound as the installable offer's, for the same reason (prompt
    # room); on the live tree the census finds 3 across both files.
    cap: int = 12,
) -> tuple[ProducerBypass, ...]:
    """Fns of ``file_rel`` that construct a wrapper W without invoking W's
    typed producer — the call sites the closure makes writable.

    Each hit carries ``erases_generic``, the fact the INSTRUCTION split keys
    on (see the field): a generic-self hit may be told to invoke the producer,
    a concrete-self hit must be told to build W's own per-type table and the
    producer's name must stay out of its instruction entirely — told with an
    offer line naming the call, the model complied over the compiler three
    times (run 57025-6ba652f2, identical `E0277` at 17:45/17:55/18:05 UTC).

    The predicate, per fn of the prompted file: the body constructs a struct
    literal of W (`constructs_type` — a TOKEN match, so `E` never matches
    `Error`), the producer's name is absent from the body's identifier tokens
    (a token test too: a body calling `construct_from_context` must not read
    as calling `construct`), and the fn is not itself a table builder (the
    producer constructs W inside its own body by definition and must not
    convict itself). Deterministic, extractor-based, no LLM, no name
    heuristics — the same contract as `installable_fns`.

    Returns () when the measured set holds no typed producer, which is the
    same gate as the installable offer: this fires only where the loop has
    already MEASURED that the writable set is not paying."""
    trs = list(type_records)
    relations = wrapper_producers(fn_records, trs, escalated)
    if not relations:
        return ()
    tables = _table_type_names(trs)
    out: list[ProducerBypass] = []
    for r in fn_records:
        if str(r.get("file", "")) != file_rel:
            continue
        name = str(r.get("fn_name", ""))
        body = str(r.get("body_text", "") or "")
        if not name or not body:
            continue
        if any(constructs_type(body, t) for t in tables):
            continue  # a table builder IS a producer, not a bypass
        tokens = frozenset(identifier_tokens(body))
        for w, producer, pfile in relations:
            if name == producer and str(r.get("file", "")) == pfile:
                continue
            if constructs_type(body, w) and producer not in tokens:
                out.append(ProducerBypass(
                    fn_name=name,
                    container=" ".join(
                        str(r.get("container", "")).split("{", 1)[0].split()),
                    wrapper=w, producer=producer, producer_file=pfile,
                    same_file=pfile == file_rel,
                    erases_generic=bool(r.get("impl_self_generic", False))))
                break
    out.sort(key=lambda b: (b.fn_name, b.container))
    return tuple(out[:cap])


def panic_file_stems(crash_text: str) -> frozenset[str]:
    """Bare ``.rs`` stems named in a panic or crash excerpt.

    ``panicked at /rustc/…/library/std/src/backtrace.rs:183`` yields
    ``backtrace``. A stdlib panic names no crate frame; the stem is how a
    callee's return type is matched to the panic without cataloguing a
    language type.
    """
    out: set[str] = set()
    for raw in (crash_text or "").replace("\\", "/").splitlines():
        # Binary lists in a CRASH preview name tests/foo.rs; those are
        # not the panic site. Only the rustc/std `panicked at` line
        # (or an ASAN frame) names the module whose type the callee
        # returned.
        if "panicked at" not in raw and "asan" not in raw.lower():
            continue
        for part in raw.replace(":", " ").split():
            if ".rs" not in part:
                continue
            head = part.split(".rs", 1)[0]
            stem = head.rsplit("/", 1)[-1].strip()
            if stem:
                out.add(stem.lower())
    return frozenset(out)


def _return_idents(sig: str) -> frozenset[str]:
    """Lowercased identifier tokens of a signature's return type."""
    arrow = (sig or "").find("->")
    if arrow < 0:
        return frozenset()
    return frozenset(t.lower() for t in identifier_tokens(sig[arrow + 2:]))


def crash_vtable_callees(
    failed_body: str,
    crash_text: str,
    fn_records: Sequence[Mapping[str, object]],
    type_records: Iterable[TypeRecord],
    *,
    cap: int = 12,
) -> frozenset[FnLocator]:
    """Fns a compiling fill's crash-revert actually ran through a table.

    Inverse of :func:`installable_fns`: that starts from a producer that
    BUILDS a fn-pointer table; this starts from a body that compiled,
    crash-reverted with a panic outside the write set, and CALLED a crate
    fn whose return type matches the panic file stem, whose body loads a
    table field. The locators are the free fns whose signature matches
    that field — the same match installable_fns uses.

    Run ``269600-aa403030`` fill 1 retired the as_ptr lesson and wrote
    the type-correct call; the splice compiled and crash-reverted (net
    -31, panic in std backtrace Debug). pair_wait then deferred the file
    that holds the table's installed fn. Empty crash text, no matching
    return type, or no field load returns empty, so recorded fills that
    did not crash this way are unchanged.
    """
    stems = panic_file_stems(crash_text)
    if not stems or not (failed_body or "").strip():
        return frozenset()
    records = list(fn_records)
    bodies = [failed_body]
    for r in records:
        name = str(r.get("fn_name") or "")
        if not name or not names_method_call(failed_body, name):
            continue
        if not (_return_idents(str(r.get("sig_text") or "")) & stems):
            continue
        text = str(r.get("body_text") or "")
        if text:
            bodies.append(text)
    wanted: list[tuple[bool, tuple[str, ...], str]] = []
    seen_shape: set[tuple[bool, tuple[str, ...], str]] = set()
    for tr in type_records:
        if not tr.name or tr.kind not in ("struct", "enum"):
            continue
        for f in tr.fields:
            shape = fn_pointer_shape(f.ty)
            if shape is None or not f.name:
                continue
            if not any(names_method_call(b, f.name) for b in bodies):
                continue
            if shape in seen_shape:
                continue
            seen_shape.add(shape)
            wanted.append(shape)
    if not wanted:
        return frozenset()
    out: list[FnLocator] = []
    seen: set[tuple[str, str]] = set()
    for r in records:
        name = str(r.get("fn_name") or "")
        file_ = str(r.get("file") or "")
        if not name or not file_ or str(r.get("container") or ""):
            continue
        key = (file_, name)
        if key in seen:
            continue
        shape = fn_decl_shape(str(r.get("sig_text") or ""))
        if shape is None:
            continue
        if any(_coercible(shape, want) for want in wanted):
            seen.add(key)
            out.append(FnLocator(file_, name))
            if len(out) >= cap:
                break
    return frozenset(out)


def crash_vtable_callees_from_store(
    records: Mapping[str, object],
    fn_records: Sequence[Mapping[str, object]],
    type_records: Iterable[TypeRecord],
) -> frozenset[FnLocator]:
    """Union of :func:`crash_vtable_callees` over stored fill records that
    still hold a compiling crash-revert's body and panic text.

    A pair that crash-reverted then rolled back leaves no sweep crash to
    localize; the durable memory is the retro store's last_failed_body
    and last_error_preview / regressed_output. Empty store, or records
    with no crash excerpt, returns empty.
    """
    out: set[FnLocator] = set()
    for raw in (records or {}).values():
        if not isinstance(raw, dict):
            continue
        body = str(raw.get("last_failed_body") or "")
        if not body.strip():
            continue
        parts = [str(raw.get("last_error_preview") or "")]
        extra = raw.get("regressed_output")
        if isinstance(extra, dict):
            parts.extend(str(v) for v in extra.values())
        crash = "\n".join(parts)
        if "CRASH" not in crash.upper() and "panicked at" not in crash:
            continue
        out |= crash_vtable_callees(body, crash, fn_records, type_records)
    return frozenset(out)
