"""The frozen HEAD of a type declaration, split from its authorable body.

A public struct is two different things in one declaration. Its head — the
visibility, the `struct` keyword, the name, the generic parameters and their
bounds — IS the crate's external contract: users name it, bound it, and match
its arity. Its FIELDS are not: a private field is invisible outside the crate,
so what the type holds is an implementation choice, not API.

The layout-authoring stage needs that split as a first-class concept. When the
model is allowed to design private fields of public structs, the head must come
back byte-equivalent (modulo whitespace) or the "frozen public surface" promise
is silently broken — a renamed generic or a dropped bound compiles fine here and
breaks the API contract everywhere else.

Pure text→VO; mask-aware (comments/strings never parse as code), no regex.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.rust_lex import code_mask

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CHARS = _IDENT_START | set("0123456789")
_TYPE_KEYWORDS = {"struct", "enum", "union", "trait", "type"}
_OPENERS = {"{": "}", "(": ")", "[": "]"}


@dataclass(frozen=True, slots=True)
class TypeHeader:
    """The part of a type declaration that stays frozen when its fields do not.

    `generics` is whitespace-stripped so `<T, E>` and `<T,E>` compare equal;
    bounds are KEPT (`<T: ?Sized>` ≠ `<T>` — a dropped bound is an API change).
    """

    vis: str        # "", "pub", "pub(crate)", …
    kind: str       # "struct" | "enum" | "union" | "trait" | "type"
    name: str
    generics: str   # "<T,E>" or "" — whitespace removed


def _skip_group(text: str, mask: list[bool], i: int) -> int:
    """Index just past the group opened at `i` (`{`, `(` or `[`)."""
    opener = text[i]
    closer = _OPENERS[opener]
    depth = 0
    n = len(text)
    while i < n:
        if not mask[i]:
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return n


def _skip_generics(text: str, mask: list[bool], i: int) -> int:
    """Index just past the `<…>` opened at `i`, counting nested angles and
    ignoring the `>` of a `->` return arrow (`F: Fn(A) -> B` is a legal bound)."""
    depth = 0
    n = len(text)
    while i < n:
        if not mask[i]:
            ch = text[i]
            if ch == "-" and i + 1 < n and text[i + 1] == ">":
                i += 2
                continue
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return n


def parse_type_header(text: str) -> TypeHeader | None:
    """The FIRST type declaration's head in `text`, or None if there is none.

    Attributes (`#[repr(C)]`) and doc comments are skipped, not parsed: they
    are not part of the head this compares — a `#[derive]` the model adds or
    drops is a layout choice its own compile gate judges.
    """
    mask = code_mask(text)
    n = len(text)
    i = 0
    vis = ""
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch == "#":                      # attribute: `#[…]` / `#![…]`
            j = i + 1
            while j < n and (mask[j] or text[j] in " \t\r\n!"):
                j += 1
            i = _skip_group(text, mask, j) if j < n and text[j] == "[" else i + 1
            continue
        if ch not in _IDENT_START:
            i += 1
            continue
        j = i
        while j < n and not mask[j] and text[j] in _IDENT_CHARS:
            j += 1
        word = text[i:j]
        if word == "pub":
            k = j
            while k < n and (mask[k] or text[k].isspace()):
                k += 1
            if k < n and text[k] == "(":   # pub(crate) / pub(in path)
                end = _skip_group(text, mask, k)
                vis = "pub" + "".join(text[k:end].split())
                i = end
            else:
                vis = "pub"
                i = j
            continue
        if word in _TYPE_KEYWORDS:
            k = j
            while k < n and (mask[k] or text[k].isspace()):
                k += 1
            s = k
            while k < n and not mask[k] and text[k] in _IDENT_CHARS:
                k += 1
            if k == s or text[s] not in _IDENT_START:
                return None
            name = text[s:k]
            while k < n and (mask[k] or text[k].isspace()):
                k += 1
            generics = ""
            if k < n and text[k] == "<":
                end = _skip_generics(text, mask, k)
                generics = "".join(text[k:end].split())
            return TypeHeader(vis=vis, kind=word, name=name, generics=generics)
        vis = ""                            # any other word resets the run
        i = j
    return None


def declaration_text(text: str) -> str:
    """The declaration, attributes skipped, body cut at ``{`` or ``;``.

    Whitespace is collapsed and comments and strings are not part of it. This
    is the identity of an item whose head :func:`parse_type_header` does not
    read (a fn, or a block with no type keyword): the signature is the item,
    and the body is not. A ``#[cfg]`` in front of the item is skipped with
    every other attribute — when the item exists is carried from the frozen
    definition, and it does not make a second item.
    """
    mask = code_mask(text)
    n = len(text)
    i = 0
    while i < n:
        if mask[i] or text[i].isspace():
            i += 1
            continue
        if text[i] == "#":
            j = i + 1
            while j < n and (mask[j] or text[j] in " \t\r\n!"):
                j += 1
            i = _skip_group(text, mask, j) if j < n and text[j] == "[" else i + 1
            continue
        break
    start = i
    depth = 0
    end = n
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch == "{":
            if depth == 0:
                end = i
                break
            depth += 1
        elif ch in "([":
            depth += 1
        elif ch in ")]}":
            if depth:
                depth -= 1
        elif ch == ";" and depth == 0:
            end = i
            break
        i += 1
    chars = [text[k] for k in range(start, end) if not mask[k]]
    return "".join("".join(chars).split())


def headers_match(a: TypeHeader | None, b: TypeHeader | None) -> bool:
    """True iff both heads parse and declare the same public contract."""
    return a is not None and b is not None and a == b


def field_block_span(text: str) -> tuple[int, int] | None:
    """(open, close) indices of the FIRST type declaration's field block —
    the `{…}` of a braced struct or the `(…)` of a tuple struct. None for a
    unit struct (`struct X;`) or when no declaration is found."""
    mask = code_mask(text)
    n = len(text)
    i = 0
    seen_kind = False
    in_where = False
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch in _IDENT_START:
            j = i
            while j < n and not mask[j] and text[j] in _IDENT_CHARS:
                j += 1
            word = text[i:j]
            if word in _TYPE_KEYWORDS:
                seen_kind = True
            elif word == "where":
                # `struct S<T> where T: Fn(u8) { … }` — parens inside a where
                # clause are bound syntax, not a tuple struct's field list.
                in_where = True
            i = j
            continue
        if in_where and ch in "([":
            i = _skip_group(text, mask, i)
            continue
        if ch == "#":
            j = i + 1
            while j < n and (mask[j] or text[j] in " \t\r\n!"):
                j += 1
            i = _skip_group(text, mask, j) if j < n and text[j] == "[" else i + 1
            continue
        if ch == "<":
            i = _skip_generics(text, mask, i)
            continue
        if not seen_kind:
            i += 1
            continue
        if ch == ";":
            return None
        if ch in "{(":
            return i, _skip_group(text, mask, i) - 1
        i += 1
    return None


def declares_pub_field(definition: str) -> bool:
    """True iff the declaration's field block carries a `pub` visibility.

    An authored replacement that marks a previously-private field `pub` WIDENS
    the crate's API surface — the one thing field authoring may never do. A
    struct body holds only fields, so any `pub` token in it is a field's."""
    span = field_block_span(definition)
    if span is None:
        return False
    open_idx, close_idx = span
    inner = definition[open_idx + 1:close_idx]
    mask = code_mask(inner)
    n = len(inner)
    i = 0
    while i < n:
        if mask[i] or inner[i] not in _IDENT_START:
            i += 1
            continue
        j = i
        while j < n and not mask[j] and inner[j] in _IDENT_CHARS:
            j += 1
        if inner[i:j] == "pub":
            return True
        i = j
    return False
