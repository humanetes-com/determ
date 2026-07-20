"""Rust identifier tokenisation — pure domain (P7 source-regex→structured).

Replaces the ``\\bfn\\s+(\\w+)`` / ``\\b<name>\\b`` regex scans that built the
leaf-order reference-count map in pipeline.py. A maximal-``\\w+``-run tokeniser
is byte-equivalent to Python's word-boundary regex for identifier words (a
``\\b<name>\\b`` match is exactly a maximal word-run equal to <name>), so the
produced counts — and thus the leaf-first ordering — are unchanged.

Pure: stdlib only, no I/O, no regex. Intentionally a token scan (not a syn AST
walk) because the original counted occurrences EVERYWHERE — including comments
and string literals — as an acknowledged approximation that is "robust enough
for leaf-first scheduling"; a token scan preserves that exactly.
"""

from __future__ import annotations

from collections import Counter


def _word_spans(text: str) -> list[tuple[int, int]]:
    """`(start, end)` of every maximal ``[A-Za-z0-9_]+`` run — the unit Python's
    ``\\b...\\b`` word boundaries delimit."""
    spans: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i].isalnum() or text[i] == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _kw_followed_by(text: str, keyword: str, target: str) -> bool:
    """True iff a whole-word `keyword` token is followed — across one-or-more
    whitespace — immediately by `target` ending on a word boundary. Byte-equivalent
    to ``\\b<keyword>\\s+<target>\\b`` (so `unsafe extern fn` or `unsafe/*c*/fn` do
    NOT match, matching the regex's adjacency)."""
    n = len(text)
    for s, e in _word_spans(text):
        if text[s:e] != keyword:
            continue
        j = e
        while j < n and text[j].isspace():
            j += 1
        if j == e:
            continue  # `\s+` requires at least one whitespace char
        if text.startswith(target, j):
            k = j + len(target)
            if k == n or not (text[k].isalnum() or text[k] == "_"):
                return True
    return False


def _is_ascii_ident_start(c: str) -> bool:
    """True iff `c` is an ASCII letter or underscore — the ``[A-Za-z_]`` of a
    Rust fn-name lead char (ASCII-only, unlike Python's unicode ``\\w``)."""
    return c == "_" or ("a" <= c <= "z") or ("A" <= c <= "Z")


def is_unsafe_fn(sig: str) -> bool:
    """True iff `sig` contains `unsafe fn` (``\\bunsafe\\s+fn\\b``)."""
    return _kw_followed_by(sig, "unsafe", "fn")


def is_trait_impl_container(container: str) -> bool:
    """True iff `container` is a TRAIT impl header (``impl Trait for Type``) —
    whose member signatures are fixed by the trait (adding a bound → E0276).

    Container strings come from the syn enumerator token stream
    (``impl StdError for ContextError < C , E >``) or a compact header
    (``impl Display for X``). Trait impl iff some whole-word ``for`` token is
    NOT immediately followed by a ``<`` token — an HRTB renders as
    ``for < 'a >`` (spaced) or ``for<'a>`` (compact, no bare ``for`` token),
    so neither form false-positives. Inherent impls (``impl Foo``), trait
    declarations (``trait Foo``), and free fns (empty container) are False."""
    ws = container.split()
    if not ws or ws[0] != "impl":
        return False
    return any(
        tok == "for" and (i + 1 >= len(ws) or not ws[i + 1].startswith("<"))
        for i, tok in enumerate(ws))


def declares_fn(sig: str, fn_name: str) -> bool:
    """True iff `sig` declares `fn <fn_name>` (``\\bfn\\s+<fn_name>\\b``)."""
    return _kw_followed_by(sig, "fn", fn_name)


_TYPE_DEF_KEYWORDS = frozenset({"struct", "enum", "trait", "type", "impl"})


def _is_camelcase_type(word: str) -> bool:
    """True iff `word` matches ``[A-Z][A-Za-z0-9]+`` as a whole: ASCII
    uppercase-led, length ≥ 2, all ASCII alphanumerics (no underscore)."""
    return len(word) >= 2 and word.isascii() and word.isalnum() and word[0].isupper()


