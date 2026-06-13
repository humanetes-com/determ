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
    import re as _re
    cleaned = _re.sub(r'<think>.*?</think>\s*', '', text, flags=_re.DOTALL).strip()
    fence = _re.search(r'```[a-zA-Z0-9_-]*\n(.*?)```', cleaned, flags=_re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return cleaned


def strip_think_blocks(text: str) -> str:
    """Remove `<think>…</think>` reasoning blocks some models leak into output
    (caller strips surrounding whitespace as needed)."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)


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
