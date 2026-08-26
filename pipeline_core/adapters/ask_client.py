"""Guarded LLM ask client — context-limit-enforcing transport (P6 engine-relocation).

Wraps the raw `model_interact.ask` transport with: the section-aware context
guard (pure, in `domain.context_guard`), a budget-utilization warning, prompt
content capture + structured telemetry (llm.request/response/error, each
self-correlating by prompt_sha), and a 3-try network-retry loop with a
guaranteed terminal event (so a request can never be left "open" on its thread).

Distinct from `adapters.llm.ModelInteractLLM` (the *raw* `ports.LLM` port): this
is the budget-guarded composition that used to be `pipeline.ask`. Lifted verbatim
out of the god-file; every collaborator is injected by the composition root —
including `ask_raw` as a LATE-BINDING callable, because `_hot_reload_mi` rebinds
the transport global when model_interact.py changes on disk; capturing it here
would pin a stale function.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..domain.context_guard import fit_prompt

_NETWORK_ERR_WORDS = ("Timeout", "Connect", "Protocol", "Network")


@dataclass(frozen=True, slots=True)
class AskClient:
    """The model-call entry point with its collaborators injected."""

    ask_raw: Callable[..., str]
    hot_reload: Callable[[], None]
    emit: Callable[..., None]
    guard: Callable[[str, str], None]
    store_prompt_content: Callable[[str], dict[str, Any]]
    caller_site: Callable[[], str]
    disclaimer: Callable[[], str]
    model_name: str
    context_chars: int
    single_call_char_limit: int

    def ask(self, prompt: str, system: str | None = None, model: str | None = None,
            max_tokens: int | None = None, think: bool | None = None,
            temperature: float | None = None, followup_think: bool = False) -> str:
        disclaimer = self.disclaimer()
        system = f"{disclaimer}\n\n{system}" if system else disclaimer
        sys_len = len(system)

        fitted = fit_prompt(prompt, sys_len, context_chars=self.context_chars)
        for tag, msg in fitted.events:
            self.guard(tag, msg)
        prompt = fitted.prompt

        kwargs: dict[str, Any] = {}
        if model is not None:
            kwargs["model"] = model
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if think is not None:
            kwargs["think"] = think
        if temperature is not None:
            kwargs["temperature"] = temperature
        # Forwarded only when set: `ask_raw` is late-bound and the fakes that
        # stand in for it take `(prompt, system, **k)` or `(*a, **k)` today,
        # but the default call must stay byte-identical to what it was.
        if followup_think:
            kwargs["followup_think"] = True
        self.hot_reload()

        prompt_meta = self.store_prompt_content(prompt)
        system_meta = self.store_prompt_content(system) if system else None
        # Budget-utilization warning: flag prompts consuming most of the input
        # window so silent overflows surface in logs.
        prompt_len = len(prompt) + (len(system) if system else 0)
        if prompt_len > self.single_call_char_limit * 90 // 100:
            self.guard("prompt/budget_high",
                       f"prompt {prompt_len:,} chars = "
                       f"{prompt_len * 100 // max(1, self.single_call_char_limit)}% of "
                       f"SINGLE_CALL_CHAR_LIMIT ({self.single_call_char_limit:,})")
        # prompt_sha on the REQUEST (not just the response) makes every call
        # self-correlating, independent of the thread-name heuristic (debt #6).
        self.emit("llm.request", data={
            "model": model or self.model_name,
            "prompt": prompt_meta,
            "prompt_sha": prompt_meta["sha256"],
            "system": system_meta,
            "max_tokens": max_tokens,
            "caller": self.caller_site(),
        })
        t0 = time.monotonic()
        # Guarantee a terminal event for every request: if the loop exits without
        # emitting an llm.response/llm.error, the finally emits one. A request
        # left "open" on its thread is exactly the vanished-response / stream
        # collision C10 reports (debt #6).
        terminal = False
        try:
            for net_attempt in range(3):
                try:
                    response = self.ask_raw(prompt, system, **kwargs)
                    dur_ms = int((time.monotonic() - t0) * 1000)
                    self.emit("llm.response", duration_ms=dur_ms,
                              data={"prompt_sha": prompt_meta["sha256"],
                                    "response": self.store_prompt_content(response or "")})
                    terminal = True
                    return response
                except Exception as e:
                    ename = type(e).__name__
                    if net_attempt < 2 and any(w in ename for w in _NETWORK_ERR_WORDS):
                        self.guard("ask/network_retry", f"attempt={net_attempt} err={ename}: {e}")
                        time.sleep(10)
                        continue
                    self.emit("llm.error", duration_ms=int((time.monotonic() - t0) * 1000),
                              data={"prompt_sha": prompt_meta["sha256"],
                                    "error_type": ename, "error_msg": str(e)[:500]})
                    terminal = True
                    raise
            raise RuntimeError("ask: retry loop exhausted")
        finally:
            if not terminal:
                self.emit("llm.error", duration_ms=int((time.monotonic() - t0) * 1000),
                          data={"prompt_sha": prompt_meta["sha256"],
                                "error_type": "Unpaired",
                                "error_msg": "ask() exited without a terminal llm event"})
