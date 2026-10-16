"""Rust identifier tokenisation — pure domain (source-regex→structured).

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
from dataclasses import dataclass


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

    Inherent impls (``impl Foo``), trait declarations (``trait Foo``), and free
    fns (empty container) are False."""
    return impl_trait_name(container) is not None


def _is_ascii_ident_char(c: str) -> bool:
    """True iff `c` may appear INSIDE a Rust identifier — the word boundary
    used to tell the keyword `for` from `for_each`/`before`."""
    return c == "_" or c.isalnum()


def canon_token_text(text: str) -> str:
    """Rust text in its printer-insensitive comparable form: whitespace runs
    collapsed, then every space dropped unless it separates two identifier
    characters.

    Two printers render the same Rust: the extractor's pretty-printer is
    token-spaced (`Own < ErrorImpl >`, `impl Context < T , E > for
    Result < T , E >`) while source-verbatim text is not, and any identity
    comparison across that seam splits on formatting unless both sides pass
    through this. Instances of the class: field-type matching returned () on
    every file-first prompt for five sweeps (run 55484-84c991f1, 2026-08-22,
    `installable_fns._canon`), and the peel's per-body implication refused
    `none_implicated` on its first live compile failure because the
    diagnostics resolved to extractor-shaped container keys while the landed
    bodies carried source-shaped ones (run `264297-dd681686`, 2026-08-24).
    `dyn StdError + Send` keeps its word-separating spaces; everything else
    loses them on both sides."""
    t = " ".join(text.split())
    out: list[str] = []
    for i, ch in enumerate(t):
        if ch == " " and not (
                i > 0 and i + 1 < len(t)
                and _is_ascii_ident_char(t[i - 1])
                and _is_ascii_ident_char(t[i + 1])):
            continue
        out.append(ch)
    return "".join(out)


def _skip_generics(s: str) -> str:
    """`s` past a leading balanced ``<...>`` list (unchanged when absent)."""
    if not s.startswith("<"):
        return s
    depth = 0
    for i, ch in enumerate(s):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return s[i + 1:].lstrip()
    return s


def _bare_trait_path(path: str) -> str:
    """Last segment of a trait path, generic args dropped: ``crate::kind::
    AdhocKind`` and ``Context<T, E>`` → ``AdhocKind`` / ``Context``. An impl may
    name a trait through a path while its declaration in the same file is bare,
    so only the final segment carries comparable identity."""
    head = path.split("<", 1)[0]
    return head.rsplit("::", 1)[-1].strip()


def impl_trait_name(container: str) -> str | None:
    """Bare trait name implemented by a trait-impl header, else None.

    ``impl<T> AdhocKind for &T where T: Display`` → ``AdhocKind``;
    ``impl<T, E> Context<T, E> for Result<T, E>`` → ``Context``. Used both to
    decide that the trait owns the member signatures (E0276) and to route a body
    the model tagged with an impl header to the trait that declares the member —
    an empty ``impl Trait for Ty {}`` holds no body, so the only place such a
    body can live is the trait's own default method.

    Container strings arrive as the syn enumerator token stream
    (``impl < T > AdhocKind for & T``), as a pretty-printed header with a
    newline where-clause, or as the model's compact single line
    (``impl<T> AdhocKind for &T``) — all three must read the same. The trait
    ends at the first whole-word ``for`` at generic depth 0 that does not
    introduce an HRTB (``for<'a>`` compact, ``for < 'a >`` spaced)."""
    s = " ".join(_strip_outer_attrs(_unescape_layout(container)).split())
    if not (s == "impl" or s.startswith("impl ") or s.startswith("impl<")):
        return None
    s = _skip_generics(s[4:].lstrip())
    depth = 0
    for i, ch in enumerate(s):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif (depth == 0 and s.startswith("for", i)
              and (i == 0 or not _is_ascii_ident_char(s[i - 1]))
              and (i + 3 >= len(s) or not _is_ascii_ident_char(s[i + 3]))):
            rest = s[i + 3:].lstrip()
            if rest.startswith("<"):        # HRTB, not the trait/type separator
                continue
            return _bare_trait_path(s[:i].strip()) or None
    return None


