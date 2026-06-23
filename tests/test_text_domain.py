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
