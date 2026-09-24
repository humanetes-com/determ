"""Unit tests for the guarded LLM client (`pipeline_core.adapters.ask_client`)
relocated from pipeline.ask. Fully fake-injected — no model, no network.

Covers: request/response pairing, the network-retry loop (retry-then-success and
exhaustion), non-network error pairing, the BaseException unpaired-terminal
finally, the budget-high guard, and trim-event forwarding.
"""
from __future__ import annotations

import pytest

from pipeline_core.adapters.ask_client import AskClient


def _client(ask_raw, **over):
    events: list = []
    guards: list = []
    base = {
        "ask_raw": ask_raw,
        "hot_reload": lambda: None,
        "emit": lambda ev, **k: events.append((ev, k)),
        "guard": lambda tag, msg: guards.append(tag),
        "store_prompt_content": lambda s: {"sha256": "sha"},
        "caller_site": lambda: "test",
        "disclaimer": lambda: "DISCLAIMER",
        "model_name": "m",
        "context_chars": 1_000_000,
        "single_call_char_limit": 1_000_000,
    }
    base.update(over)
    return AskClient(**base), events, guards


def _names(events):
    return [e for e, _ in events]


def test_success_pairs_request_response():
    c, events, _ = _client(lambda *a, **k: "ok")
    assert c.ask("hi") == "ok"
    assert _names(events) == ["llm.request", "llm.response"]
    assert all(k["data"]["prompt_sha"] == "sha" for _, k in events)


def test_disclaimer_prepended_to_system():
    seen = {}
    def raw(prompt, system=None, **k):
        seen["system"] = system
        return "ok"
    c, _, _ = _client(raw)
    c.ask("hi", system="SYS")
    assert seen["system"].startswith("DISCLAIMER")
    assert "SYS" in seen["system"]


def test_exception_pairs_request_error():
    def boom(*a, **k):
        raise ValueError("boom")
    c, events, _ = _client(boom)
    with pytest.raises(ValueError, match="boom"):
        c.ask("hi")
    assert _names(events) == ["llm.request", "llm.error"]


class ConnectTimeout(Exception):  # name contains a network word → triggers retry
    pass


def test_network_error_retries_then_succeeds(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_: None)
    calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectTimeout("transient")
        return "ok"
    c, events, guards = _client(flaky)
    assert c.ask("hi") == "ok"
    assert calls["n"] == 2
    assert "ask/network_retry" in guards
    assert _names(events) == ["llm.request", "llm.response"]


def test_network_error_exhausts_to_terminal_error(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_: None)
    def always(*a, **k):
        raise ConnectTimeout("down")
    c, events, _ = _client(always)
    with pytest.raises(ConnectTimeout):
        c.ask("hi")
    assert _names(events) == ["llm.request", "llm.error"]


# --- temperature threads through to the transport ---

def test_temperature_forwarded_to_transport_when_set():
    seen = {}
    def raw(prompt, system=None, **k):
        seen.update(k)
        return "ok"
    c, _, _ = _client(raw)
    assert c.ask("hi", temperature=0.1) == "ok"
    assert seen.get("temperature") == 0.1


def test_temperature_absent_when_none_keeps_request_byte_identical():
    # When temperature is None the key must NOT be sent, so an unsafe-fn-free
    # request is byte-identical to the previous behaviour (no cassette MISS).
    seen = {}
    def raw(prompt, system=None, **k):
        seen.update(k)
        return "ok"
    c, _, _ = _client(raw)
    c.ask("hi")
    assert "temperature" not in seen


def test_base_exception_emits_unpaired_terminal():
    class Abort(BaseException):
        pass
    def abort(*a, **k):
        raise Abort()
    c, events, _ = _client(abort)
    with pytest.raises(Abort):
        c.ask("hi")
    # request, then the finally's unpaired terminal error
    assert _names(events) == ["llm.request", "llm.error"]
    assert events[-1][1]["data"]["error_type"] == "Unpaired"


def test_budget_high_guard_fires():
    c, _, guards = _client(lambda *a, **k: "ok", single_call_char_limit=100)
    c.ask("x" * 200)  # well over 90% of 100
    assert "prompt/budget_high" in guards


