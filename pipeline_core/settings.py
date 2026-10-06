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
    LanguageDriverKind,
    PhaseCDriver,
    PhaseCMode,
)
from .domain.model_caps import model_context_chars, safe_input_char_limit
from .domain.model_profile import resolve_model_knobs

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
    """Backend context length + thinking-budget tokens, with the legacy defaults.

    vLLM resolves both through `resolve_model_knobs` (env → the served
    model's profile → default), the values the transport plans with. The
    thinking default here used to be 0 against the transport's 16384, so an
    unset key sized prompts with no reasoning reserve the transport then took."""
    if backend is Backend.VLLM:
        knobs = resolve_model_knobs(os.environ)
        return knobs.max_model_len, knobs.thinking_budget
    if backend is Backend.SGLANG:
        return int(os.getenv("SGLANG_MAX_MODEL_LEN", "65536")), int(os.getenv("SGLANG_THINKING_BUDGET", "0"))
    if backend is Backend.OLLAMA:
        return int(os.getenv("OLLAMA_NUM_CTX", "8192")), 0
    return 8_192, 0  # huggingface / unrecognised → safe fallback


def _file_floor_pct(coverage_target: float) -> float:
    """`COVERAGE_FILE_FLOOR`: unset or blank follows the aggregate target, so
    an `.env` that names one coverage number gets it per file too."""
    raw = (os.getenv("COVERAGE_FILE_FLOOR") or "").strip()
    return float(raw) if raw else coverage_target


