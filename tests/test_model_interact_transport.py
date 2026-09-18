"""Unit tests for model_interact transport helpers (regex→str-ops migration)."""
from __future__ import annotations

import re

from model_interact import _parse_input_token_overflow

_OLD_RE = re.compile(r'prompt contains at least (\d+) input tokens')


def _old(msg: str) -> int | None:
    m = _OLD_RE.search(msg)
    return int(m.group(1)) if m else None


def test_overflow_parses_token_count():
    msg = "This model's maximum context length: prompt contains at least 41234 input tokens, however..."
    assert _parse_input_token_overflow(msg) == 41234


def test_overflow_none_when_absent():
    assert _parse_input_token_overflow("some unrelated 400 BadRequest error") is None


def test_overflow_matches_old_regex():
    samples = [
        "prompt contains at least 8192 input tokens",
        "blah prompt contains at least 100 input tokens blah",
        "prompt contains at least 500 foo tokens",      # wrong tail → None (both)
        "prompt contains at least input tokens",        # no digits → None
        "prompt contains at least 12abc input tokens",  # digits then non-tail char
        "",
        "PROMPT CONTAINS AT LEAST 99 INPUT TOKENS",     # case-sensitive → None (both)
    ]
    for s in samples:
        assert _parse_input_token_overflow(s) == _old(s), s


def test_ask_threads_followup_think_to_the_controller(monkeypatch):
    """`ask` is the late-bound `ask_raw` behind `AskClient`; the flag has to
    reach `LLMController.run` by keyword, and the default has to be the
    pre-existing call (followup_think=False) so every other caller is
    byte-identical."""
    import model_interact as mi
    seen: dict = {}

    class Ctl:
        def run(self, backend, **kw):
            seen.clear()
            seen.update(kw)
            return "ok"

    monkeypatch.setattr(mi, "_controller", Ctl())
    monkeypatch.setattr(mi, "_backend", object())
    assert mi.ask("p") == "ok"
    assert seen["followup_think"] is False
    mi.ask("p", followup_think=True)
    assert seen["followup_think"] is True