def camelcase_type_names(text: str) -> set[str]:
    """Set of CamelCase identifiers in `text` — byte-equivalent to
    ``set(re.findall(r'\\b([A-Z][A-Za-z0-9]+)\\b', text))`` (the `\\b` boundaries
    mean a CamelCase word inside `Foo_Bar` or `fooBar` never matches)."""
    return {w for s, e in _word_spans(text) if _is_camelcase_type(w := text[s:e])}


def defined_type_names(text: str) -> set[str]:
    """CamelCase names declared after a `struct`/`enum`/`trait`/`type`/`impl`
    keyword — byte-equivalent to
    ``re.finditer(r'\\b(?:struct|enum|trait|type|impl)\\s+([A-Z][A-Za-z0-9]+)\\b')``.
    The name must follow the keyword across one-or-more whitespace (so `impl<T>`
    or `type=` never capture)."""
    spans = _word_spans(text)
    words = [text[s:e] for s, e in spans]
    out: set[str] = set()
    for idx, w in enumerate(words):
        if w not in _TYPE_DEF_KEYWORDS or idx + 1 >= len(words):
            continue
        gap = text[spans[idx][1]:spans[idx + 1][0]]
        if not gap or not gap.isspace():
            continue
        name = words[idx + 1]
        if _is_camelcase_type(name):
            out.add(name)
    return out


_TYPE_DECL_KEYWORDS = frozenset({"struct", "enum", "trait"})


def defines_type(text: str, name: str) -> bool:
    """True iff `text` declares `struct`/`enum`/`trait` `<name>` — byte-equivalent
    to ``re.search(rf'\\b(?:struct|enum|trait)\\s+{re.escape(name)}\\b', text)`` for
    an identifier `name`. The keyword token must be followed across one-or-more
    whitespace by a word-span equal to `name` exactly (so `struct FooBar` does NOT
    satisfy name=`Foo`, and `mystruct Foo` never matches — `struct` is mid-word)."""
    spans = _word_spans(text)
    for idx in range(len(spans) - 1):
        s, e = spans[idx]
        if text[s:e] not in _TYPE_DECL_KEYWORDS:
            continue
        ns, ne = spans[idx + 1]
        gap = text[e:ns]
        if gap and gap.isspace() and text[ns:ne] == name:
            return True
    return False


def _consume_opt_kw_ws(line: str, i: int, kw: str) -> int:
    """If `line[i:]` starts with `kw` followed by ≥1 whitespace, return the index
    past `kw` and all following whitespace (greedy ``\\s+``); else return `i`
    unchanged — emulates an optional ``(?:<kw>\\s+)?`` group (backtracks to empty
    when `kw` is not whitespace-terminated, e.g. `public` for kw=`pub`)."""
    n = len(line)
    if line.startswith(kw, i):
        j = i + len(kw)
        if j < n and line[j].isspace():
            while j < n and line[j].isspace():
                j += 1
            return j
    return i


def is_fn_signature_line(line: str) -> bool:
    """True iff `line` is a fn-signature line lead — byte-equivalent to
    ``re.match(r'^\\s*(?:pub\\s+)?(?:async\\s+)?fn\\s+', line)``: leading whitespace,
    an optional `pub`, an optional `async`, then `fn` followed by ≥1 whitespace."""
    n = len(line)
    i = 0
    while i < n and line[i].isspace():  # ^\s*
        i += 1
    i = _consume_opt_kw_ws(line, i, "pub")
    i = _consume_opt_kw_ws(line, i, "async")
    return line.startswith("fn", i) and i + 2 < n and line[i + 2].isspace()


