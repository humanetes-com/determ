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
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

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


def names_method_call(text: str, name: str) -> bool:
    """True when `name` appears in `text` in CALL position — `.name`, `::name`,
    `name(`, or a turbofished `name::<T>(` — rather than merely as a word.

    The notes filter needs "does this note advertise calling this method", and a
    bare whole-word test answers a different question the moment a method name is
    also an English word. Measured 2026-08-29 on the live anyhow tree: the crate
    declares `Bool::not` inside `mod __private::not` in `src/lib.rs`, so `not` is
    walled from every other file and became a hidden symbol; `contains_word`
    matched the ordinary prose "not" and would have dropped 3 of the 5 stored
    notes, of which only the `ext_context` one names an unreachable call. Prose
    can never look like a call, so call-position matching closes the collision for
    every English-colliding method the crate has (`is`, `new`, `from`, `context`,
    `source`, `read`, `extend` are all method names here too). A parenthesized
    English aside after the word (`is not (yet) implemented`) does look like
    `name(` and is dropped; none of the five stored notes on 2026-08-29 take that
    shape. Receiver / path position (`.name`, `::name`) does not require
    parentheses, so `use e.ext_context` and `Bool::not` count as advertising the call.

    Deliberately NOT a general "is this identifier used as code" test: a note that
    merely mentions a symbol in passing is left in the prompt, because the thing
    being prevented is an instruction to CALL something unreachable.
    """
    start = 0
    while (k := text.find(name, start)) >= 0:
        start = k + 1
        before = text[k - 1] if k > 0 else ""
        if before in _IDENT_CHARS:
            continue
        rest = text[k + len(name):]
        after = rest[0] if rest else ""
        if after in _IDENT_CHARS:
            continue
        # Receiver / path position: `.name`, `::name`.
        if before == "." or text[max(0, k - 2):k] == "::":
            return True
        # Call position: `name(` or `name::<T>(`, whitespace tolerated.
        tail = rest.lstrip()
        if tail.startswith("("):
            return True
        if tail.startswith("::<") and ">" in tail:
            if tail[tail.find(">") + 1:].lstrip().startswith("("):
                return True
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
    (regex-free; replaces `use (?:std|alloc)::[^;]*\\bSymbol\\b`-style patterns).
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
    """Locate the FIRST `[pub ]mod <name> {` opener (regex-free; mirrors
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
    (regex-free; mirrors `#\\[(?:\\w+::)*test\\]`)."""
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


def fn_name_on_line(line: str) -> str:
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
            name = fn_name_on_line(lines[j])
            if name:
                out.append(name)
            break
        i = j + 1
    return out


def attributed_fn_names(text: str, attr: str) -> list[str]:
    """Names of every fn carrying `#[<attr>]`, in source order — each fn once,
    however many times the attribute is repeated on it.

    :func:`count_attrs` counts attribute OCCURRENCES, and the two diverge the
    moment a splice leaves a duplicate behind. Live on
    `.skeleton-filled/stable/src/error.rs`: **30 `#[cold]` lines against 16
    annotated fns**, because 17 attribute runs hold a repeat (several are x3).
    The standing handover invariant "`#[cold]` = 30, must not grow" was
    therefore counting the duplication, not the annotation — it reads GREEN on
    a tree where real `#[cold]` fns were lost and RED on one that was merely
    cleaned. A guard that moves for the wrong reason in both directions is
    worse than no guard; this is what it should have counted.
    """
    want = "#[" + attr
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s.startswith(want) or (
                len(s) > len(want) and s[len(want)] in _IDENT_CHARS):
            i += 1
            continue
        j = i
        while j < len(lines):
            nxt = lines[j].strip()
            if j > i and not (not nxt or nxt.startswith(("//", "#"))):
                name = fn_name_on_line(lines[j])
                if name:
                    out.append(name)
                break
            j += 1
        i = j + 1
    return out


MAX_ATTR_LINES = 64
"""Ceiling on how many source lines one attribute may span. A guard against a
runaway scan on malformed input, not a property of Rust."""


def attr_bracket_delta(line: str) -> int:
    """Net `(`/`[` minus `)`/`]` for one line, ignoring bracket characters
    inside string literals (`#[path = "a(b).rs"]` must not skew the balance)."""
    depth = 0
    in_str = False
    escaped = False
    for ch in line:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
    return depth


