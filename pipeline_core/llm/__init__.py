"""High-level LLM control plane.

The *business logic* of talking to a reasoning model — thinking-budget recovery,
ctx-budget-aware output capping, structural early-stop, boundary-token cleanup and
input-too-long / network retry — lives here, **once**, independent of which server
actually serves the tokens.

Backends (in ``model_interact.py``) are reduced to *transports*: they only know how
to open a connection, stream normalized :class:`Delta` events, and report their own
budget shape. They carry no recovery or capping policy. The :class:`LLMController`
owns all of that and drives any transport identically — Ollama, vLLM, SGLang
or HuggingFace.

This module is a foundational facade (like ``log``/``retry``/``io``), not one of the
architecture layers, so it is intentionally outside the import-linter layer contract.
"""

from __future__ import annotations

from .budget import BudgetSpec, BudgetState, request_max_tokens
from .controller import Hooks, LLMController
from .detect import StopDetector
from .events import (
    Delta,
    InputTooLong,
    StreamMeta,
    Transport,
    TransportTimeout,
    TransportUnavailable,
)
from .thinktag import InlineThinkSplitter

__all__ = [
    "BudgetSpec",
    "BudgetState",
    "Delta",
    "Hooks",
    "InlineThinkSplitter",
    "InputTooLong",
    "LLMController",
    "StopDetector",
    "StreamMeta",
    "Transport",
    "TransportTimeout",
    "TransportUnavailable",
    "request_max_tokens",
]
