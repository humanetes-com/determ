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


class RouteOrder(_Coercible):
    """Phase-C fill-route sequencing (``PHASE_C_ROUTE_ORDER``).

    DEFAULT is the historical ladder: the file-first pass takes only files
    holding >=2 viable impl blocks (plus refill-deferred free-fn/oversized
    blocks), the per-impl loop takes viable blocks, the per-fn route takes
    everything else — measured on the 2026-08-12 fixture validation runs,
    that sent 37 of 41 hardcrate fills and 9 of 13 microcrate fills through
    the per-fn route at one fn per LLM call.

    FILE_PRIMARY widens the file-first pass to EVERY file with frontier work
    — single-block files and blocks the impl route never attempts (free fns,
    oversized) included — so one call designs a whole file's frontier
    together and the per-impl/per-fn routes become the fallback for what it
    leaves. Acceptance gates are unchanged; only dispatch grouping moves."""

    DEFAULT = "default"
    FILE_PRIMARY = "file_primary"


class PhaseCDriver(_Coercible):
    """Phase-C outer-loop driver (``PHASE_C_DRIVER``)."""

    FN_WALK = "fn_walk"
    TDD = "tdd"


class LanguageDriverKind(_Coercible):
    """Who is the language expert in fill and layout prompts (``LANGUAGE_DRIVER``).

    GENERIC (default): the fill model. The pipeline is the gate — who may
    write, what evidence sits in the prompt, whether a two-file change is
    one transaction. Trait-contract tutorials, a Display/Debug name-set,
    a hardcoded derive list, the Send/Sync tutorial, language gotcha
    DETAIL, name-keyed construction recipes, and crate-symbol conversion
    families are not injected.

    CATALOG: those pre-2026-09-09 tables, kept so an A/B can turn them
    back on. Production call sites go through the bound driver; when
    generic is bound the catalog functions are not called.
    """

    GENERIC = "generic"
    CATALOG = "catalog"


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


class ProcessFailure(_Coercible):
    """Why a subprocess produced NO normal exit code (the rc=-1 sentinel made
    explicit). TIMEOUT is retryable/slow; BINARY_MISSING is a
    broken environment no retry can fix."""

    TIMEOUT = "timeout"
    BINARY_MISSING = "binary_missing"
