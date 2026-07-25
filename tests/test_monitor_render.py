"""R7 regression tests for monitor.py's render-half text helpers (ANSI strip,
pipeline_state done-key, CALL_START marker) — each pinned to the regex it replaced."""
from __future__ import annotations

import re

import monitor as m
from pipeline_core.monitor import render as _render


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


def test_driver_counter_prefers_the_live_outer_loop() -> None:
    """`pass=?/?` was rendered for hours on healthy tdd runs: the per-fn engine
    emits no pass.start once the frontier is all re-fills. The tdd iter wins."""
    tdd = {"driver": "tdd", "tdd_iter": 4, "tdd_stall": 2, "tdd_max_stalls": 3,
           "pass_n": None, "max_passes": None}
    assert _render.driver_counter(tdd) == "iter=4  stall=2/3"
    # Budget omitted until an iter event carries it (older runs) — never "?/?".
    assert _render.driver_counter({**tdd, "tdd_max_stalls": None}) == "iter=4"
    # An unreported iter shows "?" — a fabricated "iter=1" would be the same
    # class of lie as the "?/?" this replaced.
    assert _render.driver_counter({"driver": "tdd", "tdd_iter": 0}) == "iter=?"
    per_fn = {"driver": "per-fn", "pass_n": 2, "max_passes": 3}
    assert _render.driver_counter(per_fn) == "pass=2/3"
    assert _render.driver_counter({}) == "pass=?/?"