def mod_container_ident(container: str) -> str | None:
    """Bare ident of an extractor ``mod name`` container, else None.

    Inline-mod fns are recorded with ``container = "mod {ident}"`` — the
    enumerator pushes that string onto ``container_stack`` (``enumerate.rs``).
    ``mod __private`` → ``__private``. Nested mods keep the innermost ident
    (the stack's last entry). Not a path compare: a model tag
    ``impl crate::__private`` is matched by :func:`impl_head_name`'s last
    segment against this ident. Empty, ``impl …``, and ``trait …`` return
    None so an inherent header cannot land on an impl member by this rule.
    """
    s = " ".join(container.split())
    if not s.startswith("mod "):
        return None
    rest = s[4:].strip()
    if not rest:
        return None
    return rest.rsplit("::", 1)[-1] or None


def impl_head_name(container: str) -> str | None:
    """Bare name an ``impl`` header carries when it names only ONE path.

    ``impl AdhocKind`` → ``AdhocKind``; ``#[cfg(…)]\\nimpl BoxedKind`` →
    ``BoxedKind``; ``impl<T> Foo<T>`` → ``Foo``. None when the header has a
    ``for`` separator (that is `impl_trait_name`'s job) or names no path.

    In Rust this form is an INHERENT impl on the named type, so it must never
    be read as "implements that trait" on its own. Callers add the matching
    declaration: the trait-default routing tier requires a trait of that
    name in the same file that declares the member; member-identity requires
    a unique ``mod {ident}`` container whose ident equals this last segment.
    The model abbreviates ``impl<T> AdhocKind for &T`` to ``impl AdhocKind`` when it tags
    a returned body, and a tag that names the trait unambiguously is an answer,
    not a miss."""
    s = " ".join(_strip_outer_attrs(_unescape_layout(container)).split())
    if not (s.startswith("impl ") or s.startswith("impl<")):
        return None
    if impl_trait_name(s) is not None:
        return None
    head = _skip_generics(s[4:].lstrip()).strip().split(" ", 1)[0]
    return _bare_trait_path(head) or None


_TRAIT_DECL_MODIFIERS = frozenset({"unsafe", "auto", "default"})


def trait_def_name(container: str) -> str | None:
    """Bare name of a trait DECLARATION container, else None.

    ``pub trait AdhocKind: Sized`` → ``AdhocKind``; ``trait Kind<T>`` →
    ``Kind``. A trait declaration hosts its members' DEFAULT bodies, so it is a
    splice target in its own right — distinct from ``impl Trait for Ty``, which
    only overrides them."""
    s = " ".join(_strip_outer_attrs(_unescape_layout(container)).split())
    toks = s.split()
    if not toks or toks[0] == "impl":
        return None
    for i, tok in enumerate(toks):
        if tok != "trait":
            # Visibility (`pub`, `pub(crate)`, spaced `pub ( crate )`) and the
            # declaration modifiers are all that may precede `trait`; anything
            # else means this is not a trait declaration header.
            if not (tok.startswith("pub") or tok in _TRAIT_DECL_MODIFIERS
                    or tok in {"(", ")", "crate", "in", "self", "super"}
                    or tok.endswith(")")):
                return None
            continue
        if i + 1 >= len(toks):
            return None
        name = toks[i + 1].split("<", 1)[0].split(":", 1)[0].strip()
        return name or None
    return None


def signature_fixed_by_trait(container: str) -> bool:
    """True iff the trait — not the member — owns the signature of a fn in
    `container`, so a model-proposed where-clause must be dropped (E0276).

    Holds for a trait IMPL (``impl Display for X``) and equally for the trait
    DECLARATION itself (``trait AdhocKind``): tightening the bounds of a default
    method rewrites the contract every implementor is checked against, which the
    frozen public API forbids just as it forbids tightening an impl member."""
    return is_trait_impl_container(container) or trait_def_name(container) is not None


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


