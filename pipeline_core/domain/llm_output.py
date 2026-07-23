"""LLM-output extraction — parse <FILE> blocks + genesis content from model responses.

Relocated verbatim from pipeline.py (slice 5b foundation). The regex here operates on
model *response text* (the <FILE path="…"> protocol, markdown artefacts, genesis
delimiters), not Rust source — source parsing stays on the syn extractor. Logging goes
through the :mod:`pipeline_core.log` facade; the genesis-format text helper
`extract_code_content` is imported from :mod:`pipeline_core.domain.text`.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .. import log as _log
from .rust_fn_headers import unwrap_own_fn_definition
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


def _read_quoted_attr(text: str, pos: int, name: str) -> tuple[str, int] | None:
    """If `text[pos:]` opens with `name="<value>"` (value = `[^"]+`, i.e. ≥1
    non-quote char), return (value, pos-after-closing-quote); else None.

    The value is HTML-unescaped: models emit XML-safe attribute values for
    generic headers (`block="impl&lt;T, E&gt; Context&lt;T, E&gt; …"`), which
    would never match a real container (run 346122: every file-first body for
    src/context.rs dropped on `&lt;`-escaped headers)."""
    lead = name + '="'
    if not text.startswith(lead, pos):
        return None
    v = pos + len(lead)
    end = text.find('"', v)
    if end in (-1, v):                       # need a closing quote AND ≥1 char
        return None
    return html.unescape(text[v:end]), end + 1


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


def _find_fn_close(text: str, start: int) -> tuple[int, int] | None:
    """Nearest `</FN\\s*>` at/after `start`. Returns (close_start, pos_after_`>`)."""
    j = start
    while True:
        c = text.find("</FN", j)
        if c == -1:
            return None
        k = _skip_ws(text, c + 4)
        if k < len(text) and text[k] == ">":
            return c, k + 1
        j = c + 1


def _find_fn_blocks(text: str) -> list[tuple[str, str, str]]:
    """Tolerant, regex-free replacement for
    `<FN\\s+name="([^"]+)"\\s+block="([^"]+)"\\s*>(.*?)</FN\\s*>` (DOTALL findall):
    (name, block, body) per well-formed block, in order. A malformed header or an
    unterminated block is skipped exactly as a regex non-match would skip it
    (retry at the next `<FN`); the body is the nearest-close `.*?` span."""
    out: list[tuple[str, str, str]] = []
    n = len(text)
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
        q = _skip_ws(text, p)
        if q == p:                           # `\s+` between attrs
            i = s + 1; continue
        block = _read_quoted_attr(text, q, "block")
        if block is None:
            i = s + 1; continue
        bl_val, p = block
        p = _skip_ws(text, p)                # `\s*` before `>`
        if p >= n or text[p] != ">":
            i = s + 1; continue
        body_start = p + 1
        close = _find_fn_close(text, body_start)
        if close is None:
            i = s + 1; continue
        body_end, after = close
        out.append((nm_val, bl_val, text[body_start:body_end]))
        i = after


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
