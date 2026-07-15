#!/usr/bin/env python3
"""
Pipeline for the deterministic process loop.

Usage:
  python pipeline.py --project <subfolder>
"""

import dataclasses
import functools
import importlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4 as _uuid4

import model_interact as _mi
from model_interact import MODEL
from model_interact import ask as _ask_raw
from pipeline_core import log as _pclog
from pipeline_core import retry as _pcretry
from pipeline_core.adapters import build_adapters
from pipeline_core.adapters import coverage as _covmod
from pipeline_core.adapters import discovery as _discovery
from pipeline_core.adapters import log_rotation as _log_rotation
from pipeline_core.adapters import model_catalog as _model_catalog
from pipeline_core.adapters import state as _state_mod
from pipeline_core.adapters import strip as _strip_mod
from pipeline_core.adapters import workspace_files as _wsfiles
from pipeline_core.adapters.artifacts import (  # noqa: F401  (smoke pin: test_api_surface_capture uses pipeline.load_api_surface)
    load_api_surface,
    load_manifest,
)
from pipeline_core.adapters.artifacts import (
    load_original_structure as _artifacts_load_original_structure,
)
from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.adapters.behaviour_store import load_behaviour_suite
from pipeline_core.adapters.cargo import CargoEnv
from pipeline_core.adapters.file_constraints import FileConstraintsReader
from pipeline_core.adapters.gen_tests_storage import (
    derive_gentest_prelude as _derive_gentest_prelude,
)
from pipeline_core.adapters.gen_tests_storage import (
    gen_tests_path as _gen_tests_path,
)
from pipeline_core.adapters.gen_tests_storage import (
    gen_tests_siblings as _gen_tests_siblings,
)
from pipeline_core.adapters.gen_tests_storage import (
    load_gen_tests_origin as _load_gen_tests_origin,
)
from pipeline_core.adapters.model_notes_store import (
    FileModelNotesStore as _FileModelNotesStore,
)
from pipeline_core.adapters.repo_spec_docs import FR_PREFIX as _REPO_SPEC_FR_PREFIX
from pipeline_core.adapters.repo_spec_docs import (
    load_repo_spec_docs as _adapters_load_repo_spec_docs,
)
from pipeline_core.adapters.run_banner import banner_lines as _banner_lines
from pipeline_core.adapters.run_registry import FileRunRegistry, RunRegistryEntry
from pipeline_core.adapters.structure import CliStructureReader as _CliStructureReader
from pipeline_core.adapters.telemetry import LogTelemetry
from pipeline_core.adapters.telemetry import emit_per_file_test_overview as _emit_per_file_test_overview
from pipeline_core.application import orchestrator as _orchestrator
from pipeline_core.application.parallel import (
    generate_in_parallel as _generate_in_parallel,
)
from pipeline_core.application.reconstruct import cluster_membership as _membership
from pipeline_core.application.reconstruct import fix_diagnostics as _fix_diag
from pipeline_core.application.reconstruct import impl_fill as _impl_fill
from pipeline_core.application.reconstruct import per_fn_retry as _per_fn_retry
from pipeline_core.application.reconstruct.failure_diagnosis import (
    classify_phase_c_test_failures as _classify_phase_c_test_failures_impl,
)
from pipeline_core.application.reconstruct.failure_diagnosis import (
    record_tdd_regression as _record_tdd_regression_impl,
)
from pipeline_core.application.reconstruct.fill_merge import (
    merge_fn_blocks_into_disk as _merge_fn_blocks_into_disk,
)
from pipeline_core.application.reconstruct.macro_invariants import (
    compute_cluster_macro_invariants as _compute_cluster_macro_invariants,
)
from pipeline_core.application.reconstruct.source_view import (
    annotate_file_view as _annotate_file_view_impl,
)
from pipeline_core.application.reconstruct.test_locator import (
    find_tests_referencing_fn as _find_tests_referencing_fn,
)
from pipeline_core.application.strip_bake import (
    bake_skeleton_artifacts as _bake_skeleton_artifacts_impl,
)
from pipeline_core.application.tasks.legacy_test_run import (
    LegacyTestRunDeps,
)
from pipeline_core.application.tasks.legacy_test_run import (
    run_legacy_single as _run_legacy_single,
)
from pipeline_core.application.tasks.task0c import task0c_augment_module as _task0c_impl
from pipeline_core.composition import App as _App
from pipeline_core.composition import CompositionConfig as _CompositionConfig
from pipeline_core.composition import Helpers as _Helpers
from pipeline_core.composition import Ports as _Ports
from pipeline_core.composition import build_app as _build_app
from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.cargo_toml import cargo_default_features as _domain_cargo_default_features
from pipeline_core.domain.cargo_toml import cargo_is_proc_macro as _domain_cargo_is_proc_macro
from pipeline_core.domain.cargo_toml import cargo_package_name as _domain_cargo_package_name
from pipeline_core.domain.cargo_toml import load_cargo_toml as _domain_load_cargo_toml
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.crate_name import crate_display_name as _crate_display_name
from pipeline_core.domain.enums import (
    Backend,
    CoverageBackend,
    ModelTier,
)
from pipeline_core.domain.impl_block_ranges import (
    impl_where_clause,
)
from pipeline_core.domain.impl_block_ranges import (
    render_impl_block as _render_impl_block,
)
from pipeline_core.domain.llm_output import (
    GENESIS_DELIM_END,
    GENESIS_DELIM_START,
    _extract_genesis,
)
from pipeline_core.domain.llm_output import (
    _find_fn_blocks as _find_fn_blocks,  # R8.5 parsers — re-export for call sites/tests
)
from pipeline_core.domain.llm_output import (
    _split_leading_where as _split_leading_where,
)
from pipeline_core.domain.llm_output import (
    extract_fn_bodies as extract_fn_bodies,  # R8.5 parser — re-export for call sites/tests
)
from pipeline_core.domain.model_caps import (
    model_context_chars as _model_context_chars_pure,
)
from pipeline_core.domain.model_caps import (
    model_tier as _model_tier,
)
from pipeline_core.domain.models import (
    BuildCluster,
    Diagnostic,
    FnLocator,
)
from pipeline_core.domain.models import (
    ClusterFillResult as ClusterFillResult,  # noqa: PLC0414 (typed fill-result boundary)
)
from pipeline_core.domain.prompt_budget import PromptBudgets
from pipeline_core.domain.run_state import RunPlan as _RunPlan
from pipeline_core.domain.run_state import RunState
from pipeline_core.domain.rust_fn_headers import (  # noqa: F401  (smoke pin: test_sig_tighten_guard_and_where_parse)
    is_pub_fn as _is_pub_fn,
)
from pipeline_core.domain.test_cmd import parse_cmd as _parse_cmd
from pipeline_core.domain.test_command import (  # noqa: F401  (smoke pin: test_derive_check_only_cmd)
    derive_check_only_cmd as _derive_check_only_cmd,
)
from pipeline_core.domain.test_grouping import split_oversized_groups as _split_oversized_groups
from pipeline_core.domain.test_report import U2Verdict
from pipeline_core.domain.test_results import (
    OUTPUT_SEPARATOR as OUTPUT_SEPARATOR,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.test_results import (
    _parse_test_counts as _parse_test_counts,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.test_results import (
    count_passing_tests,
    extract_test_output_from_file,
    validate_task3_output_strict,
)
from pipeline_core.domain.text import (
    PromptDigest,
)
from pipeline_core.domain.text import (
    extract_code_content as extract_code_content,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.text import (
    strip_markdown_fences as _strip_markdown_fences,
)
from pipeline_core.io import read, write
from pipeline_core.ports import structure as _structure_port
from pipeline_core.prompts import runtime as _prompts_runtime
from pipeline_core.prompts import shaping as _shaping
from pipeline_core.prompts.crate_idioms import (
    extract_crate_idioms as _extract_crate_idioms,
)
from pipeline_core.prompts.disclaimer import (
    from_scratch_disclaimer as _prompts_disclaimer,
)
from pipeline_core.prompts.per_fn import (  # noqa: F401  (smoke pin: test_pipeline_smoke uses pipeline._build_per_fn_fix_prompt)
    build_per_fn_fix_prompt as _build_per_fn_fix_prompt,
)
from pipeline_core.prompts.per_impl import (
    build_per_impl_prompt as _build_per_impl_prompt,
)
from pipeline_core.prompts.sections import (
    build_dispatch_family_section as _build_dispatch_family_section_pure,
)
from pipeline_core.prompts.sections import (
    gotchas_for_error as _gotchas_for_error,
)
from pipeline_core.prompts.sections import (
    gotchas_summary as _gotchas_summary,
)
from pipeline_core.prompts.sections import (
    tag_block as _tag_block,
)
from pipeline_core.prompts.skeleton_index import (
    format_skeleton_index as _format_skeleton_index,
)
from pipeline_core.prompts.skeleton_index import (
    format_skeleton_index_for_cluster as _format_skeleton_index_for_cluster,
)
from pipeline_core.prompts.test_environment import (
    describe_test_environment as _describe_test_environment_impl,
)
from pipeline_core.retry import with_retry
from pipeline_core.settings import Settings

# Names re-exported through pipeline.py for backward-compatible call sites + the smoke
# suite (which references them as pipeline.X). Declaring them here documents the public
# surface and tells the linter these imports are intentional re-exports, not dead code.
__all__ = [
    "count_passing_tests", "validate_task3_output_strict", "extract_test_output_from_file",
    "GENESIS_DELIM_START", "GENESIS_DELIM_END", "_extract_genesis",
    "_tag_block", "with_retry", "_strip_markdown_fences",
    "_format_skeleton_index",  # re-export: smoke calls pipeline._format_skeleton_index (no internal caller)
    "_split_oversized_groups",  # re-export: smoke calls pipeline._split_oversized_groups (no internal caller)
]

_mi_mtime: float = os.path.getmtime(_mi.__file__)


# Attribution-cluster deps-binding shims (P6): bodies live in failure_diagnosis;
# these bind the resident module-globals at call time so the existing
# `monkeypatch.setattr(pipeline, "_classify_phase_c_test_failures", …)` seams hold.
def _classify_phase_c_test_failures(test_out: str, filled_this_pass: list[FnLocator],
                                    failed_tests: list[str], cluster_name: str) -> dict[str, dict]:
    return _classify_phase_c_test_failures_impl(
        test_out, filled_this_pass, failed_tests, cluster_name,
        ask=_ASK_CLIENT.ask, log=_log, model=_TASK0C_MODEL, err_cap=_FIX_ERR_CAP)


def _hot_reload_mi() -> None:
    """Reload model_interact.py if it changed on disk since last ask() call."""
    global _ask_raw, _mi_mtime
    try:
        mtime = os.path.getmtime(_mi.__file__)
        if mtime != _mi_mtime:
            importlib.reload(_mi)
            _ask_raw  = _mi.ask
            _mi_mtime = mtime
            _log("  [hot-reload] model_interact.py reloaded")
    except Exception as e:
        _log(f"  [hot-reload] reload failed: {e}")

# .env is already loaded at model_interact import (above) — no second load needed
# (Debt #1). load_dotenv does not override already-set vars anyway.

# Composition root: resolve all env-driven knobs once into one typed object.
# Everything downstream reads SETTINGS rather than os.environ for these values.
# (Per-task numeric tuning knobs still live as their own getenv reads for now.)
SETTINGS = Settings.from_env(model=MODEL)

# Per-task model overrides — each task uses the most appropriate model.
# Single-sourced from Settings.task_models (env TASK*_MODEL, falling back to MODEL).
# Rationale for each choice is documented in .env.
_TASK0_MODEL  = SETTINGS.task_models.task0
_TASK0C_MODEL = SETTINGS.task_models.task0c
_TASK1_MODEL  = SETTINGS.task_models.task1
_TASK2_MODEL  = SETTINGS.task_models.task2
_TASK4_MODEL  = SETTINGS.task_models.task4


_TASK0C_TIER = _model_tier(_TASK0C_MODEL)
_TASK1_TIER  = _model_tier(_TASK1_MODEL)
_TASK2_TIER  = _model_tier(_TASK2_MODEL)
_TASK4_TIER  = _model_tier(_TASK4_MODEL)

# Thinking directive (mid-tier only): injected at the start of generation prompts.
# Guides WHAT to reason about during the model's thinking budget before writing code.
# Omitted for weak-tier — they don't have a thinking budget; chain-of-thought in the
# prompt just adds tokens to the output rather than improving reasoning.
_TASK2_THINKING_DIRECTIVE = (
    "Before writing code: reason through all required public types, trait impls, "
    "module structure, and re-exports against the API surface and test assertions. "
    "Then write complete, compilable source.\n\n"
    if _TASK2_TIER == ModelTier.MID else ""
)

_FIX_THINKING_DIRECTIVE = (
    "Before writing fixes: trace the error to its root cause — identify the specific "
    "type mismatch, missing impl, or wrong signature — then write a targeted correction.\n\n"
    if _TASK2_TIER == ModelTier.MID else ""
)

# Verbosity guard (weak-tier only): appended after FILE_FORMAT_INSTRUCTION.
# Weak-tier models sometimes emit prose outside FILE markers despite the instruction;
# this explicit reminder reduces that failure mode. Mid-tier models follow reliably.
_TASK2_VERBOSITY_GUARD = (
    ""
    if _TASK2_TIER == ModelTier.MID
    else "Output ONLY the FILE markers above — no prose, no markdown fences, no text outside markers.\n"
)

# Task 1: requirements / contract extraction.
# Thinking directive guides the model to trace every behavioral contract to a specific
# test assertion before writing, preventing omissions and hallucination.
_TASK1_THINKING_DIRECTIVE = (
    "Before writing requirements: for each test assertion, trace exactly what behavior "
    "it verifies — function name, arguments, return value, invariants. Miss nothing.\n\n"
    if _TASK1_TIER == ModelTier.MID else ""
)

# Task 0c: coverage test generation.
# Thinking directive guides planning: which input values cover which branches, what
# assertion verifies each result. Prevents tests that execute code but don't assert.
_TASK0C_THINKING_DIRECTIVE = (
    "Before writing tests: for each uncovered function, determine which input values "
    "or states exercise each branch, and what assertion verifies the result. Then write.\n\n"
    if _TASK0C_TIER == ModelTier.MID else ""
)

# Task 0c: compile / test-block fix.
# Directs the model to identify the error class before writing the fix.
_TASK0C_FIX_THINKING_DIRECTIVE = (
    "Before writing the fix: identify the exact error — wrong type, wrong path, missing "
    "impl, bad import — then write the minimal correction.\n\n"
    if _TASK0C_TIER == ModelTier.MID else ""
)

# Task 4: genesis update.
# The prompts already prescribe a step-by-step analysis structure, so no thinking
# directive is added. Only a verbosity guard for weak-tier models that might miss the
# custom GENESIS_DELIM_START / GENESIS_DELIM_END markers.
_TASK4_VERBOSITY_GUARD = (
    ""
    if _TASK4_TIER == ModelTier.MID
    else "Use ONLY the delimiters shown above for output — no text outside them.\n"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Source code file extensions — the ONLY file types that may be created or overwritten
# by the model. Everything else (Cargo.toml, *.toml, *.json, *.proto, *.yaml, …) is
# unconditionally protected so the AI can never corrupt workspace config files.
MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Model-aware context budget (auto-detected from environment)
# ---------------------------------------------------------------------------
# start.sh already sets OLLAMA_NUM_CTX in .env based on measured free VRAM.
# Here we read it back and derive all content limits from it so that they
# automatically adapt to any hardware change without manual edits.
#
# Derivation: (num_ctx_tokens - prompt_overhead) * chars_per_token
#   prompt_overhead ≈ 6k tokens (instructions + system prompt + response buffer)
#   chars_per_token ≈ 4 (conservative; typical Rust/English source is 3.5–4.5)
#
# Hardware reference:
#   RTX 3090 24 GB, qwen3:32b Q4_K_M (~20 GB): OLLAMA_NUM_CTX=32768 → ~104k chars budget
#   Fallback (8192 tokens):                                           → ~26k chars budget

def _model_context_chars() -> int:
    """Return the safe content budget in chars for one model call.

    Thin shim: resolves env live → pipeline_core.domain.model_caps.model_context_chars.
    Reads env per-call (not SETTINGS) so late env changes recompute the budget.
    """
    backend = Backend.coerce(os.getenv("BACKEND"), Backend.OLLAMA)
    var = backend.max_len_env_var
    raw = os.getenv(var) if var else None
    max_len = int(raw) if raw and raw.strip().isdigit() else None
    return _model_context_chars_pure(MODEL, backend, max_len)


MODEL_CONTEXT_CHARS = _model_context_chars()

_GUARD_LOG        = os.getenv("PIPELINE_GUARD_LOG",   str(Path(__file__).parent / "logs" / "pipeline_guards.log"))
_PIPELINE_LOG     = os.getenv("PIPELINE_LOG",         str(Path(__file__).parent / "logs" / "pipeline_anyhow.log"))
_LOG_MAX_BYTES    = 512 * 1024   # keep last 512 KB in pipeline_anyhow.log
_LOG_TRIM_EVERY   = 50           # check file size every N _log() calls
_log_trim_counter = 0
_RUN_REGISTRY     = Path(os.getenv("PIPELINE_RUN_REGISTRY", "/tmp/determ_pipelines.json"))


def _guard(location: str, msg: str) -> None:
    """Log a content truncation event to both the pipeline log and the guard log file.

    Format: GUARD: [location] msg
    Grep `GUARD:` across pipeline log + guard log to find all truncations in one place.
    Also emits a `guard.fired` structured event so the JSONL log captures truncations.
    """
    import datetime
    line = f"  GUARD: [{location}] {msg}"
    _log(line)
    try:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with open(_GUARD_LOG, "a") as fh:
            fh.write(f"[{ts}] {line}\n")
    except Exception:
        pass
    _emit("guard.fired", data={"location": location, "msg": msg})


def _pipeline_parallelism() -> int:
    """Single global parallelism knob: PIPELINE_PARALLELISM (default 1).

    Governs every concurrent unit in the run — task0c split-and-merge degree,
    phase-C per-fn fill workers, and task2a concurrent clusters. Only LLM
    calls and per-cluster cargo (isolated target dirs) overlap; splice+cargo
    within one source tree stays serialised via _splice_lock_for().

    The legacy per-step knobs (PHASE_C_PARALLELISM, TASK0C_SPLIT_N) are dead;
    warn when set so a stale .env doesn't silently configure nothing.
    """
    global _legacy_parallelism_warned
    if not _legacy_parallelism_warned:
        for _legacy in ("PHASE_C_PARALLELISM", "TASK0C_SPLIT_N"):
            if os.getenv(_legacy) is not None:
                _guard("parallelism/legacy_knob_ignored",
                       f"{_legacy} is ignored — set PIPELINE_PARALLELISM instead")
        _legacy_parallelism_warned = True
    return max(1, int(os.getenv("PIPELINE_PARALLELISM", "1")))


_legacy_parallelism_warned = False


# ── Structured event log ───────────────────────────────────────────────────────
# Append-only JSONL at logs/events.jsonl. One JSON object per line. Cheap to
# write, crash-safe, tail-friendly, easy to ingest into SQLite later.
#
# Fixed schema fields (see backlog "Structured event log for pipeline observability"):
#   ts             ISO 8601 UTC, ms precision
#   product        e.g. "anyhow"  — set once by init_events()
#   pipeline_sha   determ HEAD short SHA — set once by init_events()
#   event          dot.notation verb: "llm.request", "coverage.measured", ...
#   (optional)     task, cluster, module, file, attempt, duration_ms, data
#
# Prompts/responses are content-addressed to logs/prompts/{sha256}.txt to keep
# events.jsonl small. ~90% of context is repeated across calls (genesis,
# profile, examples) so dedup is strong.

_EVENTS_LOG  = os.getenv("PIPELINE_EVENTS_LOG",
                         str(Path(__file__).parent / "logs" / "events.jsonl"))
_PROMPTS_DIR = Path(os.getenv("PIPELINE_PROMPTS_DIR",
                              str(Path(__file__).parent / "logs" / "prompts")))
_RUN_CONTEXT: dict[str, str] = {"product": "unknown", "pipeline_sha": "unknown"}
# Per-process run identity (debt #21b): tags every event so the C10 audit can
# scope a run by IDENTITY, not just a `ts >=` window — a concurrent xdist worker
# (distinct pid) writing to the SHARED events.jsonl no longer pollutes the
# window. pid+uuid: pid distinguishes concurrent processes, uuid guards pid reuse
# across sequential runs that read the same file. Echoed in the PIPELINE STARTED
# log line so `audit.load_run` recovers it.
_RUN_ID = f"{os.getpid()}-{_uuid4().hex[:8]}"
_RUN_CONTEXT["run_id"] = _RUN_ID


def init_events(product: str) -> None:
    """Initialize the event log: cache the product name and determ HEAD SHA."""
    _RUN_CONTEXT["product"] = product
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        _RUN_CONTEXT["pipeline_sha"] = result.stdout.strip() or "unknown"
    except Exception:
        _RUN_CONTEXT["pipeline_sha"] = "unknown"
    with suppress(Exception):
        _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def _store_prompt_content(text: str) -> dict[str, int | str]:
    """Content-address a prompt/response (domain PromptDigest). Returns
    {sha256, char_count} and writes logs/prompts/{sha256}.txt if not already
    present. Never raises."""
    digest = PromptDigest.of(text)
    if text:
        path = _PROMPTS_DIR / f"{digest.sha256}.txt"
        if not path.exists():
            with suppress(Exception):
                path.write_text(text, encoding="utf-8", errors="replace")
    return digest.as_dict()


# C-finale: the events.jsonl sink + `_phase` relocated to
# adapters.telemetry.JsonlEventEmitter; the emitter holds a live ref to
# `_RUN_CONTEXT` (mutated by init_events). Shims keep every call site stable.
# P3-S2/S3: the emitter is built in build_app (App.emitter) from these two
# late-bound residents; the shims below stay as the (test-patched) seam names.


def _emit(event: str, **fields) -> None:
    _APP.emitter.emit(event, **fields)


def _phase(*a, **k):
    return _APP.emitter.phase(*a, **k)


# Set once per run by _register_run; used to build the from-scratch disclaimer.
_CRATE_DISPLAY_NAME = ""


# Public `ask` shim removed (G/P6). Entry point is _ASK_CLIENT.ask(...);
# tests that intercept LLM calls patch pipeline._ask_raw (lower, stable seam).


# Maximum safe input chars, derived mathematically from the KV cache budget.
#
# model_interact._cap() guarantees at least min_content_tok output tokens when:
#   input_chars ≤ (ctx - thinking_budget - 512 - min_content_tok) × cpt_floor
#
# cpt_floor=3.0 matches the floor in model_interact._estimated_input_tokens (line ~846).
# Actual measured qwen3 cpt on Rust code is 3.1–3.6 consistently.
#
# History: the old empirical cap of 120_000 was a workaround for cpt_floor=2.0.
# With cpt=2.0 and a 199K char prompt (ptr.rs observed case), estimated input tokens
# = 99K > VLLM_MAX_MODEL_LEN=98304, so _cap() hit its 512-token minimum, leaving
# almost no content budget → model could not write a test block → empty response.
#
# REVERT if empty responses return or model output degrades (session 26 added this):
#   1. In model_interact.py ~line 846: max(3.0, _vllm_cpt) → max(2.0, _vllm_cpt)
#   2. Override the budget in Settings.from_env with the old empirical cap:
#        _THINKING_MODEL_CHAR_LIMIT = 120_000
#        single_call_char_limit = min(_THINKING_MODEL_CHAR_LIMIT, mcc * 3 // 4)
#      (the safe-input math now lives in domain.model_caps.safe_input_char_limit).
# slice 5b/Settings: the safe-input + single-call char budgets are now computed once in
# Settings.from_env (pipeline_core.settings, via domain.model_caps.safe_input_char_limit).
# These constants are single-sourced from SETTINGS; call sites are unchanged.
_SAFE_INPUT_CHAR_LIMIT        = SETTINGS.safe_input_char_limit
_SAFE_INPUT_CHAR_LIMIT_TASK0C = SETTINGS.safe_input_char_limit_task0c
SINGLE_CALL_CHAR_LIMIT        = SETTINGS.single_call_char_limit

# P6: guarded LLM ask client — focused adapter, composition-injected. `ask_raw`
# is late-bound because `_hot_reload_mi` rebinds the module global on disk change.
_ASK_CLIENT = AskClient(
    ask_raw=lambda *a, **k: _ask_raw(*a, **k),
    hot_reload=lambda: _hot_reload_mi(),
    emit=lambda *a, **k: _emit(*a, **k),
    guard=_guard,
    store_prompt_content=lambda s: _store_prompt_content(s),
    caller_site=_pclog.caller_site,
    disclaimer=lambda: _prompts_disclaimer(_CRATE_DISPLAY_NAME),
    model_name=MODEL, context_chars=MODEL_CONTEXT_CHARS,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
)

# Supplementary-material prompt caps — the per-section sizing policy is a domain
# VO (PromptBudgets) derived from the call budget; see domain/prompt_budget.py.
_BUDGETS = PromptBudgets.derive(SINGLE_CALL_CHAR_LIMIT)
_FIX_ERR_CAP = SETTINGS.fix_caps.fix_err_cap  # Settings passthrough, not a budget fraction

# Plan 10 U2: test-based acceptance. When on, a per-fn fill is accepted only if
# the fn's OWN tests pass (not just compile); test failures route through the
# same fix loop as compile errors. Default OFF — identical to legacy behavior —
# so it can be validated live before enabling. Fail-safe: any error in the test
# check falls back to compile-based acceptance.
_UNIFIED_FILL_LOOP = SETTINGS.fix_caps.unified_fill_loop
_U2_TEST_TIMEOUT   = SETTINGS.fix_caps.u2_test_timeout


# task0c output is a small test block (~500-2000 chars), so reserve only 2000 chars
# for the response — use almost the full context window for source+siblings+tests.
# Do NOT use SINGLE_CALL_CHAR_LIMIT here (that reserves 25% for large outputs like
# full source files or requirements docs, which task0c does not produce).
#
# task0c generates small test blocks — no 25% output reservation needed.
# Use the full safe input limit rather than the 75%-budget SINGLE_CALL_CHAR_LIMIT.
TASK0C_PROMPT_CHAR_CAP = _SAFE_INPUT_CHAR_LIMIT_TASK0C

# Per-file limit before semantic splitting (FR-001) now derives in build_app
# (App.run_deps.FILE_CHAR_LIMIT, from settings).

# Shared output format instruction injected into every prompt that expects <FILE> output.
# Explicitly forbids code fences — LLMs default to wrapping content in ```lang``` blocks,
# which breaks the extractor. Stating consequences ("will be ignored") improves compliance.
FILE_FORMAT_INSTRUCTION = """
CRITICAL OUTPUT FORMAT:
- Use ONLY these markers to output files — nothing else:

<FILE path="path/to/file">
file contents go here
</FILE>

- Do NOT wrap contents in code fences (no ```rust, ```python, ```json, or any other ```).
- Do NOT add any text, explanation, or commentary outside the FILE markers.
- Any response that does not use these exact markers will be silently discarded.
"""

# Max chars per intermediate group summary (stage-1 hierarchical synthesis).
# 50% of context leaves room for the merge prompt and the model's output.
INTERMEDIATE_CHAR_LIMIT = MODEL_CONTEXT_CHARS // 2

# Number of partials per intermediate group in stage-1 synthesis.
# When total partials <= this value, stage-1 is skipped entirely and all partials
# go directly to stage-2 (final synthesis). 20 covers argh (18 files) with no intermediate.
SYNTHESIS_GROUP_SIZE = 20

# Timeout in seconds for the full test subprocess.
TEST_TIMEOUT = 600

# Timeout in seconds for the compile-only build check.
BUILD_TIMEOUT = 120

# task0c stops augmenting once coverage reaches this percent (was hardcoded 100).
COVERAGE_AUGMENTATION_TARGET = float(os.getenv("COVERAGE_AUGMENTATION_TARGET", "98"))

# Backstop for the inner build-fix loop (TASK 2 + compile check). The
# real terminator is the per-loop floor-stall on rustc error count
# (`_t2_max_stalls`). Set to a high value so the stall budget always
# fires first under the "no hard caps on improvement" principle. Operators
# wanting a strict regen cap can override the env.
BUILD_CHECK_RETRIES = int(os.getenv("BUILD_CHECK_RETRIES", "10000"))

# Fix attempts after TASK 3 failure before escalating to TASK 4 + full regeneration.
# Each attempt asks the LLM to fix only the specific files causing test failures,
# preserving files that are already correct — same principle as TASK 0c.
TASK2_FIX_RETRIES = 2

# Max chars for genesis.md and functional_requirements.md when passed to task4.
# Set high enough that truncation never occurs in practice on a large local context.

# Directory for locked module source backups (bottom-up integration).
# Layout vocabulary now lives in pipeline_core.domain.workspace_layout.
from pipeline_core.domain.workspace_layout import (  # noqa: E402
    CHUNK_CACHE_DIR,
    COVERAGE_STRATEGY_NAME,
    PROJECT_CONTEXT_NAME,
    SOURCE_EXTENSIONS,
)

# Sentinel file written by TASK 0c when coverage augmentation is complete.

# Path to cargo-tarpaulin binary (installed via `cargo install cargo-tarpaulin`).
# Resolve against SUDO_USER's home when running under sudo, so `sudo continue.sh`
# doesn't look in /root/.cargo instead of the real user's home.
_real_home = Path(os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}") if os.environ.get("SUDO_USER") else str(Path.home()))
TARPAULIN_BIN = _real_home / ".cargo" / "bin" / "cargo-tarpaulin"
GRCOV_BIN     = _real_home / ".cargo" / "bin" / "grcov"
COVERAGE_BACKEND = SETTINGS.coverage_backend

# Pragma comment each backend recognises to exclude a line from coverage.
COVERAGE_IGNORE_PRAGMA = (
    "// # tarpaulin coverage ignore" if COVERAGE_BACKEND is CoverageBackend.TARPAULIN
    else "// GRCOV_EXCL_LINE"
)

# <FILE>-extraction + genesis delimiters relocated to domain.llm_output (slice 5b);
# imported at top. (R8.5: the dead `_CODE_BLOCK_FILE_PATTERN` — never consumed — removed.)

# Separator written into tests_output_N.txt between genesis snapshot and output.

# Manifest tracking which source files the model generated.

# Persistent log file written alongside the script.
LOG_NAME = "run.log"


# WORKSPACE_DIR (tmp/ for cloned/copied project repos) → domain.workspace_layout.

# Per-file attempt log for TASK 0c: JSONL, one entry per generation attempt (BL-002).

# Test/example file discovery moved to pipeline_core.adapters.discovery (P5).
# Re-exported here so existing references keep working through the shim.
SKIP_DIR_NAMES = _discovery.SKIP_DIR_NAMES

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Per-run state (git-managed / audit / log paths / progress) — one injected
# holder (the ClusterScope pattern) replacing the scattered run-globals.
_RUN = RunState()


def _log(msg: str):
    """Print to stdout and append to the run log (unless --nolog)."""
    global _log_trim_counter
    print(msg)
    sys.stdout.flush()
    if not _RUN.nolog and _RUN.log_path is not None:
        with _RUN.log_path.open("a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    _log_trim_counter += 1
    if _log_trim_counter >= _LOG_TRIM_EVERY:
        _log_trim_counter = 0
        _log_rotation.trim_head(_PIPELINE_LOG, _LOG_MAX_BYTES)


def _resolve_stdout_path() -> str | None:
    """Best-effort: when stdout is redirected (nohup ... > /tmp/foo.log) return
    the resolved target path so monitors can find the live log. Returns None
    when stdout is a tty or otherwise non-file."""
    try:
        if sys.stdout.isatty():
            return None
        return str(Path(f"/proc/{os.getpid()}/fd/1").resolve())
    except Exception:
        return None


def _register_run(project_dir: Path) -> None:
    """Build this run's coordinate entry and hand it to the file registry so
    monitor.py can auto-discover it. Sets the crate display name for the
    from-scratch disclaimer."""
    global _CRATE_DISPLAY_NAME
    _CRATE_DISPLAY_NAME = _crate_display_name(project_dir.name)
    entry = RunRegistryEntry(
        pid=os.getpid(),
        project=project_dir.name,
        project_dir=str(project_dir),
        pipeline_log=_PIPELINE_LOG,
        run_log=(str(_RUN.log_path) if _RUN.log_path else None),
        stdout_log=_resolve_stdout_path(),
        stream_log=os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG",
            str(Path(__file__).parent / "logs" / "stream.log"))),
        events_log=os.getenv("PIPELINE_EVENTS_LOG",
            str(Path(__file__).parent / "logs" / "events.jsonl")),
        guard_log=_GUARD_LOG,
        argv=sys.argv[1:],
        cwd=os.getcwd(),
        started_at=int(time.time()),
    )
    FileRunRegistry(_RUN_REGISTRY).register(entry.as_dict())


def init_log(root: Path, nolog: bool = False, progress_mode: bool = False):
    _RUN.nolog = nolog
    _RUN.progress_mode = progress_mode
    if not nolog:
        _RUN.log_path = root / LOG_NAME
    Path(_GUARD_LOG).parent.mkdir(parents=True, exist_ok=True)
    _RUN.run_started_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    for line in _banner_lines(Path(__file__), _RUN_ID, sys.argv,
                              _pipeline_parallelism()):
        _log(line)
    _register_run(root)
    _emit("parallelism.config", data={"parallelism": _pipeline_parallelism()})


# --- flow self-audit (--audit) ----------------------------------------------
# Runs pipeline_core.audit (the tools/flow_audit.py core) over THIS run's own
# records at iteration boundaries + on a timer; verdicts go to the log and a
# flow_audit.report event the monitor renders. Read-only and fail-safe — it
# observes the run, never steers it.
def _run_flow_audit(context: str) -> None:
    if not _RUN.audit_enabled:
        return
    try:
        from pipeline_core.audit import FAIL, audit
        verdicts = audit(Path(_EVENTS_LOG), _PROMPTS_DIR, _RUN.log_path,
                         _RUN.audit_project_dir, _RUN.run_started_iso,
                         int(os.getenv("AUDIT_PLATEAU_N", "3")))
        _log(f"  [audit] flow invariants ({context}):")
        for v in verdicts:
            _log(f"    {v.check.ljust(22)} {v.status:4s} {v.evidence}")
        _emit("flow_audit.report", data={
            "context": context,
            "failed": sum(1 for v in verdicts if v.status == FAIL),
            "verdicts": [{"check": v.check, "status": v.status,
                          "evidence": v.evidence} for v in verdicts],
        })
    except Exception as exc:
        _log(f"  [audit] skipped: {exc}")


def _start_audit_thread() -> None:
    import threading as _t
    interval = int(os.getenv("AUDIT_INTERVAL_SEC", "900"))

    def _loop() -> None:
        while True:
            time.sleep(interval)
            _run_flow_audit(f"periodic/{interval}s")

    _t.Thread(target=_loop, name="flow-audit", daemon=True).start()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# SEARCH/REPLACE patch protocol REMOVED. The model could not reliably
# reproduce an anchor byte-exact (flush-left vs indented bodies → no_match),
# and the anchor example leaked crate-specific source. Fix-loop edit
# granularity is now selected by FIX_LOOP_STRATEGY: `legacy` = whole-file
# rewrite (the proven path for small files), `per_fn` = per-fn body splice
# (for large files where whole-file rewrites truncate). See generate_fix_loop.




# C11/S77: application.parallel.generate_in_parallel with ask + guard bound.
generate_in_parallel = functools.partial(
    _generate_in_parallel, ask=_ASK_CLIENT.ask, guard=_guard)


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: read/write relocated to pipeline_core.io (imported at top).


# slice 5b foundation: read/write relocated to pipeline_core.io (imported at top).




# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).
# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).
# ---------------------------------------------------------------------------
# BL-010: Rust source condenser — signatures and type definitions only
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# BL-021: SQLite pipeline knowledge store
# ---------------------------------------------------------------------------
# Per-project SQLite knowledge store relocated to adapters/pipeline_db.py.
from pipeline_core.adapters.pipeline_db import (  # noqa: E402
    genesis_rules_count,
)

# ---------------------------------------------------------------------------
# BL-023: Test assertion extractor for TASK 2
# ---------------------------------------------------------------------------

# Cargo.toml accessors live in domain/cargo_toml.py (pure, tomllib — R8). Re-exported
# under the legacy `_`-names so the ~15 call sites stay unchanged.
_load_cargo_toml = _domain_load_cargo_toml
_cargo_package_name = _domain_cargo_package_name
_cargo_is_proc_macro = _domain_cargo_is_proc_macro
_cargo_default_features = _domain_cargo_default_features

# Workspace/dependency scanners live in adapters/workspace_scan.py (P4);
# bound in build_app (App.run_deps.scan_workspace_structure, S72).

# ---------------------------------------------------------------------------
# Module lock management
# ---------------------------------------------------------------------------

# load_locked_source_context → pipeline_core.adapters.artifacts.WorkspaceArtifacts.

# ---------------------------------------------------------------------------
# Workspace stub helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Failure classification (build error vs test logic failure)
# ---------------------------------------------------------------------------

# classify_failure → pipeline_core.domain.failure_classification (FailureCategory).


# ---------------------------------------------------------------------------
# Chunk extraction cache
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Failure log
# ---------------------------------------------------------------------------

# append_failure_log / load_failure_log → pipeline_core.adapters.failure_journal
# (FileFailureJournal), injected into the reconstruction use-cases directly.

# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


# ---------------------------------------------------------------------------
# Step output validators (anti-corruption layer) — relocated to pipeline_core (H/P6)
# ---------------------------------------------------------------------------
# validate_task1_output + validate_task4_output → pipeline_core.application.validate
# validate_task2_output + validate_task2_api_surface_conformance: thin wrappers below
# that bind the impure extractor deps at the composition root.
from pipeline_core.application.validate import (  # noqa: E402, F401  (smoke pin: validate_task4_output)
    validate_task4_output,
)

# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).
# validate_task4_output → pipeline_core.application.validate (H/P6), imported above.


# ---------------------------------------------------------------------------
# Test file collection -- language agnostic
# ---------------------------------------------------------------------------

# Discovery moved to pipeline_core.adapters.discovery (P5). Shims preserve the
# module-level names existing call sites and configure() wiring reference.
is_test_file = _discovery.is_test_file


def collect_test_files(project_dir: "Path") -> list[tuple[str, str]]:
    """#14: pass the gen_tests-decl predicate explicitly (was discovery.configure);
    `_is_gen_tests_decl_only_src` is late-bound (defined below) at call time."""
    return _discovery.collect_test_files(
        project_dir, is_gen_tests_decl_only=_is_gen_tests_decl_only)


format_test_files = _shaping.format_test_files


# chunk_test_files + chunk_test_files_by_file relocated to prompts.shaping (P6).
chunk_test_files = _shaping.chunk_test_files
chunk_test_files_by_file = _shaping.chunk_test_files_by_file


# ---------------------------------------------------------------------------
# Startup: discover and validate the test command from README.md
# ---------------------------------------------------------------------------

# discover_test_command → application.tasks.discover_test_command, bound in
# build_app (App.run_deps, S72).

# ---------------------------------------------------------------------------
# Source file manifest
# ---------------------------------------------------------------------------

# Source-file lifecycle (write/delete/lock + manifest/locked ledgers) →
# pipeline_core.adapters.workspace_files; re-bound here by name so the resident
# callers + dep-wiring (e.g. save_manifest_fn) keep resolving unchanged.
delete = _wsfiles.delete
save_manifest = _wsfiles.save_manifest
load_locked_modules = _wsfiles.load_locked_modules
lock_module = _wsfiles.lock_module
write_source_files = _wsfiles.write_source_files
delete_source_files = _wsfiles.delete_source_files


# clean_state → adapters.workspace_lifecycle, bound in build_app (App.run_deps, S72).

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# ---------------------------------------------------------------------------
# Build check (compile-only, no tests)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Compiler cluster system
# ---------------------------------------------------------------------------

# BuildCluster + ModuleAugmentState relocated to pipeline_core.domain.models (slice 6);
# imported at the top of this module and re-exported for unchanged call sites.


# Run-state: which compilation cluster is active + the full plan. Explicit,
# injectable object (pipeline_core.domain.cluster_scope.ClusterScope) replacing
# the former _current_cluster/_all_clusters module globals. Engine code reads
# `_SCOPE.active` at call time (e.g. _cargo_env); the augment loop activates each
# cluster via `_SCOPE` (it satisfies ports.augment.ClusterScope).
_SCOPE = ClusterScope()

# Cargo env builder (cluster-aware): pipeline_core.adapters.cargo.CargoEnv reads the
# active cluster from _SCOPE at call time. Zero-arg callable seam every cargo call binds.
_cargo_env = CargoEnv(_SCOPE)

# ---------------------------------------------------------------------------
# Git workspace helpers
# ---------------------------------------------------------------------------


# Slice 4b: git workspace I/O relocated to pipeline_core.adapters.workspace. These are
# thin shims delegating to the adapter with injected log/emit and the live `_git_managed`
# flag, until call sites migrate onto a GitWorkspace instance (slice 5).
# S3.5: WorkspaceDeps construction migrated into build_app; re-exported above as _APP.ws_deps.


# S80: git_commit/git_tag construct in build_app (live ws_deps + run_state.git_managed).

# git_clean_reset / setup_workspace → adapters.workspace, bound in build_app
# (App.run_deps, S72).

# _estimate_cluster_ceiling + _cluster_pre_check relocated to
# pipeline_core.adapters.augment_engine.CargoAugmentEngine (P6).


# ---------------------------------------------------------------------------
# Skeleton extractor (TASK 2 Phase A) — wraps the Rust binary built by setup.sh.
# ---------------------------------------------------------------------------

SKELETON_EXTRACTOR_BIN = Path(__file__).parent / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"

# Composition root (continued from SETTINGS): build the port adapters once, here where
# the extractor-binary path is in scope. `pipeline.py` is the __main__/composition layer
# (CODE_ARCHITECTURE.md §1), so it owns construction; call sites migrate onto ADAPTERS
# one task at a time (slice 5). Adapters wrap real primitives and never import back here.
ADAPTERS = build_adapters(SETTINGS, extractor_bin=SKELETON_EXTRACTOR_BIN, model=MODEL)
# Run a genesis audit every this many total module iterations (across all modules).
GENESIS_AUDIT_INTERVAL = 5
# After this many consecutive module iterations with no pass-count improvement,
# escalate to whole-project context in TASK 2.
NO_IMPROVEMENT_THRESHOLD = 3
# Phase 3 S1: the composition factory. main() will route through this; for now it
# owns the boundary providers (the extractor StructureReader) so subsequent stages
# repoint call sites + tests onto `_APP.*` (see refactoring.md §Phase 3).
# Phase 3: cross-cutting boundary callables, late-binding (lambdas read the live
# module globals so the e2e monkeypatch seam is preserved while wiring migrates).
_PORTS = _Ports(
    ask=lambda *a, **k: _ASK_CLIENT.ask(*a, **k),
    log=lambda m: _log(m),
    emit=lambda ev, **f: _emit(ev, **f),
    guard=lambda loc, msg: _guard(loc, msg),
    cargo_env=lambda: _cargo_env(),
)
_SKELETON_EXTRACTOR_BIN = (
    Path(__file__).parent / "tools" / "skeleton_extractor"
    / "target" / "release" / "skeleton-extractor"
)

_CFG = _CompositionConfig(
    task4_verbosity_guard=_TASK4_VERBOSITY_GUARD, extra_cap=_BUDGETS.extra,
    tarpaulin_bin=TARPAULIN_BIN, grcov_bin=GRCOV_BIN, build_timeout=BUILD_TIMEOUT,
    ignore_pragma=COVERAGE_IGNORE_PRAGMA,
    skip_dir_names=frozenset(SKIP_DIR_NAMES),
    intermediate_char_limit=INTERMEDIATE_CHAR_LIMIT,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    readme_cap=_BUDGETS.readme, profile_cap=_BUDGETS.profile,
    project_context_name=PROJECT_CONTEXT_NAME,
    failed_body_cap=_BUDGETS.failed_body, fix_err_cap=_FIX_ERR_CAP,
    coverage_strategy_name=COVERAGE_STRATEGY_NAME,
    no_improvement_threshold=NO_IMPROVEMENT_THRESHOLD,
    genesis_audit_interval=GENESIS_AUDIT_INTERVAL,
    module_plateau_abort=int(os.getenv("MODULE_PLATEAU_ABORT", "10")),
    stall_refill_iters=int(os.getenv("TASK2A_STALL_REFILL_ITERS", "2")),
    test_timeout=TEST_TIMEOUT,
    build_check_retries=BUILD_CHECK_RETRIES,
    task2_build_max_stalls=int(os.getenv("TASK2_BUILD_MAX_STALLS", "2")),
    task2_fix_retries=TASK2_FIX_RETRIES,
    synthesis_group_size=SYNTHESIS_GROUP_SIZE,
    chunk_cache_dir=CHUNK_CACHE_DIR,
    task1_thinking_directive=_TASK1_THINKING_DIRECTIVE,
    task2_thinking_directive=_TASK2_THINKING_DIRECTIVE,
    skeleton_extractor_bin=_SKELETON_EXTRACTOR_BIN,
    layout_authoring_enabled=os.getenv("ENABLE_LAYOUT_AUTHORING", "0") == "1",
    layout_retrigger_max_attempts=int(os.getenv("LAYOUT_RETRIGGER_MAX_ATTEMPTS", "2")),
    setup_script=Path(__file__).parent / "setup.sh",
)
# Phase 3 S3.5-10: resident pipeline helpers the migrated deps need, late-binding.
# Soft cap on stub members per impl block. Larger blocks fall back to the
# per-fn path so the output budget always fits. Anyhow's `impl Error` is
# the only block likely to exceed this in practice.
_PER_IMPL_MAX_STUBS = int(os.getenv("PHASE_C_IMPL_MAX_STUBS", "12"))

class _LateStructure:
    """P2 backward-compat: late-binds `_enumerate_fns_via_extractor` so existing tests
    that monkeypatch that pipeline shim are still honoured by the attribution use-case.
    New tests inject a real `StructureReader` fake via `AttributionDeps.structure`."""

    def enumerate_fns(self, src_dir: Path) -> "list[dict] | None":
        return _enumerate_fns_via_extractor(src_dir)


_FILE_CONSTRAINTS = FileConstraintsReader(
    configs_dir=Path(__file__).parent / "project_configs",
    is_nightly=lambda: _SCOPE.active is not None and not _SCOPE.active.is_default,
    log=_log,
)
_set_current: "Callable[..., None]" = _state_mod.set_current

_HELPERS = _Helpers(
    ask_retrospective=lambda *a, **k: _ask_retrospective(*a, **k),
    ask_per_fn_body=lambda *a, **k: _ask_per_fn_body(*a, **k),
    extract_fn_blocks=lambda *a, **k: _extract_fn_blocks(*a, **k),
    format_skeleton_index_for_cluster=lambda *a, **k: _format_skeleton_index_for_cluster(*a, **k),
    leaf_first_group_order=lambda *a, **k: _leaf_first_group_order(*a, **k),
    merge_fn_blocks_into_disk=lambda *a, **k: _merge_fn_blocks_into_disk(*a, **k),
    collect_test_files=lambda *a, **k: collect_test_files(*a, **k),
    enumerate_mods=lambda sd: _APP.structure.enumerate_mods(sd),
    task0c_augment_module=lambda pd, mn, md, crate_type="", **k: _task0c_impl(pd, mn, md, _APP.task0c_deps, crate_type, **k),
    scan_stub_records_via_app=lambda *a, **k: _APP.scan_stub_records_for_cluster(*a, **k),
    collect_impl_blocks_via_app=lambda *a, **k: _APP.collect_impl_blocks_for_cluster(*a, **k),
    classify_phase_c_test_failures=lambda *a, **k: _classify_phase_c_test_failures(*a, **k),
    set_current=lambda *a, **k: _set_current(*a, **k),
    cluster_gate_for=lambda *a, **k: _cluster_gate_for_sibling(*a, **k),
    bake_skeleton_artifacts=lambda *a, **k: _bake_skeleton_artifacts(*a, **k),
    describe_test_environment=lambda *a, **k: _describe_test_environment(*a, **k),
    generate_in_parallel=lambda *a, **k: generate_in_parallel(*a, **k),
    load_file_constraints=lambda *a, **k: _FILE_CONSTRAINTS.load(*a, **k),
    coverage_target=COVERAGE_AUGMENTATION_TARGET,
    task0c_prompt_char_cap=TASK0C_PROMPT_CHAR_CAP,
    task0c_fix_thinking_directive=_TASK0C_FIX_THINKING_DIRECTIVE,
    task0c_thinking_directive=_TASK0C_THINKING_DIRECTIVE,
    emit_per_file_test_overview=lambda *a, **k: _emit_per_file_test_overview(*a, **k),
    fix_diag=_fix_diag,  # the fix_diagnostics module (imported at top)
    gotchas_for_error=lambda *a, **k: _gotchas_for_error(*a, **k),
    gotchas_summary=lambda *a, **k: _gotchas_summary(*a, **k),
    render_impl_block=lambda *a, **k: _render_impl_block(*a, **k),
    settings_now=lambda: SETTINGS,
    store_prompt_content=lambda t: _store_prompt_content(t),
    ask_raw=lambda *a, **k: _ask_raw(*a, **k),
    pipeline_parallelism=lambda: _pipeline_parallelism(),
    propagate_or_quarantine_sibling=lambda gt, om, ps, pn, log, tag="": _membership.propagate_or_quarantine_sibling(gt, om, ps, pn, log, tag),
    task3_legacy_single=lambda *a, **k: _task3_legacy_single(*a, **k),
    task3_inline_retry=lambda *a, **k: _task3_inline_retry(*a, **k),
    source_extensions=SOURCE_EXTENSIONS,
    fix_thinking_directive=_FIX_THINKING_DIRECTIVE,
    task2_verbosity_guard=_TASK2_VERBOSITY_GUARD,
    structure_via_app=_LateStructure(),
    file_format_instruction=FILE_FORMAT_INSTRUCTION,
    build_dispatch_family_section=lambda *a, **k: _build_dispatch_family_section_pure(
        *a, **k, single_call_char_limit=SINGLE_CALL_CHAR_LIMIT),
    build_per_impl_prompt=lambda *a, **k: _build_per_impl_prompt(*a, **k),
    cargo_err_sig=lambda *a, **k: _cargo_err_sig(*a, **k),
    cargo_err_sig_from_diagnostics=lambda *a, **k: _cargo_err_sig_from_diagnostics(*a, **k),
    collect_sibling_filled_bodies=lambda *a, **k: _collect_sibling_filled_bodies(*a, **k),
    compute_cluster_macro_invariants=lambda *a, **k: _compute_cluster_macro_invariants(*a, **k),
    dispatch_families=lambda sd: _APP.structure.enumerate_dispatch_families(sd),
    extract_crate_idioms=lambda *a, **k: _extract_crate_idioms(*a, **k),
    extract_impl_where_clause=lambda *a, **k: impl_where_clause(*a, **k),
    find_tests_referencing_fn=lambda *a, **k: _find_tests_referencing_fn(*a, **k),
    module_of=lambda *a, **k: _module_of(*a, **k),
    types_section_for_fix=lambda *a, **k: _APP.fix_context.types_section_for_fix(*a, **k),
    bound_methods_section_for=lambda *a, **k: _APP.fix_context.bound_methods_section_for(*a, **k),
    u2_test_gate=lambda *a, **k: _u2_test_gate(*a, **k),
    test_regression_baseline=lambda *a, **k: _test_regression_baseline(*a, **k),
    test_regression_gate=lambda *a, **k: _test_regression_gate(*a, **k),
    per_impl_max_stubs=_PER_IMPL_MAX_STUBS,
    converge_in_cluster=lambda *a, **k: _converge_in_cluster(*a, **k),
    count_relevant_notes=lambda *a, **k: _count_relevant_notes(*a, **k),
    effective_stalls=lambda *a, **k: _effective_stalls(*a, **k),
    order_frontier=lambda *a, **k: _order_frontier(*a, **k),
    record_tdd_regression=lambda *a, **k: _record_tdd_regression_impl(
        *a, **k, enumerate_fns=_enumerate_fns_via_extractor,
        append_note=lambda *a, **k: _MODEL_NOTES_STORE.append(*a, **k),
        body_cap=_BUDGETS.failed_body),
    reset_exhaustion_flags=lambda *a, **k: _reset_exhaustion_flags(*a, **k),
    run_cargo=lambda *a, **k: _run_cargo(*a, **k),
    task2_fill_fns_one_by_one=lambda *a, **k: _task2_fill_fns_one_by_one(*a, **k),
    enumerate_types=lambda sd: _APP.structure.enumerate_types(sd),
    enumerate_fns=lambda *a, **k: _enumerate_fns_via_extractor(*a, **k),
    task3=lambda *a, **k: _APP.task3(*a, **k),
    run_flow_audit=lambda *a, **k: _run_flow_audit(*a, **k),
    load_model_notes=lambda *a, **k: _load_model_notes(*a, **k),
    genesis_rules_count=lambda *a, **k: genesis_rules_count(*a, **k),
    derive_gentest_prelude=lambda *a, **k: _derive_gentest_prelude(*a, **k),
    is_gen_tests_file=lambda *a, **k: _is_gen_tests_file(*a, **k),
    gen_tests_siblings=lambda *a, **k: _gen_tests_siblings(*a, **k),
    load_gen_tests_origin=lambda *a, **k: _load_gen_tests_origin(*a, **k),
    gen_tests_path=lambda *a, **k: _gen_tests_path(*a, **k),
    build_gen_tests_decl=lambda *a, **k: _build_gen_tests_decl(*a, **k),
    run_cargo_check_diag=lambda *a, **k: _run_cargo_check_diag(*a, **k),
    init_log=lambda *a, **k: init_log(*a, **k),
    init_events=lambda *a, **k: init_events(*a, **k),
    start_audit_thread=lambda: _start_audit_thread(),
)
# Phase 3 S3.4: TASK-1→4 reconstruction role callables, late-binding (the wrapped
# fns are defined later in this module; lambdas resolve them at call time).
# S76: the reconstruction roles construct in build_app (ReconstructRoles dissolved).
# P1: the real StructureReader — injected into the fill engine (no monkeypatch seam).
_STRUCTURE = _CliStructureReader(ADAPTERS.extractor)
# S23: telemetry as a standalone injected value (so module-level consumers like
# _CLUSTER_AUGMENTOR bind it without reading _APP — clears the last _APP-to-end blocker).
_TELEMETRY = LogTelemetry()
_APP: _App = _build_app(ADAPTERS, SETTINGS, _RUN, _SCOPE, _PORTS, _CFG, _HELPERS,
                  _STRUCTURE, _TELEMETRY,
                  ask_client=_ASK_CLIENT,
                  events_log=lambda: _EVENTS_LOG, run_context=_RUN_CONTEXT,
                  app_ref=lambda: _APP)
# S3.5/6/10: these deps are consumed only inside `def` bodies (lazy), so their
# construction migrates into build_app and they're re-exported here by name.
_VALIDATOR_DEPS = _APP.validator_deps  # smoke pin: test_pipeline_smoke uses pipeline._VALIDATOR_DEPS
# S64: the fill roles construct in build_app; this pin keeps the workspace-model
# adapter reachable for tests asserting the wired reader (test_structure_reader).
_FILL_WS = _APP.fill_roles.ws
# S71: structure caches live on the App; pins for the smoke tests + residual
# pipeline call sites (bound methods on the live instance — invalidate clears it).
_file_cfg_predicate = _APP.structure_cache.file_cfg_predicate
_fn_belongs_to_cluster = _APP.structure_cache.fn_belongs_to_cluster
_invalidate_mod_records_cache = _APP.structure_cache.invalidate
# S65: coverage deps + backend-selected provider construct in build_app.
_coverage_provider: _covmod.CoverageProvider = _APP.coverage_provider
# #23: build.rs cfg authority — prunes phantom clusters from source `#[cfg]` atoms.


# _LateStructure hoisted above _HELPERS (step 22).


# P2: composition root for the attribution use-case. Collaborators are late-bound
# (lambdas resolve the module globals at call time) so every existing monkeypatch
# seam keeps working; the AttributionDeps record is the clean DI rail a fake can fill.
# P3-S3 (step 22): AttributionDeps constructed in build_app (App.attribution_deps).
# The application-layer composition context. main()'s task dispatch migrates onto it
# slice by slice (5b); for now it bundles the resolved settings + adapters.
# Wire the cross-cutting infra seams to the pipeline's concrete sinks/config (slice 5b).
_pclog.set_sinks(_log, _emit)
_pcretry.configure(max_retries=MAX_RETRIES)








def _bake_skeleton_artifacts(project_dir: Path, module_name: str) -> dict:
    # C5 (Hard Rule 11): relocated to strip_bake.bake_skeleton_artifacts; shim binds deps.
    return _bake_skeleton_artifacts_impl(project_dir, module_name, _APP.bake_artifacts_deps)








# Per-fn fill rules relocated to application/reconstruct/fill_rules.py (P2);
# aliases keep engine call sites + pins unchanged.
from pipeline_core.application.reconstruct.fill_rules import (  # noqa: E402
    extract_fn_blocks as _extract_fn_blocks,
)
from pipeline_core.application.reconstruct.fill_rules import (  # noqa: E402
    module_of as _module_of,
)

# _merge_fn_blocks_into_disk → application/reconstruct/fill_merge.py (imported above).


# ---------------------------------------------------------------------------
# Per-fn TDD fill (Flavour B): one LLM call per unimplemented function.
# We own the splice site; the model returns only the body text.
# ---------------------------------------------------------------------------

# fn locate / pub-check / signature-block-extract are pure domain (imported as
# _locate_fn / _is_pub_fn / _extract_signature_block at the top). The
# file-MUTATING splices live in application/reconstruct/splice.py; pipeline.py
# keeps thin late-binding shims (NOT partials) so the e2e harness's
# `_enumerate_fns_via_extractor` monkeypatch is resolved at CALL time.










# _extract_body_response relocated to pipeline_core.domain.llm_output (G/P6)


# Attribute openers (`#[`, `#![`, incl. the spaced skeleton form `# ! [ doc … ]`)
# are located structurally via domain.rust_scan.attr_opener_end.


# doc-attr helpers → pipeline_core.domain.doc_render (H/§7.3); the source/prompt
# VIEWS (_condense_rust_to_signatures, _annotate_file_view) →
# application/reconstruct/source_view.py (imported above).


# Below this file-view size, keep doc comments in FULL (their usage examples are
# concrete guidance). Above it, condense to one-line intents to protect budget.
# ~1/6 of a single call → only the few genuinely huge files (e.g. error.rs) get
# condensed; the many small/medium files keep their examples.
_DOC_KEEP_FULL_LIMIT = SINGLE_CALL_CHAR_LIMIT // 6









# ---------------------------------------------------------------------------
# Model self-correction notes — stored in functional_requirements_{crate}.md
# under the `## Model Notes — Confusions to Avoid` section. The MODEL writes
# entries in its own voice after fix loops; the next attempt reads them as
# part of the per-fn prompt. See CLAUDE.md "The model authors its own
# anti-hallucination notes".
# ---------------------------------------------------------------------------

# Section parse/render (_preserve_model_notes, strip, dedup/rank/promote rules) →
# pipeline_core.domain.model_notes (regex-free). I/O + run-state → the store
# (adapters/model_notes_store.py), constructed in build_app (S64); pipeline.py
# keeps thin name-preserving wrappers so dep-injection sites + test seams are stable.
_MODEL_NOTES_STORE: "_FileModelNotesStore" = _APP.model_notes_store

_load_model_notes: Callable[..., list[str]] = _MODEL_NOTES_STORE.load










# Repository Specifications (2026-06-11): the crate's spec-document set —
# functional requirements + genesis (prose + structured rules) + API surface
# — injected into EVERY code-producing prompt from t2 onward via the prompts
# runtime facade. Loaded here (I/O + mtime cache), rendered by the pure
# pipeline_core.prompts.repo_spec selector.
_FR_PREFIX = _REPO_SPEC_FR_PREFIX


# _load_repo_spec_docs → adapters/repo_spec_docs.load_repo_spec_docs (S51).
_load_repo_spec_docs = _adapters_load_repo_spec_docs
from pipeline_core.adapters.repo_spec_docs import (  # noqa: E402
    make_repo_spec_section as _make_repo_spec_section,
)

# Slice 8/S75: the prompt builders live in pipeline_core/prompts/ (pure
# text→text); their impure providers now come from pipeline_core homes
# (budget-bound partials + the notes store) and are wired into the prompts
# runtime facade once at import time. The module aliases double as test pins.
_annotate_file_view = functools.partial(
    _annotate_file_view_impl, doc_keep_full_limit=_DOC_KEEP_FULL_LIMIT)
_repo_spec_section = _make_repo_spec_section(_BUDGETS.repo_spec)
_prompts_runtime.bind(_prompts_runtime.PromptRuntimeDeps(
    annotate_file_view=_annotate_file_view,
    promoted_error_rules=_MODEL_NOTES_STORE.promoted_error_rules,
    repo_spec_section=_repo_spec_section,
))


_SHUTDOWN_SENTINEL_PATH = Path("/tmp/pipeline_shutdown_on_finish")
# P7.2: per-fn cargo-check inner loop is now governed by a failure-floor
# stall budget against rustc error count. Hard cap was a counter — the
# user's "no hard limits" principle replaced that with floor-stall as the
# sole non-success exit. The 10k backstop is now a runaway safety net only;
# `PHASE_C_FN_MAX_STALLS` is the real terminator. Legacy env vars
# `PHASE_C_FN_HARD_CAP` and `PHASE_C_FIX_CAP_PER_FN` still work for
# operators who want to clamp behaviour explicitly.
_PER_FN_FIX_HARD_CAP = SETTINGS.fix_caps.per_fn_fix_hard_cap
_PER_FN_FIX_MAX_STALLS = SETTINGS.fix_caps.per_fn_fix_max_stalls
# P1: per-pass test-fix loop now governed by a failure-floor stall budget,
# not a flat count. Iterate as long as the minimum failure count keeps
# decreasing. Break when N consecutive iterations make no new minimum, or
# when the absolute hard cap fires (runaway safety). `PHASE_C_FIX_CAP_PER_PASS`
# is the legacy alias for the hard cap; kept for backward compatibility.
_PER_PASS_TEST_FIX_MAX_STALLS = SETTINGS.fix_caps.per_pass_test_fix_max_stalls
# Hard cap is a backstop value (10k) — floor-stall is the real exit.
_PER_PASS_TEST_FIX_HARD_CAP = SETTINGS.fix_caps.per_pass_test_fix_hard_cap

# Pass-through / focus-convergence smoke mode. With PIPELINE_PASS_THROUGH=1 every
# phase-c stall budget collapses to PIPELINE_PASS_THROUGH_STALLS (default 1): each
# fn/impl/test-fix loop does a couple of attempts then reverts-to-stub and moves on,
# giving an end-to-end run for controller/routing telemetry without grinding to
# convergence. PIPELINE_CONVERGE_MODULE names ONE module exempt from the collapse —
# it keeps full budgets and converges for real (the deterministic R3/R9 baseline).
_PASS_THROUGH = SETTINGS.fix_caps.pass_through
_PASS_THROUGH_STALLS = SETTINGS.fix_caps.pass_through_stalls
_CONVERGE_MODULE = SETTINGS.fix_caps.converge_module


def _effective_stalls(full_budget: int, module: str | None) -> int:
    """Stall budget for a phase-c loop, honoring pass-through/focus mode. Normal
    runs return full_budget unchanged. Under pass-through every module collapses to
    PIPELINE_PASS_THROUGH_STALLS EXCEPT the designated converge module, which keeps
    full_budget so it reaches real convergence."""
    if not _PASS_THROUGH:
        return full_budget
    if module and _CONVERGE_MODULE and module == _CONVERGE_MODULE:
        return full_budget
    return min(full_budget, _PASS_THROUGH_STALLS)


def _converge_in_cluster(cluster: "BuildCluster | None") -> str | None:
    """_CONVERGE_MODULE if its source file is active (not gated) in this cluster,
    else None. Cluster-scoped loops (TDD / per-pass test-fix) run whole-cluster, so
    they keep full budget whenever the converge module's tests live in the cluster."""
    if not _CONVERGE_MODULE:
        return None
    gated = {_module_of(f) for f in (cluster.gated_files or [])} if cluster else set()
    return _CONVERGE_MODULE if _CONVERGE_MODULE not in gated else None


# _improvement_loop (the callback->RefineStrategy adapter) was relocated to
# pipeline_core.application.refine_loop.run_callback_loop (P3.3) and is
# imported above as _improvement_loop. The 4 in-file callers (generate_fix_loop
# closures, _task3_inline_retry) move onto it directly in P3.4/P3.5.


# Retro/diagnosis calls MUST run with think=True (FR-028). The served
# Qwen3.6 model (vLLM + --reasoning-parser qwen3) returns EMPTY content under
# think=False — its hybrid-thinking path never populates `content`. Fill calls
# already use think=True and work. think=True was once dropped pipeline-wide for
# context-budget reasons, but that was the large fill prompts; retro INPUT is
# tiny, so we cap OUTPUT tokens instead of disabling thinking. The cap must be
# generous enough for reasoning + the 2-line answer to both emit (if reasoning
# alone exhausts the budget, `content` is truncated to empty again).
_RETRO_MAX_TOKENS = 6144


def _ask_retrospective(prompt: str, model: str | None = None) -> str:
    """Issue a retro/diagnosis prompt with think=True and a bounded output
    budget. One retry when the first response is empty (transient empty-content
    from the reasoning endpoint). Returns "" if both attempts come back empty.
    Raising callers keep their own try/except for true LLM exceptions."""
    result = ""
    for _attempt in range(2):
        result = _ASK_CLIENT.ask(prompt, model=model or _TASK2_MODEL,
                               think=True, max_tokens=_RETRO_MAX_TOKENS)
        if (result or "").strip():
            return result
    return result or ""






# Cargo diagnostic parsing relocated to application/reconstruct/cargo_errors.py
# (P2 slice 5, regex-free): the rendered-text error-signature fallback is now
# the structured `err_sig_from_text` scan (R7 standing rule).
# JSON-diagnostic cargo check relocated to application/reconstruct/cargo_runner.py
# (P2 slice 6): composes the Cargo PORT + diagnostics_from_json, per the port
# docstring ("JSON-diagnostic checks compose on top in the application layer").
import pipeline_core.application.reconstruct.cargo_runner as _reconstruct_cargo_runner  # noqa: E402
import pipeline_core.application.reconstruct.regression_guard as _regression_guard  # noqa: E402
from pipeline_core.application.reconstruct.cargo_errors import (  # noqa: E402
    err_sig as _cargo_err_sig,
)
from pipeline_core.application.reconstruct.cargo_errors import (  # noqa: E402
    err_sig_from_diagnostics as _cargo_err_sig_from_diagnostics,
)


def _run_cargo_check_diag(
    cmd: str,
    cwd: Path,
    env: dict,
    timeout: int = 180,
) -> "tuple[int, str, list[Diagnostic]]":
    return _APP.cargo_runner.check_diag(cmd, cwd, env, timeout)  # P3-S2: App port




def _run_cargo(cmd: str, cwd: Path, env: dict, timeout: int = 180) -> tuple[int, str]:
    # Port-composed runner (P2 slice 8): see application/reconstruct/cargo_runner.run_cargo.
    return _APP.cargo_runner.run(cmd, cwd, env, timeout)  # P3-S2: App port


# FR-024: every cargo test invocation must be wrapped in an improvement-driven
# loop. These helpers provide the metric (pass/fail counts), the attribution
# fallbacks (recently-filled queue, test-reverse map), and the stall budget.







def _u2_test_gate(
    fn_tests: "set[str]", unit_fns: "frozenset[FnLocator]",
    test_cmd: "str | None", target_src_dir: Path, cluster_env: "dict | None",
) -> "U2Verdict":
    """F3 — the SINGLE reusable test-acceptance gate. Returns a typed
    :class:`U2Verdict` (ACCEPT / ACCEPT_EXTERNAL_ONLY / REJECT): stub-panics
    that fire in a fn OUTSIDE `unit_fns` are subtracted so an innocent unit is
    never charged for another unit's still-`unimplemented!()` stub.

    Shim injects the patchable `_run_cargo` seam + `_guard`, the config, and the
    live `_APP.structure` (resolved at call time so a test's fake reader wins).
    """
    return _reconstruct_cargo_runner.u2_test_gate(
        _run_cargo, fn_tests, unit_fns, test_cmd, target_src_dir, cluster_env,
        structure=_APP.structure,
        enabled=_UNIFIED_FILL_LOOP, timeout=_U2_TEST_TIMEOUT,
        err_cap=_FIX_ERR_CAP, guard=_guard)


def _test_regression_baseline(tc: "str|None", cwd: Path, env: "dict|None") -> "tuple[int, frozenset]|None":
    return _regression_guard.baseline(_run_cargo, _U2_TEST_TIMEOUT, _UNIFIED_FILL_LOOP, tc, cwd, env)


def _test_regression_gate(tc: "str|None", cwd: Path, env: "dict|None", base: "tuple|None") -> "tuple[frozenset, str]|None":
    return _regression_guard.gate(_run_cargo, _guard, _U2_TEST_TIMEOUT, _UNIFIED_FILL_LOOP, tc, cwd, env, base)




# _ask_per_fn_body + _collect_sibling_filled_bodies relocated to pipeline_core (G/P6).
# Tests that patch pipeline._ask_per_fn_body / pipeline._collect_sibling_filled_bodies
# still work: the aliased names remain in this module's namespace.
from pipeline_core.adapters.per_fn_ask import (  # noqa: E402
    ask_per_fn_body as _ask_per_fn_body_impl,
)
from pipeline_core.application.reconstruct.sibling_context import (  # noqa: E402
    collect_sibling_filled_bodies as _collect_sibling_filled_bodies,
)

# S77: per-fn conversation protocol with the guarded client + task2 model bound.
_ask_per_fn_body: "Callable[..., tuple[str | None, str]]" = functools.partial(
    _ask_per_fn_body_impl, _ASK_CLIENT, model=_TASK2_MODEL)


# Retrospectives store relocated to pipeline_core/adapters/retros.py (P2);
# aliases keep engine call sites + smoke pins unchanged.
from pipeline_core.adapters.retros import (  # noqa: E402
    canon_container as _canon_container,  # noqa: F401  (smoke pin: test_canon_container_unifies_renderings)
)
from pipeline_core.adapters.retros import (  # noqa: E402
    reset_exhaustion_flags as _reset_exhaustion_flags,
)

# S3.9: PhaseCRetroDeps construction migrated into build_app; re-export by name.


def _count_relevant_notes(fr_path: Path | None,
                            fn_key: tuple[str, str]) -> int:
    """Count model-notes currently relevant to a (file, fn) — proxy for
    'has the model's accumulated knowledge about this fn grown since
    the prior attempt'. Used by the TDD driver's D3 re-entry policy.
    """
    if fr_path is None or not fr_path.exists():
        return 0
    try:
        fr_path.read_text(encoding="utf-8")  # readability probe; content unused
    except Exception:
        return 0
    rel, fn = fn_key
    section = _MODEL_NOTES_STORE.filter_for_prompt(
        fr_path, file_rel=rel, fn_name=fn,
        file_text="", current_error="",
    )
    return section.count("\n- ") if section else 0


# P3-S3: PhaseCTddDeps constructed in build_app (App.phase_c_tdd_deps).
# Typed boundary (plan #77 T1): partial binds only the deps — the fully typed
# keyword signature lives on the engine fn itself, no **kwargs hop to hide it.


# _PER_IMPL_MAX_STUBS moved above _HELPERS (P3-S3 value dependency).

# Per-source-tree splice+cargo locks. Originally one global lock (hoisted
# from `_task2_fill_fns_one_by_one` because the per-impl path also needs to
# serialise splice + cargo against any other worker mutating the same
# cluster's source tree). With PIPELINE_PARALLELISM driving clusters
# concurrently the lock must be PER TREE: workers inside one cluster still
# serialise (deterministic file mutations; cargo's target/.cargo-lock blocks
# concurrent invocations in one dir anyway), while distinct clusters —
# distinct .skeleton-filled/{c}/ trees with their own target/ — proceed in
# parallel.
# S75: the per-tree lock map lives in application/reconstruct/splice.py
# (process-global singleton preserved); alias doubles as the test pin.
import pipeline_core.application.reconstruct.splice as _reconstruct_splice  # noqa: E402

_splice_lock_for = _reconstruct_splice.splice_lock_for  # test pin: test_parallelism




# THE fix-loop driver — relocated to the application layer (P1) so task0c
# can drive it too; these aliases keep every call site + test pin unchanged.


# P3-S3: ImplFillDeps constructed in build_app (App.impl_fill_deps).
_phase_c_fill_impl_blocks: "Callable[..., ClusterFillResult]" = functools.partial(
    _impl_fill.fill_impl_blocks, _APP.impl_fill_deps)



# Leaf-first GROUP ordering stays aliased (used by the per-impl path); the per-fn
# pass ctx / scheduling / early-exit helpers are now consumed inside the
# application-layer use-cases, not here (P2).
from pipeline_core.application.reconstruct import (  # noqa: E402
    leaf_first_group_order as _leaf_first_group_order,
)

# ===========================================================================
# P2 / R3.3 — phase-C fill engine composition root.
#
# The per-fn fill unit (`FillOneFn`) and the per-cluster coordinator
# (`PhaseCFillEngine`) live in `application/reconstruct/`. `pipeline.py` is the
# composition root: the focused `adapters.fill_engine_roles` adapters (wired as
# `_FILL_*` below) bind the concrete engine helpers to the narrow
# `ports.fill_engine` roles, and `_task2_fill_fns_one_by_one` becomes a thin shim
# that assembles the use-cases and runs them. The adapters delegate via
# late-binding lambdas so test monkeypatches (scripted cargo/model,
# extractor-absent fallbacks) are honoured at call time.
# ===========================================================================
from pipeline_core.application.reconstruct.fill_one_fn import (  # noqa: E402
    FillCaps,
    FillOneFn,
)

# Back-compat re-export seams: these moved into the fill-engine use-cases (P2),
# but the unit suites pin them via `pipeline._x` — keep the aliases.
from pipeline_core.application.reconstruct.fill_rules import (
    failure_hit_unfilled_stub as _failure_hit_unfilled_stub,  # noqa: E402,F401
)
from pipeline_core.application.reconstruct.phase_c_engine import (  # noqa: E402
    EngineConfig,
    PhaseCFillEngine,
)
from pipeline_core.application.reconstruct.scheduling import (
    group_priority_levels as _group_priority_levels,  # noqa: E402,F401
)
from pipeline_core.prompts.per_fn import build_per_fn_prompt as _build_per_fn_prompt  # noqa: E402,F401

# S64: the fill-engine role adapters construct in build_app (`_APP.fill_roles`);
# the engine factory below sources them from the LIVE `_APP` at call time.


def _build_phase_c_engine(*, structure: "_structure_port.StructureReader | None" = None) -> "PhaseCFillEngine":
    """Composition root: bind the concrete helpers to the fill-engine ports and
    assemble the use-cases. Built per call so monkeypatched SETTINGS / parallelism
    (tests) are honoured at call time. `structure` lets a test inject a fake
    StructureReader (P1 constructor-DI seam) instead of monkeypatching the
    enumerate globals — it is swapped into the WorkspaceModel adapter only."""
    roles = _APP.fill_roles
    # P3-S2: default to the App's structure provider at CALL time, so a
    # test-injected _APP (build_app/replace with a fake reader) intercepts.
    ws = dataclasses.replace(
        roles.ws, structure=structure if structure is not None else _APP.structure)
    caps = FillCaps(
        per_fn_fix_hard_cap=_PER_FN_FIX_HARD_CAP,
        per_fn_fix_max_stalls=_PER_FN_FIX_MAX_STALLS,
        fix_err_cap=_FIX_ERR_CAP,
    )
    fill_one = FillOneFn(tel=roles.tel, mut=roles.mut, cargo=roles.cargo, prompt=roles.prompt,
                         retros=roles.retros, tests=roles.tests, stall=roles.stall, caps=caps)
    cfg = EngineConfig(
        parallelism=_pipeline_parallelism(),
        fill_unit=SETTINGS.phase_c_fill_unit,
        per_pass_test_fix_hard_cap=_PER_PASS_TEST_FIX_HARD_CAP,
        per_pass_test_fix_max_stalls=_PER_PASS_TEST_FIX_MAX_STALLS,
        pass_max_stalls=int(os.getenv("PHASE_C_PASS_MAX_STALLS", "2")),
        fix_err_cap=_FIX_ERR_CAP,
    )
    return PhaseCFillEngine(tel=roles.tel, mut=roles.mut, cargo=roles.cargo, prompt=roles.prompt,
                            ws=ws, retros=roles.retros, tests=roles.tests, stall=roles.stall,
                            impl_path=roles.impl, fill_one=fill_one, config=cfg,
                            behaviour_loader=lambda pd, c: load_behaviour_suite(pd, c).specs)


def _task2_fill_fns_one_by_one(
    *args: Any,
    structure: "_structure_port.StructureReader | None" = None,
    **kwargs: Any,
) -> "ClusterFillResult":
    """Phase C per-cluster fill engine shim → `PhaseCFillEngine.run` (the typed
    signature + pass/worker orchestration live there); `structure` is the P1
    test-injection seam (fake StructureReader)."""
    return _build_phase_c_engine(structure=structure).run(*args, **kwargs)




# P3-S3: GroupFillDeps constructed in build_app (App.group_fill_deps).








def _enumerate_fns_via_extractor(src_dir: Path) -> list[dict] | None:
    """Fn wire-dicts for the not-yet-decomposed engine call sites (P2–P4 will route
    them through the injected `StructureReader`). Logic + dict shape: CliStructureReader."""
    return _APP.structure.enumerate_fns(src_dir)  # P3-S2: via App port






# --- Per-impl-block unit of work -----------------------------------------
#
# The skeleton extractor tags every fn with `container` — the impl-block (or
# trait, or mod) header text that owns the fn. Grouping fns by (file,
# container) yields an impl-block view of the codebase. The per-impl Phase C
# path operates on these blocks instead of individual fns so that the model
# sees the impl-level design decisions (lifetime parameters, where-clauses,
# trait headers) when filling each member — solving the class of failures
# where the right fn body depends on a constraint declared at the impl level
# that per-fn prompts cannot show.


# _impl_block_id → domain.impl_routing.impl_block_id (P3-S5).






# ---------------------------------------------------------------------------
# Plan 4 — TDD-scoped driver: frontier extraction from cargo test output.
#
# A "frontier" maps (file_rel, fn_name) → set of test names that hit it at
# runtime. Built from cargo test output by parsing per-test stdout blocks
# for `not yet implemented` panics (and optionally assertion failures), then
# resolving panic file:line back to the containing fn via the syn extractor.
#
# The TDD outer loop in `_generate_one_cluster_tdd` uses this to drive
# what gets filled — only fns reachable from at least one failing test.
# ---------------------------------------------------------------------------




from pipeline_core.application.reconstruct.scheduling import (  # noqa: E402
    order_frontier as _order_frontier,
)

# ---------------------------------------------------------------------------
# Cluster-aware stub filter (P5 Step 1, focused fix). Filters stubs to only
# those whose enclosing cfg evaluates true for the current cluster. Skips
# fns that wouldn't compile in this cluster anyway (e.g., nightly.rs items
# being scanned in the stable cluster). Pure static analysis, no LLM.
# ---------------------------------------------------------------------------

# Inline `#[cfg(X)]` predicate extraction (incl. the spaced skeleton form
# `# [cfg (X)] mod foo ;`) is done via domain.cfg.cfg_inline_predicates.






# Cache of mod records per src_dir to avoid re-invoking the extractor for
# every fn lookup. Invalidated when target_src_dir's identity changes — the
# pipeline only ever queries one dir at a time during a cluster pass.




















# ---------------------------------------------------------------------------
# Pipeline state file — pipeline_state.md
#
# Tracks which phases completed, against which source hash, so restarts can
# validate cached results and resume from the right point.
#
# Format (one phase per line):
#   [phase_key]  status  hash=<16-char>  detail text
#
# status: done | in_progress | pending
# hash:   16-char SHA-256 prefix of the source inputs for that phase, or "-"
# ---------------------------------------------------------------------------



# Slice 9: the pipeline_state.md store moved to pipeline_core/adapters/state.py
# (regex-free line parser; same file format, locking, atomic replace). Aliases
# keep every call site + the task-deps records unchanged.
_mark_phase = _state_mod.mark_phase
_phase_valid: Callable[[Path, str], bool] = _state_mod.phase_valid




# task0_project_profile/context → application.tasks.task0_docs, bound in
# build_app (App.run_deps, S72).


# ---------------------------------------------------------------------------
# Requirements synthesis (TASK 1) — SOLID design
#
# Two implementations:
#   StaticTask1Synthesizer  — current approach: extract requirements from tests
#                             using natural-language descriptions of behavior.
#   LLMTask1Synthesizer     — contract-first / TDD: extract every method
#                             signature, type, trait, module path called from
#                             tests as explicit API contracts, then synthesize.
#
# Active implementation is selected by TASK1_SYNTHESIZER in .env:
#   TASK1_SYNTHESIZER=static   (conservative — current behavior)
#   TASK1_SYNTHESIZER=llm      (default — contract-first, more deterministic)
# ---------------------------------------------------------------------------

# API-surface section cap: ~25% of the single-call budget. The skeleton must
# survive truncation because TASK 1 treats it as ground truth — but we still
# bound it so it doesn't crowd out tests and README on large projects.
# Plan 9 F3: was SINGLE_CALL//4 (~79K) — dropped 38% of a 129K API surface at
# task1. Raised to //2 (~159K) so the whole API reaches FR synthesis; the
# context guard still protects total-prompt overflow.




# --- TASK 1 synthesizer engine relocated to application/generators (P4). The
# synthesizers are use-cases over the narrow `ports.synthesizer` roles (debt #14:
# replaced the old `configure()` global-injection seam). The focused role adapters
# (`adapters.task2_roles`, wired as `_GEN_*` below) bind BOTH the TASK-2 and TASK-1
# roles; `_build_task1_config()` resolves the constants into a VO. -----------------




# slice 5b: _task1_proc_macro moved to application.tasks.task1.




# ---------------------------------------------------------------------------
# Source code generation (TASK 2) — SOLID design
#
# Two implementations:
#   StaticTask2Generator  — current prompt-based approach (one big ask per attempt)
#   LLMTask2Generator     — file-by-file: first asks which files are needed, then
#                           generates each independently (better for large workspaces)
#
# Active implementation is selected by TASK2_GENERATOR in .env:
#   TASK2_GENERATOR=static    (default — proven, conservative)
#   TASK2_GENERATOR=llm       (experimental — file-by-file)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Source code generation (TASK 2) — Clean Architecture (P4)
#
# The Task2Generator family lives in
# `pipeline_core/application/generators/task2_generator.py` as use-cases over
# the narrow `ports.generator` roles. pipeline.py is the COMPOSITION ROOT: the
# focused role adapters (`adapters.task2_roles`, wired as `_GEN_*` below) bind the
# concrete helpers to those roles via late-binding lambdas (so test/scripted-harness
# them via build_app's generator factories (S64). Each role is a narrow ISP port.
#
# Selected by TASK2_GENERATOR in .env: static | llm | skeleton | filelist (default).
# ---------------------------------------------------------------------------
from pipeline_core.domain.generation import (  # noqa: E402
    build_gen_tests_decl as _build_gen_tests_decl,
)
from pipeline_core.domain.generation import (
    is_gen_tests_decl_only_src as _is_gen_tests_decl_only,
)

# P6: the `_Task2Adapter` god-object is dissolved — each TASK-1/TASK-2 generator
# role is a focused adapter (adapters.task2_roles), constructor-injected with
# late-binding lambdas over the resident composition-root callables (so the
# scripted-harness / test monkeypatches still intercept at call time). The
# generator/synthesizer use-cases receive narrow role ports (ISP) instead of one
# multi-role binding object.
# P6: cross-cluster skeleton merge — focused adapter, composition-injected.
# P6: mutation-testing behaviour hardening — focused adapter, composition-injected
# (ask late-bound so a test monkeypatching pipeline.ask still intercepts).
# _MUTATION constructed above _HELPERS (S43 definition-order).




# _format_whole_project_context → pipeline_core.adapters.artifacts.WorkspaceArtifacts.


# S79: generate_fix_loop constructs in build_app (app_ref-live deps); pin kept
# for the e2e routing/stub/fix-gate tests.
generate_fix_loop: "Callable[..., tuple[bool, str]]" = _APP.task0c_deps.generate_fix_loop


# slice 5b: deps for the extracted TASK 2 (defined after generate_fix_loop, its latest dep).
# P7.3: task3 inline retry. Currently task3 is one-shot; failure escalates
# straight to task4 + full regen. The inline retry catches the case where
# only a handful of fns broke a test — classifier identifies them, a single
# LLM call per fn fixes the body, cargo test re-runs. Bounded by stall budget
# and a snapshot/revert guard so we never leave the tree worse than we found it.

def _task3_inline_retry(
    project_dir: Path,
    test_command: str,
    iteration,
    initial_test_output: str,
    cluster: "BuildCluster | None" = None,
) -> tuple[bool, str]:
    """Composition-root shim -> per_fn_retry.run_task3_inline_retry (chunk H,
    deps migrated into build_app in S27). The deps' fill-role adapters +
    Helpers lambdas late-bind the module globals, so the scripted-cargo /
    scripted-model monkeypatch seams (test_e2e_build_fix / test_e2e_routing)
    still intercept at call time; env knobs are read per call in pipeline_core.
    """
    return _per_fn_retry.run_task3_inline_retry(
        project_dir, test_command, iteration, initial_test_output, cluster,
        deps=_APP.task3_retry_deps)


def _task3_legacy_single(
    project_dir, test_command, iteration, output_dir, genesis_path,
) -> tuple[bool, str]:
    """Composition-root shim -> legacy_test_run.run_legacy_single. Builds the
    deps fresh per call, binding the resident telemetry / cargo / io / inline-
    retry collaborators by name so the e2e monkeypatch seams (pipeline._emit,
    pipeline._run_cargo via inline-retry, …) still intercept at call time. The
    single-shot orchestration itself lives in pipeline_core."""
    return _run_legacy_single(
        project_dir, test_command, iteration, output_dir, genesis_path,
        LegacyTestRunDeps(
            phase=_phase,
            emit=_emit,
            log=_log,
            cargo_env=_cargo_env,
            parse_cmd=_parse_cmd,
            store_prompt_content=_store_prompt_content,
            task3_inline_retry=_task3_inline_retry,
            read=read,
            write=write,
            test_timeout=TEST_TIMEOUT,
        ),
    )






# ---------------------------------------------------------------------------
# TASK 1.5 — Static use-statement audit (no LLM)
# ---------------------------------------------------------------------------

# task1_static_audit → application/tasks/task1.py (S49); bound in build_app (S72).


# ---------------------------------------------------------------------------
# Genesis audit — detect and resolve contradictions in genesis.md
# ---------------------------------------------------------------------------


# Phase 3 S3 slice 1: genesis-audit deps now CONSTRUCTED in composition.build_app;
# pipeline re-exports the name so call sites + tests are unchanged.
_GENESIS_AUDIT_DEPS = _APP.genesis_audit_deps  # smoke pin




# ---------------------------------------------------------------------------
# Coverage augmentation (TASK 0c) — pre-strip step
# Generates unit tests (inline #[cfg(test)]) for each module until 100%
# line coverage is reached per tarpaulin.
# Must run before strip_to_tests.py (full implementation must be present).
# Trigger: pipeline.py --augment-tests
# ---------------------------------------------------------------------------

# _cargo_env (cluster-aware cargo env) is defined near _SCOPE above.


# slice 5b: deps records for the extracted TASK 3 / TASK 4. Defined here, after every
# collaborator they bind (notably _cargo_env). The task3/task4 shims above read these.
# S3.7: Task4Deps construction migrated into build_app; re-export by name.


def _describe_test_environment() -> str:
    """Thin shim binding the live cargo env + active cluster cfg; impl in
    prompts/test_environment.describe_test_environment."""
    active = _SCOPE.active
    return _describe_test_environment_impl(
        _cargo_env(),
        active.active_cfg if active is not None else None,
        active.excluded_cfg if active is not None else None)


# Slice 4b: coverage I/O relocated to pipeline_core.adapters.coverage. _COV_DEPS injects
# the pipeline's log + cargo-env + live-cluster accessor + tool paths/timeout/pragma so the
# adapter stays import-independent of pipeline.py. `cluster` is a callable (the active
# cluster is reassigned across the run; resolve at call time = original global semantics).
# S26: _COV_DEPS constructed standalone above (before _APP); also injected into build_app.




# S26: _coverage_provider constructed standalone above (before _APP); also injected
# into build_app. No longer an _APP re-export → unblocks the _APP-to-end move.


# Sibling-name parser + predicate live in discovery (P5); shims keep the
# private names the call sites already use.
_match_gen_tests_sibling = _discovery.match_gen_tests_sibling
_is_gen_tests_file: Callable[[str], bool] = _discovery.is_gen_tests_file


# Prelude imports injected into generated test modules when absent. Each row is
# (import_stmt, accepted `use`-prefixes, required symbol token): present when a
# `use <prefix>…<symbol>…` statement exists (R7: rust_scan.use_imports_symbol).


# _inject_allow_before_mod → domain/coverage_annotation.inject_allow_before_mod (S42).












def _cluster_gate_for_sibling(cluster, source_text: str, uncov_lines,
                              sibling_body: str = "",
                              project_dir: "Path | None" = None) -> str:
    """#26: the cfg gate to stamp on a gen_tests sibling. Gate it to its cluster
    only when the lines it targets are cluster-SPECIFIC (live inside a `#[cfg]`
    region that some cluster compiles out); a cluster-agnostic test is emitted
    UNGATED so a later cluster sees those lines already covered and skips
    re-augmenting them (kills the cross-cluster duplication). Only the default
    (stable) cluster shares — a non-default cluster keeps its gate (it runs after
    stable, so its agnostic lines are already covered; its real work is the gated
    delta). This is the documented-safe criterion: a `not(egma)` stable-only test
    stays gated to stable, never leaking into the nightly harness.

    #26 residual: even when the TARGET lines are agnostic, a body that CALLS a
    cluster-specific symbol (a fn compiled out under some cluster) would fail to
    compile if shared ungated into that cluster's harness. So keep the gate when
    `sibling_body` references any cluster-specific symbol — only a body that is
    both line-agnostic AND symbol-agnostic is shared."""
    # C4: relocated to cluster_membership.cluster_gate_for_sibling; shim binds
    # _SCOPE.all + a LAZY cluster-specific-symbols provider (extractor + cache).
    contexts = [_cfg.CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false) for c in _SCOPE.all]
    return _membership.cluster_gate_for_sibling(
        cluster, source_text, uncov_lines, sibling_body, _SCOPE.all,
        lambda: _APP.structure_cache.cluster_specific_symbols(project_dir, contexts))




# _select_relevant_siblings → application/tasks/task0c.select_relevant_siblings (S38).


# TestAugmentor (ABC) → ports/augment.py; StaticRustTestAugmentor →
# adapters/augment_engine.py (C10). The factory binds the cargo collaborators.






# _derive_gentest_prelude → adapters/gen_tests_storage.derive_gentest_prelude (S33);
# alias lives at the import (line ~100s) so every reference sees it defined.




# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# restart_from_augmented → App.restore_from_augmented.run, bound in build_app
# (App.run_deps, S72).

# ---------------------------------------------------------------------------
# Per-module pipeline helpers
# ---------------------------------------------------------------------------

# Strip subsystem moved to pipeline_core.adapters.strip (P5-S2). Shims keep the
# module-level names existing call sites reference.
strip_module_source = _strip_mod.strip_module_source
ORIGINAL_STRUCTURE_DIR = _strip_mod.ORIGINAL_STRUCTURE_DIR


# _api_surface_path → pipeline_core.adapters.artifacts (imported above as _api_surface_path).


# load_api_surface → pipeline_core.adapters.artifacts (imported above).


# slice 5b: deps for the extracted TASK 1 (defined after load_api_surface, its latest dep).
# P3-S3: Task1Deps constructed in build_app (App.task1_deps).


# Relocated to adapters/artifacts.py (P3-S5); alias keeps call sites + fakes stable.
load_original_structure: "Callable[[Path, str], list[str]]" = _artifacts_load_original_structure


# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


# --best-models-list body relocated to adapters.model_catalog.print_best_models.
cmd_best_models = _model_catalog.print_best_models


# P6: per-file generation constraints reader (config TOML I/O) — focused adapter,
# composition-injected with the configs dir + the active-cluster predicate + log.
# _FILE_CONSTRAINTS constructed above _HELPERS (S43 definition-order).


# slice 5b: deps for the extracted TASK 0c engine (defined before main(), after every
# collaborator). subprocess/git injected as the module; _current_cluster via a call-time
# getter (mutable global); coverage provider as a value.

# S28: deps for the relocated fix-loop body (fix_loop.generate_fix_loop).
# P3-S3 (step 31): GenerateFixLoopDeps constructed in build_app (App.gen_fix_loop_deps).


# P3-S3 (step 42): Task0cDeps constructed in build_app (App.task0c_deps).


# P6/S65: API-surface capture constructs in build_app; pin kept for tests
# (test_pipeline_smoke uses pipeline._API_SURFACE).
_API_SURFACE = _APP.api_surface
# S75 pins: cfg-scan + sibling-propagation now bind in build_app / _membership.
task0_cfg_scan = _APP.run_deps.task0_cfg_scan
_propagate_or_quarantine_sibling = _membership.propagate_or_quarantine_sibling

# F11 layout runners + strip/bake deps construct in build_app (S66).

# S24: the role adapters (_PHASE_GATE/…/_ANALYST) + spine use-cases
# (_LOCK_DEPS/_REATTACH_DEPS/_RECONSTRUCTION_STEP/_FAILURE_ANALYSIS/_COVERAGE_DOCS/
# _FLOW_AUDIT/_DOCS_METRICS/_FAILURE_JOURNAL/_WORKSPACE_ARTIFACTS/_TEST_RUNNER) are
# consumed only INSIDE build_app now → their pipeline re-exports were dead and deleted.
# main() reads only these two:


# S25: the TASK-0c augmentation cluster (_AUGMENT_STORE/_AUGMENT_ENGINE/
# _CLUSTER_AUGMENTOR) is built before _APP and exposed as _APP.augment_roles.

# C8: the standalone --phase-*-only / --skeleton-backend handlers live in the
# orchestrator; this binds them to the resident pipeline shims.
# P3-S3 (step 47): PhaseSubcommandDeps constructed in build_app.


# S72: RunDeps constructs in build_app (App.run_deps); main() reads it there.

def main():
    parser = _orchestrator.build_arg_parser()
    args = parser.parse_args()

    if args.best_models_list:
        cmd_best_models(vram_gb=args.vram_gb)
        sys.exit(0)

    root = Path(__file__).parent
    # Parse-don't-validate (#77 T1): the Namespace is read once, here.
    plan = _RunPlan.from_args(args)
    _orchestrator.run_pipeline(_APP.run_deps, plan, root)


def _shutdown_on_finish_enabled() -> bool:
    """Runtime authority for power-off-on-finish: the sentinel file (written
    "1"/"0" by the monitor's [p] toggle) overrides .env SHUTDOWN_ON_FINISH.
    No sentinel → .env default. /tmp clears on reboot → back to .env."""
    try:
        return _SHUTDOWN_SENTINEL_PATH.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return os.getenv("SHUTDOWN_ON_FINISH", "0").strip() == "1"


def _poweroff_on_finish() -> None:
    """Power off when the run ends on its own (any sys.exit or crash). Not on
    Ctrl-C; not on SIGTERM (restart.sh kill terminates the process before the
    finally block runs). Gated by _shutdown_on_finish_enabled — toggleable
    live from the monitor, like graceful stop."""
    if not _shutdown_on_finish_enabled():
        return
    _log("shutdown-on-finish enabled — powering off (sudo -n systemctl poweroff).")
    subprocess.run(["sudo", "-n", "systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    _interrupted = False
    try:
        main()
    except KeyboardInterrupt:
        _interrupted = True
        raise
    finally:
        if not _interrupted:
            _poweroff_on_finish()
