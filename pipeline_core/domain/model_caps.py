"""Pure model-capability helpers: tier classification and context budget math.

No env reads, no I/O — callers resolve env/config and pass values in. This keeps the
budget logic unit-testable and the domain layer pure.

Context-budget derivation: (num_ctx_tokens - prompt_overhead) * chars_per_token,
with prompt_overhead ≈ 6k tokens (instructions + system prompt + response buffer)
and chars_per_token ≈ 4 (conservative; typical Rust/English source is 3.5-4.5).
Reference: OLLAMA_NUM_CTX=32768 → ~104k chars; the 8192-token fallback → ~26k.
"""

from __future__ import annotations

from .enums import Backend, ModelTier

# Model name prefixes that follow FILE markers reliably and benefit from explicit
# reasoning directives. Everything else is treated as weak-tier.
_MID_TIER_PREFIXES: tuple[str, ...] = (
    "qwen3", "qwen2.5-72",
    "llama-3.3-70", "llama-3.1-70", "llama-3.1-405",
    "mistral-large", "mixtral-8x22",
    # Muse-Glimmer-30B. Without an entry it classifies WEAK by fallback and the
    # prompts silently lose the FILE markers and explicit reasoning directives —
    # a quality drop with no error, on a model whose card reports 51.2 on
    # SWE-Bench Pro and which is built for multi-step tool use. Matched on the
    # bare name, so it covers both the BF16 repo and the FP8-block one
    # ("muse-glimmer-30b", "muse-glimmer-30b-fp8-block").
    "muse-glimmer",
)

# Headroom (tokens) reserved for the system prompt + generation; the rest is content.
_CONTEXT_HEADROOM_TOKENS = 6_000
_MIN_CONTENT_TOKENS = 4_000
_FALLBACK_CONTEXT_TOKENS = 8_192
_CHARS_PER_TOKEN = 4

# Input-budget math (mirrors model_interact._cap): the max input chars that still guarantee
# `min_content_tokens` of output. cpt_floor=3.0 matches model_interact's estimate floor.
_CPT_FLOOR = 3.0
_VLLM_OVERHEAD_TOKENS = 512
_MIN_SAFE_INPUT_CHARS = 4_000


def safe_input_char_limit(ctx_tokens: int, thinking_tokens: int, min_content_tokens: int) -> int:
    """Max input chars that guarantee at least ``min_content_tokens`` of output.

        input_chars ≤ (ctx - thinking - vllm_overhead - min_content) × cpt_floor

    Pure: caller resolves ctx/thinking from the backend env. Never below a 4k-char floor.
    """
    return max(
        _MIN_SAFE_INPUT_CHARS,
        int((ctx_tokens - thinking_tokens - _VLLM_OVERHEAD_TOKENS - min_content_tokens) * _CPT_FLOOR),
    )


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
    return (_FALLBACK_CONTEXT_TOKENS - _CONTEXT_HEADROOM_TOKENS) * _CHARS_PER_TOKEN
