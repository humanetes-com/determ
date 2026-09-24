"""Per-fn LLM ask adapter.

The extraction ladder for a per-fn fill, as turns on the unit's
conversation (`application.reconstruct.unit_conversation`), and the factory
that opens that conversation over the guarded client. Collaborators are
injected so both are testable without a live model.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
)
from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    UnitConversation,
)
from pipeline_core.domain.llm_output import extract_body_response
from pipeline_core.domain.rust_idents import is_unsafe_fn
from pipeline_core.prompts.pull_menu import pull_menu_section
from pipeline_core.prompts.retry_turn import BODY_OUTPUT, AttemptFeedback


class _TurnsClient(Protocol):
    def ask_turns(self, messages: list[dict[str, Any]], *, model: str | None = None,
                  max_tokens: int | None = None, think: bool | None = None,
                  temperature: float | None = None, followup_think: bool = False,
                  ) -> str: ...

    def fit(self, prompt: str) -> str: ...


def conversation_factory(
    client: _TurnsClient, *, model: str,
    log: Callable[[str], None], emit: Callable[..., None],
    max_transcript_chars: int,
) -> Callable[..., UnitConversation]:
    """`open(label, *, route, sources, max_pulls)` -> the unit's conversation
    over `client`, bound to `model`. `sources` None serves no requests and
    appends no request menu."""

    def _ask_turns(messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return client.ask_turns(messages, model=model, **kwargs)

    def _open(label: str, *, route: str = "per-fn",
              sources: PullSources | None = None,
              max_pulls: int = 2) -> UnitConversation:
        return UnitConversation(
            ConversationPorts(ask_turns=_ask_turns, log=log, emit=emit,
                              fit=client.fit,
                              max_transcript_chars=max_transcript_chars),
            label=label, route=route,
            requests=(make_request_answerer(sources, log=log, emit=emit,
                                            max_pulls=max_pulls)
                      if sources is not None else None),
            menu=(pull_menu_section(sources.available_kinds(), max_pulls)
                  if sources is not None else ""))

    return _open


def _carries_answer(reply: str) -> bool:
    """A reply that carries a body in any form the extractor reads. A reply
    without one is a context request when it holds a `<REQUEST …/>` — never
    a body: extract_body_response's raw fallback would otherwise splice the
    tag text verbatim."""
    return "<BODY>" in reply or "<FN " in reply or "```" in reply


def ask_per_fn_body(
    turn: str | AttemptFeedback,
    fn_name: str,
    attempts_log: list[str],
    *,
    conversation: UnitConversation,
    sig_text: str = "",
    unsafe_temp: float | None = None,
) -> tuple[str | None, str]:
    """One per-fn attempt on the unit's conversation, with the extraction
    ladder as turns.

    `turn` is the opening prompt when the conversation has not been opened,
    else the feedback the fix loop measured about the last reply. Requests
    in a reply are answered on the transcript (the request turn stays above
    its answer). A reply with no usable body is re-asked on the SAME
    transcript — at the model's default thinking, then twice with thinking
    off — rather than by re-sending the prompt to a model that no longer has
    its reply: the reasoning that ran out before writing the body is the
    context the re-ask needs.

    Returns ``(body, last_response)``; ``body`` is ``None`` when all three
    replies are empty or stub-only.

    For ``unsafe`` fns, lowers sampling temperature to ``PHASE_C_UNSAFE_TEMP``
    (env, default 0.1): rigid pointer/vtable bodies have one correct token
    sequence, not a range of creative choices.
    """
    is_unsafe = bool(sig_text) and is_unsafe_fn(sig_text)
    eff_unsafe_temp = unsafe_temp if unsafe_temp is not None else float(
        os.getenv("PHASE_C_UNSAFE_TEMP", "0.1"))
    base: dict[str, Any] = {}
    if is_unsafe:
        base["temperature"] = eff_unsafe_temp
    last_response = ""
    for rung, label in enumerate(("default", "think=False", "think=False+retry")):
        kwargs = dict(base)
        if rung:
            kwargs["think"] = False
        if rung == 0 and not conversation.opened:
            assert isinstance(turn, str)
            last_response = conversation.open(
                turn, has_deliverable=_carries_answer, **kwargs)
        elif rung == 0:
            assert isinstance(turn, AttemptFeedback)
            last_response = conversation.retry(
                turn, has_deliverable=_carries_answer, **kwargs)
        else:
            last_response = conversation.reask(
                BODY_OUTPUT, (fn_name,), has_deliverable=_carries_answer,
                **kwargs)
        if not _carries_answer(last_response) and "<REQUEST" in last_response:
            # Still asking once the request budget is spent: the next rung
            # asks for the body on the same transcript.
            attempts_log.append(f"{fn_name}: unanswered context request")
            continue
        body = extract_body_response(last_response, fn_name)
        if body and not is_unimplemented_body(body):
            if label != "default":
                attempts_log.append(f"{fn_name}: recovered via {label}")
            return body, last_response
        attempts_log.append(f"{fn_name}: empty/stub from {label}")
    return None, last_response
