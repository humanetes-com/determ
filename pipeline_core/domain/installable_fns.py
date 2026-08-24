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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pipeline_core.domain.models import FnLocator, TypeRecord
from pipeline_core.domain.rust_idents import identifier_tokens
from pipeline_core.domain.type_production import constructs_type


@dataclass(frozen=True, slots=True)
class Installable:
    """One fn a producer could install, and the field that would hold it."""

    fn_name: str
    type_name: str        # the struct whose field would hold it
    field: str            # the field's name
    references: int       # occurrences of the fn's name in the file


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
    word-separating space (`dyn StdError`) and loses the rest on both sides."""
    t = _norm(text)
    out: list[str] = []
    for i, ch in enumerate(t):
        if ch == " " and not (
                i > 0 and i + 1 < len(t)
                and _is_ident(t[i - 1]) and _is_ident(t[i + 1])):
            continue
        out.append(ch)
    return "".join(out)


def _is_ident(c: str) -> bool:
    return c.isalnum() or c == "_"


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
    return (d_params == f_params and d_ret == f_ret
            and (f_unsafe or not d_unsafe))

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


def installable_fns(
    fn_records: Sequence[Mapping[str, object]],
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
    here = [r for r in fn_records if str(r.get("file", "")) == file_rel]
    # Types an escalated producer builds a literal of.
    built: list[TypeRecord] = []
    for tr in type_records:
        if not tr.name or tr.kind not in ("struct", "enum"):
            continue
        if any(constructs_type(str(r.get("body_text", "") or ""), tr.name)
               for r in here if str(r.get("fn_name", "")) in esc_names):
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
            name = str(r.get("fn_name", ""))
            # A member of an impl block is reachable by its own container and
            # is not a free fn the table can name.
            if not name or name in seen or str(r.get("container", "")):
                continue
            shape = fn_decl_shape(str(r.get("sig_text", "") or ""))
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