@dataclass(frozen=True, slots=True)
class Attribute:
    """One `#[...]` attribute, however many source lines it occupies.

    `line` / `end_line` are its 0-based first and last lines (equal for the
    common one-line case) and `text` is the whole attribute joined into a single
    string, so two attributes are comparable whether or not the emitter chose to
    wrap them. The span is what lets a caller REMOVE an attribute, not merely
    count it."""

    line: int
    end_line: int
    text: str


def attribute_runs(text: str) -> list[list[Attribute]]:
    """Every contiguous attribute run, in source order.

    Attributes are read attribute-by-attribute, not line-by-line. The pipeline
    writes each `#[...]` on one line, but the Rust side re-emits these files
    through `prettyplease`, which wraps a long `#[allow(...)]` across ten. A
    line-at-a-time scan treats the wrapped attribute's continuation lines as the
    end of the run, so it sees two short runs where there is one long one — and
    two attributes it cannot compare where there are two it can.
    """
    out: list[list[Attribute]] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip().startswith("#["):
            i += 1
            continue
        run: list[Attribute] = []
        while i < n and lines[i].strip().startswith("#["):
            buf: list[str] = []
            depth = 0
            j = i
            while j < n and j - i < MAX_ATTR_LINES:
                stripped = lines[j].strip()
                buf.append(stripped)
                depth += attr_bracket_delta(stripped)
                if depth <= 0:
                    break
                j += 1
            else:
                # Unbalanced past the ceiling: take the opening line alone
                # rather than swallowing the rest of the file.
                buf, j = [lines[i].strip()], i
            run.append(Attribute(i, j, " ".join(buf)))
            i = j + 1
        out.append(run)
    return out


def duplicate_attribute_runs(text: str) -> list[tuple[int, str]]:
    """`(1-based line, the repeated attribute)` for every attribute RUN that
    contains the same attribute twice.

    A splice that re-attaches an item's attributes instead of replacing them
    accumulates them silently — rustc accepts repeated `cfg`/`cold`, so nothing
    ever fails. The 2026-07-27 repair (`scaffold_rewrite.DefReplacement` /
    `rust_scan.item_start` expanding the splice span back over the outer
    attributes) stopped the accumulation; this is the check that keeps it
    stopped, since the only symptom is a count nobody re-measures.
    """
    out: list[tuple[int, str]] = []
    for run in attribute_runs(text):
        seen: set[str] = set()
        for attr in run:
            if attr.text in seen:
                out.append((attr.line + 1, attr.text))
            seen.add(attr.text)
    return out


def drop_duplicate_attributes(text: str) -> str:
    """`text` with every repeated attribute removed from its run, first kept.

    The inverse of :func:`duplicate_attribute_runs`, for cleaning residue a
    since-fixed splice left behind. Deliberately conservative:

    * only whole attributes that are BYTE-identical after joining are removed,
      so nothing that differs in meaning — or merely in spelling — is touched;
    * the FIRST occurrence survives with its own lines and indentation intact,
      so the item keeps the attribute order it was authored with;
    * nothing outside an attribute run is read or rewritten.

    Repeated `cfg`/`cold` are idempotent to rustc, so this changes no
    compilation — it changes what a count of those attributes means.
    """
    lines = text.splitlines(keepends=True)
    drop: set[int] = set()
    for run in attribute_runs(text):
        seen: set[str] = set()
        for attr in run:
            if attr.text in seen:
                drop.update(range(attr.line, attr.end_line + 1))
            seen.add(attr.text)
    if not drop:
        return text
    return "".join(ln for k, ln in enumerate(lines) if k not in drop)


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


def declared_fn_names(text: str) -> frozenset[str]:
    """Names declared by ``fn`` items anywhere in ``text``, at any nesting depth.

    Same token scan as `item_names`, narrowed to the one keyword: the caller
    that needs this is asking "does this crate declare something CALLABLE by
    that name", for which a struct or const of the same name is not an answer.
    Rust keeps field names and fn names in separate namespaces, so a note may
    name a real field and still be prescribing a call that cannot resolve."""
    tokens = text.split()
    return frozenset(
        name for i, tok in enumerate(tokens[:-1]) if tok == "fn"
        for name in (_leading_ident(tokens[i + 1]),) if name)


