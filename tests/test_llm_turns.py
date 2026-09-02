"""A session transcript reaches the model as one message list, with the
model's own previous answer and the harness's result as turns — the
property the stateless fill route lacked (run `3003-93e9a9bf`, 2026-09-02:
four independent 88K-char documents for one two-fn write, the same
private-field diagnostic on three of them, none carrying the previous
reply).
"""
from __future__ import annotations

from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.domain.text import turns_digest_text
from pipeline_core.llm import Hooks, LLMController
from pipeline_core.llm.events import Delta
from tests.test_llm_controller import SENTINEL, FakeTransport


def test_run_turns_streams_the_transcript_verbatim_and_digests_it() -> None:
    emitted: list[tuple[str, dict]] = []
    hooks = Hooks(emit=lambda ev, data: emitted.append((ev, data)))
    t = FakeTransport([[Delta(content="<FN name=\"a\" block=\"\">2</FN>")]],
                      finish_reasons=["stop"], thinking=False)
    turns = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "turn 0"},
        {"role": "assistant", "content": "<FN name=\"a\" block=\"\">1</FN>"},
        {"role": "user", "content": "RESULT: did not compile"},
    ]
    out = LLMController(hooks, default_sentinel=SENTINEL).run_turns(
        t, messages=turns, model="m", max_tokens=100)
    assert out == "<FN name=\"a\" block=\"\">2</FN>"
    # Every turn reached the transport in order, the prior answer included.
    assert t.calls[0]["messages"] == turns
    # The provenance event keys on the canonical digest of the whole list.
    metas = [d for ev, d in emitted if ev == "llm.stream_meta"]
    assert metas and metas[0]["prompt_sha"]
    from pipeline_core.domain.text import PromptDigest
    assert metas[0]["prompt_sha"] == PromptDigest.of(turns_digest_text(turns)).sha256
    # The caller's list is not mutated.
    assert turns[-1]["content"] == "RESULT: did not compile"


def test_a_different_earlier_turn_digests_differently() -> None:
    a = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "1"},
         {"role": "user", "content": "r"}]
    b = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "2"},
         {"role": "user", "content": "r"}]
    assert turns_digest_text(a) != turns_digest_text(b)


def _client(raw_turns, **over):
    events: list = []
    base = {
        "ask_raw": lambda *a, **k: "unused",
        "hot_reload": lambda: None,
        "emit": lambda ev, **k: events.append((ev, k)),
        "guard": lambda tag, msg: None,
        "store_prompt_content": lambda s: {"sha256": "sha" + str(len(s))},
        "caller_site": lambda: "test",
        "disclaimer": lambda: "DISCLAIMER",
        "model_name": "m",
        "context_chars": 1_000_000,
        "single_call_char_limit": 1_000_000,
        "ask_turns_raw": raw_turns,
    }
    base.update(over)
    return AskClient(**base), events


def test_ask_turns_prefixes_the_disclaimer_and_pairs_request_with_response() -> None:
    seen: dict = {}

    def raw(messages, **k):
        seen["messages"] = messages
        return "reply"

    c, events = _client(raw)
    turns = [{"role": "user", "content": "turn 0"}]
    assert c.ask_turns(turns, model="m") == "reply"
    assert seen["messages"][0] == {"role": "system", "content": "DISCLAIMER"}
    assert seen["messages"][1] == turns[0]
    names = [e for e, _ in events]
    assert names == ["llm.request", "llm.response"]
    assert events[0][1]["data"]["turns"] == 2
    assert events[0][1]["data"]["prompt_sha"] == events[1][1]["data"]["prompt_sha"]


def test_ask_turns_prepends_to_an_existing_system_turn() -> None:
    seen: dict = {}

    def raw(messages, **k):
        seen["messages"] = messages
        return "ok"

    c, _ = _client(raw)
    c.ask_turns([{"role": "system", "content": "SYS"}, {"role": "user", "content": "u"}])
    assert seen["messages"][0]["content"] == "DISCLAIMER\n\nSYS"
    assert len(seen["messages"]) == 2
