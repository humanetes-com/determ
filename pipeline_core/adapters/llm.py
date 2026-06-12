"""LLM adapter — wraps model_interact (the served-model boundary).

Satisfies the :class:`pipeline_core.ports.LLM` Protocol. This is the *raw* model call:
context-budget guarding stays in the application layer (``pipeline.ask``), so this adapter
is deliberately thin and unaware of prompt limits.

``model_interact`` is hot-reloaded on disk by ``pipeline._hot_reload_mi``; reload reuses
the same module object, so resolving ``mi.ask`` at call time (not binding it at __init__)
picks up the reloaded function automatically.
"""

from __future__ import annotations

from typing import Any

import model_interact as mi


class ModelInteractLLM:
    """Delegates to ``model_interact.ask``; carries the default model/max_tokens."""

    def __init__(self, *, default_model: str | None = None, default_max_tokens: int | None = None) -> None:
        self._model = default_model
        self._max_tokens = default_max_tokens

    def ask(self, prompt: str, *, system: str | None = None, think: bool | None = None) -> str:
        kwargs: dict[str, Any] = {"system": system, "model": self._model, "think": think}
        if self._max_tokens is not None:
            kwargs["max_tokens"] = self._max_tokens
        return mi.ask(prompt, **kwargs)
