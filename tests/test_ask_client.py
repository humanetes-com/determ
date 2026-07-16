"""Unit tests for the guarded LLM client (`pipeline_core.adapters.ask_client`)
relocated from pipeline.ask in P6. Fully fake-injected — no model, no network.

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
