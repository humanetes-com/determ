"""What a generated test block reads that is NOT `pub` — pure domain.

Generated tests that pin public behaviour live in `tests/` as integration
tests (`use crate::*;`), so private fns, fields and types of the file are
not in scope and a test that names one will not compile. Older trees still
carry inline `#[cfg(test)]` siblings with `use super::*;`; this module is
the check that names what a block reads that the file does not export, so
those leftovers are refused at admission rather than freezing the layout.

The instance: `src/error.gen_tests_3.rs:29` reads `err.inner.ptr.as_ptr()`
on an inline sibling and made an authored layout that satisfied every
public pin uncompilable (anyhow, 2026-08). The test contract is PUBLIC API
ONLY.

`names_private_items` reports three classes, each as a readable name:

- `fn <name>` — a fn the file declares without a `pub` visibility (any `pub`,
  `pub(crate)` included, counts as public: the layout stage keeps every
  signature, the contract is about the crate's INTENDED surface) that the block
  calls. Trait-impl methods (`impl Display for X { fn fmt … }`) and trait
  declarations are the trait's API and never count; fns inside `#[cfg(test)]`
  modules are the file's own tests and never count; a name declared both `pub`
  and private in the file is ambiguous and never counts.
- `<Type>.<field>[.<field>…]` — a private field of a struct the file declares,
  read through a local whose type is stated or constructed
  (`oracle_field_paths.read_field_paths`), written in a struct literal
  (`Counter { value: 3 }`), or — when the field name is private on every struct
  of the file that declares it — read as `.field` on any receiver. A tuple
  struct with a private positional field is reported as `<Type>(..)` when the
  block constructs it.
- `struct <Name>` / `enum <Name>` / `union <Name>` / `type <Name>` — a type the
  file declares without `pub` that the block names as a bare path start.

Everything here is a token scan over `rust_lex.code_mask` — no regex.
"""
from __future__ import annotations

from dataclasses import dataclass

from .oracle_field_paths import read_field_paths
from .rust_lex import code_mask
from .rust_scan import field_reads

_IDENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_TYPE_KEYWORDS = frozenset({"struct", "enum", "union"})
_ALIAS_KEYWORDS = frozenset({"type", "const", "static"})
_FN_QUALIFIERS = frozenset({"pub", "unsafe", "const", "async", "extern", "default"})


@dataclass(frozen=True, slots=True)
class DeclaredItem:
    """One item a file declares, with its visibility and span (offsets into
    the source; `line_start`/`line_end` are 1-based, inclusive)."""

    kind: str                       # fn | struct | enum | union | type | const | static | trait | mod
    name: str
    is_pub: bool
    start: int
    end: int
    line_start: int
    line_end: int
    owner: str = ""                 # for a method: the inherent impl's type
    fields: tuple[tuple[str, bool], ...] = ()   # named fields (name, is_pub); structs
    tuple_fields: tuple[bool, ...] = ()         # positional fields' visibility; tuple structs


def _is_ident(c: str) -> bool:
    return c.isalnum() or c == "_"


def _match_close(text: str, mask: list[bool], open_at: int, open_: str, close: str) -> int | None:
    depth = 0
    for i in range(open_at, len(text)):
        if mask[i]:
            continue
        if text[i] == open_:
            depth += 1
        elif text[i] == close:
            depth -= 1
            if depth == 0:
                return i
    return None


