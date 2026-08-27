"""LLM-output extraction — parse <FILE> blocks + genesis content from model responses.

Relocated verbatim from pipeline.py (slice 5b foundation). The regex here operates on
model *response text* (the <FILE path="…"> protocol, markdown artefacts, genesis
delimiters), not Rust source — source parsing stays on the syn extractor. Logging goes
through the :mod:`pipeline_core.log` facade; the genesis-format text helper
`extract_code_content` is imported from :mod:`pipeline_core.domain.text`.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .. import log as _log
from .rust_fn_headers import fn_body_within_item, unwrap_own_fn_definition
from .rust_idents import canon_fn_name
from .text import extract_code_content

# The `<FILE path="…">…</FILE>` model-protocol parser is a tolerant,
# regex-free state machine (`_find_file_blocks` / `_bad_file_marker` below).
# Fenced ```lang … ``` code blocks (fallback when no <FILE> tag) and the prompt-only
# `// !! UNCOVERED` markers models sometimes copy back into their output. Both are
# model-output protocol parsing (fenced-block / comment-marker class, distinct
# from the `<FILE>`/`<FN>` block protocol), kept here so the
# application layer (task0c) stays regex-free.
_CODE_BLOCK_PATTERN = re.compile(r'```[a-zA-Z0-9_-]*\n(.*?)```', re.DOTALL)
_UNCOVERED_MARKER_PATTERN = re.compile(r'\s*//\s*!!\s*UNCOVERED[^\n]*')

GENESIS_DELIM_START = "===GENESIS_START==="
GENESIS_DELIM_END = "===GENESIS_END==="


def _find_file_blocks(text: str) -> list[tuple[str, str]]:
    """Regex-free, byte-equivalent replacement for `<FILE path="([^"]+)">(.*?)
    </FILE>` (DOTALL findall): (path, body) per well-formed block, in order. The
    body is the nearest-`</FILE>` span; a malformed/unterminated block is skipped
    exactly as a regex non-match would skip it (retry at the next open tag).
    Single-quoted paths are folded to this canonical form first by
    `_rewrite_sq_file_blocks` (in `_normalize_file_markers`)."""
    out: list[tuple[str, str]] = []
    lead, close_tag = '<FILE path="', "</FILE>"
    i = 0
    while True:
        s = text.find(lead, i)
        if s == -1:
            return out
        v = s + len(lead)
        end = text.find('"', v)                   # path = `[^"]+`
        if end in (-1, v):                        # need a closing quote AND ≥1 char
            i = s + 1
            continue
        if text[end + 1:end + 2] != ">":          # the `">` that closes the open tag
            i = s + 1
            continue
        body_start = end + 2
        e = text.find(close_tag, body_start)
        if e == -1:
            i = s + 1
            continue
        out.append((text[v:end], text[body_start:e]))
        i = e + len(close_tag)


def _rewrite_sq_file_blocks(text: str) -> str:
    """Regex-free, byte-equivalent replacement for
    ``re.sub(r"<FILE path='([^']+)'>(.*?)</FILE>", '<FILE path="\\1">\\2</FILE>', text)``
    (DOTALL): rewrite every single-quoted `<FILE path='…'>…</FILE>` block to the
    canonical double-quoted form, copying unmatched text verbatim. Same
    non-greedy nearest-`</FILE>` and skip-on-malformed semantics as the finder."""
    open_tag, close_tag = "<FILE path='", "</FILE>"
    out: list[str] = []
    i = last = 0
    while True:
        s = text.find(open_tag, i)
        if s == -1:
            out.append(text[last:])
            return "".join(out)
        v = s + len(open_tag)
        end = text.find("'", v)
        if end in (-1, v) or text[end + 1:end + 2] != ">":
            i = s + 1
            continue
        body_start = end + 2
        e = text.find(close_tag, body_start)
        if e == -1:
            i = s + 1
            continue
        out.append(text[last:s])
        out.append(f'<FILE path="{text[v:end]}">{text[body_start:e]}</FILE>')
        i = last = e + len(close_tag)


def _is_ascii_alnum(c: str) -> bool:
    return c.isascii() and c.isalnum()


def _bad_file_marker(text: str) -> bool:
    """Regex-free `<([^>]+\\.[a-zA-Z0-9]+)>` detector: a `<…>` whose inner text
    (no `>`) ends with `.<ascii-alnum>+` — the wrong `<path/to/file.ext>` shape
    a model uses instead of the canonical `<FILE path="…">`. Used only to warn."""
    i = 0
    while (lt := text.find("<", i)) != -1:
        gt = text.find(">", lt + 1)
        if gt == -1:
            return False                          # no `>` after → no later `<` can match either
        inner = text[lt + 1:gt]                    # `[^>]+`
        j = len(inner)
        while j > 0 and _is_ascii_alnum(inner[j - 1]):   # trailing `[a-zA-Z0-9]+`
            j -= 1
        if j >= 2 and j < len(inner) and inner[j - 1] == ".":  # `.` preceded by ≥1 char
            return True
        i = lt + 1
    return False


def _normalize_file_markers(text: str) -> str:
    """
    Small models frequently prefix <FILE ...> tags with markdown artefacts:
      ### <FILE path="x">   ->  <FILE path="x">
      ### FILE path="x">    ->  <FILE path="x">
      <FILE path='x'>       ->  <FILE path="x">
    Normalise these so `_find_file_blocks` sees the canonical double-quoted tag.

    The two `#`-prefix passes are text-cleanup heuristics over malformed model
    output (not structured-fact extraction), so per R7.6 they legitimately stay
    as `re.sub` — replicating `#{1,6}`'s greedy backtracking char-by-char buys no
    fidelity. The single-quote→double-quote pass IS structured (`_rewrite_sq_file_blocks`,
    regex-free, R8.5); the canonical-tag *extraction* is the state machine above.
    """
    # Strip leading `###` (or `##`, `#`) before a <FILE ... > tag on the same line.
    text = re.sub(r'#{1,6}\s*(<FILE\s)', r'\1', text)
    # Handle `### FILE path=` (missing opening `<`)
    text = re.sub(r'#{1,6}\s*FILE\s+path=', '<FILE path=', text)
    # Single-quote to double-quote: <FILE path='x'> -> <FILE path="x">
    text = _rewrite_sq_file_blocks(text)
    return text


def _looks_like_prose(content: str, path: str) -> bool:
    """Return True if file content appears to be prose description rather than source code.

    Guards against the model writing 'This snippet outlines...' style descriptions
    instead of actual code inside <FILE> blocks.
    """
    # Only apply to source code extensions
    if not any(path.endswith(ext) for ext in ('.rs', '.py', '.js', '.ts', '.go', '.java', '.c', '.cpp', '.h')):
        return False
    first_line = content.lstrip().split('\n')[0] if content.strip() else ''
    prose_starters = (
        'this ', 'the ', 'here ', 'below ', 'note:', 'note ', 'file ', 'module ',
        'this file', 'this module', 'this snippet', 'this code', 'this implementation',
        'the following', 'below is', 'here is', 'as outlined', 'per the',
    )
    return any(first_line.lower().startswith(p) for p in prose_starters)


def extract_tagged_block(text: str, tag: str) -> str | None:
    """The inner text of the first ``<TAG> … </TAG>`` span (non-greedy, DOTALL), or
    None — byte-equivalent to ``re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)``
    projected onto ``group(1)`` (caller trims)."""
    open_t, close_t = f"<{tag}>", f"</{tag}>"
    i = text.find(open_t)
    if i == -1:
        return None
    j = text.find(close_t, i + len(open_t))
    if j == -1:
        return None
    return text[i + len(open_t):j]


def extract_signature_block(response: str) -> str | None:
    """Pull ``<SIGNATURE> … </SIGNATURE>`` from an LLM response; the trimmed
    inner text or None if absent/empty."""
    inner = extract_tagged_block(response, "SIGNATURE")
    if inner is None:
        return None
    return inner.strip() or None


def extract_response_code_fence(text: str) -> str | None:
    """The first non-empty fenced body trying ```` ```rust `` then ```` ```[rs] `` —
    byte-equivalent to searching ``r'```rust\\s*(.*?)```'`` then
    ``r'```(?:rs)?\\s*(.*?)```'`` (DOTALL), `.strip()`ped, first non-empty."""
    for lang, optional in (("rust", False), ("rs", True)):
        body = _scan_code_fence(text, lang, optional)
        if body is not None:
            stripped = body.strip()
            if stripped:
                return stripped
    return None


def extract_body_response(response: str, fn_name: str | None = None) -> str | None:
    """Pull a Rust function body out of an LLM response.

    Priority: <BODY>…</BODY> tag → <FN name=…> block → ```rust``` fence → raw
    response (last resort). Returns None on empty/blank.

    The `<FN>` rung exists because several per-fn prompts specify that format,
    and a model given one fn will often answer with its siblings too. Without
    it the raw fallback returns the WHOLE reply — literal `<FN …>` tags and all
    — as "the body", which splices unparseable text and loses a correct answer.
    A reply that uses `<FN>` never reaches the raw fallback: when the wanted
    name is absent and several blocks are offered, None (ask again) beats
    splicing a sibling's body into this fn.
    """
    if not response or not response.strip():
        return None
    inner = extract_tagged_block(response, "BODY")
    if inner is not None:
        return inner.strip() or None
    blocks = extract_fn_bodies(response)
    if blocks:
        named = [b for b in blocks
                 if fn_name and b["fn_name"] == canon_fn_name(fn_name)]
        chosen = named[0] if named else (blocks[0] if len(blocks) == 1 else None)
        return (chosen["body"].strip() or None) if chosen else None
    fenced = extract_response_code_fence(response)
    if fenced:
        return fenced
    return response.strip() or None


def _scan_code_fence(text: str, lang: str, optional: bool) -> str | None:
    """First ```` ``` `` + (optional) `lang` + ``\\s*`` + capture-to-next-```` ``` ````."""
    n = len(text)
    i = 0
    while (k := text.find("```", i)) >= 0:
        p = k + 3
        if text.startswith(lang, p):
            p += len(lang)
        elif not optional:
            i = k + 1
            continue
        while p < n and text[p].isspace():  # \s*
            p += 1
        close = text.find("```", p)
        if close != -1:
            return text[p:close]
        i = k + 1
    return None


def extract_code_blocks(text: str) -> list[str]:
    """Return the contents of every fenced ```lang … ``` block in model output."""
    return _CODE_BLOCK_PATTERN.findall(text)


def strip_uncovered_markers(text: str) -> str:
    """Remove `// !! UNCOVERED` annotation markers the model copied back from an
    annotated prompt into its output (prompt-only markers must not persist in source)."""
    return _UNCOVERED_MARKER_PATTERN.sub('', text)


def extract_files(text: str) -> dict[str, str]:
    """Parse all <FILE path="...">...</FILE> blocks from model output."""
    from .text import strip_markdown_fences
    text = _normalize_file_markers(text)
    files = {}
    for path, content in _find_file_blocks(text):
        content = strip_markdown_fences(content)
        if _looks_like_prose(content, path):
            _log.log(f"  WARNING: model wrote prose instead of code for `{path}` — skipping.")
            continue
        files[path] = content
    if not files and _bad_file_marker(text):
        _log.log("  WARNING: model used wrong file format (e.g. <path/to/file> instead of "
                 '<FILE path="path/to/file">). Retrying so it uses the correct format.')
    return files


def split_api_surface_by_file(api_surface: str) -> list[tuple[str, str]]:
    """Parse a monolithic API-surface doc into `(path, full_section_text)` pairs,
    where the section text INCLUDES its `<FILE path="…">…</FILE>` framing.

    Regex-free (R8.5) replacement for `<FILE path="([^"]+)">(.*?)</FILE>` (DOTALL,
    non-greedy): require the literal `<FILE path="` open, read the path up to the
    next `"`, require the `">` that closes the open tag, then the nearest
    `</FILE>`. A block missing any of those is skipped, matching a regex non-match.
    """
    out: list[tuple[str, str]] = []
    open_tag, close_tag = '<FILE path="', "</FILE>"
    i = 0
    while True:
        s = api_surface.find(open_tag, i)
        if s == -1:
            return out
        p = s + len(open_tag)
        q = api_surface.find('"', p)
        if q in (-1, p) or api_surface[q + 1:q + 2] != ">":  # -1: no close; p: empty path
            i = p
            continue
        e = api_surface.find(close_tag, q)
        if e == -1:
            return out
        out.append((api_surface[p:q], api_surface[s:e + len(close_tag)]))
        i = e + len(close_tag)


_RETRO_LABELS = ("category", "guidance", "note", "reason")


def _strip_retro_decorations(line: str) -> str:
    """Strip bullet/markdown decoration from a retrospective line (regex-free):
    leading bullets `*-•`, then surrounding `*` (markdown bold) and spaces."""
    s = line.strip().lstrip("*").lstrip("-").lstrip("•").strip()
    return s.strip("*").strip()


def _retro_label(s: str) -> tuple[str | None, str]:
    """If `s` opens with `<label>[:=]` (case-insensitive, spaces allowed before
    the separator), return (label, rest_after_separator). Mirrors the old
    `^<label>\\s*[:=]\\s*` regexes without regex."""
    low = s.lower()
    for lab in _RETRO_LABELS:
        if low.startswith(lab):
            after = s[len(lab):].lstrip()
            if after[:1] in (":", "="):
                return lab, after[1:].lstrip()
    return None, ""


def parse_retrospective(result: str, *, guidance_cap: int = 1200) -> dict[str, str]:
    """Parse a fix-loop retrospective response into {category, guidance, signal}.
    Tolerant + regex-free (R7): handles `CATEGORY:`/`category =`/`**GUIDANCE**:`
    and bullet prefixes; GUIDANCE may span multiple lines. Falls back to the
    whole response as guidance when neither field parses."""
    cat = ""
    guidance_lines: list[str] = []
    in_guidance = False
    for ln in (result or "").splitlines():
        s = _strip_retro_decorations(ln)
        lab, rest = _retro_label(s)
        if lab == "category" and rest.split():  # needs a token (old `\S+`)
            cat = rest.split()[0].rstrip(".,;").upper()
            in_guidance = False
            continue
        if lab == "guidance" and rest:           # needs content (old `.+`)
            guidance_lines = [rest.strip()]
            in_guidance = True
            continue
        if in_guidance and s:
            # continuation — stop on any new label, else accumulate.
            if _retro_label(s)[0] is not None:
                in_guidance = False
            else:
                guidance_lines.append(s)
    guid = " ".join(guidance_lines).strip()
    if not cat and not guid and (result or "").strip():
        guid = (result or "").strip()[:guidance_cap]
    return {"category": cat, "guidance": guid,
            "signal": "ok" if (cat or guid) else "unparseable"}


def parse_self_note(resp: str, *, min_len: int = 20, max_len: int = 300) -> str | None:
    """Pure parse of a model self-correction note response. Takes the last
    non-empty line, strips surrounding backticks/quotes, and rejects a
    declined note (`NONE` or shorter than `min_len`). Returns the note capped
    at `max_len`, or None. Regex-free (R7)."""
    if not resp:
        return None
    lines = [ln.strip() for ln in resp.splitlines() if ln.strip()]
    if not lines:
        return None
    candidate = lines[-1].strip("`").strip("\"'")
    if candidate.upper() == "NONE" or len(candidate) < min_len:
        return None
    return candidate[:max_len]


def _extract_genesis(text: str) -> str | None:
    """
    Extract genesis.md content from Task 4 response.
    1. Primary: content between GENESIS_DELIM_START / GENESIS_DELIM_END.
    2. Fallback: <FILE path="genesis.md">...</FILE> block.
    3. Last resort: content inside any markdown/text code fence (model wraps in ```markdown).
    Returns the stripped content string, or None if not found.
    """
    start = text.find(GENESIS_DELIM_START)
    end = text.find(GENESIS_DELIM_END)
    if start != -1 and end != -1 and end > start:
        return text[start + len(GENESIS_DELIM_START):end].strip()
    # Model output start delimiter but forgot the closing one — use rest of response.
    if start != -1:
        content = text[start + len(GENESIS_DELIM_START):].strip()
        if len(content) > 100:
            return content
    # Fallback: FILE tag format
    files = extract_files(text)
    if "genesis.md" in files:
        return files["genesis.md"]
    # Last resort: strip thinking + any code fence and take the bare content.
    content = extract_code_content(text)
    if content and len(content) > 100:
        return content
    return None


# ---------------------------------------------------------------------------
# <FN name="X" block="Y">body</FN> model-protocol parsers — tolerant,
# regex-free state machines (NOT model-JSON; local models adhere poorly to it).
# Same model-output concern as the <FILE> block parser above.
# ---------------------------------------------------------------------------


def _skip_ws(text: str, pos: int) -> int:
    """Advance past `\\s*` from `pos` (str.isspace mirrors regex `\\s` for ASCII)."""
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    return pos


def _attr_value_close(text: str, start: int) -> int:
    """Index of the `"` that CLOSES an attribute value opened at `start`, or -1.

    A backslash-escaped quote does NOT close the value. Models that put a
    `#[cfg(feature = "std")]` line inside an attribute escape its quotes the
    JSON way (`\\"std\\"`); reading the first bare `"` as the terminator
    truncated the value mid-attribute and the whole tag then failed to parse.
    """
    i, n = start, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2                           # the escaped char, whatever it is
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _read_quoted_attr(text: str, pos: int, name: str,
                      *, allow_empty: bool = False) -> tuple[str, int] | None:
    """If `text[pos:]` opens with `name="<value>"` (value = `[^"]+`, i.e. ≥1
    non-quote char), return (value, pos-after-closing-quote); else None.

    The value is HTML-unescaped: models emit XML-safe attribute values for
    generic headers (`block="impl&lt;T, E&gt; Context&lt;T, E&gt; …"`), which
    would never match a real container (run 346122: every file-first body for
    src/context.rs dropped on `&lt;`-escaped headers).

    `allow_empty` accepts a zero-length value, and only the `block` reader asks
    for it. Refusing one cost the whole tag, not just the attribute: the caller
    reads `name`, tries `block` at the next position, and a None leaves the
    scan sitting ON `block=""` — so the `>` is never found and
    `_find_fn_blocks` drops the record. `block=""` is not malformed, it is the
    only honest answer a FREE fn can give, since the prompt asks for
    `block="IMPL_HEADER"` and a free fn has no impl header.
    nightly/src/error.rs 2026-08-04T16:12:06Z was the first prompt this
    pipeline ever sent that asked for free fns — `object_ref`,
    `object_downcast`, `context_downcast`, `context_chain_downcast`, the four
    carrying the `test_downcast` use-after-free. The model answered all four
    with `block=""` and the call was logged `empty/no FN blocks (4 FN tag(s) in
    reply)` and discarded.

    `name` keeps the ≥1-char rule: a tag that names no fn identifies nothing,
    and empty there is the shape `_find_fn_blocks` is pinned to skip exactly as
    the regex it replaced did."""
    lead = name + '="'
    if not text.startswith(lead, pos):
        return None
    v = pos + len(lead)
    end = _attr_value_close(text, v)
    if end == -1 or (end == v and not allow_empty):
        return None
    return html.unescape(text[v:end].replace('\\"', '"')), end + 1


_XML_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def xml_unescape_strict(text: str) -> str:
    """Resolve XML entity references, STRICTLY: only the five predefined names
    and numeric refs, and only when terminated by `;`.

    `html.unescape` must NOT be used on code. HTML5 accepts a named reference
    without its semicolon, so it rewrites the ordinary Rust `&ampersand` to
    `&ersand` and `&gtr_bound` to `>r_bound` — silently mangling any identifier
    whose name starts with an entity name. Attribute values can afford that
    leniency; a function body cannot.
    """
    if "&" not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = text.find(";", i + 1, i + 12)
        ref = text[i + 1:end] if end != -1 else ""
        if ref in _XML_ENTITIES:
            out.append(_XML_ENTITIES[ref])
            i = end + 1
        elif ref.startswith("#") and ref[1:].isdigit():
            out.append(chr(int(ref[1:])))
            i = end + 1
        elif ref[:2].lower() == "#x" and ref[2:] and all(
                c in "0123456789abcdefABCDEF" for c in ref[2:]):
            out.append(chr(int(ref[2:], 16)))
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def xml_unescaped_variant(body: str) -> str | None:
    """The XML-unescaped reading of a model-emitted body, or None when it is
    identical to the input.

    A model that renders `block="impl&lt;E&gt; …"` in an attribute renders
    `match &amp;mut self.state` in the body of the same reply — the escaping is
    a property of the reply, not of the field. The body is CODE, so this
    variant is only ever a *candidate*: the caller keeps the verbatim body when
    it parses and falls back to this reading only when it does not, which is
    what stops a legitimate `"&amp;"` inside a string literal from being
    rewritten to `"&"`.
    """
    variant = xml_unescape_strict(body)
    return variant if variant != body else None


def _tag_terminator(text: str, pos: int) -> int:
    """Index just past the `>` that closes a tag at `pos`, or -1.

    `&gt;` is accepted there as well as a literal `>`. A model that renders its
    generics XML-safe renders the tag's own terminator the same way — live:
    `<FN name="new" block="impl&lt;T&gt; Own&lt;T&gt;…"&gt;`, two correct
    bodies for `src/ptr.rs::Own<T>` discarded on that one entity. The reading
    is decidable rather than a guess: this position is the end of the
    attribute list, before any body has begun, and the only token the grammar
    allows there is the terminator.
    """
    if pos < len(text) and text[pos] == ">":
        return pos + 1
    if text.startswith("&gt;", pos):
        return pos + 4
    return -1


def _self_closing_terminator(text: str, pos: int) -> int:
    """Index just past a SELF-CLOSING tag terminator (`/>` — or `/&gt;`, the
    XML-safe rendering) at `pos`, or -1.

    A self-closed `<FN …/>` is a COMPLETE record whose body slot is empty by
    construction — the model that emits it has put the answer somewhere else,
    and the observed somewhere-else is the `block` attribute:
    `<FN name="context" block="match self {…}"/></FN>` (per-impl maximal
    last-resort reply, `src/context.rs::Context<T,E> for Result<T,E>`, run
    `275772-425da59c` 2026-08-19, recurring across the 16 maximal fires of
    2026-08-18/19). Refusing the `/` cost the whole reply: the scan sat on it,
    never found the `>`, dropped both records, and the call was logged
    `maximal attempt returned no FN blocks (2 FN tag(s) in reply)` — the
    standing rule being that N tags in with 0 records out is always the
    reader. Accepted here, the record reaches `_recover_slots`, whose
    body↔block swap already recovers the answer.
    """
    if pos < len(text) and text[pos] == "/":
        return _tag_terminator(text, pos + 1)
    return -1




#: What may follow an attribute value's closing quote inside the open tag —
#: nothing (the quote is the last thing before the window), or a terminator the
#: model did type. Anything else means the quote was not the value's end.
_TAG_TAILS = ("", ">", "/>", "&gt;", "/&gt;")


def _attr_is_terminated(text: str, pos: int) -> bool:
    """True when `pos` (after an attribute's closing quote) is a tag end.

    Optional whitespace, then `>` / `/>` (or the XML-safe forms). A first
    `"` that is not followed by one of these is interior — the live shape
    is `block="impl>#[cfg(..., feature = "std")]`, where `_read_quoted_attr`
    succeeds on the `"` before `std` and the tag never terminates.
    """
    q = _skip_ws(text, pos)
    return (_self_closing_terminator(text, q) != -1
            or _tag_terminator(text, q) != -1)


def _bare_tag_gt(text: str, start: int, limit: int) -> int:
    """Index of a `>` that can close an FN open tag in `text[start:limit]`, or -1.

    A generic closer (`Foo<E>`) is seen at depth 1 and skipped. `->` and `=>`
    are skipped too. The live hole is a `>` at depth 0 that is none of those:
    `block="impl>#[cfg…]`. Taking the first `>` instead split
    `block="impl Foo<E>"</FN>` at the generic closer (body became the leftover
    `"`), which is the missing-`>` recovery this helper must not steal.
    """
    depth = 0
    i = start
    while i < limit:
        ch = text[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth > 0:
                depth -= 1
            elif i > start and text[i - 1] in "-=":
                pass
            else:
                return i
        i += 1
    return -1


def _block_closed_by_bare_gt(text: str, pos: int) -> tuple[str, int] | None:
    """`(block-header, body-start)` for `block="impl>BODY`.

    The model dropped the attribute's closing quote and used `>` as the
    tag terminator — the protocol's own `block="IMPL_HEADER">` minus one
    `"`. Only fires when a tag-closing `>` appears before any `"` after
    `block="`, so an unescaped `"std"` in the BODY cannot steal the
    attribute close. Reply `eb7b448cd4c1…` (2026-08-22T13:57Z): 6 of 20
    tags were this shape (`context_backtrace`, `context_downcast`,
    `context_drop_rest`, `no_backtrace`, `object_boxed`,
    `object_reallocate_boxed`) and the first-quote path dropped every one.
    """
    q = _skip_ws(text, pos)
    if not text.startswith('block="', q):
        return None
    start = q + len('block="')
    cands = [x for x in (text.find("</FN", start), text.find("<FN ", start))
             if x != -1]
    window = min(cands) if cands else len(text)
    quote = text.find('"', start, window)
    limit = quote if quote != -1 else window
    gt = _bare_tag_gt(text, start, limit)
    if gt == -1:
        return None
    raw = text[start:gt].rstrip()
    # `>` in `new<E>` / `-> Self` is not a tag closer. The live hole is
    # specifically `block="impl>#[cfg…]` / `block="impl>unsafe fn`.
    head = raw.split(None, 1)[0] if raw.split() else ""
    if head.split("<", 1)[0] not in ("impl", "trait", "mod"):
        return None
    value = html.unescape(raw.replace('\\"', '"'))
    return value, gt + 1


def _unterminated_open_tag_value(text: str, pos: int) -> tuple[str, int] | None:
    """`(block-attribute value, resume index)` for a `<FN name="…"` whose open
    tag never terminates, or None when there is no `block="` to read.

    `pos` is the index just past the `name` attribute. The reading is the one
    the strict path would have produced had the model typed the character it
    dropped, which is why the value is returned in the ATTRIBUTE slot: the
    caller emits it with an empty body and `_recover_slots`' existing
    body-in-the-`block`-attribute swap turns it into the body, exactly as it
    does for a self-closed tag.

    `_find_fn_close` already grants the CLOSING tag this tolerance — a bare
    `</FN` with no `>` is accepted rather than discarding a body over
    punctuation — and the opening tag had none. Three model shapes reach here,
    all of them complete answers:

        <FN name="new" block="…}"</FN>          the `>` is simply absent
        <FN name="drop" block="unsafe {…}\n</FN>   the closing `"` is absent
        <FN name="len" block="…feature = "std"…" />  an UNESCAPED `"` inside

    The third closes `_attr_value_close` early, so the tag is unterminated from
    that point on; taking the LAST candidate quote in the window rather than
    the first is what reads it as the model meant it.

    The WINDOW is load-bearing, not defensive. This record cannot extend past
    the next `</FN` or the next `<FN `, whichever comes first. Without the
    `</FN` bound one fumbled character cascades: the value scans into the
    following tag's text, the scan resumes mid-tag, and every remaining tag in
    the reply is lost — `0b38076c` (4 tags), `acbf57e7` (5) and `e7167489` (2)
    each returned zero records that way. Without the `<FN ` bound a reply whose
    tags are terminated but never closed loses the rest of itself instead:
    `b05175b1`, 28 records today, drops to 3 when its third tag's unescaped
    quote lets it swallow the other 25.
    """
    cands = [x for x in (text.find("</FN", pos), text.find("<FN ", pos))
             if x != -1]
    window = min(cands) if cands else len(text)
    q = _skip_ws(text, pos)
    if not text.startswith('block="', q):
        return None
    start = q + len('block="')
    seg = text[start:window]
    close = _last_unescaped_quote(seg)
    if close == -1:
        # No quote closes the value anywhere in the window: the model opened
        # `block="` and ran the body straight into `</FN>`. The window IS the
        # value.
        value = seg
    elif seg[close + 1:].strip() in _TAG_TAILS:
        value = seg[:close]
    else:
        # A quote closes the value and then something that is not a terminator
        # follows it. That is not a dropped character, it is a tag whose shape
        # this reader cannot decide — `<FN name="f" block="b"x>y</FN>` has a
        # real `>` one character further on, so the body is `y`, not `b`.
        # Guessing here would splice the attribute over the answer.
        return None
    return html.unescape(value.rstrip().replace('\\"', '"')), window


def _last_unescaped_quote(seg: str) -> int:
    """Index of the last `"` in `seg` that is not backslash-escaped, or -1.

    Escaped quotes are the model rendering a `#[cfg(feature = \\"std\\")]` line
    inside an attribute the JSON way, and `_attr_value_close` already refuses
    to end a value on one. The LAST unescaped quote rather than the first is
    what reads an UNescaped `"std"` correctly: the first one closes the value
    mid-body, the last one is where the model actually ended it."""
    out, i, n = -1, 0, len(seg)
    while i < n:
        if seg[i] == "\\":
            i += 2
            continue
        if seg[i] == '"':
            out = i
        i += 1
    return out


def _find_fn_close(text: str, start: int) -> tuple[int, int] | None:
    """Nearest `</FN\\s*>` at/after `start`. Returns (close_start, pos_after_`>`).

    A bare `</FN` with no `>` is accepted as a fallback, after every
    well-formed close has been ruled out: the model DID close the block and
    fumbled one character, and reading that as unterminated discards the body
    over punctuation."""
    j = start
    bare = -1
    while True:
        c = text.find("</FN", j)
        if c == -1:
            return (bare, bare + 4) if bare != -1 else None
        k = _skip_ws(text, c + 4)
        end = _tag_terminator(text, k)
        if end != -1:
            return c, end
        if bare == -1:
            bare = c
        j = c + 1


def _find_fn_blocks(text: str) -> list[tuple[str, str, str]]:
    """Tolerant, regex-free reader for `<FN name="…" block="…">body</FN>`:
    (name, block, body) per block, in order.

    Two departures from the strict grammar, both because a tag the reader
    refuses is a tag the caller reports as `no FN blocks` — indistinguishable
    from a model that said nothing, and the reply is discarded whole:

    * `block` is OPTIONAL. It disambiguates WHICH container the body belongs
      to; a reply that omits it has still answered. Absent reads as `""`, and
      so does an explicitly empty `block=""` — the shape a FREE fn's answer
      takes, since the prompt asks for an impl header and a free fn has none.
      Both mean "no container named", which is what the free-fn reroute wants.
    * A missing `</FN>` ends the record with an EMPTY body rather than
      dropping it — the header's attributes survive, and when the model put
      the body in one of them :func:`extract_fn_bodies` still recovers it.
      Deliberately not "take the rest of the reply": that would splice
      whatever prose followed a truncated body.
    * A SELF-CLOSED tag (`<FN …/>`) is the same case arriving closed: a
      complete record with an empty body slot, accepted for
      :func:`_self_closing_terminator`'s reason. It ends AT the `/>` — the
      scan must not go looking for a `</FN>`, which would swallow any
      following `<FN>` block into this record's body.
    * Either tag's terminator may arrive as `&gt;` (:func:`_tag_terminator`).
    """
    out: list[tuple[str, str, str]] = []
    i = 0
    while True:
        s = text.find("<FN", i)
        if s == -1:
            return out
        p = _skip_ws(text, s + 3)
        if p == s + 3:                       # `\s+` needs ≥1 whitespace
            i = s + 1; continue
        name = _read_quoted_attr(text, p, "name")
        if name is None:
            i = s + 1; continue
        nm_val, p = name
        bl_val = ""
        q = _skip_ws(text, p)
        block = (_read_quoted_attr(text, q, "block", allow_empty=True)
                 if q > p else None)
        if block is not None and not _attr_is_terminated(text, block[1]):
            # First `"` was interior. Fail over: the quote did not close
            # the attribute, so the terminated-tag path must not run.
            block = None
        if block is not None:
            bl_val, q = block
            q = _skip_ws(text, q)            # `\s*` before `>`
        else:
            closed = _block_closed_by_bare_gt(text, p)
            if closed is not None:
                bl_val, body_start = closed
                close = _find_fn_close(text, body_start)
                if close is None:
                    out.append((nm_val, bl_val, ""))
                    i = body_start
                    continue
                body_end, after = close
                out.append((nm_val, bl_val, text[body_start:body_end]))
                i = after
                continue
        sc = _self_closing_terminator(text, q)
        if sc != -1:
            out.append((nm_val, bl_val, ""))
            i = sc
            continue
        body_start = _tag_terminator(text, q)
        if body_start == -1:
            recovered = _unterminated_open_tag_value(text, p)
            if recovered is None:
                i = s + 1; continue
            bl_val, i = recovered
            out.append((nm_val, bl_val, ""))
            continue
        close = _find_fn_close(text, body_start)
        if close is None:
            out.append((nm_val, bl_val, ""))
            i = body_start; continue
        body_end, after = close
        out.append((nm_val, bl_val, text[body_start:body_end]))
        i = after


_CONTAINER_KEYWORDS = ("impl", "trait", "mod")


def _is_bare_ident(text: str) -> bool:
    """`text` is a single Rust identifier — the only thing the `name` slot can
    hold that actually identifies a function."""
    t = text.strip()
    if not t or not (t[0].isalpha() or t[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in t)


def _names_a_container(text: str) -> bool:
    """`text` names a CONTAINER (`impl …`, `trait …`, `mod …`, or an attribute
    decorating one) rather than being a function body."""
    t = text.strip()
    if t.startswith("#["):
        return True
    head = t.split(None, 1)[0] if t.split() else ""
    return head.split("<", 1)[0] in _CONTAINER_KEYWORDS


def _declared_fn_name(text: str) -> str | None:
    """Identifier of the first `fn <ident>` DECLARATION in `text`, else None."""
    n = len(text)
    i = 0
    while (k := text.find("fn", i)) != -1:
        i = k + 2
        if k and (text[k - 1].isalnum() or text[k - 1] == "_"):
            continue                         # `my_fn`, `.rfn` — not the keyword
        if i >= n or not text[i].isspace():
            continue
        j = _skip_ws(text, i)
        e = j
        while e < n and (text[e].isalnum() or text[e] == "_"):
            e += 1
        if e > j and not text[j].isdigit():
            return text[j:e]
    return None


def _recover_slots(name: str, block: str, body: str) -> tuple[str, str, str]:
    """Re-assign a tag's three slots — fn name, container, body — by CONTENT.

    The grammar assigns them by POSITION, so a model that shuffles them
    produced exactly what a model that answered nothing produced: no records,
    `no FN blocks` in the log, and a discarded call. On 07-31 eight of
    `anyhow`'s replies arrived shuffled and every one of them carried a usable
    body — one 53 KB reasoning trace ending in a correct one-line fix, thrown
    away six GPU-minutes after it was asked for. Three shapes, each
    unambiguous from the text alone:

        <FN name="source">fn source(…) {…}</FN>            container slot EMPTY
        <FN name="construct_from_boxed" block="<body>"></FN>  body IN container
        <FN name="impl From<E> for Error" …>              container in the NAME

    Runs only where the strict reading found nothing usable, so a well-formed
    reply takes exactly the path it always did."""
    if not body.strip() and block.strip():
        # Body slot empty, container slot full: the model wrote the answer
        # into the attribute. The attribute is then not a container.
        body, block = block, ""
    fn_name = name
    declared = _declared_fn_name(body)
    if declared is not None and not _is_bare_ident(canon_fn_name(name)):
        # The name slot holds a container header, so it carries no fn
        # identity; the body names the fn whenever the reply restated the
        # definition, and the header is the container that body belongs to.
        if not block.strip() and _names_a_container(name):
            block = name
        fn_name = declared
    if _names_a_container(body):
        # The answer restates the whole item. Never true of a body the strict
        # reading accepted — this text came out of the container slot.
        inner = fn_body_within_item(body, canon_fn_name(fn_name))
        if inner is not None:
            body = inner
    return fn_name, block, body


def _split_leading_where(body: str) -> tuple[str | None, str]:
    """Mirror `^\\s*(where\\s[^\\n]+)\\n` matched on `body + "\\n"`: an optional
    leading `where` clause on the body's first line. Returns (clause|None,
    remaining_body) — the splicer rewrites the fn signature when a clause is present."""
    t = body + "\n"
    k = _skip_ws(t, 0)
    if not t.startswith("where", k):
        return None, body
    w = k + 5
    if w >= len(t) or not t[w].isspace():    # the single `\s` after `where`
        return None, body
    r = w + 1
    while r < len(t) and t[r] != "\n":       # `[^\n]+`
        r += 1
    if r == w + 1:                           # needs ≥1 non-newline char
        return None, body
    return t[k:r].strip(), body[r:].lstrip()


def extract_fn_bodies(text: str) -> list[dict[str, Any]]:
    """Parse `<FN name="X" block="...">body</FN>` blocks from a model
    response. Returns a list of records:
        {"fn_name": str, "block": str, "where_clause": str | None,
         "body": str}
    The order in the response is preserved. Bodies are stripped of
    surrounding fences if present. ``fn_name`` is reduced to its bare
    identifier (:func:`canon_fn_name`) so a path-qualified answer still
    resolves at splice time instead of being lost as `fn_not_found`.
    """
    out: list[dict[str, Any]] = []
    for fn_name, block_header, body in _find_fn_blocks(text or ""):
        body_stripped = body.strip()
        # A tag whose name slot does not identify a fn, or whose body slot is
        # empty, was not understood — not answered-with-nothing. Read its
        # slots by content before giving up on the call (`_recover_slots`).
        if not (body_stripped and _is_bare_ident(canon_fn_name(fn_name))):
            fn_name, block_header, body_stripped = _recover_slots(
                fn_name, block_header, body_stripped)
        # Strip outermost code fence if the model wrapped the body.
        if body_stripped.startswith("```"):
            lines = body_stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            body_stripped = "\n".join(lines).strip()
        if not body_stripped:
            continue
        # A reply that wraps the body in the target's full `fn` definition
        # would splice as a NESTED fn: the outer fn then has no tail
        # expression (guaranteed E0308) and the duplicate header breaks every
        # later bare-name revert (`ambiguous_name`). Unwrap to the inner body.
        _bare = canon_fn_name(fn_name)
        unwrapped = unwrap_own_fn_definition(body_stripped, _bare)
        if unwrapped:
            body_stripped = unwrapped
        # Detect an optional leading `where ...` clause used to tighten bounds.
        where_clause, body_stripped = _split_leading_where(body_stripped)
        out.append({
            "fn_name": canon_fn_name(fn_name),
            "block": block_header.strip(),
            "where_clause": where_clause,
            "body": body_stripped,
        })
    return out


def salvage_complete_blocks(text: str) -> str:
    """The longest prefix of a TRUNCATED reply that still ends on a closed
    element — everything up to and including the last `</FN>` / `</FILE>`.
    `""` when the reply closed nothing.

    Why this exists rather than dropping a cut reply whole. The forced
    followup writes the reply for 1 595 of the 1 596 followups on record,
    so a rule that discards a cut followup discards the fill. A truncation
    loses the LAST element and nothing before it: a 25-unit write list cut
    after the 21st `</FN>` is 21 usable bodies plus a fragment, and
    :func:`extract_fn_bodies` already drops the fragment. Keeping the closed
    prefix is what the main stream has always effectively done (its
    `llm/length_truncated` guard logs and splices); this makes the followup
    path agree instead of being the strict one.

    Conservative on purpose: the cut is taken at a WELL-FORMED closer even
    though :func:`_find_fn_blocks` tolerates malformed ones, because the
    element a truncation damaged is exactly the one whose closer is likely
    malformed. A reply that closed nothing yields `""` — a mid-body cut of
    a single element has no complete answer in it, and splicing half a body
    is the failure this salvage must not reintroduce.
    """
    t = text or ""
    if not t:
        return ""
    ends = []
    for closer in ("</FN>", "</FILE>"):
        i = t.rfind(closer)
        if i != -1:
            ends.append(i + len(closer))
    return t[:max(ends)] if ends else ""


def fn_tag_occurrences(text: str) -> int:
    """How many `<FN` tags the reply CONTAINS, parseable or not.

    Lets a caller's log line separate a model that said nothing from one whose
    answer was not understood. Both used to print `no FN blocks`, and the two
    call for opposite responses: ask again vs. fix the reader."""
    return (text or "").count("<FN")



@dataclass(frozen=True, slots=True)
class ReaderLoss:
    """A reply that carried more `<FN` tags than the reader turned into
    records — the standing rule being that N tags in with 0 records out is
    ALWAYS the reader, never the model.

    `response_sha` is the key of the content-addressed reply store, computed
    the same way the transport computes it, so a consumer of the event can open
    the exact artefact instead of re-deriving it from a timestamp. That is the
    whole point of carrying it: six reader shapes have been found since
    2026-08-04 (the empty `block`, the `&lt;`-escaped header, the `&gt;`
    terminator, the self-closed tag, the `)`-closed generic list, the
    unterminated open tag) and each one cost an investigation because the reply
    that would have named it was not identified in the log.

    PARTIAL loss counts. Every drop site used to ask only `if not parsed`, so a
    7-tag reply yielding 1 record was indistinguishable from a clean one — and
    partial loss is most of the recorded instances.
    """

    tags: int
    records: int
    response_sha: str

    @property
    def lost(self) -> int:
        return self.tags - self.records


def reader_loss(text: str, records: Sequence[Any]) -> ReaderLoss | None:
    """`ReaderLoss` when `text` carried more FN tags than `records` holds, else
    None.

    The count is a tag count, not a body count, so it is an upper bound on two
    edges: a model that emits the same `<FN name=…>` twice is reported as one
    lost, and prose that quotes the tag syntax (a retrospective GUIDANCE string
    naming `<FN ...>`) reads as a tag. Both are rare in a fill reply and both
    fail LOUD — a false alarm names an artefact that can be opened and
    dismissed in one command, while the silence it replaces cost 79 bodies."""
    tags = fn_tag_occurrences(text)
    if tags <= len(records):
        return None
    return ReaderLoss(
        tags=tags, records=len(records),
        response_sha=hashlib.sha256(
            (text or "").encode("utf-8", "replace")).hexdigest())

def bind_headerless_names(records: list[dict[str, Any]],
                          member_names: list[str]) -> list[dict[str, Any]]:
    """Give a fn identity to records that arrived without one.

    A reply whose name slot held a CONTAINER header names no function — the
    model answered with the prompt's container instead of its member. When
    the prompt asked about exactly ONE member the answer is that member's;
    there is nothing else it could be. With two or more targets the record is
    left alone: a visible `fn_not_found` beats a coin flip."""
    if len(member_names) != 1:
        return records
    only = member_names[0]
    return [r if _is_bare_ident(str(r.get("fn_name", "")))
            else {**r, "fn_name": only}
            for r in records]
