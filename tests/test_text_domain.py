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


# --- P7: model-output text predicates (moved from pipeline.py) ---

from pipeline_core.domain.text import (  # noqa: E402
    block_is_prose,
    flags_unreachable,
    parse_attempt_number,
)


def test_block_is_prose_matches_regex():
    pat = re.compile(r"block to our Rust file|Here's how|You can add|here is", re.IGNORECASE)
    for t in ["Here's how to add it", "BLOCK TO OUR RUST FILE", "fn main() {}",
              "you can add this", "here IS the code", "", "no match prose"]:
        assert block_is_prose(t) == bool(pat.search(t)), t


def test_flags_unreachable_matches_regex():
    pat = re.compile(r'dead.code|unreachable|genuinely|not coverable|cannot be covered|grcov artifact',
                     re.IGNORECASE)
    for t in ["this is dead code", "dead_code lint", "dead\ncode", "UNREACHABLE branch",
              "genuinely impossible", "cannot be covered", "grcov artifact", "deadcode", "fine", ""]:
        assert flags_unreachable(t) == bool(pat.search(t)), t


def test_parse_attempt_number_matches_regex():
    for d in ["attempt 3", "fix attempt 12 of 5", "attempt", "attempt x", "no counter",
              "attempt 0", "", "attempt 7 attempt 9"]:
        m = re.search(r"attempt (\d+)", d)
        assert parse_attempt_number(d) == (int(m.group(1)) if m else None), d


# --- P7: llm_output tagged-block + fence extractors ---

from pipeline_core.domain.llm_output import (  # noqa: E402
    extract_response_code_fence,
    extract_tagged_block,
)


def test_extract_tagged_block_matches_regex():
    for tag in ("SIGNATURE", "BODY"):
        pat = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
        cases = [f"x<{tag}>inner</{tag}>y", f"<{tag}>a</{tag}><{tag}>b</{tag}>",
                 f"<{tag}>unclosed", "no tag", f"<{tag}></{tag}>", f"<{tag}>multi\nline</{tag}>"]
        for t in cases:
            m = pat.search(t)
            assert extract_tagged_block(t, tag) == (m.group(1) if m else None), (tag, t)


def _old_fence(r):
    for fence in (r"```rust\s*(.*?)```", r"```(?:rs)?\s*(.*?)```"):
        m = re.search(fence, r, re.DOTALL)
        if m:
            c = m.group(1).strip()
            if c:
                return c
    return None


def test_extract_response_code_fence_matches_regex():
    for t in ["```rust\nfn f(){}\n```", "```\ncode\n```", "```rs\nx\n```", "no fence",
              "```rust   \n  body  \n```", "```python\nx\n```", "", "text ```rust a```"]:
        assert extract_response_code_fence(t) == _old_fence(t), t
