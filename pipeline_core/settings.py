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


@dataclass(frozen=True, slots=True)
class TaskModels:
    """Per-task model overrides; each falls back to the global default model."""

    task0: str   # project profile — small/fast
    task0c: str  # coverage augmentation — best code model
    task1: str   # requirements synthesis — code understanding
    task2: str   # code generation — best code model
    task4: str   # failure analysis — reasoning model

    @classmethod
    def from_env(cls, default: str) -> TaskModels:
        def g(key: str) -> str:
            return os.getenv(key, default)
        return cls(
            task0=g("TASK0_MODEL"), task0c=g("TASK0C_MODEL"), task1=g("TASK1_MODEL"),
            task2=g("TASK2_MODEL"), task4=g("TASK4_MODEL"),
        )


@dataclass(frozen=True, slots=True)
class FixLoopCaps:
    """Fix/test loop caps + stall budgets, single-sourced from env. The matching
    pipeline.py module globals mirror these (so the e2e/smoke harness can still
    monkeypatch the patchable seam, e.g. ``pipeline._PASS_THROUGH``); this is the
    env-reading source of truth, not the patch point."""

    fix_err_cap: int            # per-attempt error excerpt cap in the fix loop
    unified_fill_loop: bool     # U2 post-fill test gate enabled
    u2_test_timeout: int        # U2 gate scoped-test timeout (s)
    per_fn_fix_hard_cap: int    # runaway backstop for per-fn fix attempts
    per_fn_fix_max_stalls: int  # consecutive no-progress attempts before give-up
    per_pass_test_fix_hard_cap: int
    per_pass_test_fix_max_stalls: int
    pass_through: bool          # smoke mode: collapse every stall budget
    pass_through_stalls: int    # collapsed budget under pass-through
    converge_module: str        # one module exempt from pass-through collapse

    @classmethod
    def from_env(cls, single_call_char_limit: int) -> FixLoopCaps:
        return cls(
            fix_err_cap=max(2_000, single_call_char_limit // 10),
            unified_fill_loop=os.getenv("UNIFIED_FILL_LOOP", "1").strip() == "1",
            u2_test_timeout=int(os.getenv("U2_TEST_TIMEOUT", "120")),
            per_fn_fix_hard_cap=int(os.getenv(
                "PHASE_C_FN_HARD_CAP", os.getenv("PHASE_C_FIX_CAP_PER_FN", "10000"))),
            per_fn_fix_max_stalls=int(os.getenv("PHASE_C_FN_MAX_STALLS", "3")),
            per_pass_test_fix_hard_cap=int(os.getenv(
                "PHASE_C_TEST_FIX_HARD_CAP", os.getenv("PHASE_C_FIX_CAP_PER_PASS", "10000"))),
            per_pass_test_fix_max_stalls=int(os.getenv("PHASE_C_TEST_FIX_MAX_STALLS", "3")),
            pass_through=os.getenv("PIPELINE_PASS_THROUGH", "0") == "1",
            pass_through_stalls=int(os.getenv("PIPELINE_PASS_THROUGH_STALLS", "1")),
            converge_module=os.getenv("PIPELINE_CONVERGE_MODULE", "").strip(),
        )


def _resolve_ctx_thinking(backend: Backend) -> tuple[int, int]:
    """Backend context length + thinking-budget tokens, with the legacy defaults."""
    if backend is Backend.VLLM:
        return int(os.getenv("VLLM_MAX_MODEL_LEN", "65536")), int(os.getenv("VLLM_THINKING_BUDGET", "0"))
    if backend is Backend.SGLANG:
        return int(os.getenv("SGLANG_MAX_MODEL_LEN", "65536")), int(os.getenv("SGLANG_THINKING_BUDGET", "0"))
    if backend is Backend.OLLAMA:
        return int(os.getenv("OLLAMA_NUM_CTX", "8192")), 0
    return 8_192, 0  # huggingface / unrecognised → safe fallback


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
    task_models: TaskModels
    fix_caps: FixLoopCaps
    # Behaviour specification knobs (the harvest before task0c and the
    # admission contract in task0c). Percent of the cluster's harvested
    # behaviours the generated tests must pin before task0c stops; 0 turns the
    # pin requirement and the extra behaviour passes off. Probe cap = admitted
    # public-API programs per cluster, each one model batch plus one cargo run.
    behaviour_coverage_target: float = 0.0
    task0d_probe_max: int = 12
    task0d_exercises: bool = True
    # Blocks honoured per synthesis reply. The probe-target cap bounds how many
    # units are ASKED about; without this a single reply's block count bounded
    # the cargo runs, and each run costs a compile at up to the probe timeout.
    task0d_blocks_per_reply: int = 8
    # Behaviour classes requested per cluster, scarcest first.
    task0d_classes_per_cluster: int = 4
    # Capture/guess split: when on, task0c stops listing (and requiring a pin
    # on) the behaviour classes whose exact value a model would have to
    # predict; the capture route owns those. Off restores the pre-split
    # listing, so the split can be turned off without a code edit.
    behaviour_capture_split: bool = True

    @classmethod
    def from_env(cls, model: str = "") -> Settings:
        backend = Backend.coerce(os.getenv("BACKEND"), Backend.OLLAMA)
        var = backend.max_len_env_var
        raw = os.getenv(var) if var else None
        max_len = int(raw) if raw and raw.strip().isdigit() else None

        mcc = model_context_chars(model, backend, max_len)
        ctx, thinking = _resolve_ctx_thinking(backend)
        safe = safe_input_char_limit(ctx, thinking, _MIN_CONTENT_TOKENS_GENERAL)
        safe_t0c = safe_input_char_limit(ctx, thinking, _MIN_CONTENT_TOKENS_TASK0C)
        single_call = min(safe, mcc * 3 // 4)

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
            single_call_char_limit=single_call,
            intermediate_char_limit=mcc // 2,
            task_models=TaskModels.from_env(model),
            fix_caps=FixLoopCaps.from_env(single_call),
            behaviour_coverage_target=float(os.getenv("BEHAVIOUR_COVERAGE_TARGET", "0")),
            task0d_probe_max=int(os.getenv("TASK0D_PROBE_MAX", "12")),
            task0d_exercises=os.getenv("TASK0D_EXERCISES", "1").strip() == "1",
            task0d_blocks_per_reply=int(
                os.getenv("TASK0D_BLOCKS_PER_REPLY", "8")),
            task0d_classes_per_cluster=int(
                os.getenv("TASK0D_CLASSES_PER_CLUSTER", "4")),
            behaviour_capture_split=os.getenv("BEHAVIOUR_CAPTURE_SPLIT", "1").strip() == "1",
        )