def _skip_balanced_angles(text: str, i: int) -> int | None:
    """`text[i]` is ``<``; return the index just past the matching ``>`` (nesting
    honoured), or None if the angles never balance."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def impl_self_type(container: str) -> str | None:
    """Self/target type name of an impl signature: the type after `for` in
    `impl Trait for X<…>`, else the head type in `impl X<…>` (inherent). Returns the
    bare type identifier (generics stripped) or None.

    Replaces ``\\bfor\\s+([A-Za-z_]\\w*)`` + ``\\bimpl\\s+(?:<[^>]*>\\s+)?([A-Za-z_]\\w*)``
    (for-branch first, impl-branch fallback). Byte-equal to the old regexes wherever
    they matched; additionally a structural fix — BALANCED own-generics so
    `impl<T: Tr<X>> Foo` resolves to `Foo` (old single-`>` `<[^>]*>` returned ""), and
    a space-less `impl<T> Foo` resolves (old required `impl\\s+`)."""
    spans = _word_spans(container)
    n = len(container)
    # for-branch: first `for <ident>` (whitespace-gap, ASCII-led name).
    for idx in range(len(spans) - 1):
        s, e = spans[idx]
        if container[s:e] != "for":
            continue
        ns, ne = spans[idx + 1]
        gap = container[e:ns]
        if gap and gap.isspace() and _is_ascii_ident_start(container[ns]):
            return container[ns:ne]
    # impl-branch: `impl [<generics>] <ident>`.
    for s, e in spans:
        if container[s:e] != "impl":
            continue
        p = e
        while p < n and container[p].isspace():
            p += 1
        if p < n and container[p] == "<":
            np = _skip_balanced_angles(container, p)
            if np is None:
                continue
            p = np
            while p < n and container[p].isspace():
                p += 1
        if p < n and _is_ascii_ident_start(container[p]):
            q = p + 1
            while q < n and (container[q].isalnum() or container[q] == "_"):
                q += 1
            return container[p:q]
    return None


_CRATE_LINT_KEYWORDS = ("deny", "forbid")


def crate_lint_attr_args(text: str) -> list[str]:
    """Inner argument strings of every ``#![deny(…)]`` / ``#![forbid(…)]`` crate
    inner-attribute, in source order — group(1) of
    ``#!\\[(?:deny|forbid)\\(([^)]+)\\)\\]``. Args run up to the first ``)`` (≥1 char)
    and the attr must close ``)]``. The caller splits each on commas."""
    out: list[str] = []
    n = len(text)
    i = 0
    while True:
        j = text.find("#![", i)
        if j == -1:
            break
        k = j + 3
        kw = next((w for w in _CRATE_LINT_KEYWORDS if text.startswith(w + "(", k)), None)
        if kw is None:
            i = j + 1
            continue
        op = k + len(kw) + 1  # past `(`
        cp = text.find(")", op)
        if cp > op and cp + 1 < n and text[cp + 1] == "]":  # `([^)]+)\)\]`
            out.append(text[op:cp])
            i = cp + 2
        else:
            i = j + 1
    return out


def _ident_token_start(text: str, s: int, e: int) -> int:
    """Within word-span ``[s, e)``, the index of the first ASCII ident-start char
    (``[A-Za-z_]``) — the offset where ``re.findall(r'[A-Za-z_]\\w*')`` begins inside
    a run, skipping leading digits — or `e` if the run is all digits."""
    p = s
    while p < e and not _is_ascii_ident_start(text[p]):
        p += 1
    return p


def identifier_tokens(text: str) -> list[str]:
    """Every ``[A-Za-z_]\\w*`` identifier token, in order — byte-equivalent to
    ``re.findall(r'[A-Za-z_]\\w*', text)``. Each maximal word-run yields at most one
    token (the suffix from its first letter/underscore to the run end); all-digit
    runs yield nothing."""
    out: list[str] = []
    for s, e in _word_spans(text):
        p = _ident_token_start(text, s, e)
        if p < e:
            out.append(text[p:e])
    return out


def ascii_word_tokens(text: str) -> list[str]:
    """ASCII identifier tokens, in order — byte-equivalent to
    ``re.findall(r'\\b([A-Za-z_][A-Za-z0-9_]*)\\b', text)``. A ``\\b…\\b`` match is
    exactly a maximal ``\\w+`` run that is entirely ASCII ``[A-Za-z0-9_]`` and
    letter/underscore-led; a run containing a non-ASCII word char (or starting with
    a digit) has no internal boundary, so it yields nothing — unlike the unicode-`\\w`
    `identifier_tokens`."""
    out: list[str] = []
    for s, e in _word_spans(text):
        c = text[s]
        if (("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_") and all(
            ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch == "_"
            for ch in text[s:e]
        ):
            out.append(text[s:e])
    return out


def mask_identifiers(text: str, repl: str = "X") -> str:
    """`text` with every ``[A-Za-z_]\\w*`` identifier token replaced by `repl` —
    byte-equivalent to ``re.sub(r'[A-Za-z_]\\w*', repl, text)`` (leading digits of a
    run are NOT part of the match, so they survive)."""
    out: list[str] = []
    last = 0
    for s, e in _word_spans(text):
        p = _ident_token_start(text, s, e)
        if p < e:
            out.append(text[last:p])
            out.append(repl)
            last = e
    out.append(text[last:])
    return "".join(out)


def _is_mixed_case_ident(w: str) -> bool:
    """True iff `w` (a maximal word-span) matches one of the two alternatives of
    ``[A-Z][a-zA-Z0-9]{1,}|[a-z][a-z0-9_]{2,}[a-z0-9]``: an ASCII CamelCase name
    (upper-led, ≥2, no underscore) OR a lower_snake name (lower-led, ≥4, ASCII
    ``[a-z0-9_]`` only, ending on ``[a-z0-9]``)."""
    n = len(w)
    alt1 = n >= 2 and "A" <= w[0] <= "Z" and all(
        ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") for c in w)
    alt2 = n >= 4 and "a" <= w[0] <= "z" and (("a" <= w[-1] <= "z") or ("0" <= w[-1] <= "9")) and all(
        ("a" <= c <= "z") or ("0" <= c <= "9") or c == "_" for c in w)
    return alt1 or alt2


def mixed_case_identifiers(text: str) -> set[str]:
    """CamelCase or lower_snake identifiers in `text` — byte-equivalent to
    ``set(re.findall(r'\\b([A-Z][a-zA-Z0-9]{1,}|[a-z][a-z0-9_]{2,}[a-z0-9])\\b', text))``.
    Each ``\\b…\\b`` match is exactly a maximal word-span (underscores are word chars,
    so the regex can only match a whole span), classified by `_is_mixed_case_ident`."""
    return {w for s, e in _word_spans(text) if _is_mixed_case_ident(w := text[s:e])}


def fn_reference_counts(text: str) -> dict[str, int]:
    """Map each `fn <name>` declaration's name to how many times that name word
    occurs anywhere in `text` — a usage-frequency map for leaf-first ordering.

    Byte-equivalent to the old ``re.finditer(r"\\bfn\\s+([A-Za-z_]\\w*)")`` +
    per-name ``re.findall(r"\\b<name>\\b")``: a name follows `fn` only across
    pure whitespace (so `fn(i32)` pointer types / `fn /*c*/ x` never match), and
    each name is counted across the whole text in first-declaration order."""
    spans = _word_spans(text)
    words = [text[s:e] for s, e in spans]
    freq = Counter(words)
    counts: dict[str, int] = {}
    for idx, w in enumerate(words):
        if w != "fn" or idx + 1 >= len(words):
            continue
        gap = text[spans[idx][1]:spans[idx + 1][0]]
        if not gap or not gap.isspace():
            continue  # `\s+`: name must follow `fn` across one-or-more whitespace
        name = words[idx + 1]
        # `fn <name>`: name is a letter/underscore-led identifier (`[A-Za-z_]\w*`).
        if (name[0].isalpha() or name[0] == "_") and name not in counts:
            counts[name] = freq[name]
    return counts


def contains_fn_decl(text: str) -> bool:
    """True iff `text` contains a `fn <word>` declaration (``\\bfn\\s+\\w+``):
    an `fn` token followed across one-or-more whitespace by a word char."""
    spans = _word_spans(text)
    for idx, (s, e) in enumerate(spans):
        if text[s:e] != "fn" or idx + 1 >= len(spans):
            continue
        gap = text[e:spans[idx + 1][0]]
        if gap and gap.isspace():
            return True
    return False


def fn_decl_names(text: str) -> set[str]:
    """Names declared as `fn <name>` immediately before a `<` or `(` — byte-equivalent
    to ``re.findall(r'\\bfn\\s+([A-Za-z_]\\w*)\\s*[<(]')``. `fn`, whitespace, an
    ASCII-led identifier, optional whitespace, then `<` or `(`."""
    spans = _word_spans(text)
    n = len(text)
    out: set[str] = set()
    for idx, (s, e) in enumerate(spans):
        if text[s:e] != "fn" or idx + 1 >= len(spans):
            continue
        ns, ne = spans[idx + 1]
        gap = text[e:ns]
        if not (gap and gap.isspace()):
            continue
        name = text[ns:ne]
        if not _is_ascii_ident_start(name[0]):
            continue
        j = ne
        while j < n and text[j].isspace():
            j += 1
        if j < n and text[j] in "<(":
            out.add(name)
    return out


def first_fn_decl_name(text: str) -> str | None:
    """Name of the leftmost `fn <name>` declaration (``re.search(r'\\bfn\\s+([A-Za-z_]\\w*)')``),
    or None. `fn`, whitespace, an ASCII-led identifier."""
    spans = _word_spans(text)
    for idx, (s, e) in enumerate(spans):
        if text[s:e] != "fn" or idx + 1 >= len(spans):
            continue
        ns, ne = spans[idx + 1]
        gap = text[e:ns]
        if not (gap and gap.isspace()):
            continue
        name = text[ns:ne]
        if _is_ascii_ident_start(name[0]):
            return name
    return None


def _consume_opt_pub(text: str, i: int) -> int:
    """Emulate the optional ``(?:pub(?:\\([^)]*\\))?\\s+)?`` group: consume `pub`, an
    optional ``(restriction)`` (no nested ``)``), then the required ``\\s+``. Returns
    `i` unchanged when the trailing whitespace is absent — so `pub(crate) fn` and
    `pub fn` consume through the gap, but `public`/`pub)`/`pub (x)` (space before the
    paren) backtrack to no-pub, exactly as the regex's optional group does."""
    n = len(text)
    if not text.startswith("pub", i):
        return i
    base = i + 3
    if base < n and text[base] == "(":            # greedy: try `(restriction)` first
        cp = text.find(")", base + 1)             # `[^)]*` then `\)`
        if cp != -1:
            k = cp + 1
            if k < n and text[k].isspace():        # `\s+`
                while k < n and text[k].isspace():
                    k += 1
                return k
    if base < n and text[base].isspace():          # paren-absent: `\s+` right after `pub`
        k = base
        while k < n and text[k].isspace():
            k += 1
        return k
    return i


