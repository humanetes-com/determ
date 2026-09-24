"""Conversation doubles for tests that script the per-fn ask port.

A fill unit talks to the model through `UnitConversation`. A fake
`ask_per_fn_body` that returns a scripted body must still put the turn on
the conversation, or every later attempt would look like an opening one —
the pre-conversation behaviour, which is exactly what those tests must no
longer see. `send` does that over a transport that answers with a fixed
reply, and returns the text the model was sent, for assertions.
"""
from __future__ import annotations

from typing import Any

from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    UnitConversation,
)


def idle_conversation(label: str = "unit", *, route: str = "per-fn",
                      reply: str = "RESP", **_k: Any) -> UnitConversation:
    return UnitConversation(
        ConversationPorts(ask_turns=lambda _m, **_kw: reply,
                          log=lambda _s: None),
        label=label, route=route)


def send(conversation: UnitConversation, turn: Any) -> str:
    """Put `turn` (an opening prompt or an `AttemptFeedback`) on the
    conversation the way the per-fn ladder does; the text it was sent."""
    if isinstance(turn, str):
        conversation.open(turn, has_deliverable=lambda _r: True)
    else:
        conversation.retry(turn, has_deliverable=lambda _r: True)
    return str(conversation.messages[-2]["content"])