def impl_self_type(container: str) -> str | None:
    """Self/target type name of an impl, generics stripped, or ``None``.

    Accepts BOTH renderings the pipeline carries: the raw signature
    (``impl Trait for X<…>``, ``impl X<…>`` inherent) and the canonical
    whitespace-free key form produced by :func:`canon_container`
    (``TraitforX<…>``, ``DebugforErrorImpl<E>``) — the retrospectives store keys
    on the latter, where ``for`` has no surrounding whitespace to split on.
    Raw text still has the ``for`` KEYWORD's word boundaries, so it is split
    exactly there (:func:`_raw_for_tail`). The canon form erased them, and a
    bare substring scan mis-cuts inside idents that merely contain ``for``
    (``Platform`` → ``m``): a canon ``for`` at angle depth 0 counts as the
    separator only when what follows can BEGIN a self type — ``&``/``*``/
    ``(``/``[``, an upper- or underscore-led ident, a primitive (``u8``,
    ``str`` …) or a glued ``dyn``/``mut`` head (``fordynAny``) — never the
    lowercase tail of the word it sits in (``Plat·for·m``). The LAST such
    ``for`` wins: a trait name ending in ``…for`` glues a false candidate to
    the left of the real separator, never to its right."""
    words = container.split()
    if len(words) > 1:
        tail = _raw_for_tail(words)
        if tail is not None:
            joined = "".join(_strip_lifetimes(tail).split())
            return _leading_type_ident(_drop_orphan_plus(joined)) or None
    s = canon_container(container)
    depth = 0
    cut = -1
    for i, ch in enumerate(s):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif (depth == 0 and s.startswith("for", i)
              and _self_type_head(s[i + 3:])):
            cut = i + 3
    if cut != -1:
        s = s[cut:]
    return _leading_type_ident(s) or None


def _raw_for_tail(words: list[str]) -> str | None:
    """Tokens after the ``for`` keyword of a whitespace-rendered impl header,
    ``None`` when it has none (inherent impl). Word-exact — an ident that
    merely CONTAINS ``for`` (``Platform``) and a glued HRTB binder
    (``for<'a>``) are not the keyword — and angle-depth aware, so a
    token-spaced binder (``for < 'a >``) inside bounds cannot split; the scan
    stops at a depth-0 ``where``, past which only bounds live."""
    depth = 0
    cut: int | None = None
    stop = len(words)
    for k, w in enumerate(words):
        if depth == 0:
            if w == "where":
                stop = k
                break
            if w == "for" and k + 1 < len(words):
                cut = k + 1
        for i, ch in enumerate(w):
            if ch == "<":
                depth += 1
            elif ch == ">" and depth and (i == 0 or w[i - 1] != "-"):
                depth -= 1
    return " ".join(words[cut:stop]) if cut is not None else None


_PRIMITIVE_TYPES = frozenset({
    "bool", "char", "str",
    "u8", "u16", "u32", "u64", "u128", "usize",
    "i8", "i16", "i32", "i64", "i128", "isize", "f32", "f64",
})


def _self_type_head(rest: str) -> bool:
    """Whether ``rest`` (canon text after a candidate ``for``) can begin a
    self type. The letters continuing a camelCase word never can: they are
    lowercase and name no primitive (``Plat·for·m`` leaves ``m``)."""
    if not rest:
        return False
    if rest[0] in "&*([":
        return True
    j = 0
    while j < len(rest) and (rest[j].isalnum() or rest[j] == "_"):
        j += 1
    head = rest[:j]
    if not head or head[0].isdigit():
        return False
    return (head[0].isupper() or head[0] == "_"
            or head in _PRIMITIVE_TYPES
            or (head[:3] in ("dyn", "mut") and head[3:4].isupper()))


def _leading_type_ident(s: str) -> str:
    """Leading identifier of a type rendering (``Vec<T>`` -> ``Vec``, ``&Error``
    -> ``Error``); canon gluing is undone for ``dyn``/``mut`` heads
    (``&mutW`` -> ``W``, ``dynAny`` -> ``Any``). Empty when the head is not an
    identifier."""
    i = 0
    while i < len(s) and s[i] in "&*(":
        i += 1
    for kw in ("dyn", "mut"):
        if s.startswith(kw, i) and s[i + 3:i + 4].isupper():
            i += 3
    j = i
    while j < len(s) and (s[j].isalnum() or s[j] == "_"):
        j += 1
    name = s[i:j]
    return name if name and not name[0].isdigit() else ""


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


def is_compile_fail_test_source(text: str) -> bool:
    """True when a test file asserts that OTHER programs fail to compile.

    `tests/compiletest.rs` on the anyhow tree is the whole shape:

        #[test]
        fn ui() {
            let t = trybuild::TestCases::new();
            t.compile_fail("tests/ui/*.rs");
        }

    It asserts that every program under `tests/ui/` is REJECTED by rustc, with
    stderr byte-matching a committed `.stderr` file. No rewrite of any function
    body in the crate can change what rustc prints for those programs, so a
    repair loop that re-authors bodies cannot satisfy it — and on 2026-08-05 it
    was in the frontier, contributing `lib.rs::not` (from the `not(nightly)` in
    its own `#[rustversion::attr]`) at a ~2-minute model call.

    Recognised without running anything: the harness crate is named in the
    source. `trybuild` and `compiletest` are the two in use here; `compile_fail`
    alone is NOT enough, because a doctest attribute of that name says something
    different and lives in `///` comments.
    """
    return "trybuild" in text or "compiletest_rs" in text


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