def fn_names_with_body(text: str) -> set[str]:
    """Names of every line-leading `fn` declaration that reaches a body ``{`` —
    byte-equivalent to the MULTILINE finditer of
    ``(?:^|\\n)[ \\t]*(?:pub(?:\\([^)]*\\))?\\s+)?(?:async\\s+)?(?:unsafe\\s+)?(?:const\\s+)?fn\\s+([A-Za-z_]\\w*)[^{]*\\{``
    (the group_fill stub-skip scan). Matches begin at a line lead, allow the
    `pub/async/unsafe/const` modifier run, then `fn <name>` up to the first ``{``;
    non-overlapping, so a brace-less decl swallows a following one just as the regex
    did (`[^{]*` spans newlines)."""
    n = len(text)
    out: set[str] = set()
    starts = [0] + [k + 1 for k, c in enumerate(text) if c == "\n"]  # `(?:^|\n)` line leads
    barrier = 0                                                       # finditer non-overlap
    for bol in starts:
        if bol < barrier:
            continue
        i = bol
        while i < n and text[i] in " \t":          # `[ \t]*`
            i += 1
        i = _consume_opt_pub(text, i)
        i = _consume_opt_kw_ws(text, i, "async")
        i = _consume_opt_kw_ws(text, i, "unsafe")
        i = _consume_opt_kw_ws(text, i, "const")
        if not text.startswith("fn", i):
            continue
        j = i + 2
        if j >= n or not text[j].isspace():          # `fn\s+`
            continue
        while j < n and text[j].isspace():
            j += 1
        if j >= n or not _is_ascii_ident_start(text[j]):  # `[A-Za-z_]`
            continue
        ns = j
        j += 1
        while j < n and (text[j].isalnum() or text[j] == "_"):  # `\w*`
            j += 1
        name = text[ns:j]
        while j < n and text[j] != "{":              # `[^{]*`
            j += 1
        if j >= n:                                    # `\{` never reached → no match
            continue
        out.add(name)
        barrier = j + 1
    return out


