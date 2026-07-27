"""Regex-free scanners over rendered Rust-ish text (skeletons, merged source).

The pipeline's structured parser for *original* source is the syn-based extractor
(producing :mod:`pipeline_core.domain.models` records). These helpers operate on
ALREADY-rendered skeleton / marker text where re-running syn is neither available
nor reliable (signatures-only, elided bodies, FILE/TESTFILE framing). They use
deterministic token / substring scanning — never regex — mirroring the existing
``text.find``-based ``#[cfg(test)]`` block finder (Hard Rule: no regex).
"""

from __future__ import annotations

import string
from collections.abc import Sequence

from .rust_idents import declares_fn
from .rust_lex import code_mask

_ITEM_KEYWORDS = frozenset({"fn", "struct", "enum", "trait", "type", "const"})
_IDENT_START = frozenset(string.ascii_letters + "_")
_IDENT_CHARS = _IDENT_START | frozenset(string.digits)


def contains_word(text: str, word: str) -> bool:
    """True when `word` occurs in `text` as a whole identifier token (regex `\\bword\\b`):
    neither neighbour is an identifier char. (`word` is an identifier, so its own ends
    are word chars — only the neighbours decide the boundary.)"""
    start = 0
    while (k := text.find(word, start)) >= 0:
        before = text[k - 1] if k > 0 else ""
        after = text[k + len(word)] if k + len(word) < len(text) else ""
        if before not in _IDENT_CHARS and after not in _IDENT_CHARS:
            return True
        start = k + 1
    return False


def count_word(text: str, word: str) -> int:
    """Count of whole-word occurrences of identifier `word` — byte-equivalent to
    ``len(re.findall(r'\\bword\\b', text))`` (non-overlapping; resumes past each hit)."""
    cnt = 0
    start = 0
    w = len(word)
    n = len(text)
    while (k := text.find(word, start)) >= 0:
        before = text[k - 1] if k > 0 else ""
        after = text[k + w] if k + w < n else ""
        if before not in _IDENT_CHARS and after not in _IDENT_CHARS:
            cnt += 1
            start = k + w
        else:
            start = k + 1
    return cnt


def count_macro_calls(text: str, name: str) -> int:
    """Count of ``name`` used as a macro invocation — byte-equivalent to
    ``len(re.findall(r'\\bname\\s*!', text))``: a whole-word `name` followed across
    optional whitespace by ``!`` (the ``\\s*!`` enforces the trailing boundary)."""
    cnt = 0
    start = 0
    w = len(name)
    n = len(text)
    while (k := text.find(name, start)) >= 0:
        before = text[k - 1] if k > 0 else ""
        if before not in _IDENT_CHARS:  # \b before name (name[0] is an ident char)
            q = k + w
            while q < n and text[q].isspace():
                q += 1
            if q < n and text[q] == "!":
                cnt += 1
                start = q + 1
                continue
        start = k + 1
    return cnt


def use_imports_symbol(block: str, prefixes: Sequence[str], symbol: str) -> bool:
    """True when `block` already has a `use <prefix>…<symbol>…` import — the
    presence check the gen-prelude injector uses to avoid double-importing
    (regex-free, R7; replaces `use (?:std|alloc)::[^;]*\\bSymbol\\b`-style patterns).
    Scans each `use ` occurrence, requires the text right after `use ` to start
    with one of `prefixes`, and `symbol` to appear as a whole identifier token
    within that statement (up to its terminating `;`)."""
    needle = "use "
    pos = 0
    while (u := block.find(needle, pos)) >= 0:
        semi = block.find(";", u)
        stmt = block[u: semi if semi >= 0 else len(block)]
        body = stmt[len(needle):]
        if any(body.startswith(p) for p in prefixes) and contains_word(stmt, symbol):
            return True
        pos = u + len(needle)
    return False


def find_mod_opener(text: str, *, gen_only: bool) -> tuple[int, int] | None:
    """Locate the FIRST `[pub ]mod <name> {` opener (regex-free, R7; mirrors
    `(?:pub\\s+)?mod\\s+(?:gen_)?\\w+\\s*\\{`). Returns `(start, brace)` where
    `start` is the insert-before index (the `pub` if present, else `mod`) and
    `brace` is the index of `{`; None when absent. `gen_only` requires the module
    name to start with `gen_` (and have ≥1 char after). Arbitrary inter-token
    whitespace is allowed."""
    i = 0
    n = len(text)
    while (m := text.find("mod", i)) >= 0:
        i = m + 3
        after = m + 3
        if m > 0 and text[m - 1] in _IDENT_CHARS:      # tail of a longer ident
            continue
        if after >= n or not text[after].isspace():     # need \s+ after `mod`
            continue
        j = after
        while j < n and text[j].isspace():
            j += 1
        k = j
        while k < n and text[k] in _IDENT_CHARS:
            k += 1
        name = text[j:k]
        if not name or (gen_only and not (name.startswith("gen_") and len(name) > 4)):
            continue
        b = k
        while b < n and text[b].isspace():
            b += 1
        if b >= n or text[b] != "{":
            continue
        start = m
        head = text[:m].rstrip()                        # `pub` + \s+ before `mod`?
        if len(head) < m and head.endswith("pub") and (
                len(head) == 3 or head[-4] not in _IDENT_CHARS):
            start = len(head) - 3
        return start, b
    return None


def after_brace_newline(text: str, brace_idx: int) -> int | None:
    """Index just past the last newline in the whitespace run following the `{`
    at `brace_idx` (mirrors greedy `\\{\\s*\\n`), or None when no newline follows
    through whitespace only."""
    n = len(text)
    j = brace_idx + 1
    last_nl = -1
    while j < n and text[j].isspace():
        if text[j] == "\n":
            last_nl = j
        j += 1
    return last_nl + 1 if last_nl >= 0 else None


