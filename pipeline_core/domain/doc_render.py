"""Markdown-in-Rust-doc-comment rendering helpers (P7 — replaces the doc-attr regexes
in `_doc_attr_string` / `_condense_doc_run`).

These operate on the text INSIDE Rust ``#[doc = "..."]`` attributes / doc comments when
condensing them to a one-line prompt summary. Pure: stdlib only, no regex.

The attribute-stripping/condensing functions (``attr_span_end`` …
``condense_doc_attrs``) produce the PROMPT view of a source file (the on-disk
file is never modified) — they strip or one-line-summarise doc/crate attributes
to protect the context budget. Relocated from pipeline.py (H/§7.3).
"""

from __future__ import annotations

from pipeline_core.domain.rust_scan import attr_opener_end


def doc_eq_value_offset(attr_inner: str) -> int | None:
    """If `attr_inner` begins with ``doc\\s*=\\s*`` (a ``doc = "..."`` attribute), return
    the offset just past that prefix (the old ``re.match(r'doc\\s*=\\s*', s).end()``);
    else None. ``doc`` is matched literally (no word boundary), as the regex did."""
    if not attr_inner.startswith("doc"):
        return None
    n = len(attr_inner)
    p = 3
    while p < n and attr_inner[p].isspace():  # \s*
        p += 1
    if p >= n or attr_inner[p] != "=":
        return None
    p += 1
    while p < n and attr_inner[p].isspace():  # \s*
        p += 1
    return p


def is_link_ref_def(s: str) -> bool:
    """True iff `s` starts with a markdown link-reference definition ``[label]:`` —
    byte-equivalent to ``re.match(r'\\[[^\\]]+\\]:', s)`` (label is ≥1 non-``]`` char)."""
    if not s.startswith("["):
        return False
    close = s.find("]", 1)
    return close > 1 and close + 1 < len(s) and s[close + 1] == ":"


def strip_inline_md_links(text: str) -> str:
    """Reduce every inline markdown link ``[label](url)`` to its `label` — byte-equivalent
    to ``re.sub(r'\\[([^\\]]+)\\]\\([^)]*\\)', r'\\1', text)`` (label ≥1 non-``]``; url is any
    run of non-``)``)."""
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] == "[":
            close = text.find("]", i + 1)
            if close > i + 1 and close + 1 < n and text[close + 1] == "(":  # [^\]]+ then ](
                rclose = text.find(")", close + 2)
                if rclose != -1:  # [^)]* then )
                    out.append(text[i + 1:close])  # group 1 (the label)
                    i = rclose + 1
                    continue
        out.append(text[i])
        i += 1
    return "".join(out)