def is_fn_decl_line(line: str) -> bool:
    """True iff `line` leads a fn declaration — byte-equivalent to
    ``re.match(r'^\\s*(?:pub\\s+)?(?:async\\s+)?(?:unsafe\\s+)?fn\\s+\\w', line)``:
    leading whitespace, optional `pub`/`async`/`unsafe` (each ``\\s+``-terminated),
    then `fn`, ``\\s+``, and a word char. (Like `is_fn_signature_line` but with the
    `unsafe` modifier and the trailing ``\\w`` requirement.)"""
    n = len(line)
    i = 0
    while i < n and line[i].isspace():               # `^\s*`
        i += 1
    i = _consume_opt_kw_ws(line, i, "pub")
    i = _consume_opt_kw_ws(line, i, "async")
    i = _consume_opt_kw_ws(line, i, "unsafe")
    if not line.startswith("fn", i):
        return False
    j = i + 2
    if j >= n or not line[j].isspace():              # `fn\s+`
        return False
    while j < n and line[j].isspace():
        j += 1
    return j < n and (line[j].isalnum() or line[j] == "_")  # `\w`


def test_fn_names(text: str) -> list[str]:
    """Names of fns declared on the line immediately after a ``#[test]`` attribute,
    in source order — byte-equivalent to the finditer of
    ``#\\s*\\[\\s*test\\s*\\][^\\n]*\\n[^\\n]*?fn\\s+(\\w+)\\s*\\(``. A ``#[test]`` attr
    (flexible inter-token whitespace, may span a newline before ``[``), then on the
    NEXT line the first ``fn <name> (`` (lazy ``[^\\n]*?``); non-overlapping."""
    out: list[str] = []
    i = 0
    while True:
        h = text.find("#", i)
        if h < 0:
            break
        m = _match_test_attr_fn(text, h)
        if m is None:
            i = h + 1
            continue
        name, end = m
        out.append(name)
        i = end
    return out


