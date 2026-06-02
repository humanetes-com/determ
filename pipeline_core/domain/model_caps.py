"""Pure model-capability helpers: tier classification and context budget math.

No env reads, no I/O — callers resolve env/config and pass values in. This keeps the
budget logic unit-testable and the domain layer pure.
"""

from __future__ import annotations

from .enums import Backend, ModelTier

# Model name prefixes that follow FILE markers reliably and benefit from explicit
# reasoning directives. Everything else is treated as weak-tier.
_MID_TIER_PREFIXES: tuple[str, ...] = (
    "qwen3", "qwen2.5-72",
    "llama-3.3-70", "llama-3.1-70", "llama-3.1-405",
    "mistral-large", "mixtral-8x22",
    "claude", "gpt-4", "gemini-1.5", "gemini-2",
)

# Headroom (tokens) reserved for the system prompt + generation; the rest is content.
_CONTEXT_HEADROOM_TOKENS = 6_000
_MIN_CONTENT_TOKENS = 4_000
_CLAUDE_CONTEXT_TOKENS = 200_000
_FALLBACK_CONTEXT_TOKENS = 8_192
_CHARS_PER_TOKEN = 4


def model_tier(model: str) -> ModelTier:
    """Classify a model as MID (capable instruction-follower) or WEAK (small/distilled)."""
    name = model.lower().split(":")[0].split("/")[-1]
    if any(name.startswith(prefix) for prefix in _MID_TIER_PREFIXES):
        return ModelTier.MID
    return ModelTier.WEAK


def model_context_chars(
    model: str,
    backend: Backend,
    max_len_tokens: int | None,
) -> int:
    """Safe content budget, in chars, for one model call.

    ``max_len_tokens`` is the backend-reported context length when known (e.g. from
    ``OLLAMA_NUM_CTX`` / ``VLLM_MAX_MODEL_LEN``); ``None`` when the backend exposes none.
    """
    if max_len_tokens is not None:
        return max(max_len_tokens - _CONTEXT_HEADROOM_TOKENS, _MIN_CONTENT_TOKENS) * _CHARS_PER_TOKEN
    if model.startswith("claude") or backend is Backend.CLAUDE:
        return (_CLAUDE_CONTEXT_TOKENS - _CONTEXT_HEADROOM_TOKENS) * _CHARS_PER_TOKEN
    return (_FALLBACK_CONTEXT_TOKENS - _CONTEXT_HEADROOM_TOKENS) * _CHARS_PER_TOKEN
