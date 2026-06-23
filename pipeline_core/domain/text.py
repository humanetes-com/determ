"""Pure text helpers — LLM-output cleanup + prompt-budget truncation (pure domain).

Relocated verbatim from pipeline.py (slice 5b foundation). No I/O, no pipeline deps — so
tasks import them instead of carrying them in a deps record. The regex here operates on
LLM response text / generic strings (fence stripping, think-block removal), not Rust
source — source parsing stays on the syn extractor. Bodies moved as-is; no new patterns.
"""

from __future__ import annotations

import re


def strip_markdown_fences(content: str) -> str:
    """Remove markdown code fences that models sometimes include in file content.

    Handles three cases:
    1. Opening fence at start:  ```rust\\ncode\\n```  →  code
    2. Closing fence at end (after opening already stripped)
    3. Orphaned standalone fence lines anywhere in the content (e.g. the model
       wrapped only part of the output, leaving a stray ``` in the middle).
    Rust doc-comment fences (/// ```) start with slashes so are unaffected.
    """
    content = content.strip()
    # Strip opening fence: ```lang or ``` on its own line at the very start
    content = re.sub(r'^```[a-zA-Z0-9_-]*\s*\n', '', content)
    # Strip closing fence at the very end
    content = re.sub(r'\n```\s*$', '', content)
    # Strip any remaining standalone fence lines anywhere in the file
    # (lines that are exactly ``` or ```lang with optional trailing whitespace)
    content = re.sub(r'^```[a-zA-Z0-9_-]*\s*$', '', content, flags=re.MULTILINE)
    return content.strip()


def _is_fence_lang_char(c: str) -> bool:
    """A ``[a-zA-Z0-9_-]`` fence-info-string char (ASCII)."""
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c in "_-"


def _remove_think_blocks(text: str, *, eat_trailing_ws: bool = False) -> str:
    """Remove every ``<think>…</think>`` span (non-greedy, DOTALL) — byte-equivalent
    to ``re.sub(r'<think>.*?</think>', '')`` (or ``…</think>\\s*`` when
    `eat_trailing_ws`). A ``<think>`` with no closing tag is left as-is."""
    out: list[str] = []
    n = len(text)
    i = 0
    while True:
        k = text.find("<think>", i)
        if k == -1:
            out.append(text[i:])
            break
        close = text.find("</think>", k + 7)
        if close == -1:  # no close → .*? can't match → keep the rest verbatim
            out.append(text[i:])
            break
        out.append(text[i:k])
        i = close + 8
        if eat_trailing_ws:
            while i < n and text[i].isspace():  # trailing \s*
                i += 1
    return "".join(out)


def _first_fenced_body(text: str) -> str | None:
    """Body of the first ```` ```[lang]\\n … ``` ```` fence — byte-equivalent to the
    group(1) of ``re.search(r'```[a-zA-Z0-9_-]*\\n(.*?)```', text, re.DOTALL)``."""
    n = len(text)
    i = 0
    while (k := text.find("```", i)) >= 0:
        p = k + 3
        while p < n and _is_fence_lang_char(text[p]):  # [a-zA-Z0-9_-]*
            p += 1
        if p < n and text[p] == "\n":
            body_start = p + 1
            close = text.find("```", body_start)
            if close != -1:
                return text[body_start:close]
        i = k + 1
    return None


def strip_simple_code_fence(txt: str) -> str:
    """If `txt` is fenced (starts with ``` ``` ```), strip its leading ```` ```lang\\n ````
    opener and trailing ```` \\n``` ```` closer; else return it unchanged. Byte-equivalent
    to the guarded ``if txt.startswith('```'): re.sub(r'^```[a-zA-Z]*\\n?','') ;
    re.sub(r'\\n?```\\s*$','')`` used to unwrap fenced JSON manifests."""
    if not txt.startswith("```"):
        return txt
    n = len(txt)
    p = 3  # ^```[a-zA-Z]*\n?
    while p < n and (("a" <= txt[p] <= "z") or ("A" <= txt[p] <= "Z")):
        p += 1
    if p < n and txt[p] == "\n":
        p += 1
    txt = txt[p:]
    t = txt.rstrip()  # \n?```\s*$
    if t.endswith("```"):
        bt = len(t) - 3
        txt = t[:bt - 1] if bt > 0 and t[bt - 1] == "\n" else t[:bt]
    return txt


def extract_code_content(text: str) -> str:
    """Strip thinking blocks and outermost code fence, return bare code content.

    Used as last-resort fallback when the model ignores <FILE> markers.
    Works for whole-file extraction (TASK 2 source files, genesis, etc.)
    where brace-counting is not appropriate.
    Steps:
      1. Remove <think>...</think> blocks (compat path leaks these as content).
      2. Find the first code fence (```[lang]\\n...```) and return its body.
      3. If no fence, return the full text stripped of leading/trailing whitespace.
    """
    cleaned = _remove_think_blocks(text, eat_trailing_ws=True).strip()
    body = _first_fenced_body(cleaned)
    return body.strip() if body is not None else cleaned