def _match_test_attr_fn(text: str, h: int) -> tuple[str, int] | None:
    """Try the ``#[test]`` → next-line ``fn <name>(`` match starting at the `#` at
    `h`; return `(name, end_offset)` or None. Helper for `test_fn_names`."""
    n = len(text)
    p = h + 1
    while p < n and text[p].isspace():               # `\s*`
        p += 1
    if p >= n or text[p] != "[":
        return None
    p += 1
    while p < n and text[p].isspace():               # `\s*`
        p += 1
    if not text.startswith("test", p):
        return None
    p += 4
    while p < n and text[p].isspace():               # `\s*`
        p += 1
    if p >= n or text[p] != "]":
        return None
    p += 1
    nl = text.find("\n", p)                           # `[^\n]*\n`
    if nl < 0:
        return None
    ls = nl + 1
    le = text.find("\n", ls)
    line_end = n if le < 0 else le                    # `fn` must start on this next line
    k = ls
    while True:
        f = text.find("fn", k)                        # lazy `[^\n]*?fn` (line-bound)
        if f < 0 or f >= line_end:
            return None
        j = f + 2                                     # tail `\s+(\w+)\s*\(` may cross newlines
        if j < n and text[j].isspace():               # `\s+`
            while j < n and text[j].isspace():
                j += 1
            ns = j
            while j < n and (text[j].isalnum() or text[j] == "_"):  # `(\w+)`
                j += 1
            if j > ns:
                t = j
                while t < n and text[t].isspace():    # `\s*`
                    t += 1
                if t < n and text[t] == "(":          # `\(`
                    return text[ns:j], t + 1
        k = f + 1


