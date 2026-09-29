"""The setup of one harvested example: the statements its assertion needs.

A test item asserts several independent cases. `tests/test_ensure.rs::test_pat`
holds eight of them, each `let test = || Ok(ensure!(…)); assert_err(test, …);`.
The harvester used to store the WHOLE item as every example's `given`, capped
at 2000 chars: on the anyhow `stable` suite baked 2026-09-28 the 197
test-assertion examples came from 48 test items, all 29 multi-assertion items
stored one identical `given`, and 193.8 KB of the store's 194 KB of `given`
text was those copies. A prompt that quoted an example quoted the whole test
again, and a reader could not tell which of the eight `let test` bindings the
example's `when` (the bare identifier `test`) meant.

A prefix ("the statements before this assertion") does not fix it: it is a
growing copy of the same item, once per assertion. The setup is the def-use
slice instead — walking back from the assertion's statement, the statements
that BIND a name the example still needs (the most recent binding wins, so
the eighth `let test` shadows the other seven), the local items it names
(`fn`, `struct`, `impl` of a needed type), and the expression statements that
touch a needed binding (`v.push(1)` mutates what the assertion reads). An
earlier assertion is never setup: it observes, it does not prepare.

Pure text over the item source; the same function runs at harvest for every
language-shaped item the scanner can split. It names no crate and no test.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass

from pipeline_core.domain.rust_lex import code_mask


@dataclass(frozen=True, slots=True)
class _Structure:
    opaque: list[bool]
    close_of: dict[int, int]


def structural_scan(text: str) -> _Structure:
    """Which offsets are inside a literal or comment, and the bracket pairs
    (open offset -> close offset) outside them."""
    opaque = code_mask(text)
    stack: list[int] = []
    close_of: dict[int, int] = {}
    match = {")": "(", "]": "[", "}": "{"}
    for i, c in enumerate(text):
        if opaque[i]:
            continue
        if c in "([{":
            stack.append(i)
        elif c in ")]}":
            while stack and text[stack[-1]] != match[c]:
                stack.pop()
            if stack:
                close_of[stack.pop()] = i
    return _Structure(opaque, close_of)

_IDENT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
# Statements that end at their closing brace, not at a `;`.
_BLOCK_HEADS = ("if", "for", "while", "loop", "match", "fn", "impl", "struct",
                "enum", "trait", "mod", "unsafe", "macro_rules!")
# Words that are never a binding or a use of one.
_NOT_NAMES = frozenset({
    "let", "mut", "ref", "fn", "impl", "for", "struct", "enum", "trait", "mod",
    "if", "else", "while", "loop", "match", "return", "break", "continue", "in",
    "as", "move", "unsafe", "where", "use", "pub", "crate", "self", "Self",
    "super", "const", "static", "type", "true", "false", "dyn", "async", "await",
    "Some", "None", "Ok", "Err",
})
_ITEM_KEYWORDS = ("fn", "struct", "enum", "trait", "mod", "const", "static",
                  "type", "macro_rules!")


@dataclass(frozen=True, slots=True)
class Statement:
    """One top-level statement of a fn body: its text and char span."""

    text: str
    start: int
    end: int


def fn_body_span(item: str) -> tuple[int, int] | None:
    """`(open, close)` offsets of the first fn body brace pair in `item`: the
    `{` that follows `fn NAME(…)` and its match. None when there is none."""
    sc = structural_scan(item)
    i = 0
    while (at := item.find("fn ", i)) >= 0:
        i = at + 3
        if sc.opaque[at] or (at > 0 and item[at - 1] in _IDENT):
            continue
        paren = item.find("(", at)
        if paren < 0:
            return None
        close_paren = sc.close_of.get(paren)
        if close_paren is None:
            return None
        k = close_paren + 1
        while k < len(item):
            if not sc.opaque[k] and item[k] == "{":
                close = sc.close_of.get(k)
                return None if close is None else (k, close)
            if not sc.opaque[k] and item[k] == ";":
                return None
            k += 1
        return None
    return None


def _first_word(text: str) -> str:
    t = text.lstrip()
    while t.startswith("#"):
        # attributes attach to the statement after them
        end = t.find("]")
        if end < 0:
            return ""
        t = t[end + 1:].lstrip()
    if t.startswith("macro_rules!"):
        return "macro_rules!"
    j = 0
    while j < len(t) and t[j] in _IDENT:
        j += 1
    if j == 0 and t.startswith("{"):
        return "{"
    return t[:j]


def _strip_leading_trivia(text: str) -> str:
    """`text` without leading blank lines and `//` comment lines."""
    lines = text.split("\n")
    while lines and (not lines[0].strip() or lines[0].strip().startswith("//")):
        lines.pop(0)
    return "\n".join(lines)


def statements(item: str) -> list[Statement]:
    """Top-level statements of `item`'s fn body, in order. A statement ends at
    a `;` at body depth 0, or at the `}` that closes a block statement (`if`,
    `for`, `loop`, a local `fn`/`struct`/`impl`, a bare `{ … }`) when no
    `else` follows. The trailing tail expression is a statement too. Offsets
    index `item`."""
    span = fn_body_span(item)
    if span is None:
        return []
    open_at, close_at = span
    sc = structural_scan(item)
    out: list[Statement] = []
    start = open_at + 1
    depth = 0
    i = start
    while i < close_at:
        if sc.opaque[i]:
            i += 1
            continue
        c = item[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0 and c == "}":
                head = _first_word(_strip_leading_trivia(item[start:i]))
                if head in _BLOCK_HEADS or head == "{":
                    rest = item[i + 1:close_at].lstrip()
                    if not rest.startswith("else") and not rest.startswith(".") \
                            and not rest.startswith("?"):
                        _push(out, item, start, i + 1)
                        start = i + 1
        elif c == ";" and depth == 0:
            _push(out, item, start, i + 1)
            start = i + 1
        i += 1
    _push(out, item, start, close_at)
    return out


def _push(out: list[Statement], item: str, start: int, end: int) -> None:
    raw = item[start:end]
    body = _strip_leading_trivia(raw)
    if not body.strip() or body.strip() == ";":
        return
    lead = len(raw) - len(body)
    out.append(Statement(text=body.rstrip(), start=start + lead, end=end))


def _names(text: str) -> list[tuple[int, str]]:
    """`(offset, name)` for each identifier in `text` outside literals and
    comments that can name a binding: not a field or method (after `.`), not
    a later path segment (after `::`), not a keyword, not a lifetime."""
    sc = structural_scan(text)
    out: list[tuple[int, str]] = []
    n = len(text)
    i = 0
    while i < n:
        if sc.opaque[i] or text[i] not in _IDENT or text[i].isdigit():
            i += 1
            continue
        s = i
        while i < n and text[i] in _IDENT:
            i += 1
        word = text[s:i]
        before = text[s - 1] if s > 0 else ""
        if before in (".", "'") or text.startswith("::", s - 2) or before in _IDENT:
            continue
        if word in _NOT_NAMES:
            continue
        out.append((s, word))
    return out


def uses(text: str) -> frozenset[str]:
    return frozenset(w for _, w in _names(text))


def binds(stmt: str) -> frozenset[str]:
    """Names a statement introduces: a `let` pattern's identifiers (not its
    type annotation, not its initializer), or a local item's name (`fn`,
    `struct`, `enum`, `trait`, `const`, `static`, `type`, `mod`,
    `macro_rules!`), or the self type an `impl` adds behaviour to, or the
    last segments a `use` brings in."""
    text = _strip_attrs(stmt)
    head = _first_word(text)
    if head == "let":
        pat_start = text.find("let") + 3
        sc = structural_scan(text)
        depth = 0
        end = len(text)
        for k in range(pat_start, len(text)):
            if sc.opaque[k]:
                continue
            ch = text[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif depth == 0 and (ch == "=" and not text.startswith("==", k)
                                 and not text.startswith("=>", k)) or depth == 0 and ch == ":" and not text.startswith("::", k) \
                    and not text.startswith("::", k - 1):
                end = k
                break
        pattern = text[pat_start:end]
        return frozenset(w for _, w in _names(pattern)
                         if not w[:1].isupper())
    if head == "unsafe":
        rest = text[len("unsafe"):].lstrip()
        if _first_word(rest) in ("fn", "trait", "impl"):
            return binds(rest)
        return frozenset()          # an `unsafe { … }` block is an expression
    if head in _ITEM_KEYWORDS:
        after = text[text.find(head) + len(head):]
        names = _names(after)
        return frozenset({names[0][1]}) if names else frozenset()
    if head == "impl":
        header = text.split("{", 1)[0]
        target = header.rsplit(" for ", 1)[-1] if " for " in header else header[4:]
        type_names = [w for _, w in _names(target) if w[:1].isupper()]
        return frozenset(type_names[:1])
    if head == "use":
        body = text[3:].rstrip(";")
        out: set[str] = set()
        for _, w in _names(body.replace("::", " ").replace("{", " ").replace("}", " ")):
            out.add(w)
        return frozenset(out)
    return frozenset()


def _strip_attrs(stmt: str) -> str:
    t = stmt.lstrip()
    while t.startswith("#"):
        end = t.find("]")
        if end < 0:
            return t
        t = t[end + 1:].lstrip()
    return t


def is_observation(stmt: str, observers: frozenset[str]) -> bool:
    """True when the statement is an assertion (an `assert…!` macro or a call
    to a name in `observers`, the file's asserting helpers): it observes a
    case, it does not set one up."""
    text = _strip_attrs(stmt)
    word = _first_word(text)
    if word.startswith("assert"):
        return True
    return word in observers and text[len(word):].lstrip().startswith("(")


def setup_slice(item: str, pos: int, *, observers: frozenset[str] = frozenset()) -> str:
    """The setup of the assertion found at char offset `pos` of `item`: the
    def-use slice of the statements before the one containing `pos`, joined
    in source order and dedented. "" when the assertion needs nothing bound in
    the item (or `pos` is outside a body).

    `observers` are the file's asserting helper names; a statement that calls
    one is an earlier observation and is not setup — UNLESS it names a needed
    binding declared `mut`: `assert_eq!("3", chain.next()…)` advances the
    iterator the next assertion reads, so the case after it is not reproduced
    without it."""
    stmts = statements(item)
    idx = next((k for k, s in enumerate(stmts) if s.start <= pos < s.end), -1)
    if idx < 0:
        return ""
    bound_at = [binds(s.text) for s in stmts]
    mut_at = [mut_binds(s.text) for s in stmts]

    def is_mut(name: str, before: int) -> bool:
        for k in range(before - 1, -1, -1):
            if name in bound_at[k]:
                return name in mut_at[k]
        return False

    needed = set(uses(stmts[idx].text))
    keep: list[int] = []
    for j in range(idx - 1, -1, -1):
        text = stmts[j].text
        used = uses(text)
        if is_observation(text, observers):
            if any(is_mut(n, j) for n in used & needed):
                keep.append(j)
                needed |= used
            continue
        bound = bound_at[j]
        if bound & needed:
            keep.append(j)
            needed -= bound
            if _first_word(_strip_attrs(text)) == "let":
                needed |= uses(_let_initializer(text))
            else:
                needed |= used - bound
            continue
        if not bound and used & needed:
            keep.append(j)
            needed |= used
    keep.sort()
    return _dedent("\n".join(stmts[j].text for j in keep))


def mut_binds(stmt: str) -> frozenset[str]:
    """The names a `let` pattern binds with `mut` (`let (mut err, dropped)`
    -> {err})."""
    text = _strip_attrs(stmt)
    if _first_word(text) != "let":
        return frozenset()
    names = binds(stmt)
    out: set[str] = set()
    for off, word in _names_with_keywords(text):
        if word in names and text[:off].rstrip().endswith("mut"):
            out.add(word)
    return frozenset(out)


def _names_with_keywords(text: str) -> list[tuple[int, str]]:
    return [(o, w) for o, w in _names(text)]


def _let_initializer(stmt: str) -> str:
    """The text after a `let` pattern's `=` (the whole statement when none),
    plus the type annotation: what the binding READS, not what it binds."""
    text = _strip_attrs(stmt)
    sc = structural_scan(text)
    depth = 0
    for k in range(len(text)):
        if sc.opaque[k]:
            continue
        ch = text[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "=" and not text.startswith("==", k) \
                and not text.startswith("=>", k) and k > 0 and text[k - 1] not in "!<>=":
            return text[k + 1:]
    return text


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


# --- file-level helpers ---------------------------------------------------

_ITEM_HEADS = ("fn", "struct", "enum", "impl", "const", "static", "trait", "type",
               "macro_rules!", "unsafe", "pub", "mod")


@dataclass(frozen=True, slots=True)
class FileItem:
    """One top-level item of a test file: the name it binds (for an `impl`,
    the self type it adds behaviour to), its text (attributes included) and
    whether it is a test fn."""

    name: str
    text: str
    is_test: bool = False
    is_impl: bool = False


def top_level_items(src: str) -> list[FileItem]:
    """The file's top-level items in order. An item runs from its first
    attribute line to the `;` or closing `}` that ends it at depth 0. `use`
    lines, `mod x;` declarations and inner attributes (`#![…]`) are not items
    a helper resolves to and are skipped."""
    sc = structural_scan(src)
    out: list[FileItem] = []
    n = len(src)
    i = 0
    start = -1
    depth = 0
    while i < n:
        if sc.opaque[i]:
            i += 1
            continue
        c = src[i]
        if depth == 0 and start < 0 and not c.isspace():
            start = i
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0 and c == "}":
                text = src[start:i + 1]
                head = _first_word(_strip_attrs(text))
                if head != "" and not _strip_attrs(text).startswith("#"):
                    rest = src[i + 1:].lstrip()
                    if not rest.startswith(";"):
                        _add_item(out, text)
                        start = -1
        elif c == ";" and depth == 0:
            _add_item(out, src[start:i + 1])
            start = -1
        i += 1
    return out


def _add_item(out: list[FileItem], text: str) -> None:
    body = _strip_attrs(text)
    if not body or body.startswith("use ") or body.startswith("pub use ") \
            or body.startswith("!") or text.lstrip().startswith("#!"):
        return
    lead = body
    while True:
        w = _first_word(lead)
        if w.startswith("pub"):
            lead = lead[len(w):].lstrip()
            if lead.startswith("("):
                lead = lead[lead.find(")") + 1:].lstrip()
            continue
        if w in ("unsafe", "extern", "async"):
            lead = lead[len(w):].lstrip()
            if lead.startswith('"'):
                lead = lead[lead.find('"', 1) + 1:].lstrip()
            continue
        break
    head = _first_word(lead)
    if not head:
        return
    body = lead
    if head == "mod" and body.rstrip().endswith(";"):
        return
    is_test = "#[test]" in text or "#[tokio::test]" in text
    if head == "impl":
        header = body.split("{", 1)[0]
        params = header[4:len(header) - len(_skip_generics(header[4:]))]
        own = {w for _, w in _names(params)}
        tail = header.rsplit(" for ", 1)[-1] if " for " in header else _skip_generics(header[4:])
        names = [w for _, w in _names(tail) if w[:1].isupper()]
        # A blanket impl over its own parameter (`impl<T> Trait for T`) adds
        # behaviour to no nameable type; `T` elsewhere is someone else's
        # parameter.
        for name in [n for n in names[:1] if n not in own]:
            out.append(FileItem(name=name, text=text.strip(), is_impl=True))
        return
    after_head = body[len(head):].lstrip()
    if head not in _ITEM_HEADS and after_head.startswith("!"):
        # A declaring macro invoked at item level (`declare!(Name);`): it
        # binds the type names it is handed.
        declared = [w for _, w in _names(after_head[1:]) if w[:1].isupper()]
        for name in declared:
            out.append(FileItem(name=name, text=text.strip()))
        return
    if head not in _ITEM_HEADS and not head.startswith("macro_rules!"):
        return
    rest = body[body.find(head) + len(head):]
    found = _names(rest)
    if not found:
        return
    out.append(FileItem(name=found[0][1], text=text.strip(), is_test=is_test))


def _skip_generics(text: str) -> str:
    """`text` past a leading `<…>` generics list."""
    t = text.lstrip()
    if not t.startswith("<"):
        return t
    depth = 0
    for k, ch in enumerate(t):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return t[k + 1:]
    return t


# Helper key suffix of a test file's `use` block (`tests/x.rs::use`).
IMPORTS_KEY_SUFFIX = "::use"


def use_block(src: str) -> str:
    """The file's top-level `use` declarations, joined in order: what names
    the examples of that file see, and from which crate."""
    sc = structural_scan(src)
    out: list[str] = []
    depth = 0
    i = 0
    n = len(src)
    while i < n:
        if sc.opaque[i]:
            i += 1
            continue
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and (src.startswith("use ", i) or src.startswith("pub use ", i)) \
                and (i == 0 or src[i - 1] in "\n\t "):
            end = i
            d = 0
            while end < n:
                ch = src[end]
                if not sc.opaque[end]:
                    if ch in "([{":
                        d += 1
                    elif ch in ")]}":
                        d -= 1
                    elif ch == ";" and d == 0:
                        break
                end += 1
            out.append(src[i:end + 1].strip())
            i = end + 1
            continue
        i += 1
    return "\n".join(out)


def mod_declarations(src: str) -> list[str]:
    """Names of the `mod NAME;` declarations at the top level of `src` (the
    sibling modules a test file pulls in, `tests/NAME/mod.rs` or
    `tests/NAME.rs`)."""
    out: list[str] = []
    for line in src.splitlines():
        t = line.strip()
        for prefix in ("mod ", "pub mod "):
            if t.startswith(prefix) and t.endswith(";"):
                name = t[len(prefix):-1].strip()
                if name and all(ch in _IDENT for ch in name):
                    out.append(name)
    return out


def item_index(files: list[tuple[str, str]]) -> dict[str, list[tuple[str, FileItem]]]:
    """name -> `(key, item)` for every top-level item of `files` (`(path,
    text)` pairs). The key is `path::name`, and `path::name::implN` for the
    N-th `impl` of that name in that file, so a type's struct and each of its
    impls stay distinct entries under one name."""
    index: dict[str, list[tuple[str, FileItem]]] = {}
    for path, text in files:
        impl_n: dict[str, int] = {}
        for item in top_level_items(text):
            if item.is_impl:
                impl_n[item.name] = impl_n.get(item.name, 0) + 1
                key = f"{path}::{item.name}::impl{impl_n[item.name]}"
            else:
                key = f"{path}::{item.name}"
            index.setdefault(item.name, []).append((key, item))
    return index


def helper_closure(
    code: str,
    index: dict[str, list[tuple[str, FileItem]]],
    *,
    extra: tuple[str, ...] = (),
) -> tuple[tuple[str, str], ...]:
    """`(key, text)` of the file-level items `code` names, transitively, in
    first-reached order. A type's struct and its impls answer to one name.
    Test fns are never helpers. `extra` names are needed as well (the
    asserting helper a test routes its literal through)."""
    needed: list[str] = [*extra, *sorted(uses(code))]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    keys: set[str] = set()
    while needed:
        name = needed.pop(0)
        if name in seen:
            continue
        seen.add(name)
        for key, item in index.get(name, ()):
            if item.is_test or key in keys:
                continue
            keys.add(key)
            out.append((key, item.text))
            needed.extend(sorted(uses(item.text) - seen))
    return tuple(out)