def strip_think_blocks(text: str) -> str:
    """Remove `<think>…</think>` reasoning blocks some models leak into output
    (caller strips surrounding whitespace as needed)."""
    return _remove_think_blocks(text)


def trim_to_line(text: str, cap: int, marker: str = "\n// ... (truncated)") -> str:
    """Truncate text at the last newline before cap — never mid-line. FR-020."""
    if len(text) <= cap:
        return text
    cut = text.rfind("\n", 0, cap)
    return text[:cut if cut != -1 else cap] + marker


def smart_truncate(text: str, total_limit: int = 8000, head: int = 2000, tail: int = 6000) -> str:
    """
    Keep the beginning (build errors, first failures) and end (summary) of
    output. Truncating from the middle avoids losing either signal.
    """
    if len(text) <= total_limit:
        return text
    omitted = len(text) - head - tail
    return text[:head] + f"\n\n[... {omitted:,} chars omitted ...]\n\n" + text[-tail:]


def strip_list_marker(line: str) -> str:
    """Strip a leading list/enumeration marker from `line` (regex-free, R7).

    Removes a leading run of `-`, `*`, or digits, an optional single `.`, `)`,
    or `:`, then any following whitespace — the shapes the LLM uses when it
    returns a file list as a bulleted/numbered list (`- src/x.rs`, `1. x.rs`,
    `2) x.rs`). Lines without such a prefix (`src/lib.rs`) are returned
    unchanged. Equivalent to the former `re.sub(r'^[\\-\\*\\d]+[\\.\\):]?\\s*', '', line)`.
    """
    n = len(line)
    j = 0
    while j < n and (line[j] in "-*" or line[j].isdigit()):
        j += 1
    if j == 0:
        return line  # no marker run → unchanged
    if j < n and line[j] in ".):":
        j += 1
    while j < n and line[j].isspace():
        j += 1
    return line[j:]


def tagged_path_block_spans(text: str, tag: str) -> list[tuple[int, int]]:
    """`(start, end)` spans of each `\\n<tag path="…">…</tag>` block, in order
    (regex-free, R7; mirrors `\\n<tag path="[^"]*">.*?</tag>` with DOTALL —
    non-greedy: each block ends at its nearest `</tag>`)."""
    open_lead = f'\n<{tag} path="'
    close = f"</{tag}>"
    spans: list[tuple[int, int]] = []
    i = 0
    while (s := text.find(open_lead, i)) >= 0:
        q = text.find('"', s + len(open_lead))     # [^"]* then closing quote
        if q < 0:
            break
        if text[q + 1:q + 2] != ">":                # require `">` immediately after
            i = s + len(open_lead)
            continue
        e = text.find(close, q + 2)
        if e < 0:
            break
        spans.append((s, e + len(close)))
        i = e + len(close)
    return spans


def sibling_comment_block_spans(text: str) -> list[tuple[int, int]]:
    """`(start, end)` spans of each `\\n// --- <name> ---\\n<body>` block, where the
    body runs (non-slash) up to the next `\\n// ---` header or end-of-text
    (regex-free, R7; mirrors `\\n// --- [^\\n]+ ---\\n[^/]*?(?=\\n// ---|$)`)."""
    header = "\n// --- "
    spans: list[tuple[int, int]] = []
    i = 0
    while (s := text.find(header, i)) >= 0:
        nl = text.find("\n", s + len(header))       # end of the `// --- name ---` line
        # the header line must contain a closing ` ---`
        line = text[s + 1:nl] if nl >= 0 else text[s + 1:]
        if not (line.startswith("// --- ") and line.rstrip().endswith(" ---")):
            i = s + len(header)
            continue
        body_start = nl + 1 if nl >= 0 else len(text)
        # body = [^/]*? up to the next `\n// ---` header or end ($). The `[^/]`
        # means a body containing `/` can never reach the lookahead → no match
        # (faithful to the original, which thus rarely fires on real Rust).
        j = body_start
        n = len(text)
        matched = False
        while j < n:
            if text.startswith("\n// ---", j):
                matched = True
                break
            if text[j] == "/":
                break
            j += 1
        else:
            matched = True  # ran to end-of-text ($)
        if matched:
            spans.append((s, j))
        i = j if j > body_start else s + len(header)
    return spans


def strip_trailing_file_tag(text: str) -> str:
    """Strip a trailing `</FILE>` tag (with surrounding whitespace) and `.strip()`
    the result (regex-free, R7). Equivalent to `re.sub(r'\\s*</FILE>\\s*$', '', text)`
    then `.strip()`: only the single trailing tag at end-of-text is removed."""
    tag = "</FILE>"
    stripped = text.rstrip()              # \s*$
    if stripped.endswith(tag):
        stripped = stripped[:-len(tag)]   # the \s* before the tag is handled by .strip()
    return stripped.strip()