def mod_body(text: str) -> str | None:
    """Dedented body of the FIRST `[pub ]mod NAME { ... }` block, or None when no
    such block exists. The opener is located structurally (`find_mod_opener`);
    the matching `}` is found by brace-depth over the raw text — mirroring the
    established `_extract_gen_tests_body` scan (naive of string/char literals, as
    that one is). Leading common indentation is stripped so the body can be
    re-wrapped or concatenated flat."""
    opener = find_mod_opener(text, gen_only=False)
    if opener is None:
        return None
    brace = opener[1]
    depth = 1
    i = brace + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                body = text[brace + 1:i]
                lines = body.splitlines()
                non_empty = [ln for ln in lines if ln.strip()]
                if non_empty:
                    indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
                    if indent > 0:
                        body = "\n".join(
                            (ln[indent:] if len(ln) >= indent and ln[:indent].isspace() else ln)
                            for ln in lines
                        )
                return body.strip() + "\n"
        i += 1
    return None


def count_test_attrs(block: str) -> int:
    """Number of `#[test]` attributes, incl. path-qualified `#[tokio::test]`
    (regex-free, R7; mirrors `#\\[(?:\\w+::)*test\\]`)."""
    count = 0
    i = 0
    while (k := block.find("#[", i)) >= 0:
        i = k + 2
        j = k + 2
        while True:                       # consume zero or more `<ident>::` segments
            seg = j
            while j < len(block) and block[j] in _IDENT_CHARS:
                j += 1
            if j > seg and block[j:j + 2] == "::":
                j += 2
                continue
            j = seg
            break
        if block[j:j + 5] == "test]":
            count += 1
    return count


def count_attrs(block: str, name: str) -> int:
    """Number of `#[<name>]` / `#[<name> = …]` / `#[<name>(…)]` attributes, incl.
    path-qualified heads (`#[foo::<name>]`) — regex-free companion to
    :func:`count_test_attrs` for an arbitrary attribute."""
    count = 0
    i = 0
    while (k := block.find("#[", i)) >= 0:
        i = k + 2
        j = k + 2
        while True:                       # consume zero or more `<ident>::` segments
            seg = j
            while j < len(block) and block[j] in _IDENT_CHARS:
                j += 1
            if j > seg and block[j:j + 2] == "::":
                j += 2
                continue
            j = seg
            break
        if block[j:j + len(name)] == name:
            t = j + len(name)
            while t < len(block) and block[t] in " \t":
                t += 1
            if t < len(block) and block[t] in "]=(":
                count += 1
    return count


def _fn_name_on_line(line: str) -> str:
    """The name in a `fn <name>(` declaration on `line`, else ""."""
    k = 0
    while (k := line.find("fn", k)) >= 0:
        boundary = k == 0 or line[k - 1] not in _IDENT_CHARS
        j = k + 2
        if boundary and j < len(line) and line[j].isspace():
            while j < len(line) and line[j].isspace():
                j += 1
            s = j
            while j < len(line) and line[j] in _IDENT_CHARS:
                j += 1
            if j > s:
                return line[s:j]
        k += 2
    return ""


