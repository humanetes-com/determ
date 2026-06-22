"""Unit tests for the test-command discovery domain (`pipeline_core.domain.test_command`).

Pins the P6 R7 migration: the structured `backtick_spans` char-scan replaces the old
``re.finditer(r'`([^`]+)`', text)`` backtick scan. The `_old_regex_spans` reference here
is the EXACT original regex — the equivalence test proves byte-for-byte parity so the
relocation is behaviour-preserving.
"""
from __future__ import annotations

import re

from pipeline_core.domain.test_command import (
    backtick_spans,
    extract_command_from_response,
    looks_like_command,
    normalize_bare_tool,
)

_OLD_RE = re.compile(r'`([^`]+)`')


def _old_regex_spans(text: str) -> list[str]:
    return [m.group(1) for m in _OLD_RE.finditer(text)]


def test_backtick_spans_match_old_regex():
    cases = [
        "run `cargo test` to test",
        "no backticks here",
        "empty `` pair then `npm test`",
        "`leading` and `trailing`",
        "unterminated `cargo test without close",
        "```\nfenced\n```",
        "adjacent ``code``together",
        "`a``b`",
        "",
        "`",
        "``",
        "```",
    ]
    for text in cases:
        assert backtick_spans(text) == _old_regex_spans(text), text


def test_extract_prefers_known_prefix_in_backticks():
    assert extract_command_from_response("Use `cargo test` to run.") == "cargo test"
    assert extract_command_from_response("Run `$ pytest`.") == "pytest"
    assert extract_command_from_response("Path `/usr/bin/pytest`.") == "/usr/bin/pytest"


def test_extract_falls_back_to_first_line_after_colon():
    assert extract_command_from_response("The command is: cargo test") == "cargo test"
    assert extract_command_from_response("$ make check") == "make check"
    assert extract_command_from_response("Run the suite with pytest -q") == "Run the suite with pytest -q"
    assert extract_command_from_response("") == ""


def test_looks_like_command():
    assert looks_like_command("cargo test")
    assert looks_like_command("./scripts/test.sh")
    assert looks_like_command("/usr/bin/pytest")
    assert not looks_like_command("just some prose words")


def test_normalize_bare_tool():
    assert normalize_bare_tool("cargo") == "cargo test"
    assert normalize_bare_tool("  NPM ") == "npm test"
    assert normalize_bare_tool("cargo test") is None
    assert normalize_bare_tool("pytest") is None