def stubbed_fn_names(text: str) -> frozenset[str]:
    """Names of the fns in `text` whose body is nothing but the skeletoniser's
    hollow marker — `unimplemented!()`, in either the bare or the `::core::`
    spelling the skeletoniser writes.

    Answers "which of this file's fns are still holes" from the file text
    alone, at any nesting depth, with no extractor binary and no cluster
    context. That is what makes it usable at CONVICTION-WRITE time, where the
    only thing in hand is the snapshot the failed attempt compiled against: a
    retrospective that blames a sibling for being a stub can then record which
    siblings those were, and a later reader can see they have since filled.

    Comments are stripped before the test, because a reverted body is not bare.
    `make_failed_attempt_body` keeps the attempt and the cargo error beside the
    marker, so the live tree's hollow bodies run 20-40 lines of `//` before the
    `unimplemented!()` — matching only a bare body would report a freshly-failed
    fn as filled, which is the opposite of the truth.

    A bodyless declaration (a trait method's `fn f(&self);`, an `extern` block)
    is not a hole in this crate and is never reported.
    """
    mask = code_mask(text)
    n = len(text)
    out: set[str] = set()
    i = 0
    while i < n:
        if mask[i] or not _word_at(text, i, "fn"):
            i += 1
            continue
        j = i + 2
        while j < n and (mask[j] or text[j].isspace()):
            j += 1
        k = j
        while k < n and (text[k].isalnum() or text[k] == "_"):
            k += 1
        name = text[j:k]
        if not name:
            i += 2
            continue
        end = _skip_fn_item(text, mask, k)
        # `_skip_fn_item` stops ON a depth-0 `}` for a fn-POINTER type
        # (`object_drop: unsafe fn(Own<ErrorImpl>)` in a vtable struct is the
        # live case) — no body, nothing declared, and consuming it would walk
        # past the rest of the enclosing item.
        body_open = text.find("{", k, end)
        if body_open != -1 and not mask[body_open]:
            close = _matching_brace(text, mask, body_open)
            if close is not None and is_hollow_body(text[body_open + 1:close - 1]):
                out.add(name)
        i = max(end, i + 2)
    return frozenset(out)


