"""The SHAPE of a type declaration — what a body written against it depends on.

Two bakes of one file declare the same type in whatever spelling their authored
layout happened to choose. A body cares about what the declaration MEANS: which
fields exist, what each one's type resolves to, who may see it, which generic
parameters carry which bounds, and — under `#[repr(C)]` — the field order. It
does not care whether a bound is written inline or in a `where` clause, whether a
path is spelled through an import or in full, whether the last field carries a
trailing comma, or what the doc comments say.

The text comparison this replaces could not tell those apart. Measured on
`anyhow`, 2026-09-15, run `119837-09e3ac57`, cluster `nightly` entry after
`stable` closed at 125: the cross-cluster seed refused 34 of `stable`'s verified
bodies as written against a differently shaped type. Every `ptr.rs` body (0 of
19 adopted) was refused because `stable` wrote `pub struct Own<T: ?Sized> { pub
ptr: core::ptr::NonNull<T> }` and `nightly` wrote `pub struct Own<T> where T:
?Sized { pub ptr: NonNull<T> }` under `use core::ptr::NonNull;`; `Error` differed
by a trailing comma; `ErrorVTable` by `crate::StdError` against an imported
`StdError`. The field scan also read the where-clause bound `T:` as a field named
`T`, so every body mentioning `T` was refused. The cluster then rebuilt that
pointer layer through the fill loop, and the first half-built state aborted ten
test binaries (pass 42 -> 20).

Pure domain: token-level, no IO. A declaration this module cannot parse is
reported unparseable and the caller keeps its conservative whole-type answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Path", "ItemShape", "use_bindings", "item_shape", "shape_divergence",
           "declaration_field_names"]

_PATH_ROOTS = frozenset({"crate", "self", "super"})
# Every re-export of `core` under `std`/`alloc` names the same item, and a
# spelling choice between them is not a shape.
_CORE_ALIASES = frozenset({"std", "alloc"})
_NOT_A_PATH = frozenset({
    "dyn", "fn", "unsafe", "mut", "const", "extern", "impl", "for", "where", "as",
    "pub", "struct", "enum", "union", "trait", "type", "static", "ref", "in",
    "use", "mod", "Self",
})


@dataclass(frozen=True, slots=True)
class Path:
    """A resolved path. `open` marks a bare name no import and no local item
    binds (prelude, glob import, extern-prelude): it denotes whatever path ends
    in that name, and compares by last segment against a closed path."""

    segs: tuple[str, ...]
    open: bool = False


Tok = str | Path


def _tok_eq(a: Tok, b: Tok) -> bool:
    if isinstance(a, Path) and isinstance(b, Path):
        if a.open or b.open:
            return a.segs[-1] == b.segs[-1]
        return a.segs == b.segs
    if isinstance(a, Path) or isinstance(b, Path):
        return False
    return a == b


def _seq_eq(a: Sequence[Tok], b: Sequence[Tok]) -> bool:
    return len(a) == len(b) and all(_tok_eq(x, y) for x, y in zip(a, b, strict=True))


def _bag_eq(a: Sequence[Sequence[Tok]], b: Sequence[Sequence[Tok]]) -> bool:
    """Multiset equality under `_seq_eq` — bound lists are unordered."""
    if len(a) != len(b):
        return False
    rest = list(b)
    for x in a:
        for k, y in enumerate(rest):
            if _seq_eq(x, y):
                del rest[k]
                break
        else:
            return False
    return True


# --- lexing --------------------------------------------------------------

def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _lex(text: str) -> list[str]:
    """Tokens of `text` with comments (doc comments included) and whitespace
    dropped. String literals stay one token each — `feature = "std"` and
    `feature = "alloc"` are different attributes."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif c == "'":
            j = i + 1
            while j < n and _is_ident_char(text[j]):
                j += 1
            if j < n and text[j] == "'":       # char literal
                out.append(text[i:j + 1])
                i = j + 1
            else:                               # lifetime
                out.append(text[i:j])
                i = j
        elif _is_ident_start(c) or c.isdigit():
            j = i + 1
            while j < n and _is_ident_char(text[j]):
                j += 1
            out.append(text[i:j])
            i = j
        elif text.startswith("::", i) or text.startswith("->", i) \
                or text.startswith("=>", i):
            out.append(text[i:i + 2])
            i += 2
        else:
            out.append(c)
            i += 1
    return _drop_trailing_commas(out)