def _strip_lifetimes(s: str) -> str:
    """Drop every lifetime token. Runs on the STILL-SPACED header: whitespace is
    the token boundary that ends a lifetime name, so stripping after a
    whitespace join would read ``&'a mut T`` as ``&'amut T`` and eat ``amut``.

    Two impls in one crate whose headers differ ONLY in lifetime names would be
    overlapping impls — coherence rejects that — so a lifetime carries no
    routing identity and eliding it cannot conflate two real containers. What it
    DOES recover is the model's rendering of one: ``Formatter<'_'>`` (a lifetime
    closed like a char literal, an easy slip since ``'_`` reads as an
    unterminated quote) and ``Foo<'a>`` vs ``Foo<'_>`` both reduce to the header
    the file declares instead of being dropped as an unknown block. A trailing
    ``'`` is consumed with the token precisely so the mangled form lands on the
    same string as the well-formed one."""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] != "'":
            out.append(s[i])
            i += 1
            continue
        i += 1
        while i < len(s) and (s[i].isalnum() or s[i] == "_"):
            i += 1
        if i < len(s) and s[i] == "'":
            i += 1
    return "".join(out)


def _drop_empty_generics(s: str) -> str:
    """Collapse a generic list left EMPTY by lifetime elision, and close a list
    the input never closed. Whitespace-free input.

    ``Chain<'_>`` elides to ``Chain<>``, which must equal ``Chain``. The canon
    also has to be TOTAL over truncated headers: a model tagging a body
    ``block="impl Default for Chain<'_"`` (the quoted attribute cut at the
    lifetime) canonicalised to ``DefaultforChain<`` against a target
    ``DefaultforChain<>`` — a one-character difference that dropped a correct,
    name-unique answer and escalated the unit to a budget-off retry. Both sides
    must reduce to the same string, so a dangling ``<`` is treated as the empty
    list it was on its way to being."""
    while "<>" in s:
        s = s.replace("<>", "")
    return s[:-1] if s.endswith("<") else s


def _drop_orphan_plus(s: str) -> str:
    """Clean up the ``+`` left behind by an elided trait-object bound, so
    ``Box<dyn E+Send+'static>`` and ``Box<dyn E+Send>`` agree — as they do in
    the type system, where the bound is the default. Whitespace-free input."""
    while "++" in s:
        s = s.replace("++", "+")
    s = s.replace("+>", ">")
    return s[:-1] if s.endswith("+") else s


def _unescape_layout(s: str) -> str:
    """Turn LITERAL ``\\n``/``\\r``/``\\t`` two-char escapes into real whitespace.

    A model that renders a multi-line impl header inside a single-line attribute
    writes the line breaks as backslash-n rather than as newlines. Nothing
    downstream treats that as space, so the where-clause split (which needs a
    real ``" where "``) never fires and the clause stays in the key: ``impl<T>
    Write for Indented<'_, T>\\nwhere\\n    T: Write,`` canonicalised to
    ``WriteforIndented<,T>\\nwhere\\nT:Write,`` and matched no container, so a
    correct body was dropped. A backslash cannot appear in a well-formed impl
    header, so rewriting these is unambiguous."""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in "nrt":
            out.append(" ")
            i += 2
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _strip_outer_attrs(s: str) -> str:
    """Drop leading outer attributes and comments from an impl header.

    The model is shown the file text, where the impl's ``#[cfg(...)]`` sits on
    the line directly above ``impl``, so it routinely echoes the attribute as
    part of the block header. The extractor's container strings carry no
    attribute, so the two render the same block differently and every body
    routed to it was dropped (anyhow: ``Debug``/``Display``/``StdError for
    BoxedError``, 3 correct bodies discarded in one run).

    Dropping the attribute cannot conflate two real containers HERE: two impls
    differing only by ``cfg`` are alternative spellings of one block, and a
    cluster activates exactly one cfg-set, so only one is ever live at a time.
    Bracket depth is tracked through string literals so ``#[doc = "]"]`` does
    not end the attribute early; Rust block comments nest, so those are counted
    too."""
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        if s.startswith("#[", i) or s.startswith("#![", i):
            j = s.index("[", i) + 1
            depth = 1
            while j < n and depth:
                ch = s[j]
                if ch == '"':
                    j += 1
                    while j < n and s[j] != '"':
                        j += 2 if s[j] == "\\" else 1
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                j += 1
            i = j
            continue
        if s.startswith("//", i):
            nl = s.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if s.startswith("/*", i):
            j = i + 2
            depth = 1
            while j < n and depth:
                if s.startswith("/*", j):
                    depth += 1
                    j += 2
                    continue
                if s.startswith("*/", j):
                    depth -= 1
                    j += 2
                    continue
                j += 1
            i = j
            continue
        break
    return s[i:]