def _optional_float(raw: str | None) -> float | None:
    """An env float, None when unset or blank (the server default)."""
    return float(raw) if raw and raw.strip() else None


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
    # The task0c line stop: the module aggregate percent, and the per-file
    # floor over the lines a test can reach (unset = the aggregate target;
    # 0 = off). See `domain.file_coverage_floor`.
    coverage_augmentation_target: float = 98.0
    coverage_file_floor: float = 0.0
    # Consecutive task0c passes over a module with no new best covered-line
    # count (and no function newly accounted for) before it is accepted as
    # hard_block. The backstop for a target the module cannot reach.
    task0c_pass_stall_limit: int = 10
    task0d_probe_max: int = 12
    # Compositions of two public items t0d asks about per cluster: edges of
    # the public API (one item's result type is another's receiver or
    # argument) that no example asserts. `TASK0D_PROBE_EDGES=0` asks none.
    task0d_probe_edges: int = 8
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
    # Pin env-moving public Debug via two-world captures. On: a capture whose
    # value moves under a perturb (RUST_BACKTRACE) is recaptured and emitted
    # as two tests, the probe ask ranks EXACT_FORMAT by Debug probes, and a
    # Display observe is a miss for that ask. Off restores the pre-2026-09-02
    # contract: movers are withdrawn, harvest Display counts as exact-format
    # coverage, and the ask is Display, Debug, or alternate.
    behaviour_pin_env_debug: bool = True
    # The behaviour book's t0d rule calls. Off (`BEHAVIOUR_BOOK_RULES=0`)
    # still builds the glossary and features and writes the Gherkin view,
    # with no sentence and no model call. The other three bound the calls:
    # per chunk of examples one draft call, one ablation call, and one check
    # call per honoured draft.
    behaviour_book_rules: bool = True
    behaviour_book_max_features: int = 24
    behaviour_book_examples_per_prompt: int = 24
    behaviour_book_drafts_per_reply: int = 6
    # Chunks of the book drafted and checked at once. Every chunk is
    # independent (its ablation and checks read only its own examples), and
    # the book ran one call at a time: 62 calls, 10,090 s on run
    # `72627-751534fe` (2026-09-30). `BEHAVIOUR_BOOK_LANES`, else the
    # pipeline's `PIPELINE_PARALLELISM`, else 1. 1 is the serial build.
    behaviour_book_lanes: int = 1
    # Sampling temperature of the book judge's calls (ablation, set,
    # leave-one-out); drafting keeps the server default. None (unset or
    # empty `BEHAVIOUR_BOOK_PREDICT_TEMPERATURE`) forwards no temperature.
    # Left unset until `scripts/book_judge_replay.py`'s agreement line
    # chooses a value. Printed as `predict-temp=` on the behaviour-book line.
    behaviour_book_predict_temperature: float | None = None
    # Whether the book's draft, judge and Given-phrase calls ask the model to
    # reason. Off by default: on run `450922-661c5e9f` the book's 105 calls
    # replied p50 787 content chars after p50 26,981 reasoning chars, 492.8
    # call-minutes (`scripts/measure_phase_reasoning.py`). The probe and
    # rule-probe program calls write Rust and keep the model's default.
    # `BEHAVIOUR_BOOK_THINK=1` restores reasoning on the book. Printed as
    # `think=` on the behaviour-book line.
    behaviour_book_think: bool = False
    # A feature's kept sentences are judged again on each of its chunks'
    # uncovered examples (one set call, plus leave-one-out calls when it
    # gains). `BEHAVIOUR_BOOK_SIBLING_CHUNKS=0` leaves each chunk with the
    # rules drafted from its own examples. Read off the behaviour-book line
    # (`N by a sibling chunk's rule`).
    behaviour_book_sibling_chunks: bool = True
    # One sentence per root the examples are given (a test-file item their
    # code names), drafted in one call per test file and printed beside the
    # pointer in the generated view only. `BEHAVIOUR_BOOK_GIVEN_PHRASES=0`
    # asks for none. Read off the behaviour-book line (`givens: N phrase(s)
    # for M root(s)`).
    behaviour_book_given_phrases: bool = True
    # Percent of t0d's own captures held out of the suite as the agreement
    # reading (`domain/held_out.py`): never emitted as tests, never in a
    # prompt or the book; run against the reconstruction when a cluster's
    # fill closes and at task3, printed as `behaviour-agreement:`.
    # `BEHAVIOUR_HELD_OUT_SHARE=0` holds nothing out and takes no reading.
    behaviour_held_out_share: int = 25
    # Kept rules of the behaviour book tested on new captures at t0d, after
    # the book: per rule the model writes programs the sentence covers, the
    # original prints their values, and the rule is confirmed, refuted or
    # dropped on them. `BEHAVIOUR_RULE_PROBES=0` skips the stage;
    # `BEHAVIOUR_RULE_PROBE_PROGRAMS` is the programs asked per rule (each
    # costs two cargo runs).
    behaviour_rule_probes: int = 8
    behaviour_rule_probe_programs: int = 3
    # Who is the language expert in fill and layout prompts. generic (default):
    # the fill model; the pipeline is the gate. catalog: the pre-2026-09-09
    # std-trait tutorials, Display/Debug name-set, standard derive set, and
    # Send/Sync tutorial, kept as an A/B. Read at composition; no code_id
    # change. Confirmed on the cluster-entry line (language-driver=…).
    language_driver: LanguageDriverKind = LanguageDriverKind.GENERIC

    @classmethod
    def from_env(cls, model: str = "") -> Settings:
        backend = Backend.coerce(os.getenv("BACKEND"), Backend.OLLAMA)
        var = backend.max_len_env_var
        raw = os.getenv(var) if var else None
        max_len = int(raw) if raw and raw.strip().isdigit() else None
        if backend is Backend.VLLM:
            max_len = resolve_model_knobs(os.environ).max_model_len

        mcc = model_context_chars(model, backend, max_len)
        ctx, thinking = _resolve_ctx_thinking(backend)
        safe = safe_input_char_limit(ctx, thinking, _MIN_CONTENT_TOKENS_GENERAL)
        safe_t0c = safe_input_char_limit(ctx, thinking, _MIN_CONTENT_TOKENS_TASK0C)
        single_call = min(safe, mcc * 3 // 4)
        _coverage_target = float(os.getenv("COVERAGE_AUGMENTATION_TARGET", "98"))

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
            coverage_augmentation_target=_coverage_target,
            coverage_file_floor=_file_floor_pct(_coverage_target),
            task0c_pass_stall_limit=max(1, int(os.getenv("TASK0C_PASS_STALL_LIMIT", "10"))),
            task0d_probe_max=int(os.getenv("TASK0D_PROBE_MAX", "12")),
            task0d_probe_edges=int(os.getenv("TASK0D_PROBE_EDGES", "8")),
            task0d_exercises=os.getenv("TASK0D_EXERCISES", "1").strip() == "1",
            task0d_blocks_per_reply=int(
                os.getenv("TASK0D_BLOCKS_PER_REPLY", "8")),
            task0d_classes_per_cluster=int(
                os.getenv("TASK0D_CLASSES_PER_CLUSTER", "4")),
            behaviour_capture_split=os.getenv("BEHAVIOUR_CAPTURE_SPLIT", "1").strip() == "1",
            behaviour_pin_env_debug=os.getenv("BEHAVIOUR_PIN_ENV_DEBUG", "1").strip() == "1",
            behaviour_book_rules=os.getenv("BEHAVIOUR_BOOK_RULES", "1").strip() == "1",
            behaviour_book_max_features=int(os.getenv("BEHAVIOUR_BOOK_MAX_FEATURES", "24")),
            behaviour_book_examples_per_prompt=int(
                os.getenv("BEHAVIOUR_BOOK_EXAMPLES_PER_PROMPT", "24")),
            behaviour_book_drafts_per_reply=int(os.getenv("BEHAVIOUR_BOOK_DRAFTS_PER_REPLY", "6")),
            behaviour_book_lanes=max(1, int(
                os.getenv("BEHAVIOUR_BOOK_LANES") or os.getenv("PIPELINE_PARALLELISM") or "1")),
            behaviour_book_predict_temperature=_optional_float(
                os.getenv("BEHAVIOUR_BOOK_PREDICT_TEMPERATURE")),
            behaviour_book_think=os.getenv("BEHAVIOUR_BOOK_THINK", "0").strip() == "1",
            behaviour_rule_probes=int(os.getenv("BEHAVIOUR_RULE_PROBES", "8")),
            behaviour_rule_probe_programs=int(os.getenv("BEHAVIOUR_RULE_PROBE_PROGRAMS", "3")),
            behaviour_held_out_share=max(0, min(100, int(
                os.getenv("BEHAVIOUR_HELD_OUT_SHARE", "25")))),
            behaviour_book_sibling_chunks=os.getenv(
                "BEHAVIOUR_BOOK_SIBLING_CHUNKS", "1").strip() == "1",
            behaviour_book_given_phrases=os.getenv(
                "BEHAVIOUR_BOOK_GIVEN_PHRASES", "1").strip() == "1",
            language_driver=LanguageDriverKind.coerce(
                os.getenv("LANGUAGE_DRIVER"), LanguageDriverKind.GENERIC),
        )