def test_trim_events_forwarded_to_guard():
    # tiny context forces the guard to trim and forward an event.
    c, _, guards = _client(lambda *a, **k: "ok", context_chars=2_000)
    c.ask("z" * 10_000)
    assert any(g.startswith("context-guard/") for g in guards)


# --- followup_think threads through to the transport only when set ---

def test_followup_think_forwarded_only_when_true():
    # The default call must stay byte-identical: `ask_raw` is late-bound and a
    # fake standing in for it may not take the keyword at all.
    seen = {}
    def raw(prompt, system=None, **k):
        seen.clear()
        seen.update(k)
        return "ok"
    c, _, _ = _client(raw)
    c.ask("hi")
    assert "followup_think" not in seen
    c.ask("hi", followup_think=False)
    assert "followup_think" not in seen
    c.ask("hi", followup_think=True)
    assert seen.get("followup_think") is True


def test_followup_think_false_keeps_a_keywordless_fake_working():
    def raw(prompt, system=None, model=None, max_tokens=None, think=None, temperature=None):
        return "ok"
    c, _, _ = _client(raw)
    assert c.ask("hi", followup_think=False) == "ok"


# ── the contents index opens a request-capable prompt, read off what is sent ──

from pipeline_core.domain.prompt_holdings import (  # noqa: E402
    CONTENTS_OPEN,
    FileView,
    contents_index,
    file_block,
)

_MENU = '\nYou may reply <REQUEST kind="file" arg="src/x.rs"/>.'


def test_a_request_capable_prompt_is_sent_opened_by_its_index():
    sent: list[str] = []
    c, _, _ = _client(lambda p, *a, **k: sent.append(p) or "ok",
                      contents=contents_index)
    prompt = file_block("src/a.rs", "fn a() {}", view=FileView.WHOLE) + _MENU
    c.ask(prompt)
    assert sent[0].startswith(CONTENTS_OPEN) and sent[0].endswith(prompt)
    assert "src/a.rs — the whole file" in sent[0]


def test_no_index_without_a_request_menu_or_without_the_hook():
    """Negative controls: a prompt that cannot request, and the pre-change
    client (`contents=None`, `PROMPT_CONTENTS_INDEX=0`), send it unchanged."""
    prompt = file_block("src/a.rs", "fn a() {}", view=FileView.WHOLE)
    for over, text in (({"contents": contents_index}, prompt),
                       ({}, prompt + _MENU)):
        sent: list[str] = []
        c, _, _ = _client(lambda p, *a, _s=sent, **k: _s.append(p) or "ok", **over)
        c.ask(text)
        assert sent == [text]


def test_the_index_never_lists_a_block_the_guard_dropped():
    """The index is read off the FITTED prompt: a middle file block the
    context guard removed is not listed, and room for the index was reserved
    before fitting so the sent prompt still fits."""
    blocks = "".join(
        "\n" + file_block(f"src/{n}.rs", "x" * 3000, view=FileView.WHOLE)
        for n in ("first", "middle", "last"))
    sent: list[str] = []
    c, _, guards = _client(lambda p, *a, **k: sent.append(p) or "ok",
                           contents=contents_index, context_chars=8_500)
    c.ask("task" + blocks + _MENU)
    assert "context-guard/section_removal" in guards
    assert "src/middle.rs" not in sent[0]
    assert "src/first.rs —" in sent[0] and "src/last.rs —" in sent[0]
    assert len(sent[0]) + len("DISCLAIMER") <= 8_500


def test_a_session_indexes_its_first_user_turn_only():
    got: list = []
    c, _, _ = _client(lambda *a, **k: "ok", contents=contents_index,
                      ask_turns_raw=lambda turns, **k: got.append(turns) or "ok")
    turn0 = file_block("src/a.rs", "fn a() {}", view=FileView.WHOLE) + _MENU
    c.ask_turns([{"role": "user", "content": turn0},
                 {"role": "assistant", "content": "<REQUEST kind=\"file\" arg=\"src/a.rs\"/>"},
                 {"role": "user", "content": "answer" + _MENU}])
    turns = got[0]
    assert turns[1]["content"].startswith(CONTENTS_OPEN)
    assert turns[3]["content"] == "answer" + _MENU
