"""R7 regression tests for monitor.py's render-half text helpers (ANSI strip,
pipeline_state done-key, CALL_START marker) — each pinned to the regex it replaced."""
from __future__ import annotations

import re

import monitor as m


def test_plain_strips_ansi_like_old_regex() -> None:
    old = lambda s: re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', s)  # noqa: E731
    for s in ["\033[31mred\033[0m", "plain", "\033[1;32mx\033[0my\033[m",
              "\033[bad", "a\033[99Hb"]:
        assert m._plain(s) == old(s), repr(s)


def test_state_done_key_matches_old_regex() -> None:
    old_re = re.compile(r"\[([^\]/]+)(?:/[^\]]*)?\]\s+done\b")

    def old(line):
        mm = old_re.match(line)
        return mm.group(1) if mm else None

    for line in ["[stable] done\n", "[task0c/nightly]   done extra", "[x] donezo",
                 "[a/b/c] done", "[only]done", "no bracket", "[ ] done", "[k]\tdone"]:
        assert m._state_done_key(line) == old(line), repr(line)


def test_call_start_value_matches_old_regex() -> None:
    old_re = re.compile(r'\[CALL_START: ([\d.]+)\]')

    def old(c):
        mm = old_re.search(c)
        return mm.group(1) if mm else None

    for c in ["[CALL_START: 12.34]", "x [CALL_START: 5] y", "[CALL_START: ]",
              "[CALL_START: 1.2.3]", "no marker", "[CALL_START: abc]", "[CALL_START: 9"]:
        assert m._call_start_value(c) == old(c), repr(c)