def _line_of(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


class _Scanner:
    """A one-pass item scanner. Module and inherent-impl bodies are entered;
    fn bodies, trait impls, trait declarations, `#[cfg(test)]` modules, macro
    definitions and `extern` blocks are stepped over whole."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.mask = code_mask(text)
        self.n = len(text)
        self.items: list[DeclaredItem] = []

    # -- token helpers ----------------------------------------------------
    def _skip_ws(self, i: int) -> int:
        while i < self.n and (self.mask[i] or self.text[i].isspace()):
            i += 1
        return i

    def _ident_at(self, i: int) -> str:
        j = i
        while j < self.n and not self.mask[j] and _is_ident(self.text[j]):
            j += 1
        return self.text[i:j]

    def _skip_attr(self, i: int) -> int:
        """Past a `#[…]` / `#![…]` opening at `i`."""
        j = i + 1
        if j < self.n and self.text[j] == "!":
            j += 1
        if j < self.n and self.text[j] == "[":
            close = _match_close(self.text, self.mask, j, "[", "]")
            return close + 1 if close is not None else self.n
        return i + 1

    def _skip_generics(self, i: int) -> int:
        i = self._skip_ws(i)
        if i < self.n and self.text[i] == "<":
            close = _match_close(self.text, self.mask, i, "<", ">")
            return close + 1 if close is not None else self.n
        return i

    def _item_end(self, i: int) -> int:
        """Past the `;` or the balanced `{…}` that ends the item whose header
        continues at `i`; `(`/`[` nesting is tracked so `struct A([u8; 4]);`
        ends at its own semicolon."""
        nest = 0
        while i < self.n:
            if self.mask[i]:
                i += 1
                continue
            c = self.text[i]
            if c in "([":
                nest += 1
            elif c in ")]":
                nest -= 1
            elif nest == 0 and c == ";":
                return i + 1
            elif nest == 0 and c == "{":
                close = _match_close(self.text, self.mask, i, "{", "}")
                return close + 1 if close is not None else self.n
            i += 1
        return self.n

    # -- items ------------------------------------------------------------
    def scan(self, start: int, end: int, owner: str) -> None:
        i = start
        pending: list[str] = []          # qualifier tokens since the last boundary
        pending_start = -1               # offset of the first qualifier, -1 = none yet
        test_mod_pending = False
        while i < end:
            if self.mask[i]:
                i += 1
                continue
            c = self.text[i]
            if c == "#":
                attr_end = self._skip_attr(i)
                attr = self.text[i:attr_end]
                if "cfg" in attr and "test" in attr:
                    test_mod_pending = True
                i = attr_end
                continue
            if c.isspace():
                i += 1
                continue
            if c == "{":
                close = _match_close(self.text, self.mask, i, "{", "}")
                i = close + 1 if close is not None else end
                pending, pending_start, test_mod_pending = [], -1, False
                continue
            if c in ";}":
                i += 1
                pending, pending_start, test_mod_pending = [], -1, False
                continue
            if c in _IDENT_START:
                word = self._ident_at(i)
                after = i + len(word)
                if word == "pub":
                    if pending_start < 0:
                        pending_start = i
                    j = self._skip_ws(after)
                    if j < self.n and self.text[j] == "(":
                        close = _match_close(self.text, self.mask, j, "(", ")")
                        after = close + 1 if close is not None else after
                    pending.append("pub")
                    i = after
                    continue
                if word in _FN_QUALIFIERS:
                    if pending_start < 0:
                        pending_start = i
                    pending.append(word)
                    i = after
                    if word == "extern":
                        i = self._skip_ws(i)
                        if i < self.n and self.text[i] == '"':
                            j = i + 1
                            while j < self.n and self.mask[j]:
                                j += 1
                            i = j
                    continue
                is_pub = "pub" in pending
                item_at = pending_start if pending_start >= 0 else i
                if word == "fn":
                    i = self._fn(after, owner, is_pub, item_at)
                elif word in _TYPE_KEYWORDS:
                    i = self._type(word, after, is_pub, item_at)
                elif word in _ALIAS_KEYWORDS:
                    i = self._alias(word, after, is_pub, item_at)
                elif word == "impl":
                    i = self._impl(after)
                elif word == "trait":
                    i = self._record_and_skip("trait", after, is_pub, item_at)
                elif word == "mod":
                    i = self._mod(after, is_pub, item_at, test_mod_pending)
                elif word in ("macro_rules", "use"):
                    i = self._item_end(after)
                else:
                    i = after
                    continue
                pending, pending_start, test_mod_pending = [], -1, False
                continue
            i += 1

    def _name_after(self, i: int) -> tuple[str, int]:
        i = self._skip_ws(i)
        name = self._ident_at(i)
        return name, i + len(name)

    def _record(self, kind: str, name: str, is_pub: bool, start: int, end: int,
                owner: str = "", fields: tuple[tuple[str, bool], ...] = (),
                tuple_fields: tuple[bool, ...] = ()) -> None:
        self.items.append(DeclaredItem(
            kind, name, is_pub, start, end, _line_of(self.text, start),
            _line_of(self.text, max(start, end - 1)), owner, fields, tuple_fields))

    def _fn(self, after: int, owner: str, is_pub: bool, start: int) -> int:
        name, p = self._name_after(after)
        end = self._item_end(p)
        if name:
            self._record("fn", name, is_pub, start, end, owner=owner)
        return end

    def _record_and_skip(self, kind: str, after: int, is_pub: bool, start: int) -> int:
        name, p = self._name_after(after)
        end = self._item_end(p)
        if name:
            self._record(kind, name, is_pub, start, end)
        return end

    def _alias(self, kind: str, after: int, is_pub: bool, start: int) -> int:
        name, p = self._name_after(after)
        end = self._item_end(p)
        if name:
            self._record(kind, name, is_pub, start, end)
        return end

    def _type(self, kind: str, after: int, is_pub: bool, start: int) -> int:
        name, p = self._name_after(after)
        p = self._skip_generics(p)
        end = self._item_end(p)
        fields: tuple[tuple[str, bool], ...] = ()
        tuple_fields: tuple[bool, ...] = ()
        if name and kind == "struct":
            q = self._skip_ws(p)
            if q < self.n and self.text[q] == "(":
                close = _match_close(self.text, self.mask, q, "(", ")")
                if close is not None:
                    tuple_fields = self._tuple_visibility(q + 1, close)
            elif q < self.n and self.text[q] == "{":
                close = _match_close(self.text, self.mask, q, "{", "}")
                if close is not None:
                    fields = self._named_fields(q + 1, close)
        if name:
            self._record(kind, name, is_pub, start, end, fields=fields, tuple_fields=tuple_fields)
        return end

    def _tuple_visibility(self, start: int, end: int) -> tuple[bool, ...]:
        out: list[bool] = []
        i = start
        nest = 0
        field_start = start
        while i <= end:
            if i == end or (not self.mask[i] and self.text[i] == "," and nest == 0):
                seg = self.text[field_start:i].strip()
                if seg:
                    out.append(seg.startswith("pub"))
                field_start = i + 1
            elif not self.mask[i] and self.text[i] in "([<":
                nest += 1
            elif not self.mask[i] and self.text[i] in ")]>":
                nest -= 1
            i += 1
        return tuple(out)

    def _named_fields(self, start: int, end: int) -> tuple[tuple[str, bool], ...]:
        out: list[tuple[str, bool]] = []
        i = start
        nest = 0
        seg_start = start
        while i <= end:
            if i == end or (not self.mask[i] and self.text[i] == "," and nest == 0):
                seg = self.text[seg_start:i]
                name, is_pub = self._field_of(seg)
                if name:
                    out.append((name, is_pub))
                seg_start = i + 1
            elif not self.mask[i] and self.text[i] in "([<{":
                nest += 1
            elif not self.mask[i] and self.text[i] in ")]>}":
                nest -= 1
            i += 1
        return tuple(out)

    @staticmethod
    def _field_of(seg: str) -> tuple[str, bool]:
        """`(name, is_pub)` of one `[#[attr]] [pub[(…)]] name: Type` segment."""
        s = seg
        while True:
            s = s.lstrip()
            if s.startswith("#["):
                close = s.find("]")
                s = s[close + 1:] if close != -1 else ""
                continue
            if s.startswith("//"):
                nl = s.find("\n")
                s = s[nl + 1:] if nl != -1 else ""
                continue
            break
        is_pub = False
        if s.startswith("pub"):
            rest = s[3:]
            if not rest or not _is_ident(rest[0]):
                is_pub = True
                s = rest.lstrip()
                if s.startswith("("):
                    close = s.find(")")
                    s = s[close + 1:].lstrip() if close != -1 else ""
        j = 0
        while j < len(s) and _is_ident(s[j]):
            j += 1
        name = s[:j]
        rest = s[j:].lstrip()
        if not name or not rest.startswith(":") or rest.startswith("::"):
            return "", False
        return name, is_pub

    def _impl(self, after: int) -> int:
        """An inherent impl's body is scanned as methods of its type; a trait
        impl (`impl X for Y`) is stepped over — its methods are the trait's."""
        i = self._skip_generics(after)
        header_end = i
        depth = 0
        while header_end < self.n:
            if self.mask[header_end]:
                header_end += 1
                continue
            ch = self.text[header_end]
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif ch == "{" and depth == 0:
                break
            elif ch == ";" and depth == 0:
                return header_end + 1
            header_end += 1
        if header_end >= self.n:
            return self.n
        header = self.text[i:header_end]
        close = _match_close(self.text, self.mask, header_end, "{", "}")
        if close is None:
            return self.n
        words = self._words(header)
        if "for" in words:
            return close + 1
        owner = ""
        for w in words:
            if w[0].isupper():
                owner = w
                break
        self.scan(header_end + 1, close, owner)
        return close + 1

    @staticmethod
    def _words(text: str) -> list[str]:
        out: list[str] = []
        i = 0
        while i < len(text):
            if text[i] in _IDENT_START:
                j = i
                while j < len(text) and _is_ident(text[j]):
                    j += 1
                out.append(text[i:j])
                i = j
            else:
                i += 1
        return out

    def _mod(self, after: int, is_pub: bool, start: int, is_test: bool) -> int:
        name, p = self._name_after(after)
        q = self._skip_ws(p)
        if q < self.n and self.text[q] == ";":
            return q + 1
        if q >= self.n or self.text[q] != "{":
            return self._item_end(p)
        close = _match_close(self.text, self.mask, q, "{", "}")
        if close is None:
            return self.n
        if name:
            self._record("mod", name, is_pub, start, close + 1)
        if not is_test:
            self.scan(q + 1, close, "")
        return close + 1


def declared_items(file_source: str) -> tuple[DeclaredItem, ...]:
    """Every item `file_source` declares outside `#[cfg(test)]` modules, trait
    declarations and trait impls, in file order."""
    s = _Scanner(file_source)
    s.scan(0, s.n, "")
    return tuple(s.items)


def fn_line_spans(file_source: str) -> dict[str, tuple[int, int]]:
    """fn name → (first line, last line) of its declaration + body. A name
    declared more than once (two impls) spans from its first start to its last
    end."""
    out: dict[str, tuple[int, int]] = {}
    for it in declared_items(file_source):
        if it.kind != "fn":
            continue
        prev = out.get(it.name)
        out[it.name] = ((min(prev[0], it.line_start), max(prev[1], it.line_end))
                        if prev else (it.line_start, it.line_end))
    return out


def public_item_names(items: tuple[DeclaredItem, ...]) -> frozenset[str]:
    return frozenset(it.name for it in items if it.is_pub and it.kind != "mod")


def _word_hits(code: str, mask: list[bool], word: str) -> list[int]:
    """Offsets where `word` occurs as a whole identifier in unmasked code."""
    out: list[int] = []
    at = 0
    while (at := code.find(word, at)) >= 0:
        end = at + len(word)
        before = code[at - 1] if at else " "
        after = code[end] if end < len(code) else " "
        if not mask[at] and not _is_ident(before) and not _is_ident(after):
            out.append(at)
        at = end
    return out


def _next_nonspace(code: str, i: int) -> str:
    while i < len(code) and code[i].isspace():
        i += 1
    return code[i] if i < len(code) else ""


def _literal_fields(code: str, mask: list[bool], open_at: int) -> list[str]:
    """Field names written in the struct literal whose `{` is at `open_at`."""
    close = _match_close(code, mask, open_at, "{", "}")
    if close is None:
        return []
    out: list[str] = []
    depth = 0
    i = open_at + 1
    seg_start = i
    while i <= close:
        if i == close or (not mask[i] and code[i] == "," and depth == 0):
            seg = code[seg_start:i].strip()
            j = 0
            while j < len(seg) and _is_ident(seg[j]):
                j += 1
            name = seg[:j]
            rest = seg[j:].lstrip()
            if name and (not rest or rest.startswith(":") and not rest.startswith("::")):
                out.append(name)
            seg_start = i + 1
        elif not mask[i] and code[i] in "([{":
            depth += 1
        elif not mask[i] and code[i] in ")]}":
            depth -= 1
        i += 1
    return out


def names_private_items(block_text: str, file_source: str) -> tuple[str, ...]:
    """What `block_text` reads of `file_source` that is not `pub` — see the
    module docstring for the three classes and their spellings. Sorted, unique;
    empty when the block stays on the public API."""
    items = declared_items(file_source)
    if not items or not block_text.strip():
        return ()
    code = block_text
    mask = code_mask(code)
    found: set[str] = set()

    # -- private fns the block calls ------------------------------------
    pub_fn_names = {it.name for it in items if it.kind == "fn" and it.is_pub}
    private_fns = {it.name for it in items if it.kind == "fn" and not it.is_pub} - pub_fn_names
    for name in private_fns:
        for at in _word_hits(code, mask, name):
            if _next_nonspace(code, at + len(name)) == "(":
                found.add(f"fn {name}")
                break

    # -- private types the block names -------------------------------------
    pub_type_names = {it.name for it in items if it.kind in _TYPE_KEYWORDS | {"type"} and it.is_pub}
    for it in items:
        if it.kind not in _TYPE_KEYWORDS | {"type"} or it.is_pub or it.name in pub_type_names:
            continue
        for at in _word_hits(code, mask, it.name):
            before = code[:at].rstrip()
            if before.endswith("::") or before.endswith("."):
                continue
            found.add(f"{it.kind} {it.name}")
            break

    # -- private fields --------------------------------------------------
    structs = [it for it in items if it.kind == "struct"]
    private_fields = {it.name: {f for f, p in it.fields if not p} for it in structs}
    pub_field_names = {f for it in structs for f, p in it.fields if p}
    with_private = {name for name, fs in private_fields.items() if fs}
    # (a) typed reads: a local whose type is stated or constructed.
    for path in read_field_paths("", code, types=with_private):
        if path.fields and path.fields[0] in private_fields[path.root_type]:
            found.add(path.expr)
    # (b) struct-literal writes and tuple constructions.
    for it in structs:
        if not it.fields and not it.tuple_fields:
            continue
        for at in _word_hits(code, mask, it.name):
            end = at + len(it.name)
            j = end
            while j < len(code) and code[j].isspace():
                j += 1
            if j >= len(code):
                continue
            if code[j] == "{" and it.fields:
                for f in _literal_fields(code, mask, j):
                    if f in private_fields[it.name]:
                        found.add(f"{it.name}.{f}")
            elif code[j] == "(" and it.tuple_fields and not all(it.tuple_fields):
                found.add(f"{it.name}(..)")
    # (c) untyped reads, only for names private on EVERY declaring struct.
    by_field: dict[str, set[str]] = {}
    for it in structs:
        for f, _p in it.fields:
            by_field.setdefault(f, set()).add(it.name)
    unambiguous = {f for f, owners in by_field.items()
                   if f not in pub_field_names and all(f in private_fields[o] for o in owners)}
    for f, count in field_reads(code, unambiguous).items():
        if count <= 0:
            continue
        owners = sorted(by_field[f])
        owner = owners[0] if len(owners) == 1 else "/".join(owners)
        if not any(f in x.split(".")[1:] for x in found):
            found.add(f"{owner}.{f}")
    return tuple(sorted(found))