def names_impl_or_trait(header: str) -> bool:
    """True iff `header` opens an ``impl`` or ``trait`` item — i.e. it is a
    statement about a CONTAINER, whatever else it gets wrong.

    Leading attributes and comments are stripped first, so a model that echoes
    the ``#[cfg(...)]`` line above the impl is still recognised. Everything
    else a model writes where a container header was asked for is not a
    container statement at all: a comment (``// free function``), or the fn's
    own signature line (``pub(crate) fn construct_from_std<E> where E: …``).
    Both shapes arrived in one src/error.rs reply on 2026-08-01."""
    s = " ".join(_strip_outer_attrs(_unescape_layout(header)).split())
    return (s in ("impl", "trait")
            or s.startswith(("impl ", "impl<", "trait ", "trait<")))


def canon_container(container: str) -> str:
    """Canonical container identity. The extractor's fn records, the impl-block
    manifest, and model-returned ``block`` attributes render the same impl
    differently (token-spaced ``impl Context < T , E > for Result < T , E >``
    vs pretty-printed ``impl<T, E> Context<T, E> for Result<T, E>\\nwhere\\n
    E: ...`` vs the model's single-line ``impl<T, E> Context<T, E> for
    Result<T, E>``). Reduce all to ``<Trait>for<SelfTy>``: normalise literal
    ``\\n`` escapes to real whitespace, drop leading outer attributes/comments,
    drop a leading impl-generics list, drop the where-clause, drop a trailing
    body brace, drop all whitespace, elide lifetimes. Keys the retrospectives
    store and the per-impl body routing.

    The trailing brace is the model copying the header off the file, where the
    ``{`` that opens the block sits on the header's own line (or the line
    below). ``#[cfg(...)]\\nimpl Debug for BoxedError\\n{`` canonicalised to
    ``DebugforBoxedError{`` against the tree's ``DebugforBoxedError`` and both
    `fmt` bodies for src/wrapper.rs were dropped on 2026-08-01. Only a TRAILING
    brace is removed, never a split at the first one: a const-generic self type
    (``impl Foo for Bar<{ N }>``) carries braces that are part of its identity
    and ends in ``>``."""
    return _canon_tail(_header_core(container))


@dataclass(frozen=True, slots=True)
class ContainerIdentity:
    """What a container header names: the trait path it implements, split
    into ``::`` segments (empty for an inherent impl, a trait declaration or
    a module), and the self type. Both in :func:`canon_container`'s
    normal form, so ``canon`` is that function's string."""

    trait: tuple[str, ...]
    self_ty: str
    canon: str

    def names(self, other: ContainerIdentity) -> bool:
        """True when the two headers name one impl of one file: the self
        types are equal and one trait path is a SEGMENT suffix of the other
        (``ext::StdError`` and ``StdError``; ``core::fmt::Debug`` and
        ``fmt::Debug``). Equal identities are the degenerate case.

        Segments, never characters: ``Error for E`` is a character suffix
        of ``StdError for E`` and names a different trait. An inherent
        header names only an inherent header."""
        if self.self_ty != other.self_ty:
            return False
        a, b = self.trait, other.trait
        if not a or not b:
            return a == b
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        return long[len(long) - len(short):] == short