def attributed_test_fn_names(text: str) -> list[str]:
    """Names of every `#[test]`-family attributed fn, in source order.

    Tolerates attribute lines between the attribute and the declaration
    (`#[test]` / `#[should_panic]` / `fn x()`), which the next-line match in
    `rust_idents.test_fn_names` (pinned byte-equivalent to its old regex) drops
    — a dropped name is a test that no caller can hold accountable."""
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not is_test_attr_line(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt or nxt.startswith(("//", "#")):
                j += 1
                continue
            name = _fn_name_on_line(lines[j])
            if name:
                out.append(name)
            break
        i = j + 1
    return out


def _leading_ident(token: str) -> str:
    """The leading Rust identifier of ``token`` (``foo(`` → ``foo``, ``Bar<T>`` → ``Bar``)."""
    out: list[str] = []
    for ch in token:
        if ch in _IDENT_CHARS:
            out.append(ch)
        else:
            break
    name = "".join(out)
    return name if name and name[0] in _IDENT_START else ""


def item_names(text: str) -> list[str]:
    """Names declared after an item keyword (fn/struct/enum/trait/type/const).

    Token-scan replacement for ``\\b(?:fn|struct|...)\\s+([A-Za-z_]\\w*)``: the
    keyword must be a whitespace-delimited token, and the name is the leading
    identifier of the following token. Order-preserving; duplicates kept (callers
    that want a set can wrap in ``set()``).
    """
    tokens = text.split()
    names: list[str] = []
    for i, tok in enumerate(tokens[:-1]):
        if tok in _ITEM_KEYWORDS:
            name = _leading_ident(tokens[i + 1])
            if name:
                names.append(name)
    return names


def struct_declarations(text: str) -> list[tuple[str, int]]:
    """(name, index of its ``struct`` keyword) per ``struct NAME`` item, in
    declaration order.

    Comment/string aware (``code_mask``) and fn-body blind: a ``struct`` in a
    doc example, in an annotated-stub comment (failed attempts are preserved as
    comments, cargo error text included), or a fn-local helper is not a
    declaration of the file's layout."""
    mask = code_mask(text)
    found: list[tuple[str, int]] = []
    n = len(text)
    i = 0
    while i < n:
        if mask[i] or text[i] not in _IDENT_START:
            i += 1
            continue
        j = i
        while j < n and not mask[j] and text[j] in _IDENT_CHARS:
            j += 1
        word = text[i:j]
        if word == "struct":
            k = j
            while k < n and (mask[k] or text[k].isspace()):
                k += 1
            s = k
            while k < n and not mask[k] and text[k] in _IDENT_CHARS:
                k += 1
            if k > s and text[s] in _IDENT_START:
                found.append((text[s:k], i))
            i = k
        elif word == "fn":
            i = _skip_fn_item(text, mask, j)
        else:
            i = j
    return found


def struct_names(text: str) -> list[str]:
    """Names declared by ``struct NAME`` items, in declaration order.

    The layout fingerprint is taken over this set and must survive body
    splices, hence the comment/fn-body blindness of `struct_declarations`."""
    return [name for name, _ in struct_declarations(text)]


def _skip_fn_item(text: str, mask: Sequence[bool], i: int) -> int:
    """Index just past the fn whose ``fn`` keyword ends at ``i``: past its
    body's matching ``}``, or past the ``;`` of a bodyless declaration
    (trait method, extern). A ``}`` at bracket depth 0 before either means
    this ``fn`` was a fn-POINTER type inside some braced item
    (``f: fn(u32)``) — stop there without consuming, so items after the
    enclosing brace stay visible to the caller's scan."""
    n = len(text)
    group = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch in "([":
            group += 1
        elif ch in ")]":
            group -= 1
        elif group <= 0:
            if ch == ";":
                return i + 1
            if ch == "}":
                return i
            if ch == "{":
                depth = 0
                while i < n:
                    if not mask[i]:
                        if text[i] == "{":
                            depth += 1
                        elif text[i] == "}":
                            depth -= 1
                            if depth == 0:
                                return i + 1
                    i += 1
                return n
        i += 1
    return n


def mod_decls(text: str) -> list[str]:
    """Names from ``mod NAME;`` declarations (NOT inline ``mod NAME { ... }``).

    Char-scan replacement for ``\\bmod\\s+(\\w+)\\s*;``: a ``mod`` token (word
    boundary before), then ``\\s+``, an identifier (``\\w+``), optional
    whitespace, then ``;``. An inline module (``mod m {``) lacks that ``;`` and
    is excluded, matching the regex exactly. Order-preserving.
    """
    n = len(text)
    names: list[str] = []
    i = 0
    while True:
        p = text.find("mod", i)
        if p == -1:
            return names
        i = p + 3
        if p > 0 and text[p - 1] in _IDENT_CHARS:  # \b: not a word boundary
            continue
        j = p + 3
        if j >= n or not text[j].isspace():         # \s+ (at least one)
            continue
        while j < n and text[j].isspace():
            j += 1
        k = j
        while k < n and text[k] in _IDENT_CHARS:     # \w+
            k += 1
        if k == j:
            continue
        m = k
        while m < n and text[m].isspace():           # \s*
            m += 1
        if m < n and text[m] == ";":
            names.append(text[j:k])


def count_cfg_attrs(text: str, needles: Sequence[str]) -> int:
    """Count ``#[cfg(...)]`` attributes whose predicate (up to the first ``)``)
    contains any of ``needles``.

    Substring-scan replacement for ``#\\[cfg\\([^)]*(needle)``; preserves the
    original first-``)`` truncation semantics so the count is unchanged.
    """
    marker = "#[cfg("
    count = 0
    i = 0
    while True:
        p = text.find(marker, i)
        if p == -1:
            return count
        close = text.find(")", p)
        segment = text[p:close if close != -1 else len(text)]
        if any(n in segment for n in needles):
            count += 1
        i = p + len(marker)


def rust_delimiters_balanced(text: str) -> bool:
    """Best-effort syntactic sanity check: are ``{}``, ``[]``, ``()`` balanced in
    ``text``? Respects string literals, char literals, and line/block comments.

    Catches the most common LLM-output corruption (extra closing paren, missing
    brace) without a full Rust parser. Used to validate gen_tests siblings before
    committing them and to detect already-broken siblings at startup. Regex-free
    char scan (R7)."""
    if not text:
        return True
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_str = False
    in_chr = False
    in_line_cmt = False
    in_blk_cmt = 0
    esc = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if esc:
            esc = False
            i += 1
            continue
        if in_line_cmt:
            if c == "\n":
                in_line_cmt = False
            i += 1
            continue
        if in_blk_cmt:
            if c == "*" and nxt == "/":
                in_blk_cmt -= 1
                i += 2
                continue
            if c == "/" and nxt == "*":
                in_blk_cmt += 1
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if in_chr:
            if c == "\\":
                esc = True
            elif c == "'":
                in_chr = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "/" and nxt == "/":
            in_line_cmt = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_blk_cmt = 1
            i += 2
            continue
        # `'` is either a char literal or a lifetime. Skipping both left the
        # in_chr branch dead and counted the brace in `let c = '{';` as a real
        # opener, so a test using a brace/paren char literal was rejected as
        # corrupt output. Disambiguate by lookahead: a char literal closes on
        # `'` right after one char or one escape; anything else is a lifetime
        # (`'a`), which carries no delimiter and is skipped.
        if c == "'":
            if nxt == "\\":
                in_chr = True
                i += 1
                continue
            if i + 2 < n and text[i + 2] == "'":
                i += 3          # `'x'` consumed whole
                continue
            i += 1              # lifetime
            continue
        if c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack[-1] != pairs[c]:
                return False
            stack.pop()
        i += 1
    return not stack and not in_str and not in_chr and not in_blk_cmt


# --- source-LINE classification (replaces _extract_branch_guard / ---
# --- _is_structural_line / _has_nonempty_test_body source-regex scans) ---

_GUARD_ITEM_KEYWORDS = ("impl", "struct", "enum", "trait")
_FN_MODIFIERS = ("pub", "async", "unsafe")


def _starts_word(s: str, kw: str) -> bool:
    """`s` begins with `kw` ended on a ``\\b`` word boundary (next char non-ident
    or end-of-string)."""
    return s.startswith(kw) and (len(s) == len(kw) or s[len(kw)] not in _IDENT_CHARS)


def is_rust_item_boundary_line(stripped: str) -> bool:
    """True iff a left-stripped source line opens an item that ENDS a branch-guard
    back-walk: a fn signature (``(?:pub\\s+|async\\s+|unsafe\\s+)*fn\\s+``) or an
    ``impl``/``struct``/``enum``/``trait`` item (``^(?:impl|struct|enum|trait)\\b``).
    Byte-equivalent to the two boundary ``re.match`` tests in `_extract_branch_guard`."""
    for kw in _GUARD_ITEM_KEYWORDS:
        if _starts_word(stripped, kw):
            return True
    n = len(stripped)
    i = 0
    while True:  # (?:pub\s+|async\s+|unsafe\s+)*
        for m in _FN_MODIFIERS:
            if stripped.startswith(m, i) and i + len(m) < n and stripped[i + len(m)].isspace():
                j = i + len(m)
                while j < n and stripped[j].isspace():
                    j += 1
                i = j
                break
        else:
            break
    return stripped.startswith("fn", i) and i + 2 < n and stripped[i + 2].isspace()


def _consume_brace_else(s: str, i: int) -> int:
    """Past an optional leading ``}`` + whitespace — emulates ``(?:\\}\\s*)?`` then
    leaves `i` at the first char of the expected ``else``."""
    n = len(s)
    if i < n and s[i] == "}":
        i += 1
        while i < n and s[i].isspace():
            i += 1
    return i


def branch_guard_label(stripped: str) -> str | None:
    """Branch-guard description for a left-stripped line, or None — byte-equivalent
    to the if / else-if / else / match-arm / while ladder in `_extract_branch_guard`
    (same precedence)."""
    n = len(stripped)
    # ^(?:\}\s*)?else\s+if\b   OR   ^if\b
    i = _consume_brace_else(stripped, 0)
    else_if = False
    if stripped.startswith("else", i):
        j = i + 4
        if j < n and stripped[j].isspace():
            while j < n and stripped[j].isspace():
                j += 1
            else_if = _starts_word(stripped[j:], "if")
    if else_if or _starts_word(stripped, "if"):
        return stripped[:120]
    # ^(?:\}\s*)?else\s*\{
    i = _consume_brace_else(stripped, 0)
    if stripped.startswith("else", i):
        j = i + 4
        while j < n and stripped[j].isspace():
            j += 1
        if j < n and stripped[j] == "{":
            return "else branch"
    # =>\s*\{?\s*$   (anchored at end; a trailing `{` is optional)
    t = stripped.rstrip()
    if t.endswith("=>") or (t.endswith("{") and t[:-1].rstrip().endswith("=>")):
        return f"match arm: {stripped[:100]}"
    if _starts_word(stripped, "while"):
        return stripped[:120]
    return None


def is_structural_brace_line(text: str) -> bool:
    """True for structural-only Rust lines (`}`, `} else {`, `};`, `},`) that LLVM
    miscounts — byte-equivalent to ``re.match(r'^\\}(\\s*else\\s*\\{)?\\s*[;,]?\\s*$')``."""
    n = len(text)
    if n == 0 or text[0] != "}":
        return False
    i = 1
    j = i  # optional (\s*else\s*\{)
    while j < n and text[j].isspace():
        j += 1
    if text.startswith("else", j):
        k = j + 4
        while k < n and text[k].isspace():
            k += 1
        if k < n and text[k] == "{":
            i = k + 1
    while i < n and text[i].isspace():  # \s*
        i += 1
    if i < n and text[i] in ";,":  # [;,]?
        i += 1
    while i < n and text[i].isspace():  # \s*
        i += 1
    return i == n


def has_nonempty_test_fn(block: str) -> bool:
    """True iff `block` has a `#[test]` whose following ``{`` is matched by
    ``[^}]*\\S`` — byte-equivalent to ``re.search(r'#\\[test\\][^{]*\\{[^}]*\\S', block,
    re.DOTALL)``. (Quirk preserved: a `{}` qualifies because the closing `}` is itself
    the ``\\S`` char — the regex matched empty bodies too.)"""
    i = 0
    while (k := block.find("#[test]", i)) >= 0:
        i = k + 7
        b = block.find("{", k + 7)
        if b == -1:
            continue
        tail = block[b + 1:]
        if "}" in tail or tail.strip():
            return True
    return False


# --- scan_downstream_api source scans (use-paths + ```rust fences) ---

def module_use_imports(
    text: str, module: str,
) -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]]]:
    """Parse `use <module>::…` imports from `text`. Returns three lists mirroring the
    three regexes in `scan_downstream_api` (caller keeps the split/strip/filter):
      - brace_groups: inner of ``use <module>::{INNER}``           (``\\{([^}]+)\\}``)
      - submod_brace: (submod, inner) of ``use <module>::SUB::{INNER}``
      - submod_single: (submod, item) of ``use <module>::SUB::ITEM`` (``(\\w+)::(\\w+)\\b``)
    `module` is matched as a literal (the old patterns used ``re.escape(module)``).
    Each list is built by an INDEPENDENT left-to-right pass — the three originals
    were separate ``re.finditer`` calls, so their matches may overlap in the text
    (e.g. a ``::SUB::ITEM`` that lies inside a ``{...}`` brace group)."""
    prefix = "use " + module + "::"
    plen = len(prefix)
    n = len(text)

    def _word_end(p: int) -> int:
        while p < n and (text[p].isalnum() or text[p] == "_"):
            p += 1
        return p

    # (a) use module::{INNER}
    brace_groups: list[str] = []
    i = 0
    while (k := text.find(prefix, i)) >= 0:
        i = k + 1
        q = k + plen
        if q < n and text[q] == "{":
            close = text.find("}", q + 1)
            if close > q + 1:  # [^}]+ requires ≥1 char
                brace_groups.append(text[q + 1:close])
                i = close + 1

    # (b) use module::SUB::{INNER}
    submod_brace: list[tuple[str, str]] = []
    i = 0
    while (k := text.find(prefix, i)) >= 0:
        i = k + 1
        q = k + plen
        s = _word_end(q)
        if s == q or text[s:s + 3] != "::{":
            continue
        close = text.find("}", s + 3)
        if close > s + 3:
            submod_brace.append((text[q:s], text[s + 3:close]))
            i = close + 1

    # (c) use module::SUB::ITEM
    submod_single: list[tuple[str, str]] = []
    i = 0
    while (k := text.find(prefix, i)) >= 0:
        i = k + 1
        q = k + plen
        s = _word_end(q)
        if s == q or text[s:s + 2] != "::":
            continue
        t = _word_end(s + 2)
        if t > s + 2:
            submod_single.append((text[q:s], text[s + 2:t]))
            i = t

    return brace_groups, submod_brace, submod_single