def _drop_trailing_commas(toks: list[str]) -> list[str]:
    """A comma before a closing delimiter is spelling — `fn(Own<ErrorImpl>,)`
    is `fn(Own<ErrorImpl>)` — except the one that makes `(T,)` a 1-tuple
    rather than a parenthesised `T`: kept when its group holds a single element
    and the `(` does not open an argument list (`fn(`, `Fn(`, a tuple struct)."""
    out: list[str] = []
    stack: list[tuple[int, int, bool]] = []    # (index in out, top-level commas, is arg list)
    for t in toks:
        if t in ("(", "[", "{", "<"):
            args = t == "(" and bool(out) and (_is_ident_start(out[-1][:1]) or out[-1] == ">")
            stack.append((len(out), 0, args))
            out.append(t)
            continue
        if t in (")", "]", "}", ">") and stack:
            start, commas, args = stack.pop()
            if out and out[-1] == "," and len(out) - 1 > start + 1:
                single_tuple = t == ")" and not args and commas == 1
                if not single_tuple:
                    out.pop()
            out.append(t)
            continue
        if t == "," and stack:
            s, c, a = stack[-1]
            stack[-1] = (s, c + 1, a)
        out.append(t)
    return out


_OPEN = {"(": ")", "[": "]", "{": "}", "<": ">"}


def _match(toks: Sequence[str], i: int) -> int:
    """Index of the delimiter closing the one at `i`, or -1. Angle brackets
    count only against each other; `->` and `=>` are single tokens, so the `>`
    of a return arrow is never mistaken for a closer."""
    want = _OPEN[toks[i]]
    depth = 0
    for k in range(i, len(toks)):
        t = toks[k]
        if t == toks[i]:
            depth += 1
        elif t == want:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _split_top(toks: Sequence[str], sep: str) -> list[list[str]]:
    """`toks` split on `sep` outside any bracket pair; empty pieces (a trailing
    comma) dropped."""
    parts: list[list[str]] = [[]]
    depth = 0
    for t in toks:
        if t in ("(", "[", "{", "<"):
            depth += 1
        elif t in (")", "]", "}", ">"):
            depth -= 1
        if t == sep and depth == 0:
            parts.append([])
        else:
            parts[-1].append(t)
    return [p for p in parts if p]


# --- imports -------------------------------------------------------------

def use_bindings(text: str) -> dict[str, tuple[str, ...]]:
    """Name -> full path for every file-level `use` in `text`. Globs bind
    nothing. A name two imports bind to different paths (cfg alternatives) is
    left unbound, so it resolves open rather than to a guess."""
    from .rust_use_items import use_items

    bound: dict[str, tuple[str, ...]] = {}
    clash: set[str] = set()
    for item in use_items(text):
        toks = _lex(item.text)
        if "use" not in toks:
            continue
        body = toks[toks.index("use") + 1:]
        if body and body[-1] == ";":
            body = body[:-1]
        for name, path in _use_tree(body, ()):
            if name in bound and bound[name] != path:
                clash.add(name)
            bound[name] = path
    for name in clash:
        bound.pop(name, None)
    return bound