def is_hollow_body(body: str) -> bool:
    """True when `body` carries no code but an `unimplemented!()`."""
    core = strip_line_block_comments(body).strip().rstrip(";").strip()
    return core in ("unimplemented!()", "::core::unimplemented!()")


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
    char scan."""
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


def body_without_own_closing_brace(body: str) -> str | None:
    """`body` minus the fn's OWN closing brace, or None if it does not carry one.

    A body written between an existing `{` and `}` must be brace-balanced. A
    reply that ends its answer with the fn's closing brace is short exactly one
    `{`, and splicing it verbatim closes the fn early: everything after the
    splice point shifts out one level and `top_level_item_names` changes, which
    is the `item_set_changed` refusal. Observed on nightly/src/error.rs
    2026-08-03 10:37 — all 17 bodies of one file-first reply ended `\\n    }`,
    every splice refused, the whole file-first attempt landed nothing and fell
    back to per-block. The adjacent reply for the same file 12 minutes earlier
    (10:25) closed no body and spliced 17 for 17, so the shape is a per-reply
    slip, not a property of the prompt.

    Repairs only the exact shape it can prove: masked brace balance exactly -1
    with that unmatched `}` the last brace-relevant character in the body.
    Comments and strings are not code, so `}  // done` is the same slip and is
    repaired; balance -2, or an unbalanced body ending in an expression, is
    left alone for the caller to refuse, because the point of the item-set
    fingerprint is that a body which cannot be shown to be balanced is not
    written."""
    mask = code_mask(body)
    depth = 0
    last_close = -1
    last_code = -1
    for i, ch in enumerate(body):
        if mask[i] or ch.isspace():
            continue
        last_code = i
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            last_close = i
    if depth != -1 or last_close < 0 or last_close != last_code:
        return None
    return (body[:last_close] + body[last_close + 1:]).rstrip()


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
    for depth, i, k in _iter_item_headers(text, mask, keyword, name):
        if depth != 0:
            continue
        end = _item_end(text, mask, k)
        if end is None:
            return None
        return item_start(text, mask, i), end
    return None


def locate_item_declaration(
    text: str, keyword: str, name: str,
) -> tuple[int, int] | None:
    """`[start, end)` of the ``<keyword> <name>`` declaration wherever it is
    written — including inside an inline ``mod`` block — or None.

    Module depth is a property of where a crate CHOSE to put an item, not of
    whether the item is part of the private surface: `anyhow` declares
    `trait StdError` and `trait Sealed` inside `mod ext`, and both are ordinary
    replaceable surface items. A locator that can only see depth 0 reports those
    as absent, which reads to every caller as "the live tree does not have it"
    — the propagation then fails MECHANICALLY, refunds its attempt, and re-fires
    on the identical bug next sweep until the mechanical cap blocks the
    component for the rest of the run.

    A depth-0 declaration still wins outright, so nothing that resolves today
    changes. Below that, the match must be UNIQUE: two same-named items in
    sibling modules are two different types, and a splice that slices the first
    one it happens to reach rewrites an item the caller never named. Ambiguity
    is reported as None — the caller's "could not locate" path is recoverable,
    editing the wrong declaration is not."""
    mask = code_mask(text)
    nested: list[tuple[int, int]] = []
    for depth, i, k in _iter_item_headers(text, mask, keyword, name):
        end = _item_end(text, mask, k)
        if end is None:
            continue
        span = (item_start(text, mask, i), end)
        if depth == 0:
            return span
        nested.append(span)
    return nested[0] if len(nested) == 1 else None


def without_test_fns(text: str, names: Iterable[str]) -> str | None:
    """`text` with each named `#[test]` fn removed whole: its attributes, the
    `///` doc lines directly above it, and the lines it occupies. None when a
    name is not a test fn of `text`, cannot be located uniquely, or the
    removal would take any other test with it.

    The span is the structural one `locate_item_declaration` gives (outer
    attributes included), widened to whole lines. Doc lines go with the item
    because a `///` left with nothing after it is a compile error; plain `//`
    comments stay, so a file-level marker comment above the first test is
    never removed. Every other test is checked to survive by name, which is
    what a caller withdrawing some tests and judging the rest relies on."""
    want = sorted(set(names))
    declared = attributed_test_fn_names(text)
    if not want or any(n not in declared for n in want):
        return None
    out = text
    for name in want:
        span = locate_item_declaration(out, "fn", name)
        if span is None:
            return None
        start, end = span
        line_start = out.rfind("\n", 0, start) + 1
        if out[line_start:start].strip():
            return None
        start = line_start
        while start > 0:
            prev = out.rfind("\n", 0, start - 1) + 1
            row = out[prev:start].strip()
            if not (row.startswith("///") and not row.startswith("////")):
                break
            start = prev
        nl = out.find("\n", end)
        if out[end:(len(out) if nl == -1 else nl)].strip():
            return None
        end = len(out) if nl == -1 else nl + 1
        out = out[:start] + out[end:]
    left = attributed_test_fn_names(out)
    if sorted(left) != sorted(n for n in declared if n not in want):
        return None
    return out


_MEMBER_KINDS = frozenset({"struct", "enum", "union"})


def _definition_terminator(
    text: str, mask: Sequence[bool],
) -> tuple[str, int] | None:
    """Where the FIRST item in `text` ends: `("{", i)` for a braced shape,
    `(";", i)` for a tuple or unit one, None if it ends nowhere.

    First, not last, because a stored definition is not always one item. The
    crate-wide layout registry holds `Error` as a struct plus `unsafe impl Send`
    plus `unsafe impl Sync` under one key, and a scan that runs to the end of
    the blob reads the last impl's `}` as the struct's own shape.

    `(`/`[` nesting is tracked because a generic default (`<E = ()>`) and an
    outer attribute both sit ahead of the body."""
    nest = 0
    for i, ch in enumerate(text):
        if mask[i]:
            continue
        if ch in "([":
            nest += 1
        elif ch in ")]":
            nest -= 1
        elif nest == 0 and ch == "{":
            return "{", i
        elif nest == 0 and ch == ";":
            return ";", i
    return None


def _definition_body_open(text: str, mask: Sequence[bool]) -> int | None:
    """Offset of the `{` opening a type definition's body, or None for a tuple
    or unit shape."""
    end = _definition_terminator(text, mask)
    return end[1] if end is not None and end[0] == "{" else None


def _definition_kind(text: str, mask: Sequence[bool], end: int) -> str:
    """`struct` / `enum` / `union` read off a definition's head, or ""."""
    i = 0
    while i < end:
        if mask[i] or text[i] not in _IDENT_START:
            i += 1
            continue
        j = i
        while j < end and not mask[j] and text[j] in _IDENT_CHARS:
            j += 1
        if text[i:j] in _MEMBER_KINDS:
            return text[i:j]
        i = j
    return ""