def rust_code_fences(text: str) -> list[str]:
    """Bodies of every ```` ```rust `` fenced block in `text`, in source order —
    byte-equivalent to ``re.finditer(r'```rust\\s*(.*?)```', text, re.DOTALL)``: the
    capture starts after the leading whitespace and runs to the next ```` ``` ````."""
    out: list[str] = []
    needle = "```rust"
    n = len(text)
    i = 0
    while (k := text.find(needle, i)) >= 0:
        p = k + len(needle)
        while p < n and text[p].isspace():  # \s*
            p += 1
        end = text.find("```", p)
        if end == -1:  # unterminated fence → `.*?```` cannot match
            break
        out.append(text[p:end])
        i = end + 3
    return out


# --- more source line/token classifiers ---

_TYPE_DECL_LINE_KEYWORDS = ("struct", "enum", "trait", "impl", "type", "macro_rules")
_DECL_KEYWORDS = frozenset({
    "mod", "fn", "struct", "enum", "trait", "impl", "use", "extern",
    "static", "const", "type", "macro_rules", "pub", "unsafe",
})


def is_type_decl_line(line: str) -> bool:
    """True iff `line` begins a type/item declaration — byte-equivalent to
    ``re.match(r'\\s*(?:pub(?:\\([^)]*\\))?\\s+)?(?:struct|enum|trait|impl|type|macro_rules)\\b')``:
    leading ws, an optional ``pub``/``pub(...)`` visibility, then one of the item
    keywords on a word boundary."""
    n = len(line)
    i = 0
    while i < n and line[i].isspace():  # \s*
        i += 1
    if line.startswith("pub", i):  # optional (?:pub(?:\([^)]*\))?\s+)
        j = i + 3
        if j < n and line[j] == "(":
            close = line.find(")", j + 1)
            if close != -1:
                j = close + 1
        if j < n and line[j].isspace():  # \s+ terminates the pub clause
            while j < n and line[j].isspace():
                j += 1
            i = j
    for kw in _TYPE_DECL_LINE_KEYWORDS:
        if line.startswith(kw, i) and (i + len(kw) == n or line[i + len(kw)] not in _IDENT_CHARS):
            return True
    return False


