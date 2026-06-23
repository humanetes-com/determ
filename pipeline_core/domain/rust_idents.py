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
