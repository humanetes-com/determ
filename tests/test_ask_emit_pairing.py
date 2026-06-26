"""Debt #6: every pipeline.ask() request must get a terminal llm event on its
thread (response or error), carrying the same prompt_sha — so the flow-audit C10
parallel-stream check can never see a 'vanished response'. Covers the success,
Exception, and BaseException (the gap the old code missed) paths.
"""
from __future__ import annotations

import pytest

import pipeline


@pytest.fixture
def captured(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(pipeline, "_emit", lambda ev, **k: events.append((ev, k)))
    monkeypatch.setattr(pipeline, "_store_prompt_content", lambda s: {"sha256": "abc123"})
    monkeypatch.setattr(pipeline, "_hot_reload_mi", lambda: None)
    return events


def _names(events):
    return [e for e, _ in events]


def test_success_pairs_request_response(captured, monkeypatch):
    monkeypatch.setattr(pipeline, "_ask_raw", lambda *a, **k: "ok")
    assert pipeline._ASK_CLIENT.ask("hi") == "ok"
    assert _names(captured) == ["llm.request", "llm.response"]
    assert all(k["data"]["prompt_sha"] == "abc123" for _, k in captured)


def test_exception_pairs_request_error(captured, monkeypatch):
    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(pipeline, "_ask_raw", boom)
    with pytest.raises(ValueError, match="boom"):
        pipeline._ASK_CLIENT.ask("hi")
    assert _names(captured) == ["llm.request", "llm.error"]
    assert all(k["data"]["prompt_sha"] == "abc123" for _, k in captured)


def test_base_exception_still_emits_terminal(captured, monkeypatch):
    # A BaseException (not Exception) used to escape after the request emit with
    # no paired terminal event → C10 'vanished response'. The finally guarantees one.
    class _Abort(BaseException):
        pass

    def abort(*a, **k):
        raise _Abort()

    monkeypatch.setattr(pipeline, "_ask_raw", abort)
    with pytest.raises(_Abort):
        pipeline._ASK_CLIENT.ask("hi")
    names = _names(captured)
    assert names[0] == "llm.request"
    assert names[-1] == "llm.error"  # terminal event guaranteed by finally
    assert captured[-1][1]["data"]["error_type"] == "Unpaired"
