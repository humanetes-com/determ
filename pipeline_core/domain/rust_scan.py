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
        # char vs lifetime — treat both as opaque; good enough for balance.
        if c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack[-1] != pairs[c]:
                return False
            stack.pop()
        i += 1
    return not stack and not in_str and not in_chr and not in_blk_cmt


# --- P7: source-LINE classification (replaces _extract_branch_guard / ---
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


# --- P7: scan_downstream_api source scans (use-paths + ```rust fences) ---

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


# --- P7: more source line/token classifiers ---

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


# --- P7: Rust comment stripping (two distinct algorithms — byte-equivalent) ---

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


# --- P7: Rust attribute-opener scan (replaces _ATTR_OPENER_RE) ---

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