def use_symbol_paths(text: str) -> list[str]:
    """Captured paths of every ``\\buse\\s+([\\w:]+(?:::\\{[^}]+\\})?)\\s*;`` — the
    group(1) of each `use foo::bar;` / `use foo::{A, B};` statement, in source
    order. Emulates the regex's greedy `[\\w:]+` + optional `::{…}` backtrack so
    the brace-group form captures `foo::{A, B}` rather than `foo::`."""
    spans = _word_spans(text)
    n = len(text)
    out: list[str] = []
    for s, e in spans:
        if text[s:e] != "use":
            continue
        q = e  # `\s+` after `use`
        while q < n and text[q].isspace():
            q += 1
        if q == e:
            continue
        p = q  # maximal `[\w:]+`
        while p < n and (text[p].isalnum() or text[p] == "_" or text[p] == ":"):
            p += 1
        if p == q:
            continue
        for k in range(p, q, -1):  # backtrack the `[\w:]+` end, greedy = largest first
            if text[k:k + 2] == "::" and k + 2 < n and text[k + 2] == "{":
                cb = text.find("}", k + 3)
                if cb > k + 3:  # `[^}]+` requires ≥1 char
                    j = cb + 1
                    while j < n and text[j].isspace():
                        j += 1
                    if j < n and text[j] == ";":
                        out.append(text[q:cb + 1])
                        break
            j = k  # optional group absent: `\s*;`
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == ";":
                out.append(text[q:k])
                break
    return out


def attr_call_args(text: str, name: str) -> list[str]:
    """Inner argument string of every ``#[<name>(...)]`` — group(1) of
    ``#\\[<name>\\(([^)]+)\\)`` (literal `name`, args up to the first `)`, ≥1 char),
    in source order. Used to scan tests for `#[derive(...)]` / helper-attr usage."""
    needle = "#[" + name + "("
    width = len(needle)
    out: list[str] = []
    i = 0
    while True:
        j = text.find(needle, i)
        if j == -1:
            break
        k = text.find(")", j + width)
        if k == -1:
            i = j + width
            continue
        inner = text[j + width:k]
        if inner:  # `[^)]+` requires ≥1 char
            out.append(inner)
            i = k + 1
        else:
            i = j + width
    return out


# ---------------------------------------------------------------------------
# Identifier extraction for text analysis (requirement/genesis documents)
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "not", "use", "let", "mut", "pub", "fn",
    "mod", "impl", "struct", "enum", "trait", "type", "this", "that",
    "with", "from", "into", "self", "true", "false", "none", "some",
    "return", "match", "where", "else", "then", "each", "when", "which",
    "must", "should", "will", "can", "all", "any", "one", "new", "get",
    "set", "add", "run", "has", "its", "if", "in", "of", "to", "at",
    "as", "is", "it", "on", "do", "no", "be", "by", "or", "an", "a",
    "always", "never", "only", "also", "both", "either", "every",
    "given", "used", "list", "item", "value", "values", "name", "names",
    "error", "result", "output", "input", "string", "number", "data",
    "file", "path", "field", "method", "function", "module",
    "crate", "test", "tests", "assert", "check", "build", "code",
})

_META_PHRASES: tuple[str, ...] = (
    "this document describes",
    "functional requirements are",
    "the pipeline",
    "this requirement",
    "the following requirements",
    "requirements document",
    "requirement specification",
    "the purpose of this",
)


