"""Unit tests for pipeline_core.llm.cleanup — chat-boundary / think-token strip."""
from __future__ import annotations

from pipeline_core.llm import cleanup


def test_strip_think_removes_pair():
    assert cleanup.strip_think("a<think>hidden</think>b") == "ab"


def test_clean_text_unchanged_no_events():
    out, events = cleanup.strip_chat_boundaries("plain answer", model="qwen")
    assert out == "plain answer"
    assert events == []


def test_role_echo_truncated_with_event():
    text = "real answer<|endoftext|><|im_start|>user echoed prompt back"
    out, events = cleanup.strip_chat_boundaries(text, model="qwen")
    assert out == "real answer"
    assert len(events) == 1
    assert events[0][0] == "compat/endoftext_role_echo"


def test_numbered_alternatives_truncated_with_event():
    text = "real answer<|endoftext|><|im_start|> 1. alt one 2. alt two"
    out, events = cleanup.strip_chat_boundaries(text, model="qwen")
    assert out == "real answer"
    assert len(events) == 1
    assert "numbered alternatives" in events[0][1]


def test_bare_tokens_stripped_with_event():
    text = "<|im_start|>system\nanswer body<|endoftext|>"
    out, events = cleanup.strip_chat_boundaries(text, model="qwen")
    assert "<|im_start|>" not in out and "<|endoftext|>" not in out
    assert "answer body" in out
    assert len(events) == 1
    assert events[0][0] == "compat/endoftext_stripped"


def test_think_stripped_inside_boundaries():
    out, events = cleanup.strip_chat_boundaries("<think>plan</think>final", model="qwen")
    assert out == "final"
    assert events == []