def attr_span_end(text: str, open_idx: int) -> int:
    """Given `open_idx` = index of the `[` that opens an attribute, return the
    index just past the matching `]`. Bracket depth is tracked ONLY outside
    string literals, so `[`/`]` inside a doc string (markdown links, badges,
    embedded code) do not corrupt the count. Returns -1 if unbalanced.

    Handles normal (`"..."`, with `\\"` escapes) and raw (`r"..."`, `r#"..."#`)
    string literals — the forms that appear inside `#[doc = ...]`.
    """
    n = len(text)
    i = open_idx
    depth = 0
    while i < n:
        c = text[i]
        if c == '"':
            # Possible raw string: count preceding `r` + `#`s.
            hashes = 0
            j = i - 1
            while j >= 0 and text[j] == '#':
                hashes += 1
                j -= 1
            is_raw = j >= 0 and text[j] == 'r'
            i += 1
            if is_raw:
                closer = '"' + ('#' * hashes)
                end = text.find(closer, i)
                i = n if end == -1 else end + len(closer)
            else:
                while i < n:
                    if text[i] == '\\':
                        i += 2
                        continue
                    if text[i] == '"':
                        i += 1
                        break
                    i += 1
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def strip_attrs_for_view(text: str, *, bang: bool, doc_only: bool) -> str:
    """Remove attributes from the PROMPT view only (on-disk file is untouched).

    `bang=True`  → strip inner `#![...]` attributes (crate-level).
    `bang=False` + `doc_only=True` → strip outer `#[doc...]` attributes.

    Attribute spans are located string-literal-aware via `attr_span_end`, so
    `[`/`]` inside doc content can never desync the parser (the bug that leaked
    bare doc text into the view). The trailing newline after a stripped
    attribute is swallowed so no blank line is left behind.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '#':
            res = attr_opener_end(text, i)
            if res:
                is_bang, _aend = res
                open_idx = _aend - 1  # index of the `[`
                end = attr_span_end(text, open_idx)
                if end != -1:
                    inner = text[open_idx + 1:end - 1].lstrip()
                    want = (is_bang if bang
                            else (not is_bang and (not doc_only
                                                   or inner.startswith('doc'))))
                    if want:
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
        out.append(text[i])
        i += 1
    return ''.join(out)


def strip_doc_attrs(text: str) -> str:
    """Strip `#[doc = "..."]` attributes from the file view shown to the LLM.

    anyhow's source embeds hundreds of chars of example code (with `[]`
    markdown and embedded newlines) in `#[doc = "..."]` per public fn. They
    eat prompt budget and don't help fill a body. Stripped from the PROMPT
    view only — the on-disk file keeps them so `cargo doc` still works.
    """
    return strip_attrs_for_view(text, bang=False, doc_only=True)


def strip_crate_attrs(text: str) -> str:
    """Strip inner `#![...]` crate attributes from the file view shown to the
    LLM (including the multi-line `#![doc = "..."]` module doc). PROMPT view
    only — the on-disk file keeps them so cargo builds correctly.
    """
    return strip_attrs_for_view(text, bang=True, doc_only=False)


def doc_attr_string(attr_inner: str) -> str | None:
    """Given the text inside an attribute's brackets (e.g. `doc = " summary"`),
    return the doc string content, or None if this is not a `doc = "..."` attr.
    `#[doc(hidden)]` and other non-string doc attrs return "" (drop, no text)."""
    off = doc_eq_value_offset(attr_inner)
    if off is None:
        return None
    rest = attr_inner[off:].lstrip()
    if not rest.startswith('"'):
        return ""  # doc(hidden) / doc(alias=...) etc — no prose to keep
    body = rest[1:]
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == '\\' and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
            continue
        if body[i] == '"':
            break
        out.append(body[i])
        i += 1
    return ''.join(out)


def condense_doc_run(contents: list[str]) -> str:
    """Condense a run of consecutive doc-attr strings to a one-line summary:
    first sentence of prose, with fenced code, badges, and link-refs dropped
    and inline `[text](url)` reduced to `text`. Empty when the doc is all
    examples/badges (nothing worth a line)."""
    summary: list[str] = []
    for raw in contents:
        s = raw.strip()
        if s.startswith('```'):
            break                      # code example begins — stop the summary
        if not s:
            if summary:
                break                  # blank line after prose ends the summary
            continue
        if s.startswith('[![') or is_link_ref_def(s):
            continue                   # badge / link reference definition
        summary.append(s)
        if '.' in s:
            break                      # sentence terminator reached
    text = ' '.join(summary).strip()
    if '.' in text:
        text = text[:text.index('.') + 1]
    text = strip_inline_md_links(text)   # [t](url) -> t
    text = " ".join(text.split())
    return text[:200].rstrip()


def condense_doc_attrs(text: str) -> str:
    """Condense (not delete) doc comments in the prompt view: each run of
    `#[doc=...]` / `#![doc=...]` attributes collapses to a single
    `// <intent>` (or `//! <intent>`) line. Non-doc `#![...]` crate attrs are
    dropped (they don't help fill a body). All other tokens pass through.

    Deterministic Python (Hard Rule 9) — preserves the author's intent overview
    at ~1 line instead of either dumping hundreds of chars of examples or
    erasing the intent entirely (FR-026). String-literal-aware via
    `attr_span_end`, so `[]` inside doc content can never desync the parse.
    """
    out: list[str] = []
    run: list[str] = []
    run_bang = False
    run_active = False

    def _flush() -> None:
        nonlocal run, run_active, run_bang
        if run_active:
            s = condense_doc_run(run)
            if s:
                out.append(('//! ' if run_bang else '// ') + s + '\n')
            run = []
            run_active = False
            run_bang = False

    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '#':
            res = attr_opener_end(text, i)
            if res:
                is_bang, _aend = res
                open_idx = _aend - 1
                end = attr_span_end(text, open_idx)
                if end != -1:
                    inner = text[open_idx + 1:end - 1].strip()
                    doc_str = doc_attr_string(inner)
                    if doc_str is not None:           # a doc attr
                        if not run_active:
                            run_active = True
                            run_bang = is_bang
                        if doc_str:
                            run.append(doc_str)
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
                    if is_bang:                       # non-doc crate attr → drop
                        _flush()
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
        if not c.isspace():
            _flush()
        out.append(c)
        i += 1
    _flush()
    return ''.join(out)