def extract_identifiers(text: str) -> set[str]:
    """CamelCase + snake_case identifiers from `text`, filtered by stop-words."""
    return {w for w in mixed_case_identifiers(text) if w.lower() not in _STOP_WORDS}


def use_path_requirement_gaps(
    reqs_text: str, test_files: list[tuple[str, str]],
) -> tuple[int, list[str]]:
    """Static TASK-1.5 audit (pure): every `use` symbol path in the test files
    whose LEAF identifier (last `::` segment, alias-stripped) does not appear as
    a whole word anywhere in the requirements text. Returns
    (total_use_paths_found, sorted_gaps). std/core/alloc/super/self/crate
    prefixes are never gaps. Requirements docs name symbols under module
    headings (`## Module: \\`cap\\`` + bare fn names), never as full
    `module::name` paths — so the match unit is the leaf word, with the full
    path accepted too."""
    SKIP_PREFIXES = ('std::', 'core::', 'alloc::', 'super::', 'self::', 'crate::')
    found: set[str] = set()
    for _rel, content in test_files:
        # use foo::bar::{A, B, C};  or  use foo::bar::Baz;
        for raw in use_symbol_paths(content):
            if '::' not in raw:
                continue
            if '::{' in raw:
                prefix, items_str = raw.split('::{', 1)
                for item in items_str.rstrip('}').split(','):
                    item = item.strip()
                    if item:
                        found.add(f"{prefix}::{item}")
            else:
                found.add(raw)
    req_words = {reqs_text[s:e] for s, e in _word_spans(reqs_text)}

    def _leaf(sym: str) -> str:
        leaf = sym.rsplit('::', 1)[-1].strip()
        if ' as ' in leaf:  # brace-group alias: `use m::{Foo as F};`
            leaf = leaf.split(' as ', 1)[0].strip()
        return leaf

    gaps = [
        sym for sym in sorted(found)
        if not any(sym.startswith(p) for p in SKIP_PREFIXES)
        and (leaf := _leaf(sym)) not in ('*', 'self')
        and leaf not in req_words
        and sym not in reqs_text
    ]
    return len(found), gaps


def canon_fn_name(name: str) -> str:
    """The bare fn identifier from a model-returned ``name`` attribute.

    A Rust identifier can never contain ``:``, so a colon means the name
    arrived PATH-QUALIFIED (``Error: root_cause``, ``Error::root_cause``,
    ``impl ErrorImpl: provide``) and the fn identity is the last segment. That
    is not an ambiguous answer — the container travels separately in the
    ``block`` attribute — so looking the qualified string up as a fn name and
    dropping the body on `fn_not_found` discards work the model got right.
    Returns the input unchanged when the tail is not an identifier, so a shape
    this rule does not understand is never made worse."""
    s = name.strip()
    if ":" not in s:
        return s
    tail = s.rsplit(":", 1)[1].strip()
    if not tail or not _is_ascii_ident_start(tail[0]):
        return s
    if not all(c.isalnum() or c == "_" for c in tail):
        return s
    return tail


def canon_container(container: str) -> str:
    """Canonical container identity. The extractor's fn records, the impl-block
    manifest, and model-returned ``block`` attributes render the same impl
    differently (token-spaced ``impl Context < T , E > for Result < T , E >``
    vs pretty-printed ``impl<T, E> Context<T, E> for Result<T, E>\\nwhere\\n
    E: ...`` vs the model's single-line ``impl<T, E> Context<T, E> for
    Result<T, E>``). Reduce all to ``<Trait>for<SelfTy>``: drop a leading
    impl-generics list, drop the where-clause, drop all whitespace. Keys the
    retrospectives store and the per-impl body routing (debt #43)."""
    s = " ".join(container.split())
    if s.startswith("impl"):
        s = s[4:].lstrip()
        if s.startswith("<"):
            depth = 0
            for i, ch in enumerate(s):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                    if depth == 0:
                        s = s[i + 1:]
                        break
    if s.endswith(" where"):
        s = s[: -len(" where")]
    s = s.split(" where ")[0]
    return "".join(s.split())