def starts_with_pub(sig: str) -> bool:
    """True iff `sig` begins (after leading ws) with the `pub` keyword on a word
    boundary — byte-equivalent to ``re.match(r'\\s*pub\\b', sig)``."""
    i = 0
    n = len(sig)
    while i < n and sig[i].isspace():
        i += 1
    return sig.startswith("pub", i) and (i + 3 == n or sig[i + 3] not in _IDENT_CHARS)


def contains_decl_keyword(text: str) -> bool:
    """True iff any whole-word token of `text` is a declaration keyword (`mod`, `fn`,
    `struct`, …, `macro_rules`, `pub`, `unsafe`) — byte-equivalent to
    ``re.search(r'\\b(mod|fn|…|unsafe)\\b', text)``."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] in _IDENT_CHARS:
            j = i + 1
            while j < n and text[j] in _IDENT_CHARS:
                j += 1
            if text[i:j] in _DECL_KEYWORDS:
                return True
            i = j
        else:
            i += 1
    return False


def has_no_std_attr(text: str) -> bool:
    """True iff `text` contains a ``#![no_std]`` crate attribute (whitespace-tolerant)
    — byte-equivalent to ``re.search(r'#\\s*!\\s*\\[\\s*no_std\\s*\\]', text)``."""
    n = len(text)
    i = 0
    while (k := text.find("#", i)) >= 0:
        i = k + 1
        p = k + 1
        while p < n and text[p].isspace():
            p += 1
        if p >= n or text[p] != "!":
            continue
        p += 1
        while p < n and text[p].isspace():
            p += 1
        if p >= n or text[p] != "[":
            continue
        p += 1
        while p < n and text[p].isspace():
            p += 1
        if not text.startswith("no_std", p):
            continue
        p += 6
        while p < n and text[p].isspace():
            p += 1
        if p < n and text[p] == "]":
            return True
    return False


# --- Rust comment stripping (two distinct algorithms — byte-equivalent) ---

def strip_comment_spans(text: str) -> str:
    """Remove ``//`` line comments, ``/* */`` block comments AND ``#`` line comments
    in a SINGLE ordered-alternation pass — byte-equivalent to
    ``re.sub(r'//[^\\n]*|/\\*.*?\\*/|#[^\\n]*', '', text, flags=re.DOTALL)``. A ``/*``
    with no closing ``*/`` is NOT a comment (the ``/`` survives); the terminating
    ``\\n`` of a line comment is kept (``[^\\n]*``)."""
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        two = text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            i = j if j != -1 else n
        elif two == "/*":
            close = text.find("*/", i + 2)
            if close != -1:
                i = close + 2
            else:
                out.append(text[i])
                i += 1
        elif text[i] == "#":
            j = text.find("\n", i)
            i = j if j != -1 else n
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _strip_line_comments(text: str) -> str:
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i:i + 2] == "//":  # //[^\n]*
            j = text.find("\n", i)
            i = j if j != -1 else n
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _strip_block_comments(text: str) -> str:
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i:i + 2] == "/*":  # /\*.*?\*/ (DOTALL; first close)
            close = text.find("*/", i + 2)
            if close != -1:
                i = close + 2
            else:
                out.append(text[i])
                i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def strip_line_block_comments(text: str) -> str:
    """Remove ``//`` line comments THEN ``/* */`` block comments, in two SEQUENTIAL
    passes (NOT a combined alternation — the order is observable, e.g. for ``/* // */``)
    — byte-equivalent to ``re.sub(r'/\\*.*?\\*/', '', re.sub(r'//[^\\n]*', '', text),
    flags=re.DOTALL)``."""
    return _strip_block_comments(_strip_line_comments(text))


# --- Rust attribute-opener scan (replaces _ATTR_OPENER_RE) ---

def attr_opener_end(text: str, i: int) -> tuple[bool, int] | None:
    """Match an attribute opener ``#\\s*(!?)\\s*\\[`` anchored at `i` — byte-equivalent
    to ``_ATTR_OPENER_RE.match(text, i)``. Returns ``(is_bang, end)`` where `is_bang`
    is True for ``#![`` (group 1 == ``!``) and `end` is the index just past the ``[``
    (the old ``m.end()``); None when `text[i]` is not the start of an opener."""
    n = len(text)
    if i >= n or text[i] != "#":
        return None
    p = i + 1
    while p < n and text[p].isspace():  # \s*
        p += 1
    is_bang = False
    if p < n and text[p] == "!":  # (!?)
        is_bang = True
        p += 1
    while p < n and text[p].isspace():  # \s*
        p += 1
    if p < n and text[p] == "[":
        return is_bang, p + 1
    return None


_TOP_LEVEL_ITEM_KEYWORDS = frozenset({
    "fn", "struct", "enum", "trait", "type", "const", "static", "mod", "union",
})


def top_level_item_names(text: str) -> list[str]:
    """``"<keyword> <name>"`` per item declared at brace depth 0, in order.

    The fingerprint a body splice must not change. A body write lands strictly
    inside one fn's braces, so it can only alter this list by UNBALANCING them
    and escaping to file scope — which is how a returned body such as
    ``unimplemented!() }\\npub fn Ok<T>(t: T) -> Result<T> { Ok(t)`` parses
    cleanly yet defines a second top-level `Ok` (E0255 on the whole crate).
    Nested helper fns stay invisible here: they sit at depth ≥ 1, so a body
    that legitimately declares one still fingerprints identically.

    Comment/string aware (`code_mask`), so a `{` in a doc example or in an
    annotated failed attempt does not shift the depth."""
    mask = code_mask(text)
    n = len(text)
    i = 0
    depth = 0
    out: list[str] = []
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch not in _IDENT_START:
            i += 1
            continue
        j = i
        while j < n and not mask[j] and text[j] in _IDENT_CHARS:
            j += 1
        word = text[i:j]
        if depth == 0 and word in _TOP_LEVEL_ITEM_KEYWORDS:
            k = j
            while k < n and (mask[k] or text[k].isspace()):
                k += 1
            s = k
            while k < n and not mask[k] and text[k] in _IDENT_CHARS:
                k += 1
            if k > s and text[s] in _IDENT_START:
                out.append(f"{word} {text[s:k]}")
            j = k
        i = j
    return out


def locate_top_level_item(
    text: str, keyword: str, name: str,
) -> tuple[int, int] | None:
    """`[start, end)` of the depth-0 ``<keyword> <name>`` item, INCLUDING the
    outer attributes attached to it, or None.

    Exists because an AST extractor's rendered `definition` is NOT the bytes on
    disk: syn/prettyplease re-wraps long declarations and adds trailing commas
    inside wrapped argument lists, so matching that text against the file — even
    whitespace-tolerantly — misses every item wide enough to wrap. Anything that
    needs to REPLACE a declaration must locate it structurally and slice the
    file itself.

    Attributes are part of the span on purpose: splicing a replacement that
    carries its own ``#[repr(C)]`` in below a surviving one leaves the item with
    two, and a duplicated ``#[cfg]`` silently changes which cluster it compiles
    in. Comment/string aware, so a brace inside a doc example cannot shift the
    depth."""
    mask = code_mask(text)
    n = len(text)
    i = 0
    depth = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch not in _IDENT_START:
            i += 1
            continue
        j = i
        while j < n and not mask[j] and text[j] in _IDENT_CHARS:
            j += 1
        if depth != 0 or text[i:j] != keyword:
            i = j
            continue
        k = j
        while k < n and (mask[k] or text[k].isspace()):
            k += 1
        s = k
        while k < n and not mask[k] and text[k] in _IDENT_CHARS:
            k += 1
        if text[s:k] != name:
            i = k or j
            continue
        end = _item_end(text, mask, k)
        if end is None:
            return None
        return _item_start(text, mask, i), end
    return None


def _item_end(text: str, mask: Sequence[bool], after_name: int) -> int | None:
    """End offset of an item whose name ends at `after_name`: past the `;` of a
    bodyless declaration, or past the `}` closing its braced body. Whichever
    delimiter comes first at TOP level decides, so `struct Foo(u8);` and
    `type A = B;` end at their semicolon and never swallow the next item.

    `(`/`[` nesting is tracked because both delimiters occur INSIDE a
    declaration's own header: the `;` of an array length (`struct Buf([u8; 16]);`,
    `type Repr = [u8; 4];`) would otherwise end the item mid-type, and splicing a
    replacement over that span leaves ` 16]);` behind as unparseable text."""
    n = len(text)
    i = after_name
    nest = 0
    while i < n:
        if mask[i]:
            i += 1
            continue
        if text[i] in "([":
            nest += 1
            i += 1
            continue
        if text[i] in ")]":
            nest -= 1
            i += 1
            continue
        if nest != 0:
            i += 1
            continue
        if text[i] == ";":
            return i + 1
        if text[i] == "{":
            depth = 0
            while i < n:
                if not mask[i]:
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                        if depth == 0:
                            return i + 1
                i += 1
            return None
        i += 1
    return None


_ITEM_QUALIFIERS = frozenset({"pub", "unsafe", "auto", "default"})


def _item_start(text: str, mask: Sequence[bool], kw_start: int) -> int:
    """`kw_start` moved back over everything that declares the SAME item: its
    visibility (`pub`, `pub(crate)`, `pub(in path)`), other leading qualifiers,
    and its outer attributes.

    Stopping at the keyword would splice a replacement that carries its own
    `pub(crate)` in below the surviving one — `pub(crate) pub(crate) struct` —
    and would leave a duplicate `#[repr]`/`#[cfg]` above it."""
    p = kw_start
    while True:
        q = p - 1
        while q >= 0 and (mask[q] or text[q].isspace()):
            q -= 1
        if q < 0:
            return p
        if text[q] == "]":                       # #[attr]
            r = _open_delim(text, mask, q, "]", "[")
            if r <= 0 or text[r - 1] != "#":
                return p
            p = r - 1
            continue
        if text[q] == ")":                       # pub(crate) / pub(in path)
            r = _open_delim(text, mask, q, ")", "(")
            if r < 0:
                return p
            q = r - 1
            while q >= 0 and (mask[q] or text[q].isspace()):
                q -= 1
        s = q + 1                                # a bare qualifier keyword
        while q >= 0 and not mask[q] and text[q] in _IDENT_CHARS:
            q -= 1
        if q + 1 >= s or text[q + 1:s] not in _ITEM_QUALIFIERS:
            return p
        p = q + 1


def _open_delim(
    text: str, mask: Sequence[bool], close_at: int, close: str, open_: str,
) -> int:
    """Index of the delimiter opening the pair that closes at `close_at`, or -1."""
    depth = 0
    i = close_at
    while i >= 0:
        if not mask[i]:
            if text[i] == close:
                depth += 1
            elif text[i] == open_:
                depth -= 1
                if depth == 0:
                    return i
        i -= 1
    return -1


def redeclared_inner_body(body: str, fn_name: str) -> str | None:
    """The nested fn's own body when `body` is nothing but a re-declaration of
    `fn_name` itself; `None` for every other shape.

    Detection and REPAIR are the same question asked twice, so they are one
    function: the returned text is the work the model actually wrote, which a
    caller splices back in place of the wrapper instead of reverting the fn to
    a stub and throwing that work away. Dedented to column 0 — the caller owns
    re-indentation. An empty string means the shape matched but the nested fn
    was itself empty: nothing to recover.

    See :func:`body_is_only_redeclaration` for why the shape is illegal.
    """
    if not fn_name or not body.strip():
        return None
    head = strip_outer_attributes(body)
    mask = code_mask(head)
    open_at = next((i for i, c in enumerate(head) if c == "{" and not mask[i]), -1)
    if open_at < 0 or not declares_fn(head[:open_at], fn_name):
        return None
    depth = 0
    for i in range(open_at, len(head)):
        if mask[i]:
            continue
        if head[i] == "{":
            depth += 1
        elif head[i] == "}":
            depth -= 1
            if depth == 0:
                tail = head[i + 1:]
                tmask = code_mask(tail)
                if any(c.strip() for j, c in enumerate(tail) if not tmask[j]):
                    return None
                return _dedent_block(head[open_at + 1:i])
    return None


def _dedent_block(text: str) -> str:
    """`text` shifted left by its smallest non-blank indent, blank lines emptied."""
    lines = text.strip("\n").splitlines()
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(ln[cut:] if ln.strip() else "" for ln in lines).strip()


def body_is_only_redeclaration(body: str, fn_name: str) -> bool:
    """True when `body` is nothing but a re-declaration of `fn_name` itself.

    The fingerprint of a model that answered with the WHOLE item when it was
    asked for a body. Splicing `fn f(..) { work() }` between the braces of the
    existing `fn f` yields::

        fn f(..) {          // the real fn — body is now EMPTY
            fn f(..) {      // a nested item; never called
                work()
            }
        }

    That parses, and the file's top-level item list is unchanged because a
    nested item sits at depth >= 1 — so neither the parse gate nor the
    item-set gate rejects it. The outer fn silently returns `()` instead of
    doing the work, while the fn reads as FILLED to every later stub scan: an
    unimplemented fn that no longer looks unimplemented.

    Only the do-nothing shape is refused. A body that declares a same-named
    nested helper AND runs code (recursive-helper style) is legal Rust that
    works, so it is left alone.
    """
    return redeclared_inner_body(body, fn_name) is not None


def strip_outer_attributes(text: str) -> str:
    """`text` from its first real token onward — leading outer attributes, doc
    comments and comments removed.

    A model-proposed signature usually re-emits the item's attributes. Those
    attributes already sit in the file and are not the model's to restate: a
    signature-only splice that starts at the `fn`/`pub` line leaves the
    originals in place, so writing them again DUPLICATES them — and a duplicated
    `#[cfg(...)]` silently changes which cluster the fn compiles in.

        WRONG — model sig written verbatim     RIGHT — attributes consumed
        #[cfg(feature = "std")]                #[cfg(feature = "std")]
        #[cold]                                #[cold]
        #[cfg(feature = "std")]                pub fn f<T: Clone>(x: T) -> T {
        #[cold]
        pub fn f<T: Clone>(x: T) -> T {

        WRONG                                  RIGHT
        /// doc line                           pub fn g(x: u8) -> u8 {
        #[inline]
        #[inline]
        pub fn g(x: u8) -> u8 {

    Bracket matching is mask-aware, so a `]` inside an attribute's string
    literal (`#[doc = "a ] b"]`) does not close it. An unterminated attribute
    leaves the text untouched from that point (conservative: never truncate)."""
    mask = code_mask(text)
    n = len(text)
    i = 0
    while i < n:
        if text[i].isspace() or mask[i]:
            i += 1
            continue
        opened = attr_opener_end(text, i)
        if opened is None:
            break
        _bang, j = opened
        depth = 1
        while j < n and depth:
            if not mask[j]:
                if text[j] == "[":
                    depth += 1
                elif text[j] == "]":
                    depth -= 1
            j += 1
        if depth:
            break
        i = j
    return text[i:]


def has_code_brace(text: str) -> bool:
    """True iff `text` contains a `{` outside strings/comments — the test for
    "this is a bare signature, not a whole item (or several)". A signature has
    no braces of its own: generics use `<>`, a where-clause's `Fn() -> ()` has
    none. A `{` therefore means the candidate carries a BODY, and splicing it
    where only a signature is expected duplicates the item that follows."""
    mask = code_mask(text)
    return any(ch == "{" and not mask[k] for k, ch in enumerate(text))


# --- test-attribute line detection (replaces _count_test_fns_cfg_aware regexes) ---

_TEST_ATTR_TOKENS = (
    "test", "tokio::test", "async_std::test", "serial_test::serial",
    "rstest", "parameterized", "test_case",
)


def is_attr_line(line: str) -> bool:
    """True iff `line` (after leading ws) opens an attribute ``#[`` — byte-equivalent
    to ``re.match(r'^\\s*#\\s*\\[', line)``."""
    n = len(line)
    i = 0
    while i < n and line[i].isspace():
        i += 1
    if i >= n or line[i] != "#":
        return False
    i += 1
    while i < n and line[i].isspace():
        i += 1
    return i < n and line[i] == "["


def is_test_attr_line(line: str) -> bool:
    """True iff `line` is a ``#[test]``-family attribute line — byte-equivalent to
    ``re.search(r'^\\s*#\\s*\\[\\s*(?:test|tokio::test|async_std::test|serial_test::serial|'
    ``rstest|parameterized|test_case)\\s*[\\s\\],(]', line)``. The token must be followed
    by whitespace or one of ``] , (`` (so ``#[testing]`` does NOT match)."""
    n = len(line)
    i = 0
    while i < n and line[i].isspace():  # ^\s*
        i += 1
    if i >= n or line[i] != "#":
        return False
    i += 1
    while i < n and line[i].isspace():  # \s*
        i += 1
    if i >= n or line[i] != "[":
        return False
    i += 1
    while i < n and line[i].isspace():  # \s*
        i += 1
    for tok in _TEST_ATTR_TOKENS:
        if not line.startswith(tok, i):
            continue
        k = j = i + len(tok)
        while k < n and line[k].isspace():  # \s*
            k += 1
        if k > j or (k < n and line[k] in "],("):  # [\s\],(] : whitespace consumed, or ]/,/(
            return True
    return False


def replace_elision_placeholders(content: str) -> str:
    r"""Replace every ``{ ... }`` elision-placeholder body with ``{ todo!() }`` —
    byte-equivalent to ``re.sub(r'\{\s*\.\.\.\s*\}', '{ todo!() }', content)``."""
    n = len(content)
    out: list[str] = []
    i = 0
    while i < n:
        if content[i] == "{":
            j = i + 1
            while j < n and content[j].isspace():  # \s*
                j += 1
            if content[j:j + 3] == "...":
                k = j + 3
                while k < n and content[k].isspace():  # \s*
                    k += 1
                if k < n and content[k] == "}":
                    out.append("{ todo!() }")
                    i = k + 1
                    continue
        out.append(content[i])
        i += 1
    return "".join(out)


_ASSERT_SUFFIXES = ("_eq", "_ne", "_matches", "_approx_eq", "")


def _word_before(line: str, k: int) -> bool:
    """True iff `line[k-1]` is a word char (so a ``\\b`` sits at index `k`)."""
    return k > 0 and (line[k - 1].isalnum() or line[k - 1] == "_")


def line_has_assertion(line: str) -> bool:
    r"""True iff `line` contains a Rust test assertion — byte-equivalent to
    ``re.search(r'\b(assert(?:_eq|_ne|_matches|_approx_eq)?!|assert!\s*\(|\.unwrap\(\)|\.expect\()', line)``.
    The ``\b`` precedes the WHOLE group: `assert…` must start at a word boundary, and
    ``.unwrap()`` / ``.expect(`` require a word char immediately before the ``.``."""
    n = len(line)
    for needle in (".unwrap()", ".expect("):  # \b\.unwrap\(\) / \b\.expect\(  → '.' after a word char
        k = line.find(needle)
        while k >= 0:
            if _word_before(line, k):
                return True
            k = line.find(needle, k + 1)
    k = line.find("assert!")  # \bassert!\s*\(
    while k >= 0:
        if not _word_before(line, k):
            q = k + 7
            while q < n and line[q].isspace():
                q += 1
            if q < n and line[q] == "(":
                return True
        k = line.find("assert!", k + 1)
    k = line.find("assert")  # \bassert(suffix)?!
    while k >= 0:
        if not _word_before(line, k):
            p = k + 6
            for suf in _ASSERT_SUFFIXES:
                if line.startswith(suf, p) and line.startswith("!", p + len(suf)):
                    return True
        k = line.find("assert", k + 1)
    return False


def gen_mod_body_start(block: str) -> int | None:
    r"""Index just past the ``{`` of the first ``mod gen_<name> {`` wrapper, or None —
    byte-equivalent to the ``m.end()`` of ``re.search(
    r'(?:#\[[^\]]*\]\s*\n)*\s*(?:pub\s+)?mod\s+gen_\w+\s*\{', block)``. (The optional
    attr/pub prefix never changes which ``mod`` matches; ``mod`` is NOT word-bounded,
    matching the regex.)"""
    n = len(block)
    i = 0
    while (k := block.find("mod", i)) >= 0:
        i = k + 3
        p = k + 3
        if p >= n or not block[p].isspace():  # \s+
            continue
        while p < n and block[p].isspace():
            p += 1
        if not block.startswith("gen_", p):  # gen_
            continue
        e = p + 4
        while e < n and (block[e].isalnum() or block[e] == "_"):  # \w+
            e += 1
        if e == p + 4:  # need ≥1 word char after gen_
            continue
        b = e
        while b < n and block[b].isspace():  # \s*
            b += 1
        if b < n and block[b] == "{":
            return b + 1
    return None


def _skip_fn_modifiers(text: str, p: int) -> int:
    """Past an optional ``(pub(…)? )(async )(unsafe )(const )`` modifier run (each
    keyword + ``\\s+`` whitespace, which may span newlines), starting at `p`."""
    n = len(text)
    if text.startswith("pub", p):  # pub(?:\([^)]*\))?\s+
        q = p + 3
        if q < n and text[q] == "(":
            c = text.find(")", q + 1)
            if c != -1:
                q = c + 1
        if q < n and text[q].isspace():
            while q < n and text[q].isspace():
                q += 1
            p = q
    for kw in ("async", "unsafe", "const"):  # <kw>\s+
        if text.startswith(kw, p):
            q = p + len(kw)
            if q < n and text[q].isspace():
                while q < n and text[q].isspace():
                    q += 1
                p = q
    return p


def named_fn_decl_start(text: str, fn_name: str) -> int | None:
    r"""``m.start()`` of the first ``(?:^|\n)[ \t]*(mods)?fn <fn_name>\b`` declaration
    (MULTILINE), or None — byte-equivalent to that regex's match start. The modifier
    ``\s+`` may span newlines (``pub`` one line, ``fn`` the next), so the start can be
    the ``\n`` of an earlier line."""
    n = len(text)
    line_start = 0
    while line_start <= n:
        p = line_start
        while p < n and text[p] in " \t":  # [ \t]*
            p += 1
        p = _skip_fn_modifiers(text, p)
        if text.startswith("fn", p):
            q = p + 2
            if q < n and text[q].isspace():  # fn\s+
                while q < n and text[q].isspace():
                    q += 1
                if text.startswith(fn_name, q):
                    e = q + len(fn_name)
                    if e == n or text[e] not in _IDENT_CHARS:  # \b
                        return line_start - 1 if line_start > 0 else 0
        nl = text.find("\n", line_start)
        if nl == -1:
            break
        line_start = nl + 1
    return None
