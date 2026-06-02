"""Typed settings — the home for the scattered module-level globals + os.getenv.

Built once at the composition root via ``Settings.from_env`` and injected/read downward, so
the application layer never reads ``os.environ`` directly. Owns the derived char-budget
limits (computed once from backend + model) that the pipeline previously kept as loose
module constants.

Note: only the *LLM-serving* ``BACKEND`` maps to :class:`Backend`. The unrelated
generator ``backend`` (static/llm/skeleton) is a different concept and stays separate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .domain.enums import (
    Backend,
    CoverageBackend,
    FillUnit,
    FixStrategy,
    PhaseCDriver,
    PhaseCMode,
)
from .domain.model_caps import model_context_chars, safe_input_char_limit

# Output reservation (tokens) for the two budget classes.
_MIN_CONTENT_TOKENS_GENERAL = 16_384  # task1/2/3/4 — large outputs (full files, docs)
_MIN_CONTENT_TOKENS_TASK0C = 4_096    # task0c — small test blocks only


def _resolve_ctx_thinking(backend: Backend) -> tuple[int, int]:
    """Backend context length + thinking-budget tokens, with the legacy defaults."""
    if backend is Backend.VLLM:
        return int(os.getenv("VLLM_MAX_MODEL_LEN", "65536")), int(os.getenv("VLLM_THINKING_BUDGET", "0"))
    if backend is Backend.SGLANG:
        return int(os.getenv("SGLANG_MAX_MODEL_LEN", "65536")), int(os.getenv("SGLANG_THINKING_BUDGET", "0"))
    if backend is Backend.OLLAMA:
        return int(os.getenv("OLLAMA_NUM_CTX", "8192")), 0
    return 8_192, 0  # claude / huggingface / unrecognised → safe fallback


@dataclass(frozen=True, slots=True)
class Settings:
    backend: Backend
    coverage_backend: CoverageBackend
    phase_c_mode: PhaseCMode
    phase_c_driver: PhaseCDriver
    phase_c_fill_unit: FillUnit
    fix_loop_strategy: FixStrategy
    fix_loop_unit: FillUnit
    backend_max_len_tokens: int | None
    # Served model id + derived char budgets (computed once from backend + model).
    model: str
    model_context_chars: int
    safe_input_char_limit: int
    safe_input_char_limit_task0c: int
    single_call_char_limit: int
    intermediate_char_limit: int

    @classmethod
    def from_env(cls, model: str = "") -> "Settings":
        backend = Backend.coerce(os.getenv("BACKEND"), Backend.OLLAMA)
        var = backend.max_len_env_var
        raw = os.getenv(var) if var else None
        max_len = int(raw) if raw and raw.strip().isdigit() else None

        mcc = model_context_chars(model, backend, max_len)
        ctx, thinking = _resolve_ctx_thinking(backend)
        safe = safe_input_char_limit(ctx, thinking, _MIN_CONTENT_TOKENS_GENERAL)
        safe_t0c = safe_input_char_limit(ctx, thinking, _MIN_CONTENT_TOKENS_TASK0C)

        return cls(
            backend=backend,
            coverage_backend=CoverageBackend.coerce(
                os.getenv("COVERAGE_BACKEND"), CoverageBackend.TARPAULIN
            ),
            phase_c_mode=PhaseCMode.coerce(os.getenv("PHASE_C_MODE"), PhaseCMode.PER_GROUP),
            phase_c_driver=PhaseCDriver.coerce(os.getenv("PHASE_C_DRIVER"), PhaseCDriver.FN_WALK),
            phase_c_fill_unit=FillUnit.coerce(os.getenv("PHASE_C_FILL_UNIT"), FillUnit.IMPL),
            fix_loop_strategy=FixStrategy.coerce(os.getenv("FIX_LOOP_STRATEGY"), FixStrategy.PER_FN),
            fix_loop_unit=FillUnit.coerce(os.getenv("FIX_LOOP_UNIT"), FillUnit.IMPL),
            backend_max_len_tokens=max_len,
            model=model,
            model_context_chars=mcc,
            safe_input_char_limit=safe,
            safe_input_char_limit_task0c=safe_t0c,
            single_call_char_limit=min(safe, mcc * 3 // 4),
            intermediate_char_limit=mcc // 2,
        )
