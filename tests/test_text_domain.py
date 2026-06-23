"""Byte-equivalence pins for the regex-free helpers in `pipeline_core.domain.text`
(P7 — think-block + code-fence stripping)."""
from __future__ import annotations

import re

from pipeline_core.domain.text import (
    extract_code_content,
    strip_simple_code_fence,
    strip_think_blocks,
)


def _old_strip_think(text: str) -> str:
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)


def _old_extract(text: str) -> str:
    cleaned = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
    m = re.search(r'```[a-zA-Z0-9_-]*\n(.*?)```', cleaned, flags=re.DOTALL)
    return m.group(1).strip() if m else cleaned


def _old_simple_fence(txt: str) -> str:
    if txt.startswith("```"):
        txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
        txt = re.sub(r'\n?```\s*$', '', txt)
    return txt


_THINK_CASES = [
    "before<think>reason</think>after", "<think>a</think><think>b</think>x",
    "no think here", "<think>unclosed", "x</think>y<think>z</think>",
    "<think>\nmulti\nline\n</think>  trailing", "", "<think></think>",
]


def test_strip_think_blocks_matches_regex():
    for t in _THINK_CASES:
        assert strip_think_blocks(t) == _old_strip_think(t), t


_FENCE_CASES = [
    "```rust\nfn f() {}\n```", "<think>x</think>\n```\ncode\n```", "plain text",
    "```py\nimport os\n```\ntail", "```nolang```", "  ```rust\nbody\n```  ",
    "```rust no newline```", "", "text without fence",
]


def test_extract_code_content_matches_regex():
    for t in _FENCE_CASES:
        assert extract_code_content(t) == _old_extract(t), t


_JSON_FENCE_CASES = [
    '```json\n{"a": 1}\n```', '```\n{"x": 2}\n```', '{"plain": true}',
    '```\n{}', '{}\n```', '```js{"y":3}```', '} ```', '\n```', '', '```json\n[]\n```  ',
]


def test_strip_simple_code_fence_matches_regex():
    for t in _JSON_FENCE_CASES:
        assert strip_simple_code_fence(t) == _old_simple_fence(t), t


# --- P7: more regex-free text helpers (strip_markdown_fences, blank-line/asterisk) ---

from pipeline_core.domain.text import (  # noqa: E402
    collapse_blank_lines,
    strip_markdown_fences,
    strip_surrounding_asterisks,
)


def _old_strip_md_fences(content: str) -> str:
    content = content.strip()
    content = re.sub(r'^```[a-zA-Z0-9_-]*\s*\n', '', content)
    content = re.sub(r'\n```\s*$', '', content)
    content = re.sub(r'^```[a-zA-Z0-9_-]*\s*$', '', content, flags=re.MULTILINE)
    return content.strip()


def test_strip_markdown_fences_matches_regex():
    cases = [
        "```rust\nfn f() {}\n```", "```\ncode\n```\n```\ntail\n```", "no fences here",
        "```py\nx\n```\n\n```\ny\n```", "```rust\nbody", "text\n```\nmore", "",
        "```\n\n\n```", "```lang\n```", "  ```rust\nz\n```  ",
    ]
    for t in cases:
        assert strip_markdown_fences(t) == _old_strip_md_fences(t), t


def test_collapse_blank_lines_matches_regex():
    for t in ["a\n\nb", "a\n \n \nb", "a\nb", "a\n\n\n\nb", "\n\n", "x\n\n ", "", "a\n \nb\n\nc"]:
        assert collapse_blank_lines(t) == re.sub(r'\n\s*\n+', '\n', t), t


def test_strip_surrounding_asterisks_matches_regex():
    for s in ["**bold**", "* item", "trailing *", "***", "no stars", "* * *", "",
              "*x", "x*", "**a* b**"]:
        assert strip_surrounding_asterisks(s) == re.sub(r'^\*+\s*|\s*\*+$', '', s), s
