"""Stringly-typed pipeline knobs, as StrEnums.

Each member's *value* is the exact lowercase token the pipeline compares against today
(e.g. ``Backend.OLLAMA == "ollama"`` is True because StrEnum is a str subclass), so these
can replace bare string literals at comparison sites without changing semantics.

``coerce`` is pure (no env read): case-insensitive, with a typed fallback. The env read
itself lives in the settings layer — domain stays I/O-free.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self


class _Coercible(StrEnum):
    """StrEnum with a lenient, case-insensitive ``coerce`` + fallback."""

    @classmethod
    def coerce(cls, value: str | None, default: Self) -> Self:
        if value is None:
            return default
        token = value.strip().lower()
        for member in cls:
            if member.value == token:
                return member
        return default


class Backend(_Coercible):
    """LLM serving backend (``BACKEND`` env)."""

    OLLAMA = "ollama"
    VLLM = "vllm"
    SGLANG = "sglang"
    HUGGINGFACE = "huggingface"

    @property
    def max_len_env_var(self) -> str | None:
        """Env var holding this backend's context length, if it exposes one."""
        return {
            Backend.VLLM: "VLLM_MAX_MODEL_LEN",
            Backend.SGLANG: "SGLANG_MAX_MODEL_LEN",
            Backend.OLLAMA: "OLLAMA_NUM_CTX",
        }.get(self)


class CoverageBackend(_Coercible):
    """Coverage tool (``COVERAGE_BACKEND`` env)."""

    TARPAULIN = "tarpaulin"
    GRCOV = "grcov"
    LLVM_COV = "llvm-cov"


class FillUnit(_Coercible):
    """Granularity of a phase-C fill / fix unit (``PHASE_C_FILL_UNIT`` / ``FIX_LOOP_UNIT``)."""

    IMPL = "impl"
    FN = "fn"


class FixStrategy(_Coercible):
    """Fix-loop strategy (``FIX_LOOP_STRATEGY``)."""

    PER_FN = "per_fn"
    WHOLE_FILE = "whole_file"


class PhaseCMode(_Coercible):
    """Phase-C fill mode (``PHASE_C_MODE``)."""

    PER_GROUP = "per_group"
    PER_FN = "per_fn"


class PhaseCDriver(_Coercible):
    """Phase-C outer-loop driver (``PHASE_C_DRIVER``)."""

    FN_WALK = "fn_walk"
    TDD = "tdd"


class ModelTier(_Coercible):
    """Instruction-following capability tier of a model."""

    MID = "mid"
    WEAK = "weak"


class Task1Backend(_Coercible):
    """TASK 1 requirements-synthesizer selector (``TASK1_SYNTHESIZER`` env)."""

    STATIC = "static"   # conservative prompt-based path
    LLM = "llm"         # default: contract-first, more deterministic


class Task2Backend(_Coercible):
    """TASK 2 source-generator selector (``TASK2_GENERATOR`` env)."""

    LLM = "llm"           # file-by-file experimental
    STATIC = "static"     # one big ask per attempt
    SKELETON = "skeleton"
    FILELIST = "filelist"  # default