def _header_core(container: str) -> str:
    """The single-spaced header past ``impl`` and its generics, with the
    where-clause and a trailing body brace dropped."""
    s = " ".join(_strip_outer_attrs(_unescape_layout(container)).split())
    if s.startswith("impl"):
        s = _skip_generics(s[4:].lstrip())
    while s.rstrip().endswith("{"):
        s = s.rstrip()[:-1]
    if s.endswith(" where"):
        s = s[: -len(" where")]
    return s.split(" where ")[0]


def _canon_tail(s: str) -> str:
    # Lifetime elision runs first. A list still open after that is a header
    # whose last `>` never arrived; closing it here is what every identity
    # comparison reads (`canon_container`, `container_identity`).
    return repair_truncated_generics(_drop_empty_generics(
        _drop_orphan_plus("".join(_strip_lifetimes(s).split()))))


def _path_segments(path: str) -> tuple[str, ...]:
    """``path`` split at ``::`` outside any generic list. Whitespace-free."""
    out: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(path):
        ch = path[i]
        if ch in "<(":
            depth += 1
        elif ch in ">)":
            depth -= 1
        elif depth == 0 and path.startswith("::", i):
            out.append(path[start:i])
            start = i + 2
            i += 1
        i += 1
    out.append(path[start:])
    return tuple(seg for seg in out if seg)


def container_identity(container: str) -> ContainerIdentity:
    """The header's identity (see :class:`ContainerIdentity`).

    A reply tags each body with the header it answers to, and a model that
    sees an impl inside ``mod ext`` writes the trait by the path it would
    use from outside: ``impl<E> ext::StdError for E where …`` for the file's
    ``impl<E> StdError for E``. The router compared the canonical strings
    byte for byte and dropped the body (run `3040-51eab48c`, 2026-10-02,
    `src/context.rs`, `raw_canon='ext::StdErrorforE'`). The separator is the
    one :func:`impl_trait_name` already reads: the first whole-word ``for``
    at generic depth 0 that does not open an HRTB."""
    core = _header_core(container)
    is_impl = " ".join(
        _strip_outer_attrs(_unescape_layout(container)).split()
    ).startswith("impl")
    canon = _canon_tail(core)
    if is_impl:
        depth = 0
        for i, ch in enumerate(core):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif (depth == 0 and core.startswith("for", i)
                  and (i == 0 or not _is_ascii_ident_char(core[i - 1]))
                  and (i + 3 >= len(core)
                       or not _is_ascii_ident_char(core[i + 3]))):
                rest = core[i + 3:].lstrip()
                if rest.startswith("<"):    # HRTB, not the separator
                    continue
                return ContainerIdentity(
                    trait=_path_segments(_canon_tail(core[:i])),
                    self_ty=_canon_tail(rest), canon=canon)
    return ContainerIdentity(trait=(), self_ty=canon, canon=canon)


def repair_truncated_generics(canon: str) -> str:
    """``canon`` with a model's mis-closed generic list repaired, or ``canon``
    itself when nothing is wrong with it.

    The file-first reply tags each body with the impl header it answers to,
    and the model closes the LAST generic list with the wrong bracket often
    enough to have dropped correct bodies in two consecutive batches of one
    run: ``block="impl<T, E> Context<T, E> for Result<T, E)"`` and ``impl<T>
    Context<T, Infallible> for Option<T)`` (run `3124988-ac6886ba`,
    2026-08-21, both `with_context` bodies, batches 2 and 3), and ``impl<C>
    StdError for ContextError<C, Error]`` (run `1246060-2339497b`, both
    `source` bodies). The router compared the canonical form byte-for-byte,
    matched nothing, and logged `block header does not match any impl in this
    file` — an answer dropped on a one-character slip.

    Only a TRAILING run of ``)`` / ``]`` is rewritten, and only while an
    angle bracket is still open at that point: ``Foofor(A,B)`` (a tuple self
    type, depth 0 at its ``)``) is identity and is returned unchanged. A list
    still open after that — no closer was written at all — is closed by
    appending the missing ``>``. A shorter argument list is a different
    type and is not rewritten into a longer one: ``Foo<A, B`` becomes
    ``Foo<A, B>``, which does not equal ``Foo<A, B, C>``."""
    depth = 0
    for ch in canon:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
    if depth <= 0:
        return canon
    out = list(canon)
    i = len(out) - 1
    while i >= 0 and depth > 0 and out[i] in ")]":
        out[i] = ">"
        depth -= 1
        i -= 1
    if depth > 0:
        out.append(">" * depth)
    return "".join(out)