def declared_members(definition: str) -> frozenset[str]:
    """Every member name a struct / enum / union DEFINITION declares: its field
    names, and for an enum its variant names plus each variant's named fields.

    Tuple and unit shapes contribute nothing — they declare no names that a
    later definition could drop.

    Fields are read at brace depth 1 (depth 2 inside an enum variant) and only
    outside `(`/`[` nesting. That nesting rule is what keeps a NAMED fn-pointer
    parameter out: a vtable may be written `object_drop: unsafe fn(e:
    Own<ErrorImpl>)`, which declares the member `object_drop` and not a member
    `e`. Collecting `e` would invent a member for the next authored definition
    to "drop", and the caller's whole use of this function is to act on what a
    re-author dropped.

    A `:` followed by another `:` is a path separator rather than a field's type
    annotation — `vtable: &'static crate::error::ErrorVTable` declares one
    member, and `crate` is not it."""
    mask = code_mask(definition)
    brace = _definition_body_open(definition, mask)
    if brace is None:
        return frozenset()
    kind = _definition_kind(definition, mask, brace)
    if kind not in _MEMBER_KINDS:
        return frozenset()
    field_depth = 2 if kind == "enum" else 1
    out: set[str] = set()
    n = len(definition)
    depth = 0
    nest = 0
    i = brace
    while i < n:
        if mask[i]:
            i += 1
            continue
        ch = definition[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif ch in "([":
            nest += 1
        elif ch in ")]":
            nest -= 1
        elif nest == 0 and ch in _IDENT_START:
            j = i
            while j < n and not mask[j] and definition[j] in _IDENT_CHARS:
                j += 1
            k = j
            while k < n and definition[k] in " \t\r\n":
                k += 1
            annotated = (k < n and definition[k] == ":"
                         and not definition.startswith("::", k))
            is_field = depth == field_depth and annotated
            # An enum's variant name carries no type annotation — it is
            # followed by its own payload, a comma, a discriminant or the
            # closing brace.
            is_variant = (kind == "enum" and depth == 1 and not annotated
                          and k < n and definition[k] in "{(,=}")
            if is_field or is_variant:
                out.add(definition[i:j])
            i = j
            continue
        i += 1
    return frozenset(out)


def _tuple_arity(text: str, mask: Sequence[bool], semi: int) -> int:
    """Positional field count of the tuple shape terminated by the `;` at
    `semi` — the `(…)` group closing just before it.

    Found by walking back from that `;` rather than forward from the head,
    because a generic default carries its own parentheses: in `struct Foo<E =
    ()>(E);` the first group is the default and the storage is the second. The
    `;` is the caller's, not the end of the text — a registry entry may hold
    further items after this one."""
    close = -1
    for i in range(semi - 1, -1, -1):
        if mask[i] or text[i] in " \t\r\n":
            continue
        close = i
        break
    if close < 0 or text[close] != ")":
        return 0
    depth = 0
    for i in range(close, -1, -1):
        if mask[i]:
            continue
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                inner = text[i + 1:close]
                if not inner.strip():
                    return 0
                nest = 0
                fields = 1
                for k, ch in enumerate(inner):
                    if mask[i + 1 + k]:
                        continue
                    if ch in "([<":
                        nest += 1
                    elif ch in ")]>":
                        nest -= 1
                    elif ch == "," and nest == 0:
                        fields += 1
                return fields
    return 0


def declared_member_count(definition: str) -> int:
    """How many storage slots a definition declares — named members for a
    braced shape, positional fields for a tuple one.

    Counting the tuple form is the point. `struct Error { ptr: Own<ErrorImpl> }`
    rewritten as `struct Error(NonNull<ErrorImpl>)` drops the NAME `ptr` while
    keeping the storage, and a caller that reads only `declared_members` sees
    that as a type emptied to nothing. It is a re-spelling, and the bodies that
    said `.ptr` can say `.0`."""
    mask = code_mask(definition)
    end = _definition_terminator(definition, mask)
    if end is None:
        return 0
    if end[0] == "{":
        return len(declared_members(definition))
    return _tuple_arity(definition, mask, end[1])


def field_reads(text: str, names: Iterable[str]) -> dict[str, int]:
    """How many times each of `names` is read as a FIELD of something in
    `text` — a `.name` that is not a call.

    Excluding `.name(` is what separates a field from a method that happens to
    share its name. `anyhow`'s live tree writes both `err.backtrace()` (the
    public method on `Error`) and `(*this.as_ptr()).backtrace` (the field on
    `ErrorImpl`); counting the calls would report a field as load-bearing on the
    strength of a method that says nothing about the layout. Measured on the
    93-passing tree (2026-08-01), `backtrace` has 2 calls against 5 field reads,
    and every other member of that component has 0 calls.

    Names absent from `text` are reported as 0 rather than omitted, so a caller
    can tell "looked and found none" from "never asked"."""
    wanted = {n for n in names if n}
    found = dict.fromkeys(wanted, 0)
    if not wanted:
        return found
    mask = code_mask(text)
    n = len(text)
    i = 0
    while i < n:
        if mask[i] or text[i] != ".":
            i += 1
            continue
        if text.startswith("..", i):
            # A range or a struct-update base (`..vtable`, `..Default::default()`)
            # — the second dot would otherwise read as a field access on nothing.
            i += 2
            continue
        j = i + 1
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n or mask[j] or text[j] not in _IDENT_START:
            i += 1
            continue
        e = j
        while e < n and not mask[e] and text[e] in _IDENT_CHARS:
            e += 1
        word = text[j:e]
        k = e
        while k < n and text[k] in " \t\r\n":
            k += 1
        if word in wanted and not (k < n and text[k] == "("):
            found[word] += 1
        i = e
    return found


@dataclass(frozen=True, slots=True)
class TraitImplBlock:
    """One ``impl <Trait> for <Type>`` item. `header` is its identity — see
    `trait_impl_blocks` for what that does and does not compare."""

    header: str
    start: int
    end: int


def trait_impl_blocks(text: str) -> list[TraitImplBlock]:
    """Every ``impl <Trait> for <Type>`` block written at brace depth 0, in file
    order, spanning outer attributes and qualifiers through the closing brace.

    `header` is everything from the start of the item to the opening brace of
    its body — attributes, `unsafe`, generics, trait, self type — verbatim but
    for whitespace runs collapsed to one space. Two blocks with equal headers
    are the same impl written twice, which is E0119 and does not compile. That
    is what makes the list actionable: text about to be spliced into a file that
    already holds one of its impls can only build if the older copy goes.

    Verbatim includes comments and string literals. Blanking comments would be
    convenient and blanking strings comes with it in `code_mask`, which reduces
    `#[cfg(feature = "std")]` and `#[cfg(feature = "alloc")]` to one header —
    two impls that are gated apart and both compile. A comment inside an impl
    header instead costs a missed match, and a missed match is the E0119 the
    caller would have had regardless.

    The header is compared WHOLE, on purpose. A shorter identity — trait name
    and self type, the pair rustc's coherence rules ultimately reduce to — is
    wrong here because reducing them is a type-system judgement this scanner
    cannot make. Measured on the live `anyhow` tree (2026-08-01), it collapses
    `impl AsRef<dyn StdError + Send + Sync> for Error` onto `impl AsRef<dyn
    StdError> for Error`, all three `impl From<…> for Box<…>`, and the two
    cfg-gated `impl StdError for ContextError<…>` — nine distinct impls in two
    files, every one of which compiles today. Deleting a working impl is far
    worse than leaving a duplicate: the duplicate is a compile error the caller
    already had, the deletion is silent.

    Inherent impls (`impl Error { … }`) are excluded. A type may carry any
    number of them, so a second one is not evidence of anything.

    Depth 0 only. Coherence is crate-wide, so a duplicate written inside an
    inline `mod` is still E0119 and is still missed here — again, degrading to
    the compile error rather than to a wrong edit."""
    mask = code_mask(text)
    out: list[TraitImplBlock] = []
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
        if depth != 0 or text[i:j] != "impl":
            i = j
            continue
        brace = _impl_body_open(text, mask, j)
        end = None if brace is None else _matching_brace(text, mask, brace)
        start = item_start(text, mask, i)
        if end is not None and brace is not None and _is_trait_impl(
                _blank_comments(text, mask, j, brace)):
            out.append(TraitImplBlock(
                " ".join(text[start:brace].split()), start, end))
        i = j if end is None else end
    return out


def _blank_comments(
    text: str, mask: Sequence[bool], start: int, end: int,
) -> str:
    """`text[start:end]` with every commented or quoted byte blanked out.

    For SCANNING only — a `for` inside a doc comment must not make an inherent
    impl look like a trait impl. The identity that comes out of the same span is
    built from the raw bytes; see `trait_impl_blocks`."""
    return "".join(" " if mask[k] else text[k] for k in range(start, end))


def _impl_body_open(text: str, mask: Sequence[bool], start: int) -> int | None:
    """Offset of the `{` opening an impl body, scanning from just past `impl`.

    `(`/`[` nesting is tracked for the same reason `_item_end` tracks it: an
    associated const's array length or a `Fn(..)` bound sits in the header."""
    n = len(text)
    nest = 0
    i = start
    while i < n:
        if mask[i]:
            i += 1
            continue
        c = text[i]
        if c in "([":
            nest += 1
        elif c in ")]":
            nest -= 1
        elif nest == 0 and c == "{":
            return i
        elif nest == 0 and c == ";":
            return None
        i += 1
    return None


def _matching_brace(text: str, mask: Sequence[bool], open_at: int) -> int | None:
    """Offset just past the `}` closing the block opened at `open_at`."""
    depth = 0
    for i in range(open_at, len(text)):
        if mask[i]:
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _is_trait_impl(header: str) -> bool:
    """True when the text between `impl` and the body brace names a trait — i.e.
    it carries a `for` at bracket depth 0, past the generic parameter list.

    Depth matters: `impl<T> Trait for Vec<T> where T: Iterator<Item = for<'a>
    fn(&'a u8)>` holds three `for`s and only the first at depth 0 is the one
    that makes this a trait impl rather than an inherent one. A `where` clause
    at depth 0 ends the search — everything after it is bounds."""
    rest = _skip_generics(header)
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if c == "-" and rest[i + 1:i + 2] == ">":   # a return arrow, not a close
            i += 2
            continue
        if c in "<([":
            depth += 1
        elif c in ">)]":
            depth -= 1
        elif depth == 0 and _word_at(rest, i, "for"):
            return True
        elif depth == 0 and _word_at(rest, i, "where"):
            return False
        i += 1
    return False


def _skip_generics(header: str) -> str:
    """`header` past its `impl` generic parameter list, if it has one."""
    s = header.lstrip()
    if not s.startswith("<"):
        return s
    depth = 0
    i = 0
    while i < len(s):
        if s[i] == "-" and s[i + 1:i + 2] == ">":
            i += 2
            continue
        if s[i] == "<":
            depth += 1
        elif s[i] == ">":
            depth -= 1
            if depth == 0:
                return s[i + 1:]
        i += 1
    return s


def _word_at(text: str, i: int, word: str) -> bool:
    """True when `word` starts at `i` as a whole identifier token."""
    if not text.startswith(word, i):
        return False
    before = text[i - 1] if i else " "
    after = text[i + len(word):i + len(word) + 1] or " "
    return before not in _IDENT_CHARS and after not in _IDENT_CHARS


def _iter_item_headers(
    text: str, mask: Sequence[bool], keyword: str, name: str,
) -> Iterator[tuple[int, int, int]]:
    """Every ``<keyword> <name>`` item header in `text`, in file order, as
    ``(module_depth, keyword_start, name_end)``.

    Deliberately stops at the header: whether an item whose body never closes is
    fatal or merely skippable depends on the depth the caller was after, so
    `_item_end` is the caller's call to make and to react to."""
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
        if text[i:j] != keyword:
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
        yield depth, i, k
        i = k


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


def item_start(text: str, mask: Sequence[bool], kw_start: int) -> int:
    """`kw_start` moved back over everything that declares the SAME item: its
    visibility (`pub`, `pub(crate)`, `pub(in path)`), other leading qualifiers,
    and its outer attributes.

    Stopping at the keyword would splice a replacement that carries its own
    `pub(crate)` in below the surviving one — `pub(crate) pub(crate) struct` —
    and would leave a duplicate `#[repr]`/`#[cfg]` above it. Duplicated `#[cfg]`
    is the damaging case: the surviving predicate ANDs with the replacement's,
    so an item re-authored to widen or drop a gate stays gated by the old one
    and the tree is neither the old layout nor the new. Copies also accumulate,
    one per re-splice."""
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