def _use_tree(toks: Sequence[str], prefix: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
    segs = list(prefix)
    i = 0
    if toks and toks[0] == "::":
        i = 1
    while i < len(toks):
        t = toks[i]
        if t == "{":
            close = _match(toks, i)
            out: list[tuple[str, tuple[str, ...]]] = []
            for part in _split_top(list(toks[i + 1:close]), ","):
                out.extend(_use_tree(part, tuple(segs)))
            return out
        if t == "*":
            return []
        if t == "as" and i + 1 < len(toks):
            if toks[i + 1] == "_":
                return []
            return [(toks[i + 1], _core_root(tuple(segs)))] if segs else []
        if t == "self" and segs:
            # `{self}` binds the prefix's last segment, `{self as q}` binds `q`
            # to the prefix: `self` names the prefix, never a segment of it.
            i += 1
            continue
        if t != "::":
            segs.append(t)
        i += 1
    if not segs:
        return []
    return [(segs[-1], _core_root(tuple(segs)))]


def _core_root(segs: tuple[str, ...]) -> tuple[str, ...]:
    if segs and segs[0] in _CORE_ALIASES:
        return ("core",) + segs[1:]
    return segs


# --- declarations --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Scope:
    bindings: dict[str, tuple[str, ...]]
    local_items: frozenset[str]
    params: frozenset[str]


def _resolve(toks: Sequence[str], scope: _Scope) -> list[Tok]:
    """Paths in `toks` replaced by `Path` values, resolved through the file's
    imports; everything else kept as its token."""
    out: list[Tok] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        absolute = t == "::" and i + 1 < n and _is_ident_start(toks[i + 1][:1])
        if not (absolute or (_is_ident_start(t[:1]) and t not in _NOT_A_PATH)):
            out.append(t)
            i += 1
            continue
        j = i + 1 if absolute else i
        segs = [toks[j]]
        j += 1
        while j + 1 < n and toks[j] == "::" and _is_ident_start(toks[j + 1][:1]):
            segs.append(toks[j + 1])
            j += 2
        head = segs[0]
        if len(segs) == 1 and not absolute and head in scope.params:
            out.append(head)
        elif absolute or head in _PATH_ROOTS:
            out.append(Path(_core_root(tuple(segs))))
        elif head in scope.bindings:
            out.append(Path(_core_root(scope.bindings[head] + tuple(segs[1:]))))
        elif len(segs) == 1 and head in scope.local_items:
            out.append(Path(("self", head)))
        elif len(segs) == 1:
            out.append(Path((head,), open=True))
        else:
            out.append(Path(_core_root(tuple(segs))))
        i = j
    return out


@dataclass(frozen=True, slots=True)
class _Param:
    kind: str                           # "lt" | "ty" | "const"
    name: str
    bounds: tuple[tuple[Tok, ...], ...]
    default: tuple[Tok, ...]


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    vis: tuple[str, ...]
    attrs: frozenset[tuple[str, ...]]
    ty: tuple[Tok, ...]


@dataclass(frozen=True, slots=True)
class ItemShape:
    """One parsed declaration. `fields` is set for a braced struct/union,
    `tuple_fields` for a tuple struct, `body` for everything else braced (enum
    variants); at most one of the three is non-empty."""

    kind: str
    name: str
    attrs: frozenset[tuple[str, ...]]
    params: tuple[_Param, ...]
    where_extra: tuple[tuple[Tok, ...], ...]
    fields: tuple[_Field, ...] = ()
    tuple_fields: tuple[tuple[tuple[str, ...], tuple[Tok, ...]], ...] = ()
    body: tuple[Tok, ...] = ()

    @property
    def is_repr_c(self) -> bool:
        return any(a and a[0] == "repr" and "C" in a for a in self.attrs)


def _take_attrs(toks: Sequence[str], i: int) -> tuple[frozenset[tuple[str, ...]], int]:
    attrs: set[tuple[str, ...]] = set()
    while i + 1 < len(toks) and toks[i] == "#" and toks[i + 1] == "[":
        close = _match(toks, i + 1)
        if close < 0:
            break
        inner = tuple(toks[i + 2:close])
        if inner and inner[0] != "doc":
            attrs.add(inner)
        i = close + 1
    return frozenset(attrs), i


def _take_vis(toks: Sequence[str], i: int) -> tuple[tuple[str, ...], int]:
    if i < len(toks) and toks[i] == "pub":
        if i + 1 < len(toks) and toks[i + 1] == "(":
            close = _match(toks, i + 1)
            return tuple(toks[i:close + 1]), close + 1
        return ("pub",), i + 1
    return (), i


def _bounds(toks: Sequence[str], scope: _Scope) -> list[tuple[Tok, ...]]:
    return [tuple(_resolve(b, scope)) for b in _split_top(toks, "+")]


def item_shape(text: str, kind: str, name: str) -> ItemShape | None:
    """The shape of the `kind name` declaration in `text`, with paths resolved
    through `text`'s own imports and items — or None when the declaration is
    absent or not a form this parser reads (it never guesses)."""
    from .rust_scan import item_names, locate_item_declaration

    span = locate_item_declaration(text, kind, name)
    if span is None:
        return None
    toks = _lex(text[span[0]:span[1]])
    attrs, i = _take_attrs(toks, 0)
    _vis, i = _take_vis(toks, i)
    if i + 1 >= len(toks) or toks[i] != kind or toks[i + 1] != name:
        return None
    i += 2
    raw_params: list[list[str]] = []
    if i < len(toks) and toks[i] == "<":
        close = _match(toks, i)
        if close < 0:
            return None
        raw_params = _split_top(toks[i + 1:close], ",")
        i = close + 1
    param_names = []
    for p in raw_params:
        head = p[1] if p[0] == "const" and len(p) > 1 else p[0]
        param_names.append(head)
    scope = _Scope(use_bindings(text), frozenset(item_names(text)) - {name},
                   frozenset(n for n in param_names if not n.startswith("'")))
    bounds: dict[str, list[tuple[Tok, ...]]] = {n: [] for n in param_names}
    defaults: dict[str, tuple[Tok, ...]] = {}
    kinds: dict[str, str] = {}
    for p, pname in zip(raw_params, param_names, strict=True):
        if p[0] == "const":
            kinds[pname] = "const"
            eq = p.index("=") if "=" in p else len(p)
            bounds[pname] = [tuple(_resolve(p[3:eq], scope))]
            defaults[pname] = tuple(_resolve(p[eq + 1:], scope))
            continue
        kinds[pname] = "lt" if pname.startswith("'") else "ty"
        rest = p[1:]
        eq = rest.index("=") if "=" in rest else len(rest)
        if rest[:1] == [":"]:
            bounds[pname].extend(_bounds(rest[1:eq], scope))
        defaults[pname] = tuple(_resolve(rest[eq + 1:], scope))

    tuple_fields: list[tuple[tuple[str, ...], tuple[Tok, ...]]] = []
    if kind == "struct" and i < len(toks) and toks[i] == "(":
        close = _match(toks, i)
        if close < 0:
            return None
        for part in _split_top(toks[i + 1:close], ","):
            _a, k = _take_attrs(part, 0)
            vis, k = _take_vis(part, k)
            tuple_fields.append((vis, tuple(_resolve(part[k:], scope))))
        i = close + 1

    where_extra: list[tuple[Tok, ...]] = []
    if i < len(toks) and toks[i] == "where":
        j = i + 1
        while j < len(toks) and toks[j] not in ("{", ";"):
            j += 1
        for pred in _split_top(toks[i + 1:j], ","):
            if ":" not in pred:
                return None
            colon = pred.index(":")
            lhs, rhs = pred[:colon], pred[colon + 1:]
            if len(lhs) == 1 and lhs[0] in bounds:
                bounds[lhs[0]].extend(_bounds(rhs, scope))
            else:
                where_extra.append(tuple(_resolve(pred, scope)))
        i = j

    params = tuple(
        _Param(kinds[n], n, tuple(bounds[n]), defaults.get(n, ()))
        for n in param_names)
    if tuple_fields or (i < len(toks) and toks[i] == ";"):
        return ItemShape(kind, name, attrs, params, tuple(where_extra),
                         tuple_fields=tuple(tuple_fields))
    if i >= len(toks) or toks[i] != "{":
        return None
    close = _match(toks, i)
    if close < 0:
        return None
    inner = toks[i + 1:close]
    if kind not in ("struct", "union"):
        return ItemShape(kind, name, attrs, params, tuple(where_extra),
                         body=tuple(_resolve(inner, scope)))
    fields: list[_Field] = []
    for part in _split_top(inner, ","):
        f_attrs, k = _take_attrs(part, 0)
        vis, k = _take_vis(part, k)
        if k + 1 >= len(part) or part[k + 1] != ":":
            return None
        fields.append(_Field(part[k], vis, f_attrs, tuple(_resolve(part[k + 2:], scope))))
    return ItemShape(kind, name, attrs, params, tuple(where_extra), fields=tuple(fields))


def _params_eq(a: Sequence[_Param], b: Sequence[_Param]) -> bool:
    return len(a) == len(b) and all(
        x.kind == y.kind and x.name == y.name and _bag_eq(x.bounds, y.bounds)
        and _seq_eq(x.default, y.default)
        for x, y in zip(a, b, strict=True))


WHOLE = "whole"


def shape_divergence(target: ItemShape, donor: ItemShape) -> str | frozenset[str]:
    """How two shapes of one type differ, in the terms a body depends on.

    * `frozenset()` — same shape; no body is refused on its account.
    * a set of FIELD names — only those fields changed (type, visibility or
      attributes), or exist on one side only. A body reaches a field by name, so
      naming the field is what a body that depends on the change must do. When
      the field SET differs, the type name is in the set too: a struct literal
      written against the donor lists the donor's fields and never names the one
      the target added.
    * `WHOLE` — the type itself differs (kind, attributes, generics, where
      predicates, variant list, tuple fields, or field ORDER under `#[repr(C)]`,
      which is a layout contract a cast between instantiations relies on); the
      caller refuses the type name and every field of both sides."""
    if (target.kind != donor.kind or target.attrs != donor.attrs
            or not _params_eq(target.params, donor.params)
            or not _bag_eq(target.where_extra, donor.where_extra)
            or not _seq_eq(target.body, donor.body)
            or len(target.tuple_fields) != len(donor.tuple_fields)
            or any(tv != dv or not _seq_eq(tt, dt) for (tv, tt), (dv, dt)
                   in zip(target.tuple_fields, donor.tuple_fields, strict=True))):
        return WHOLE
    t_fields = {f.name: f for f in target.fields}
    d_fields = {f.name: f for f in donor.fields}
    if target.is_repr_c:
        t_order = [f.name for f in target.fields if f.name in d_fields]
        d_order = [f.name for f in donor.fields if f.name in t_fields]
        if t_order != d_order:
            return WHOLE
    changed = set(t_fields) ^ set(d_fields)
    out = set(changed)
    if changed:
        out.add(target.name)
    for name in set(t_fields) & set(d_fields):
        t, d = t_fields[name], d_fields[name]
        if t.vis != d.vis or t.attrs != d.attrs or not _seq_eq(t.ty, d.ty):
            out.add(name)
    return frozenset(out)


def declaration_field_names(shape: ItemShape | None, decl: str) -> set[str]:
    """Field names a body can reach on this type. Read from the parsed fields
    when there are any; otherwise from the braced body only — never from the
    generics or `where` clause, whose `T: ?Sized` is a bound, not a field."""
    if shape is not None and shape.fields:
        return {f.name for f in shape.fields}
    toks = _lex(decl)
    if "{" not in toks:
        return set()
    open_ = toks.index("{")
    close = _match(toks, open_)
    inner = toks[open_ + 1:close if close > 0 else len(toks)]
    out: set[str] = set()
    for k in range(len(inner) - 1):
        if inner[k + 1] == ":" and _is_ident_start(inner[k][:1]) \
                and (k == 0 or inner[k - 1] in (",", "{", "(", "pub", ")", "]")):
            out.add(inner[k])
    return out
