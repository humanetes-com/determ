"""Per-fn LLM ask adapter (G/P6).

The 3-attempt response-extraction ladder for per-fn reconstruction prompts.
Extracted from pipeline._ask_per_fn_body; all collaborators injected so the
function is testable without a live model.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.context_pull import extract_context_pull
from pipeline_core.domain.llm_output import extract_body_response
from pipeline_core.domain.rust_idents import is_unsafe_fn


class _AskClient(Protocol):
    def ask(self, prompt: str, system: str | None = None, model: str | None = None,
            max_tokens: int | None = None, think: bool | None = None,
            temperature: float | None = None) -> str: ...


def ask_per_fn_body(
    client: _AskClient,
    prompt: str,
    fn_name: str,
    attempts_log: list[str],
    *,
    sig_text: str = "",
    model: str,
    unsafe_temp: float | None = None,
    answer_request: Any = None,
) -> tuple[str | None, str]:
    """3-attempt extraction ladder for a per-fn reconstruction prompt.

    Returns ``(body, last_response)``. ``body`` is ``None`` when all three
    attempts yield empty or stub-only output.

    For ``unsafe`` fns, lowers sampling temperature to ``PHASE_C_UNSAFE_TEMP``
    (env, default 0.1): rigid pointer/vtable bodies have one correct token
    sequence, not a range of creative choices.

    ``answer_request`` (P2 pulls): a bodyless reply carrying a
    ``<REQUEST .../>`` is a context request, NOT a failed extraction — the
    answerer returns the prompt addition and the SAME rung re-asks (no
    think=False rung burned); the answerer's budget bounds the loop, and the
    augmented prompt persists into later rungs.
    """
    is_unsafe = bool(sig_text) and is_unsafe_fn(sig_text)
    eff_unsafe_temp = unsafe_temp if unsafe_temp is not None else float(
        os.getenv("PHASE_C_UNSAFE_TEMP", "0.1"))
    last_response = ""
    for think_off, label in [(False, "default"), (True, "think=False"),
                             (True, "think=False+retry")]:
        kwargs: dict[str, Any] = {"model": model}
        if think_off:
            kwargs["think"] = False
        if is_unsafe:
            kwargs["temperature"] = eff_unsafe_temp
        while True:
            last_response = client.ask(prompt, **kwargs)
            # P2: a request-only reply (no <BODY>, no fence) is a context
            # request, never a body — extract_body_response's raw fallback
            # would otherwise splice the tag text verbatim.
            if (answer_request is not None
                    and "<BODY>" not in last_response
                    and "```" not in last_response
                    and extract_context_pull(last_response) is not None):
                addition = answer_request(last_response)
                if addition is not None:
                    attempts_log.append(f"{fn_name}: answered context request")
                    prompt = prompt + addition
                    continue
                attempts_log.append(f"{fn_name}: unanswered context request")
                break
            body = extract_body_response(last_response)
            if body and not is_unimplemented_body(body):
                if label != "default":
                    attempts_log.append(f"{fn_name}: recovered via {label}")
                return body, last_response
            break
        attempts_log.append(f"{fn_name}: empty/stub from {label}")
    return None, last_response
