#!/usr/bin/env python3
"""
Pipeline for the deterministic process loop.

Usage:
  python pipeline.py --project <subfolder>
"""

import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import partial as _partial
from pathlib import Path
from typing import TypeVar

_RT = TypeVar("_RT")

import importlib

import model_interact as _mi
from model_interact import MODEL
from model_interact import ask as _ask_raw
from pipeline_core import log as _pclog
from pipeline_core import retry as _pcretry
from pipeline_core.adapters import build_adapters
from pipeline_core.adapters import coverage as _covmod
from pipeline_core.adapters import discovery as _discovery
from pipeline_core.adapters import model_catalog as _model_catalog
from pipeline_core.adapters import state as _state_mod
from pipeline_core.adapters import strip as _strip_mod
from pipeline_core.adapters import vllm_bootstrap as _vllm_bootstrap
from pipeline_core.adapters import workspace as _wsmod
from pipeline_core.adapters import workspace_scan as _workspace_scan
from pipeline_core.adapters.api_surface import ApiSurfaceCapture
from pipeline_core.adapters.artifacts import (
    WorkspaceArtifacts,
    load_api_surface,
    load_manifest,
)
from pipeline_core.adapters.artifacts import (
    api_surface_path as _api_surface_path,
)
from pipeline_core.adapters.artifacts import (
    load_type_names as _load_type_names,
)
from pipeline_core.adapters.artifacts import (
    type_manifest_path as _type_manifest_path,
)
from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.adapters.augment_engine import CargoAugmentEngine
from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.adapters.build_script import CargoBuildScriptReader
from pipeline_core.adapters.cargo import CargoEnv
from pipeline_core.adapters.cluster_merge import ClusterMerger
from pipeline_core.adapters.commit_gate import CallableCommitGate
from pipeline_core.adapters.coverage import CoverageDeps
from pipeline_core.adapters.failure_journal import FileFailureJournal
from pipeline_core.adapters.file_constraints import FileConstraintsReader
from pipeline_core.adapters.fill_engine_roles import (
    CargoFillAdapter,
    EngineTelemetryAdapter,
    ImplFillPathAdapter,
    MutatorAdapter,
    PromptContextAdapter,
    RetrosAdapter,
    StallPolicyAdapter,
    TestLinkerAdapter,
    WorkspaceModelAdapter,
)
from pipeline_core.adapters.mutation import MutationTester
from pipeline_core.adapters.ratchet import BestTreeRatchet
from pipeline_core.adapters.reconstruct_roles import (
    AnalystAdapter,
    CoverageDocsAdapter,
    DocsMetricsAdapter,
    FixerAdapter,
    FlowAuditAdapter,
    GeneratorAdapter,
    PhaseGateAdapter,
    RequirementsAdapter,
)
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.adapters.task2_roles import (
    CargoBackendAdapter,
    ClustersAdapter,
    ContextDocsAdapter,
    FillEngineAdapter,
    GeneratorTelemetryAdapter,
    GenTestsAdapter,
    LlmAskAdapter,
    ProgressFactoryAdapter,
    PromptShapingAdapter,
    RequirementsDocAdapter,
    SourceScanAdapter,
    WorkspaceAdapter,
)
from pipeline_core.adapters.telemetry import LogTelemetry
from pipeline_core.adapters.test_runner import CallableTestRunner
from pipeline_core.adapters.workspace import WorkspaceDeps
from pipeline_core.adapters.workspace_restore import RestoreFromAugmented
from pipeline_core.adapters.workspace_scan import (
    _dependency_graph_factory,
    _workspace_scanner_factory,
)
from pipeline_core.application import orchestrator as _orchestrator
from pipeline_core.application.cluster_augment import (
    AugmentContext,
    ClusterAugmentor,
)
from pipeline_core.application.gen_tests_reattach import (
    ReattachDeps,
)
from pipeline_core.application.generators.task1_synthesizer import (
    LLMTask1Synthesizer,
    StaticTask1Synthesizer,
    Task1Synthesizer,
)
from pipeline_core.application.reconstruct import attribution as _attribution
from pipeline_core.application.reconstruct import fix_diagnostics as _fix_diag
from pipeline_core.application.reconstruct import group_fill as _group_fill
from pipeline_core.application.reconstruct import impl_fill as _impl_fill
from pipeline_core.application.reconstruct import phase_c_tdd as _phase_c_tdd
from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.fix_unit import FixUnitDeps
from pipeline_core.application.reconstruct.generate_fix import (
    GenerateFixDeps,
    run_generate_fix,
)
from pipeline_core.application.reconstruct.impl_blocks import (
    collect_impl_blocks as _collect_impl_blocks_impl,
)
from pipeline_core.application.reconstruct.module_reconstructor import ModuleReconstructor
from pipeline_core.application.reconstruct.reconstruction_step import ReconstructionStep
from pipeline_core.application.reconstruct.requirements_phase import RequirementsPhase
from pipeline_core.application.reconstruct.retrospective import (
    PhaseCRetroDeps,
)
from pipeline_core.application.reconstruct.retrospective import (
    diagnose_phase_c as _diagnose_phase_c,
)
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.application.refine_loop import run_callback_loop as _improvement_loop
from pipeline_core.application.strip_bake import (
    StripBakeDeps,
)
from pipeline_core.application.strip_bake import (
    strip_and_bake_module as _strip_and_bake_module,
)
from pipeline_core.application.tasks import cfg_scan as _cfg_scan
from pipeline_core.application.tasks.discover_test_command import (
    DiscoverTestCommandDeps,
)
from pipeline_core.application.tasks.discover_test_command import (
    discover_test_command as _discover_test_command_impl,
)
from pipeline_core.application.tasks.genesis_audit import (
    GenesisAuditDeps,
    genesis_audit,
)
from pipeline_core.application.tasks.task0_docs import (
    Task0DocsDeps,
)
from pipeline_core.application.tasks.task0_docs import (
    task0_project_context as _task0_context_impl,
)
from pipeline_core.application.tasks.task0_docs import (
    task0_project_profile as _task0_profile_impl,
)
from pipeline_core.application.tasks.task0b_discovery import (
    Task0bDeps,
)
from pipeline_core.application.tasks.task0b_discovery import (
    task0b_discovery as _task0b_impl,
)
from pipeline_core.application.tasks.task0c import Task0cDeps
from pipeline_core.application.tasks.task0c import task0c_augment_module as _task0c_impl
from pipeline_core.application.tasks.task1 import Task1Deps
from pipeline_core.application.tasks.task1 import task1 as _task1_impl
from pipeline_core.application.tasks.task2 import (
    Task2Deps,
)
from pipeline_core.application.tasks.task2 import (
    task2 as _task2_impl,
)
from pipeline_core.application.tasks.task2 import (
    task2_fix_loop as _task2_fix_impl,
)
from pipeline_core.application.tasks.task2 import (
    task2_inner_loop as _task2_inner_impl,
)
from pipeline_core.application.tasks.task3 import Task3Deps, run_clusters_strict
from pipeline_core.application.tasks.task3 import task3 as _task3_impl
from pipeline_core.application.tasks.task4 import Task4Deps
from pipeline_core.application.tasks.task4 import task4 as _task4_impl
from pipeline_core.application.workspace_lifecycle import (
    save_gen_tests_siblings as _wl_save_gen_tests_siblings,
)
from pipeline_core.application.workspace_lifecycle import (
    wipe_module_sources as _wl_wipe_module_sources,
)
from pipeline_core.domain import attribution as _domain_attribution
from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain import cluster_plan as _cluster_plan
from pipeline_core.domain import model_report as _model_report
from pipeline_core.domain.cargo_toml import cargo_default_features as _domain_cargo_default_features
from pipeline_core.domain.cargo_toml import cargo_entry_point_paths as _domain_cargo_entry_point_paths
from pipeline_core.domain.cargo_toml import cargo_is_proc_macro as _domain_cargo_is_proc_macro
from pipeline_core.domain.cargo_toml import cargo_package_name as _domain_cargo_package_name
from pipeline_core.domain.cargo_toml import load_cargo_toml as _domain_load_cargo_toml
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.compile_probe import CompileProbe
from pipeline_core.domain.enums import (
    Backend,
    CoverageBackend,
    PhaseCDriver,
    PhaseCMode,
)
from pipeline_core.domain.failure_classification import (
    classify_failure as _classify_failure,
)
from pipeline_core.domain.fix_report import FixReport as _FixReport
from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.impl_block_ranges import impl_where_clause, parse_impl_block_ranges
from pipeline_core.domain.llm_output import (
    GENESIS_DELIM_END,
    GENESIS_DELIM_START,
    _extract_genesis,
    extract_files,
    parse_self_note,
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
from pipeline_core.domain.llm_output import (
    split_api_surface_by_file as _domain_split_api_surface_by_file,
)
from pipeline_core.domain.model_caps import (
    model_context_chars as _model_context_chars_pure,
)
from pipeline_core.domain.model_caps import (
    model_tier as _model_tier_pure,
)
from pipeline_core.domain.model_notes import (
    parse_note_bullets as _parse_note_bullets,
)
from pipeline_core.domain.model_notes import (
    preserve_model_notes as _preserve_model_notes,
)
from pipeline_core.domain.model_notes import (
    render_with_notes as _render_with_notes,
)
from pipeline_core.domain.model_notes import (
    slice_model_notes as _slice_model_notes,
)
from pipeline_core.domain.models import (
    BuildCluster,
    ConstRecord,
    Diagnostic,
    DispatchFamily,
    FnRecord,
    Frontier,
    ImplBlock,
    ModRecord,
    TypeRecord,
)
from pipeline_core.domain.refine import (
    parse_retrospective_categories as _parse_retrospective_categories,
)
from pipeline_core.domain.retry_outcome import (
    RetryMode,
    RetryVerdict,
    build_verdict,
    failing_test_verdict,
)
from pipeline_core.domain.rust_fn_headers import fn_headers, macro_headers
from pipeline_core.domain.rust_idents import (
    attr_call_args,
    camelcase_type_names,
    contains_fn_decl,
    crate_lint_attr_args,
    declares_fn,
    defined_type_names,
    defines_type,
    first_fn_decl_name,
    fn_decl_names,
    fn_reference_counts,
    identifier_tokens,
    impl_self_type,
    is_fn_signature_line,
    is_unsafe_fn,
    mask_identifiers,
    mixed_case_identifiers,
    use_symbol_paths,
)
from pipeline_core.domain.rust_macros import macro_definitions
from pipeline_core.domain.rust_scan import (
    after_brace_newline,
    branch_guard_label,
    contains_decl_keyword,
    contains_word,
    count_macro_calls,
    count_test_attrs,
    count_word,
    find_mod_opener,
    has_no_std_attr,
    has_nonempty_test_fn,
    is_rust_item_boundary_line,
    is_structural_brace_line,
    is_type_decl_line,
    item_names,
    module_use_imports,
    rust_code_fences,
    starts_with_pub,
    strip_comment_spans,
    strip_line_block_comments,
    use_imports_symbol,
)
from pipeline_core.domain.rust_scan import (
    rust_delimiters_balanced as _rust_delimiters_balanced,
)
from pipeline_core.domain.source_snapshot import SourceSnapshot
from pipeline_core.domain.test_results import (
    OUTPUT_SEPARATOR,
    _parse_test_counts,
    build_test_name_index,
    count_passing_tests,
    extract_test_output_from_file,
    insert_no_run,
    per_file_test_stats,
    rustc_error_signatures,
    validate_task3_output_strict,
)
from pipeline_core.domain.test_results import (
    parse_failed_tests as _parse_failed_tests,
)
from pipeline_core.domain.text import (
    extract_code_content as extract_code_content,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.text import (
    smart_truncate,
    strip_trailing_file_tag,
)
from pipeline_core.domain.text import (
    strip_markdown_fences as _strip_markdown_fences,
)
from pipeline_core.io import read, write
from pipeline_core.ports import structure as _structure_port
from pipeline_core.prompts import runtime as _prompts_runtime
from pipeline_core.prompts import shaping as _shaping
from pipeline_core.prompts.context import (
    PerFnFixSections,
)
from pipeline_core.prompts.per_fn import (
    build_per_fn_fix_prompt as _build_per_fn_fix_prompt,
)
from pipeline_core.prompts.per_impl import (
    build_per_impl_prompt as _build_per_impl_prompt,
)
from pipeline_core.prompts.repo_spec import (
    RepoSpecDocs,
    identifiers_for_target,
    render_repo_spec,
)
from pipeline_core.prompts.sections import (
    build_dispatch_family_section as _build_dispatch_family_section_pure,
)
from pipeline_core.prompts.sections import (
    format_type_for_prompt as _format_type_for_prompt,
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
from pipeline_core.retry import with_retry
from pipeline_core.settings import Settings

# Names re-exported through pipeline.py for backward-compatible call sites + the smoke
# suite (which references them as pipeline.X). Declaring them here documents the public
# surface and tells the linter these imports are intentional re-exports, not dead code.
__all__ = [
    "count_passing_tests", "validate_task3_output_strict", "extract_test_output_from_file",
    "GENESIS_DELIM_START", "GENESIS_DELIM_END", "_extract_genesis",
    "_tag_block", "with_retry", "_strip_markdown_fences",
]

_mi_mtime: float = os.path.getmtime(_mi.__file__)


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


def _model_tier(model: str) -> str:
    """Thin shim → pipeline_core.domain.model_caps.model_tier (returns 'mid'/'weak')."""
    return str(_model_tier_pure(model))


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
    if _TASK2_TIER == "mid" else ""
)

_FIX_THINKING_DIRECTIVE = (
    "Before writing fixes: trace the error to its root cause — identify the specific "
    "type mismatch, missing impl, or wrong signature — then write a targeted correction.\n\n"
    if _TASK2_TIER == "mid" else ""
)

# Verbosity guard (weak-tier only): appended after FILE_FORMAT_INSTRUCTION.
# Weak-tier models sometimes emit prose outside FILE markers despite the instruction;
# this explicit reminder reduces that failure mode. Mid-tier models follow reliably.
_TASK2_VERBOSITY_GUARD = (
    ""
    if _TASK2_TIER == "mid"
    else "Output ONLY the FILE markers above — no prose, no markdown fences, no text outside markers.\n"
)

# Task 1: requirements / contract extraction.
# Thinking directive guides the model to trace every behavioral contract to a specific
# test assertion before writing, preventing omissions and hallucination.
_TASK1_THINKING_DIRECTIVE = (
    "Before writing requirements: for each test assertion, trace exactly what behavior "
    "it verifies — function name, arguments, return value, invariants. Miss nothing.\n\n"
    if _TASK1_TIER == "mid" else ""
)

# Task 0c: coverage test generation.
# Thinking directive guides planning: which input values cover which branches, what
# assertion verifies each result. Prevents tests that execute code but don't assert.
_TASK0C_THINKING_DIRECTIVE = (
    "Before writing tests: for each uncovered function, determine which input values "
    "or states exercise each branch, and what assertion verifies the result. Then write.\n\n"
    if _TASK0C_TIER == "mid" else ""
)

# Task 0c: compile / test-block fix.
# Directs the model to identify the error class before writing the fix.
_TASK0C_FIX_THINKING_DIRECTIVE = (
    "Before writing the fix: identify the exact error — wrong type, wrong path, missing "
    "impl, bad import — then write the minimal correction.\n\n"
    if _TASK0C_TIER == "mid" else ""
)

# Task 4: genesis update.
# The prompts already prescribe a step-by-step analysis structure, so no thinking
# directive is added. Only a verbosity guard for weak-tier models that might miss the
# custom GENESIS_DELIM_START / GENESIS_DELIM_END markers.
_TASK4_VERBOSITY_GUARD = (
    ""
    if _TASK4_TIER == "mid"
    else "Use ONLY the delimiters shown above for output — no text outside them.\n"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Source code file extensions — the ONLY file types that may be created or overwritten
# by the model. Everything else (Cargo.toml, *.toml, *.json, *.proto, *.yaml, …) is
# unconditionally protected so the AI can never corrupt workspace config files.
SOURCE_EXTENSIONS = {
    # Systems / compiled
    ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".go", ".swift", ".kt", ".kts", ".java", ".cs", ".vb", ".fs", ".fsx",
    ".d", ".nim", ".cr", ".zig", ".v", ".odin",
    # Scripting
    ".py", ".rb", ".pl", ".pm", ".php", ".php3", ".php4", ".php5", ".phtml",
    ".lua", ".sh", ".bash", ".zsh", ".fish", ".tcl",
    # JVM / functional
    ".scala", ".sc", ".clj", ".cljs", ".cljc", ".groovy", ".gvy", ".gradle",
    ".ex", ".exs", ".hs", ".lhs", ".ml", ".mli", ".mll", ".mly",
    ".erl", ".hrl", ".elm", ".purs",
    # Web / JS
    ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
    ".coffee", ".vue", ".svelte",
    # Database / query
    ".sql",
    # Other
    ".r", ".R", ".m", ".mm", ".f", ".f90", ".f95", ".f03", ".f08",
    ".ada", ".adb", ".ads", ".pas", ".pp",
}

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


def _trim_pipeline_log() -> None:
    """Truncate logs/pipeline_anyhow.log to _LOG_MAX_BYTES by dropping the head."""
    try:
        path = _PIPELINE_LOG
        size = os.path.getsize(path)
        if size <= _LOG_MAX_BYTES:
            return
        with open(path, "r+b") as fh:
            fh.seek(-_LOG_MAX_BYTES, 2)
            tail = fh.read()
            nl = tail.find(b"\n")
            if nl >= 0:
                tail = tail[nl + 1:]
            fh.seek(0)
            fh.write(tail)
            fh.truncate()
    except Exception:
        pass


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
    """Content-address a prompt/response. Returns {sha256, char_count}.

    Writes to logs/prompts/{sha256}.txt if not already present. Never raises.
    """
    if not text:
        return {"sha256": "", "char_count": 0}
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    path = _PROMPTS_DIR / f"{h}.txt"
    if not path.exists():
        with suppress(Exception):
            path.write_text(text, encoding="utf-8", errors="replace")
    return {"sha256": h, "char_count": len(text)}


def _emit(event: str, **fields) -> None:
    """Append a structured event to logs/events.jsonl. Never raises."""
    try:
        record: dict = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "product": _RUN_CONTEXT.get("product", "unknown"),
            "pipeline_sha": _RUN_CONTEXT.get("pipeline_sha", "unknown"),
            "event": event,
            # Emitting thread — per-thread stream logs key on this name, so
            # the C10 audit can prove concurrent calls never shared a stream.
            "thread": threading.current_thread().name,
            # OS thread identity — lets C10 tell a real two-threads-share-a-name
            # collision (different tid) from benign same-thread reentrancy. debt #6.
            "tid": threading.get_ident(),
        }
        for k, v in fields.items():
            if v is not None:
                record[k] = v
        with open(_EVENTS_LOG, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass  # observability must never crash the pipeline


@contextmanager
def _phase(prefix: str, **fields):
    """Emit `{prefix}.start` on enter and `{prefix}.done` on exit with auto duration_ms.

    Yields a mutable dict the caller can update; the keys become extra fields on
    `.done`. Top-level keys (task, cluster, module, attempt, data) merge with
    `fields`; the `data` dict deep-merges so callers can add per-result info.

    On exception, `.done` is still emitted with `data.error = <ExceptionName>` so
    every `.start` has a matching `.done`. The exception is re-raised.
    """
    t0 = time.monotonic()
    _emit(f"{prefix}.start", **fields)
    extra: dict = {}
    err: str | None = None
    try:
        yield extra
    except BaseException as _e:
        err = type(_e).__name__
        raise
    finally:
        merged: dict = {**fields, **{k: v for k, v in extra.items() if k != "data"}}
        merged_data: dict = {**(fields.get("data") or {}), **(extra.get("data") or {})}
        if err is not None:
            merged_data["error"] = err
        if merged_data:
            merged["data"] = merged_data
        merged["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _emit(f"{prefix}.done", **merged)


# Set once per run by _register_run; used to build the from-scratch disclaimer.
_CRATE_DISPLAY_NAME = ""


def _from_scratch_disclaimer() -> str:
    """Anti-leakage system note: our impl is independent of any public crate of the same name.

    Local models often 'know' a popular crate (anyhow, serde, …) and silently reconstruct
    its real internals from memory. That defeats tests-only reconstruction. Stated on every
    call so any code-viewing step is reminded the bytes it sees are ours, not upstream's.
    """
    name = _CRATE_DISPLAY_NAME or "this crate"
    return (
        f"This project reuses the name `{name}`, but the implementation has been written "
        f"entirely from scratch and is COMPLETELY DIFFERENT from any public library known as "
        f"`{name}`. Do NOT assume types, signatures, internal structure, or behavior from the "
        f"upstream `{name}`. Reason ONLY from the code, signatures, and tests shown to you; "
        f"the structure of names and modules may differ from what you remember."
    )


def ask(prompt: str, system: str | None = None, model: str | None = None,
        max_tokens: int | None = None, think: bool | None = None) -> str:
    """Context-limit-enforcing model call. Thin shim over the relocated guarded
    client (`pipeline_core.adapters.ask_client.AskClient`); kept as the
    `pipeline.ask` name for the monkeypatch test seam + late-bind Deps."""
    return _ASK_CLIENT.ask(prompt, system, model, max_tokens, think)


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
    caller_site=_pclog.caller_site, disclaimer=_from_scratch_disclaimer,
    model_name=MODEL, context_chars=MODEL_CONTEXT_CHARS,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
)

# Supplementary material caps — scale with context so they don't freeze at
# values tuned for an old (small) context window.
_README_CAP    = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # ~5% of budget
_PROFILE_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 30)   # ~3% of budget
_GENESIS_CAP   = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # methodology doc
_FIX_ERR_CAP   = SETTINGS.fix_caps.fix_err_cap             # per-attempt error in fix loop
_REPO_SPEC_CAP = max(2_500, SINGLE_CALL_CHAR_LIMIT // 10)   # REPOSITORY_SPECIFICATIONS in per-fn/per-impl prompts
_REPO_SPEC_CAP_WIDE = max(5_000, SINGLE_CALL_CHAR_LIMIT // 5)  # …in whole-file fix prompts (bigger budget)
_STRATEGY_CAP  = max(4_000, SINGLE_CALL_CHAR_LIMIT // 15)   # coverage strategy doc
_SIBLING_CAP   = max(3_000, SINGLE_CALL_CHAR_LIMIT // 30)   # per-sibling condensed source
_SRC_SNIP_CAP  = max(4_000, SINGLE_CALL_CHAR_LIMIT // 10)   # source snippet in task0c prompts
_EXTRA_CAP     = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # extra context files (Cargo.toml etc.)
_WORKING_CAP   = max(800,   SINGLE_CALL_CHAR_LIMIT // 100)  # "what worked before" hint

# Per-fn / per-impl reconstruction context (Plan 9 F1/F2). Types+traits are the
# highest-value context for body reconstruction — they drive the dominant
# E0277/E0599/E0046/E0053 errors. Budget-derive generously; do NOT freeze at
# 8192-era hardcoded values (3000 chars / 6 types). The char cap governs; the
# count cap is only a runaway safety net.
_TYPES_SECTION_CAP = max(8_000, SINGLE_CALL_CHAR_LIMIT // 3)   # type/trait defs in fill prompt
_TYPES_COUNT_CAP   = max(40,    SINGLE_CALL_CHAR_LIMIT // 6000)  # safety net only; chars govern
_FAILED_BODY_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 12)  # prior failed body in fix prompt

# Plan 10 U2: test-based acceptance. When on, a per-fn fill is accepted only if
# the fn's OWN tests pass (not just compile); test failures route through the
# same fix loop as compile errors. Default OFF — identical to legacy behavior —
# so it can be validated live before enabling. Fail-safe: any error in the test
# check falls back to compile-based acceptance.
_UNIFIED_FILL_LOOP = SETTINGS.fix_caps.unified_fill_loop
_U2_TEST_TIMEOUT   = SETTINGS.fix_caps.u2_test_timeout


def prompt_budget(*fixed_parts: str) -> int:
    """Chars remaining for dynamic content (binds the configured char limit to the
    relocated `prompts.shaping.prompt_budget`)."""
    return _shaping.prompt_budget(SINGLE_CALL_CHAR_LIMIT, *fixed_parts)


# task0c output is a small test block (~500-2000 chars), so reserve only 2000 chars
# for the response — use almost the full context window for source+siblings+tests.
# Do NOT use SINGLE_CALL_CHAR_LIMIT here (that reserves 25% for large outputs like
# full source files or requirements docs, which task0c does not produce).
#
# task0c generates small test blocks — no 25% output reservation needed.
# Use the full safe input limit rather than the 75%-budget SINGLE_CALL_CHAR_LIMIT.
TASK0C_PROMPT_CHAR_CAP = _SAFE_INPUT_CHAR_LIMIT_TASK0C

# Per-file limit before semantic splitting (FR-001).
FILE_CHAR_LIMIT = min(_SAFE_INPUT_CHAR_LIMIT, MODEL_CONTEXT_CHARS * 85 // 100)

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

# Persistent cache directory for chunk extraction results.
# Keyed by content hash — survives across iterations and restarts.
CHUNK_CACHE_DIR = ".chunk_cache"

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
TASK4_DOC_LIMIT = 200_000

# Directory for locked module source backups (bottom-up integration).
# Layout vocabulary now lives in pipeline_core.domain.workspace_layout.
from pipeline_core.domain.workspace_layout import (  # noqa: E402
    CLUSTER_AUGMENTED_DIR,
    FAILURE_LOG_NAME,
    LOCKED_MODULES_DIR,
    MODULE_AUGMENTED_DIR,
    MODULE_STRIPPED_DIR,
)

# Sentinel file written by TASK 0c when coverage augmentation is complete.
COVERAGE_AUGMENT_SENTINEL = ".coverage_augmented"

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
MANIFEST_NAME = ".generated_files.txt"

# Persistent log file written alongside the script.
LOG_NAME = "run.log"

# Project profile: produced once at startup, stable across all iterations.
PROJECT_PROFILE_NAME = "project_profile.md"

# Behavioral context synthesized from test content (README substitute for README-less projects).
PROJECT_CONTEXT_NAME = "project_context.md"

# Hash file that invalidates the profile and context cache when project inputs change.
PROJECT_INPUTS_HASH_NAME = "project_profile.inputs_hash"

# Discovery pass output: import paths, type construction patterns, feature flags (BL-001).
COVERAGE_STRATEGY_NAME = "coverage_strategy.md"
COVERAGE_STRATEGY_HASH_NAME = "coverage_strategy.inputs_hash"
PIPELINE_STATE_NAME = _state_mod.PIPELINE_STATE_NAME  # single-sourced (slice 9)

# Workspace directory for cloned/copied project repos (relative to pipeline root).
WORKSPACE_DIR = "tmp"

# Compiler cluster plan — derived from source, written once per project.
BUILD_CONFIGS_NAME = "build_configs.md"

# Per-file attempt log for TASK 0c: JSONL, one entry per generation attempt (BL-002).

# Test/example file discovery moved to pipeline_core.adapters.discovery (P5).
# Re-exported here so existing references keep working through the shim.
TEST_DIR_NAMES = _discovery.TEST_DIR_NAMES
SKIP_DIR_NAMES = _discovery.SKIP_DIR_NAMES

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_path: Path = None
_nolog: bool = False        # --nolog: skip writing to run.log
_progress_mode: bool = False  # --progress: compact \r-rewriting terminal progress


def _log(msg: str):
    """Print to stdout and append to the run log (unless --nolog)."""
    global _log_trim_counter
    print(msg)
    sys.stdout.flush()
    if not _nolog and _log_path is not None:
        with _log_path.open("a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    _log_trim_counter += 1
    if _log_trim_counter >= _LOG_TRIM_EVERY:
        _log_trim_counter = 0
        _trim_pipeline_log()


def _git_file_hash(root: Path, filename: str) -> str:
    # Slice 4b: relocated to adapters.workspace; thin shim until call sites migrate.
    return _wsmod.git_file_hash(root, filename)


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
    """Write this run's coordinates to the shared registry so monitor.py can
    auto-discover it. Idempotent: also prunes dead pids."""
    global _CRATE_DISPLAY_NAME
    _name = project_dir.name
    for _suffix in ("-master", "-main", "-trunk"):
        if _name.endswith(_suffix):
            _name = _name[: -len(_suffix)]
            break
    _CRATE_DISPLAY_NAME = _name
    try:
        pid = os.getpid()
        stdout_log = _resolve_stdout_path()
        entry: dict = {
            "pid": pid,
            "project": project_dir.name,
            "project_dir": str(project_dir),
            "pipeline_log": _PIPELINE_LOG,
            "run_log": (str(_log_path) if _log_path else None),
            "stdout_log": stdout_log,
            "stream_log": os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG",
                str(Path(__file__).parent / "logs" / "stream.log"))),
            "events_log": os.getenv("PIPELINE_EVENTS_LOG",
                str(Path(__file__).parent / "logs" / "events.jsonl")),
            "guard_log": _GUARD_LOG,
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "started_at": int(time.time()),
        }
        reg: list[dict] = []
        if _RUN_REGISTRY.exists():
            try:
                reg = json.loads(_RUN_REGISTRY.read_text())
                if not isinstance(reg, list):
                    reg = []
            except Exception:
                reg = []
        # Drop dead pids and any duplicate of our own pid.
        reg = [r for r in reg
               if r.get("pid") != pid and Path(f"/proc/{r.get('pid')}").exists()]
        reg.append(entry)
        _RUN_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        _RUN_REGISTRY.write_text(json.dumps(reg, indent=2))

        import atexit
        def _unregister() -> None:
            try:
                if not _RUN_REGISTRY.exists():
                    return
                cur = json.loads(_RUN_REGISTRY.read_text())
                cur = [r for r in cur if r.get("pid") != pid]
                _RUN_REGISTRY.write_text(json.dumps(cur, indent=2))
            except Exception:
                pass
        atexit.register(_unregister)
    except Exception as e:
        # Never let registry bookkeeping break a real run.
        with suppress(Exception):
            _log(f"  (registry write failed: {e})")


def init_log(root: Path, nolog: bool = False, progress_mode: bool = False):
    global _log_path, _nolog, _progress_mode
    _nolog = nolog
    _progress_mode = progress_mode
    if not nolog:
        _log_path = root / LOG_NAME
    Path(_GUARD_LOG).parent.mkdir(parents=True, exist_ok=True)
    _log(f"\n{'=' * 60}")
    _log(f"{'=' * 60}")
    _py_hash = hashlib.md5(Path(__file__).read_bytes()).hexdigest()[:8]
    _git_hash = ""
    try:
        import subprocess as _sp
        _git_hash = " git=" + _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=_sp.DEVNULL, text=True
        ).strip()
    except Exception:
        pass
    global _RUN_STARTED_ISO
    _RUN_STARTED_ISO = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    _log(f"  PIPELINE STARTED — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC  pipeline.py={_py_hash}{_git_hash}")
    _log(f"  CMD: {sys.executable} {' '.join(sys.argv)}")
    _log(f"  PIPELINE_PARALLELISM={_pipeline_parallelism()}")
    _log(f"{'=' * 60}")
    _log(f"{'=' * 60}")
    _register_run(root)
    _emit("parallelism.config", data={"parallelism": _pipeline_parallelism()})


# --- flow self-audit (--audit) ----------------------------------------------
# Runs pipeline_core.audit (the tools/flow_audit.py core) over THIS run's own
# records at iteration boundaries + on a timer; verdicts go to the log and a
# flow_audit.report event the monitor renders. Read-only and fail-safe — it
# observes the run, never steers it.
_AUDIT_ENABLED = False
_RUN_STARTED_ISO = ""                 # set by init_log; scopes verdicts to this run
_AUDIT_PROJECT_DIR: "Path | None" = None


def _run_flow_audit(context: str) -> None:
    if not _AUDIT_ENABLED:
        return
    try:
        from pipeline_core.audit import FAIL, audit
        verdicts = audit(Path(_EVENTS_LOG), _PROMPTS_DIR, _log_path,
                         _AUDIT_PROJECT_DIR, _RUN_STARTED_ISO,
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
# Progress bar
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _render_progress(current: int, total: int, avg_seconds: float | None,
                     bar_width: int = 30) -> str:
    """
    Render a single-line progress bar string.
    Example: [=====>    ] 45/276 (16%) | avg 2m04s/chunk | ETA ~7h21m
    """
    pct = current / total if total else 0
    filled = int(bar_width * pct)
    arrow = "=" * filled + (">" if filled < bar_width else "")
    bar = f"[{arrow:<{bar_width}}]"
    base = f"{bar} {current}/{total} ({pct:.0%})"
    if avg_seconds and avg_seconds > 0:
        remaining = avg_seconds * (total - current)
        base += f" | avg {_fmt_duration(avg_seconds)}/chunk | ETA ~{_fmt_duration(remaining)}"
    return base


class ChunkProgress:
    """
    Tracks per-chunk timing and emits progress output after each chunk.

    Default mode  : prints a new line per chunk (log-friendly).
    --progress mode: overwrites the same terminal line with \\r (clean live view).
    """

    def __init__(self, total: int, rolling_window: int = 5):
        self.total = total
        self.window = rolling_window
        self._times: list[float] = []   # recent chunk durations
        self._local = threading.local()  # per-thread start time (concurrent chunks)
        self._lock = threading.Lock()     # guards _times/_last_line_len/print
        self._last_line_len: int = 0    # for \r padding in progress mode

    def start_chunk(self):
        self._local.chunk_start = time.monotonic()

    def finish_chunk(self, current: int):
        elapsed = time.monotonic() - getattr(self._local, "chunk_start", time.monotonic())
        with self._lock:
            self._times.append(elapsed)
            if len(self._times) > self.window:
                self._times.pop(0)
            avg = sum(self._times) / len(self._times) if self._times else None

            line = _render_progress(current, self.total, avg)

            if _progress_mode:
                # Overwrite previous line in terminal.
                padded = line.ljust(self._last_line_len)
                print(f"\r{padded}", end="", flush=True)
                self._last_line_len = len(line)
            else:
                # New line — works in log files and when tailing.
                _log(f"  {line}")

    def finish(self):
        """Print a final newline when in --progress mode."""
        if _progress_mode and self._last_line_len:
            print()  # move past the \r line


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


def extract_test_mod(text: str) -> str:
    """Brace-counter extraction of a gen_tests mod block.

    Finds the first #[cfg(test)] in text, scans forward past any attribute
    lines (#[allow(...)], etc.) to the opening brace of mod gen_*, then walks
    character-by-character counting brace depth until the block closes.
    Returns everything from #[cfg(test)] to the closing }, or "" if not found.
    No regex — works regardless of what the model puts between cfg and mod.
    """
    cfg_pos = text.find('#[cfg(test)]')
    if cfg_pos == -1:
        return ""
    mod_pos = text.find('mod gen_', cfg_pos)
    if mod_pos == -1:
        mod_pos = text.find('mod gen_tests', cfg_pos)
    if mod_pos == -1:
        return ""
    brace_start = text.find('{', mod_pos)
    if brace_start == -1:
        return ""
    depth = 0
    for i, ch in enumerate(text[brace_start:], brace_start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[cfg_pos:i + 1].strip()
    return ""


def generate_in_parallel(
    queries: "list[str]",
    *,
    model: "str | None" = None,
    think: "bool | None" = None,
) -> "list[str]":
    """Fire one ask() per query concurrently; return responses index-aligned to
    `queries`.

    Used by task0c split-and-merge: the N subset prompts share their large file-
    context prefix and differ only in the small "target these functions" tail, so
    vLLM's prefix cache prefills the shared part once and the marginal KV per
    extra request is just its tail — N requests batch on the GPU where N big
    distinct prompts would thrash (measured: shared-prefix x4 clean, distinct x4
    thrashed).

    ask() is already thread-safe (per-call OpenAI client in model_interact;
    content-addressed prompt store; append-only event log). A failed/empty call
    contributes "" rather than aborting the batch. len<=1 short-circuits to a
    single synchronous call (no threads).
    """
    if not queries:
        return []
    if len(queries) == 1:
        try:
            return [ask(queries[0], model=model, think=think)]
        except Exception:
            return [""]

    import threading as _t
    results: list[str] = [""] * len(queries)

    def _worker(i: int) -> None:
        try:
            results[i] = ask(queries[i], model=model, think=think) or ""
        except Exception as _e:  # one bad request must not sink the batch
            _guard("task0c/candidate_error", f"request {i}: {type(_e).__name__}: {_e}")
            results[i] = ""

    threads = [_t.Thread(target=_worker, args=(i,), name=f"task0c-gen-{i}") for i in range(len(queries))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: read/write relocated to pipeline_core.io (imported at top).


# slice 5b foundation: read/write relocated to pipeline_core.io (imported at top).


def _write_fr_preserving_notes(path, new_content: str) -> None:
    """Write FR content, preserving an existing `## Model Notes` section.
    Use at every TASK 1 write site so regenerating FR never wipes the
    model's accumulated self-corrections.
    """
    p = Path(path)
    old = p.read_text(encoding="utf-8") if p.exists() else None
    merged = _preserve_model_notes(old, new_content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(merged, encoding="utf-8")


def delete(path):
    p = Path(path)
    if p.exists():
        p.unlink()


def _parse_cmd(cmd: str) -> list[str]:
    """Split a shell command string into a list, falling back to str.split on parse error."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _derive_check_only_cmd(check_cmd: str) -> str:
    """Derive a compile-only (`cargo check`) command from a caller's check_cmd:
    swap the `test`/`build` subcommand → `check`, drop the test harness args
    (anything after a bare `--`) and `--no-run`/`--no-fail-fast`. Used as the
    fast inner-loop check for the per_fn fix strategy — `cargo check` skips
    codegen and test execution, and incremental compilation means a check
    after editing one fn recompiles only that module + its dependents.

    Examples:
      cargo test -p anyhow --no-run          → cargo check -p anyhow
      cargo +nightly test -p x -- --threads  → cargo +nightly check -p x
      cargo check -p x                       → cargo check -p x  (unchanged)
    """
    out_toks: list[str] = []
    for t in _parse_cmd(check_cmd):
        if t == "--":
            break
        if t in ("test", "build"):
            out_toks.append("check"); continue
        if t in ("--no-run", "--no-fail-fast"):
            continue
        out_toks.append(t)
    s = " ".join(out_toks).strip()
    return s if "check" in s.split() else "cargo check"


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# ---------------------------------------------------------------------------
# BL-010: Rust source condenser — signatures and type definitions only
# ---------------------------------------------------------------------------

def _condense_rust_to_signatures(source: str, filename: str = "code.rs") -> str:
    """
    Reduce a Rust source file to structural declarations (type definitions +
    function signatures). Function bodies are replaced with { /* ... */ }.
    Used by TASK 0b (fits more files in context) and TASK 0c attempt history
    (compresses failed test blocks to save space for the full compile error).

    Uses grep-ast (tree-sitter) when available; falls back to brace-counting.
    """
    try:
        from grep_ast import TreeContext
        tc = TreeContext(filename, source, color=False, line_number=False,
                         show_top_of_file_lines=0, margin=0)
        # Mark all lines as of interest so we get the full structural skeleton.
        tc.add_lines_of_interest(set(range(1, source.count('\n') + 2)))
        tc.add_context()
        result = tc.format()
        if result and len(str(result).strip()) > 20:
            return str(result)
    except Exception:
        pass

    # Fallback: brace-counting line scanner.
    lines = source.splitlines(keepends=True)
    result_lines: list[str] = []
    depth = 0
    skip_until_depth: int | None = None

    for line in lines:
        if skip_until_depth is not None:
            depth += line.count('{') - line.count('}')
            if depth <= skip_until_depth:
                skip_until_depth = None
            continue

        stripped = line.strip()
        is_fn = contains_fn_decl(stripped) and not stripped.startswith('//')
        is_type_kw = is_type_decl_line(line)

        net = line.count('{') - line.count('}')

        if is_fn and not is_type_kw and '{' in line and net > 0:
            last_brace = line.rindex('{')
            result_lines.append(line[:last_brace].rstrip() + ' { /* ... */ }\n')
            skip_until_depth = depth
            depth += net
        else:
            depth += net
            result_lines.append(line)

    return ''.join(result_lines)


# ---------------------------------------------------------------------------
# BL-021: SQLite pipeline knowledge store
# ---------------------------------------------------------------------------

PIPELINE_DB_NAME = "pipeline_knowledge.sqlite"


def _pipeline_db(project_dir: Path) -> sqlite3.Connection:
    """Open (and initialize if new) the per-project knowledge store."""
    db_path = project_dir / PIPELINE_DB_NAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _init_pipeline_db(conn)
    return conn


def _init_pipeline_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS type_signals (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        project  TEXT NOT NULL,
        file     TEXT NOT NULL,
        type_name TEXT NOT NULL,
        difficulty INTEGER NOT NULL DEFAULT 1,
        pattern  TEXT,
        ts       TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS compile_patterns (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        project        TEXT NOT NULL,
        module         TEXT NOT NULL,
        error_category TEXT NOT NULL,
        example        TEXT,
        fix_hint       TEXT,
        iteration      INTEGER,
        ts             TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS genesis_rules (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        project      TEXT NOT NULL,
        module_scope TEXT NOT NULL DEFAULT '*',
        category     TEXT NOT NULL,
        rule_text    TEXT NOT NULL,
        iteration    INTEGER,
        ts           TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS module_status (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        project    TEXT NOT NULL,
        module     TEXT NOT NULL,
        iteration  INTEGER NOT NULL,
        outcome    TEXT NOT NULL,
        pass_count INTEGER,
        fail_count INTEGER,
        ts         TEXT NOT NULL
    );
    """)
    conn.commit()


def _db_project_name(project_dir: Path) -> str:
    return project_dir.name


def type_signals_write(project_dir: Path, file: str, type_name: str,
                       difficulty: int = 1, pattern: str = "") -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO type_signals(project,file,type_name,difficulty,pattern,ts) "
            "VALUES(?,?,?,?,?,?)",
            (_db_project_name(project_dir), file, type_name, difficulty, pattern,
             datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def type_signals_read(project_dir: Path) -> str:
    """Return a formatted summary of types that were difficult to test, for TASK 1."""
    try:
        conn = _pipeline_db(project_dir)
        rows = conn.execute(
            "SELECT type_name, SUM(difficulty) as total, GROUP_CONCAT(pattern, ' | ') "
            "FROM type_signals WHERE project=? "
            "GROUP BY type_name ORDER BY total DESC LIMIT 20",
            (_db_project_name(project_dir),)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Types that were difficult to construct in tests (extract especially precise "
                 "requirements for these):"]
        for type_name, total, patterns in rows:
            line = f"  - {type_name} (difficulty score: {total})"
            if patterns:
                first = patterns.split(' | ')[0]
                if first:
                    line += f" — e.g. {first[:120]}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def compile_patterns_write(project_dir: Path, module: str, error_category: str,
                            example: str = "", fix_hint: str = "",
                            iteration: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO compile_patterns(project,module,error_category,example,fix_hint,iteration,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (_db_project_name(project_dir), module, error_category, example[:500],
             fix_hint[:500], iteration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def compile_patterns_read(project_dir: Path, module: str) -> str:
    """Return known compile pitfalls for this module, for TASK 2 prompt."""
    try:
        conn = _pipeline_db(project_dir)
        rows = conn.execute(
            "SELECT error_category, example, fix_hint FROM compile_patterns "
            "WHERE project=? AND (module=? OR module='*') "
            "ORDER BY id DESC LIMIT 15",
            (_db_project_name(project_dir), module)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Known compile pitfalls for this module (avoid repeating these errors):"]
        seen: set[str] = set()
        for cat, example, fix_hint in rows:
            if cat in seen:
                continue
            seen.add(cat)
            line = f"  - [{cat}]"
            if example:
                line += f" e.g. `{example[:100]}`"
            if fix_hint:
                line += f" → fix: {fix_hint[:100]}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def genesis_rules_write(project_dir: Path, module_scope: str, category: str,
                         rule_text: str, iteration: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO genesis_rules(project,module_scope,category,rule_text,iteration,ts) "
            "VALUES(?,?,?,?,?,?)",
            (_db_project_name(project_dir), module_scope, category, rule_text[:1000],
             iteration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def genesis_rules_read(project_dir: Path, module_scope: str = "*",
                        categories: list | None = None) -> str:
    """Return structured rules relevant to a module and failure categories, for TASK 1/2."""
    try:
        conn = _pipeline_db(project_dir)
        placeholders = "?,?" if module_scope != "*" else "?"
        args: list = [_db_project_name(project_dir)]
        if module_scope != "*":
            args += [module_scope, "*"]
        else:
            args += ["*"]
        if categories:
            cat_placeholders = ",".join("?" * len(categories))
            query = (
                f"SELECT category, rule_text FROM genesis_rules "
                f"WHERE project=? AND module_scope IN ({placeholders}) "
                f"AND category IN ({cat_placeholders}) "
                f"ORDER BY id DESC LIMIT 20"
            )
            args += categories
        else:
            query = (
                f"SELECT category, rule_text FROM genesis_rules "
                f"WHERE project=? AND module_scope IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 20"
            )
        rows = conn.execute(query, args).fetchall()
        conn.close()
        if not rows:
            return ""
        seen: set[str] = set()
        lines = ["Structured rules from previous failures (apply these):"]
        for cat, rule in rows:
            key = cat + rule[:50]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  [{cat}] {rule}")
        return "\n".join(lines)
    except Exception:
        return ""


def genesis_rules_count(project_dir: Path, module_scope: str = "*") -> int:
    """Total structured genesis rules recorded for a module scope (richness signal)."""
    try:
        conn = _pipeline_db(project_dir)
        args: list = [_db_project_name(project_dir)]
        ph = "?" if module_scope == "*" else "?,?"
        args += ["*"] if module_scope == "*" else [module_scope, "*"]
        n = conn.execute(f"SELECT COUNT(*) FROM genesis_rules WHERE project=? "
                         f"AND module_scope IN ({ph})", args).fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def module_status_write(project_dir: Path, module: str, iteration: int,
                         outcome: str, pass_count: int = 0,
                         fail_count: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO module_status(project,module,iteration,outcome,pass_count,fail_count,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (_db_project_name(project_dir), module, iteration, outcome, pass_count,
             fail_count, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# BL-023: Test assertion extractor for TASK 2
# ---------------------------------------------------------------------------

def _extract_test_assertions(test_files: list, module_dir_rel: str = "",
                              max_assertions: int = 40) -> str:
    """
    Extract assert_* lines with minimal surrounding context from test files.
    Returns a compact 'assertion manifest' that TASK 2 can use as ground truth.
    Only includes files relevant to the current module (if module_dir_rel given).
    """
    assertion_re = re.compile(
        r'\b(assert(?:_eq|_ne|_matches|_approx_eq)?!|assert!\s*\(|\.unwrap\(\)|\.expect\()',
        re.MULTILINE,
    )
    results: list[str] = []

    for rel, content in test_files:
        if module_dir_rel and not (rel.startswith(module_dir_rel) or
                                   f"/{module_dir_rel}" in rel):
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if assertion_re.search(stripped) and not stripped.startswith('//'):
                # Include one line of context before (usually the `let` binding)
                context_line = lines[i - 1].strip() if i > 0 else ""
                entry = (f"  {context_line}  // context\n  {stripped}"
                         if context_line and context_line.startswith("let")
                         else f"  {stripped}")
                results.append(entry)
                if len(results) >= max_assertions:
                    break
        if len(results) >= max_assertions:
            break

    if not results:
        return ""
    return ("Assertions the generated code must satisfy (from test files):\n"
            + "\n".join(results))


def _categorize_compile_error(error_output: str) -> str:
    """Classify a compile error into a short category tag for compile_patterns."""
    o = error_output.lower()
    if "cannot find type" in o or "unresolved import" in o or "use of undeclared" in o:
        return "missing_type"
    if "module not found" in o or "no module named" in o or "mod " in o and "not found" in o:
        return "module_declaration"
    if "trait bound" in o or "does not implement" in o or "the trait" in o:
        return "trait_bound"
    if "mismatched types" in o or "expected" in o and "found" in o:
        return "type_mismatch"
    if "cannot borrow" in o or "borrow" in o and "conflict" in o:
        return "borrow_checker"
    if "lifetime" in o:
        return "lifetime"
    if "unused import" in o or "dead_code" in o:
        return "lint_error"
    if "proc-macro" in o or "proc_macro" in o:
        return "proc_macro"
    return "compile_error"


# Cargo.toml accessors live in domain/cargo_toml.py (pure, tomllib — R8). Re-exported
# under the legacy `_`-names so the ~15 call sites stay unchanged.
_load_cargo_toml = _domain_load_cargo_toml
_cargo_package_name = _domain_cargo_package_name
_cargo_is_proc_macro = _domain_cargo_is_proc_macro
_cargo_default_features = _domain_cargo_default_features

# Workspace/dependency scanners live in adapters/workspace_scan.py (P4). Wire the
# pipeline-bound collaborators into that module here at the composition root.
# #14: explicit constructor injection (was workspace_scan.configure() globals).
_WS_SCAN_DEPS = _workspace_scan.WorkspaceScanDeps(
    ask=ask, task0_model=_TASK0_MODEL,
    skip_dir_names=frozenset(SKIP_DIR_NAMES), parse_cmd=_parse_cmd)


def scan_downstream_api(project_dir: Path, module_name: str) -> str:
    """
    Scan all .rs source files in the workspace for 'use <module_name>::...' imports.
    Returns a formatted hint telling the model what the module must publicly export.
    Used to give argh_shared (and similar leaf crates) context about their API contract
    even though they have no upstream locked deps to learn from.
    """
    direct_imports: set[str] = set()
    submodule_imports: dict[str, set[str]] = {}

    for p in sorted(project_dir.rglob("*.rs")):
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        try:
            text = p.read_text(errors='replace')
        except Exception:
            continue

        brace_groups, submod_brace, submod_single = module_use_imports(text, module_name)

        # use module::{A, B, C}
        for inner in brace_groups:
            for item in inner.split(','):
                item = item.strip()
                if item:
                    direct_imports.add(item)

        # use module::submod::{A, B}
        for submod, inner in submod_brace:
            for item in inner.split(','):
                item = item.strip()
                if item:
                    submodule_imports.setdefault(submod, set()).add(item)

        # use module::submod::Item  (single item, no braces)
        for submod, item in submod_single:
            if submod[0].islower() and item[0].isupper():
                submodule_imports.setdefault(submod, set()).add(item)

    # Also scan functional_requirements.md for type names used in code blocks that look like
    # internal types (not imported from outside). These must be defined in the module itself.
    internal_types: set[str] = set()
    func_req_path = project_dir / "functional_requirements.md"
    if func_req_path.exists():
        try:
            func_text = func_req_path.read_text(errors='replace')
            # Find type names in code blocks that are not imported from elsewhere
            # Look for PascalCase identifiers in Rust signatures inside ```rust blocks
            for code in rust_code_fences(func_text):
                # Find PascalCase identifiers
                for name in camelcase_type_names(code):
                    # Skip if it's a stdlib/known type or already in our direct/submodule imports
                    if name in ('Result', 'Option', 'String', 'Vec', 'Ok', 'Err', 'Some', 'None',
                                'Box', 'Arc', 'Rc', 'Fn', 'FnMut', 'FnOnce', 'Send', 'Sync',
                                'Self', 'Args', 'bool', 'usize', 'str'):
                        continue
                    if name in direct_imports:
                        continue
                    if any(name in v for v in submodule_imports.values()):
                        continue
                    internal_types.add(name)
        except Exception:
            pass

    if not direct_imports and not submodule_imports and not internal_types:
        return ""

    lines = [
        f"\n⚠️  REQUIRED PUBLIC API for `{module_name}` (inferred from workspace source files):",
        f"Downstream crates import from `{module_name}` as follows — you MUST match these paths exactly:",
    ]

    if submodule_imports:
        lines.append("")
        lines.append("Sub-module paths (must be separate .rs files declared with `pub mod` in lib.rs):")
        for submod in sorted(submodule_imports.keys()):
            exports = ', '.join(sorted(submodule_imports[submod]))
            lines.append(f"  {module_name}/src/{submod}.rs  →  pub mod {submod};  // exports: {exports}")
        lines.append("")
        lines.append("lib.rs MUST contain these exact pub mod declarations:")
        for submod in sorted(submodule_imports.keys()):
            lines.append(f"  pub mod {submod};")

    if direct_imports:
        lines.append("")
        lines.append(f"Types imported directly from the crate root (`use {module_name}::TypeName`):")
        for item in sorted(direct_imports):
            lines.append(f"  pub use ...::{item};  // or define directly in lib.rs")

    if internal_types:
        lines.append("")
        lines.append("Internal types referenced in functional requirements — MUST be defined in lib.rs at the crate root:")
        lines.append("⚠️  DO NOT put these in submodules. Define them directly in lib.rs so all submodules can use `crate::TypeName`.")
        for item in sorted(internal_types):
            lines.append(f"  pub struct {item}; // define in lib.rs, NOT in submodules")

    return '\n'.join(lines) + '\n'


def scan_workspace_structure(project_dir: Path) -> str:
    """
    Produce a markdown workspace map for TASK 2.
    Implementation is selected by WORKSPACE_SCANNER in .env (default: llm).
    """
    return _workspace_scanner_factory(_WS_SCAN_DEPS).scan(project_dir)


# ---------------------------------------------------------------------------
# Module lock management
# ---------------------------------------------------------------------------

def load_locked_modules(project_dir: Path) -> set[str]:
    """Return the set of module names that have been locked (tests pass, files frozen)."""
    p = project_dir / LOCKED_MODULES_DIR / "locked.txt"
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def lock_module(project_dir: Path, module_name: str, module_dir: Path,
                manifest_path: Path) -> int:
    """
    Lock a module: copy its generated files to .locked_modules/<name>/ as a
    permanent backup, then record the module name in locked.txt.

    The live files remain on disk so the compiler can use them as dependencies
    when building subsequent modules. The backup is used to:
      - Restore locked files if the project directory is cleaned between runs.
      - Provide locked-source context when generating dependent modules.

    Returns the number of files locked.
    """
    lock_dir = project_dir / LOCKED_MODULES_DIR / module_name
    lock_dir.mkdir(parents=True, exist_ok=True)

    module_rel = module_dir.relative_to(project_dir)
    files = load_manifest(manifest_path)
    locked_count = 0

    for rel_path in files:
        full = project_dir / rel_path
        if not full.exists():
            continue
        try:
            Path(rel_path).relative_to(module_rel)  # only files under this module's dir
        except ValueError:
            continue
        dest = lock_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(full.read_text())
        locked_count += 1

    # Update locked.txt
    locked = load_locked_modules(project_dir)
    locked.add(module_name)
    lock_txt = project_dir / LOCKED_MODULES_DIR / "locked.txt"
    lock_txt.parent.mkdir(parents=True, exist_ok=True)
    lock_txt.write_text("\n".join(sorted(locked)) + "\n")

    _log(f"  LOCKED '{module_name}' ({locked_count} file(s) → {LOCKED_MODULES_DIR}/{module_name}/)")
    return locked_count


# load_locked_source_context → pipeline_core.adapters.artifacts.WorkspaceArtifacts.

# ---------------------------------------------------------------------------
# Workspace stub helpers
# ---------------------------------------------------------------------------

def _write_workspace_stub(crate_type: str, module_dir: Path) -> Path:
    """
    Write a minimal entry-point stub so Cargo can parse the workspace.
    Bin crates get `fn main() {}`, all others get an empty lib stub.
    Always writes (creates or overwrites) so state is deterministic.
    Returns the path written.
    """
    if "bin" in crate_type.lower() and "lib" not in crate_type.lower():
        entry = module_dir / "src" / "main.rs"
        content = "fn main() {}\n"
    else:
        entry = module_dir / "src" / "lib.rs"
        content = "// pipeline workspace stub\n"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(content)
    return entry


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
# Step output validators (anti-corruption layer)
# ---------------------------------------------------------------------------

# Words that appear in any English text and carry no signal about whether the
# requirements document contains module-specific identifiers.
_STOP_WORDS = {
    "the", "and", "for", "are", "not", "use", "let", "mut", "pub", "fn",
    "mod", "impl", "struct", "enum", "trait", "type", "this", "that",
    "with", "from", "into", "self", "true", "false", "none", "some",
    "return", "match", "where", "else", "then", "each", "when", "which",
    "must", "should", "will", "can", "all", "any", "one", "new", "get",
    "set", "add", "run", "has", "its", "if", "in", "of", "to", "at",
    "as", "is", "it", "on", "do", "no", "be", "by", "or", "an", "a",
    "always", "never", "only", "also", "both", "either", "every",
    "given", "used", "list", "item", "value", "values", "name", "names",
    "error", "result", "output", "input", "string", "number", "data",
    "file", "path", "field", "method", "function", "module",
    "crate", "test", "tests", "assert", "check", "build", "code",
}

_META_PHRASES = [
    "this document describes",
    "functional requirements are",
    "the pipeline",
    "this requirement",
    "the following requirements",
    "requirements document",
    "requirement specification",
    "the purpose of this",
]


def _extract_identifiers(text: str) -> set[str]:
    """Extract CamelCase and snake_case identifiers from text, filtered by stop-words."""
    raw = mixed_case_identifiers(text)
    return {w for w in raw if w.lower() not in _STOP_WORDS}


def validate_task1_output(req_path: Path, test_files: list) -> tuple[bool, str]:
    """
    Anti-corruption check before TASK 2.
    Returns (ok, reason). All checks are deterministic — no LLM calls.
    """
    content = req_path.read_text(encoding="utf-8") if req_path.exists() else ""

    if len(content) < 500:
        return False, f"too short ({len(content)} chars, need ≥ 500)"

    content_lower = content.lower()
    for phrase in _META_PHRASES:
        if phrase in content_lower:
            return False, f"meta-commentary detected ('{phrase}')"

    req_idents = _extract_identifiers(content)
    test_idents: set[str] = set()
    for _, tc in test_files:
        test_idents.update(_extract_identifiers(tc))

    overlap = req_idents & test_idents
    if len(overlap) < 3:
        sample = sorted(overlap)[:5]
        return False, (
            f"requirements too generic — only {len(overlap)} identifiers overlap "
            f"with test files (need ≥ 3). Overlap: {sample}"
        )

    return True, "ok"


# Regex patterns for TASK 2 placeholder detection.
_PLACEHOLDER_RE = re.compile(
    r'unimplemented!\s*\(\)|todo!\s*\(\)|#\s*TODO|#\s*FIXME|pass\s*$|raise\s+NotImplementedError',
    re.MULTILINE,
)


def validate_task2_output(project_dir: Path, expected_paths: list[str],
                          req_path: Path | None = None) -> tuple[bool, str]:
    """
    Anti-corruption check before TASK 3.
    Checks: all files present, none empty, none placeholder-only, named types exist in code.
    """
    if not expected_paths:
        return True, "ok (no expected paths recorded)"

    missing = [p for p in expected_paths if not (project_dir / p).exists()]
    if missing:
        return False, f"missing files: {', '.join(missing[:5])}"

    tiny = [
        p for p in expected_paths
        if (project_dir / p).exists() and (project_dir / p).stat().st_size < 50
    ]
    if tiny:
        return False, f"empty/stub files (< 50 bytes): {', '.join(tiny[:5])}"

    # Placeholder check: reject files where all non-comment, non-whitespace content
    # is unimplemented!()/todo!()/pass/etc. These compile but contain no real logic.
    placeholder_files = []
    all_source = ""
    for p in expected_paths:
        fp = project_dir / p
        if not fp.exists():
            continue
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        all_source += raw
        stripped = strip_comment_spans(raw)
        stripped = re.sub(r'\s+', '', stripped)
        placeholders_found = len(_PLACEHOLDER_RE.findall(raw))
        non_placeholder_len = len(re.sub(r'\s+', '', _PLACEHOLDER_RE.sub("", stripped)))
        if placeholders_found > 0 and non_placeholder_len < 50:
            placeholder_files.append(p)
    if placeholder_files:
        return False, f"placeholder-only files (all unimplemented!/todo!): {', '.join(placeholder_files[:3])}"

    # Type-presence check: CamelCase types named in requirements must appear as
    # struct/enum definitions in the generated code.
    if req_path and req_path.exists():
        req_content = req_path.read_text(encoding="utf-8", errors="ignore")
        named_types = set(re.findall(r'\b([A-Z][a-zA-Z0-9]{2,})\b', req_content))
        named_types -= {"README", "TODO", "FIXME", "API", "URL", "CLI", "LLM", "AI"}
        if named_types:
            # syn DEFINITIONS in the generated files (scoped to expected_paths to
            # match the old all_source search); regex fallback when no binary.
            _type_recs = _enumerate_types_via_extractor(project_dir)
            if _type_recs is not None:
                _defined = {r.name for r in _type_recs
                            if r.kind in ("struct", "enum", "trait") and r.file in set(expected_paths)}
                missing_types = [t for t in sorted(named_types) if t not in _defined]
            else:
                missing_types = [t for t in sorted(named_types)
                                 if not defines_type(all_source, t)]
            # Only fail if more than half the named types are absent — allows for
            # types that are re-exported or defined via macros.
            if len(missing_types) > max(1, len(named_types) // 2):
                return False, (
                    f"types from requirements not defined in generated code "
                    f"({len(missing_types)}/{len(named_types)} missing): "
                    f"{', '.join(missing_types[:5])}"
                )

    return True, "ok"


_PUB_ITEM_RE = re.compile(
    r'\bpub(?:\([^)]*\))?\s+'
    r'(?:unsafe\s+)?(?:async\s+)?(?:const\s+)?(?:default\s+)?'
    r'(fn|struct|enum|trait|type|mod)\s+(\w+)',
)


def _extract_pub_items(text: str) -> set[tuple[str, str]]:
    """Return {(kind, name)} for every pub item in the API-surface LISTING artifact.

    NOTE: this parses the `_extract_public_symbols` *listing* (`pub struct X { ... }`,
    `pub fn sig`, …), which is a summary artifact, NOT valid Rust source — so syn cannot
    parse it and the regex is legitimate here (Hard Rule applies to Rust *source*). The
    generated-source side uses `_pub_items_from_tree` (syn). See R7.2 in CODE_ARCHITECTURE.md.
    """
    return {(m.group(1), m.group(2)) for m in _PUB_ITEM_RE.finditer(text)}


def _pub_items_from_tree(src_dir: Path) -> set[tuple[str, str]]:
    """Return {(kind, name)} for pub struct/enum/trait/fn in a Rust source tree, via the syn
    extractor (no regex on source). Excludes examples/benches/tests + scratch dirs by record
    path. Only these kinds matter for the conformance check — the API-surface listing never
    contains `type`/`mod`/`const` items."""
    def _excluded(rel: str) -> bool:
        parts = set(Path(rel).parts)
        return bool(parts & {"examples", "benches", "tests"}) or bool(parts & SKIP_DIR_NAMES)

    out: set[tuple[str, str]] = set()
    for t in _enumerate_types_via_extractor(src_dir) or []:
        if t.is_pub and t.kind in {"struct", "enum", "trait"} and not _excluded(t.file):
            out.add((t.kind, t.name))
    for f in _enumerate_fns_via_extractor(src_dir) or []:   # dicts (engine contract)
        if f.get("is_pub") and not _excluded(f.get("file", "")):
            out.add(("fn", f.get("fn_name", "")))
    return out


def validate_task2_api_surface_conformance(
    project_dir: Path, module_name: str, module_dir: Path,
) -> tuple[bool, str]:
    """Compare pub items in the regenerated source to those captured pre-strip
    in the api_surface. Returns (ok, drift_report).

    Soft check: missing pub items are reported but do NOT block the iteration.
    The drift report is logged and (when present) appended to test_output before
    TASK 4 so the methodology refinement sees both the cargo failure and the
    contract violation in one prompt. Use this signal to spot "LLM ignored the
    given api_surface" failures distinctly from "tests genuinely don't pass".
    """
    api_text = load_api_surface(project_dir, module_name)
    if not api_text:
        return True, "no api_surface captured — skipping conformance check"

    api_items = _extract_pub_items(api_text)
    if not api_items:
        return True, "api_surface contained no pub items — skipping"

    # Generated source — parsed with syn (the extractor walks the tree itself), NOT regex.
    src_root = module_dir / "src"
    walk_root = src_root if src_root.is_dir() else module_dir
    gen_items = _pub_items_from_tree(walk_root)

    missing = api_items - gen_items
    if not missing:
        return True, f"ok — all {len(api_items)} pub item(s) present in regenerated source"

    by_kind: dict[str, list[str]] = {}
    for kind, name in sorted(missing):
        by_kind.setdefault(kind, []).append(name)
    parts = [f"{kind}: {', '.join(names)}" for kind, names in sorted(by_kind.items())]
    report = (
        f"API drift — {len(missing)} of {len(api_items)} pub item(s) missing from "
        f"regenerated source: " + "; ".join(parts)
    )
    return False, report


_TEST_ATTR_RE = re.compile(
    r"#\s*\[\s*(?:test|tokio::test|async_std::test|"
    r"serial_test::serial|rstest|parameterized|test_case)\s*[\s\],(]",
)


def _count_test_attrs_in_text(text: str) -> int:
    """Count `#[test]`-family attribute occurrences in a Rust source string.
    Naive but adequate: counts every macro-attribute that recognises as a
    test marker. Used by the per-cluster expected_count computation.

    Regex is deliberate here, NOT a no-regex-on-source violation: this counts markers in
    generated test code that may not yet be valid Rust (mid-fill), where syn would fail to
    parse and under-count. A robust marker count is the right tool. See CODE_ARCHITECTURE.md
    §8 R7.3."""
    return len(_TEST_ATTR_RE.findall(text or ""))


def _count_test_fns_cfg_aware(
    text: str,
    cfg_true: set[str],
    cfg_false: set[str],
    default_features: "frozenset[str] | set[str]" = frozenset(),
) -> int:
    """Count `#[test]`-attributed fn declarations in `text` whose surrounding
    `#[cfg(...)]` attributes evaluate true (or undecidable) under the given
    cluster cfg sets. A `#[test]` fn with a `#[cfg(X)]` where X is in
    cfg_false is NOT counted — cargo won't compile it under this cluster
    and we shouldn't expect it to report a verdict.

    Pragmatic line-level walker: for each line containing `#[test]`,
    look at the contiguous block of `#[...]` attribute lines around the
    fn declaration. Collect every `#[cfg(...)]` predicate. If any of
    them evaluates false under (cfg_true, cfg_false), drop the fn.
    Otherwise count it (true or undecidable predicates → still counted).
    """
    # A file-level inner attribute `#![cfg(P)]` gates the WHOLE file (idiomatic
    # for integration-test files). When P is false for this cluster cargo
    # compiles the file to an EMPTY test binary, so NONE of its `#[test]` fns
    # run — they must not inflate the expected count, else the cluster is
    # permanently 'incomplete' and the post-task3 fix loop churns on a phantom
    # test (hardcrate `tests/cap.rs` under `stable`). Same decidable-false
    # semantics as the runner-side file filter, so the count matches cargo.
    if _file_top_cfg_excluded(text, cfg_true, cfg_false):
        return 0
    lines = text.splitlines()
    test_attr_re = re.compile(
        r"^\s*#\s*\[\s*(?:test|tokio::test|async_std::test|"
        r"serial_test::serial|rstest|parameterized|test_case)\s*[\s\],(]",
    )
    cfg_inline_re = re.compile(
        r"#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]",
    )
    attr_line_re = re.compile(r"^\s*#\s*\[")
    count = 0
    for i, line in enumerate(lines):
        if not test_attr_re.search(line):
            continue
        # Walk backward across the attribute block above this #[test] to
        # collect cfg predicates that gate this fn.
        gates: list[str] = []
        for cm in cfg_inline_re.finditer(line):
            gates.append(cm.group(1).strip())
        j = i - 1
        while j >= 0:
            s = lines[j].strip()
            if not s:
                break  # blank line ends the attribute block
            if s.startswith("//") or s.startswith("/*"):
                j -= 1
                continue
            if not attr_line_re.match(lines[j]):
                break  # not an attribute line — chain ends
            for cm in cfg_inline_re.finditer(lines[j]):
                gates.append(cm.group(1).strip())
            j -= 1
        # If any gate keeps cargo from running this test, skip it. cargo-faithful:
        # a custom `--cfg`/non-default feature off here = the test never runs.
        if any(_cfg_false_for_cargo(g, cfg_true, cfg_false, default_features) for g in gates):
            continue
        count += 1
    return count


def _file_active_for_cluster(
    src_dir: Path,
    file_path: Path,
    cfg_true: set[str],
    cfg_false: set[str],
    _depth: int = 0,
    default_features: "frozenset[str] | set[str]" = frozenset(),
) -> bool:
    """Whether `file_path` is COMPILED under the cluster cfg: evaluate every
    cfg predicate along its mod-declaration chain up to the crate root
    (lib.rs/main.rs), via the syn extractor's ModRecords. A file whose chain
    is decidably False never compiles — counting its tests inflated the
    expected count and made the cluster permanently 'incomplete' (microcrate:
    cap.rs counted under stable although `mod cap;` is extra_cap-gated).

    Undecidable predicates and unmatched files count as ACTIVE — this helper
    may under-exclude but never over-exclude (the strict verdict must not
    silently shrink the oracle, FR-018)."""
    stem = file_path.stem
    if stem in ("lib", "main") or _depth > 32:
        return True
    recs = _mod_records_cached(src_dir)
    if recs is None:
        # Extractor unavailable: single-level gate (AST-less environments).
        pred = _file_cfg_predicate(src_dir, file_path.name)
        return pred is None or not _cfg_false_for_cargo(pred, cfg_true, cfg_false, default_features)
    matches = [r for r in recs if r.name == stem or r.file == file_path.name]
    if not matches:
        return True
    # Dual-form decls (`#[cfg(X)] mod m;` / `#[cfg(not(X))] mod m;`): the file
    # is active if ANY declaration chain is active.
    for rec in matches:
        if any(_cfg_false_for_cargo(p, cfg_true, cfg_false, default_features)
               for p in rec.cfg_predicates):
            continue
        if _file_active_for_cluster(src_dir, Path(rec.declared_in),
                                    cfg_true, cfg_false, _depth + 1, default_features):
            return True
    return False


def _count_active_tests_for_cluster(
    project_dir: Path,
    cluster: "BuildCluster | None",
) -> int:
    """FR-024 strict-verdict helper: how many `#[test]` fns SHOULD have run
    if cargo test compiles every test source active under `cluster`'s cfg.

    Counts three sources, all cfg-filtered against the cluster's
    (cfg_true, cfg_false) sets so cargo-cannot-compile tests don't inflate
    the expected count:

      1. `tests/**/*.rs` — integration tests; per-test cfg attrs filter.
      2. `src/*.gen_tests_*.rs` — gen_tests siblings; included only if
         the parent file's `#[path] mod gen_tests_N;` decl is active
         under this cluster's cfg (the decl carries the gate).
      3. `src/**/*.rs` (excluding gen_tests siblings) — per-test cfg
         attrs filter, same as tests/.

    Returns the cfg-aware total. Used to gate SUCCESS: cargo must report
    at least this many tests across all binaries before TASK 3 accepts.
    """
    if cluster is None:
        cfg_true: set[str] = set()
        cfg_false: set[str] = set()
    else:
        cfg_true = set(getattr(cluster, "cfg_true", []) or [])
        cfg_false = set(getattr(cluster, "cfg_false", []) or [])

    # Crate default features — a `feature = "X"` that IS a default is ON even when
    # the cluster sets don't list it, so it must NOT be force-excluded (FR-018).
    cargo_toml = project_dir / "Cargo.toml"
    default_features: frozenset[str] = frozenset()
    if cargo_toml.exists():
        try:
            default_features = frozenset(
                _cargo_default_features(cargo_toml.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            default_features = frozenset()

    total = 0
    # 1. tests/**/*.rs — cfg-filtered per-test.
    tests_dir = project_dir / "tests"
    if tests_dir.is_dir():
        for tp in tests_dir.rglob("*.rs"):
            try:
                text = tp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            total += _count_test_fns_cfg_aware(text, cfg_true, cfg_false, default_features)

    src_dir = project_dir / "src"
    if not src_dir.is_dir():
        return total

    # 2. gen_tests siblings — gated by their parent #[path] decl (R7: decl scan).
    for parent in src_dir.glob("*.rs"):
        if _is_gen_tests_file(parent.name):
            continue
        # A parent file that never compiles under this cluster (mod-decl chain
        # cfg'd out) takes its gen_tests siblings with it.
        if not _file_active_for_cluster(src_dir, parent, cfg_true, cfg_false, 0, default_features):
            continue
        try:
            ptext = parent.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for decl in _gen_tests_decl_blocks(ptext):
            gate = decl.gate.strip()
            sib_name = decl.sibling_name or ""
            if gate:
                v = _eval_cfg_pred(gate, cfg_true, cfg_false)
                if v is False:
                    continue
            sib = src_dir / sib_name
            if not sib.exists():
                continue
            try:
                stext = sib.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # gen_tests bodies don't have additional cfg gates on
            # individual #[test] fns (they're written by task0c, which
            # doesn't emit per-test cfgs), so cfg-aware vs naive count
            # produces the same result here.
            total += _count_test_fns_cfg_aware(stext, cfg_true, cfg_false, default_features)

    # 3. src/**/*.rs (excluding gen_tests siblings) — inline test mods.
    for srf in src_dir.rglob("*.rs"):
        if _is_gen_tests_file(srf.name):
            continue
        if not _file_active_for_cluster(src_dir, srf, cfg_true, cfg_false, 0, default_features):
            continue
        try:
            stext = srf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        total += _count_test_fns_cfg_aware(stext, cfg_true, cfg_false, default_features)
    return total


# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


def validate_task4_output(genesis_before: str, genesis_after: str,
                          test_output: str,
                          failure_type: str = "test") -> tuple[bool, str]:
    """
    Anti-corruption check on genesis.md update.
    Returns (ok, reason). A stall or duplicate is flagged but not fatal.
    """
    if genesis_after.strip() == genesis_before.strip():
        return False, "genesis_stall — TASK 4 produced no update to genesis.md"

    if len(genesis_after) <= len(genesis_before):
        return False, "genesis shrank — TASK 4 may have removed content"

    new_text = genesis_after[len(genesis_before):]

    # R9.5: the identifier-overlap check is a TEST-failure heuristic. For BUILD
    # failures test_output is compiler errors (project-specific idents) but the
    # build prompt MANDATES general, identifier-free structural rules — so a correct
    # update has empty intersection. Applying it there contradicts the prompt and
    # falsely stalls legitimate updates. Skip it for build; keep stall/shrank/dedup.
    if failure_type != "build":
        # Verify new content references at least one identifier from the test output.
        test_idents = _extract_identifiers(test_output)
        new_idents = _extract_identifiers(new_text)
        if test_idents and not (test_idents & new_idents):
            return False, "genesis update contains no identifiers from the failing test output"

    # Deduplication: flag if any substantial new paragraph already exists verbatim.
    new_paragraphs = [p.strip() for p in new_text.split("\n\n") if len(p.strip()) > 100]
    for para in new_paragraphs:
        if para in genesis_before:
            return False, "genesis update duplicates existing content (paragraph already present)"

    return True, "ok"


# ---------------------------------------------------------------------------
# Resume detection
# ---------------------------------------------------------------------------

def detect_resume_point(root: Path, project_dir: Path, manifest_path: Path,
                        genesis_path: Path, functional_req_path: Path) -> tuple:
    """
    Inspect existing files to determine exactly where the previous run stopped.
    Returns (iteration, start_task) where start_task is 1-6.

    State machine:
      The four observable signals are:
        - max_n:            highest N in tests_output_N.txt (0 if none)
        - has_functional:   functional_requirements.md exists
        - has_manifest:     .generated_files.txt exists

      Possible states and their meaning:
        max_n=0, no functional, no manifest  -> clean start, TASK 1 of iter 1
        max_n=0, functional,    no manifest  -> TASK 1 done, resume TASK 2 of iter 1
        max_n=0, functional,    manifest     -> TASK 2 done, resume TASK 3 of iter 1
        max_n=0, no functional, manifest     -> corrupt state, clean up, TASK 1 of iter 1

        max_n>0, no functional, no manifest  -> iteration max_n complete, TASK 1 of max_n+1
        max_n>0, functional,    no manifest  -> TASK 1 of max_n+1 done, resume TASK 2
        max_n>0, functional,    manifest     -> TASK 2 of max_n+1 done OR crash after
                                               TASK 3 of max_n; safest: resume TASK 3
        max_n>0, no functional, manifest     -> TASK 5 done, TASK 6 not done; finish
                                               cleanup then TASK 1 of max_n+1
    """
    existing = list(project_dir.glob("tests_output_*.txt"))
    max_n = 0
    if existing:
        max_n = max(
            int(re.search(r"tests_output_(\d+)\.txt", p.name).group(1))
            for p in existing
        )

    has_functional = functional_req_path.exists()
    has_manifest = manifest_path.exists()

    if max_n == 0:
        if not has_functional and not has_manifest:
            _log("Clean start: beginning from iteration 1, TASK 1.")
            return 1, 1
        if has_functional and not has_manifest:
            _log("Resume: TASK 1 completed. Resuming iteration 1 from TASK 2.")
            return 1, 2
        if has_functional and has_manifest:
            _log("Resume: TASK 2 completed. Resuming iteration 1 from TASK 3.")
            return 1, 3
        # manifest but no functional_req: corrupt, clean up
        _log("Unexpected state (manifest without functional_requirements.md). Cleaning up.")
        delete_source_files(project_dir, manifest_path)
        return 1, 1

    # max_n > 0
    next_iter = max_n + 1

    if not has_functional and not has_manifest:
        _log(f"Resume: iteration {max_n} complete. Starting iteration {next_iter} from TASK 1.")
        return next_iter, 1

    if has_functional and not has_manifest:
        _log(f"Resume: TASK 1 of iteration {next_iter} completed. Resuming from TASK 2.")
        return next_iter, 2

    if has_functional and has_manifest:
        # Ambiguous: could be after TASK 2 of next_iter OR after TASK 3 of max_n.
        # Safest resolution: treat as after TASK 2 of next_iter and run TASK 3.
        # Worst case: we re-run tests once with the same code; harmless.
        _log(f"Resume: source files present. Resuming iteration {next_iter} from TASK 3.")
        return next_iter, 3

    # no functional_req, manifest exists: TASK 5 done, TASK 6 not done
    _log(f"Resume: TASK 5 of iteration {max_n} done. Completing TASK 6 then starting {next_iter}.")
    delete_source_files(project_dir, manifest_path)
    return next_iter, 1


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
        project_dir, is_gen_tests_decl_only=_is_gen_tests_decl_only_src)


collect_examples = _discovery.collect_examples
collect_protected_test_files = _discovery.collect_protected_test_files


format_test_files = _shaping.format_test_files
_split_at_semantic_boundaries = _shaping.split_at_semantic_boundaries


# chunk_test_files + chunk_test_files_by_file relocated to prompts.shaping (P6).
chunk_test_files = _shaping.chunk_test_files
chunk_test_files_by_file = _shaping.chunk_test_files_by_file


# ---------------------------------------------------------------------------
# Startup: discover and validate the test command from README.md
# ---------------------------------------------------------------------------

def discover_test_command(readme_text: str, project_dir: Path | None = None) -> str:
    """Discover the project's primary test command. Thin shim over the relocated
    use-case (`pipeline_core.application.tasks.discover_test_command`); deps bound below."""
    return _discover_test_command_impl(readme_text, project_dir, _DISCOVER_TEST_CMD_DEPS)


# ---------------------------------------------------------------------------
# Source file manifest
# ---------------------------------------------------------------------------

def save_manifest(manifest_path: Path, files: list):
    write(manifest_path, "\n".join(files))


# load_manifest → pipeline_core.adapters.artifacts (imported above).


def write_source_files(files_dict: dict, project_dir: Path, manifest_path: Path,
                       existing_test_files: set) -> int:
    """
    Write source files to disk and record them in the manifest.
    Skips any path that matches an existing test file or a protected project file
    (README.md, LICENSE, etc.) that must never be overwritten.
    Returns the number of files actually written.
    """
    # Files that must never be overwritten regardless of what the model generates.
    PROTECTED = {"README.md", "readme.md", "README.rst", "LICENSE", "LICENSE.md",
                 "LICENSE.txt", "Cargo.lock"}
    written = []
    skipped = []
    RUST_SOURCE_DIRS = {'src', 'tests', 'benches', 'examples', 'build'}
    for rel_path, content in files_dict.items():
        # Strip leading slashes — model sometimes returns absolute-looking paths like
        # /argh_derive/src/lib.rs instead of argh_derive/src/lib.rs. Python's Path /
        # operator discards the left operand when the right side is absolute, which
        # would escape the project directory entirely.
        rel_path = rel_path.lstrip("/")
        # Normalise Rust source paths: insert src/ if the file sits directly under a
        # crate directory with no intermediate source-subdir component.
        # e.g. "argh_shared/traits.rs" → "argh_shared/src/traits.rs"
        if Path(rel_path).suffix == '.rs':
            parts = Path(rel_path).parts
            if len(parts) == 2 and parts[0] not in RUST_SOURCE_DIRS:
                fixed = str(Path(parts[0]) / 'src' / parts[1])
                _log(f"  PATH NORMALISED: {rel_path} → {fixed}")
                rel_path = fixed
            # For library crates the entry point must be lib.rs, not main.rs.
            # If the model generates */src/main.rs but */src/lib.rs already exists
            # (i.e. the workspace stub is there), treat this as a mislabelled lib.rs.
            if rel_path.endswith('/src/main.rs') or rel_path == 'src/main.rs':
                lib_path = rel_path[:-len('main.rs')] + 'lib.rs'
                if (project_dir / lib_path).exists():
                    _log(f"  PATH NORMALISED: {rel_path} → {lib_path} (lib crate stub present)")
                    rel_path = lib_path
        # Replace `{ ... }` placeholder bodies with `{ todo!() }` so Rust code
        # compiles during the build check phase (model sometimes uses ... as a stub body).
        if Path(rel_path).suffix == '.rs' and '...' in content:
            content = re.sub(r'\{\s*\.\.\.\s*\}', '{ todo!() }', content)
        if not rel_path:
            continue
        if rel_path in existing_test_files:
            skipped.append(rel_path)
            continue
        # Block writing into any test directory — model must never create test files.
        p_parts = Path(rel_path).parts
        if any(part in TEST_DIR_NAMES for part in p_parts):
            skipped.append(rel_path)
            continue
        # Protect top-level project files by name (any depth would be unusual, keep cheap).
        if Path(rel_path).name in PROTECTED and (project_dir / rel_path).exists():
            skipped.append(rel_path)
            continue
        # Protect any existing non-source file (Cargo.toml, *.toml, *.json, *.proto, etc.)
        # The model must only create/overwrite source code files.
        p = Path(rel_path)
        if p.suffix not in SOURCE_EXTENSIONS and (project_dir / p).exists():
            skipped.append(rel_path)
            _log(f"  PROTECTED (non-source file): {rel_path}")
            continue
        full = project_dir / rel_path
        write(full, content)
        written.append(rel_path)
        _log(f"  wrote {rel_path}")
    if skipped:
        _log(f"  SKIPPED (would overwrite test/config files): {', '.join(skipped)}")
    # Merge with any files written in previous inner-loop attempts so the
    # manifest always tracks the full accumulated set on disk.
    existing = load_manifest(manifest_path)
    merged = list(dict.fromkeys(existing + written))  # deduplicate, preserve order
    save_manifest(manifest_path, merged)
    return len(written)


def delete_source_files(project_dir: Path, manifest_path: Path,
                        module_dir: Path | None = None,
                        existing_test_files: set | None = None):
    """Delete model-generated files (tracked in the manifest).

    If module_dir is provided, also sweeps that directory for any untracked
    source files (ghost files written by the fix loop but never added to the
    manifest) — these are deleted as well, provided they are not test files.
    """
    files = load_manifest(manifest_path)
    deleted = 0
    deleted_paths = set()
    for rel_path in files:
        full = project_dir / rel_path
        if full.exists():
            full.unlink()
            deleted += 1
            deleted_paths.add(str(full))
    delete(manifest_path)

    # Sweep module src/ for ghost files (written by fix loop, not in manifest).
    if module_dir is not None:
        src_dir = module_dir / "src"
        if src_dir.exists():
            for candidate in src_dir.rglob("*"):
                if not candidate.is_file():
                    continue
                if candidate.suffix not in SOURCE_EXTENSIONS:
                    continue
                if str(candidate) in deleted_paths:
                    continue
                rel = str(candidate.relative_to(project_dir))
                if existing_test_files and rel in existing_test_files:
                    continue
                # Only delete if the file has no test blocks — it's a ghost implementation file.
                try:
                    content = candidate.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "#[cfg(test)]" not in content and "#[test]" not in content:
                    candidate.unlink()
                    deleted += 1
                    _log(f"  Deleted ghost file (untracked by manifest): {rel}")

    _log(f"  Deleted {deleted} generated source file(s).")


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# ---------------------------------------------------------------------------
# Build check (compile-only, no tests)
# ---------------------------------------------------------------------------

def _infer_build_check_command(test_command: str, project_dir: Path) -> str | None:
    """
    Infer a compile-only command from the test command and project layout.
    Returns None if no suitable command can be determined.

    The build check command runs BEFORE the full test suite and is used in the
    inner loop: if compilation fails, TASK 2 is retried with the compiler error
    rather than running the slow full test cycle.
    """
    cmd = test_command.strip().lower()

    # Rust / Cargo
    if cmd.startswith("cargo"):
        return "cargo check --workspace"

    # Python / pytest
    if cmd.startswith("pytest") or cmd.startswith("python -m pytest"):
        return None  # Python is interpreted; syntax errors surface at import time

    # Node / npm
    if cmd.startswith("npm ") or cmd.startswith("npx "):
        # Only attempt if a build script is defined (avoid running tests again).
        pkg_json = project_dir / "package.json"
        if pkg_json.exists():
            try:
                scripts = json.loads(pkg_json.read_text()).get("scripts", {})
                if "build" in scripts:
                    return "npm run build"
            except Exception:
                pass
        return None

    # Go
    if cmd.startswith("go test"):
        return "go build ./..."

    return None


def run_build_check(project_dir: Path, build_cmd: str) -> tuple[bool, str]:
    """
    Run the compile-only check command in project_dir.
    Returns (success, output) where output contains stderr + stdout on failure.
    """
    env = _cargo_env()

    try:
        result = subprocess.run(
            _parse_cmd(build_cmd),
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            env=env,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Build check timed out after {BUILD_TIMEOUT}s."
    except FileNotFoundError as e:
        return False, f"Build check command not found: {e}"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _profile_inputs_hash(readme_text: str, test_files: list) -> str:
    """SHA-256 of (readme + pure test file paths + first 500 chars of each).

    Only hashes files from tests/ directories — excludes src/*.rs files that
    happen to contain inline #[cfg(test)] blocks. task0c appends gen_tests
    blocks to those source files, which would otherwise change the hash on
    every restart and force unnecessary re-runs of task0a and task0b.
    """
    stable = [(rel, content) for rel, content in test_files
              if "tests/" in rel or rel.startswith("tests")]
    paths_joined = "\n".join(rel for rel, _ in stable)
    excerpts = "\n".join(content[:500] for _, content in stable[:25])
    raw = (readme_text + paths_joined + excerpts).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


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

# True when the project_dir is a git-managed workspace (--repo or auto-detected).
# Enables git commits at key pipeline transitions.
_git_managed: bool = False


# ---------------------------------------------------------------------------
# Git workspace helpers
# ---------------------------------------------------------------------------

def _is_git_repo(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path, check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


# Slice 4b: git workspace I/O relocated to pipeline_core.adapters.workspace. These are
# thin shims delegating to the adapter with injected log/emit and the live `_git_managed`
# flag, until call sites migrate onto a GitWorkspace instance (slice 5).
_WS_DEPS = WorkspaceDeps(log=_log, emit=_emit)


def _git_default_branch(project_dir: Path) -> str:
    return _wsmod.git_default_branch(project_dir)


def git_commit(project_dir: Path, message: str) -> bool:
    return _wsmod.git_commit(_WS_DEPS, project_dir, message, managed=_git_managed)


def git_tag(project_dir: Path, tag: str) -> None:
    _wsmod.git_tag(_WS_DEPS, project_dir, tag, managed=_git_managed)


def git_clean_reset(project_dir: Path) -> None:
    _wsmod.git_clean_reset(_WS_DEPS, project_dir)


def setup_workspace(repo: str, workspace_dir: Path, force_fresh: bool = False) -> Path:
    """
    Prepare a git-managed workspace for the given repo (URL or local path).

    - URL       → git clone into workspace_dir/{name}
    - local git → copy into workspace_dir/{name} (preserves history)
    - local dir → copy into workspace_dir/{name}, git init + initial commit

    In all cases, checks out (or creates) a 'pipeline' branch and returns
    the workspace path.

    If `force_fresh` is True AND the repo source is a URL, an existing
    workspace is deleted and re-cloned. This is what --clean + --repo means:
    nuke target/, stale gen_tests artifacts, half-applied LLM edits — start
    from a true upstream HEAD. For local-path repos we never auto-delete the
    user's source tree.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Derive workspace name from repo
    # Normalize various GitHub URL forms to HTTPS (SSH requires a key; HTTPS works everywhere).
    # git@github.com:user/repo.git  →  https://github.com/user/repo
    # github.com/user/repo          →  https://github.com/user/repo
    if re.match(r'^git@github\.com:', repo):
        repo = "https://github.com/" + repo.split(":", 1)[1]
    elif re.match(r'^github\.com/', repo):
        repo = "https://" + repo

    name = repo.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    dest = workspace_dir / name

    _is_url = repo.startswith("http://") or repo.startswith("https://") \
              or repo.startswith("git@") or repo.startswith("ssh://")

    if dest.exists() and force_fresh and _is_url:
        _log(f"  [git] --clean + URL repo: removing {dest} for fresh re-clone")
        shutil.rmtree(dest)

    if dest.exists():
        _log(f"  [git] workspace exists at {dest} — resuming")
    elif _is_url:
        _log(f"  [git] cloning {repo} → {dest}")
        subprocess.run(["git", "clone", repo, str(dest)], check=True)
    else:
        src = Path(repo).resolve()
        if not src.exists():
            raise RuntimeError(f"Repo path not found: {src}")
        _log(f"  [git] copying {src} → {dest}")
        shutil.copytree(str(src), str(dest))
        if not (dest / ".git").exists():
            _log("  [git] no .git found — initialising git repo")
            subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial: source snapshot", "--no-gpg-sign"],
                cwd=dest, check=True, capture_output=True,
            )
            _log("  [git] initial commit done")

    # Checkout or create pipeline branch
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=dest, capture_output=True, text=True,
    ).stdout.strip()
    if current != "pipeline":
        branches = subprocess.run(
            ["git", "branch", "--list", "pipeline"],
            cwd=dest, capture_output=True, text=True,
        ).stdout.strip()
        if branches:
            subprocess.run(["git", "checkout", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            _log("  [git] checked out existing pipeline branch")
        else:
            subprocess.run(["git", "checkout", "-b", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            _log("  [git] created pipeline branch")

    return dest


def _nightly_rules_for_cluster() -> str:
    """Return the nightly boundary rules to inject into generation prompts.
    Non-default cluster: cluster-specific override from prompt_nightly_rule.
    Default/stable cluster: derived from the cluster's excluded_cfg list.
    Returns empty string if no exclusions apply.
    """
    if _SCOPE.active and not _SCOPE.active.is_default:
        return _SCOPE.active.prompt_nightly_rule
    if _SCOPE.active and _SCOPE.active.excluded_cfg:
        predicates = ", ".join(f"`{p}`" for p in sorted(_SCOPE.active.excluded_cfg))
        return (
            f"- Do NOT write tests for code gated behind these excluded cfg predicates: {predicates}.\n"
            f"  Skip any source lines inside their `#[cfg(...)]` blocks — they are untestable on this toolchain."
        )
    return ""


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
# P1: the real StructureReader — injected into the fill engine (no monkeypatch seam).
_STRUCTURE = CliStructureReader(ADAPTERS.extractor)
# #23: build.rs cfg authority — prunes phantom clusters from source `#[cfg]` atoms.
_BUILD_SCRIPT_READER = CargoBuildScriptReader()
# P6: TASK 0c augmentation sentinels (injected into ClusterAugmentor + strip/bake).
_AUGMENT_STORE = FileAugmentStore()


class _LateStructure:
    """P2 backward-compat: late-binds `_enumerate_fns_via_extractor` so existing tests
    that monkeypatch that pipeline shim are still honoured by the attribution use-case.
    New tests inject a real `StructureReader` fake via `AttributionDeps.structure`."""

    def enumerate_fns(self, src_dir: Path) -> "list[dict] | None":
        return _enumerate_fns_via_extractor(src_dir)


# P2: composition root for the attribution use-case. Collaborators are late-bound
# (lambdas resolve the module globals at call time) so every existing monkeypatch
# seam keeps working; the AttributionDeps record is the clean DI rail a fake can fill.
_ATTR_DEPS = _attribution.AttributionDeps(
    structure=_LateStructure(),
    classify=lambda *a, **k: _classify_phase_c_test_failures(*a, **k),
    locate_fn=lambda *a, **k: _locate_fn(*a, **k),
    run_check_diag=lambda *a, **k: _run_cargo_check_diag(*a, **k),
    derive_check_only_cmd=lambda c: _derive_check_only_cmd(c),
    is_gen_tests_file=lambda n: _is_gen_tests_file(n),
    log=lambda m: _log(m),
)
# The application-layer composition context. main()'s task dispatch migrates onto it
# slice by slice (5b); for now it bundles the resolved settings + adapters.
# Wire the cross-cutting infra seams to the pipeline's concrete sinks/config (slice 5b).
_pclog.set_sinks(_log, _emit)
_pcretry.configure(max_retries=MAX_RETRIES)


def _prune_redundant_clusters(
    clusters: list["BuildCluster"],
    source_dir: Path,
    cargo_toml_path: "Path | None" = None,
    min_gated_items: int = 5,
) -> list["BuildCluster"]:
    """Drop clusters whose work is already covered by another cluster.

    Two pruning strategies:
      (P) Default-redundancy: if a cluster's distinguishing cfg_true is a
          subset of Cargo.toml's `default = [...]` features, drop it —
          the default build (cluster with no cfgs added) already covers
          its activated set.
      (Q) Empty-cluster: count items in `source_dir/src/` whose
          `#[cfg(...)]` references the cluster's distinguishing predicate.
          If fewer than `min_gated_items`, the cluster's contribution is
          too small to justify its own LLM run. Drop it.

    Always keep at least one cluster (the default). Returns the pruned
    list. Logs each prune decision with reason.
    """
    if not clusters:
        return clusters
    # Parse Cargo.toml [features].default to get baseline-enabled features.
    default_features: set[str] = set()
    if cargo_toml_path is None:
        cargo_toml_path = source_dir / "Cargo.toml"
    if cargo_toml_path.exists():
        default_features = set(_cargo_default_features(cargo_toml_path.read_text(errors="replace")))

    # Count per-predicate gated items in source.
    pred_counts: dict[str, int] = {}
    src_dir = source_dir / "src"
    if src_dir.exists():
        pred_re = re.compile(r'#\[cfg(?:_attr)?\(([^)]+(?:\([^)]*\)[^)]*)*)\)')
        bare_re = re.compile(r'\b([a-z_]\w*)(?!\s*=)')
        feat_re = re.compile(r'\bfeature\s*=\s*"([^"]*)"')
        skip = {"any", "all", "not", "cfg", "cfg_attr", "feature"}
        for rs in src_dir.rglob("*.rs"):
            try:
                txt = rs.read_text(errors="replace")
            except Exception:
                continue
            for m in pred_re.finditer(txt):
                inner = m.group(1)
                for fm in feat_re.finditer(inner):
                    name = fm.group(1)
                    if name:
                        pred_counts[name] = pred_counts.get(name, 0) + 1
                for bm in bare_re.finditer(feat_re.sub("", inner)):
                    name = bm.group(1)
                    if name and name not in skip:
                        pred_counts[name] = pred_counts.get(name, 0) + 1

    keep: list[BuildCluster] = []
    dropped: list[tuple[str, str]] = []
    # Sort clusters: stable/default first so we always retain a baseline.
    ordered = sorted(clusters, key=lambda c: (not c.is_default, c.name))
    has_baseline = False
    for c in ordered:
        distinguishing = list(c.active_cfg or [])
        # (P) default-redundancy: drop if all distinguishing cfgs are in defaults.
        if distinguishing and all(p in default_features for p in distinguishing):
            if has_baseline:
                dropped.append((c.name, f"all cfgs {distinguishing} are in default features"))
                continue
        # (Q) empty-cluster: drop if distinguishing predicates gate too little.
        if distinguishing:
            total_gated = sum(pred_counts.get(p, 0) for p in distinguishing)
            if total_gated < min_gated_items and not c.is_default:
                if has_baseline:
                    dropped.append((c.name,
                                    f"distinguishing cfgs gate only {total_gated} item(s) "
                                    f"(< {min_gated_items})"))
                    continue
        keep.append(c)
        if c.is_default or not distinguishing:
            has_baseline = True

    if not keep:
        # Pathological: nothing survived. Keep the lex-first as safety baseline.
        keep = [sorted(clusters, key=lambda c: c.name)[0]]
        _log(f"  [prune] all clusters were redundant; keeping baseline '{keep[0].name}' as safety.")

    if dropped:
        for name, reason in dropped:
            _log(f"  [prune] dropped cluster '{name}': {reason}")
    else:
        _log(f"  [prune] no redundant clusters; keeping all {len(keep)}.")
    return keep


def _filter_tests_for_cluster(test_files: list[tuple[str, str]],
                              cluster: "BuildCluster | None") -> list[tuple[str, str]]:
    """File-level cluster test filter.

    For each test file, evaluate any top-level `#![cfg(...)]` against the
    cluster's cfg-set. If the predicate is decidably false, drop the file.
    Files with no top-level cfg, or with undecidable predicates, are kept
    (safe default — better to include than wrongly exclude).
    """
    if cluster is None:
        return test_files
    cfg_true_set = {_normalize_cfg_pred(p) for p in cluster.cfg_true}
    cfg_false_set = {_normalize_cfg_pred(p) for p in cluster.cfg_false}

    out: list[tuple[str, str]] = []
    for rel, content in test_files:
        if _file_top_cfg_excluded(content, cfg_true_set, cfg_false_set):
            continue
        out.append((rel, content))
    return out


def _normalize_cfg_pred(p: str) -> str:
    """Normalize a cfg predicate for set comparison (strip spaces).
    Thin shim → domain (`pipeline_core.domain.cfg`)."""
    return _cfg.normalize_pred(p)


def _file_top_cfg_excluded(content: str, cfg_true: set[str], cfg_false: set[str]) -> bool:
    """Return True if the file has a top-level `#![cfg(...)]` that evaluates
    to false. Conservative: returns False (don't exclude) on any uncertainty.

    We use a narrow regex match for the common case `#![cfg(PRED)]` rather
    than full AST parsing — test files are small and the pattern is common.
    """
    # Match all top-level #![cfg(...)] occurrences (allow nested parens up to 3 deep).
    pat = re.compile(r'^\s*#!\[cfg\((.*?)\)\]\s*$', re.MULTILINE)
    for m in pat.finditer(content):
        pred = m.group(1).strip()
        if _eval_cfg_pred(pred, cfg_true, cfg_false) is False:
            return True
    return False


def _eval_cfg_pred(pred: str, cfg_true: set[str], cfg_false: set[str]) -> "bool | None":
    """Evaluate a cfg predicate (`not/all/any`, bare ident, `feature = "X"`).
    Thin shim → `pipeline_core.domain.cfg.CfgSet.evaluate`."""
    return _cfg.CfgSet.of(cfg_true, cfg_false).evaluate(pred)


def _cfg_false_for_cargo(pred: str, cfg_true: set[str], cfg_false: set[str],
                         default_features: "frozenset[str] | set[str]" = frozenset()) -> bool:
    """cargo-faithful "does this cfg keep the item from compiling/running here?".
    Thin shim → `pipeline_core.domain.cfg.CfgSet.excludes_for_cargo`."""
    return _cfg.CfgSet.of(cfg_true, cfg_false, default_features).excludes_for_cargo(pred)


def _cluster_extractor_args(cluster: "BuildCluster | None") -> list[str]:
    """Build `--cfg-true PRED` / `--cfg-false PRED` CLI args for the extractor
    from a BuildCluster. Returns empty list if cluster is None."""
    if cluster is None:
        return []
    out: list[str] = []
    for pred in cluster.cfg_true:
        out.extend(["--cfg-true", pred])
    for pred in cluster.cfg_false:
        out.extend(["--cfg-false", pred])
    return out


def _extract_skeleton(input_dir: Path, output_dir: Path,
                      cluster: "BuildCluster | None" = None) -> dict:
    """Invoke the Rust skeleton extractor over a live source tree.

    Run only during the bake step (`_bake_skeleton_artifacts`), before
    strip removes the original source (Hard Rule 11).

    When `cluster` is provided, item-level `#[cfg(...)]` filtering runs
    first — items whose predicates evaluate to false under the cluster's
    cfg-set are dropped before body-stubbing.

    Returns {'ok': bool, 'reports': list[dict], 'stderr': str}. On success,
    `output_dir` contains the skeletonised .rs files mirroring input_dir's
    layout.
    """
    # slice 5a: via Extractor adapter. Map the cluster's cfg-sets to the adapter's
    # cfg_true/cfg_false tuples (same `--cfg-true/--cfg-false` args as _cluster_extractor_args).
    ct = tuple(cluster.cfg_true) if cluster is not None else ()
    cf = tuple(cluster.cfg_false) if cluster is not None else ()
    return ADAPTERS.extractor.skeletonise(input_dir, output_dir, cfg_true=ct, cfg_false=cf)


def _compute_fn_name_counts(src_dir: Path) -> dict[str, int]:
    """Count word-boundary fn-name references across every .rs file under
    `src_dir`. Used to build leaf-first fill ordering for Phase C.

    Caller is responsible for passing a directory that contains real source
    (typically the live project_dir/src/ BEFORE strip). Result is a dict
    `{fn_name: ref_count}` that gets serialised to disk so Phase C never
    needs to re-read source after strip.
    """
    text_parts: list[str] = []
    if not src_dir.exists():
        return {}
    for p in src_dir.rglob("*.rs"):
        try:
            text_parts.append(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    text = "\n".join(text_parts)
    if not text:
        return {}
    # P7: the `fn <name>` declaration scan + per-name `\b<name>\b` reference
    # count are now a pure identifier tokeniser (byte-equivalent), no regex.
    return fn_reference_counts(text)


def _bake_skeleton_artifacts(
    project_dir: Path, module_name: str,
) -> dict:
    """Run BEFORE strip while the live source is still on disk.

    Discovers clusters, prunes redundant ones, then for each cluster:
      - extracts the skeleton (signatures + types + macros, bodies →
        `unimplemented!()`) to `.skeleton/{cluster}/`
      - computes fn-name reference counts and writes them to
        `.skeleton/{cluster}/_leaf_order.json`

    Cluster metadata is saved to `.skeleton/_clusters.json` so TASK 2 can
    rehydrate clusters post-strip without re-scanning source.

    Per Hard Rule 11: this is the ONLY place pipeline code is allowed to
    read the original source. After strip, every downstream phase reads
    `.skeleton/` artefacts only.

    Returns {'ok': bool, 'clusters': list[str], 'reason': str}.
    """
    skel_root = project_dir / ".skeleton"
    skel_root.mkdir(parents=True, exist_ok=True)

    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        return {"ok": False, "clusters": [],
                "reason": f"no source at {src_dir} — strip already happened?"}

    # Cluster discovery on the live source (Principle 1: deterministic).
    try:
        clusters = task0_cfg_scan(project_dir)
    except Exception as e:
        return {"ok": False, "clusters": [], "reason": f"task0_cfg_scan: {e}"}
    if not clusters:
        clusters = [BuildCluster(
            name="default", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
            is_default=True,
        )]
    clusters = sorted(clusters, key=lambda c: c.name)
    _log(f"  [bake-skeleton] discovered {len(clusters)} cluster(s): "
         f"{', '.join(c.name for c in clusters)}")

    cargo_toml = project_dir / "Cargo.toml"
    clusters = _prune_redundant_clusters(
        clusters, project_dir,
        cargo_toml_path=cargo_toml if cargo_toml.exists() else None,
    )
    _log(f"  [bake-skeleton] processing {len(clusters)} cluster(s) after pruning: "
         f"{', '.join(c.name for c in clusters)}")

    name_counts = _compute_fn_name_counts(src_dir)
    _log(f"  [bake-skeleton] computed reference counts for {len(name_counts)} fn name(s).")

    baked: list[str] = []
    for cluster in clusters:
        skeleton_dir = skel_root / cluster.name
        if skeleton_dir.exists():
            shutil.rmtree(skeleton_dir)
        skeleton_dir.mkdir(parents=True, exist_ok=True)
        # Pass `src_dir` (NOT project_dir) so the extractor can never walk
        # into prior cluster outputs under `.skeleton/`. Output mirrors
        # under `skeleton_dir/src/`, matching the runnable-crate layout
        # Phase A expects.
        res = _extract_skeleton(src_dir, skeleton_dir / "src", cluster=None)
        if not res["ok"]:
            _log(f"  [bake-skeleton] cluster {cluster.name}: extractor FAILED: "
                 f"{res['stderr']}")
            return {"ok": False, "clusters": baked,
                    "reason": f"extract {cluster.name}: {res['stderr']}"}
        order_path = skeleton_dir / "_leaf_order.json"
        try:
            order_path.write_text(json.dumps(name_counts, sort_keys=True),
                                  encoding="utf-8")
        except Exception as e:
            _log(f"  [bake-skeleton] cluster {cluster.name}: leaf-order write failed: {e}")
        _log(f"  [bake-skeleton] cluster {cluster.name}: {len(res['reports'])} "
             f"file(s) skeletonised → {skeleton_dir}")
        baked.append(cluster.name)

    # Save cluster metadata for TASK 2 to rehydrate.
    try:
        manifest = {
            "module": module_name,
            "clusters": [
                {
                    "name": c.name,
                    "env_extra": dict(c.env_extra),
                    "tarpaulin_exclude_files": list(c.tarpaulin_exclude_files),
                    "excluded_cfg": list(c.excluded_cfg),
                    "active_cfg": list(c.active_cfg),
                    "prompt_nightly_rule": c.prompt_nightly_rule,
                    "is_default": c.is_default,
                    "cfg_true": list(getattr(c, "cfg_true", []) or []),
                    "cfg_false": list(getattr(c, "cfg_false", []) or []),
                    "toolchain": getattr(c, "toolchain", None),
                    "requires_nightly": bool(getattr(c, "requires_nightly", False)),
                }
                for c in clusters
            ],
        }
        (skel_root / "_clusters.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )
    except Exception as e:
        _log(f"  [bake-skeleton] cluster manifest write failed: {e}")

    # Capture each gen_tests sibling's ORIGIN cluster cfg from the live (pre-strip)
    # decls. task0c stamps the generating cluster's cfg on the `mod gen_tests_N;`
    # decl; strip then removes the decl and `saved_gen_blocks` keeps only the body,
    # losing provenance. Without this manifest, downstream propagation re-derives
    # the gate from the CURRENT cluster (always-active → no protection), so a
    # nightly-only test (e.g. `request_ref`) leaks into stable/no_core → E0658.
    # Keyed by sibling stem "{parent_stem}.gen_tests_{n}" → origin cfg ('' = default).
    try:
        origin: dict[str, str] = {}
        for rs in sorted(src_dir.rglob("*.rs")):
            if _is_gen_tests_file(rs.name):
                continue
            try:
                text = rs.read_text(encoding="utf-8")
            except OSError:
                continue
            for n, cfg in _gen_tests_origin_cfgs(text).items():
                origin[f"{rs.stem}.gen_tests_{n}"] = cfg
        (skel_root / "_gen_tests_origin.json").write_text(
            json.dumps(origin, indent=2, sort_keys=True), encoding="utf-8",
        )
        if origin:
            _log(f"  [bake-skeleton] captured origin cfg for {len(origin)} gen_tests sibling(s).")
    except Exception as e:
        _log(f"  [bake-skeleton] gen_tests origin manifest write failed: {e}")

    return {"ok": True, "clusters": baked, "reason": ""}


def _load_baked_clusters(project_dir: Path) -> list["BuildCluster"]:
    """Rehydrate clusters from `.skeleton/_clusters.json` (written by
    `_bake_skeleton_artifacts` before strip). Returns [] if absent.

    Replaces every post-strip call to `task0_cfg_scan` — those would need
    the source tree, which is gone.
    """
    path = project_dir / ".skeleton" / "_clusters.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[BuildCluster] = []
    for c in data.get("clusters", []):
        bc = BuildCluster(
            name=c.get("name", ""),
            env_extra=dict(c.get("env_extra", {}) or {}),
            tarpaulin_exclude_files=list(c.get("tarpaulin_exclude_files", []) or []),
            excluded_cfg=list(c.get("excluded_cfg", []) or []),
            active_cfg=list(c.get("active_cfg", []) or []),
            prompt_nightly_rule=c.get("prompt_nightly_rule", "") or "",
            is_default=bool(c.get("is_default", False)),
        )
        # cfg_true / cfg_false / toolchain are read-only @properties derived
        # from active_cfg, excluded_cfg, and requires_nightly. They're written
        # into the manifest for human readability but must not be assigned
        # back onto the cluster.
        if c.get("requires_nightly"):
            bc.requires_nightly = True
        out.append(bc)
    return out


def _load_baked_leaf_order(project_dir: Path, cluster_name: str) -> dict[str, int]:
    """Load the `{fn_name: ref_count}` map saved during the bake step.
    Returns {} if absent — caller falls back to alphabetical order."""
    path = project_dir / ".skeleton" / cluster_name / "_leaf_order.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _load_skeleton_files(skeleton_dir: Path) -> list[tuple[str, str]]:
    """Return [(relative_path, content), ...] for every .rs file under skeleton_dir."""
    out: list[tuple[str, str]] = []
    for p in sorted(skeleton_dir.rglob("*.rs")):
        try:
            out.append((str(p.relative_to(skeleton_dir)), p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _format_skeleton_for_prompt(skeleton_files: list[tuple[str, str]]) -> str:
    """Render the skeleton as a sequence of <FILE> blocks for prompt embedding."""
    parts: list[str] = []
    for rel, content in skeleton_files:
        parts.append(f'<FILE path="{rel}">\n{content}\n</FILE>')
    return "\n\n".join(parts)


def _format_skeleton_index_for_cluster(
    skeleton_files: list[tuple[str, str]],
    cluster: "BuildCluster | None",
) -> str:
    """Same as _format_skeleton_index but additionally elides items whose
    `#[cfg(...)]` evaluates to false under the cluster's cfg-set.

    Used in Phase C prompts so the model sees only items that compile in
    THIS cluster — reduces cognitive load without affecting the on-disk
    unfiltered skeleton (which Rust's own cfg eval handles correctly).

    Items whose cfg is undecidable (unknown keys) are kept (safe default).
    """
    if cluster is None:
        return _format_skeleton_index(skeleton_files)
    cfg_true = {_normalize_cfg_pred(p) for p in cluster.cfg_true}
    cfg_false = {_normalize_cfg_pred(p) for p in cluster.cfg_false}

    # Per-item cfg attr matcher: a leading `#[cfg(...)]` (or several) above
    # the item declaration.
    cfg_attr_re = re.compile(r'^[ \t]*#\[cfg\(([^\]]+(?:\([^\]]*\)[^\]]*)*)\)\]\s*$', re.MULTILINE)

    filtered_files: list[tuple[str, str]] = []
    for rel, content in skeleton_files:
        # Walk the content line by line collecting leading #[cfg(...)] groups
        # before each top-level item; drop the item block if any cfg evaluates
        # to false.
        lines = content.split("\n")
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            # Collect attributes that precede the current item (any line
            # starting with `#[...]` or `#![...]` or doc comment).
            attr_start = i
            collected_cfgs: list[str] = []
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()
                m = cfg_attr_re.match(line)
                if m:
                    collected_cfgs.append(m.group(1).strip())
                    i += 1
                    continue
                # Any other attribute / doc / blank line: keep walking.
                if stripped.startswith("#[") or stripped.startswith("#![") or stripped.startswith("///") or stripped.startswith("//!") or stripped == "":
                    i += 1
                    continue
                break
            # i now points to (probably) an item declaration. Evaluate cfgs.
            keep = True
            for pred in collected_cfgs:
                if _eval_cfg_pred(pred, cfg_true, cfg_false) is False:
                    keep = False
                    break
            if keep:
                # Emit attr block + the item.
                out_lines.extend(lines[attr_start:i + 1])
                i += 1
            else:
                # Skip this item entirely — including its body if it has one.
                # Simple heuristic: skip until brace depth returns to zero or
                # we hit a `;` at depth zero (for body-less items like
                # `pub use X;`).
                if i >= len(lines):
                    break
                line = lines[i]
                if "{" in line:
                    depth = line.count("{") - line.count("}")
                    i += 1
                    while i < len(lines) and depth > 0:
                        depth += lines[i].count("{") - lines[i].count("}")
                        i += 1
                else:
                    # body-less item terminated by ';' on the same line
                    i += 1
        filtered_files.append((rel, "\n".join(out_lines)))
    return _format_skeleton_index(filtered_files)


def _format_skeleton_index(skeleton_files: list[tuple[str, str]]) -> str:
    """Render skeleton as signatures-only index. Drops bodies (which are
    `unimplemented!()` anyway), keeps type declarations + function signatures
    + macro definitions for cross-reference. Used in Phase C prompts so we
    don't waste input tokens on body stubs the model already knows about.

    P3.11: macro_rules! bodies are also elided. Phase A preserves macros
    verbatim on disk; the prompt's index just needs the macro NAMES so the
    model knows what's exported, not the multi-KB rule definitions.
    """
    out_parts: list[str] = []

    def _elide_bodies(text: str, headers: list, terminator: str) -> str:
        """Walk header matches; for each, brace-match to find the body and
        replace it with `terminator`. Returns the rewritten text."""
        result_parts: list[str] = []
        last = 0
        for h in headers:
            body_start = h.brace_open + 1
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            body_end = i
            result_parts.append(text[last:h.start])
            result_parts.append(text[h.start:h.brace_open].rstrip())
            result_parts.append(terminator)
            last = body_end
        result_parts.append(text[last:])
        return "".join(result_parts)

    for rel, content in skeleton_files:
        # Order matters: elide macros first (their bodies contain `fn` patterns
        # in rule expansions that would otherwise confuse the fn scan).
        condensed = _elide_bodies(content, macro_headers(content), " { /* macro body elided — preserved verbatim on disk */ }")
        condensed = _elide_bodies(condensed, fn_headers(condensed, line_anchored=True), ";")
        out_parts.append(f'<FILE path="{rel}" mode="signatures-only">\n{condensed}\n</FILE>')
    return "\n\n".join(out_parts)


def _collect_skeleton_symbols(skeleton_files: list[tuple[str, str]]) -> set[str]:
    """Return the set of fn/method names declared in the skeleton.

    Used by Phase B validation: every target_symbol the LLM emits should exist
    in this set. We use a coarse regex — sufficient for validation, not
    intended to be precise.
    """
    syms: set[str] = set()
    for _, content in skeleton_files:
        syms |= fn_decl_names(content)
    return syms


def _task2_group_tests(
    skeleton_dir: Path,
    test_files: list[tuple[str, str]],
    cluster_name: str,
    module_name: str,
) -> list[dict]:
    """Phase B: ask the LLM to group test files into cohesive units.

    Returns a list of group dicts: {name, rationale, test_files, target_symbols}.
    Falls back to one-group-per-test-file on LLM failure or validation failure.
    Result is also written to skeleton_dir/groups.json.
    """
    out_path = skeleton_dir / "groups.json"
    skeleton_files = _load_skeleton_files(skeleton_dir)
    if not skeleton_files or not test_files:
        groups = _fallback_groups_per_file(test_files)
        out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
        return groups

    skeleton_block = _format_skeleton_for_prompt(skeleton_files)
    tests_block = "\n\n".join(
        f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        for rel, content in test_files
    )
    sym_set = _collect_skeleton_symbols(skeleton_files)
    test_paths = sorted({rel for rel, _ in test_files})

    prompt = f"""You are planning a test-driven reconstruction of the `{module_name}` crate (cluster: {cluster_name}).

The implementation skeleton below has all type definitions, signatures, and macros, but
every function body is `unimplemented!()`. Your job is to group the test files into
cohesive units. Each group should exercise a coherent piece of functionality so that one
LLM call per group can fill the necessary `unimplemented!()` bodies and make that group's
tests pass.

Rules:
  - Aim for 6-15 groups. Each group should contain 1-3 test files (smaller
    groups → smaller per-call output → fewer empty responses from the model).
  - Every test file appears in EXACTLY one group. Include compile-fail tests
    (e.g. tests/compiletest.rs, trybuild harness files) — those are real
    tests and must be assigned to a group too.
  - target_symbols must be function/method names that exist in the skeleton.
  - rationale: one sentence describing what the group tests.

Test files you MUST cover (every entry here appears in exactly one group):
{chr(10).join(f"  - {p}" for p in test_paths)}

Implementation skeleton:
{skeleton_block}

Test files:
{tests_block}

Output a JSON object with this exact shape (no prose, no markdown fences):

{{
  "groups": [
    {{
      "name": "short_kebab_case_name",
      "rationale": "one sentence",
      "test_files": ["tests/foo.rs", "tests/bar.rs"],
      "target_symbols": ["func_a", "Method::name", "Type::method"]
    }}
  ]
}}
"""

    for attempt in range(2):
        response = ask(prompt, model=_TASK1_MODEL)
        groups, err, recoverable_missing = _parse_and_validate_groups(
            response, test_paths, sym_set
        )
        if groups is not None:
            # P3.6: post-process to enforce max group size = 3.
            groups = _split_oversized_groups(groups, max_size=3)
            _log(f"  [phase-b] LLM produced {len(groups)} group(s) (post-split).")
            out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
            return groups
        # Auto-recover small omissions (≤2 missing test files) by appending
        # them to a "miscellaneous" group rather than burning another full LLM
        # round-trip. Compile-fail tests (trybuild) tend to get silently
        # dropped by the model.
        if recoverable_missing and len(recoverable_missing) <= 2 and attempt == 0:
            # Re-parse to get the partial groups (validator returned None when
            # it rejected, but the parsed structure is still useful).
            partial = _extract_groups_or_none(response)
            if partial is not None:
                partial.append({
                    "name": "miscellaneous_auto_added",
                    "rationale": "auto-appended test files omitted by LLM grouping",
                    "test_files": sorted(recoverable_missing),
                    "target_symbols": [],
                })
                # Apply P3.6 split even on the auto-recovered path so big
                # groups still get chunked.
                partial = _split_oversized_groups(partial, max_size=3)
                _log(f"  [phase-b] auto-recovered {len(recoverable_missing)} "
                     f"omitted test file(s) into a 'miscellaneous' group "
                     f"({len(partial)} groups post-split).")
                out_path.write_text(json.dumps(partial, indent=2), encoding="utf-8")
                return partial
        _log(f"  [phase-b] attempt {attempt + 1} rejected: {err}")

    _log("  [phase-b] LLM grouping failed validation twice — falling back to per-file groups.")
    groups = _fallback_groups_per_file(test_files)
    out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
    return groups


def _split_oversized_groups(groups: list[dict], max_size: int = 3) -> list[dict]:
    """Split any group with >max_size test files into smaller chunks.

    Each chunk inherits the parent's rationale + target_symbols, with the
    name suffixed by a 1-based index. Keeps target_symbols on every chunk
    so leaf-first ordering still works.
    """
    out: list[dict] = []
    for g in groups:
        tests = list(g.get("test_files", []))
        if len(tests) <= max_size:
            out.append(g)
            continue
        base_name = g.get("name", "group")
        chunks: list[list[str]] = [tests[i:i + max_size] for i in range(0, len(tests), max_size)]
        for idx, chunk in enumerate(chunks, 1):
            out.append({
                "name": f"{base_name}_{idx}of{len(chunks)}",
                "rationale": f"{g.get('rationale', '')} (auto-split {idx}/{len(chunks)})",
                "test_files": chunk,
                "target_symbols": list(g.get("target_symbols", [])),
            })
    return out


def _extract_groups_or_none(response: str) -> list[dict] | None:
    """Parse the LLM's JSON manifest into a groups list without validation."""
    txt = response.strip()
    if txt.startswith("```"):
        txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
        txt = re.sub(r'\n?```\s*$', '', txt)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "groups" not in data:
        return None
    g = data["groups"]
    return g if isinstance(g, list) else None


def _parse_and_validate_groups(
    response: str, test_paths: list[str], known_symbols: set[str]
) -> tuple[list[dict] | None, str, set[str]]:
    """Parse the LLM's JSON manifest, validate coverage and symbol existence.

    Returns (groups, error_message, recoverable_missing).
      - groups is None on validation failure.
      - recoverable_missing is the set of test files the LLM omitted; populated
        only when the failure was a coverage gap (not a structural error).
    """
    # Tolerate fenced JSON.
    txt = response.strip()
    if txt.startswith("```"):
        txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
        txt = re.sub(r'\n?```\s*$', '', txt)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError as e:
        return None, f"not valid JSON: {e}", set()
    if not isinstance(data, dict) or "groups" not in data:
        return None, "missing 'groups' key", set()
    groups = data["groups"]
    if not isinstance(groups, list) or not groups:
        return None, "'groups' must be a non-empty list", set()

    seen_tests: set[str] = set()
    expected_tests = set(test_paths)
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            return None, f"group #{i} is not an object", set()
        for key in ("name", "rationale", "test_files", "target_symbols"):
            if key not in g:
                return None, f"group #{i} missing key '{key}'", set()
        if not isinstance(g["test_files"], list) or not g["test_files"]:
            return None, f"group '{g.get('name','?')}' has empty test_files", set()
        for tf in g["test_files"]:
            if tf in seen_tests:
                return None, f"test file '{tf}' appears in multiple groups", set()
            seen_tests.add(tf)
        # Symbol existence is a soft check — warn but don't reject.
        # (LLM may use slight name variations like `Self::method`.)

    missing = expected_tests - seen_tests
    extra = seen_tests - expected_tests
    if missing:
        return None, f"groups omit {len(missing)} test file(s); first: {sorted(missing)[:3]}", missing
    if extra:
        return None, f"groups include unknown test file(s): {sorted(extra)[:3]}", set()
    return groups, "ok", set()


def _fallback_groups_per_file(test_files: list[tuple[str, str]]) -> list[dict]:
    """One group per test file — deterministic safe default."""
    groups: list[dict] = []
    for rel, _ in test_files:
        stem = Path(rel).stem
        groups.append({
            "name": f"file_{stem}",
            "rationale": f"single-file fallback grouping for {rel}",
            "test_files": [rel],
            "target_symbols": [],
        })
    return groups


# Per-fn fill rules relocated to application/reconstruct/fill_rules.py (P2);
# aliases keep engine call sites + pins unchanged.
from pipeline_core.application.reconstruct.fill_rules import (  # noqa: E402
    is_unimplemented_body as _is_unimplemented_body,
)
from pipeline_core.application.reconstruct.fill_rules import (  # noqa: E402
    module_of as _module_of,
)


def _extract_fn_blocks(response: str) -> dict[str, str]:
    """Parse `<FN sig="...">body</FN>` blocks from the LLM response.

    Returns {signature → body_text}. The body is the raw content between
    the opening `<FN ...>` and closing `</FN>` markers, trimmed of leading/
    trailing whitespace. The signature must include everything from "fn name"
    to the opening `{` exclusive — matching how `_merge_fills_preserving_existing`
    keys its disk-function map.

    Tolerates loosely-formatted responses (extra whitespace, mixed quoting on
    the sig attribute).
    """
    # `(?s)` for DOTALL so body can span lines. Match sig attribute value
    # in double quotes (most common) or single quotes (sometimes used).
    pat = re.compile(
        r'<FN\s+sig\s*=\s*(?P<q>["\'])(?P<sig>.+?)(?P=q)\s*>(?P<body>.*?)</FN>',
        re.DOTALL,
    )
    out: dict[str, str] = {}
    stubs_dropped = 0
    for m in pat.finditer(response):
        sig = m.group("sig").strip()
        body = m.group("body").strip()
        if not sig or not body:
            continue
        # P3.10 enforcement: drop stub bodies so they don't appear successful.
        if _is_unimplemented_body(body) or body.strip() in ("todo!()", "todo!();"):
            stubs_dropped += 1
            continue
        out[sig] = body
    if stubs_dropped:
        _log(f"  [phase-c] dropped {stubs_dropped} stub <FN> block(s) "
             f"from model output (model re-emitted unimplemented!).")
    return out


def _merge_fn_blocks_into_disk(disk_text: str, fn_blocks: dict[str, str]) -> str:
    """Merge dict of {signature → body} into disk_text by replacing the body
    of each disk function whose signature key matches and whose body is
    currently `unimplemented!()`.

    All non-function content on disk is preserved verbatim. This is the
    P3.2 partner of `_merge_fills_preserving_existing` for the per-function
    output format — same safety guarantees, simpler input.
    """
    if not fn_blocks:
        return disk_text
    # Re-extract disk functions to find their bodies, then splice in.
    matches: list[tuple[str, int, int]] = []  # (sig, body_start, body_end)
    for h in fn_headers(disk_text, line_anchored=False):
        body_start = h.brace_open + 1
        depth = 1
        i = body_start
        while i < len(disk_text) and depth > 0:
            ch = disk_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        body_end = i - 1
        # Strip the trailing '{' so the sig matches the LLM's <FN sig="..."> key
        # (which is the signature without the opening brace).
        sig_line = disk_text[h.start:body_start].rstrip("{ \t\n").strip()
        matches.append((sig_line, body_start, body_end))

    out = disk_text
    for sig, body_start, body_end in reversed(matches):
        existing_body = out[body_start:body_end]
        if not _is_unimplemented_body(existing_body):
            continue  # already filled — never overwrite
        new_body = fn_blocks.get(sig)
        if new_body is None:
            # Try a relaxed match: normalize whitespace and compare.
            normalized_sig = " ".join(sig.split())
            for k, v in fn_blocks.items():
                if " ".join(k.split()) == normalized_sig:
                    new_body = v
                    break
        if new_body is None:
            continue
        out = out[:body_start] + new_body + out[body_end:]
    return out


# ---------------------------------------------------------------------------
# Per-fn TDD fill (Flavour B): one LLM call per unimplemented function.
# We own the splice site; the model returns only the body text.
# ---------------------------------------------------------------------------

def _locate_fn(file_text: str, fn_name: str) -> tuple[str, int, int, int] | None:
    """Return (sig_text, sig_start, body_start, body_end) for the first fn whose
    name matches `fn_name`. body_start is just after the opening `{`; body_end
    is the position of the matching `}` (exclusive end of body).

    Returns None if not found or braces are malformed.
    """
    for h in fn_headers(file_text, line_anchored=False):
        if h.name != fn_name:
            continue
        sig_start = h.start
        body_start = h.brace_open + 1
        depth = 1
        i = body_start
        while i < len(file_text) and depth > 0:
            ch = file_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            return None
        body_end = i - 1
        sig_text = file_text[sig_start:body_start].rstrip("{ \t\n").strip()
        return sig_text, sig_start, body_start, body_end
    return None


def _is_pub_fn(file_text: str, fn_name: str) -> bool:
    """True if the fn declaration includes `pub` (any visibility variant)."""
    loc = _locate_fn(file_text, fn_name)
    if loc is None:
        return False
    sig = loc[0]
    return starts_with_pub(sig)


def _replace_fn_signature(file_path: Path, fn_name: str, new_sig: str) -> bool:
    """P3.1: replace the verbatim signature line(s) of `fn_name` with
    `new_sig`. The body is left intact. Used only when the per-fn fix loop
    has tried >= 5 attempts and the model proposes a signature change in
    addition to a new body. Returns True if the splice was applied.

    Constraints:
      - target fn must exist in file
      - target fn must NOT be `pub` (changing public sigs would break tests)
      - new_sig must contain `fn {fn_name}` (sanity check; model can't rename)
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False
    if _is_pub_fn(text, fn_name) is not False:
        # Only relax for private fns; pub fns are part of the test surface.
        pass
    if _is_pub_fn(text, fn_name):
        return False
    if not declares_fn(new_sig, fn_name):
        return False
    loc = _locate_fn(text, fn_name)
    if loc is None:
        return False
    _old_sig, sig_start, body_start, _body_end = loc
    # Find the start of the line that contains the sig.
    line_start = text.rfind("\n", 0, sig_start) + 1
    # New sig must end immediately before the opening brace (which body_start
    # points just past). Compose: text[:line_start] + new_sig.rstrip() + " {" + text[body_start:]
    cleaned = new_sig.strip().rstrip("{").rstrip()
    new_text = text[:line_start] + cleaned + " {" + text[body_start:]
    file_path.write_text(new_text, encoding="utf-8")
    return True


def _extract_signature_block(response: str) -> str | None:
    """Pull `<SIGNATURE>...</SIGNATURE>` from an LLM response. Returns the
    trimmed body or None if absent."""
    m = re.search(r"<SIGNATURE>(.*?)</SIGNATURE>", response, re.DOTALL)
    if not m:
        return None
    txt = m.group(1).strip()
    return txt or None


# Byte splice + syn locator relocated to application/reconstruct/splice.py
# (P2 slice 3). Thin late-binding shims (NOT partials): the e2e harness
# monkeypatches `pipeline._enumerate_fns_via_extractor`, so the enumerator
# must be resolved at CALL time.
def _splice_fn_body_at(file_path: Path, body_byte_start: int, body_byte_end: int,
                       new_body: str, allow_nonstub: bool = False) -> tuple[bool, int]:
    return _reconstruct_splice.splice_fn_body_at(
        file_path, body_byte_start, body_byte_end, new_body,
        allow_nonstub=allow_nonstub)


def _locate_fn_by_record(file_path: Path, name: str, container: str, sig_text: str,
                         cfg_predicates: "tuple[str, ...] | None" = None,
                         ) -> "tuple[int, int, int, str] | None":
    return _reconstruct_splice.locate_fn_by_record(
        _enumerate_fns_via_extractor, file_path, name, container, sig_text, cfg_predicates)


def _splice_fn_body(file_path: Path, fn_name: str, new_body: str,
                    allow_nonstub: bool = False) -> tuple[bool, int]:
    """Replace the body of `fn_name` in `file_path` with `new_body`.

    By default only splices when the current body is an `unimplemented!()`
    stub — never overwrites an already-filled function. Pass
    `allow_nonstub=True` (fix-loop per_fn strategy) to re-fill a fn whose
    previous body compiled wrong. The file is re-read here on every call, so
    byte offsets are always fresh — safe to call repeatedly on the same file.
    Returns (success, bytes_delta).
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False, 0
    loc = _locate_fn(text, fn_name)
    if loc is None:
        return False, 0
    _sig, _sig_start, body_start, body_end = loc
    cur_body = text[body_start:body_end]
    if not allow_nonstub and not _is_unimplemented_body(cur_body):
        return False, 0
    # Normalise the body: model may have included extraneous braces or
    # leading/trailing whitespace. Strip outer braces if present.
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    if not nb or _is_unimplemented_body(nb):
        return False, 0
    # Re-indent so the body sits cleanly inside the existing braces. Detect
    # the indentation of the opening brace's line and use it +4 for body.
    line_start = text.rfind("\n", 0, _sig_start) + 1
    sig_line = text[line_start:_sig_start]
    base_indent = ""
    for ch in sig_line:
        if ch in (" ", "\t"):
            base_indent += ch
        else:
            break
    inner_indent = base_indent + "    "
    nb_indented = "\n" + "\n".join(
        (inner_indent + ln) if ln.strip() else "" for ln in nb.splitlines()
    ) + "\n" + base_indent
    new_text = text[:body_start] + nb_indented + text[body_end:]
    file_path.write_text(new_text, encoding="utf-8")
    return True, len(new_text) - len(text)


# --- Per-impl response parser + splice helpers --------------------------
#
# The per-impl prompt asks the model to emit one `<FN name="X" block="...">`
# block per stub. The parser collects them keyed by (block_container,
# fn_name) so the splicer can route each body to the correct impl.

# R8.5: the `<FN …>`/`<FILE …>` model-protocol parsers (`extract_fn_bodies`,
# `_find_fn_blocks`, `_split_leading_where`, …) live in domain.llm_output;
# imported at top. `_locate_fn` (Rust-SOURCE) now scans via domain.rust_fn_headers.


def _splice_fn_with_where(
    file_path: Path,
    container: str,
    fn_name: str,
    body: str,
    where_clause: str | None = None,
    allow_nonstub: bool = False,
) -> tuple[bool, str, int]:
    """Splice `body` into `fn_name` whose containing impl block matches
    `container`. If `where_clause` is provided, insert it into the fn
    signature (between sig and the opening brace).

    `allow_nonstub=True` (fix-loop) re-fills a fn whose body is already
    filled-but-wrong; default refuses non-stub bodies (build path).

    Returns (success, reason, bytes_delta). `reason` is "ok" on success
    or a short failure tag for telemetry / retro context.
    """
    if not file_path.exists():
        return False, "file_missing", 0
    loc = _locate_fn_by_record(file_path, fn_name, container, "")
    if loc is None:
        # Fallback: the extractor's container string sometimes includes
        # the first member's signature. Match on the impl HEADER prefix
        # only (everything before the first `{`).
        loose_container = container.split("{", 1)[0].strip()
        loc = _locate_fn_by_record(file_path, fn_name, loose_container, "")
    if loc is None:
        return False, "fn_not_found", 0
    sig_byte_start, body_byte_start, body_byte_end, sig_text = loc

    # If a where-clause patch is supplied, splice it into the signature.
    # BYTE space (offsets are byte offsets from the enumerator — see
    # _splice_fn_body_at; a char-indexed str would mis-slice on multibyte UTF-8).
    # The signature spans [sig_byte_start, opening-brace-of-body); insert
    # `\n    <clause>` before the opening brace.
    if where_clause:
        raw = file_path.read_bytes()
        brace_pos = raw.rfind(b"{", sig_byte_start, body_byte_start + 1)
        if brace_pos != -1:
            # Don't double-add a where clause if the sig already has one.
            sig_slice = raw[sig_byte_start:brace_pos].decode("utf-8", errors="replace")
            if "where" not in sig_slice:
                insert = f"\n    {where_clause}\n".encode()
                raw = raw[:brace_pos] + insert + raw[brace_pos:]
                shift = len(insert)
                body_byte_start += shift
                body_byte_end += shift
                file_path.write_bytes(raw)

    ok, delta = _splice_fn_body_at(file_path, body_byte_start, body_byte_end, body,
                                   allow_nonstub=allow_nonstub)
    if not ok:
        return False, "splice_rejected", 0
    return True, "ok", delta


def _find_tests_referencing_fn(
    test_files: list[Path],
    fn_name: str,
    max_blocks: int = 4,
) -> list[tuple[str, str]]:
    """Return up to `max_blocks` test files whose source text mentions `fn_name`.

    Cheap textual heuristic — sufficient for Rust tests that call the fn
    by name (most common). Results are ranked by reference count.
    """
    if not fn_name:
        return []
    ranked: list[tuple[int, Path]] = []
    pat = re.compile(r"\b" + re.escape(fn_name) + r"\b")
    for tf in test_files:
        try:
            txt = tf.read_text(encoding="utf-8")
        except Exception:
            continue
        n = len(pat.findall(txt))
        if n:
            ranked.append((n, tf))
    ranked.sort(key=lambda x: -x[0])
    out: list[tuple[str, str]] = []
    for _, tf in ranked[:max_blocks]:
        try:
            out.append((str(tf), tf.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _extract_body_response(response: str) -> str | None:
    """Pull a function body out of the LLM response.

    Accepts (in order):
      - `<BODY> ... </BODY>` block
      - ```rust ... ``` fenced block
      - ``` ... ``` fenced block
      - Raw response (last resort)

    Returns the trimmed text, or None on empty.
    """
    if not response or not response.strip():
        return None
    m = re.search(r"<BODY>(.*?)</BODY>", response, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        return candidate or None
    for fence in (r"```rust\s*(.*?)```", r"```(?:rs)?\s*(.*?)```"):
        m = re.search(fence, response, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
    return response.strip() or None


# Matches the start of an attribute: `#[`, `#![`, or any of those with
# Rust-legal whitespace between the tokens (the baked skeleton emits the
# spaced token form `# ! [ doc = ... ]`). Group 1 is the optional `!`.
_ATTR_OPENER_RE = re.compile(r'#\s*(!?)\s*\[')


def _attr_span_end(text: str, open_idx: int) -> int:
    """Given `open_idx` = index of the `[` that opens an attribute, return the
    index just past the matching `]`. Bracket depth is tracked ONLY outside
    string literals, so `[`/`]` inside a doc string (markdown links, badges,
    embedded code) do not corrupt the count. Returns -1 if unbalanced.

    Handles normal (`"..."`, with `\\"` escapes) and raw (`r"..."`, `r#"..."#`)
    string literals — the forms that appear inside `#[doc = ...]`.
    """
    n = len(text)
    i = open_idx
    depth = 0
    while i < n:
        c = text[i]
        if c == '"':
            # Possible raw string: count preceding `r` + `#`s.
            hashes = 0
            j = i - 1
            while j >= 0 and text[j] == '#':
                hashes += 1
                j -= 1
            is_raw = j >= 0 and text[j] == 'r'
            i += 1
            if is_raw:
                closer = '"' + ('#' * hashes)
                end = text.find(closer, i)
                i = n if end == -1 else end + len(closer)
            else:
                while i < n:
                    if text[i] == '\\':
                        i += 2
                        continue
                    if text[i] == '"':
                        i += 1
                        break
                    i += 1
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _strip_attrs_for_view(text: str, *, bang: bool, doc_only: bool) -> str:
    """Remove attributes from the PROMPT view only (on-disk file is untouched).

    `bang=True`  → strip inner `#![...]` attributes (crate-level).
    `bang=False` + `doc_only=True` → strip outer `#[doc...]` attributes.

    Attribute spans are located string-literal-aware via `_attr_span_end`, so
    `[`/`]` inside doc content can never desync the parser (the bug that leaked
    bare doc text into the view). The trailing newline after a stripped
    attribute is swallowed so no blank line is left behind.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '#':
            m = _ATTR_OPENER_RE.match(text, i)
            if m:
                is_bang = m.group(1) == '!'
                open_idx = m.end() - 1  # index of the `[`
                end = _attr_span_end(text, open_idx)
                if end != -1:
                    inner = text[open_idx + 1:end - 1].lstrip()
                    want = (is_bang if bang
                            else (not is_bang and (not doc_only
                                                   or inner.startswith('doc'))))
                    if want:
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
        out.append(text[i])
        i += 1
    return ''.join(out)


def _strip_doc_attrs(text: str) -> str:
    """Strip `#[doc = "..."]` attributes from the file view shown to the LLM.

    anyhow's source embeds hundreds of chars of example code (with `[]`
    markdown and embedded newlines) in `#[doc = "..."]` per public fn. They
    eat prompt budget and don't help fill a body. Stripped from the PROMPT
    view only — the on-disk file keeps them so `cargo doc` still works.
    """
    return _strip_attrs_for_view(text, bang=False, doc_only=True)


def _strip_crate_attrs(text: str) -> str:
    """Strip inner `#![...]` crate attributes from the file view shown to the
    LLM (including the multi-line `#![doc = "..."]` module doc). PROMPT view
    only — the on-disk file keeps them so cargo builds correctly.
    """
    return _strip_attrs_for_view(text, bang=True, doc_only=False)


def _doc_attr_string(attr_inner: str) -> "str | None":
    """Given the text inside an attribute's brackets (e.g. `doc = " summary"`),
    return the doc string content, or None if this is not a `doc = "..."` attr.
    `#[doc(hidden)]` and other non-string doc attrs return "" (drop, no text)."""
    m = re.match(r'doc\s*=\s*', attr_inner)
    if not m:
        return None
    rest = attr_inner[m.end():].lstrip()
    if not rest.startswith('"'):
        return ""  # doc(hidden) / doc(alias=...) etc — no prose to keep
    body = rest[1:]
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == '\\' and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
            continue
        if body[i] == '"':
            break
        out.append(body[i])
        i += 1
    return ''.join(out)


def _condense_doc_run(contents: "list[str]") -> str:
    """Condense a run of consecutive doc-attr strings to a one-line summary:
    first sentence of prose, with fenced code, badges, and link-refs dropped
    and inline `[text](url)` reduced to `text`. Empty when the doc is all
    examples/badges (nothing worth a line)."""
    summary: list[str] = []
    for raw in contents:
        s = raw.strip()
        if s.startswith('```'):
            break                      # code example begins — stop the summary
        if not s:
            if summary:
                break                  # blank line after prose ends the summary
            continue
        if s.startswith('[![') or re.match(r'\[[^\]]+\]:', s):
            continue                   # badge / link reference definition
        summary.append(s)
        if '.' in s:
            break                      # sentence terminator reached
    text = ' '.join(summary).strip()
    if '.' in text:
        text = text[:text.index('.') + 1]
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)   # [t](url) -> t
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:200].rstrip()


def _condense_doc_attrs(text: str) -> str:
    """Condense (not delete) doc comments in the prompt view: each run of
    `#[doc=...]` / `#![doc=...]` attributes collapses to a single
    `// <intent>` (or `//! <intent>`) line. Non-doc `#![...]` crate attrs are
    dropped (they don't help fill a body). All other tokens pass through.

    Deterministic Python (Hard Rule 9) — preserves the author's intent overview
    at ~1 line instead of either dumping hundreds of chars of examples or
    erasing the intent entirely (FR-026). String-literal-aware via
    `_attr_span_end`, so `[]` inside doc content can never desync the parse.
    """
    out: list[str] = []
    run: list[str] = []
    run_bang = False
    run_active = False

    def _flush() -> None:
        nonlocal run, run_active, run_bang
        if run_active:
            s = _condense_doc_run(run)
            if s:
                out.append(('//! ' if run_bang else '// ') + s + '\n')
            run = []
            run_active = False
            run_bang = False

    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '#':
            m = _ATTR_OPENER_RE.match(text, i)
            if m:
                is_bang = m.group(1) == '!'
                open_idx = m.end() - 1
                end = _attr_span_end(text, open_idx)
                if end != -1:
                    inner = text[open_idx + 1:end - 1].strip()
                    doc_str = _doc_attr_string(inner)
                    if doc_str is not None:           # a doc attr
                        if not run_active:
                            run_active = True
                            run_bang = is_bang
                        if doc_str:
                            run.append(doc_str)
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
                    if is_bang:                       # non-doc crate attr → drop
                        _flush()
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
        if not c.isspace():
            _flush()
        out.append(c)
        i += 1
    _flush()
    return ''.join(out)


# Below this file-view size, keep doc comments in FULL (their usage examples are
# concrete guidance). Above it, condense to one-line intents to protect budget.
# ~1/6 of a single call → only the few genuinely huge files (e.g. error.rs) get
# condensed; the many small/medium files keep their examples.
_DOC_KEEP_FULL_LIMIT = SINGLE_CALL_CHAR_LIMIT // 6


def _annotate_file_view(file_text: str, target_fn: str) -> str:
    """Return a view of file_text where every unimplemented stub is prefixed
    with a comment clarifying its status. The TARGET fn (the one being filled
    in this call) is marked distinctly. Helps the model reason about which
    fns are "to-be-filled later" vs the immediate target, instead of treating
    the stubs as permanent missing pieces it must work around.

    Doc comments are kept in FULL when the view fits comfortably in budget —
    the crate's own usage examples are concrete WRONG/RIGHT guidance the model
    learns from, so we never strip them from a small prompt (FR-026 amended).
    Only when the file view is large enough to threaten the context budget do
    we CONDENSE docs to a one-line intent. Non-doc crate attrs are always
    dropped. PROMPT view only — the on-disk file is untouched.
    """
    # Keep examples when there's room; condense only big files.
    if len(file_text) > _DOC_KEEP_FULL_LIMIT:
        file_text = _condense_doc_attrs(file_text)
    else:
        file_text = _strip_crate_attrs(file_text)   # drop only #![...] crate attrs
    out: list[str] = []
    last_fn_end = 0
    for h in fn_headers(file_text, line_anchored=False):
        fn_name = h.name
        sig_start = h.start
        body_start = h.brace_open + 1
        depth = 1
        j = body_start
        while j < len(file_text) and depth > 0:
            c = file_text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth != 0:
            continue
        body_end = j - 1
        body = file_text[body_start:body_end]
        is_stub = _is_unimplemented_body(body)
        # Find the indent of the fn's first line.
        line_start = file_text.rfind("\n", 0, sig_start) + 1
        indent = ""
        for ch in file_text[line_start:sig_start]:
            if ch in (" ", "\t"):
                indent += ch
            else:
                break
        # Emit everything before this fn from where we left off.
        out.append(file_text[last_fn_end:sig_start])
        if fn_name == target_fn:
            annot = (f"{indent}// >>> TARGET — implement THIS function below; the body is "
                     f"currently `unimplemented!()`. <<<\n")
        elif is_stub:
            annot = (f"{indent}// SKELETON STUB — body will be filled in a separate "
                     f"call; you may rely on it existing (signature is fixed).\n")
        else:
            annot = ""
        out.append(annot)
        out.append(file_text[sig_start:j])
        last_fn_end = j
    out.append(file_text[last_fn_end:])
    return "".join(out)


def _enumerate_macro_variants(
    src_dir: Path,
) -> list[tuple[str, str, str, str]]:
    """Scan all .rs files under `src_dir` for `macro_rules!` blocks and
    return a list of `(macro_name, cfg_pred, body, source_file)` tuples.

    `cfg_pred` is the predicate of the `#[cfg(...)]` attribute immediately
    preceding the macro (empty string if no cfg gate). `body` is the full
    `macro_rules! NAME { ... }` text. Used by
    `_compute_cluster_macro_invariants` to decide which variant of a
    multiply-defined macro is active under a given cluster.
    """
    variants: list[tuple[str, str, str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for md in macro_definitions(text):
            if not md.balanced:  # original `if depth != 0: continue`
                continue
            name = md.name
            start = md.decl_start
            # Walk backwards from the macro to find a leading #[cfg(...)]
            # attribute on the same item. Stop at a blank line or another
            # item — only attributes that immediately precede the macro
            # gate it.
            head = text[max(0, start - 800):start]
            cfg_pred = ""
            for am in reversed(list(re.finditer(
                    r"#\[cfg\(((?:[^()]|\([^()]*\))+)\)\]\s*$",
                    head, re.MULTILINE))):
                cfg_pred = am.group(1).strip()
                break
            variants.append((name, cfg_pred, text[start:md.body_close + 1], p.name))
    return variants


def _detect_crate_no_std_invariant(src_dir: Path) -> str:
    """Return a no_std-aware invariant string if the crate's lib.rs
    declares `#![no_std]`. Otherwise empty.

    Crate-level rather than cluster-level: anyhow is `#![no_std]` by
    default with `extern crate std` conditionally enabled via the
    `std` feature. Even with `std` enabled, `format!`, `vec!`, etc.
    are NOT auto-imported in a no_std crate root — they must be
    pulled in explicitly. The model trips over this every time.
    """
    lib_rs = src_dir / "src" / "lib.rs" if (src_dir / "src").exists() else src_dir / "lib.rs"
    if not lib_rs.exists():
        return ""
    try:
        head = lib_rs.read_text(encoding="utf-8")[:4000]
    except Exception:
        return ""
    if not has_no_std_attr(head):
        return ""
    return (
        "crate is #![no_std]. `format!`, `vec!`, `print!`, `println!` "
        "are NOT in scope by default — even when `feature = \"std\"` is "
        "enabled. If you need to produce a String, prefer passing the "
        "Display value directly (most APIs take `M: Display` and don't "
        "need a String at all). Only if you genuinely need allocation, "
        "use `alloc::format!` after `use alloc::format;` or "
        "`crate::__private::format` if the crate provides it."
    )


def _compute_cluster_macro_invariants(
    src_dir: Path, cluster: "BuildCluster | None",
) -> str:
    """For macros defined multiple times under different `#[cfg(...)]`,
    select the variant active under `cluster`'s cfg-set and render a
    compact "in this cluster, NAME!(...) expands to: <body>" line per
    macro. Macros with a single variant are skipped (no ambiguity).

    Without this section the model has to read every `#[cfg(...)]`
    variant in the MACROS block and reason about which one applies —
    leading to defensive fills (e.g. `unwrap_or(&DEFAULT)`) when the
    active expansion in this cluster guarantees a `Some`.
    """
    if cluster is None:
        return ""
    variants = _enumerate_macro_variants(src_dir)
    if not variants:
        return ""
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for name, cfg, body, src in variants:
        by_name.setdefault(name, []).append((cfg, body, src))
    cfg_true = {_normalize_cfg_pred(p) for p in cluster.cfg_true}
    cfg_false = {_normalize_cfg_pred(p) for p in cluster.cfg_false}
    lines: list[str] = []
    for name in sorted(by_name):
        vs = by_name[name]
        if len(vs) < 2:
            continue
        # Pick the variant whose cfg evaluates True. A variant with no cfg
        # is treated as always-active fallback.
        active: tuple[str, str, str] | None = None
        unconditional: tuple[str, str, str] | None = None
        for cfg, body, src in vs:
            if not cfg:
                unconditional = (cfg, body, src)
                continue
            v = _eval_cfg_pred(cfg, cfg_true, cfg_false)
            if v is True:
                active = (cfg, body, src)
                break
        if active is None and unconditional is not None:
            active = unconditional
        if active is None:
            continue
        cfg, body, src = active
        # Render compactly: strip the `macro_rules! NAME {` header and
        # outer `}` so the model sees just the arm(s).
        compact = body
        defs = macro_definitions(compact)
        if defs:
            hb = defs[0].body_open + 1  # past the `{`, then the regex's trailing `\s*`
            while hb < len(compact) and compact[hb].isspace():
                hb += 1
            compact = compact[hb:].rstrip()
            if compact.endswith("}"):
                compact = compact[:-1].rstrip()
        # Collapse internal whitespace runs for compactness.
        compact = re.sub(r"\n\s*\n+", "\n", compact)
        if len(compact) > 240:
            compact = compact[:237] + "..."
        cfg_note = f" [cfg={cfg}]" if cfg else " [always]"
        lines.append(f"{name}!(...){cfg_note} → {compact}")
    if not lines:
        return ""
    header = (
        "Macros with multiple #[cfg]-gated variants — only the active "
        "expansion for this cluster is shown."
    )
    return header + "\n" + "\n".join(lines)


def _collect_crate_macros(src_dir: Path, max_chars: int = max(6_000, SINGLE_CALL_CHAR_LIMIT // 25)) -> str:  # Plan 9 F1: budget-derived (was 6000)
    """Extract `macro_rules!` blocks from every .rs file in `src_dir`.

    Returned as a compact section the model can scan to understand how
    user-facing macros invoke internal helpers (the most common reason a
    private fn has no direct test-name match).
    """
    macros: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for md in macro_definitions(text):
            if not md.balanced:  # original `if depth != 0: continue`
                continue
            # When `#[macro_export]` precedes the decl, include it in the block
            # (the old regex captured it via the optional leading group).
            block = text[md.export_start:md.body_close + 1]
            macros.append((p.name, f"// from {p.name}\n{block}"))
    if not macros:
        return ""
    joined = "\n\n".join(b for _, b in macros)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n... (macros truncated)"
    return joined


def _find_tests_for_fn(
    tests_dir_files: list[Path],
    fn_name: str,
    file_rel: str = "",
    src_dir: Path | None = None,
    max_blocks: int = 4,
    container: str = "",
) -> list[tuple[str, str]]:
    """Layered heuristic to find tests relevant to one fn.

    Order of attempts (each adds to the candidate set):
      0. Container-type filter (P5.4) — when fn_name is generic (`new`,
         `fmt`, `source`, etc.), match by the type name extracted from
         `container` rather than the fn name alone. Tests for
         `impl Display for ContextError::fmt` only count when they
         mention "ContextError".
      1. Direct name reference — tests calling `fn_name` literally.
      2. File-stem match — for fn in `src/ensure.rs`, include tests whose
         filename contains "ensure".
      3. Macro-aware — if `src_dir` is given and `file_rel`'s file defines
         macros, include tests that call those macros by name.

    Results ranked by relevance score (container > direct > stem > macro)
    then by hit count. Returns up to `max_blocks` (path, content) tuples.
    """
    if not fn_name and not file_rel:
        return []
    # Build candidate set with scores.
    scored: dict[Path, tuple[int, int]] = {}  # path → (tier, hits) — lower tier = better

    # P5.4 Tier 0: when fn_name is generic (very common: new, fmt, source,
    # clone, drop, default), filter by the container's TYPE NAME so we don't
    # match every test that mentions e.g. "new". Falls back to the regular
    # name-based search if the container doesn't yield a clear type name.
    _GENERIC_FN_NAMES = {
        "new", "fmt", "source", "clone", "default", "drop", "next",
        "from", "into", "deref", "deref_mut", "as_ref", "as_mut",
        "hash", "eq", "cmp", "partial_cmp", "from_str", "anyhow_kind",
        "provide",
    }
    container_type = ""
    if container:
        # "for X" of "impl Trait for X<...>", else "impl X<...>" (inherent).
        container_type = impl_self_type(container) or ""
    if fn_name in _GENERIC_FN_NAMES and container_type:
        for tf in tests_dir_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            n = count_word(txt, container_type)
            if n:
                cur = scored.get(tf, (99, 0))
                # Tier 0 = highest priority. Hit count = type mentions.
                scored[tf] = (0, max(cur[1], n))

    # Tier 1: direct name.
    if fn_name:
        for tf in tests_dir_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            n = count_word(txt, fn_name)
            if n:
                cur = scored.get(tf, (99, 0))
                scored[tf] = (min(cur[0], 1), max(cur[1], n))

    # Tier 2: file-stem match.
    if file_rel:
        stem = Path(file_rel).stem.lower()
        # Strip common prefixes/suffixes that don't help matching:
        stem = re.sub(r"^(mod|tests?)_", "", stem)
        for tf in tests_dir_files:
            tname = tf.stem.lower()
            if stem and (stem in tname or tname in stem):
                cur = scored.get(tf, (99, 0))
                scored[tf] = (min(cur[0], 2), max(cur[1], 1))

    # Tier 3: macro-aware.
    if file_rel and src_dir is not None:
        fp = src_dir / file_rel
        macro_names: set[str] = set()
        try:
            txt = fp.read_text(encoding="utf-8")
            for md in macro_definitions(txt):
                macro_names.add(md.name)
        except Exception:
            pass
        if macro_names:
            for tf in tests_dir_files:
                try:
                    ttxt = tf.read_text(encoding="utf-8")
                except Exception:
                    continue
                hits = sum(count_macro_calls(ttxt, n) for n in macro_names)
                if hits:
                    cur = scored.get(tf, (99, 0))
                    scored[tf] = (min(cur[0], 3), max(cur[1], hits))

    ranked = sorted(scored.items(), key=lambda kv: (kv[1][0], -kv[1][1]))[:max_blocks]
    out: list[tuple[str, str]] = []
    for path, _ in ranked:
        try:
            out.append((str(path), path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _leaf_first_fn_order(
    stubs: list[tuple[str, str]],
    name_counts: dict[str, int] | None,
) -> list[tuple[str, str]]:
    """Sort stubs so the most-referenced fns come first (likely call-graph
    leaves — utility fns relied on by many others). Public-API entry points
    end up last, where they can see real helper bodies on disk.

    `name_counts` is the `{fn_name: ref_count}` map written during the
    pre-strip bake step (`_bake_skeleton_artifacts`). When empty/None,
    falls back to deterministic order (file, fn_name).
    """
    if not name_counts:
        return sorted(stubs)
    scores: dict[tuple[str, str], int] = {}
    for s in stubs:
        _, fn = s
        scores[s] = name_counts.get(fn, 0) if fn else 0
    # Higher refs = more depended on = fill earlier (leaf-first).
    return sorted(stubs, key=lambda s: (-scores.get(s, 0), s[0], s[1]))


# ---------------------------------------------------------------------------
# Model self-correction notes — stored in functional_requirements_{crate}.md
# under the `## Model Notes — Confusions to Avoid` section. The MODEL writes
# entries in its own voice after fix loops; the next attempt reads them as
# part of the per-fn prompt. See CLAUDE.md "The model authors its own
# anti-hallucination notes".
# ---------------------------------------------------------------------------

_MODEL_NOTES_MAX_CHARS = 4000  # cap on the notes section in any one prompt
_MODEL_NOTES_MAX_ENTRIES = 80  # hard cap; older entries dropped LRU on overflow
# _MODEL_NOTES_HEADER / _slice_model_notes / _preserve_model_notes →
# pipeline_core.domain.model_notes (imported above). Parsing has no regex now.


def _load_model_notes(fr_path: Path) -> list[str]:
    """The note bullets currently in FR (thin: read file → domain parse)."""
    try:
        fr_text = fr_path.read_text(encoding="utf-8")
    except OSError:
        return []
    _, notes_section = _slice_model_notes(fr_text)
    return _parse_note_bullets(notes_section)


def _save_model_notes(fr_path: Path, notes: list[str]) -> None:
    """Rewrite the `## Model Notes` section in FR with the given bullets.
    Creates the FR file (with just the section) if it doesn't exist.
    """
    try:
        fr_text = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""
    except OSError:
        fr_text = ""
    out = _render_with_notes(fr_text, notes)
    try:
        fr_path.parent.mkdir(parents=True, exist_ok=True)
        fr_path.write_text(out, encoding="utf-8")
    except OSError as e:
        _log(f"  [model-notes] write failed: {e}")


def _filter_model_notes_for_prompt(
    fr_path: Path,
    file_rel: str,
    fn_name: str,
    file_text: str = "",
    current_error: str = "",
    max_chars: int = _MODEL_NOTES_MAX_CHARS,
) -> str:
    """Pull notes from FR ranked by relevance for the current (file, fn).

    P3.4 ranking tiers (lower number = higher priority):
      1. Note mentions an error code present in `current_error` (E0xxx).
      2. Note keyword overlap with `current_error`'s _note_signature keywords.
      3. Note mentions the file's basename or fn name.
      4. Note mentions any symbol (type/fn) appearing in `file_text`.
      5. Otherwise (recency only).

    Returns a single string ready to insert into a <MODEL_NOTES> block,
    capped at `max_chars`. Empty string if no notes apply.
    """
    notes = _load_model_notes(fr_path)
    if not notes:
        return ""
    base = Path(file_rel).stem if file_rel else ""
    # R7: item names via the syn-shaped token scan; error codes via the cargo-text
    # scan — both structured, replacing the on-source / on-output regexes.
    symbols: set[str] = set(item_names(file_text)) if file_text else set()
    err_codes: set[str] = set()
    err_keywords: set[str] = set()
    if current_error:
        err_codes = _ecodes_in_text(current_error)
        low_err = current_error.lower()
        err_keywords = {k for k in _NOTE_SIGNATURE_KEYWORDS if k in low_err}

    def tier(n: str) -> int:
        lower = n.lower()
        if err_codes and any(c in lower.upper() or c.lower() in lower
                              for c in err_codes):
            return 1
        n_codes, n_kw = _note_signature(n)
        if err_keywords and n_kw and len(err_keywords & n_kw) >= 2:
            return 2
        if (base and base.lower() in lower) or (fn_name and fn_name.lower() in lower):
            return 3
        if any(sym in n for sym in symbols):
            return 4
        return 5

    indexed = list(enumerate(notes))
    indexed.sort(key=lambda iv: (tier(iv[1]), -iv[0]))
    out_lines: list[str] = []
    total = 0
    for _, n in indexed:
        line = f"- {n}"
        if total + len(line) + 1 > max_chars:
            break
        out_lines.append(line)
        total += len(line) + 1
    return "\n".join(out_lines)




_NOTE_HASH_CACHE: dict[Path, set[str]] = {}

# Curated set of Rust-load-bearing keywords that, when co-mentioned in two
# notes, almost certainly mean the notes are about the same confusion.
# Used by _note_signature for semantic dedup (P3.3).
_NOTE_SIGNATURE_KEYWORDS = {
    "typeid", "'static", "static", "&dyn", "dyn", "box", "nonnull",
    "ptr", "as_ptr", "as_ref", "from_raw", "deref",
    "unimplemented", "todo", "unsafe",
    "trait", "impl", "generic", "lifetime", "sized", "unsized",
    "format", "display", "debug",
    "feature", "no_std", "core::", "std::",
    "ref::", "wrapper", "delegation",
}


def _note_hash(text: str) -> str:
    """Exact-text hash for early-rejection of literal duplicates."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


def _note_signature(text: str) -> tuple[frozenset, frozenset]:
    """Semantic signature: (error codes mentioned, keyword set).

    P3.3: two notes are considered semantically duplicate when their
    signatures' keyword sets overlap by >= 80%. Catches the 4-way
    paraphrase of "TypeId::of::<T>() needs T: 'static" we saw overnight,
    where exact-hash dedup let all four through.
    """
    low = text.lower()
    err_codes = frozenset(re.findall(r"\be\d{3,4}\b", low))
    keywords = frozenset(k for k in _NOTE_SIGNATURE_KEYWORDS if k in low)
    return err_codes, keywords


def _notes_are_semantic_dupes(a: str, b: str) -> bool:
    """True when two notes mostly mean the same thing.

    Two-tier check:
      - Same error code set (non-empty) → dupe regardless of phrasing.
      - Otherwise: keyword overlap >= 2 keywords AND >= 50% Jaccard.
        50% catches the overnight TypeId-paraphrase set where shared
        keywords are {typeid, static, generic} but each note adds one
        unique extra ("impl", "lifetime", etc.).
    """
    a_err, a_kw = _note_signature(a)
    b_err, b_kw = _note_signature(b)
    if a_err and b_err and a_err == b_err:
        return True
    if not a_kw or not b_kw:
        return False
    inter = len(a_kw & b_kw)
    union = len(a_kw | b_kw)
    if inter < 2:
        return False
    return union > 0 and inter / union >= 0.5


def _append_model_note(
    fr_path: Path,
    note: str,
    error_codes: "list[str] | set[str] | None" = None,
) -> bool:
    """Append `note` to FR's model-notes section. Returns True if added,
    False on dedup or empty/short input.

    Dedup logic (P3.3 tightened):
      1. Exact hash match → reject (literal duplicate)
      2. Semantic match against any existing note → reject
         (paraphrase of an already-known rule)

    P6.4: when `error_codes` is given, prefix the stored note with a
    `[E0xxx,E0yyy]` tag. This tag is readable to the LLM and lets us
    later surface past notes tied to recurring error codes alongside
    <RECURRING_ERRORS>.
    """
    note = (note or "").strip()
    if not note or len(note) < 20:
        return False
    if note.startswith("- "):
        note = note[2:].strip()
    # P6.4: prepend error-code tag (deduped, sorted, only well-formed codes).
    if error_codes:
        clean = sorted({c for c in error_codes
                        if isinstance(c, str) and re.fullmatch(r"E\d{3,4}", c)})
        if clean and not note.startswith("[E"):
            note = f"[{','.join(clean)}] {note}"
    existing = _load_model_notes(fr_path)
    existing_hashes = _NOTE_HASH_CACHE.get(fr_path)
    if existing_hashes is None:
        existing_hashes = {_note_hash(n) for n in existing}
        _NOTE_HASH_CACHE[fr_path] = existing_hashes
    h = _note_hash(note)
    if h in existing_hashes:
        return False
    # P3.3: semantic dedup. Walk existing notes; reject if any is a paraphrase.
    for prev in existing:
        if _notes_are_semantic_dupes(prev, note):
            return False
    existing.append(note)
    existing_hashes.add(h)
    if len(existing) > _MODEL_NOTES_MAX_ENTRIES:
        dropped = existing[:-_MODEL_NOTES_MAX_ENTRIES]
        existing = existing[-_MODEL_NOTES_MAX_ENTRIES:]
        for d in dropped:
            existing_hashes.discard(_note_hash(d))
    _save_model_notes(fr_path, existing)
    return True


_NOTE_EXTRACTION_PROMPT = """A model attempted to implement a Rust function and
the first attempt did not compile. The second (or later) attempt did compile.

Read the failed body, the cargo error from that failure, and the body that
finally compiled. Then write a SINGLE short note to your future self, in
first-person voice, that names the specific confusion and how to avoid it.

Format guidance:
  - Lead with what you tried that failed ("I tried X").
  - Name the reason it failed in concrete terms ("because Y").
  - State the correction as a rule ("Use Z, not X").
  - Keep it to ONE sentence; ≤300 chars.
  - Do NOT mention the function name, file name, or crate name — the note
    should generalise. Focus on the language/library/type confusion.
  - Output ONLY the note text, no preamble, no markdown, no quotes.

If no general lesson can be extracted (e.g. the failure was just a typo),
output the literal token NONE.

<FAILED_BODY>
{failed_body}
</FAILED_BODY>

<COMPILE_ERROR>
{error_excerpt}
</COMPILE_ERROR>

<FIXED_BODY>
{fixed_body}
</FIXED_BODY>
"""


def _extract_self_note(
    failed_body: str, error_excerpt: str, fixed_body: str,
) -> str | None:
    """One LLM call to author a self-correction note. Returns None when the
    model declines (responds with NONE or empty).
    """
    if not failed_body or not error_excerpt:
        return None
    prompt = _NOTE_EXTRACTION_PROMPT.format(
        failed_body=failed_body[:_FAILED_BODY_CAP],
        error_excerpt=error_excerpt[:_FIX_ERR_CAP],
        fixed_body=fixed_body[:_FAILED_BODY_CAP] if fixed_body else "(no fixed body — fix loop exhausted)",
    )
    # Two-attempt ladder for extraction: default (thinking on) then think=False
    # fallback. Mirrors the body-extraction recovery — Qwen3.6 occasionally
    # returns empty with think=False on small prompts and vice versa.
    resp = ""
    try:
        resp = ask(prompt, model=_TASK2_MODEL)
    except Exception as e:
        _log(f"  [model-notes] extraction failed (default): {e}")
    if not resp or not resp.strip():
        try:
            resp = ask(prompt, model=_TASK2_MODEL, think=False)
        except Exception as e:
            _log(f"  [model-notes] extraction failed (think=False): {e}")
            return None
    return parse_self_note(resp)




# P5.5: track Exxxx error codes seen across the current run. When a code
# fires 3+ times, auto-promote a short rule to every subsequent per-fn
# prompt (as a <RECURRING_ERRORS> block). Catches when one class of
# Rust gotcha keeps biting and one fix-loop's local note isn't enough.
from collections import Counter as _Counter_PY

_ERROR_CODE_COUNTS: "_Counter_PY | None" = None
# P6.3: per-error-code accumulator of cargo-error excerpts (capped). When
# an error code recurs, we mine these for common type tokens / file
# locations and surface them in <RECURRING_ERRORS>.
_ERROR_CODE_EXCERPTS: dict[str, list[str]] = {}
_ERROR_EXCERPT_MAX_PER_CODE = 10  # keep at most N excerpts per code
_ERROR_EXCERPT_MAX_CHARS = 600   # truncate each excerpt
_ERROR_CODE_PROMOTED_THRESHOLD = 3
_ERROR_CODE_HINTS: dict[str, str] = {
    "E0277": "trait bound not satisfied — TWO opposite cases (see the matching gotcha for concrete examples): (a) by-value return/use of a possibly-unsized `T` (\"size cannot be known\") → you MAY add `where T: Sized` to THIS one fn, or return `&T`/`Box<T>`; (b) `T doesn't implement Display/Debug/...` → you may NOT add that bound (it is the caller's contract) — do not call that trait on `T`, build the value another way.",
    "E0308": "mismatched types — check whether you're returning a Result vs Option, owned vs referenced, or wrong generic instantiation.",
    "E0382": "use of moved value — clone, borrow, or use Cow/Arc if you need both old and new value.",
    "E0507": "cannot move out of borrowed — use .clone(), .as_ref(), or destructure with `ref`.",
    "E0599": "no method found — likely calling a method that doesn't exist on this type. Read TYPES_IN_SCOPE for the exact method names.",
    "E0609": "no field — usually trying to access a method as if it were a field. Use .name() not .name. See TYPES_IN_SCOPE.",
    "E0615": "took value of method instead of calling it — `.method` vs `.method()`. Add parens.",
    "E0107": "wrong number of generic arguments — check the type's actual generic signature in TYPES_IN_SCOPE.",
    "E0310": "type parameter may not live long enough — add a `'static` bound.",
    "E0119": "conflicting impls — remove the duplicate or specialise via different generic params.",
    "E0554": "feature attribute only on nightly — guard with #[cfg] or check that the nightly toolchain is active.",
}


def _record_error_codes_seen(output: str) -> None:
    """Increment the global counter for each unique E0xxx code found in
    `output`. Also accumulate a short excerpt per code for later mining
    of recurring type-token context (P6.3)."""
    global _ERROR_CODE_COUNTS
    if _ERROR_CODE_COUNTS is None:
        _ERROR_CODE_COUNTS = _Counter_PY()
    if not output:
        return
    codes = _ecodes_in_text(output)
    for c in codes:
        _ERROR_CODE_COUNTS[c] += 1
        # Capture an excerpt: the error[Exxxx]: line + its few surrounding
        # lines. Per-code list is capped to N excerpts (FIFO).
        excerpt = _excerpt_for_code(output, c,
                                    max_chars=_ERROR_EXCERPT_MAX_CHARS)
        if excerpt:
            bucket = _ERROR_CODE_EXCERPTS.setdefault(c, [])
            bucket.append(excerpt)
            if len(bucket) > _ERROR_EXCERPT_MAX_PER_CODE:
                bucket.pop(0)


def _mine_common_tokens(excerpts: list[str], top_n: int = 3) -> list[tuple[str, int]]:
    """From a list of cargo error excerpts, return the most common Rust
    type-like tokens (CapitalisedCamelCase names, with possible generic
    decoration). Used to enrich RECURRING_ERRORS with type context.
    """
    if not excerpts:
        return []
    tok: _Counter_PY[str] = _Counter_PY()
    # Match `Foo`, `Foo<T>`, `Box<dyn StdError + Send + Sync>` etc.
    type_re = re.compile(
        r"\b([A-Z][A-Za-z0-9_]*(?:<[^<>\n]{1,60}>)?)\b"
    )
    # Generic noise that's NOT useful type context.
    skip = {
        "Err", "Ok", "None", "Some", "Box", "Self", "T", "E", "C", "V",
        "From", "Into", "Vec", "String", "Result", "Option",
    }
    for ex in excerpts:
        seen_here: set[str] = set()
        for m in type_re.finditer(ex):
            t = m.group(1)
            # Filter: must be longer than 3 chars OR clearly composite.
            bare = t.split("<", 1)[0]
            if bare in skip:
                continue
            if len(bare) < 4 and "<" not in t:
                continue
            if t in seen_here:
                continue  # one count per excerpt
            seen_here.add(t)
            tok[t] += 1
    return [(t, n) for t, n in tok.most_common(top_n) if n >= 2]


def _promoted_error_rules(fr_path: "Path | None" = None) -> str:
    """Return a compact block listing error codes that have recurred 3+
    times this run, with their canonical fix hint and (P6.3) any shared
    type-tokens mined from the error excerpts.

    P6.4: when `fr_path` is provided, scan stored model notes for
    `[E0xxx,...]` prefix tags and append up to 2 matching past notes
    per promoted code (most-recent first, deduped). This gives the LLM
    the exact rule it wrote for itself the previous time the same code
    fired."""
    if not _ERROR_CODE_COUNTS:
        return ""
    # Pre-build code → notes index from the FR file (P6.4).
    code_to_notes: dict[str, list[str]] = {}
    if fr_path is not None:
        try:
            stored = _load_model_notes(fr_path)
        except Exception:
            stored = []
        tag_re = re.compile(r"^\[((?:E\d{3,4})(?:,E\d{3,4})*)\]\s*(.+)$")
        # Walk newest-first so the most recent rule wins per code.
        for n in reversed(stored):
            m = tag_re.match(n)
            if not m:
                continue
            body = m.group(2).strip()
            for code in m.group(1).split(","):
                bucket = code_to_notes.setdefault(code, [])
                if body not in bucket:
                    bucket.append(body)

    promoted: list[str] = []
    for code, count in _ERROR_CODE_COUNTS.most_common():
        if count < _ERROR_CODE_PROMOTED_THRESHOLD:
            break
        hint = _ERROR_CODE_HINTS.get(code,
                                      f"recurring; rustc --explain {code} for details.")
        # P6.3: mine common type tokens from the excerpts for this code.
        common = _mine_common_tokens(_ERROR_CODE_EXCERPTS.get(code, []))
        if common:
            shared = ", ".join(f"`{t}`" for t, _ in common[:3])
            promoted.append(
                f"  {code} (seen {count}×, often on {shared}): {hint}")
        else:
            promoted.append(f"  {code} (seen {count}×): {hint}")
        # P6.4: surface past notes tagged with this code.
        for past in code_to_notes.get(code, [])[:2]:
            past_short = past if len(past) <= 240 else past[:237] + "..."
            promoted.append(f"    ↳ past note: {past_short}")
    if not promoted:
        return ""
    return ("These errors keep recurring in this run; address them proactively:\n"
            + "\n".join(promoted))


# Repository Specifications (2026-06-11): the crate's spec-document set —
# functional requirements + genesis (prose + structured rules) + API surface
# — injected into EVERY code-producing prompt from t2 onward via the prompts
# runtime facade. Loaded here (I/O + mtime cache), rendered by the pure
# pipeline_core.prompts.repo_spec selector.
_REPO_SPEC_CACHE: dict[str, tuple[tuple, RepoSpecDocs]] = {}
_FR_PREFIX = "functional_requirements_"


def _strip_model_notes_section(fr_text: str) -> str:
    """Drop '## Model Notes …' from the FR copy used for repo-spec excerpts —
    the notes already reach prompts via the dedicated MODEL_NOTES filter."""
    idx = fr_text.find("## Model Notes")
    if idx == -1:
        return fr_text
    nxt = fr_text.find("\n## ", idx + 1)
    return fr_text[:idx] + (fr_text[nxt + 1:] if nxt != -1 else "")


def _load_repo_spec_docs(project_dir: Path, module_name: str) -> RepoSpecDocs:
    genesis_p = project_dir / "genesis.md"
    fr_p = project_dir / f"{_FR_PREFIX}{module_name}.md"
    api_p = _api_surface_path(project_dir, module_name)
    mtimes = tuple(p.stat().st_mtime if p.exists() else 0.0
                   for p in (genesis_p, fr_p, api_p))
    key = str(project_dir / module_name)
    cached = _REPO_SPEC_CACHE.get(key)
    if cached and cached[0] == mtimes:
        return cached[1]
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            return ""
    docs = RepoSpecDocs(
        genesis_rules=genesis_rules_read(project_dir, module_scope=module_name or "*"),
        genesis=_read(genesis_p),
        functional_req=_strip_model_notes_section(_read(fr_p)),
        api_surface=_read(api_p),
    )
    _REPO_SPEC_CACHE[key] = (mtimes, docs)
    return docs


def _repo_spec_section(
    fr_path: "Path | None" = None, *, file_rel: str = "", fn_name: str = "",
    container: str = "", char_budget: "int | None" = None,
) -> str:
    """Provider behind prompts.runtime.repo_spec_section. Derives project_dir
    and module from fr_path (functional_requirements_{module}.md). Fail-safe:
    any problem → "" (prompt renders without the section)."""
    try:
        if fr_path is None:
            return ""
        fr_path = Path(fr_path)
        if not fr_path.name.startswith(_FR_PREFIX) or not fr_path.name.endswith(".md"):
            return ""
        module = fr_path.name[len(_FR_PREFIX):-3]
        docs = _load_repo_spec_docs(fr_path.parent, module)
        return render_repo_spec(
            docs, file_rel=file_rel,
            identifiers=identifiers_for_target(fn_name, container, file_rel),
            char_budget=char_budget or _REPO_SPEC_CAP,
        )
    except Exception:
        return ""


# Slice 8: the prompt builders moved to pipeline_core/prompts/ (pure
# text→text). Their impure collaborators stay here — the regex-based
# file-view annotator, the run-state promoted-error rules, and the
# repo-spec loader — and are wired into the prompts runtime facade once
# at import time.
_prompts_runtime.bind(_prompts_runtime.PromptRuntimeDeps(
    annotate_file_view=_annotate_file_view,
    promoted_error_rules=_promoted_error_rules,
    repo_spec_section=_repo_spec_section,
))


def _build_dispatch_family_section(family: DispatchFamily) -> str:
    """Shim: render via pipeline_core.prompts with the Settings-derived cap."""
    return _build_dispatch_family_section_pure(
        family, single_call_char_limit=SINGLE_CALL_CHAR_LIMIT)


_GRACEFUL_SENTINEL_PATH = Path("/tmp/pipeline_graceful_stop")
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

# P4: directory under each project workspace where Phase C test-fix
# iterations are archived for post-hoc retrospective. Per-iter file:
# tmp/{project}/.phase_c_logs/{cluster}/pass_{n}/fix_{iter}.txt
_PHASE_C_LOGS_DIRNAME = ".phase_c_logs"


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
        result = ask(prompt, model=model or _TASK2_MODEL,
                     think=True, max_tokens=_RETRO_MAX_TOKENS)
        if (result or "").strip():
            return result
    return result or ""


def _whole_tree_retrospective(
    error_excerpt: str,
    history: "list[dict]",
    *,
    name: str = "fix_loop",
) -> str:
    """Strategy retrospective for whole-tree fix loops. Mirrors the per-fn
    `_phase_c_retrospective` but at the tree-wide compile-error level.

    Called by `_improvement_loop` when the prior iter did not reduce the
    error floor. Asks the model: "you tried X and the error count stayed
    the same — what categorically different approach should the next
    attempt take?". Returns a short guidance string the next `do_fix`
    closure injects into its prompt.

    Empty string on transient failures (LLM exception, empty response).
    """
    if not error_excerpt.strip():
        return ""
    # Compact history view — the last 3 attempts' summaries and the error
    # signature they failed against.
    recent = history[-3:] if history else []
    history_block = ""
    for h in recent:
        summary = h.get("summary") or h.get("fix_summary") or ""
        err = (h.get("error_sig") or "")[:600]
        history_block += (
            f"\nIter {h.get('iter','?')}: {summary}\n"
            f"  metric_before={h.get('metric','?')} → "
            f"metric_after={h.get('post_fix_metric','?')}\n"
            f"  error_sig: {err}\n"
        )
    prior_retro = ""
    for h in reversed(recent):
        if h.get("retro"):
            prior_retro = h["retro"]
            break
    prior_block = (
        f"\nPrior retrospective said:\n{prior_retro}\n"
        "If that guidance did not move the error floor, escalate to a\n"
        "DIFFERENT strategy this time.\n"
    ) if prior_retro else ""
    prompt = (
        f"You are diagnosing why fix-loop `{name}` has stopped reducing\n"
        f"the rustc error count across multiple attempts.\n\n"
        f"Recent attempts:\n{history_block}\n"
        f"Current error excerpt:\n```\n{error_excerpt[:_FIX_ERR_CAP]}\n```\n"
        f"{prior_block}\n"
        "Output EXACTLY two lines:\n"
        "  CATEGORY: <one short tag — e.g. WRONG_FILE_TARGETED, "
        "TRAIT_HEADER_MISMATCH, UNSIZED_RETURN, DUPLICATE_IMPL, "
        "GENUINELY_HARD>\n"
        "  GUIDANCE: <one paragraph — what concretely different approach to "
        "try next iter. Reference specific symbols, fns, or rustc error codes.>\n\n"
        "GUIDANCE must be actionable, not 'try harder'. If two iters in a row\n"
        "with non-trivial strategy changes both failed to move the floor,\n"
        "set CATEGORY: GENUINELY_HARD."
    )
    try:
        result = _ask_retrospective(prompt)
    except Exception as e:
        _log(f"  [retro/{name}] LLM call failed: {e}")
        return ""
    if not (result or "").strip():
        _log(f"  [retro/{name}] LLM returned empty content")
        return ""
    # Lightweight parse: collect everything after "GUIDANCE:" up to next
    # uppercase-tag-style label. Tolerant of decorations.
    cat = ""
    guidance_lines: list[str] = []
    in_guidance = False
    for ln in (result or "").splitlines():
        s = ln.strip().lstrip("*-• ").strip()
        s = re.sub(r"^\*+\s*|\s*\*+$", "", s)
        m = re.match(r"(?i)^category\s*[:=]\s*(\S+)", s)
        if m:
            cat = m.group(1).strip().rstrip(".,;").upper()
            in_guidance = False
            continue
        m = re.match(r"(?i)^guidance\s*[:=]\s*(.+)$", s)
        if m:
            guidance_lines = [m.group(1).strip()]
            in_guidance = True
            continue
        if in_guidance and s:
            if re.match(r"(?i)^(category|guidance|note)\s*[:=]", s):
                in_guidance = False
            else:
                guidance_lines.append(s)
    guidance = " ".join(guidance_lines).strip()
    if not cat and not guidance:
        guidance = (result or "").strip()[:1200]
    if cat:
        return f"[{cat}] {guidance}" if guidance else f"[{cat}]"
    return guidance


def _write_phase_c_test_fix_log(
    project_dir: Path,
    cluster_name: str,
    pass_n: int,
    fix_iter: int,
    test_out: str,
    failed_tests: list[str],
    fns_retried: list[tuple[str, str]],
    cur_fail_count: int,
    new_fail_count: int,
    best_fail_count: int,
    stall: int,
) -> None:
    """Archive one test-fix iteration to disk for post-hoc analysis (P4).
    Failure to write is non-fatal: this is observability, not control flow.
    """
    try:
        out_dir = (project_dir / _PHASE_C_LOGS_DIRNAME
                   / cluster_name / f"pass_{pass_n}")
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"fix_{fix_iter:02d}.txt"
        header = (
            f"# Phase C test-fix iteration\n"
            f"cluster: {cluster_name}\n"
            f"pass:    {pass_n}\n"
            f"iter:    {fix_iter}\n"
            f"prev_fail_count: {cur_fail_count}\n"
            f"new_fail_count:  {new_fail_count}\n"
            f"best_fail_count: {best_fail_count}\n"
            f"stalled:         {stall}\n"
            f"failed_tests:\n"
            + "".join(f"  - {t}\n" for t in failed_tests)
            + "fns_retried:\n"
            + "".join(f"  - {r}::{f}\n" for r, f in fns_retried)
            + "\n=== cargo test output ===\n"
        )
        fp.write_text(header + test_out, encoding="utf-8")
    except Exception as e:
        _log(f"  [phase-c/per-fn] failed to write test-fix log: {e}")


# P3: classification step run before each post-stall test-fix iteration.
# A single LLM call labels each failing test into a small fixed taxonomy so
# the loop can target the right fn(s) instead of bluntly retrying everything
# that was filled this pass.

_PHASE_C_CLASSIFIER_CATEGORIES = (
    "BAD_FILL", "TEST_PRECONDITION_MISSING",
    "CFG_BOUNDARY", "PANIC_OR_TIMEOUT", "ENV_DEPENDENT",
)


def _classify_phase_c_test_failures(
    test_out: str,
    filled_this_pass: list[tuple[str, str]],
    failed_tests: list[str],
    cluster_name: str,
) -> dict[str, dict]:
    """Ask the LLM to classify each failing test. Returns
        {test_name: {"category": str, "fn": "file::name" | "", "reason": str}}.
    Empty dict on failure / no failing tests / classifier disabled.

    This is a single short LLM call; one inference for the whole set, not
    one per test. Output schema is strictly enforced by line-level parsing.
    Unknown categories or unparseable lines are dropped (silently — we'd
    rather fall back to the naive "retry everything" path than block the loop).
    """
    if not failed_tests:
        return {}
    if os.getenv("PHASE_C_TEST_FIX_USE_CLASSIFIER", "1") == "0":
        return {}

    # Budget the test_out excerpt: classifier needs to see error blocks but
    # has no need for the full multi-MB cargo output. Ctx-budget-aware (scales
    # with the model's context, floored for small windows) — not a frozen literal.
    excerpt = test_out[-_FIX_ERR_CAP:] if len(test_out) > _FIX_ERR_CAP else test_out

    filled_lines = "\n".join(f"  - {r}::{f}" for r, f in filled_this_pass[:60])
    failing_lines = "\n".join(f"  - {t}" for t in failed_tests[:30])

    prompt = (
        f"You are diagnosing a Rust test failure in cluster `{cluster_name}` "
        f"of a source-reconstruction pipeline. The fns listed below were filled "
        f"this pass; the tests listed below failed. For each failing test, "
        f"classify the most likely cause and (when applicable) name the fn that "
        f"should be re-implemented.\n\n"
        f"Categories:\n"
        f"  BAD_FILL                  — a fn just filled this pass has a wrong "
        f"body; name the fn (file::name).\n"
        f"  TEST_PRECONDITION_MISSING — the failing test depends on a DIFFERENT "
        f"fn that hasn't been filled correctly yet; name the suspect fn if you "
        f"can identify it from the error.\n"
        f"  CFG_BOUNDARY              — the failure points at code excluded "
        f"from this cluster's cfg-set (different cluster owns it).\n"
        f"  PANIC_OR_TIMEOUT          — the test compiled but panicked at runtime, "
        f"or timed out. Include the panic site/frame if visible.\n"
        f"  ENV_DEPENDENT             — failure stems from environment (missing "
        f"file, env var, network), not the fill body.\n\n"
        f"FNs filled this pass:\n{filled_lines}\n\n"
        f"Failing tests:\n{failing_lines}\n\n"
        f"Cargo test output (tail):\n```\n{excerpt}\n```\n\n"
        f"Output one line PER failing test, EXACT format:\n"
        f"  TEST <name>: <CATEGORY> [fn=<file::name>] — <one-sentence reason>\n"
        f"The fn= field is required for BAD_FILL and TEST_PRECONDITION_MISSING; "
        f"omit the [...] block for the other categories.\n"
        f"Do not output anything else."
    )

    try:
        # Use global MAX_TOKENS default — hardcoded caps risked content
        # truncation under tight thinking-budget conditions.
        result = ask(prompt, model=_TASK0C_MODEL, think=None)
    except Exception as e:
        _log(f"  [phase-c/classifier] LLM call failed: {e}")
        return {}

    out: dict[str, dict] = {}
    line_re = re.compile(
        r"^\s*TEST\s+(?P<test>\S+?)\s*:\s*"
        r"(?P<cat>[A-Z_]+)\s*"
        r"(?:\[fn=(?P<fn>[^\]]+)\])?\s*"
        r"(?:—|-)\s*"
        r"(?P<reason>.+?)\s*$"
    )
    for ln in result.splitlines():
        m = line_re.match(ln)
        if not m:
            continue
        cat = m.group("cat")
        if cat not in _PHASE_C_CLASSIFIER_CATEGORIES:
            continue
        out[m.group("test")] = {
            "category": cat,
            "fn": (m.group("fn") or "").strip(),
            "reason": m.group("reason").strip(),
        }
    return out


def _check_graceful_stop(context: str) -> None:
    """Exit cleanly when the graceful stop sentinel is set. Called from inside
    long-running loops so `bash continue.sh --graceful` is responsive."""
    if _GRACEFUL_SENTINEL_PATH.exists():
        with suppress(OSError):
            _GRACEFUL_SENTINEL_PATH.unlink()
        _log(f"  [graceful] stop sentinel detected at {context} — exiting cleanly.")
        sys.exit(0)


# Cargo diagnostic parsing relocated to application/reconstruct/cargo_errors.py
# (P2 slice 5, regex-free): the rendered-text error-signature fallback is now
# the structured `err_sig_from_text` scan (R7 standing rule).
# JSON-diagnostic cargo check relocated to application/reconstruct/cargo_runner.py
# (P2 slice 6): composes the Cargo PORT + diagnostics_from_json, per the port
# docstring ("JSON-diagnostic checks compose on top in the application layer").
import pipeline_core.application.reconstruct.cargo_runner as _reconstruct_cargo_runner  # noqa: E402
import pipeline_core.application.reconstruct.test_targets as _reconstruct_test_targets  # noqa: E402
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
    return _reconstruct_cargo_runner.run_check_diag(
        ADAPTERS.cargo, cmd, cwd, env, timeout)


# Cargo error-text shaping relocated to application/reconstruct/cargo_errors.py
# (P2 slice 4, regex-free-ified on the way per the R7 standing rule).
from pipeline_core.application.reconstruct.cargo_errors import (  # noqa: E402
    ecodes_in_text as _ecodes_in_text,
)
from pipeline_core.application.reconstruct.cargo_errors import (  # noqa: E402
    excerpt_for_code as _excerpt_for_code,
)
from pipeline_core.application.reconstruct.cargo_errors import (  # noqa: E402
    strip_cargo_warnings as _strip_cargo_warnings,
)


def _focus_cargo_errors(output: str, file_rel: str,
                        max_chars: "int | None" = None) -> str:
    return _reconstruct_cargo_errors.focus_cargo_errors(
        output, file_rel, max_chars if max_chars is not None else _FIX_ERR_CAP)


def _run_cargo(cmd: str, cwd: Path, env: dict, timeout: int = 180) -> tuple[int, str]:
    # Port-composed runner (P2 slice 8): see application/reconstruct/cargo_runner.run_cargo.
    return _reconstruct_cargo_runner.run_cargo(
        ADAPTERS.cargo, cmd, cwd, env, timeout)


# FR-024: every cargo test invocation must be wrapped in an improvement-driven
# loop. These helpers provide the metric (pass/fail counts), the attribution
# fallbacks (recently-filled queue, test-reverse map), and the stall budget.

def _build_test_reverse_map(
    test_files: list[Path],
    src_dir: Path,
) -> dict[str, set[tuple[str, str]]]:
    """P2: delegates to _attribution.build_test_reverse_map (logic relocated)."""
    return _attribution.build_test_reverse_map(_ATTR_DEPS, test_files, src_dir)


def _filter_test_targets(frontier: Frontier, src_dir: Path) -> Frontier:
    """Drop frontier keys that resolve to a `#[cfg(test)]`/`#[test]` fn (FR-018, #17)."""
    return _reconstruct_test_targets.filter_test_targets(
        frontier, _enumerate_fns_via_extractor(src_dir) or [], log=_log)


def _attribute_failures(
    test_output: str,
    src_dir: Path,
    recently_filled: list[tuple[str, str]],
    test_reverse_map: dict[str, set[tuple[str, str]]],
    cluster_name: str,
) -> tuple[Frontier, str]:
    """P2: delegates to _attribution.attribute_failures (logic relocated)."""
    return _attribution.attribute_failures(_ATTR_DEPS, test_output, src_dir, recently_filled, test_reverse_map, cluster_name)


def _run_scoped_tests(
    test_cmd: str, cwd: Path, env: dict, *, timeout: int = 120,
) -> "tuple[bool, int, str, set[str]]":
    # P2 slice 9: pass `_run_cargo` (the patchable seam) so the e2e/smoke
    # harness's `monkeypatch pipeline._run_cargo` still intercepts test runs.
    return _reconstruct_cargo_runner.run_scoped_tests(
        _run_cargo, test_cmd, cwd, env, timeout=timeout, err_cap=_FIX_ERR_CAP)


def _u2_test_gate(
    fn_tests: "set[str]", test_cmd: "str | None",
    target_src_dir: Path, cluster_env: "dict | None",
) -> "tuple[frozenset, str] | None":
    """Plan 10 — the SINGLE reusable test-acceptance gate, called by every fill
    loop (per-fn, per-impl, post-merge) at the point a unit compiles.

    Given the test names the unit must make pass (`fn_tests`), run them via the
    shared `_run_scoped_tests` and decide:
      - return None  → ACCEPT (tests pass, or U2 off, or no tests, or the test
        run was inconclusive — never block on uncertainty).
      - return (fail_sig, detail) → DO NOT ACCEPT: the unit compiled but its own
        tests still fail. `fail_sig` is the failing-test set (use as the stall
        signature so the fix loop terminates), `detail` is the assertion / panic
        text to feed back into the next prompt.
    Fail-safe: any error → None (fall back to compile-based acceptance).
    """
    # P2 slice 11: gate logic crossed to cargo_runner; shim injects the patchable
    # `_run_cargo` seam + `_guard` and the config (resolved at call time).
    return _reconstruct_cargo_runner.u2_test_gate(
        _run_cargo, fn_tests, test_cmd, target_src_dir, cluster_env,
        enabled=_UNIFIED_FILL_LOOP, timeout=_U2_TEST_TIMEOUT,
        err_cap=_FIX_ERR_CAP, guard=_guard)


def _build_test_cmd(test_stems: list[str], cluster: "BuildCluster | None") -> str:
    """Build `cargo test --no-fail-fast --test stem1 --test stem2 ...` honoring
    the cluster toolchain."""
    parts = ["cargo"]
    if cluster and cluster.toolchain:
        parts.append(f"+{cluster.toolchain}")
    parts += ["test", "--no-fail-fast"]
    for s in test_stems:
        parts += ["--test", s]
    return shlex.join(parts)


def _ask_per_fn_body(
    prompt: str, fn_name: str, attempts_log: list[str],
    sig_text: str = "",
) -> tuple[str | None, str]:
    """Run the 3-attempt response-extraction ladder for a per-fn prompt.

    Returns (body or None, last_response). On None, the model's response was
    unparseable or stub-only on all three tries.

    For `unsafe` fns (rigid pointer/vtable manipulation), drops sampling
    temperature to PHASE_C_UNSAFE_TEMP (default 0.1). High-temperature
    creative sampling is the wrong mode for unsafe code — there's
    typically one correct token sequence per body, not a range of valid
    creative choices. The Qwen3.6 model documentation explicitly
    recommends lowering temperature for syntactically rigid code.
    """
    # Detect unsafe-fn context from the signature.
    is_unsafe = bool(sig_text) and is_unsafe_fn(sig_text)
    unsafe_temp = float(os.getenv("PHASE_C_UNSAFE_TEMP", "0.1"))
    last_response = ""
    for think_off, label in [(False, "default"), (True, "think=False"),
                             (True, "think=False+retry")]:
        kwargs: dict = {"model": _TASK2_MODEL}
        if think_off:
            kwargs["think"] = False
        if is_unsafe:
            kwargs["temperature"] = unsafe_temp
        last_response = ask(prompt, **kwargs)
        body = _extract_body_response(last_response)
        if body and not _is_unimplemented_body(body):
            if label != "default":
                attempts_log.append(f"{fn_name}: recovered via {label}")
            return body, last_response
        attempts_log.append(f"{fn_name}: empty/stub from {label}")
    return None, last_response


def _collect_sibling_filled_bodies(
    file_text: str, target_fn_name: str, target_container: str,
    max_siblings: int = 3, max_body_chars: int = 800,
) -> str:
    """FR-024 fix 3: hoist already-filled sibling-impl bodies to the prompt.

    Finds every fn with the same `target_fn_name` in `file_text` whose body
    is NOT a stub, EXCLUDING the target itself (matched by container).
    Returns a formatted block ready for `<SIBLING_REFERENCE>` injection.
    Empty string when no usable siblings exist.

    The model is much more likely to produce a working body for an
    overloaded `fn as_ref` if a sibling `as_ref` impl in the same file
    is already correct — it can pattern-match the structural template.
    """
    out: list[str] = []
    count = 0
    for h in fn_headers(file_text, line_anchored=False):
        if h.name != target_fn_name:
            continue
        sig_start = h.start
        body_start = h.brace_open + 1
        depth = 1
        i = body_start
        while i < len(file_text) and depth > 0:
            ch = file_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        body_end = i - 1
        body = file_text[body_start:body_end].strip()
        if not body or _is_unimplemented_body(body):
            continue
        # Skip the target fn itself — heuristic by container substring match
        # since we don't have AST nodes here. The container fragment usually
        # appears in the ~200 chars above the fn declaration.
        if target_container:
            context_above = file_text[max(0, sig_start - 200):sig_start]
            if target_container.strip() in context_above:
                # Likely the target — skip.
                continue
        sig = file_text[sig_start:body_start].strip().rstrip("{").rstrip()
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n    /* ...truncated... */"
        out.append(f"{sig} {{\n{body}\n}}")
        count += 1
        if count >= max_siblings:
            break
    return "\n\n".join(out)


# Retrospectives store relocated to pipeline_core/adapters/retros.py (P2);
# aliases keep engine call sites + smoke pins unchanged.
from pipeline_core.adapters.retros import (  # noqa: E402
    canon_container as _canon_container,  # noqa: F401  (smoke pin: test_canon_container_unifies_renderings)
)
from pipeline_core.adapters.retros import (  # noqa: E402
    is_fn_fully_exhausted as _is_fn_fully_exhausted,
)
from pipeline_core.adapters.retros import (  # noqa: E402
    load_retrospectives as _load_retrospectives,
)
from pipeline_core.adapters.retros import (  # noqa: E402
    reset_exhaustion_flags as _reset_exhaustion_flags,
)
from pipeline_core.adapters.retros import (  # noqa: E402
    retro_key as _retro_key,
)
from pipeline_core.adapters.retros import (  # noqa: E402
    save_retrospectives as _save_retrospectives,
)


def _phase_c_retrospective(
    fn_name: str, sig_text: str, container: str, file_rel: str,
    cluster_name: str,
    last_body: str, error_excerpt: str,
    fix_attempts: int,
    prior_retrospective: str = "",
    sibling_bodies: str = "",
) -> dict:
    """Diagnose why the per-fn fix loop exhausted on this fn. Returns
    {category, guidance, signal} — a structured diagnosis used to shape
    the NEXT attempt's prompt with explicit "try X instead" hints rather
    than just retrying the same approach.

    Categories:
      WRONG_TYPE_CHOICE / WRONG_DEREF_PATTERN / MISSING_UNSAFE_PRIMITIVE
      TRAIT_BOUND_VIOLATION / API_HALLUCINATION / NEEDS_VTABLE_ACCESS
      GENUINELY_HARD

    'GENUINELY_HARD' signals the caller should mark the fn as
    fully-exhausted and skip on subsequent iters.
    """
    return _diagnose_phase_c(
        fn_name, sig_text, container, file_rel, cluster_name, last_body,
        error_excerpt, fix_attempts, _PHASE_C_RETRO_DEPS,
        prior_retrospective=prior_retrospective, sibling_bodies=sibling_bodies,
    )


_PHASE_C_RETRO_DEPS = PhaseCRetroDeps(
    ask_retrospective=_ask_retrospective, log=_log,
    failed_body_cap=_FAILED_BODY_CAP, fix_err_cap=_FIX_ERR_CAP,
)


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
    section = _filter_model_notes_for_prompt(
        fr_path, file_rel=rel, fn_name=fn,
        file_text="", current_error="",
    )
    return section.count("\n- ") if section else 0


def _record_tdd_regression(
    fr_path: Path,
    src_dir: Path,
    filled_keys: "list[tuple[str, str]]",
    broken_tests: "set[str]",
    prev_best: int,
    now_pass: int,
) -> str:
    """Hook 3 — turn a `14→11` pass-count regression into an ACTIONABLE note.

    For each fn filled in the pass that just broke previously-passing tests,
    write a concrete WRONG-body Model Note (file + fn + the broken test names +
    the body that was applied) into the FR Model Notes channel, which the next
    fill prompt for that fn injects automatically (relevance-ranked by file/fn).
    The bare metric is unactionable; the body+broken-tests pair is the WRONG
    example the model can react to.

    Best-effort: never raises into the TDD loop. Returns a compact note (e.g.
    "regressed t1, t2") to stamp on the LoopState trajectory, or "" if none.
    """
    try:
        recs = _enumerate_fns_via_extractor(src_dir) or []
    except Exception:
        recs = []
    by_name: dict[str, dict] = {}
    for r in recs:
        nm = r.get("fn_name", "")
        if nm and nm not in by_name:
            by_name[nm] = r
    broken = ", ".join(sorted(broken_tests)[:6])
    for key in filled_keys:
        fn = key[1] if isinstance(key, tuple) and len(key) > 1 else str(key)
        rec = by_name.get(fn)
        if not rec:
            continue
        file_rel = rec.get("file", "")
        body = (rec.get("body_text", "") or "").strip()
        note = (
            f"[regression] In {Path(file_rel).name or '?'}, the body just written for "
            f"`{fn}` made previously-passing test(s) FAIL: {broken} (cluster pass "
            f"count dropped {int(prev_best)}→{int(now_pass)}). WRONG (this body "
            f"broke those tests):\n{body[:_FAILED_BODY_CAP]}\nRe-implement `{fn}` so "
            f"{broken} keep passing; do not repeat that approach."
        )
        with suppress(Exception):
            _append_model_note(fr_path, note)
    return f"regressed {broken}" if broken else ""


def _run_phase_c_tdd(**kwargs):
    """P4: delegates to application.reconstruct.phase_c_tdd (logic relocated)."""
    return _phase_c_tdd.run_phase_c_tdd(_PHASE_C_TDD_DEPS, **kwargs)


_PHASE_C_TDD_DEPS = _phase_c_tdd.PhaseCTddDeps(
    attribute_failures=lambda *a, **k: _attribute_failures(*a, **k),
    build_test_reverse_map=lambda *a, **k: _build_test_reverse_map(*a, **k),
    filter_test_targets=lambda *a, **k: _filter_test_targets(*a, **k),
    check_graceful_stop=lambda *a, **k: _check_graceful_stop(*a, **k),
    converge_in_cluster=lambda *a, **k: _converge_in_cluster(*a, **k),
    count_relevant_notes=lambda *a, **k: _count_relevant_notes(*a, **k),
    effective_stalls=lambda *a, **k: _effective_stalls(*a, **k),
    emit=lambda *a, **k: _emit(*a, **k),
    log=lambda m: _log(m),
    order_frontier=lambda *a, **k: _order_frontier(*a, **k),
    record_tdd_regression=lambda *a, **k: _record_tdd_regression(*a, **k),
    run_cargo=lambda *a, **k: _run_cargo(*a, **k),
    scan_unimplemented_for_cluster=lambda *a, **k: _scan_unimplemented_for_cluster(*a, **k),
    scan_unimplemented_functions=lambda *a, **k: _scan_unimplemented_functions(*a, **k),
    task2_fill_fns_one_by_one=lambda *a, **k: _task2_fill_fns_one_by_one(*a, **k),
)


# Soft cap on stub members per impl block. Larger blocks fall back to the
# per-fn path so the output budget always fits. Anyhow's `impl Error` is
# the only block likely to exceed this in practice.
_PER_IMPL_MAX_STUBS = int(os.getenv("PHASE_C_IMPL_MAX_STUBS", "12"))

# Per-source-tree splice+cargo locks. Originally one global lock (hoisted
# from `_task2_fill_fns_one_by_one` because the per-impl path also needs to
# serialise splice + cargo against any other worker mutating the same
# cluster's source tree). With PIPELINE_PARALLELISM driving clusters
# concurrently the lock must be PER TREE: workers inside one cluster still
# serialise (deterministic file mutations; cargo's target/.cargo-lock blocks
# concurrent invocations in one dir anyway), while distinct clusters —
# distinct .skeleton-filled/{c}/ trees with their own target/ — proceed in
# parallel.
import threading as _threading_mod

_splice_locks: "dict[str, _threading_mod.Lock]" = {}
_splice_locks_guard = _threading_mod.Lock()


def _splice_lock_for(tree: Path) -> "_threading_mod.Lock":
    """The splice+cargo lock owned by `tree` (one per resolved source tree)."""
    key = str(Path(tree).resolve())
    with _splice_locks_guard:
        return _splice_locks.setdefault(key, _threading_mod.Lock())


# THE fix-loop driver — relocated to the application layer (P1) so task0c
# can drive it too; these aliases keep every call site + test pin unchanged.


def _phase_c_fill_impl_blocks(**kwargs):
    """P4: delegates to application.reconstruct.impl_fill (logic relocated)."""
    return _impl_fill.fill_impl_blocks(_IMPL_FILL_DEPS, **kwargs)


_IMPL_FILL_DEPS = _impl_fill.ImplFillDeps(
    build_dispatch_family_section=lambda *a, **k: _build_dispatch_family_section(*a, **k),
    build_per_impl_prompt=lambda *a, **k: _build_per_impl_prompt(*a, **k),
    cargo_env=lambda *a, **k: _cargo_env(*a, **k),
    cargo_err_sig=lambda *a, **k: _cargo_err_sig(*a, **k),
    cargo_err_sig_from_diagnostics=lambda *a, **k: _cargo_err_sig_from_diagnostics(*a, **k),
    check_graceful_stop=lambda *a, **k: _check_graceful_stop(*a, **k),
    cluster_filter_type_methods=lambda *a, **k: _cluster_filter_type_methods(*a, **k),
    collect_crate_macros=lambda *a, **k: _collect_crate_macros(*a, **k),
    collect_impl_blocks_for_cluster=lambda *a, **k: _collect_impl_blocks_for_cluster(*a, **k),
    collect_sibling_filled_bodies=lambda *a, **k: _collect_sibling_filled_bodies(*a, **k),
    compute_cluster_macro_invariants=lambda *a, **k: _compute_cluster_macro_invariants(*a, **k),
    detect_crate_no_std_invariant=lambda *a, **k: _detect_crate_no_std_invariant(*a, **k),
    dispatch_families_via_extractor=lambda *a, **k: _dispatch_families_via_extractor(*a, **k),
    effective_stalls=lambda *a, **k: _effective_stalls(*a, **k),
    emit=lambda *a, **k: _emit(*a, **k),
    enumerate_types_via_extractor=lambda *a, **k: _enumerate_types_via_extractor(*a, **k),
    extract_crate_idioms=lambda *a, **k: _extract_crate_idioms(*a, **k),
    extract_impl_where_clause=lambda *a, **k: _extract_impl_where_clause(*a, **k),
    filter_model_notes_for_prompt=lambda *a, **k: _filter_model_notes_for_prompt(*a, **k),
    find_tests_referencing_fn=lambda *a, **k: _find_tests_referencing_fn(*a, **k),
    focus_cargo_errors=lambda *a, **k: _focus_cargo_errors(*a, **k),
    frontier_from_diags=lambda *a, **k: _frontier_from_diags(*a, **k),
    impl_block_id=lambda *a, **k: _impl_block_id(*a, **k),
    is_fn_fully_exhausted=lambda *a, **k: _is_fn_fully_exhausted(*a, **k),
    load_retrospectives=lambda *a, **k: _load_retrospectives(*a, **k),
    log=lambda *a, **k: _log(*a, **k),
    module_of=lambda *a, **k: _module_of(*a, **k),
    phase_c_retrospective=lambda *a, **k: _phase_c_retrospective(*a, **k),
    record_error_codes_seen=lambda *a, **k: _record_error_codes_seen(*a, **k),
    retro_key=lambda *a, **k: _retro_key(*a, **k),
    run_cargo_check_diag=lambda *a, **k: _run_cargo_check_diag(*a, **k),
    save_retrospectives=lambda *a, **k: _save_retrospectives(*a, **k),
    scan_unimplemented_for_cluster=lambda *a, **k: _scan_unimplemented_for_cluster(*a, **k),
    scan_unimplemented_records_for_cluster=lambda *a, **k: _scan_unimplemented_records_for_cluster(*a, **k),
    splice_fn_with_where=lambda *a, **k: _splice_fn_with_where(*a, **k),
    splice_lock_for=lambda *a, **k: _splice_lock_for(*a, **k),
    types_section_for_fix=lambda *a, **k: _types_section_for_fix(*a, **k),
    u2_test_gate=lambda *a, **k: _u2_test_gate(*a, **k),
    ask=lambda *a, **k: ask(*a, **k),
    failed_body_cap=_FAILED_BODY_CAP,
    per_fn_fix_hard_cap=_PER_FN_FIX_HARD_CAP,
    per_impl_max_stubs=_PER_IMPL_MAX_STUBS,
    task2_model=_TASK2_MODEL,
)


import pipeline_core.application.reconstruct.cargo_errors as _reconstruct_cargo_errors  # noqa: E402
import pipeline_core.application.reconstruct.splice as _reconstruct_splice  # noqa: E402

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
from pipeline_core.domain.models import FnFixContext as _FnFixContext  # noqa: E402
from pipeline_core.prompts.per_fn import build_per_fn_prompt as _build_per_fn_prompt  # noqa: E402,F401

# P6: the `_PhaseCFillAdapter` god-object is dissolved — each fill-engine role is
# a focused adapter (adapters.fill_engine_roles), constructor-injected with
# late-binding lambdas over the resident engine helpers (so the scripted-cargo /
# scripted-model test monkeypatches still intercept at call time). The
# `WorkspaceModelAdapter` carries the P1 constructor-injected StructureReader; a
# test swaps in a fake via `_build_phase_c_engine(structure=fake)`.
_FILL_TEL = EngineTelemetryAdapter(
    log_fn=lambda msg: _log(msg),
    emit_fn=lambda event, **kw: _emit(event, **kw),
    check_stop_fn=lambda context: _check_graceful_stop(context),
)
_FILL_MUT = MutatorAdapter(
    locate_fn_by_record_fn=lambda fp, name, c, sig, cfgp=None: _locate_fn_by_record(fp, name, c, sig, cfgp),
    locate_fn_fn=lambda text, name: _locate_fn(text, name),
    splice_fn_body_at_fn=lambda fp, bs, be, nb, allow_nonstub=False: _splice_fn_body_at(fp, bs, be, nb, allow_nonstub=allow_nonstub),
    splice_fn_body_fn=lambda fp, name, nb, allow_nonstub=False: _splice_fn_body(fp, name, nb, allow_nonstub=allow_nonstub),
    replace_fn_signature_fn=lambda fp, name, sig: _replace_fn_signature(fp, name, sig),
    extract_signature_block_fn=lambda resp: _extract_signature_block(resp),
    lock_for_fn=lambda tree: _splice_lock_for(tree),
)
_FILL_CARGO = CargoFillAdapter(
    run_fn=lambda cmd, cwd, env, timeout=180: _run_cargo(cmd, cwd, env, timeout),
    build_test_cmd_fn=lambda stems, cluster: _build_test_cmd(stems, cluster),
    u2_test_gate_fn=lambda ft, tc, tsd, ce: _u2_test_gate(ft, tc, tsd, ce),
    focus_errors_fn=lambda out, fr, max_chars=None: _focus_cargo_errors(out, fr, max_chars=max_chars),
    env_fn=lambda: _cargo_env(),
)
_FILL_PROMPT = PromptContextAdapter(
    ask_per_fn_body_fn=lambda prompt, fn, alog, sig_text="": _ask_per_fn_body(prompt, fn, alog, sig_text=sig_text),
    gather_fn=lambda **kw: _gather_fn_fix_context(**kw),
    collect_sibling_filled_bodies_fn=lambda ft, tfn, tc: _collect_sibling_filled_bodies(ft, tfn, tc),
    extract_crate_idioms_fn=lambda ib, fr, target_sig_text="", max_idioms=5: _extract_crate_idioms(ib, fr, target_sig_text=target_sig_text, max_idioms=max_idioms),
    collect_crate_macros_fn=lambda sd: _collect_crate_macros(sd),
    compute_cluster_macro_invariants_fn=lambda sd, cluster: _compute_cluster_macro_invariants(sd, cluster),
    detect_crate_no_std_invariant_fn=lambda sd: _detect_crate_no_std_invariant(sd),
)
_FILL_WS = WorkspaceModelAdapter(
    structure=_STRUCTURE,
    scan_stub_records_fn=lambda tsd, cluster: _scan_unimplemented_records_for_cluster(tsd, cluster),
    scan_stub_tuples_fn=lambda tsd, cluster: _scan_unimplemented_for_cluster(tsd, cluster),
    collect_impl_blocks_fn=lambda sd: _collect_impl_blocks(sd),
    fn_container_index_fn=lambda sd: _fn_container_index(sd),
    fn_belongs_to_cluster_fn=lambda tsd, fr, fn, cluster, fn_cfg_predicates=None: _fn_belongs_to_cluster(tsd, fr, fn, cluster, fn_cfg_predicates=fn_cfg_predicates),
    cluster_filter_type_methods_fn=lambda recs, cluster: _cluster_filter_type_methods(recs, cluster),
    is_gen_tests_file_fn=lambda name: _is_gen_tests_file(name),
    leaf_first_fn_order_fn=lambda tuples, lnc: _leaf_first_fn_order(tuples, lnc),
)
_FILL_RETROS = RetrosAdapter(
    load_fn=lambda pd: _load_retrospectives(pd),
    save_fn=lambda pd, retros: _save_retrospectives(pd, retros),
    retro_key_fn=lambda fr, fn, c: _retro_key(fr, fn, c),
    is_fn_fully_exhausted_fn=lambda cache, fr, fn, c: _is_fn_fully_exhausted(cache, fr, fn, c),
    diagnose_fn=lambda **kw: _phase_c_retrospective(**kw),
    append_note_fn=lambda fr_path, note, error_codes=None: _append_model_note(fr_path, note, error_codes=error_codes),
    extract_self_note_fn=lambda fb, ee, fixed: _extract_self_note(fb, ee, fixed),
    record_error_codes_fn=lambda out: _record_error_codes_seen(out),
)
_FILL_TESTS = TestLinkerAdapter(
    find_tests_for_fn_fn=lambda tdf, fn, file_rel="", src_dir=None, container="": _find_tests_for_fn(tdf, fn, file_rel=file_rel, src_dir=src_dir, container=container),
    classify_failures_fn=lambda to, ftp, ft, cn: _classify_phase_c_test_failures(to, ftp, ft, cn),
    write_test_fix_log_fn=lambda **kw: _write_phase_c_test_fix_log(**kw),
)
_FILL_STALL = StallPolicyAdapter(
    effective_stalls_fn=lambda budget, module: _effective_stalls(budget, module),
    converge_in_cluster_fn=lambda cluster: _converge_in_cluster(cluster),
)
_FILL_IMPL = ImplFillPathAdapter(fill_impl_blocks_fn=lambda **kw: _phase_c_fill_impl_blocks(**kw))


def _build_phase_c_engine(*, structure: "_structure_port.StructureReader | None" = None) -> "PhaseCFillEngine":
    """Composition root: bind the concrete helpers to the fill-engine ports and
    assemble the use-cases. Built per call so monkeypatched SETTINGS / parallelism
    (tests) are honoured at call time. `structure` lets a test inject a fake
    StructureReader (P1 constructor-DI seam) instead of monkeypatching the
    enumerate globals — it is swapped into the WorkspaceModel adapter only."""
    ws = _FILL_WS if structure is None else dataclasses.replace(_FILL_WS, structure=structure)
    caps = FillCaps(
        per_fn_fix_hard_cap=_PER_FN_FIX_HARD_CAP,
        per_fn_fix_max_stalls=_PER_FN_FIX_MAX_STALLS,
        fix_err_cap=_FIX_ERR_CAP,
    )
    fill_one = FillOneFn(tel=_FILL_TEL, mut=_FILL_MUT, cargo=_FILL_CARGO, prompt=_FILL_PROMPT,
                         retros=_FILL_RETROS, tests=_FILL_TESTS, stall=_FILL_STALL, caps=caps)
    cfg = EngineConfig(
        parallelism=_pipeline_parallelism(),
        fill_unit=SETTINGS.phase_c_fill_unit,
        per_pass_test_fix_hard_cap=_PER_PASS_TEST_FIX_HARD_CAP,
        per_pass_test_fix_max_stalls=_PER_PASS_TEST_FIX_MAX_STALLS,
        pass_max_stalls=int(os.getenv("PHASE_C_PASS_MAX_STALLS", "2")),
        fix_err_cap=_FIX_ERR_CAP,
    )
    return PhaseCFillEngine(tel=_FILL_TEL, mut=_FILL_MUT, cargo=_FILL_CARGO, prompt=_FILL_PROMPT,
                            ws=ws, retros=_FILL_RETROS, tests=_FILL_TESTS, stall=_FILL_STALL,
                            impl_path=_FILL_IMPL, fill_one=fill_one, config=cfg)


def _task2_fill_fns_one_by_one(
    project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    module_name: str,
    cluster_name: str,
    cluster: "BuildCluster | None" = None,
    check_cmd: str = "cargo check",
    cluster_env: dict | None = None,
    max_passes: int = 3,
    leaf_name_counts: dict[str, int] | None = None,
    fr_path: Path | None = None,
    tdd_frontier_keys: "set[tuple[str, str]] | None" = None,
    skip_end_pass_test: bool = False,
    tdd_frontier_tests: "Frontier | None" = None,
    tdd_test_cmd: str | None = None,
    structure: "_structure_port.StructureReader | None" = None,
) -> dict:
    """Phase C — the per-cluster fill engine. Thin composition-root shim over
    `application.reconstruct.phase_c_engine.PhaseCFillEngine` (P2/R3.3): for each
    pass, scan stubs → leaf-first priority levels → worker pool (each worker
    drives `FillOneFn`) → per-pass cargo test + test-fix loop → pass-level
    floor-stall. `structure` is the P1 test-injection seam (fake StructureReader).
    Returns {'fns_filled','bytes_filled','attempts','errors',
    'remaining','impls_filled','impls_failed'}."""
    return _build_phase_c_engine(structure=structure).run(
        project_dir, target_src_dir, tests_dir_files, module_name, cluster_name,
        cluster=cluster, check_cmd=check_cmd, cluster_env=cluster_env,
        max_passes=max_passes, leaf_name_counts=leaf_name_counts, fr_path=fr_path,
        tdd_frontier_keys=tdd_frontier_keys, skip_end_pass_test=skip_end_pass_test,
        tdd_frontier_tests=tdd_frontier_tests, tdd_test_cmd=tdd_test_cmd,
    )


def _merge_fills_preserving_existing(disk_text: str, model_text: str) -> str:
    """Merge model-emitted file with the on-disk file.

    Strategy: disk_text is the structural base (skeleton + any prior fills
    + all imports/types/macros/attributes). We walk it function-by-function
    and ONLY replace `unimplemented!()` bodies with the corresponding body
    from model_text. Everything else from disk is preserved verbatim —
    imports, type definitions, module declarations, attributes, comments.

    This is the safe direction: the model can emit a thin file that drops
    the surrounding context, but we never lose what's already on disk.
    The model contributes function bodies only.

    Matching is by function signature line. If the model didn't return a
    given function (or returned it with `unimplemented!()` still), the disk
    body is kept — Phase D will pick up any residuals.
    """
    def extract_fns(text: str) -> list[tuple[str, str, int, int]]:
        """Return [(signature_line, body, body_start, body_end), ...] for each fn.

        body_start is the offset of the first char inside the opening `{`.
        body_end is the offset of the closing `}` itself (exclusive end of body).
        """
        out: list[tuple[str, str, int, int]] = []
        for h in fn_headers(text, line_anchored=False):
            body_start = h.brace_open + 1  # right after the opening `{`
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue  # malformed; skip
            body_end = i - 1  # position of the matching '}'
            body = text[body_start:body_end]
            sig_line = text[h.start:body_start].strip()
            out.append((sig_line, body, body_start, body_end))
        return out

    disk_fns = extract_fns(disk_text)
    if not disk_fns:
        # Disk has no functions to fill — either an empty or
        # non-Rust-source-ish file. Don't risk overwriting structure;
        # return disk verbatim.
        return disk_text

    # Build a map from signature → model's body, for any signature the
    # model returned that ALSO doesn't look like a fresh unimplemented stub.
    # (If the model regurgitated `unimplemented!()` for some function, that
    # adds no value over what's already on disk.)
    model_bodies: dict[str, str] = {}
    for sig, body, _bs, _be in extract_fns(model_text):
        if _is_unimplemented_body(body):
            continue
        model_bodies[sig] = body

    if not model_bodies:
        return disk_text  # model gave us nothing usable

    # Walk disk fns IN REVERSE so character offsets stay valid as we splice.
    # Replace body slice only when disk's body is unimplemented AND model
    # has a real body for the same signature.
    out = disk_text
    for sig, body, body_start, body_end in reversed(disk_fns):
        if not _is_unimplemented_body(body):
            continue  # already filled — never overwrite
        new_body = model_bodies.get(sig)
        if new_body is None:
            continue  # model didn't return this function
        out = out[:body_start] + new_body + out[body_end:]
    return out


def _task2_fill_groups(skeleton_dir, target_src_dir, groups, project_dir, **kwargs):
    """P4: delegates to application.reconstruct.group_fill (logic relocated)."""
    return _group_fill.task2_fill_groups(
        _GROUP_FILL_DEPS, skeleton_dir=skeleton_dir, target_src_dir=target_src_dir,
        groups=groups, project_dir=project_dir, **kwargs)


_GROUP_FILL_DEPS = _group_fill.GroupFillDeps(
    log=lambda m: _log(m),
    ask=lambda *a, **k: ask(*a, **k),
    extract_fn_blocks=lambda *a, **k: _extract_fn_blocks(*a, **k),
    format_skeleton_index_for_cluster=lambda *a, **k: _format_skeleton_index_for_cluster(*a, **k),
    leaf_first_group_order=lambda *a, **k: _leaf_first_group_order(*a, **k),
    load_skeleton_files=lambda *a, **k: _load_skeleton_files(*a, **k),
    merge_fn_blocks_into_disk=lambda *a, **k: _merge_fn_blocks_into_disk(*a, **k),
    scan_unimplemented_functions=lambda *a, **k: _scan_unimplemented_functions(*a, **k),
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    genesis_cap=_GENESIS_CAP,
    task2_model=_TASK2_MODEL,
)


def _cmd_phase_a_only(project_dir: Path) -> int:
    """Standalone Phase A: run the pre-strip skeleton bake on live source.

    Per Hard Rule 11, the bake MUST run while the original source is still
    on disk (i.e. before strip). This command refuses to run if `src/` is
    missing or empty — strip has already happened and there is nothing to
    extract from.

    Returns 0 on success, non-zero on any failure step.
    """
    _log(f"\n=== Phase A (skeleton bake) for {project_dir.name} ===")
    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        _log(f"  [phase-a] no source at {src_dir} — strip already ran. "
             "Cannot bake skeleton from absent source (Hard Rule 11).")
        return 2
    module_name = project_dir.name
    res = _bake_skeleton_artifacts(project_dir, module_name)
    if not res.get("ok"):
        _log(f"  [phase-a] bake FAILED: {res.get('reason', 'unknown')}")
        return 3
    _log(f"  [phase-a] baked {len(res['clusters'])} cluster(s): "
         f"{', '.join(res['clusters'])}")
    return 0


_SKELETON_EXTRACTOR_BIN = (
    Path(__file__).parent / "tools" / "skeleton_extractor"
    / "target" / "release" / "skeleton-extractor"
)


def _enumerate_types_via_extractor(src_dir: Path) -> list[TypeRecord] | None:
    """Invoke the syn-based extractor's --enumerate-types subcommand.

    Returns a list of type records (struct/enum/type/trait) with fields and
    method names. Used by per-fn prompts to inject type definitions when a
    fn's signature references the type (P5.1).

    Each record has: file, kind, name, generics, definition, fields[],
    variants[], methods[], is_pub.
    """
    return _STRUCTURE.enumerate_types(src_dir)  # slice 5a: via Extractor adapter


def _cluster_filter_type_methods(
    records: list[TypeRecord],
    cluster: "BuildCluster | None",
) -> list[TypeRecord]:
    """Drop methods whose cfg is excluded by `cluster` so TYPES_IN_SCOPE
    advertises only what compiles here (Hard Rule 8). A method is kept unless
    one of its cfg predicates evaluates definitively False; undecidable
    predicates are kept (conservative — matches _fn_belongs_to_cluster).
    Fields/variants carry no cfg today and pass through unchanged."""
    if cluster is None:
        return records
    cfg_true = set(cluster.cfg_true)
    cfg_false = set(cluster.cfg_false)
    out: list[TypeRecord] = []
    for r in records:
        kept = tuple(
            m for m in r.methods
            if not any(_eval_cfg_pred(p, cfg_true, cfg_false) is False
                       for p in m.cfg_predicates)
        )
        out.append(dataclasses.replace(r, methods=kept)
                   if len(kept) != len(r.methods) else r)
    return out




def _types_section_for_fix(
    sig_text: str,
    body_text: str,
    error_excerpt: str,
    container: str,
    impl_where: str,
    type_records: "list[TypeRecord]",
) -> str:
    """TYPES_IN_SCOPE for retry/fix prompts (P5.1 parity). Selects from
    everything that can name a type at fix time — the signature, the
    enclosing impl header (+ where clause), the CURRENT body (names the
    types the previous attempt actually manipulated), and the error
    text — then formats and caps like the initial-fill path."""
    search = "\n".join([
        sig_text or "", container or "", impl_where or "",
        body_text or "", error_excerpt or "",
    ])
    recs = _types_referenced_in_text(search, type_records)[:_TYPES_COUNT_CAP]
    full = "\n\n".join(_format_type_for_prompt(r) for r in recs)
    if len(full) > _TYPES_SECTION_CAP:  # Plan 9 F5: surface starvation
        _guard("phase-c/types_truncated",
               f"{(container or sig_text)[:50]}: type context {len(full):,} > "
               f"cap {_TYPES_SECTION_CAP:,} ({len(recs)} types)")
    return full[:_TYPES_SECTION_CAP]


def _fn_container_index(src_dir: Path) -> dict[tuple[str, str], str]:
    """(file_rel_to_src, fn_name) → container ("impl … for …" / "trait …" / "").

    Slice 8a (FnContext): the syn enumerator is the one source of truth for
    which impl owns a fn — every retry site needs this to resolve the
    self-type whose definition must be injected (the BoxedError gap). First
    record wins for duplicate names, matching the previous inline builders.
    """
    idx: dict[tuple[str, str], str] = {}
    for rec in (_enumerate_fns_via_extractor(src_dir) or []):
        idx.setdefault((rec.get("file", ""), rec.get("fn_name", "")),
                       rec.get("container", ""))
    return idx


def _gather_fn_fix_context(
    *,
    file_text: str,
    file_rel: str,
    fn_name: str,
    sig_text: str,
    body_text: str,
    error_excerpt: str,
    container: str,
    type_records: "list[TypeRecord]",
    fr_path: "Path | None" = None,
) -> _FnFixContext:
    """Assemble the fix-prompt context for one fn: impl where-clause from the
    file, TYPES_IN_SCOPE from everything that names a type at fix time, and
    the relevance-filtered model notes (empty when `fr_path` is None).

    Returns the domain `FnFixContext` directly (debt #13): this composition-root
    helper stays in pipeline.py — it's prompts-layer + fr_path I/O — but its
    result is the pure VO the fill use-cases consume, so the adapter no longer
    remaps a pipeline-local twin per attempt. One place still derives every
    injectable section, so a site can't silently omit one."""
    impl_where = _extract_impl_where_clause(file_text, container)
    types_section = _types_section_for_fix(
        sig_text, body_text, error_excerpt, container, impl_where,
        type_records)
    notes_section = ""
    if fr_path is not None:
        notes_section = _filter_model_notes_for_prompt(
            fr_path, file_rel=file_rel, fn_name=fn_name,
            file_text=file_text, current_error=error_excerpt,
        )
    return _FnFixContext(
        container=container, impl_where=impl_where,
        types_section=types_section, notes_section=notes_section,
    )


def _extract_impl_where_clause(file_text: str, container: str) -> str:
    """For a fn whose container is `impl ... for ...` or `impl ...`,
    locate the impl block in `file_text` and return its `where` clause
    (or empty string if absent / not found).

    The syn extractor records the container header but drops the
    `where` clause. Bounds like `where E: ext::StdError` are critical
    for picking the right TYPES_IN_SCOPE entries — they tell us which
    trait's methods are callable on the generic parameter.

    Thin shim over `domain.impl_block_ranges.impl_where_clause` (P7: the
    old regex locator was replaced by the comment/string-aware brace scanner).
    """
    return impl_where_clause(file_text, container)


def _types_referenced_in_text(text: str, all_types: list[TypeRecord]) -> list[TypeRecord]:
    """Return type records that the fn's prompt should expose, with one
    level of transitive expansion through fields.

    Pass 1: direct text match — types whose `name` appears in `text` as a
    standalone identifier. Picks up names mentioned in the fn signature
    or container.

    Pass 2: field-transitive expansion — for each type matched in pass 1,
    parse the type names out of its fields' Rust type strings and pull
    those records in too. This is what lets the model see `Own::by_ref()`
    when the only direct match was `Error` (whose field is
    `inner: Own<ErrorImpl>`). Without this step the model sees the field
    type as a bare name with no API and starts inventing methods.

    Word boundaries guard against `Boxed` matching when searching for
    `Box`. Field expansion is one level only — going deeper would bloat
    the prompt.
    """
    if not text or not all_types:
        return []
    by_name = {rec.name: rec for rec in all_types if rec.name}
    out: list[TypeRecord] = []
    seen_names: set[str] = set()
    for rec in all_types:
        name = rec.name
        if not name or name in seen_names:
            continue
        if contains_word(text, name):
            out.append(rec)
            seen_names.add(name)

    # Pass 2: expand by one field-hop. For each matched type, look at the
    # type names appearing in its fields and pull those records in.
    field_hop: list[TypeRecord] = []
    for rec in list(out):
        for f in rec.fields or ():
            ty = f.ty or ""
            if not ty:
                continue
            # Extract identifier-shaped tokens from the field's type
            # string. Tokens like `Own`, `ErrorImpl`, `Backtrace` get
            # added; generics and primitives are dropped via the
            # by_name lookup (we only keep names that match real records).
            for tok in identifier_tokens(ty):
                if tok in seen_names:
                    continue
                hop_rec = by_name.get(tok)
                if hop_rec is None:
                    continue
                field_hop.append(hop_rec)
                seen_names.add(tok)
    out.extend(field_hop)
    return out


def _enumerate_fns_via_extractor(src_dir: Path) -> list[dict] | None:
    """Fn wire-dicts for the not-yet-decomposed engine call sites (P2–P4 will route
    them through the injected `StructureReader`). Logic + dict shape: CliStructureReader."""
    return _STRUCTURE.enumerate_fns(src_dir)


def _dispatch_families_via_extractor(src_dir: Path) -> list[DispatchFamily] | None:
    """Invoke the extractor's --enumerate-dispatch-families subcommand.

    Returns a list of family records (or None if the binary is unavailable /
    fails). Each record: method (str), traits (list[str]), macros (list[str]),
    trait_files (list[str]), macro_defs (list[str]). A family is a set of 2+
    sibling traits sharing a method that a `macro_rules!` dispatches — anyhow's
    AdhocKind/TraitKind/BoxedKind (anyhow_kind, via anyhow!) being canonical.
    Dispatch correctness is a cross-impl invariant (FR-027), so the whole
    family is co-designed in one prompt context.
    """
    return _STRUCTURE.enumerate_dispatch_families(src_dir)  # slice 5a: via adapter


def _feature_gate_cfgs_via_extractor(src_dir: Path) -> list[str] | None:
    """Invoke the extractor's --enumerate-feature-gate-cfgs subcommand.

    Returns the cfg predicates that gate a nightly `#![feature(...)]` (via
    `#[cfg_attr(cond, feature(...))]`), or None if the binary is unavailable.
    A compiler cluster whose active cfg-set intersects this MUST compile with
    the nightly toolchain (FR-030) — running it on stable yields E0554.
    """
    return ADAPTERS.extractor.enumerate_feature_gate_cfgs(src_dir)  # slice 5a: via adapter




def _scan_unimplemented_records(src_dir: Path) -> list[dict] | None:
    """Return full FnRecord dicts (from the syn enumerator) for every fn whose
    body is still unimplemented. Returns None when the extractor binary isn't
    available — callers should fall back to _scan_unimplemented_functions.

    Records include `container` and `sig_text` for disambiguating same-named
    fns (Display::fmt vs Debug::fmt).
    """
    recs = _enumerate_fns_via_extractor(src_dir)
    if recs is None:
        return None
    return [r for r in recs if r.get("has_unimplemented_body")]


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


def _collect_impl_blocks(src_dir: Path) -> list[ImplBlock] | None:
    """Group fn records into impl/trait/mod containers. Thin shim over the
    relocated use-case (`application.reconstruct.impl_blocks`); the extractor
    seam is injected. Name kept for the monkeypatch test seam."""
    return _collect_impl_blocks_impl(src_dir, enumerate_fns=_enumerate_fns_via_extractor)


def _impl_block_id(file_rel: str, container: str) -> str:
    """Stable hashable id for an impl block (used as a dict key in the
    Phase C frontier mapping and as a per-block lock key)."""
    return f"{file_rel}::{container}"


# --- Crate-internal idiom extraction --------------------------------------
#
# When the model is filling a stub, the gotchas section provides GENERIC
# Rust patterns (NonNull deref, trait header signature matching, etc.). The
# generic patterns are necessary but insufficient: each crate develops its
# own internal idioms — e.g. anyhow's `Self::construct_from_*(arg, None)`
# family, its `ContextError { context, error }` wrapper, its vtable
# fn-pointer dispatch `(vtable.method)(this)` syntax. The model can only
# discover those by seeing them used in already-filled fns of the same
# file. `_extract_crate_idioms` scans the file's filled bodies, identifies
# the highest-signal idioms, and renders a compact `<CRATE_IDIOMS>`
# section the per-impl prompt injects so the model has a cheat-sheet of
# THIS crate's patterns alongside the generic gotchas.
#
# Generic over crates: there is no anyhow-specific code in here. The
# pattern detectors look for shapes (`Self::name(args)` recurrence,
# `(field.subfield)(args)` fn-pointer calls, `unsafe { x.as_ref() }`
# chains, `Struct { … }` literal construction). Anyhow happens to be a
# rich source of all of these; smaller crates emit fewer idioms and the
# section is correspondingly shorter.


_IDIOM_SELF_CALL_RE = re.compile(
    r"\bSelf::([A-Za-z_][A-Za-z_0-9]*)\s*\(([^()]{0,200}?)\)",
    re.DOTALL,
)
_IDIOM_FN_PTR_FIELD_RE = re.compile(
    r"\(\s*([A-Za-z_][\w.]+?)\s*\)\s*\(",
)
_IDIOM_STRUCT_LITERAL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]{2,40})\s*\{\s*([^{}]{1,400})\s*\}",
    re.DOTALL,
)
_IDIOM_UNSAFE_CHAIN_RE = re.compile(
    r"unsafe\s*\{\s*([^\n{}]{1,160})\s*\}",
)


def _extract_filled_fn_bodies(impl_blocks: "list[ImplBlock]", file_rel: str) -> "list[dict]":
    """Return [{fn_name, container, body}] for every FILLED fn in `file_rel`,
    pulled from the impl block records. Free fns are dropped (they have no
    container). Used by `_extract_crate_idioms` to scan for patterns."""
    out: list[dict] = []
    for b in impl_blocks:
        if b.file != file_rel:
            continue
        for m in b.members:
            if m.has_unimplemented_body:
                continue
            body = m.body_text.strip()
            if not body:
                continue
            out.append({
                "fn_name": m.fn_name,
                "container": b.container,
                "body": body,
            })
    return out


def _extract_crate_idioms(
    impl_blocks: "list[ImplBlock]",
    file_rel: str,
    target_sig_text: str = "",
    max_idioms: int = 5,
) -> str:
    """Walk filled fn bodies in `file_rel` and surface up to `max_idioms`
    distinctive patterns the model would benefit from seeing while filling
    a stub in the same file.

    The `_IDIOM_*` regex here mine recurring TEXT patterns (Self::call, struct literals,
    fn-ptr field calls) to build a heuristic cheat-sheet — NOT a no-regex-on-source violation:
    this is fuzzy pattern-mining, not structured-fact extraction (which goes through syn). See
    CODE_ARCHITECTURE.md §8 R7.5.

    Returned string is the body of a `<CRATE_IDIOMS>` section — empty
    when no patterns clear the relevance bar. Each idiom is one line:

        # Pattern label — used by `fn caller_name` in this file
        <one-line code snippet>

    Detection axes (each rendered at most twice):
      1. Self::method(arg, None) family — recurrent constructor pattern.
      2. (field.subfield)(args) — fn-pointer-via-field dispatch (vtable).
      3. unsafe { … } single-line chains — pointer / NonNull manipulation.
      4. StructName { field: …, … } — wrapper-type construction.

    Relevance: prefer idioms whose symbols (Self::method name, struct
    name, field name) appear in `target_sig_text` so the model sees
    crate-specific patterns it's about to need.
    """
    fns = _extract_filled_fn_bodies(impl_blocks, file_rel)
    if not fns:
        return ""

    target_tokens = set(identifier_tokens(target_sig_text or ""))

    # Per-axis collectors with rank score.
    candidates: list[tuple[float, str]] = []

    # Axis 1 — Self:: family. Count occurrences per method name.
    self_method_counts: dict[str, int] = {}
    self_method_examples: dict[str, tuple[str, str]] = {}  # name → (caller, snippet)
    for fn in fns:
        for m in _IDIOM_SELF_CALL_RE.finditer(fn["body"]):
            name = m.group(1)
            self_method_counts[name] = self_method_counts.get(name, 0) + 1
            self_method_examples.setdefault(name, (fn["fn_name"], m.group(0)))
    family_names = [n for n, c in self_method_counts.items() if c >= 2]
    if family_names:
        # One synthesised entry: "this file uses Self::X / Self::Y / Self::Z as constructors"
        family_list = ", ".join(f"Self::{n}" for n in sorted(family_names))
        # Show ONE concrete call from the highest-count family member.
        top = max(family_names, key=lambda n: self_method_counts[n])
        caller, snippet = self_method_examples[top]
        snippet = " ".join(snippet.split())  # collapse whitespace
        if len(snippet) > 140:
            snippet = snippet[:140] + " …"
        rel_score = 2.0 if any(n in target_tokens for n in family_names) else 1.0
        candidates.append((
            rel_score + 0.1 * sum(self_method_counts[n] for n in family_names),
            f"# Self:: family — this file's constructors: {family_list}.\n"
            f"# Example, from `fn {caller}`:\n"
            f"#   {snippet}"
        ))

    # Axis 2 — fn-pointer field dispatch `(x.y)(args)`. Group by the path.
    fnptr_counts: dict[str, int] = {}
    fnptr_examples: dict[str, tuple[str, str]] = {}
    for fn in fns:
        for m in _IDIOM_FN_PTR_FIELD_RE.finditer(fn["body"]):
            path = m.group(1).strip()
            if "." not in path:
                continue  # likely an arithmetic expression, not a vtable access
            base = path.rsplit(".", 1)[1]
            fnptr_counts[base] = fnptr_counts.get(base, 0) + 1
            fnptr_examples.setdefault(base, (fn["fn_name"], path))
    # Pick the 2 most frequent fn-pointer fields.
    for name in sorted(fnptr_counts, key=lambda n: -fnptr_counts[n])[:2]:
        if fnptr_counts[name] < 1:
            continue
        caller, path = fnptr_examples[name]
        rel_score = 2.0 if name in target_tokens else 1.0
        candidates.append((
            rel_score + 0.05 * fnptr_counts[name],
            f"# Fn-pointer field dispatch — call via the field, NOT via\n"
            f"# method syntax. Used in `fn {caller}`:\n"
            f"#   ({path})(args…)   // NOT  {path.split('.')[-1]}(args…)"
        ))

    # Axis 3 — unsafe { … } single-line chains. Surface the most common
    # SHAPE of such a chain (collapsed by token-pattern, not exact text).
    unsafe_examples: dict[str, tuple[str, str]] = {}
    unsafe_counts: dict[str, int] = {}
    for fn in fns:
        for m in _IDIOM_UNSAFE_CHAIN_RE.finditer(fn["body"]):
            chain = " ".join(m.group(1).split())
            if not chain:
                continue
            # Shape = identifier-only with `.`s preserved.
            shape = mask_identifiers(chain)
            shape = " ".join(shape.split())
            unsafe_counts[shape] = unsafe_counts.get(shape, 0) + 1
            unsafe_examples.setdefault(shape, (fn["fn_name"], chain))
    for shape in sorted(unsafe_counts, key=lambda s: -unsafe_counts[s])[:2]:
        if unsafe_counts[shape] < 1:
            continue
        caller, chain = unsafe_examples[shape]
        if len(chain) > 140:
            chain = chain[:140] + " …"
        rel_score = 1.0 + 0.05 * unsafe_counts[shape]
        # Bias if the chain mentions a target token (e.g. inner, vtable, ptr).
        for t in identifier_tokens(chain):
            if t in target_tokens:
                rel_score += 0.5
                break
        candidates.append((
            rel_score,
            f"# Unsafe chain pattern — used in `fn {caller}`:\n"
            f"#   unsafe {{ {chain} }}"
        ))

    # Axis 4 — Struct literal construction. Show wrapper types the file
    # constructs that aren't trivial single-field types.
    struct_examples: dict[str, tuple[str, str]] = {}
    struct_counts: dict[str, int] = {}
    for fn in fns:
        for m in _IDIOM_STRUCT_LITERAL_RE.finditer(fn["body"]):
            name = m.group(1)
            # Skip stdlib-y names that are noise.
            if name in {"Self", "Some", "None", "Ok", "Err", "Box", "Vec",
                        "String", "Result", "Option"}:
                continue
            inner = " ".join(m.group(2).split())
            if not inner or "," not in inner and ":" not in inner:
                continue
            struct_counts[name] = struct_counts.get(name, 0) + 1
            struct_examples.setdefault(name, (fn["fn_name"], inner))
    for name in sorted(struct_counts, key=lambda n: -struct_counts[n])[:2]:
        if struct_counts[name] < 1:
            continue
        caller, inner = struct_examples[name]
        if len(inner) > 140:
            inner = inner[:140] + " …"
        rel_score = 2.0 if name in target_tokens else 1.0
        candidates.append((
            rel_score + 0.05 * struct_counts[name],
            f"# Wrapper construction — used in `fn {caller}`:\n"
            f"#   {name} {{ {inner} }}"
        ))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    chosen = [snippet for _score, snippet in candidates[:max_idioms]]
    return "\n\n".join(chosen)


def _collect_impl_blocks_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[ImplBlock] | None:
    """Cluster-aware variant of `_collect_impl_blocks`. A block is included
    iff at least one of its STUB members is active under this cluster's
    cfg-set. Stub members not active in the cluster are dropped from
    `stub_members` so the per-impl prompt never asks the model to fill
    something that wouldn't compile in this cluster anyway."""
    blocks = _collect_impl_blocks(target_src_dir)
    if blocks is None:
        return None
    if cluster is None:
        return blocks
    out: list[ImplBlock] = []
    for b in blocks:
        kept_stubs = tuple(
            m for m in b.stub_members
            if _fn_belongs_to_cluster(
                target_src_dir, b.file, m.fn_name, cluster,
                fn_cfg_predicates=list(m.cfg_predicates))
        )
        if not kept_stubs:
            continue
        out.append(dataclasses.replace(b, stub_members=kept_stubs))
    return out


def _scan_unimplemented_records_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[dict] | None:
    """Cluster-aware variant of _scan_unimplemented_records.

    Returns records filtered to fns whose file/fn cfg evaluates true for
    `cluster`. Returns None if the extractor binary isn't available —
    callers should fall back to _scan_unimplemented_for_cluster (tuple form).
    """
    recs = _scan_unimplemented_records(target_src_dir)
    if recs is None:
        return None
    if cluster is None:
        return recs
    out: list[dict] = []
    skipped = 0
    for r in recs:
        if _fn_belongs_to_cluster(
                target_src_dir, r["file"], r["fn_name"], cluster,
                fn_cfg_predicates=list(r.get("cfg_predicates") or [])):
            out.append(r)
        else:
            skipped += 1
    if skipped:
        _log(f"  [phase-c/per-fn] cluster {cluster.name}: skipped "
             f"{skipped} stub(s) not active in this cluster's cfg-set.")
    return out


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


_TEST_STDOUT_HEADER_RE = re.compile(r"^----\s+(\S+)\s+stdout\s+----\s*$",
                                     re.MULTILINE)
def _line_to_fn_record(
    file_text: str, line_no: int, fn_records: list[dict],
    col_no: int | None = None,
) -> dict | None:
    """P2: delegates to _domain_attribution.line_to_fn_record (logic relocated)."""
    return _domain_attribution.line_to_fn_record(file_text, line_no, fn_records, col_no)


def _extract_tdd_frontier(
    test_output: str, src_dir: Path,
) -> Frontier:
    """P2: delegates to _attribution.extract_tdd_frontier (logic relocated)."""
    return _attribution.extract_tdd_frontier(_ATTR_DEPS, test_output, src_dir)


def _behavior_frontier(
    test_output: str, project_dir: Path, existing_test_files: set,
) -> "tuple[Frontier, dict[str, tuple[str, str]]]":
    """P2: delegates to _attribution.behavior_frontier (logic relocated)."""
    return _attribution.behavior_frontier(_ATTR_DEPS, test_output, project_dir, existing_test_files)


def _extract_tdd_compile_frontier(
    build_output: str, src_dir: Path,
) -> Frontier:
    """P2: delegates to _attribution.extract_tdd_compile_frontier (logic relocated)."""
    return _attribution.extract_tdd_compile_frontier(_ATTR_DEPS, build_output, src_dir)


def _compile_error_frontier(
    output: str, project_dir: Path, check_cmd: str,
    env: dict, timeout: int,
) -> Frontier:
    """P2: delegates to _attribution.compile_error_frontier (logic relocated)."""
    return _attribution.compile_error_frontier(_ATTR_DEPS, output, project_dir, check_cmd, env, timeout)


def _frontier_from_diags(
    diagnostics: list[Diagnostic], src_dir: Path,
) -> Frontier:
    """P2: delegates to _attribution.frontier_from_diags (logic relocated)."""
    return _attribution.frontier_from_diags(_ATTR_DEPS, diagnostics, src_dir)


# "cannot find …" diagnostics — an undefined value/fn/type/macro referenced but
# never defined. When several fns in one file hit these, they likely share
# internal machinery (a constructor, a const, a helper) that no single per-fn
# fill establishes — the vtable class (FR-040).
_CANNOT_FIND_CODES = {"E0425", "E0433", "E0412", "E0531", "E0405", "E0422", "E0423"}


def _coupled_cofill_groups(
    diagnostics: list[dict], src_dir: Path,
) -> list[dict]:
    """#1 (FR-040): detect co-fill groups — 2+ fns in the SAME file that fail
    with "cannot find" errors (undefined symbols). Filled per-fn they each
    invent the shared item under inconsistent names (`ADHOC_VTABLE` vs
    `VTABLE_ADHOC`); designed together the model can define it ONCE and
    reference it consistently. Returns [{file, fns:[...], symbols:[...]}], one
    per file with >= 2 such fns. Symbols are extracted from the diagnostic
    message between backticks (no regex)."""
    recs_by_file: dict[str, list[dict]] = {}
    text_cache: dict[str, str] = {}
    fn_syms: dict[str, dict[str, set]] = {}
    for d in diagnostics or []:
        if (d.get("code") or "") not in _CANNOT_FIND_CODES:
            continue
        rel = d.get("file") or ""
        line_no = int(d.get("line") or 0)
        msg = d.get("message") or ""
        if not rel or line_no < 1:
            continue
        sym = msg.split("`")[1] if msg.count("`") >= 2 else ""
        if rel not in recs_by_file:
            _all = _enumerate_fns_via_extractor(src_dir) or []
            recs_by_file[rel] = [r for r in _all if r.get("file") == rel]
        recs = recs_by_file[rel]
        if not recs:
            continue
        if rel not in text_cache:
            try:
                text_cache[rel] = (src_dir / rel).read_text(encoding="utf-8")
            except Exception:
                continue
        rec = _line_to_fn_record(text_cache[rel], line_no, recs)
        if rec is None:
            continue
        fn_syms.setdefault(rel, {}).setdefault(rec["fn_name"], set())
        if sym:
            fn_syms[rel][rec["fn_name"]].add(sym)
    groups: list[dict] = []
    for rel, fns in fn_syms.items():
        if len(fns) >= 2:
            syms = sorted({s for ss in fns.values() for s in ss})
            groups.append({"file": rel, "fns": sorted(fns), "symbols": syms})
    return groups


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Drop ANSI SGR colour escapes (cargo's rendered-ansi diagnostics)."""
    return _ANSI_RE.sub("", text or "")


def _order_frontier(
    frontier: Frontier,
    leaf_name_counts: dict[str, int] | None,
    panic_depths: dict[tuple[str, str], int] | None = None,
) -> list[tuple[tuple[str, str], set[str]]]:
    """Order frontier entries by D2's rule: shallowest panic depth first,
    then most-callers, then alphabetical (file, fn) for reproducibility.

    `panic_depths` (optional) maps the same keys to integer panic stack
    depths. Absent → all treated as same depth, the secondary sort wins.
    Most-referenced fn (from `leaf_name_counts`) breaks final ties so the
    pre-strip leaf-order metadata still influences scheduling.
    """
    panic_depths = panic_depths or {}
    leaf_name_counts = leaf_name_counts or {}
    items = list(frontier.items())

    def _key(item: tuple[tuple[str, str], set[str]]):
        (file_rel, fn_name), tests = item
        return (
            panic_depths.get((file_rel, fn_name), 0),  # shallowest first
            -len(tests),                                # most tests first
            -leaf_name_counts.get(fn_name, 0),          # most-referenced first
            file_rel, fn_name,                          # alphabetical reproducible
        )
    return sorted(items, key=_key)


def _scan_unimplemented_functions(src_dir: Path) -> list[tuple[str, str]]:
    """Return [(file_rel_path, fn_name), ...] for every fn whose body is still
    `unimplemented!()` (or an annotated stub).

    Prefers the syn-based extractor binary for robustness — it catches every
    fn rustc would consider (free fns, impl methods, trait method defaults,
    inline-compressed declarations, cfg-attributed fns). Falls back to a
    regex scanner when the binary isn't available.

    Note: this is the LEGACY name-only return form. New code should use
    `_scan_unimplemented_records` to also get container + signature, which
    are needed to disambiguate same-named fns within a single file.
    """
    recs = _enumerate_fns_via_extractor(src_dir)
    if recs is not None:
        return [(r["file"], r["fn_name"]) for r in recs
                if r.get("has_unimplemented_body")]
    # Fallback: best-effort regex scan. Less robust but doesn't require
    # the Rust binary; kept for the tests-fixture path and for portability.
    out: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for h in fn_headers(text, line_anchored=False):
            fn_name = h.name
            body_start = h.brace_open + 1
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            body = text[body_start:i - 1]
            if _is_unimplemented_body(body):
                out.append((str(p.relative_to(src_dir)), fn_name))
    return out


# ---------------------------------------------------------------------------
# Cluster-aware stub filter (P5 Step 1, focused fix). Filters stubs to only
# those whose enclosing cfg evaluates true for the current cluster. Skips
# fns that wouldn't compile in this cluster anyway (e.g., nightly.rs items
# being scanned in the stable cluster). Pure static analysis, no LLM.
# ---------------------------------------------------------------------------

_CFG_ATTR_RE = re.compile(r"^\s*#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]\s*$")
# Inline form: matches `#[cfg(X)]` even when not on its own line. Used when
# searching the lib.rs `mod foo;` declaration — the skeleton extractor
# formats attributes with spaces (`# [cfg (X)] mod foo ;`) and may keep
# them on the same line as the item.
_CFG_INLINE_RE = re.compile(r"#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]")


def _fn_cfg_predicates(file_text: str, fn_name: str) -> list[str]:
    """Return all `#[cfg(...)]` predicates that gate `fn_name` in `file_text`.

    FALLBACK ONLY (R7.4a): callers holding extractor records must pass the
    record's `cfg_predicates` to `_fn_belongs_to_cluster` instead — this
    regex's backward window (previous blank line → decl) can over-capture
    a PRECEDING fn's cfg attrs when fns are not blank-line separated, and
    it only ever finds the FIRST same-named fn in the file.

    Walks BACKWARD from the fn's declaration line, collecting consecutive
    `#[cfg(...)]` and `#[...]` attribute lines. Stops at the first
    non-attribute, non-blank line. Returns just the cfg predicates
    (the part inside `cfg(...)`).
    """
    fn_pat = re.compile(
        r"(?:^|\n)([ \t]*)(?:pub(?:\([^)]*\))?\s+)?"
        r"(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
        r"fn\s+" + re.escape(fn_name) + r"\b",
        re.MULTILINE,
    )
    m = fn_pat.search(file_text)
    if not m:
        return []
    decl_offset = m.start() + (1 if file_text[m.start()] == "\n" else 0)
    # Window: from the previous blank line (or start) to the fn decl.
    # Captures both same-line and above-line cfg attributes that the
    # skeleton extractor may emit (`# [cfg (X)] fn foo ...` or
    # `#[cfg(X)]\nfn foo`).
    ws = file_text.rfind("\n\n", 0, decl_offset)
    window_start = ws + 2 if ws >= 0 else 0
    window = file_text[window_start:decl_offset + 1]
    preds: list[str] = []
    for cm in _CFG_INLINE_RE.finditer(window):
        preds.append(cm.group(1).strip())
    return preds


def _enumerate_mods_via_extractor(src_dir: Path) -> list[ModRecord] | None:
    """Invoke the syn-based extractor's --enumerate-mods subcommand.

    Returns a list of mod records (each: {name, file, cfg_predicates,
    declared_in}) or None if the binary isn't available or fails.
    Replaces the regex-based scan of `mod foo;` declarations — syn parses
    Rust correctly and never conflates an adjacent mod's cfg with the
    target mod's cfg.
    """
    return _STRUCTURE.enumerate_mods(src_dir)  # slice 5a: via Extractor adapter


def _enumerate_consts_via_extractor(src_dir: Path) -> list[ConstRecord] | None:
    """Invoke the syn-based extractor's --enumerate-consts subcommand: pub/private
    `const` and `static` items (name, type, is_pub). Replaces regex scans of source
    for `pub const`/`pub static` (Hard Rule: no regex on Rust source — use syn)."""
    return _STRUCTURE.enumerate_consts(src_dir)


def _public_api_from_source(content: str) -> str:
    """Render a single Rust source string's public API surface using the syn extractor —
    no regex on Rust source (Hard Rule). The extractor is path-based, so write the string to a
    throwaway crate tree and enumerate types / fns / consts over it.

    Output mirrors the legacy regex format: `pub struct Name { f: T, ... }`, `pub enum Name
    { A, B }`, `pub trait Name`, full pub fn signatures, and `pub const NAME: T`.
    """
    import tempfile as _tempfile
    lines: list[str] = []
    with _tempfile.TemporaryDirectory() as _d:
        _src = Path(_d) / "src"
        _src.mkdir()
        (_src / "lib.rs").write_text(content, encoding="utf-8")
        _td = Path(_d)
        types = _enumerate_types_via_extractor(_td) or []
        fns = _enumerate_fns_via_extractor(_td) or []        # dicts (engine contract)
        consts = _enumerate_consts_via_extractor(_td) or []

    for t in types:
        if not t.is_pub:
            continue
        if t.kind == "struct":
            if t.fields:
                fields = ", ".join(f"{f.name}: {f.ty}" for f in t.fields)
                lines.append(f"pub struct {t.name}{t.generics} {{ {fields} }}")
            else:
                lines.append(f"pub struct {t.name}{t.generics}")
        elif t.kind == "enum":
            if t.variants:
                lines.append(f"pub enum {t.name}{t.generics} {{ {', '.join(t.variants)} }}")
            else:
                lines.append(f"pub enum {t.name}{t.generics}")
        elif t.kind == "trait":
            lines.append(f"pub trait {t.name}")

    for f in fns:
        if f.get("is_pub"):
            # sig_text starts at the `fn`/`unsafe`/`async` keyword (the extractor's sig span
            # excludes the visibility), so prepend `pub ` to match the API-listing convention.
            sig = " ".join((f.get("sig_text") or "").split())
            if sig:
                lines.append(sig if sig.startswith("pub ") else f"pub {sig}")

    for c in consts:
        if c.is_pub:
            lines.append(f"pub const {c.name}: {c.ty}")

    return "\n".join(lines)


# Cache of mod records per src_dir to avoid re-invoking the extractor for
# every fn lookup. Invalidated when target_src_dir's identity changes — the
# pipeline only ever queries one dir at a time during a cluster pass.
_MOD_RECORDS_CACHE: dict[str, list[ModRecord]] = {}


def _mod_records_cached(src_dir: Path) -> "list[ModRecord] | None":
    """ModRecords for `src_dir` via the syn extractor, cached per dir
    (see _MOD_RECORDS_CACHE). None when the extractor is unavailable."""
    key = str(src_dir.resolve())
    if key not in _MOD_RECORDS_CACHE:
        recs = _enumerate_mods_via_extractor(src_dir)
        if recs is None:
            return None
        _MOD_RECORDS_CACHE[key] = recs
    return _MOD_RECORDS_CACHE[key]


def _file_cfg_predicate(target_src_dir: Path, file_rel: str) -> str | None:
    """Return the cfg predicate (if any) under which `file_rel` is compiled.

    AST-derived via the syn-based skeleton extractor (`--enumerate-mods`).
    Returns None when the file is unconditionally included or when no
    parent mod decl is found.

    Falls back to the old regex-based scan when the extractor binary
    isn't available (test-fixture environments, partial setup, etc).
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    recs = _mod_records_cached(target_src_dir)
    if recs is None:
        return _file_cfg_predicate_regex_fallback(target_src_dir, file_rel)
    file_name = Path(file_rel).name
    for r in recs:
        if r.name == stem or r.file == file_name:
            preds: list[str] = list(r.cfg_predicates)
            if not preds:
                return None
            if len(preds) == 1:
                return preds[0]
            return "all(" + ", ".join(preds) + ")"
    return None


def _invalidate_mod_records_cache() -> None:
    """Clear the mod-records cache. Call after any operation that changes
    the on-disk mod declarations in lib.rs/main.rs (strip, task1 reset,
    task2 regeneration). Cheap enough that callers should err on the side
    of invalidation."""
    _MOD_RECORDS_CACHE.clear()


def _file_cfg_predicate_regex_fallback(
    target_src_dir: Path, file_rel: str,
) -> str | None:
    """Regex-based fallback for environments without the syn extractor
    binary. Known limitation: can conflate cfg attrs from an adjacent
    mod declaration when they share a line region. The AST path is
    preferred whenever available.
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    for root in ("src/lib.rs", "src/main.rs"):
        p = target_src_dir / root
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        mod_decl_pat = re.compile(
            r"(?:^|\n)([^\n]*\b(?:pub(?:\([^)]*\))?\s+)?mod\s+"
            + re.escape(stem) + r"\s*;[^\n]*)",
            re.MULTILINE,
        )
        m = mod_decl_pat.search(text)
        if not m:
            continue
        decl_line = m.group(1)
        preds: list[str] = []
        mod_idx = decl_line.find("mod ")
        if mod_idx < 0:
            mod_idx = len(decl_line)
        for cm in _CFG_INLINE_RE.finditer(decl_line[:mod_idx]):
            preds.append(cm.group(1).strip())
        decl_start = m.start() + (1 if text[m.start()] == "\n" else 0)
        head = text[:decl_start]
        for ln in reversed(head.splitlines()):
            s = ln.strip()
            if not s:
                break
            if s.startswith("//") or s.startswith("/*"):
                continue
            # Stop on any line that contains a declaration keyword — the
            # cfg attrs on such a line belong to ITS item, not ours.
            if contains_decl_keyword(s):
                break
            if _CFG_INLINE_RE.search(ln):
                for cm in _CFG_INLINE_RE.finditer(ln):
                    preds.append(cm.group(1).strip())
                continue
            if s.startswith("#["):
                continue
            break
        if not preds:
            return None
        if len(preds) == 1:
            return preds[0]
        return "all(" + ", ".join(preds) + ")"
    return None


def _fn_belongs_to_cluster(
    target_src_dir: Path,
    file_rel: str,
    fn_name: str,
    cluster: "BuildCluster",
    fn_cfg_predicates: "list[str] | None" = None,
) -> bool:
    """True when the fn (or its enclosing file) is active in `cluster`.

    Evaluates the conjunction of:
      - The file's parent-mod cfg (if any)
      - The fn's own cfg attrs (if any)
    against the cluster's cfg_true / cfg_false sets.

    `fn_cfg_predicates` (R7.4a): the syn extractor's cfg list for THIS fn
    record — callers that hold extractor records pass it ([] = extractor
    says ungated, trusted as-is). Only when None does the legacy regex
    text-walk run (extractor-absent text-scan path); its backward window
    can over-capture a preceding fn's cfgs, which is exactly why record
    callers must not fall through to it.

    Conservative on uncertainty: when a predicate is undecidable, defaults
    to True (include the stub) so the per-fn fill loop sees it — better
    to waste a call than miss a fn we should have filled.
    """
    cfg_true = set(cluster.cfg_true)
    cfg_false = set(cluster.cfg_false)
    preds: list[str] = []
    file_pred = _file_cfg_predicate(target_src_dir, file_rel)
    if file_pred:
        preds.append(file_pred)
    if fn_cfg_predicates is not None:
        preds.extend(fn_cfg_predicates)
    else:
        try:
            text = (target_src_dir / file_rel).read_text(encoding="utf-8")
        except Exception:
            text = ""
        if text:
            preds.extend(_fn_cfg_predicates(text, fn_name))
    if not preds:
        return True  # no cfg gating → exists in every cluster
    for p in preds:
        v = _eval_cfg_pred(p, cfg_true, cfg_false)
        if v is False:
            return False
        # v is None (undecidable) → conservatively keep
    return True


def _implicated_fns_from_cargo_errors(
    cargo_output: str,
    target_src_dir: Path,
) -> list[tuple[str, str]]:
    """Parse cargo error output and return the (file_rel, fn_name) pairs the
    errors implicate.

    For each `error[...]` block: find the `--> file.rs:line:col` marker, then
    walk backward in that file from `line` to find the enclosing fn. Dedup
    by (file, fn). Used by P3.12 end-of-cluster fix loop to identify which
    fns to revert + retry.
    """
    if not cargo_output:
        return []
    cargo_output = _strip_cargo_warnings(cargo_output)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # Enclosing-fn attribution via the syn extractor (byte-span lookup); the
    # backward `fn` regex walk is the no-binary fallback. (`-->` parse = cargo output.)
    _all_recs = _enumerate_fns_via_extractor(target_src_dir)
    _recs_by_file: dict[str, list[dict]] = {}
    if _all_recs is not None:
        for _r in _all_recs:
            _recs_by_file.setdefault(_r.get("file", ""), []).append(_r)
    loc_re = re.compile(r"-->\s+([^:\s]+\.rs):(\d+):\d+")
    for m in loc_re.finditer(cargo_output):
        file_str = m.group(1)
        line_no = int(m.group(2))
        # Normalise to rel path under target_src_dir.
        try:
            abs_p = (target_src_dir / file_str).resolve()
            rel_p = str(abs_p.relative_to(target_src_dir.resolve()))
        except Exception:
            # cargo sometimes emits absolute paths; try suffix match.
            cand = Path(file_str)
            rel_p = None
            for p in target_src_dir.rglob("*.rs"):
                if str(p).endswith(file_str) or p.name == cand.name:
                    rel_p = str(p.relative_to(target_src_dir))
                    break
            if rel_p is None:
                continue
        src_path = target_src_dir / rel_p
        if not src_path.exists():
            continue
        try:
            text = src_path.read_text(encoding="utf-8")
        except Exception:
            continue
        # Enclosing fn: prefer the extractor's byte-span records (handles impl
        # methods / cfg'd fns / inline decls); fall back to the backward regex walk.
        fn_name: str | None = None
        _recs = _recs_by_file.get(rel_p)
        if _recs:
            _rec = _line_to_fn_record(text, line_no, _recs)
            if _rec:
                fn_name = _rec["fn_name"]
        if fn_name is None:
            lines = text.splitlines()
            for i in range(min(line_no, len(lines)) - 1, -1, -1):
                if nm := first_fn_decl_name(lines[i]):
                    fn_name = nm
                    break
        if not fn_name:
            continue
        key = (rel_p, fn_name)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def _scan_unimplemented_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[tuple[str, str]]:
    """Cluster-aware variant of _scan_unimplemented_functions.

    Returns only stubs that belong to `cluster` per static cfg evaluation.
    When `cluster` is None (no cluster context), behaves identically to
    the unfiltered scan — used by callers that don't yet thread cluster.
    """
    raw = _scan_unimplemented_functions(target_src_dir)
    if cluster is None:
        return raw
    out: list[tuple[str, str]] = []
    skipped: int = 0
    for rel, fn in raw:
        if _fn_belongs_to_cluster(target_src_dir, rel, fn, cluster):
            out.append((rel, fn))
        else:
            skipped += 1
    if skipped:
        _log(f"  [phase-c/per-fn] cluster {cluster.name}: skipped "
             f"{skipped} stub(s) not active in this cluster's cfg-set.")
    return out


def _filter_test_exercised(stubs: list[tuple[str, str]],
                            test_files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only stubs whose function name appears (as a token) in any test file."""
    if not stubs or not test_files:
        return []
    test_blob = "\n".join(content for _, content in test_files)
    out: list[tuple[str, str]] = []
    for rel, name in stubs:
        if contains_word(test_blob, name):
            out.append((rel, name))
    return out


def _task2_cleanup_residuals(
    target_src_dir: Path,
    project_dir: Path,
    functional_req: str,
    test_files: list[tuple[str, str]],
    module_name: str,
) -> dict:
    """Phase D: scan for residual `unimplemented!()` and patch what tests need.

    - Classifies remaining stubs as test-exercised vs not.
    - For test-exercised stubs, issues ONE LLM call with the relevant tests + the
      current source of each touched file + an instruction to fill those stubs.
    - Untouched stubs are left as-is (cargo test won't trigger them).

    Returns {'stubs_total': int, 'stubs_exercised': int, 'files_updated': int,
             'stubs_remaining': int}.
    """
    stubs = _scan_unimplemented_functions(target_src_dir)
    if not stubs:
        return {"stubs_total": 0, "stubs_exercised": 0, "files_updated": 0, "stubs_remaining": 0}

    exercised = _filter_test_exercised(stubs, test_files)
    _log(f"  [phase-d] {len(stubs)} residual stub(s); {len(exercised)} reachable from tests.")

    if not exercised:
        return {"stubs_total": len(stubs), "stubs_exercised": 0,
                "files_updated": 0, "stubs_remaining": len(stubs)}

    # Group exercised stubs by file so the model sees each affected file as one unit.
    by_file: dict[str, list[str]] = {}
    for rel, fn in exercised:
        by_file.setdefault(rel, []).append(fn)

    # Load only the affected files (plus all of the rest for type context).
    skeleton_block = _format_skeleton_for_prompt(_load_skeleton_files(target_src_dir))
    targets_block = "\n".join(
        f"  - {rel}: {', '.join(fns)}" for rel, fns in by_file.items()
    )
    # Surface only tests that mention any of the target functions (relevance filter).
    relevant_tests: list[str] = []
    for rel, content in test_files:
        if any(contains_word(content, fn)
               for fns in by_file.values() for fn in fns):
            relevant_tests.append(f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>')
    tests_block = "\n\n".join(relevant_tests)
    _fr_cap = SINGLE_CALL_CHAR_LIMIT // 4
    _fr_capped = smart_truncate(functional_req or "", total_limit=_fr_cap,
                                head=_fr_cap // 3, tail=2 * _fr_cap // 3)

    prompt = f"""You are cleaning up residual `unimplemented!()` bodies in a Rust crate after a
phase of group-driven test-driven fills.

Crate: `{module_name}`

These specific functions are still `unimplemented!()` but the tests call them:
{targets_block}

Current source on disk:
{skeleton_block}

Tests that reference these functions:
{tests_block}

Functional requirements:
{_fr_capped}

{FILE_FORMAT_INSTRUCTION}

Output the source files you modified. Fill ONLY the functions listed above.
Do NOT modify any function whose body is NOT `unimplemented!()`.
Do NOT modify type definitions, imports, or any other items.
"""
    response = ask(prompt, model=_TASK2_MODEL)
    files = extract_files(response)
    files_updated = 0
    for rel, model_text in files.items():
        if ".gen_tests_" in rel:
            continue  # never touch task0c protected test artifacts
        disk_path = target_src_dir / rel
        if not disk_path.exists():
            continue
        disk_text = disk_path.read_text(encoding="utf-8")
        merged = _merge_fills_preserving_existing(disk_text, model_text)
        if merged != disk_text:
            disk_path.write_text(merged, encoding="utf-8")
            files_updated += 1

    remaining = _scan_unimplemented_functions(target_src_dir)
    return {
        "stubs_total": len(stubs),
        "stubs_exercised": len(exercised),
        "files_updated": files_updated,
        "stubs_remaining": len(remaining),
    }


def _cmd_phase_c_only(project_dir: Path) -> int:
    """Standalone Phase A + B + C: skeleton → grouping → per-group fill."""
    rc = _cmd_phase_a_only(project_dir)
    if rc != 0:
        return rc
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    test_files = collect_protected_test_files(project_dir)
    pairs: list[tuple[str, str]] = []
    for rel in sorted(test_files):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    if not pairs:
        _log("  [phase-c] no test files found.")
        return 6

    _log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    groups = _task2_group_tests(skeleton_dir, pairs, cluster_name="default",
                                 module_name=project_dir.name)
    _log(f"  [phase-b] {len(groups)} groups produced.")

    _log(f"\n=== Phase C (per-group fill) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    if target_src_dir.exists():
        shutil.rmtree(target_src_dir)
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""
    genesis = genesis_path.read_text(encoding="utf-8") if genesis_path.exists() else ""

    result = _task2_fill_groups(skeleton_dir, target_src_dir, groups, project_dir,
                                 cluster_name="default", module_name=project_dir.name,
                                 functional_req=fr, genesis=genesis)
    _log(f"  [phase-c] processed {result['groups_processed']} group(s), "
         f"filled {result['bytes_filled']:,} bytes; "
         f"{len(result['errors'])} error(s).")
    for e in result["errors"]:
        _log(f"    - {e}")
    # Always return 0 from Phase C so downstream callers (Phase D, the
    # backend class) can perform residual cleanup regardless of per-group
    # failures. Partial Phase C output is the recovery case Phase D exists for.
    return 0


def _cmd_skeleton_backend(project_dir: Path) -> int:
    """Standalone harness invoking StaticSkeletonTask2Generator.generate()
    directly. Drives the full per-cluster Plan 4 flow without needing
    task0c/strip/task1 to have run."""
    _log(f"\n=== Skeleton backend (Plan 4 per-cluster) for {project_dir.name} ===")
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    manifest_path = project_dir / MANIFEST_NAME
    if not fr_path.exists():
        # No FR yet — synthesize an empty one so the backend can still run.
        fr_path.write_text("# (no functional_requirements.md available)\n", encoding="utf-8")
    if not genesis_path.exists():
        genesis_path.write_text("# (no genesis.md available)\n", encoding="utf-8")
    existing_test_files = collect_protected_test_files(project_dir)
    backend = _new_skeleton_task2_generator()
    ok = backend.generate(GenerationRequest(
        project_dir=project_dir,
        genesis_path=genesis_path,
        functional_req_path=fr_path,
        manifest_path=manifest_path,
        existing_test_files=existing_test_files,
        current_module=project_dir.name,
    ))
    return 0 if ok else 8


def _cmd_phase_d_only(project_dir: Path) -> int:
    """Standalone Phase A + B + C + D."""
    rc = _cmd_phase_c_only(project_dir)
    if rc != 0:
        return rc
    _log(f"\n=== Phase D (residual cleanup) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    test_files = collect_protected_test_files(project_dir)
    pairs: list[tuple[str, str]] = []
    for rel in sorted(test_files):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    fr_path = project_dir / "functional_requirements.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""

    result = _task2_cleanup_residuals(target_src_dir, project_dir, fr, pairs,
                                       module_name=project_dir.name)
    _log(f"  [phase-d] {result['stubs_total']} residual stubs, "
         f"{result['stubs_exercised']} reachable from tests; "
         f"{result['files_updated']} file(s) updated; "
         f"{result['stubs_remaining']} stub(s) still remaining.")
    return 0


def _cmd_phase_b_only(project_dir: Path) -> int:
    """Standalone Phase A + B: skeleton + LLM-driven test grouping."""
    rc = _cmd_phase_a_only(project_dir)
    if rc != 0:
        return rc
    _log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    test_files = collect_protected_test_files(project_dir)
    # collect_protected_test_files returns set of relative paths; load contents.
    pairs: list[tuple[str, str]] = []
    for rel in sorted(test_files):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    if not pairs:
        _log("  [phase-b] no test files found.")
        return 6
    # Use a synthetic cluster_name + the project name as module for a quick smoke.
    groups = _task2_group_tests(skeleton_dir, pairs, cluster_name="default",
                                 module_name=project_dir.name)
    _log(f"  [phase-b] manifest written to {skeleton_dir / 'groups.json'} "
         f"({len(groups)} groups, "
         f"{sum(len(g['test_files']) for g in groups)} test files covered).")
    for g in groups:
        _log(f"    - {g['name']}: {len(g['test_files'])} test(s); "
             f"{len(g.get('target_symbols', []))} target symbol(s)")
    return 0


def task0_cfg_scan(project_dir: Path) -> "list[BuildCluster]":
    """Derive compiler clusters from project source — no LLM, pure static scan.

    Thin orchestrator over the P5 use-case: the `#[cfg]` facts come from the syn
    extractor (no regex on source), the classification/`BuildCluster` build is the
    pure `domain.cluster_plan`, and `build_configs.md` is written here for humans.
    See `pipeline_core.application.tasks.cfg_scan` / `pipeline_core.domain.cluster_plan`.
    """
    scan = _cfg_scan.scan_build_clusters(project_dir, ADAPTERS.extractor, _BUILD_SCRIPT_READER)
    _write_build_configs(project_dir, scan.clusters, scan.predicate_counts)
    return scan.clusters


def _cfg_to_cluster_name(pred: str) -> str:
    """Map a custom cfg predicate to a short cluster name.
    Thin shim → `pipeline_core.domain.cluster_plan`."""
    return _cluster_plan.cluster_name_for_custom(pred)


def _write_build_configs(project_dir: Path, clusters: list, predicate_lines: dict) -> None:
    lines = [
        f"# Build configurations — {project_dir.name}",
        f"# {len(clusters)} cluster(s) derived from source",
        f"# Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
    ]
    for c in clusters:
        lines.append(f"## cluster: {c.name}")
        if c.env_extra:
            for k, v in c.env_extra.items():
                lines.append(f"env: {k} += {v}")
        else:
            lines.append("env: (none)")
        if c.tarpaulin_exclude_files:
            for pat in c.tarpaulin_exclude_files:
                lines.append(f"tarpaulin_exclude: {pat}")
        else:
            lines.append("tarpaulin_exclude: (none — all files measured)")
        if c.active_cfg:
            lines.append(f"active_cfg: {', '.join(c.active_cfg)}")
        else:
            lines.append("active_cfg: (default build)")
        if c.excluded_cfg:
            lines.append(f"excluded_cfg: {', '.join(c.excluded_cfg)}")
        gated = sum(predicate_lines.get(p, 0) for p in c.active_cfg)
        if gated:
            lines.append(f"gated_lines: ~{gated}")
        lines.append("")
    (project_dir / BUILD_CONFIGS_NAME).write_text("\n".join(lines))


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

def _source_hash(project_dir: Path) -> str:
    """Hash of Cargo.toml + src/*.rs (gen_tests artifacts stripped) + test files.
    Shared across all phases — if source changes, all phase hashes become stale."""
    parts: list[str] = []
    cargo = project_dir / "Cargo.toml"
    if cargo.exists():
        parts.append(cargo.read_text(errors="replace"))
    for p in sorted((project_dir / "src").glob("*.rs")) if (project_dir / "src").exists() else []:
        # Skip gen_tests sibling files entirely — pipeline-generated content.
        if re.search(r'\.gen_tests_\d+\.rs$', p.name):
            continue
        with suppress(Exception):
            parts.append(_strip_gen_tests_decls(p.read_text(errors="replace")))
    for p in sorted(project_dir.rglob("*.rs")):
        rel = str(p.relative_to(project_dir))
        if "tests/" in rel or rel.startswith("tests"):
            with suppress(Exception):
                parts.append(p.read_text(errors="replace")[:500])
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:16]


# Slice 9: the pipeline_state.md store moved to pipeline_core/adapters/state.py
# (regex-free line parser; same file format, locking, atomic replace). Aliases
# keep every call site + the task-deps records unchanged.
_state_path = _state_mod.state_path
_read_state = _state_mod.read_state
_write_state = _state_mod.write_state
_mark_phase = _state_mod.mark_phase
_set_current = _state_mod.set_current
_phase_valid = _state_mod.phase_valid
_log_state_summary = _state_mod.log_state_summary



def _commit_and_mark(
    project_dir: Path,
    phase_key: str,
    status: str,
    *,
    commit_message: str,
    tag: "str | None" = None,
    detail: str = "",
    source_hash: "str | None" = None,
) -> None:
    """Atomic transition: COMMIT the workspace, optionally TAG, THEN mark
    the phase done in pipeline_state.md.

    This ordering is the persistence fix for the original bug — the
    legacy pattern was `do_work → _mark_phase("done") → git_commit(...)`,
    which leaks irrecoverably when the process dies between the mark and
    the commit: the next run sees `state=done`, skips re-running the
    phase, and the workspace mutation is never preserved in git. Reverse
    the order: commit first, then mark. Death between commit and mark is
    benign — the next run re-runs the (idempotent) phase, the second
    `git_commit` is a no-op because the tree is already clean, and the
    `_mark_phase` finally records done.

    `commit_message` is passed verbatim to `git_commit`. If the working
    tree has no changes the commit silently no-ops (already-clean case);
    we still proceed to tag + mark, which preserves the "resume" case
    where a prior run already committed.

    Returns nothing — failures are logged inside git_commit/git_tag.
    """
    git_commit(project_dir, commit_message)
    if tag:
        git_tag(project_dir, tag)
    _mark_phase(project_dir, phase_key, status,
                detail=detail, source_hash=source_hash)



# slice 5b: deps for the extracted task0 doc tasks (profile + context).
_TASK0_DOCS_DEPS = Task0DocsDeps(
    phase_valid=_phase_valid,
    mark_phase=_mark_phase,
    ask=ask,
    write=write,
    prompt_budget=prompt_budget,
    task0_model=_TASK0_MODEL,
    model=MODEL,
    readme_cap=_README_CAP,
    profile_cap=_PROFILE_CAP,
    project_context_name=PROJECT_CONTEXT_NAME,
)


def task0_project_profile(profile_path: Path, test_files, readme_text: str) -> str:
    # slice 5b: extracted to application.tasks.task0_docs; thin shim binding deps.
    return _task0_profile_impl(profile_path, test_files, readme_text, _TASK0_DOCS_DEPS)


def task0b_discovery(strategy_path: Path, project_dir: Path, test_files,
                     readme_text: str, profile_content: str,
                     clusters: "list[BuildCluster] | None" = None,
                     force: bool = False) -> str:
    # slice 5b: extracted to application.tasks.task0b_discovery; thin shim binding deps.
    return _task0b_impl(strategy_path, project_dir, test_files, readme_text,
                        profile_content, _TASK0B_DEPS, clusters=clusters, force=force)


def task0_project_context(context_path: Path, test_files, readme_text: str,
                          profile_content: str) -> str:
    # slice 5b: extracted to application.tasks.task0_docs; thin shim binding deps.
    return _task0_context_impl(context_path, test_files, readme_text, profile_content, _TASK0_DOCS_DEPS)


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
_API_SURFACE_CAP = max(8_000, SINGLE_CALL_CHAR_LIMIT // 2)


def _format_api_surface_section(api_surface: str) -> str:
    """Wrap the captured API surface into a labelled prompt section, head+tail
    truncated to _API_SURFACE_CAP. Returns '' if api_surface is empty."""
    if not api_surface or not api_surface.strip():
        return ""
    text = smart_truncate(
        api_surface,
        total_limit=_API_SURFACE_CAP,
        head=2 * _API_SURFACE_CAP // 3,
        tail=_API_SURFACE_CAP // 3,
    )
    return (
        "\n--- Public API surface (ground truth — pre-strip skeleton) ---\n"
        "Use these signatures and type definitions exactly. Do not invent items not\n"
        "listed here, do not change signatures, and do not contradict the shapes shown\n"
        "below. The behavioral semantics (what each function returns, invariants)\n"
        "are still derived from the tests; this skeleton only defines the SHAPE.\n"
        f"{text}\n"
    )


# --- TASK 1 synthesizer engine relocated to application/generators (P4). The
# synthesizers are use-cases over the narrow `ports.synthesizer` roles (debt #14:
# replaced the old `configure()` global-injection seam). The focused role adapters
# (`adapters.task2_roles`, wired as `_GEN_*` below) bind BOTH the TASK-2 and TASK-1
# roles; `_build_task1_config()` resolves the constants into a VO. -----------------
def _build_task1_config() -> "Task1Config":
    """Resolve the TASK-1 prompt constants/budgets into a Task1Config VO."""
    return Task1Config(
        model=MODEL,
        task1_model=_TASK1_MODEL,
        thinking_directive=_TASK1_THINKING_DIRECTIVE,
        file_format_instruction=FILE_FORMAT_INSTRUCTION,
        single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
        synthesis_group_size=SYNTHESIS_GROUP_SIZE,
        readme_cap=_README_CAP,
        strategy_cap=_STRATEGY_CAP,
        chunk_cache_dir=CHUNK_CACHE_DIR,
        parallelism=_pipeline_parallelism(),
    )


def _filter_proc_macro_test_files(test_files, crate_name: str = ""):
    """Keep test files that use #[derive(...)] or the crate's own #[crate_name(...)]
    attribute. Falls back to all files if none match. (Regex kept in pipeline.py — Option 1.)"""
    attr_pattern = r'#\[derive\('
    if crate_name:
        safe = re.escape(crate_name.replace("-", "_"))
        attr_pattern += rf'|#\[{safe}\('
    relevant = [
        (rel, content) for rel, content in test_files
        if re.search(attr_pattern, content)
    ]
    return relevant or test_files


def _proc_macro_fallback_requirements(relevant, crate_name: str = "") -> str:
    """Static fallback: scan tests for derive/attribute usage and synthesise minimal
    proc-macro requirements via the structured `attr_call_args` scanner."""
    derives: set[str] = set()
    attrs: set[str] = set()
    _attr_name = (crate_name or "").replace("-", "_") or "derive"
    for _, content in relevant:
        for inner in attr_call_args(content, "derive"):
            for d in inner.split(','):
                derives.add(d.strip())
        for inner in attr_call_args(content, _attr_name):
            attrs.add(inner.strip())
    req_content = "# Proc-macro interface\n\n"
    req_content += "## Derive macros\n"
    for d in sorted(derives):
        req_content += f"- `#[derive({d})]` — implement the `{d}` trait on annotated types\n"
    if attrs:
        req_content += "\n## Helper attributes\n"
        for a in sorted(attrs):
            req_content += f"- `#[{_attr_name}({a})]`\n"
    req_content += (
        "\n## Entry points\n"
        "Each derive macro must be registered with `#[proc_macro_derive(Name)]`.\n"
        "Input: `proc_macro::TokenStream`, Output: `proc_macro::TokenStream`.\n"
        "Parse input with `syn::parse_macro_input!(input as syn::DeriveInput)`.\n"
        "Generate output with `quote::quote! { impl Name for #ident { ... } }`.\n"
    )
    return req_content


def _new_static_task1_synthesizer() -> StaticTask1Synthesizer:
    return StaticTask1Synthesizer(llm=_GEN_LLM, shaping=_GEN_SHAPING, progress=_GEN_PROGRESS,
                                  requirements=_GEN_REQUIREMENTS, cfg=_build_task1_config())


def _new_llm_task1_synthesizer() -> LLMTask1Synthesizer:
    return LLMTask1Synthesizer(llm=_GEN_LLM, shaping=_GEN_SHAPING, progress=_GEN_PROGRESS,
                               requirements=_GEN_REQUIREMENTS, cfg=_build_task1_config())


def _task1_synthesizer_factory() -> Task1Synthesizer:
    backend = os.getenv("TASK1_SYNTHESIZER", "llm").lower()
    if backend == "static":
        return _new_static_task1_synthesizer()
    return _new_llm_task1_synthesizer()


# slice 5b: _task1_proc_macro moved to application.tasks.task1.


def task1(project_dir, genesis_path, functional_req_path, test_files, readme_text,
          project_profile: str = "", crate_type: str = "", module_name: str = "",
          project_context: str = "", coverage_strategy: str = ""):
    _emit("task.start", task="task1")  # debt#7: structured stage event (monitor reads these)
    # slice 5b: extracted to application.tasks.task1; thin shim binding deps.
    return _task1_impl(
        project_dir, genesis_path, functional_req_path, test_files, readme_text, _TASK1_DEPS,
        project_profile=project_profile, crate_type=crate_type, module_name=module_name,
        project_context=project_context, coverage_strategy=coverage_strategy,
    )


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
# monkeypatches intercept at call time), and `_task2_generator_factory` injects
# them. Each role is a narrow ISP port; the god-adapter `_Task2Adapter` is gone.
#
# Selected by TASK2_GENERATOR in .env: static | llm | skeleton | filelist (default).
# ---------------------------------------------------------------------------
from pipeline_core.application.generators.task2_generator import (  # noqa: E402
    LLMTask2Generator,
    StaticFileListTask2Generator,
    StaticSkeletonTask2Generator,
    StaticTask2Generator,
    Task2Generator,
)
from pipeline_core.domain.generation import (
    GenerationRequest,  # noqa: E402
    SkeletonConfig,  # noqa: E402
    Task1Config,  # noqa: E402
    Task2Config,  # noqa: E402
)
from pipeline_core.domain.generation import (
    gen_tests_decl_blocks as _gen_tests_decl_blocks,  # noqa: E402
)
from pipeline_core.domain.generation import (
    parse_gen_tests_filename as _parse_gen_tests_filename,  # noqa: E402
)
from pipeline_core.domain.generation import (
    prelude_import_names as _prelude_import_names,  # noqa: E402
)
from pipeline_core.domain.generation import (
    strip_all_gen_tests_decls as _strip_all_gen_tests_decls,  # noqa: E402
)
from pipeline_core.domain.generation import (
    strip_gen_tests_decl as _strip_gen_tests_decl,  # noqa: E402
)
from pipeline_core.domain.generation import (
    strip_gen_tests_decl_n as _domain_strip_gen_tests_decl_n,  # noqa: E402
)

# P6: the `_Task2Adapter` god-object is dissolved — each TASK-1/TASK-2 generator
# role is a focused adapter (adapters.task2_roles), constructor-injected with
# late-binding lambdas over the resident composition-root callables (so the
# scripted-harness / test monkeypatches still intercept at call time). The
# generator/synthesizer use-cases receive narrow role ports (ISP) instead of one
# multi-role binding object.
_GEN_TELEMETRY = GeneratorTelemetryAdapter(
    log_fn=lambda msg: _log(msg),
    emit_fn=lambda event, **kw: _emit(event, **kw),
    check_stop_fn=lambda context: _check_graceful_stop(context),
    set_current_fn=lambda pd, label: _set_current(pd, label),
    mark_phase_fn=lambda pd, key, status, detail="": _mark_phase(pd, key, status, detail=detail),
    phase_valid_fn=lambda pd, key: _phase_valid(pd, key),
)
_GEN_LLM = LlmAskAdapter(ask_fn=lambda prompt, *, model=None: ask(prompt, model=model))
_GEN_WORKSPACE = WorkspaceAdapter(
    read_fn=lambda path: read(path),
    write_source_files_fn=lambda fd, pd, mp, etf: write_source_files(fd, pd, mp, etf),
    load_manifest_fn=lambda mp: load_manifest(mp),
    save_manifest_fn=lambda mp, files: save_manifest(mp, files),
    load_original_structure_fn=lambda pd, mn: load_original_structure(pd, mn),
    crate_entry_point_fn=lambda ctp, mdr, fps: _crate_entry_point(ctp, mdr, fps),
    is_proc_macro_crate_fn=lambda txt: _cargo_is_proc_macro(txt),
)
_GEN_DOCS = ContextDocsAdapter(
    scan_downstream_api_fn=lambda pd, mn: scan_downstream_api(pd, mn),
    compile_patterns_read_fn=lambda pd, m: compile_patterns_read(pd, m),
    genesis_rules_read_fn=lambda pd, module_scope="*": genesis_rules_read(pd, module_scope=module_scope),
    extract_test_assertions_fn=lambda tf, mdr="": _extract_test_assertions(tf, mdr),
    format_api_surface_section_fn=lambda api: _format_api_surface_section(api),
    split_api_surface_by_file_fn=lambda api: _split_api_surface_by_file(api),
)
_GEN_CARGO = CargoBackendAdapter(
    run_fn=lambda cmd, cwd, env, timeout=180: _run_cargo(cmd, cwd, env, timeout),
    run_check_diag_fn=lambda cmd, cwd, env, timeout=180: _run_cargo_check_diag(cmd, cwd, env, timeout),
    build_test_cmd_fn=lambda stems, cluster: _build_test_cmd(stems, cluster),
    env_fn=lambda: _cargo_env(),
    implicated_fns_fn=lambda out, tsd: _implicated_fns_from_cargo_errors(out, tsd),
    attribute_failures_fn=lambda to, sd, rf, trm, cn: _attribute_failures(to, sd, rf, trm, cn),
    filter_test_targets_fn=lambda fr, sd: _filter_test_targets(fr, sd),
)
# P6: cross-cluster skeleton merge — focused adapter, composition-injected.
_CLUSTER_MERGER = ClusterMerger(extractor_bin=_SKELETON_EXTRACTOR_BIN, log=_log)

# P6: mutation-testing behaviour hardening — focused adapter, composition-injected
# (ask late-bound so a test monkeypatching pipeline.ask still intercepts).
_MUTATION = MutationTester(
    cargo_env=_cargo_env, ask=lambda *a, **k: ask(*a, **k), log=_log,
    task0c_model=_TASK0C_MODEL, single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
)

_GEN_FILL = FillEngineAdapter(
    run_phase_c_tdd_fn=lambda **kw: _run_phase_c_tdd(**kw),
    fill_fns_one_by_one_fn=lambda **kw: _task2_fill_fns_one_by_one(**kw),
    fill_groups_fn=lambda sd, tsd, groups, pd, **kw: _task2_fill_groups(sd, tsd, groups, pd, **kw),
    group_tests_fn=lambda sd, tf, **kw: _task2_group_tests(sd, tf, **kw),
    cleanup_residuals_fn=lambda tsd, pd, fr, tf, **kw: _task2_cleanup_residuals(tsd, pd, fr, tf, **kw),
    scan_unimplemented_fn=lambda sd: _scan_unimplemented_functions(sd),
    merge_cluster_outputs_fn=lambda **kw: _CLUSTER_MERGER.merge_cluster_outputs(**kw),
    generate_fix_loop_fn=lambda **kw: generate_fix_loop(**kw),
    reset_exhaustion_flags_fn=lambda pd, reason: _reset_exhaustion_flags(pd, reason),
    build_test_reverse_map_fn=lambda tf, sd: _build_test_reverse_map(tf, sd),
)
_GEN_CLUSTERS = ClustersAdapter(
    load_baked_fn=lambda pd: _load_baked_clusters(pd),
    load_baked_leaf_order_fn=lambda pd, cn: _load_baked_leaf_order(pd, cn),
    filter_tests_for_cluster_fn=lambda tf, cluster: _filter_tests_for_cluster(tf, cluster),
    cluster_cfg_attr_fn=lambda cluster: _cluster_cfg_attr(cluster),
)
_GEN_GENTESTS = GenTestsAdapter(
    build_decl_fn=lambda name, n, allow, cluster_cfg_attr="": _build_gen_tests_decl(name, n, allow, cluster_cfg_attr=cluster_cfg_attr),
    has_decl_fn=lambda txt, pn: _has_gen_tests_decl(txt, pn),
    strip_decl_n_fn=lambda txt, pn: _strip_gen_tests_decl_n(txt, pn),
    derive_prelude_fn=lambda pd: _derive_gentest_prelude(pd),
    load_origin_fn=lambda pd: _load_gen_tests_origin(pd),
    propagate_fn=lambda gt, om, ps, pn, tag="": _propagate_or_quarantine_sibling(gt, om, ps, pn, _log, tag),
    is_gen_tests_file_fn=lambda name: _is_gen_tests_file(name),
)
_GEN_SCAN = SourceScanAdapter(
    locate_fn_fn=lambda txt, fn: _locate_fn(txt, fn),
    rust_delimiters_balanced_fn=lambda txt: _rust_delimiters_balanced(txt),
)
_GEN_SHAPING = PromptShapingAdapter(
    prompt_budget_fn=lambda *parts: prompt_budget(*parts),
    format_test_files_fn=lambda tf: format_test_files(tf),
    chunk_test_files_by_file_fn=lambda tf, limit: chunk_test_files_by_file(tf, limit),
    format_api_surface_section_fn=lambda api: _format_api_surface_section(api),
)
_GEN_PROGRESS = ProgressFactoryAdapter(make_progress_fn=lambda total: ChunkProgress(total))
_GEN_REQUIREMENTS = RequirementsDocAdapter(
    write_fr_preserving_notes_fn=lambda path, content: _write_fr_preserving_notes(path, content),
    type_signals_read_fn=lambda pd: type_signals_read(pd),
)


def _build_task2_config() -> "Task2Config":
    """Resolve the prompt scaffolding constants/budgets into a Task2Config VO."""
    return Task2Config(
        file_format_instruction=FILE_FORMAT_INSTRUCTION,
        task2_model=_TASK2_MODEL,
        thinking_directive=_TASK2_THINKING_DIRECTIVE,
        verbosity_guard=_TASK2_VERBOSITY_GUARD,
        genesis_cap=_GENESIS_CAP,
        strategy_cap=_STRATEGY_CAP,
        single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
        model_label=MODEL,
    )


def _new_static_task2_generator() -> StaticTask2Generator:
    return StaticTask2Generator(tel=_GEN_TELEMETRY, llm=_GEN_LLM, ws=_GEN_WORKSPACE,
                                docs=_GEN_DOCS, cfg=_build_task2_config())


def _new_llm_task2_generator() -> LLMTask2Generator:
    return LLMTask2Generator(tel=_GEN_TELEMETRY, llm=_GEN_LLM, ws=_GEN_WORKSPACE,
                             docs=_GEN_DOCS, cfg=_build_task2_config())


def _new_filelist_task2_generator() -> StaticFileListTask2Generator:
    return StaticFileListTask2Generator(tel=_GEN_TELEMETRY, llm=_GEN_LLM, ws=_GEN_WORKSPACE,
                                        docs=_GEN_DOCS, cfg=_build_task2_config())


def _build_skeleton_config() -> "SkeletonConfig":
    """Resolve the skeleton generator's runtime knobs (SETTINGS + env) into a VO."""
    return SkeletonConfig(
        task2_model=_TASK2_MODEL,
        parallelism=_pipeline_parallelism(),
        phase_c_per_fn=SETTINGS.phase_c_mode is PhaseCMode.PER_FN,
        phase_c_tdd=SETTINGS.phase_c_driver is PhaseCDriver.TDD,
        phase_c_max_passes=int(os.getenv("PHASE_C_MAX_PASSES", "3")),
        validate_iters=int(os.getenv("PHASE_C_VALIDATE_ITERS", "3")),
        post_merge_max_stalls=int(os.getenv("POST_MERGE_MAX_STALLS", "3")),
        post_merge_hard_cap=int(os.getenv("POST_MERGE_HARD_CAP", "10000")),
        post_merge_test_timeout=int(os.getenv("POST_MERGE_TEST_TIMEOUT", "180")),
        recently_filled_max=int(os.getenv("TDD_RECENTLY_FILLED_MAX", "50")),
    )


def _new_skeleton_task2_generator() -> StaticSkeletonTask2Generator:
    return StaticSkeletonTask2Generator(
        tel=_GEN_TELEMETRY, cargo=_GEN_CARGO, fill=_GEN_FILL, clusters=_GEN_CLUSTERS,
        gentests=_GEN_GENTESTS, scan=_GEN_SCAN, ws=_GEN_WORKSPACE,
        cfg=_build_skeleton_config())


def _crate_entry_point(cargo_toml_path: Path, module_dir_rel: str,
                        file_path_set: set) -> str | None:
    """Return the crate entry point path (relative to project root) from Cargo.toml."""
    candidates = []
    if cargo_toml_path.exists():
        text = cargo_toml_path.read_text(encoding="utf-8")
        for rel in _domain_cargo_entry_point_paths(text):
            candidates.append(str(Path(module_dir_rel) / rel) if module_dir_rel else rel)
    for default in ("src/lib.rs", "src/main.rs"):
        candidates.append(str(Path(module_dir_rel) / default) if module_dir_rel else default)
    return next((c for c in candidates if c in file_path_set), None)


def _split_api_surface_by_file(api_surface: str) -> list[tuple[str, str]]:
    """Parse monolithic API surface doc into (path, full_section_text) pairs.
    Regex-free domain scan (R8.5)."""
    return _domain_split_api_surface_by_file(api_surface)




def _is_proc_macro_crate(cargo_toml_text: str) -> bool:
    """True if Cargo.toml declares `proc-macro = true`. (Regex kept in pipeline — Option 1.)"""
    return _cargo_is_proc_macro(cargo_toml_text)


def _count_rustc_error_codes(output: str) -> int:
    """Count distinct rustc error codes (E0001 etc.) in build output.
    (Tool-output regex kept in pipeline — Option 1.)"""
    return len(set(re.findall(r"\bE\d{3,4}\b", output or "")))


def _task2_generator_factory() -> Task2Generator:
    backend = os.getenv("TASK2_GENERATOR", "filelist").lower()
    if backend == "llm":
        return _new_llm_task2_generator()
    if backend == "static":
        return _new_static_task2_generator()
    if backend == "skeleton":
        return _new_skeleton_task2_generator()
    return _new_filelist_task2_generator()  # default: filelist


# _format_whole_project_context → pipeline_core.adapters.artifacts.WorkspaceArtifacts.


def task2(project_dir, genesis_path, functional_req_path, manifest_path, existing_test_files,
          project_profile: str = "", workspace_structure: str = "", build_error: str = "",
          locked_dep_context: str = "", current_module: str = "", module_dir_rel: str = "",
          whole_project_context: str = "", coverage_strategy: str = "", api_surface: str = "",
          prev_source_context: str = ""):
    # slice 5b: extracted to application.tasks.task2; thin shim binding deps.
    return _task2_impl(
        project_dir, genesis_path, functional_req_path, manifest_path, existing_test_files,
        _TASK2_DEPS, project_profile=project_profile, workspace_structure=workspace_structure,
        build_error=build_error, locked_dep_context=locked_dep_context,
        current_module=current_module, module_dir_rel=module_dir_rel,
        whole_project_context=whole_project_context, coverage_strategy=coverage_strategy,
        api_surface=api_surface, prev_source_context=prev_source_context,
    )


def task2_inner_loop(project_dir, genesis_path, functional_req_path, manifest_path,
                     existing_test_files, project_profile, workspace_structure,
                     build_check_cmd: str | None, locked_dep_context: str = "",
                     current_module: str = "", module_dir_rel: str = "",
                     whole_project_context: str = "", coverage_strategy: str = "",
                     api_surface: str = "", prev_source_context: str = ""):
    _emit("task.start", task="task2")  # debt#7: structured stage event
    # slice 5b: extracted to application.tasks.task2; thin shim binding deps.
    return _task2_inner_impl(
        project_dir, genesis_path, functional_req_path, manifest_path, existing_test_files,
        project_profile, workspace_structure, build_check_cmd, _TASK2_DEPS,
        locked_dep_context=locked_dep_context, current_module=current_module,
        module_dir_rel=module_dir_rel, whole_project_context=whole_project_context,
        coverage_strategy=coverage_strategy, api_surface=api_surface,
        prev_source_context=prev_source_context,
    )


def _render_impl_block(file_text: str, members: "Sequence[FnRecord]") -> str:
    """Verbatim source of the impl block containing `members` (header +
    where-clause + all bodies), via byte-range parse. Used by the per_impl fix
    unit to give the model the whole impl block as context. Falls back to
    listing member signatures if the byte range can't be resolved."""
    if not members:
        return ""
    anchor = members[0].sig_byte_start
    for r in parse_impl_block_ranges(file_text):
        if r.body_open <= anchor < r.body_close:
            return file_text[r.header_start:r.body_close + 1]
    return "\n".join(m.sig_text for m in members)


def generate_fix_loop(
    project_dir: Path,
    check_cmd: str,
    source_paths: list[Path],
    existing_test_files: set,
    extra_context: str = "",
    model: str = MODEL,
    max_fix_attempts: int = 2,
    timeout: int = BUILD_TIMEOUT,
    allowed_dir_prefix: str = "",
    functional_req: str = "",
    fr_path: "Path | None" = None,
    check_fn: "Callable[[], tuple[bool, str]] | None" = None,
) -> tuple[bool, str]:
    """
    Shared iterative fix loop for any code generation step.

    check_fn, when given, REPLACES check_cmd as the verdict authority — the
    post-task3 fix loop passes the per-cluster strict runner here so a fix is
    validated under EVERY baked cluster's toolchain/env_extra. A bare
    check_cmd sees only the default cluster and instantly "passes" while a
    cfg-gated cluster is failing (the 2026-06-12 false SUCCESS).

    fr_path (functional_requirements_{module}.md) keys the Repository
    Specifications section (FR + genesis + API) injected into every fix
    prompt this loop builds — the per-fn/per-impl inner loops and the
    whole-file rewrite path.

    Pattern:
      1. Run check_cmd (compile check or test run).
      2. If it passes → return (True, output).
      3. If it fails → show LLM the error + current source files, ask it to fix
         ONLY the broken files (preserve correct ones).
      4. Write fixed files, re-run check.
      5. Repeat up to max_fix_attempts times.

    Used by:
    - TASK 0c: fix unit tests that don't compile (compile check).
    - TASK 2 inner loop: fix generated code that doesn't compile (build check).
    - Post-TASK 3: fix generated code whose tests fail at runtime (test run).

    Returns (passed, final_output).
    """
    def _log_compile_overview(output: str, attempt_n: int) -> None:
        """Log per-file error table and emit compile.file_stats event."""
        err_by_file = _fix_diag.errors_by_file(output)
        fstats = _fix_diag.file_stats(source_paths, project_dir)
        all_files = sorted({*fstats.keys(), *err_by_file.keys()})
        clean = sum(1 for f in all_files if err_by_file.get(f, 0) == 0 and f in fstats)
        total = len(all_files)
        _log(f"  [fix loop] File overview — {clean}/{total} clean, attempt {attempt_n}:")
        max_name = max((len(f.split('/')[-1]) for f in all_files), default=10)
        rows_data = []
        for f in sorted(all_files, key=lambda f: (-err_by_file.get(f, 0), f)):
            errs = err_by_file.get(f, 0)
            st = fstats.get(f, {})
            lines = st.get("lines", 0)
            fns = st.get("fns", 0)
            short = f.split("/")[-1]
            bar_s = "█" * min(errs, 8) if errs else "✓"
            _log(f"    {short.ljust(max_name)}  {str(lines).rjust(4)}L  {str(fns).rjust(3)}fn  {bar_s} {errs or 'clean'}")
            rows_data.append({"file": f, "lines": lines, "fns": fns, "errors": errs})
        _emit("compile.file_stats", data={
            "attempt": attempt_n, "clean": clean, "total": total, "files": rows_data,
        })
        # When the check command runs tests (t3 fix loop), publish per-file
        # test results alongside the compile stats.
        _emit_per_file_test_overview(
            project_dir, output, context=f"fix attempt {attempt_n}")

    env = _cargo_env()
    _fl_max_stalls = int(os.getenv("GENERATE_FIX_LOOP_MAX_STALLS", "3"))
    # `max_fix_attempts` retained as a kwarg for backward-compat but is no
    # longer a counter cap — it's used only as a logging hint. Real exit is
    # the floor-stall in `_improvement_loop`.
    _ = max_fix_attempts

    def _run_check() -> tuple[bool, str, int]:
        # Graceful-stop check at the top of each iter.
        _graceful_sentinel = Path("/tmp/pipeline_graceful_stop")
        if _graceful_sentinel.exists():
            with suppress(OSError):
                _graceful_sentinel.unlink()
            _log("  [fix loop] Graceful stop requested — exiting cleanly.")
            sys.exit(0)
        try:
            if check_fn is not None:
                ok, output = check_fn()
                if ok:
                    return True, output, 0
            else:
                result = subprocess.run(
                    _parse_cmd(check_cmd), cwd=project_dir,
                    capture_output=True, text=True, timeout=timeout, env=env,
                )
                output = result.stdout + (
                    "\n--- stderr ---\n" + result.stderr if result.stderr else ""
                )
                if result.returncode == 0:
                    return True, output, 0
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s", 10**9
        # Floor metric: compile-error sigs while it won't compile, then failing
        # -test count (so the stall budget senses behavioral repair). See docstring.
        return False, output, _fix_diag.floor_metric(output)

    def _do_fix(output: str, history: list[dict], *, retro: str) -> _FixReport:
        """Legacy whole-file rewrite strategy. Returns a typed FixReport (the
        composition root passes this as generate_fix's `whole_file_fix`)."""
        if output.startswith("TIMEOUT after "):
            return _FixReport.aborted(
                reason="check_timeout", summary="check timed out")
        _log_compile_overview(output, len(history))
        # Build source context — read current content from disk in full.
        source_block = ""
        for p in source_paths:
            rel = str(p.relative_to(project_dir))
            if rel in existing_test_files or not p.exists():
                continue
            content = p.read_text(encoding="utf-8")
            source_block += f'\n<FILE path="{rel}">\n{content}\n</FILE>\n'

        # Running narrative of prior fix attempts (from improvement-loop history).
        _attempt_lines: list[str] = []
        for rec in history:
            iter_n = rec.get("iter", "?")
            chg = rec.get("files_changed_str", "no files changed")
            err_prev = rec.get("error_preview", "")
            _attempt_lines.append(
                f"Attempt {iter_n}: changed [{chg}] → still failing.\n"
                f"Error was:\n{err_prev}"
            )
        _history_section = ""
        if _attempt_lines:
            n = len(_attempt_lines)
            _history_section = (
                f"You have already made {n} fix attempt{'s' if n != 1 else ''} in this loop. "
                f"Here is what happened — do NOT repeat the same changes:\n\n"
                + "\n\n".join(_attempt_lines)
                + "\n\nNow make a different fix that avoids every approach above.\n"
            )

        # Strategy retrospective injected when prior iter didn't improve.
        _retro_block = (
            f"\n<RETROSPECTIVE_GUIDANCE>\nThe previous attempt did not reduce "
            "the compile error count. Apply this strategy instead of repeating "
            f"the prior approach:\n{retro}\n</RETROSPECTIVE_GUIDANCE>\n"
            if retro.strip() else ""
        )

        _gotchas_brief    = _gotchas_summary()
        _gotchas_specific = _gotchas_for_error(output)
        _gotchas_block    = f"\n<RUST_GOTCHAS>\n{_gotchas_brief}\n</RUST_GOTCHAS>\n"
        if _gotchas_specific:
            _gotchas_block += (
                f"\n<RELEVANT_GOTCHAS_DETAIL>\n{_gotchas_specific}\n"
                "</RELEVANT_GOTCHAS_DETAIL>\n"
            )
        _spec_header = "\nFunctional requirements (the contract code must satisfy):\n" if functional_req else ""
        _fl_scaffold = f"""Code generation produced files that fail the check below.
{_FIX_THINKING_DIRECTIVE}Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}{_spec_header}{_retro_block}{_gotchas_block}
Error output:
```

```

Current source files:


{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}"""
        _fl_overhead = len(_fl_scaffold)
        _fl_min_err  = 10_000
        _fl_min_src  = 30_000
        _fl_min_spec = 8_000 if functional_req else 0
        _extra_budget = max(0,
            SINGLE_CALL_CHAR_LIMIT - _fl_overhead - _fl_min_err - _fl_min_src - _fl_min_spec)
        _extra_ctx_capped = smart_truncate(extra_context, total_limit=_extra_budget,
                                           head=_extra_budget // 3, tail=2 * _extra_budget // 3)
        _after_extra = max(_fl_min_err + _fl_min_src + _fl_min_spec,
                           SINGLE_CALL_CHAR_LIMIT - _fl_overhead - len(_extra_ctx_capped))
        if functional_req:
            _spec_cap = _after_extra // 4
            _remaining = _after_extra - _spec_cap
            _err_cap = _remaining // 3
            _src_cap = _remaining * 2 // 3
            _spec_capped = smart_truncate(functional_req, total_limit=_spec_cap,
                                          head=_spec_cap // 3, tail=2 * _spec_cap // 3)
            _spec_block = f"\nFunctional requirements (the contract code must satisfy):\n{_spec_capped}\n"
        else:
            _err_cap = _after_extra // 3
            _src_cap = _after_extra * 2 // 3
            _spec_block = ""
        _last_output_capped = smart_truncate(output, total_limit=_err_cap,
                                             head=_err_cap // 4, tail=3 * _err_cap // 4)
        _source_block_capped = smart_truncate(source_block, total_limit=_src_cap,
                                              head=_src_cap // 3, tail=2 * _src_cap // 3)
        # Repository Specifications complement: genesis rules + methodology
        # (the FR is already in _spec_block; no target identifiers here, so
        # the FR-excerpt/API parts of the section stay empty by design).
        _repo_block = _tag_block(
            "REPOSITORY_SPECIFICATIONS",
            _repo_spec_section(fr_path, char_budget=_REPO_SPEC_CAP_WIDE))
        prompt = f"""Code generation produced files that fail the check below.
{_FIX_THINKING_DIRECTIVE}Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}{_spec_block}{_repo_block}{_retro_block}{_gotchas_block}
Error output:
```
{_last_output_capped}
```

Current source files:
{_source_block_capped}
{_extra_ctx_capped}
{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}"""
        _log(f"  [fix loop] iter {len(history) + 1} — asking LLM to fix specific failures.")
        response = ask(prompt, model=model)
        fixed = extract_files(response)
        if not fixed:
            _log(f"  [fix loop] LLM produced no fixes on iter {len(history) + 1}.")
            _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output
            return _FixReport.unchanged(
                summary="no_fixes_produced", error_preview=_err_preview)
        # Whole-file rewrites (legacy strategy — the model returns each broken
        # file in full and we overwrite it).
        for rel, content in fixed.items():
            if rel in existing_test_files:
                continue
            if Path(rel).suffix not in SOURCE_EXTENSIONS:
                continue
            if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                _log(f"  [fix loop] REJECTED {rel}: outside allowed dir '{allowed_dir_prefix}'.")
                continue
            stripped = content.lstrip()
            if re.match(r'^error(\[E\d+\])?:', stripped) or re.match(r'^warning(\[W\d+\])?:', stripped):
                _log(f"  [fix loop] REJECTED {rel}: content looks like compiler output, not code.")
                continue
            write(project_dir / rel, content)
            _log(f"  [fix loop] Fixed: {rel}")

        _all_changed = sorted(fixed.keys())
        _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output

        # BL-025: log fix patterns for future prompts.
        if _all_changed:
            _bl025_module = allowed_dir_prefix.rstrip('/').split('/')[-2] if allowed_dir_prefix else ""
            if _bl025_module:
                _error_category = _categorize_compile_error(output)
                _fix_hint = "; ".join(_all_changed[:3])
                compile_patterns_write(
                    project_dir, _bl025_module, _error_category,
                    example=output[:200],
                    fix_hint=f"fixed in {_fix_hint}",
                )
        return _FixReport.changed(
            summary=f"rewrote={len(_all_changed)}",
            files_changed_str=", ".join(_all_changed[:4]) if _all_changed else "no files changed",
            error_preview=_err_preview, error_sig=_err_preview)

    def _do_retro(output: str, history: list[dict]) -> str:
        return _whole_tree_retrospective(
            error_excerpt=output, history=history, name="generate_fix_loop",
        )

    # --- shared helpers for the per_fn strategy (unit = fn | impl) ----------
    def _tests_for_names(names: "set[str]") -> str:
        """Test files referencing any of `names`, budget-capped."""
        parts: list[str] = []
        used = 0
        for trel in sorted(existing_test_files):
            tp = project_dir / trel
            if not tp.exists():
                continue
            try:
                tc = tp.read_text(encoding="utf-8")
            except OSError:
                continue
            if not any(n and n in tc for n in names):
                continue
            blk = f'<TEST path="{trel}">\n{tc}\n</TEST>'
            if used + len(blk) > _TYPES_SECTION_CAP:
                break
            parts.append(blk); used += len(blk)
        return ("\nTests this code must satisfy:\n" + "\n".join(parts) + "\n") if parts else ""

    def _gotchas_block_for(out: str) -> str:
        gd = _gotchas_for_error(out)
        block = f"\n<RUST_GOTCHAS>\n{_gotchas_summary()}\n</RUST_GOTCHAS>\n"
        if gd:
            block += f"<RELEVANT_GOTCHAS_DETAIL>\n{gd}\n</RELEVANT_GOTCHAS_DETAIL>\n"
        return block

    # P3.C: the two inner cargo-check loops are now the FnFixUnit / ImplFixUnit
    # use-case (application.reconstruct.fix_unit). pipeline.py is the composition
    # root — these thin adapters satisfy the ISP role ports over the resident
    # check/model/splice/prompt helpers; the loop policy and the prompt+splice
    # live in pipeline_core and are unit-tested with fakes.
    class _FixChecker:
        def probe(self, cmd, cwd, env_, timeout_):
            rc, rendered, diags = _run_cargo_check_diag(cmd, cwd, env_, timeout_)
            ebf: dict[str, int] = {}
            for d in diags:
                f = d.file or ""
                if f:
                    ebf[f] = ebf.get(f, 0) + 1
            return CompileProbe(rc=rc, rendered=_strip_ansi(rendered),
                                frontier=_frontier_from_diags(diags, project_dir),
                                errors_by_file=ebf)

    class _FixModel:
        def ask(self, prompt):
            return ask(prompt, model=model)

    class _FixSplicer:
        def locate_fn(self, text, name):
            return _locate_fn(text, name)

        def is_pub_fn(self, text, name):
            return _is_pub_fn(text, name)

        def render_impl_block(self, text, members):
            return _render_impl_block(text, members)

        def splice_fn(self, file_path, container, name, body, *,
                      where_clause=None, allow_nonstub=False):
            return _splice_fn_with_where(file_path, container, name, body,
                                         where_clause=where_clause,
                                         allow_nonstub=allow_nonstub)

        def splice_body(self, file_path, name, body, *, allow_nonstub=False):
            return _splice_fn_body(file_path, name, body,
                                   allow_nonstub=allow_nonstub)

    class _FixPrompts:
        def repo_spec_block(self, *, file_rel="", fn_name="", container=""):
            return _tag_block("REPOSITORY_SPECIFICATIONS", _repo_spec_section(
                fr_path, file_rel=file_rel, fn_name=fn_name, container=container))

        def gotchas_block(self, output):
            return _gotchas_block_for(output)

        def tests_block(self, names):
            return _tests_for_names(set(names))

    _fix_session = FixSession.for_check_cmd(
        project_dir=project_dir, check_cmd=check_cmd,
        derive_check_only=_derive_check_only_cmd, env=env, timeout=timeout,
        model=model, fr_path=fr_path,
        existing_test_files=frozenset(existing_test_files),
        allowed_dir_prefix=allowed_dir_prefix)
    _fix_unit_deps = FixUnitDeps(
        checker=_FixChecker(), model=_FixModel(), splicer=_FixSplicer(),
        prompts=_FixPrompts(), log=_log, do_retro=_do_retro,
        thinking_directive=_FIX_THINKING_DIRECTIVE, err_cap=_FIX_ERR_CAP,
        per_fn_max_stalls=int(os.getenv("FIX_LOOP_PER_FN_MAX_STALLS", "2")))

    # P3.C: the per-fn / per-impl / whole-file dispatch is now the typed strategy
    # set in application.reconstruct.generate_fix, run by its driver. This frontier
    # adapter satisfies the FrontierBuilder port (test-oracle-filtered, FR-018).
    class _Frontiers:
        def compile_frontier(self, output):
            return _filter_test_targets(
                _compile_error_frontier(output, project_dir, check_cmd, env, timeout),
                project_dir)

        def tdd_frontier(self, output):
            return _filter_test_targets(
                _extract_tdd_frontier(output, project_dir), project_dir)

        def behavior_frontier(self, output):
            fr, srcs = _behavior_frontier(output, project_dir, existing_test_files)
            return _filter_test_targets(fr, project_dir), srcs

    _gen_fix_deps = GenerateFixDeps(
        unit=_fix_unit_deps,
        frontiers=_Frontiers(),
        enumerate_fns=lambda: _enumerate_fns_via_extractor(project_dir) or [],
        collect_impl_blocks=lambda: _collect_impl_blocks(project_dir) or [],
        log_overview=_log_compile_overview,
        whole_file_fix=lambda o, h, r: _do_fix(o, h, retro=r),
        behavior_max_fns=int(os.getenv("FIX_BEHAVIOR_MAX_FNS", "8")),
        src_snip_cap=_SRC_SNIP_CAP)

    return run_generate_fix(
        session=_fix_session, deps=_gen_fix_deps,
        strategy=SETTINGS.fix_loop_strategy, unit=SETTINGS.fix_loop_unit,
        run_check=_run_check, do_retro=_do_retro,
        max_stalls=_fl_max_stalls, log=_log)


def task2_fix_loop(
    project_dir: Path,
    manifest_path: Path,
    existing_test_files: set,
    test_output: str,
    test_command: str,
    functional_req_path: Path,
    genesis_path: Path | None = None,
    module_dir_rel: str = "",
) -> tuple[bool, str]:
    # slice 5b: extracted to application.tasks.task2; thin shim binding deps.
    return _task2_fix_impl(
        project_dir, manifest_path, existing_test_files, test_output, test_command,
        functional_req_path, _TASK2_DEPS, genesis_path=genesis_path, module_dir_rel=module_dir_rel,
    )


def _cluster_check_factory(project_dir: Path) -> "Callable[[], tuple[bool, str]] | None":
    """check_fn for the post-task3 fix loop: the per-cluster strict runner
    (run_clusters_strict), so a fix only counts as passing when EVERY baked
    cluster passes under its own toolchain/env_extra. None when no baked
    clusters exist — legacy single-command pipelines keep the check_cmd path."""
    clusters = _load_baked_clusters(project_dir)
    if not clusters:
        return None

    def _check() -> tuple[bool, str]:
        all_pass, output, _results = run_clusters_strict(
            project_dir, clusters, _TASK3_DEPS)
        return all_pass, output

    return _check


# slice 5b: deps for the extracted TASK 2 (defined after generate_fix_loop, its latest dep).
_TASK2_DEPS = Task2Deps(
    generator_factory=_task2_generator_factory,
    load_manifest=load_manifest,
    read=read,
    generate_fix_loop=generate_fix_loop,
    store_prompt_content=_store_prompt_content,
    is_proc_macro_crate=_is_proc_macro_crate,
    count_error_codes=_count_rustc_error_codes,
    build_check_retries=BUILD_CHECK_RETRIES,
    build_max_stalls=int(os.getenv("TASK2_BUILD_MAX_STALLS", "2")),
    build_timeout=BUILD_TIMEOUT,
    test_timeout=TEST_TIMEOUT,
    task2_model=_TASK2_MODEL,
    task2_fix_retries=TASK2_FIX_RETRIES,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    genesis_cap=_GENESIS_CAP,
    cluster_check_factory=_cluster_check_factory,
)


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
    """Improvement-driven retry over a failed task3. Returns (passed, final_output).
    If the retry cannot beat the initial failure floor, restores the source
    tree from snapshot and returns (False, initial_output).
    """
    if os.getenv("TASK3_INLINE_RETRY", "1") == "0":
        return False, initial_test_output

    # R9 residual: be CLUSTER-AWARE. task3 runs each cluster with its own toolchain
    # + env_extra (RUSTFLAGS/cfg); the failing cluster is threaded in so the retry
    # reproduces THAT cluster's build under THAT env — the global `_cargo_env()` would
    # use whatever `_current_cluster` is stale-set to, so a cluster-cfg-specific compile
    # error (e.g. nightly-only) would not reproduce and BUILD mode would silently bail.
    _cluster_label = cluster.name if cluster is not None else "task3"
    if cluster is not None:
        _parts = ["cargo"]
        if cluster.toolchain:
            _parts.append(f"+{cluster.toolchain}")
        _parts += ["test", "--no-fail-fast"]
        test_command = shlex.join(_parts)

    def _env() -> dict:
        # Mirror task3's per-cluster invocation: base cargo env, then the cluster's
        # env_extra overlaid (its RUSTFLAGS/cfg win over the stale global cluster's).
        e = _cargo_env()
        if cluster is not None and cluster.env_extra:
            e.update(cluster.env_extra)
        return e

    max_stalls = int(os.getenv("TASK3_INLINE_MAX_STALLS", "3"))
    hard_cap = int(os.getenv("TASK3_INLINE_HARD_CAP", "10"))

    # R9.1: two modes. TEST mode (some test FAILED) attributes via the test
    # reverse-map/classifier. BUILD mode (zero FAILED lines because the tree did
    # not COMPILE) attributes via structured cargo diagnostics and fixes the fns
    # the compile errors point at — TASK 4 (a test-failure analyzer) cannot, since
    # it has no failing-test identifiers. compile_cmd compiles the tests without
    # running them so a 0-test "incomplete" cluster yields real error diagnostics.
    failed = sorted(_parse_failed_tests(initial_test_output))
    compile_cmd = test_command if "--no-run" in test_command else test_command + " --no-run"
    mode = RetryMode.BUILD if not failed else RetryMode.TEST
    build_mode = mode is RetryMode.BUILD
    if build_mode:
        _, _, _init_diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        if not _init_diags:
            # No FAILED tests AND no reproducible compile errors under this env
            # (e.g. the failure is cluster-cfg-specific) — nothing to act on here.
            return False, initial_test_output
        initial_count = len(_init_diags)
        _log(f"  [task3-retry] build-failure mode: {initial_count} compile error(s).")
    else:
        initial_count = len(failed)
        _log(f"  [task3-retry] entering inline retry: {initial_count} failing test(s).")

    # Snapshot every .rs under src/ (the files a retry might touch; tests/ is
    # excluded by the root → cheap) so a regressing retry can be reverted.
    src_root = project_dir / "src"
    snapshot = SourceSnapshot.capture(src_root)

    # P5.1 parity: fix prompts here previously carried no TYPES_IN_SCOPE and
    # no container — the model had to guess struct fields. Enumerate from the
    # current (reconstructed) src; cluster-filter so only methods that compile
    # under THIS cluster are advertised (Hard Rule 8).
    _retry_type_recs = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(src_root) or [], cluster)
    _retry_containers = _fn_container_index(src_root)
    # Model-notes recall + relevant tests (slice 8a improvement): these fix
    # prompts previously carried neither. Resolve the module FR doc when
    # unambiguous (single-module case: exactly one functional_requirements_*.md;
    # multi-module would need a per-file module map — left as today's None).
    _fr_candidates = sorted(project_dir.glob("functional_requirements_*.md"))
    _retry_fr_path = _fr_candidates[0] if len(_fr_candidates) == 1 else None
    _retry_tests_dir_files = list((project_dir / "tests").rglob("*.rs"))

    def _apply_per_fn_fixes(refs: list[tuple[str, str]], output: str) -> int:
        # Re-ask the model for each (file, fn) body and splice it. Frozen tests
        # are never rewritten — skip tests/ and gen_tests siblings even if a
        # compile error's span lands inside one (Hard Rule: tests are frozen).
        fixed_count = 0
        for rel_f, fn in refs:
            if rel_f.startswith("tests/") or _is_gen_tests_file(Path(rel_f).name):
                continue
            fp = project_dir / rel_f
            if not fp.exists():
                continue  # classifier may have hallucinated a path
            try:
                cur_text = fp.read_text(encoding="utf-8")
            except Exception:
                continue
            sloc = _locate_fn(cur_text, fn)
            if sloc is None:
                continue
            sig_t = sloc[0]
            cur_body = cur_text[sloc[2]:sloc[3]]
            err_excerpt = _focus_cargo_errors(output, rel_f, max_chars=_FIX_ERR_CAP)
            # Container index keys are rel to src/, rel_f is rel to project_dir.
            _src_rel = rel_f[4:] if rel_f.startswith("src/") else rel_f
            _ctx = _gather_fn_fix_context(
                file_text=cur_text, file_rel=rel_f, fn_name=fn,
                sig_text=sig_t, body_text=cur_body,
                error_excerpt=err_excerpt,
                container=_retry_containers.get((_src_rel, fn), ""),
                type_records=_retry_type_recs,
                fr_path=_retry_fr_path,
            )
            t_blocks = _find_tests_for_fn(
                _retry_tests_dir_files, fn, file_rel=rel_f,
                src_dir=src_root, container=_ctx.container)
            prompt = _build_per_fn_fix_prompt(
                file_text=cur_text, fn_name=fn, sig_text=sig_t,
                test_blocks=t_blocks, module_name=project_dir.name,
                cluster_name=_cluster_label, file_rel=rel_f,
                prev_body=cur_body, error_excerpt=err_excerpt,
                sections=PerFnFixSections(
                    container=_ctx.container, types=_ctx.types_section,
                    notes=_ctx.notes_section,
                    # P7: the task3-retry path injects none of these (silent "").
                    macros="", invariants="", retrospective_guidance="",
                    siblings="", crate_idioms=""),
                fr_path=_retry_fr_path,  # P6.4 past-note recall by error code
            )
            new_body, _ = _ask_per_fn_body(prompt, fn, [], sig_text=sig_t)
            if not new_body:
                continue
            # allow_nonstub: a build/test failure comes from a fn with a WRONG,
            # already-filled (non-stub) body — like every other fix-loop site
            # (cf. 17001/17100). A bare `unimplemented!()` stub compiles, so it
            # never causes the failure; without allow_nonstub `_splice_fn_body`
            # refused every real target and the inline retry was a silent no-op.
            ok2, _ = _splice_fn_body(fp, fn, new_body, allow_nonstub=True)
            if ok2:
                fixed_count += 1
                _log(f"  [task3-retry] re-spliced {rel_f}::{fn}.")
        return fixed_count

    def _run_check_test() -> tuple[bool, str, int]:
        rc, out = _run_cargo(test_command, project_dir, _env(),
                             timeout=TEST_TIMEOUT)
        f = _parse_failed_tests(out)
        return rc == 0, out, len(f)

    def _run_check_build() -> tuple[bool, str, int]:
        rc, rendered, diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        # ok = the tree COMPILES; metric = remaining compile errors (lower better).
        return rc == 0, _strip_ansi(rendered), len(diags)

    def _do_fix_test(output: str, history: list[dict]) -> dict:
        # FR-024: full attribution chain. The classifier is one step in a
        # longer chain — when it returns no actionable refs, fall back to
        # the test-reverse-map. Previously this gave up at classifier
        # exhaustion; now it has more candidates to try.
        fails_now = sorted(_parse_failed_tests(output))
        if not fails_now:
            return {"abort": True, "reason": "no_failed_tests"}
        try:
            tests_dir_files = list((project_dir / "tests").rglob("*.rs"))
            test_reverse_map = _build_test_reverse_map(
                tests_dir_files, project_dir)
        except Exception:
            test_reverse_map = {}
        frontier, _kind = _attribute_failures(
            output, project_dir,
            recently_filled=[],  # task3 has no per-cluster fill history
            test_reverse_map=test_reverse_map,
            cluster_name=_cluster_label,
        )
        frontier = _filter_test_targets(frontier, project_dir)
        refs: list[tuple[str, str]] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "no_actionable_refs"}
        fixed_count = _apply_per_fn_fixes(refs, output)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    def _do_fix_build(output: str, history: list[dict]) -> dict:
        _, _, diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        if not diags:
            return {"abort": True, "reason": "no_compile_errors"}
        frontier = _filter_test_targets(_frontier_from_diags(diags, project_dir), project_dir)
        refs: list[tuple[str, str]] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "unattributed_compile_errors"}
        fixed_count = _apply_per_fn_fixes(refs, output)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    passed, last_out, history = _improvement_loop(
        name="task3.inline_retry",
        run_check=_run_check_build if build_mode else _run_check_test,
        do_fix=_do_fix_build if build_mode else _do_fix_test,
        max_stalls=max_stalls,
        hard_cap=hard_cap,
    )

    if build_mode:
        # Re-measure compile errors. Revert if the retry INCREASED them; recurse
        # (changed output) only on strict progress so task3's re-verification
        # terminates — fewer errors each round, or back to the unchanged input.
        _, _, fin_diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        final_err = len(fin_diags)
        verdict = build_verdict(initial_errors=initial_count,
                                final_errors=final_err, has_snapshot=bool(snapshot))
        if verdict is RetryVerdict.REVERT:
            _log(f"  [task3-retry] build-fix worsened compile ({final_err} > "
                 f"{initial_count}) — reverting from snapshot.")
            snapshot.restore()
            return False, initial_test_output
        if verdict is RetryVerdict.RECURSE:
            _log(f"  [task3-retry] build-fix progress: {final_err} compile "
                 f"error(s) (initial {initial_count}) — re-running TASK 3.")
            return False, last_out
        _log(f"  [task3-retry] build-fix made no progress ({final_err} error(s)) "
             "— escalating to TASK 4.")
        return False, initial_test_output

    final_failed = _parse_failed_tests(last_out)
    if passed:
        _log(f"  [task3-retry] tests now PASS after {len(history)} iter(s).")
        return True, last_out
    # Decide whether to keep partial progress or revert. R9.2: a non-compiling tree
    # emits NO `FAILED` lines, so `len(final_failed)` reads as 0 ("near success") even
    # when a re-splice broke the build. Compile/ran-state is the real signal — if the
    # tree ran tests before the retry but no longer does, the retry made it STRICTLY
    # WORSE (a tree that won't compile is worse than N failing tests) → revert.
    init_p, init_f, _ = _parse_test_counts(initial_test_output)
    fin_p, fin_f, _ = _parse_test_counts(last_out)
    init_ran = (init_p + init_f) > 0
    final_ran = (fin_p + fin_f) > 0
    verdict = failing_test_verdict(
        passed=False, initial_failures=initial_count,
        final_failures=len(final_failed), initially_ran=init_ran,
        finally_ran=final_ran, has_snapshot=bool(snapshot))
    if verdict is RetryVerdict.REVERT:
        reason = ("retry broke the build (tests no longer run)"
                  if (init_ran and not final_ran)
                  else f"result {len(final_failed)} > initial {initial_count}")
        _log(f"  [task3-retry] {reason} — reverting from snapshot.")
        snapshot.restore()
        return False, initial_test_output
    _log(f"  [task3-retry] stopped at {len(final_failed)} failing tests "
         f"(initial {initial_count}). Keeping partial progress.")
    return False, last_out


def _emit_per_file_test_overview(project_dir: Path, output: str, *, context: str) -> None:
    """Attribute the latest cargo-test output to files (domain
    per_file_test_stats; name-based, interleaving-proof) and publish the
    per-file pass/total table to the log + a tests.file_stats event, which
    the monitor merges into its compile overview. No test lines → no-op."""
    try:
        project_dir = Path(project_dir)
        known = {
            str(p.relative_to(project_dir)).replace(os.sep, "/")
            for d in ("src", "tests") if (project_dir / d).is_dir()
            for p in (project_dir / d).rglob("*.rs")
        }
        texts: dict[str, str] = {}
        if (project_dir / "tests").is_dir():
            for p in (project_dir / "tests").rglob("*.rs"):
                with suppress(OSError):
                    texts[str(p.relative_to(project_dir)).replace(os.sep, "/")] = (
                        p.read_text(encoding="utf-8", errors="replace")
                    )
        stats = per_file_test_stats(output, known, build_test_name_index(texts))
    except Exception as exc:
        _log(f"  [test overview] skipped: {exc}")
        return
    if not stats:
        return
    ordered = sorted(stats.items(), key=lambda kv: (-kv[1]["failed"], kv[0]))
    tot_p = sum(s["passed"] for _, s in ordered)
    tot_t = sum(s["passed"] + s["failed"] for _, s in ordered)
    _log(f"  [test overview] {context} — {tot_p}/{tot_t} tests passing:")
    name_w = max(len(f.split("/")[-1]) for f, _ in ordered)
    for f, s in ordered:
        short = f.split("/")[-1]
        total = s["passed"] + s["failed"]
        mark = "✓" if s["failed"] == 0 and total else "✗"
        ign = f"  ({s['ignored']} ignored)" if s["ignored"] else ""
        _log(f"    {short.ljust(name_w)}  {mark} tests {s['passed']}/{total}{ign}")
    _emit("tests.file_stats", data={
        "context": context, "passed": tot_p, "total": tot_t,
        "files": [{"file": f, **s} for f, s in ordered],
    })


def task3(project_dir, test_command, iteration, output_dir, genesis_path):
    _emit("task.start", task="task3")  # debt#7: structured stage event
    # slice 5b: extracted to pipeline_core.application.tasks.task3; thin shim binding deps.
    passed, test_output = _task3_impl(
        project_dir, test_command, iteration, output_dir, genesis_path, _TASK3_DEPS
    )
    _emit("task.done", task="task3", data={"passed": bool(passed)})  # debt#7: aggregate verdict
    _emit_per_file_test_overview(
        Path(project_dir), extract_test_output_from_file(test_output),
        context=f"task3 iter {iteration}",
    )
    return passed, test_output


def _task3_legacy_single(
    project_dir, test_command, iteration, output_dir, genesis_path,
) -> tuple[bool, str]:
    """Pre-FR-024 single-shot path. Retained for pipelines without baked
    clusters (non-skeleton backends, plain-cargo projects). Uses the
    discovered `test_command` and the lenient `validate_task3_output`
    classifier. New skeleton-backend runs never hit this path."""
    output_file = Path(output_dir) / f"tests_output_{iteration}.txt"
    env = _cargo_env()
    _attempt = iteration if isinstance(iteration, int) else None

    with _phase("task", task="task3", attempt=_attempt,
                data={"command": test_command, "iteration": iteration}) as done:
        _emit("test.start", attempt=_attempt, data={"command": test_command})
        _t_start = time.monotonic()
        try:
            result = subprocess.run(
                _parse_cmd(test_command),
                cwd=project_dir, capture_output=True, text=True,
                timeout=TEST_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            msg = f"TIMEOUT: tests exceeded {TEST_TIMEOUT} seconds."
            genesis = read(genesis_path)
            write(output_file,
                  f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
            _log(f"  {msg}")
            _emit("test.fail",
                  duration_ms=int((time.monotonic() - _t_start) * 1000),
                  data={"reason": "timeout", "timeout_s": TEST_TIMEOUT})
            done["data"] = {"passed": False, "reason": "timeout"}
            return False, msg
        except FileNotFoundError as e:
            msg = (f"ERROR: test command not found: {e}\n"
                   f"Command was: {test_command}\n"
                   "Ensure the required runtime is installed and on PATH.")
            genesis = read(genesis_path)
            write(output_file,
                  f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
            _log(f"  {msg}")
            _emit("test.fail",
                  duration_ms=int((time.monotonic() - _t_start) * 1000),
                  data={"reason": "command_not_found", "error": str(e)[:200]})
            done["data"] = {"passed": False, "reason": "command_not_found"}
            sys.exit(1)

        test_output = result.stdout
        if result.stderr:
            test_output += "\n--- stderr ---\n" + result.stderr
        genesis = read(genesis_path)
        write(output_file,
              f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{test_output}")
        passed   = result.returncode == 0
        _pass_n, _fail_n, _ = _parse_test_counts(test_output)
        _log(f"  Tests {'PASSED' if passed else 'FAILED'}. Output -> {output_file}")
        _emit("test.pass" if passed else "test.fail",
              duration_ms=int((time.monotonic() - _t_start) * 1000),
              data={"returncode": result.returncode,
                    "pass_count": _pass_n, "fail_count": _fail_n,
                    "output": _store_prompt_content(test_output)})
        if not passed:
            retry_passed, retry_output = _task3_inline_retry(
                project_dir=project_dir,
                test_command=test_command,
                iteration=iteration,
                initial_test_output=test_output,
            )
            if retry_passed:
                passed = True
                test_output = retry_output
                write(output_file,
                      f"=== genesis.md (iteration {iteration}) ===\n{genesis}"
                      f"\n{OUTPUT_SEPARATOR}\n{test_output}")
                _pass_n, _fail_n, _ = _parse_test_counts(test_output)
                _log(f"  Tests PASSED via inline retry. Output -> {output_file}")
                _emit("test.pass", duration_ms=int(
                    (time.monotonic() - _t_start) * 1000),
                      data={"returncode": 0, "pass_count": _pass_n,
                            "fail_count": _fail_n, "via": "task3_inline_retry",
                            "output": _store_prompt_content(test_output)})
            elif retry_output != test_output:
                test_output = retry_output
                write(output_file,
                      f"=== genesis.md (iteration {iteration}) ===\n{genesis}"
                      f"\n{OUTPUT_SEPARATOR}\n{test_output}")
                _pass_n, _fail_n, _ = _parse_test_counts(test_output)
        done["data"] = {"passed": passed, "pass_count": _pass_n, "fail_count": _fail_n}
        return passed, test_output


def task4(genesis_path, functional_req_path, test_output, iteration, output_dir,
          failure_type: str = "test", failure_log: str = "",
          prev_pass_count: int = 0, prev_test_output: str = "",
          source_context: str = "", api_surface: str = ""):
    _emit("task.start", task="task4")  # debt#7: structured stage event
    # slice 5b: extracted to pipeline_core.application.tasks.task4; thin shim binding deps.
    return _task4_impl(
        genesis_path, functional_req_path, test_output, iteration, output_dir, _TASK4_DEPS,
        failure_type=failure_type, failure_log=failure_log,
        prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
        source_context=source_context, api_surface=api_surface,
    )


def _task4_extract_and_store_rules(project_dir: Path, genesis_text: str,
                                    module_scope: str, failure_type: str,
                                    iteration: int) -> None:
    """BL-024: Parse bullet-point rules from genesis.md and write to genesis_rules table."""
    # Map failure type to category tag.
    category = {
        "build": "compile",
        "assertion": "assertion",
        "panic": "runtime_panic",
        "timeout": "timeout",
        "no_tests": "test_discovery",
    }.get(failure_type, "general")

    # Extract lines that look like rules (bullet points or numbered items).
    rule_re = re.compile(r'^\s*[-*•]\s+(.+)$|^\s*\d+\.\s+(.+)$', re.MULTILINE)
    rules: list[str] = []
    for m in rule_re.finditer(genesis_text):
        rule = (m.group(1) or m.group(2) or "").strip()
        if len(rule) > 20:  # skip trivially short lines
            rules.append(rule)

    # Store unique rules (deduplicate by content prefix).
    stored_prefixes: set[str] = set()
    for rule in rules[:20]:  # cap at 20 per iteration
        prefix = rule[:40]
        if prefix not in stored_prefixes:
            stored_prefixes.add(prefix)
            genesis_rules_write(project_dir, module_scope, category, rule, iteration)

    if rules:
        _log(f"  [BL-024] Stored {len(stored_prefixes)} structured rules "
             f"(category: {category}) from genesis update.")


# ---------------------------------------------------------------------------
# TASK 1.5 — Static use-statement audit (no LLM)
# ---------------------------------------------------------------------------

def task1_static_audit(functional_req_path: Path, test_files: list) -> list[str]:
    """
    Parse every test file for `use` statements and check that each imported
    symbol path appears in functional_requirements.md.

    Returns a (possibly empty) list of symbol paths that are absent from the
    requirements. Callers should log a warning and optionally re-run TASK 1
    on the missing symbols.

    No LLM call — structured `use`-path scan. Runs in milliseconds.
    """
    SKIP_PREFIXES = ('std::', 'core::', 'alloc::', 'super::', 'self::', 'crate::')

    reqs = read(functional_req_path)
    if not reqs.strip():
        return []

    found: set[str] = set()
    for _rel, content in test_files:
        # use foo::bar::{A, B, C};  or  use foo::bar::Baz;
        for raw in use_symbol_paths(content):
            if '::' not in raw:
                continue
            if '::{' in raw:
                prefix, items_str = raw.split('::{', 1)
                for item in items_str.rstrip('}').split(','):
                    item = item.strip()
                    if item:
                        found.add(f"{prefix}::{item}")
            else:
                found.add(raw)

    gaps = [
        sym for sym in sorted(found)
        if not any(sym.startswith(p) for p in SKIP_PREFIXES)
        and sym not in reqs
    ]

    _log(f"\nTASK 1.5: {len(found)} use-paths in tests, {len(gaps)} missing from requirements.")
    if gaps:
        for g in gaps[:10]:
            _log(f"  missing: {g}")
        if len(gaps) > 10:
            _log(f"  ... and {len(gaps) - 10} more")
    return gaps


# ---------------------------------------------------------------------------
# Genesis audit — detect and resolve contradictions in genesis.md
# ---------------------------------------------------------------------------

# Run a genesis audit every this many total module iterations (across all modules).
GENESIS_AUDIT_INTERVAL = 5

# After this many consecutive module iterations with no pass-count improvement,
# escalate to whole-project context in TASK 2.
NO_IMPROVEMENT_THRESHOLD = 3


# slice 5b: genesis audit extracted to pipeline_core.application.tasks.genesis_audit.
# Deps bind the pipeline's collaborators once; main() calls the shim below unchanged.
_GENESIS_AUDIT_DEPS = GenesisAuditDeps(
    ask=ask, read=read, write=write, model=_TASK4_MODEL,
    verbosity_guard=_TASK4_VERBOSITY_GUARD,
)


def task_genesis_audit(genesis_path: Path) -> bool:
    return genesis_audit(genesis_path, _GENESIS_AUDIT_DEPS)


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
_TASK3_DEPS = Task3Deps(
    phase=_phase,
    load_baked_clusters=_load_baked_clusters,
    task3_legacy_single=_task3_legacy_single,
    cargo_env=_cargo_env,
    parse_cmd=_parse_cmd,
    count_active_tests_for_cluster=_count_active_tests_for_cluster,
    store_prompt_content=_store_prompt_content,
    task3_inline_retry=_task3_inline_retry,
    read=read,
    write=write,
    test_timeout=TEST_TIMEOUT,
)
_TASK4_DEPS = Task4Deps(
    ask=ask,
    read=read,
    write=write,
    extract_and_store_rules=_task4_extract_and_store_rules,
    model=MODEL,
    task4_model=_TASK4_MODEL,
    verbosity_guard=_TASK4_VERBOSITY_GUARD,
    intermediate_char_limit=INTERMEDIATE_CHAR_LIMIT,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
)


def _describe_test_environment() -> str:
    """Return a prompt-ready description of the cargo subprocess env.

    The pipeline knows which env vars and cfg predicates are set for the
    test process; the LLM doesn't. Without this signal, the LLM can propose
    strategies that the pipeline knows can't work — for example writing tests
    that try to enable a feature whose env var was never set in the parent
    process. Describe the env factually; let the LLM draw its own implications.

    Generic across projects: only states what IS / IS NOT set + the active
    test runner. No project-specific reasoning is baked in.
    """
    env = _cargo_env()

    # Variables the LLM should always see if present.
    visible = ("RUSTFLAGS", "CARGO_TERM_COLOR", "RUSTUP_TOOLCHAIN")
    # Variables whose ABSENCE is informative — std libs commonly gate behavior
    # on these and cache the lookup at process start, so per-test set_var
    # doesn't help.
    notable = ("RUST_BACKTRACE", "RUST_LIB_BACKTRACE", "RUST_LOG")

    lines = [
        "--- Test environment (cargo subprocess under coverage instrumentation) ---",
    ]
    for k in visible:
        if k in env:
            lines.append(f"  {k} = {env[k]!r}")
    unset = [k for k in notable if k not in env]
    if unset:
        lines.append(f"  NOT SET in parent process: {', '.join(unset)}")
        lines.append(
            "  Code paths whose execution depends on these variables (env-gated "
            "logging, backtrace capture, etc.) are unreachable from this test run."
        )

    if _SCOPE.active is not None:
        active = ", ".join(_SCOPE.active.active_cfg) if _SCOPE.active.active_cfg else "(none)"
        excluded = ", ".join(_SCOPE.active.excluded_cfg) if _SCOPE.active.excluded_cfg else "(none)"
        lines.append(f"  active cfg predicates: {active}")
        lines.append(f"  excluded cfg predicates: {excluded}")

    lines.append("  test runner: `cargo test --test-threads=1` (under cargo-llvm-cov instrumentation)")
    return "\n".join(lines) + "\n"


def _crate_name_from_toml(module_dir: Path, fallback: str) -> str:
    """Extract `name` from Cargo.toml and normalise hyphens to underscores."""
    toml = module_dir / "Cargo.toml"
    if toml.exists():
        name = _cargo_package_name(toml.read_text())
        if name:
            return name.replace("-", "_")
    return fallback.replace("-", "_")


# Slice 4b: coverage I/O relocated to pipeline_core.adapters.coverage. _COV_DEPS injects
# the pipeline's log + cargo-env + live-cluster accessor + tool paths/timeout/pragma so the
# adapter stays import-independent of pipeline.py. `cluster` is a callable (the active
# cluster is reassigned across the run; resolve at call time = original global semantics).
_COV_DEPS = CoverageDeps(
    log=_log,
    cargo_env=_cargo_env,
    cluster=_SCOPE.get_active,
    tarpaulin_bin=TARPAULIN_BIN,
    grcov_bin=GRCOV_BIN,
    build_timeout=BUILD_TIMEOUT,
    ignore_pragma=COVERAGE_IGNORE_PRAGMA,
)


def _run_tarpaulin(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
    # Thin shim until call sites migrate (slice 5).
    return _covmod.run_tarpaulin(_COV_DEPS, project_dir, module_name)


def _quick_file_coverage(project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
    """Per-file coverage snapshot. Delegates to the active CoverageProvider."""
    return _coverage_provider.measure_file(project_dir, module_name, rel_path)


def _measure_coverage(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
    _t0 = time.monotonic()
    pct, files_data, covered, coverable = _coverage_provider.measure(project_dir, module_name)
    _emit("coverage.measured",
          module=module_name,
          cluster=_SCOPE.active.name if _SCOPE.active is not None else None,
          duration_ms=int((time.monotonic() - _t0) * 1000),
          data={"pct": pct, "covered": covered, "coverable": coverable,
                "backend": COVERAGE_BACKEND, "file_count": len(files_data)})
    return pct, files_data, covered, coverable


# ── Coverage provider (relocated to pipeline_core.adapters.coverage, slice 4b) ──
# Built from _COV_DEPS at the composition root; selection keyed on COVERAGE_BACKEND.
_coverage_provider: _covmod.CoverageProvider = _covmod.make_coverage_provider(
    COVERAGE_BACKEND, _COV_DEPS,
)


# ─────────────────────────────────────────────────────────────────────────────
# gen_tests sibling-file storage
#
# Each LLM gen_tests pass for src/foo.rs writes its module body to a sibling
# file src/foo.gen_tests_N.rs, and adds a one-line path-mod declaration to
# foo.rs:
#     #[cfg(test)] #[allow(...)] #[path = "foo.gen_tests_N.rs"] mod gen_tests_N;
#
# This avoids coverage-tool contamination (test code lives in its own file,
# excluded by --ignore-filename-regex) and removes the need for GRCOV_EXCL_*
# markers wrapping inline test blocks.
# ─────────────────────────────────────────────────────────────────────────────

def _gen_tests_siblings(src_file: Path) -> list[tuple[int, Path]]:
    """Return [(N, path)] for all foo.gen_tests_N.rs siblings of src_file, sorted by N."""
    stem = src_file.stem
    pattern = re.compile(rf'^{re.escape(stem)}\.gen_tests_(\d+)\.rs$')
    out: list[tuple[int, Path]] = []
    parent = src_file.parent
    if parent.exists():
        for f in parent.iterdir():
            m = pattern.match(f.name)
            if m:
                out.append((int(m.group(1)), f))
    out.sort()
    return out


def _gen_tests_count(src_file: Path) -> int:
    return len(_gen_tests_siblings(src_file))


def _next_gen_tests_n(src_file: Path) -> int:
    """Return the next free gen_tests pass number for src_file.
    Uses max(existing) + 1 to skip over gaps from deleted siblings."""
    siblings = _gen_tests_siblings(src_file)
    return (siblings[-1][0] + 1) if siblings else 1


def _gen_tests_path(src_file: Path, n: int) -> Path:
    return src_file.with_name(f"{src_file.stem}.gen_tests_{n}.rs")


def _read_all_gen_tests(src_file: Path) -> str:
    """Concatenate gen_tests sibling bodies, each re-wrapped in `mod gen_tests_N { ... }`
    for LLM prompt context (so the LLM sees the same shape it generates)."""
    parts: list[str] = []
    for n, sibling in _gen_tests_siblings(src_file):
        try:
            body = sibling.read_text(encoding="utf-8").rstrip()
        except OSError:
            continue
        parts.append(f"#[cfg(test)]\nmod gen_tests_{n} {{\n{body}\n}}")
    return "\n\n".join(parts)


def _strip_gen_tests_decls(src_text: str) -> str:
    """Remove all gen_tests path-mod decls (R7: generation.strip_all_gen_tests_decls)."""
    return _strip_all_gen_tests_decls(src_text)


def _is_gen_tests_file(name: str) -> bool:
    """True if `name` is a pipeline-generated `*.gen_tests_<N>.rs` sibling."""
    return _match_gen_tests_sibling(name) is not None


# Sibling-name parser moved to discovery (P5); shim keeps the private name.
_match_gen_tests_sibling = _discovery.match_gen_tests_sibling


def _strip_gen_tests_decl_n(parent_text: str, pass_n: int) -> str:
    """Remove the gen_tests_<N> decl for one pass (R7: generation.strip_gen_tests_decl_n)."""
    return _domain_strip_gen_tests_decl_n(parent_text, pass_n)


def _error_signatures_from_output(cargo_output: str) -> frozenset:
    """Stable rustc error signatures (R7: test_results.rustc_error_signatures)."""
    return rustc_error_signatures(cargo_output)


# Prelude imports injected into generated test modules when absent. Each row is
# (import_stmt, accepted `use`-prefixes, required symbol token): present when a
# `use <prefix>…<symbol>…` statement exists (R7: rust_scan.use_imports_symbol).
_GEN_PRELUDE_ITEMS: list[tuple[str, tuple[str, ...], str]] = [
    ("use std::string::{String, ToString};", ("std::", "alloc::"),      "String"),
    ("use std::vec::Vec;",                    ("std::", "alloc::"),      "Vec"),
    ("use std::boxed::Box;",                  ("std::", "alloc::"),      "Box"),
    ("use std::format;",                      ("std::", "alloc::"),      "format"),
    ("use std::error::Error as StdError;",    ("std::error::Error",),    "Error"),
    ("use std::fmt::{self, Display, Debug};", ("std::fmt", "core::fmt"), "fmt"),
]


def _inject_allow_before_mod(block: str, allow_line: str, *, gen_only: bool) -> str:
    """Insert `allow_line` before the first `[gen_]mod` opener (R7: find_mod_opener)."""
    if allow_line in block:
        return block
    opener = find_mod_opener(block, gen_only=gen_only)
    if opener is None:
        return block
    return block[:opener[0]] + f"{allow_line}\n" + block[opener[0]:]


def _inject_allow_before_gen_mod(block: str, allow_line: str) -> str:
    return _inject_allow_before_mod(block, allow_line, gen_only=True)


def _inject_allow_before_any_mod(block: str, allow_line: str) -> str:
    return _inject_allow_before_mod(block, allow_line, gen_only=False)


def _inject_gen_prelude_imports(block: str) -> str:
    """Inject missing std prelude imports into a generated test module (R7: rust_scan)."""
    missing = [stmt for stmt, prefixes, symbol in _GEN_PRELUDE_ITEMS
               if not use_imports_symbol(block, prefixes, symbol)]
    if not missing:
        return block
    inject = "\n    ".join(missing)
    if "use super::*;" in block:
        return block.replace("use super::*;", f"use super::*;\n    {inject}", 1)
    opener = find_mod_opener(block, gen_only=True)
    if opener is None:
        return block
    insert_at = after_brace_newline(block, opener[1])  # after `{ … \n`
    if insert_at is None:
        return block
    return block[:insert_at] + f"    {inject}\n" + block[insert_at:]


def _count_test_attrs(block: str) -> int:
    """Count `#[test]` attrs (R7: domain.rust_scan.count_test_attrs)."""
    return count_test_attrs(block)


def _strip_trailing_file_tag(text: str) -> str:
    """Strip a trailing `</FILE>` tag (R7: domain.text.strip_trailing_file_tag)."""
    return strip_trailing_file_tag(text)


def _block_is_prose(block: str) -> bool:
    """True if a generated block looks like prose instructions, not code. (Regex in pipeline.)"""
    return bool(re.search(r'block to our Rust file|Here\'s how|You can add|here is', block, re.IGNORECASE))


def _has_nonempty_test_body(block: str) -> bool:
    """True if the block has at least one `#[test]` fn with a non-trivial body."""
    return has_nonempty_test_fn(block)


def _flags_unreachable(text: str) -> bool:
    """True if the model declared the target genuinely uncoverable. (Regex in pipeline.)"""
    return bool(re.search(
        r'dead.code|unreachable|genuinely|not coverable|cannot be covered|grcov artifact',
        text, re.IGNORECASE,
    ))


def _parse_attempt_number(detail: str) -> "int | None":
    """Parse the `attempt N` counter from a saved phase detail string. (Regex in pipeline.)"""
    m = re.search(r"attempt (\d+)", detail)
    return int(m.group(1)) if m else None


def _is_structural_line(text: str) -> bool:
    """True for structural-only Rust lines (`}`, `} else {`, etc.) that LLVM miscounts.
    (Regex in pipeline.)"""
    return is_structural_brace_line(text)


# slice 5b: deps for the extracted TASK 0b discovery (defined after its parsers).
_TASK0B_DEPS = Task0bDeps(
    ask=ask,
    write=write,
    is_test_file=is_test_file,
    is_gen_tests_file=_is_gen_tests_file,
    strip_gen_tests_decls=_strip_gen_tests_decls,
    condense_rust_to_signatures=_condense_rust_to_signatures,
    manifest_name=MANIFEST_NAME,
    sibling_cap=_SIBLING_CAP,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    task0_model=_TASK0_MODEL,
)

_DISCOVER_TEST_CMD_DEPS = DiscoverTestCommandDeps(
    ask=ask,
    task0_model=_TASK0_MODEL,
    model_name=MODEL,
    extra_cap=_EXTRA_CAP,
    sys_exit=sys.exit,
)


def _has_gen_tests_decl(src_text: str, pass_n: int) -> bool:
    """True if src_text contains a `mod gen_tests_<pass_n>;` declaration.
    Used by the TASK 2 reattach path to detect when a sibling exists on disk
    but its declaration was dropped during regeneration of the source file."""
    return bool(re.search(rf'\bmod\s+gen_tests_{pass_n}\s*;', src_text))


def _is_gen_tests_decl_only_src(content: str) -> bool:
    """True if content consists only of gen_tests path-mod decls + whitespace + comments.

    After strip, a source file like src/lib.rs is reduced to its `#[cfg(test)]
    #[path = "..."] mod gen_tests_N;` declarations -- the rest is removed by
    extract_rust_tests. Those declaration-only files leak into TASK 1's input
    because they contain `#[cfg(test)]`, but they carry no test semantics
    (the actual tests live in the sibling files referenced by the decls).
    """
    if not content.strip():
        return True
    no_decls = _strip_gen_tests_decls(content)
    # Strip line and block comments before checking for remaining code.
    return strip_line_block_comments(no_decls).strip() == ""


def _cluster_cfg_attr(cluster) -> str:
    """Return the cfg predicate string that gates code to this cluster.

    Each active cfg `N` becomes `any(N, feature = "N")` (FR-032): a cluster may
    be switched on by either `--cfg N` (RUSTFLAGS) or `--features N`, and this
    gate must be true under whichever form the cluster uses. Multiple flags
    combine via `all(...)`.

    The default (stable) cluster is gated by the NEGATION of every other
    cluster's flags: stable's gen_tests were only ever validated under the
    default build, and ungated they compile under nightly's cfg-set, where
    they can break that cluster's harness (observed live: nightly task0c
    aborted at 0% on stable's siblings). No other clusters → no gate.
    """
    if cluster is None:
        return ""
    if getattr(cluster, "is_default", True):
        other_flags: set[str] = set()
        for c in _SCOPE.all:
            if c is cluster or getattr(c, "is_default", True):
                continue
            other_flags.update(getattr(c, "active_cfg", []) or [])
        if not other_flags:
            return ""
        terms = [t for f in sorted(other_flags)
                 for t in (f, f'feature = "{f}"')]
        return "not(any(" + ", ".join(terms) + "))"
    flags = list(getattr(cluster, "active_cfg", []) or [])
    if not flags:
        return ""
    terms = [f'any({f}, feature = "{f}")' for f in flags]
    if len(terms) == 1:
        return terms[0]
    return "all(" + ", ".join(terms) + ")"


def _cluster_gate_for_sibling(cluster, source_text: str, uncov_lines) -> str:
    """#26: the cfg gate to stamp on a gen_tests sibling. Gate it to its cluster
    only when the lines it targets are cluster-SPECIFIC (live inside a `#[cfg]`
    region that some cluster compiles out); a cluster-agnostic test is emitted
    UNGATED so a later cluster sees those lines already covered and skips
    re-augmenting them (kills the cross-cluster duplication). Only the default
    (stable) cluster shares — a non-default cluster keeps its gate (it runs after
    stable, so its agnostic lines are already covered; its real work is the gated
    delta). This is the documented-safe criterion: a `not(egma)` stable-only test
    stays gated to stable, never leaking into the nightly harness."""
    base_gate = _cluster_cfg_attr(cluster)
    if not base_gate or not getattr(cluster, "is_default", False):
        return base_gate
    contexts = [_cfg.CfgSet.of(cfg_true=c.cfg_true, cfg_false=c.cfg_false) for c in _SCOPE.all]
    if _cfg.lines_are_cluster_specific(source_text, set(uncov_lines or ()), contexts):
        return base_gate
    return ""   # cluster-agnostic → shareable across clusters


def _build_gen_tests_decl(sibling_name: str, n: "int | str", allow_line: str,
                           cluster_cfg_attr: str = "") -> str:
    """Build the one-line-ish `#[cfg(test)] ... mod gen_tests_N;` declaration.

    cluster_cfg_attr (optional): cfg predicate gating this gen_tests mod to a
    specific compiler cluster, e.g. "error_generic_member_access" for nightly.
    When provided, the declaration is wrapped in #[cfg(<attr>)] so the sibling
    file is silently excluded under any other cluster's flags. This is what
    keeps task0c's per-cluster tests from breaking the harness when the next
    cluster's pre-check runs.
    """
    lines: list[str] = []
    if cluster_cfg_attr:
        lines.append(f"#[cfg({cluster_cfg_attr})]")
    lines.append("#[cfg(test)]")
    if allow_line:
        lines.append(allow_line)
    lines.append(f'#[path = "{sibling_name}"]')
    return "\n" + "\n".join(lines) + f"\nmod gen_tests_{n};\n"


def _gen_tests_origin_cfgs(file_text: str) -> dict[int, str]:
    """Map each `mod gen_tests_<n>;` decl in `file_text` to the cluster cfg
    predicate that gates it — i.e. the `#[cfg(...)]` that is NOT `#[cfg(test)]`,
    as stamped by `_build_gen_tests_decl(cluster_cfg_attr=...)` at task0c gen
    time. Returns {pass_n: cfg_predicate}; '' when only `#[cfg(test)]` is present
    (stable/default cluster). Deterministic structural scan of the pipeline's own
    fixed decl format — not a source parse of arbitrary Rust."""
    lines = file_text.splitlines()
    out: dict[int, str] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if not (s.startswith("mod gen_tests_") and s.endswith(";")):
            continue
        try:
            n = int(s[len("mod gen_tests_"):-1])
        except ValueError:
            continue
        cfg = ""
        j = i - 1
        while j >= 0:
            a = lines[j].strip()
            if a.startswith("#["):
                if a.startswith("#[cfg(") and a.endswith(")]") and a != "#[cfg(test)]":
                    inner = a[len("#[cfg("):].rsplit(")]", 1)[0]
                    if inner != "test":
                        cfg = inner
                j -= 1
                continue
            if a == "":
                j -= 1
                continue
            break
        out[n] = cfg
    return out


def _load_gen_tests_origin(project_dir: Path) -> dict[str, str]:
    """Load the gen_tests origin-cfg manifest baked pre-strip. Keyed by sibling
    stem `"{parent_stem}.gen_tests_{n}"` → cluster cfg predicate ('' = default).
    Empty dict if absent (older runs / pre-bake)."""
    path = project_dir / ".skeleton" / "_gen_tests_origin.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def _propagate_or_quarantine_sibling(
    gt: Path, origin_manifest: dict, parent_stem: str, pass_n: int,
    log_fn=None, tag: str = "",
) -> bool:
    """R9.6: the origin manifest is authoritative for which gen_tests siblings are
    legitimate. Returns True if `gt` should be propagated (it is declared in a
    non-empty manifest, OR the manifest is empty/absent → legacy pre-bake run).

    If the manifest is NON-empty and this sibling is absent from it, it was never
    declared pre-strip — i.e. never coverage-validated by task0c. Resurrecting it
    under a guessed cfg gate is the latent cluster-leak class (a nightly-only orphan
    would E0658 in a stable cluster). Quarantine it to `.rs.broken` and return False.
    """
    key = f"{parent_stem}.gen_tests_{pass_n}"
    if origin_manifest and key not in origin_manifest:
        if not gt.name.endswith(".broken"):
            broken = gt.with_name(gt.name + ".broken")
            try:
                gt.rename(broken)
                if log_fn:
                    log_fn(f"  {tag} [skeleton-C] ⚠️  orphan gen_tests sibling "
                           f"'{gt.name}' absent from origin manifest (undeclared "
                           f"pre-strip) — quarantined → {broken.name}, not propagated.")
            except OSError as e:
                if log_fn:
                    log_fn(f"  {tag} [skeleton-C] orphan quarantine failed {gt.name}: {e}")
        return False
    return True


def _extract_gen_tests_body(block: str) -> str:
    """Strip the outer `mod gen_X { ... }` wrapper from an LLM-generated block and
    return the dedented body. If the shape doesn't match (e.g. LLM returned bare
    test functions), return block unchanged so caller can still use it."""
    m = re.search(
        r'(?:#\[[^\]]*\]\s*\n)*\s*(?:pub\s+)?mod\s+gen_\w+\s*\{',
        block,
    )
    if not m:
        return block.strip() + "\n"
    start = m.end()
    depth = 1
    i = start
    while i < len(block):
        c = block[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                body = block[start:i]
                lines = body.splitlines()
                non_empty = [ln for ln in lines if ln.strip()]
                if non_empty:
                    indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
                    if indent > 0:
                        body = "\n".join(
                            (ln[indent:] if len(ln) >= indent and ln[:indent].isspace() else ln)
                            for ln in lines
                        )
                return body.strip() + "\n"
        i += 1
    return block.strip() + "\n"


# _rust_delimiters_balanced relocated to pipeline_core.domain.rust_scan
# (regex-free char scanner; imported as the module-level name above).


def _write_gen_tests_pass(
    src_file: Path,
    src_text: str,
    block: str,
    allow_line: str,
    pass_n: int,
    cluster_cfg_attr: str = "",
) -> Path:
    """Write the sibling file with extracted body, and write src_file with the decl appended.

    `src_text` is the source content WITHOUT this pass's declaration (caller-managed
    revert target). `pass_n` is the 1-indexed pass number.
    `cluster_cfg_attr` (optional): cfg predicate used to scope this gen_tests
    mod to a specific compiler cluster — see _build_gen_tests_decl.
    Returns the sibling file path.

    Performs a delimiter-balance sanity check on the extracted body before
    committing. When the body has unbalanced `()`/`[]`/`{}` (a common LLM-
    corruption mode that propagates a syntax error into the test crate and
    breaks subsequent cargo invocations), the sibling is written for
    inspection but the parent src file is NOT updated with the decl —
    making the bad pass a no-op at compile time. Returns the sibling path
    regardless (caller's contract).
    """
    sibling = _gen_tests_path(src_file, pass_n)
    body = _extract_gen_tests_body(block)
    if not _rust_delimiters_balanced(body):
        # Quarantine: still write the file (for debugging) but do NOT attach
        # the decl to the parent. cargo will never see it; pipeline can
        # inspect it offline.
        quarantine = sibling.with_suffix(".rs.broken")
        quarantine.write_text(body, encoding="utf-8")
        with suppress(Exception):
            _log(f"  [gen_tests] REJECTED pass {pass_n} for {src_file.name}: "
                 f"delimiters unbalanced. Quarantined → {quarantine.name}")
        return sibling  # parent decl unchanged
    sibling.write_text(body, encoding="utf-8")
    decl = _build_gen_tests_decl(sibling.name, pass_n, allow_line,
                                  cluster_cfg_attr=cluster_cfg_attr)
    src_file.write_text(src_text.rstrip() + decl, encoding="utf-8")
    return sibling


def _quarantine_broken_gen_tests(project_dir: Path) -> int:
    """Scan all existing gen_tests siblings; rename any with unbalanced
    delimiters to `.rs.broken` and strip their `#[path] mod gen_tests_N`
    decl from the parent src file. Returns count of quarantined files.

    Runs at pipeline startup as a recovery for state inherited from
    earlier runs that committed broken gen_tests before this validation
    existed.
    """
    moved = 0
    for src in (project_dir / "src").rglob("*.gen_tests_*.rs"):
        try:
            text = src.read_text(encoding="utf-8")
        except Exception:
            continue
        if _rust_delimiters_balanced(text):
            continue
        # Identify the parent src file (foo.gen_tests_N.rs → foo.rs).
        parsed = _parse_gen_tests_filename(src.name)
        if parsed is None:
            continue
        stem, pass_n = parsed
        parent = src.parent / f"{stem}.rs"
        # Strip the decl block from the parent (best-effort, R7: structured scan).
        try:
            if parent.exists():
                ptext = parent.read_text(encoding="utf-8")
                ptext_new = _strip_gen_tests_decl(ptext, src.name, pass_n)
                if ptext_new != ptext:
                    parent.write_text(ptext_new, encoding="utf-8")
        except Exception:
            pass
        # Rename the broken file so cargo can't pick it up via stray paths.
        try:
            src.rename(src.with_suffix(".rs.broken"))
            moved += 1
        except Exception:
            try:
                src.unlink()
                moved += 1
            except Exception:
                pass
    if moved:
        _log(f"  [gen_tests] quarantined {moved} broken sibling(s) at startup.")
    return moved


def _revert_gen_tests_pass(src_file: Path, src_text: str, sibling: Path) -> None:
    """Restore src_file to src_text (no decl) and delete the sibling file.

    R9.6 atomicity: restoring `src_text` (a snapshot) can drop the decls of MORE
    than one sibling — e.g. a split/inner-retry added extra siblings since the
    snapshot. Unlinking only the named `sibling` would orphan the rest (file on
    disk, no decl). Enforce the invariant `sibling file ⟺ decl in parent`: after
    the restore, delete every sibling of this parent whose decl the restore removed.
    """
    src_file.write_text(src_text, encoding="utf-8")
    with suppress(OSError):
        sibling.unlink()
    for _n, _sib in _gen_tests_siblings(src_file):
        if not _has_gen_tests_decl(src_text, _n):
            with suppress(OSError):
                _sib.unlink()


def _strip_all_gen_tests_artifacts(src_file: Path) -> int:
    """Delete all gen_tests siblings and remove all decls from src_file.
    Returns the number of siblings deleted."""
    count = 0
    for _, sibling in _gen_tests_siblings(src_file):
        try:
            sibling.unlink()
            count += 1
        except OSError:
            pass
    if src_file.exists():
        original = src_file.read_text(encoding="utf-8")
        cleaned = _strip_gen_tests_decls(original)
        if cleaned != original:
            src_file.write_text(cleaned, encoding="utf-8")
    return count


def _extract_branch_guard(lines: list[str], line_no: int) -> str | None:
    """Walk backward from line_no to find the nearest enclosing if/match-arm/while condition.

    Returns a short string describing the condition that must be true for line_no to execute,
    or None if no guard is found within the search window.
    """
    for i in range(line_no - 2, max(-1, line_no - 30), -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        # Stop at function/impl/struct boundary — guard must be within the same function
        if is_rust_item_boundary_line(stripped):
            break
        label = branch_guard_label(stripped)
        if label is not None:
            return label
    return None


def _annotate_uncovered(content: str, uncovered: set[int]) -> str:
    """Return source annotated with '!!' markers on uncovered lines, including branch guards."""
    lines = content.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if i in uncovered:
            guard = _extract_branch_guard(lines, i)
            guard_hint = f" [guard: {guard}]" if guard else ""
            marker = f"  // !! UNCOVERED{guard_hint}"
        else:
            marker = ""
        out.append(f"{i:4d} | {line}{marker}")
    return "\n".join(out)


def _focused_annotation(content: str, uncovered: set[int], ctx: int = 4) -> str:
    """Show only uncovered lines plus `ctx` lines of surrounding context.

    Showing the full file floods the model's context window (225 lines = ~6k chars
    for argh_shared alone), leaving little room for the generated test block and
    causing truncated or hallucinated output. A focused view of just the uncovered
    regions keeps the prompt small and the types visible.
    """
    lines = content.splitlines()
    show: set[int] = set()
    for u in uncovered:
        for i in range(max(1, u - ctx), min(len(lines) + 1, u + ctx + 1)):
            show.add(i)
    result, prev = [], None
    for i in sorted(show):
        if prev is not None and i > prev + 1:
            result.append("     | ...")
        if i in uncovered:
            guard = _extract_branch_guard(lines, i)
            guard_hint = f" [guard: {guard}]" if guard else ""
            marker = f"  // !! UNCOVERED{guard_hint}"
        else:
            marker = ""
        result.append(f"{i:4d} | {lines[i - 1]}{marker}")
        prev = i
    return "\n".join(result)


def _select_relevant_siblings(
    target_rel: str,
    target_source: str,
    all_siblings: list[tuple[str, str]],
    project_dir: Path,
) -> list[tuple[str, str]]:
    """
    Python type-name overlap ranking — replaces the BL-014 LLM planner call.

    Algorithm:
    1. Extract CamelCase identifiers used in the target file (types, structs, etc.)
    2. For each sibling, count how many of those identifiers are *defined* there
       (defined = appears after struct/enum/trait/type/impl keyword)
    3. Return all siblings sorted by overlap count descending — the budget system
       decides how many to include.

    Zero LLM calls, deterministic, and empirically better for type-heavy files
    (e.g. error.rs uses Own/Mut/Ref → correctly ranks ptr.rs first).
    """
    if not all_siblings:
        return []

    # P7: CamelCase type-name + `kw Name` definition scans → pure tokeniser
    # (byte-equivalent), no regex on source.
    used_types: set[str] = camelcase_type_names(target_source)

    def _overlap(content: str) -> int:
        return len(used_types & defined_type_names(content))

    scored = sorted(
        ((srel, sc, _overlap(sc)) for srel, sc in all_siblings),
        key=lambda x: x[2],
        reverse=True,
    )

    top = [(srel, score) for srel, _sc, score in scored if score > 0][:4]
    _log(f"  [task0c planner] {target_rel}: ranked {len(all_siblings)} siblings by type overlap"
         f" (top: {top if top else 'none matched'})")
    _emit("task0c.planner", file=target_rel)  # debt#7: monitor file-progress source
    return [(srel, sc) for srel, sc, _score in scored]


def _extract_uncovered_functions(content: str, uncovered: set[int], max_fns: int = 0) -> str:
    """Extract complete bodies of functions that contain uncovered lines.

    The model needs the full function body — not just a narrow annotation — to write
    a meaningful behavioral assertion. Seeing the full logic lets it know what the
    function returns and under what conditions.

    Falls back to _focused_annotation if no function bodies can be identified.
    """
    if not uncovered:
        return _focused_annotation(content, uncovered)

    code_lines = content.splitlines()
    n = len(code_lines)
    result_blocks: list[str] = []

    i = 0
    while i < n:
        if is_fn_signature_line(code_lines[i]):
            fn_start = i  # 0-indexed
            # Walk forward to find the matching closing brace
            depth = 0
            fn_end = i
            for j in range(i, min(n, i + 300)):
                depth += code_lines[j].count('{') - code_lines[j].count('}')
                fn_end = j
                if j > i and depth <= 0:
                    break
            # 1-indexed line numbers that belong to this function
            fn_line_nums = set(range(fn_start + 1, fn_end + 2))
            if fn_line_nums & uncovered:
                annotated = []
                for k in range(fn_start, fn_end + 1):
                    lineno = k + 1
                    marker = "  // !! UNCOVERED" if lineno in uncovered else ""
                    annotated.append(f"{lineno:4d} | {code_lines[k]}{marker}")
                result_blocks.append("\n".join(annotated))
            i = fn_end + 1
        else:
            i += 1

    if not result_blocks:
        return _focused_annotation(content, uncovered)

    # When many uncovered lines span many functions, cap to the first max_fns bodies.
    # A focused prompt (≤5 functions) produces better-targeted tests than a dump of
    # 10+ bodies where the model picks the easiest paths and ignores the rest.
    if max_fns and len(result_blocks) > max_fns:
        result_blocks = result_blocks[:max_fns]

    return "\n\n// --- next function ---\n\n".join(result_blocks)


def _filter_cfg_incompatible_lines(
    source_text: str,
    uncov_lines: set[int],
    active_cfgs: list[str],
) -> set[int]:
    """Return the subset of uncov_lines that are inside #[cfg(...)] blocks incompatible
    with active_cfgs — i.e. blocks that are compiled OUT in the current cluster.

    A block is incompatible when its cfg attribute contains not(X) where X is in active_cfgs.
    These lines can never be covered by tests in this cluster regardless of what the LLM writes.

    #20: regex-free — delegates to the typed `CfgSet` (active cfgs in `true`, so a
    `not(X)` block evaluates False → excluded).
    """
    if not active_cfgs:
        return set()
    ctx = _cfg.CfgSet.of(cfg_true=[t for p in active_cfgs for t in (p, f'feature = "{p}"')])
    return ctx.excluded_uncovered_lines(source_text, uncov_lines)


def _filter_cfg_excluded_lines(
    source_text: str,
    uncov_lines: set[int],
    excluded_cfgs: list[str],
) -> set[int]:
    """Return the subset of uncov_lines inside #[cfg(X)] blocks where X is in excluded_cfgs.

    These lines are compiled only when X is active — in a cluster where X is excluded (e.g.
    stable when error_generic_member_access is excluded), they are never compiled and can
    never be covered regardless of what the LLM writes.

    Unlike _filter_cfg_incompatible_lines (which targets not(X) blocks for active clusters),
    this function targets direct X blocks for excluded clusters.

    #20: regex-free — delegates to the typed `CfgSet` (excluded cfgs in `false`, so
    a bare `#[cfg(X)]` block evaluates False → excluded; a `not(X)` block is True →
    kept, matching the old not()-guard).
    """
    if not excluded_cfgs:
        return set()
    ctx = _cfg.CfgSet.of(cfg_false=[t for p in excluded_cfgs for t in (p, f'feature = "{p}"')])
    return ctx.excluded_uncovered_lines(source_text, uncov_lines)


def _extract_public_symbols(content: str) -> str:
    """Extract public API from source with full signatures and field types.

    Returns a structured listing that lets the model construct types correctly
    without guessing field names, enum variants, or function parameter types.

    Parsing goes through the syn extractor — NOT regex (Hard Rule: no regex on Rust
    source). See `_public_api_from_source`.
    """
    return _public_api_from_source(content)


# ---------------------------------------------------------------------------
# TestAugmentor — language-agnostic ABC for coverage augmentation (FR-S5)
#
# Separates the Rust/Cargo/tarpaulin-specific mechanics from the orchestration
# logic in task0c_augment_module. Adding support for a new language means
# implementing a new concrete class; the orchestration loop stays unchanged.
#
# Selected by TEST_AUGMENTOR in .env:
#   TEST_AUGMENTOR=static   (default — Rust/tarpaulin)
# ---------------------------------------------------------------------------

class TestAugmentor(ABC):
    """Language-agnostic interface for per-module coverage augmentation."""

    @abstractmethod
    def lib_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        """Check that the module's library target compiles. Returns (ok, error_output)."""

    @abstractmethod
    def test_harness_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        """Check that the full test harness (lib + tests) compiles. Returns (ok, error_output)."""

    @abstractmethod
    def measure_coverage(
        self, project_dir: Path, module_name: str, module_dir: Path
    ) -> tuple[float, list, int, int]:
        """Measure test coverage. Returns (coverage_pct, files_data, covered_lines, coverable_lines)."""

    @abstractmethod
    def is_applicable(self, crate_type: str) -> bool:
        """Return False if this augmentor cannot handle the given crate type."""


class StaticRustTestAugmentor(TestAugmentor):
    """Rust-specific coverage augmentation using cargo and cargo-tarpaulin."""

    def lib_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        result = subprocess.run(
            shlex.split(f"cargo build -p {module_name} --lib"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=_cargo_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr

    def test_harness_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        result = subprocess.run(
            shlex.split(f"cargo test -p {module_name} --no-run"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=_cargo_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr

    def measure_coverage(
        self, project_dir: Path, module_name: str, module_dir: Path
    ) -> tuple[float, list, int, int]:
        return _measure_coverage(project_dir, module_name)

    def is_applicable(self, crate_type: str) -> bool:
        return "proc-macro" not in crate_type.lower()


def _test_augmentor_factory() -> TestAugmentor:
    backend = os.getenv("TEST_AUGMENTOR", "static").lower()
    if backend == "static":
        return StaticRustTestAugmentor()
    _log(f"  WARNING: unknown TEST_AUGMENTOR='{backend}', falling back to static.")
    return StaticRustTestAugmentor()


def _derive_gentest_prelude(project_dir: Path) -> tuple[str, str]:
    """Derive gen_tests allow attributes and prelude imports from project source — no LLM.

    Reads lib.rs/main.rs for crate-level #![deny/forbid] lints and #![no_std].
    cargo test always links std even for no_std crates, so prelude always uses std:: paths.

    Returns (allow_line, prelude_imports_block).
    """
    # Base lints to always allow — generated test code can't control these.
    allow_lints: set[str] = {"unused", "clippy::all", "non_snake_case", "unreachable_patterns"}

    lib_rs = project_dir / "src" / "lib.rs"
    if not lib_rs.exists():
        lib_rs = project_dir / "src" / "main.rs"

    if lib_rs.exists():
        text = lib_rs.read_text(encoding="utf-8")
        # Collect all crate-level deny/forbid lints → gen_tests must allow them.
        for args in crate_lint_attr_args(text):
            for lint in args.split(","):
                allow_lints.add(lint.strip())

    allow_line = f"#[allow({', '.join(sorted(allow_lints))})]"

    # cargo test always has std available (test harness links std) even in no_std crates.
    # Pre-inject the most commonly missing items so the LLM doesn't need to guess imports.
    prelude_lines = [
        "use std::string::{String, ToString};",
        "use std::vec::Vec;",
        "use std::boxed::Box;",
        "use std::format;",
        "use std::error::Error as StdError;",
    ]
    prelude_block = "\n    ".join(prelude_lines)

    return allow_line, prelude_block








def _partition_uncovered_lines(content: str, uncovered: "set[int]", n_groups: int) -> "list[set[int]]":
    """Split a file's uncovered lines into up to `n_groups` disjoint buckets,
    keeping each function's lines together.

    Used by task0c split-and-merge so N concurrent generations each own a
    distinct subset of the file's uncovered functions (additive coverage, no
    redundant work). Functions are detected the same way _extract_uncovered_
    functions does (scan for `fn`, brace-match to the closing `}`); each
    function that contains uncovered lines becomes one unit, distributed
    round-robin (largest-first) so buckets are roughly balanced. Uncovered
    lines outside any function are pooled into one extra unit. Returns only
    non-empty buckets (so the result length may be < n_groups, e.g. when the
    file has fewer functions than groups).
    """
    n_groups = max(1, n_groups)
    if not uncovered or n_groups == 1:
        return [set(uncovered)] if uncovered else []

    code_lines = content.splitlines()
    n = len(code_lines)
    remaining = set(uncovered)
    units: list[set[int]] = []  # one per function that owns uncovered lines

    i = 0
    while i < n:
        if is_fn_signature_line(code_lines[i]):
            depth = 0
            fn_end = i
            for j in range(i, min(n, i + 300)):
                depth += code_lines[j].count('{') - code_lines[j].count('}')
                fn_end = j
                if j > i and depth <= 0:
                    break
            fn_lines = set(range(i + 1, fn_end + 2)) & remaining  # 1-indexed
            if fn_lines:
                units.append(fn_lines)
                remaining -= fn_lines
            i = fn_end + 1
        else:
            i += 1
    if remaining:  # uncovered lines outside any detected function
        units.append(remaining)

    if len(units) <= 1:
        return units if units else [set(uncovered)]

    # Distribute units largest-first into n_groups balanced buckets.
    buckets: list[set[int]] = [set() for _ in range(min(n_groups, len(units)))]
    for unit in sorted(units, key=len, reverse=True):
        smallest = min(buckets, key=len)
        smallest |= unit
    return [b for b in buckets if b]


def task0c_augment_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    crate_type: str = "",
    existing_test_files: set | None = None,
    project_context: str = "",
    coverage_strategy: str = "",
) -> tuple[float, int, bool, str]:
    # slice 5b: extracted to application.tasks.task0c; thin shim binding deps.
    return _task0c_impl(
        project_dir, module_name, module_dir, _TASK0C_DEPS,
        crate_type=crate_type, existing_test_files=existing_test_files,
        project_context=project_context, coverage_strategy=coverage_strategy,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def restart_from_augmented(root: Path, project_dir: Path) -> None:
    """Restore workspace to the task0c augmentation commit and clear only
    iteration state (strip / task1+ artifacts). Thin shim over the relocated
    adapter (`pipeline_core.adapters.workspace_restore`); deps bound below."""
    _RESTORE_FROM_AUGMENTED.run(root, project_dir)


def clean_state(root: Path, project_dir: Path, manifest_path: Path,
                genesis_path: Path, functional_req_path: Path):
    """Delete all pipeline-generated state so the next run starts fresh."""
    # Generated source files tracked in the manifest
    if manifest_path.exists():
        delete_source_files(project_dir, manifest_path)

    # Per-iteration documents, the project profile, and synthesized context (all per-project)
    profile_path       = project_dir / PROJECT_PROFILE_NAME
    context_path       = project_dir / PROJECT_CONTEXT_NAME
    hash_path          = project_dir / PROJECT_INPUTS_HASH_NAME
    strategy_path      = project_dir / COVERAGE_STRATEGY_NAME
    strategy_hash_path = project_dir / COVERAGE_STRATEGY_HASH_NAME
    state_path         = _state_path(project_dir)
    for p in [functional_req_path, genesis_path, profile_path, context_path, hash_path,
              strategy_path, strategy_hash_path, state_path]:
        if p.exists():
            p.unlink()
            _log(f"  Deleted {p.name}")

    # Test output files
    for p in project_dir.glob("tests_output_*.txt"):
        p.unlink()
        _log(f"  Deleted {p.name}")

    # Chunk partials from interrupted chunked task1
    partials_dir = project_dir / ".task1_partials"
    if partials_dir.exists():
        for p in partials_dir.glob("*.txt"):
            p.unlink()
        with suppress(OSError):
            partials_dir.rmdir()
        _log("  Deleted .task1_partials/")

    # Failure log
    failure_log_path = project_dir / FAILURE_LOG_NAME
    if failure_log_path.exists():
        failure_log_path.unlink()
        _log(f"  Deleted {FAILURE_LOG_NAME}")

    # Chunk cache (persistent across iterations — only wiped on explicit --clean)
    cache_dir = root / CHUNK_CACHE_DIR
    if cache_dir.exists():
        deleted = 0
        for p in cache_dir.glob("*.txt"):
            p.unlink()
            deleted += 1
        with suppress(OSError):
            cache_dir.rmdir()
        _log(f"  Deleted {CHUNK_CACHE_DIR}/ ({deleted} cached chunks)")

    # Cluster augmentation sentinels — must be wiped so task0c reruns on --clean
    cluster_aug_dir = project_dir / CLUSTER_AUGMENTED_DIR
    if cluster_aug_dir.exists():
        shutil.rmtree(cluster_aug_dir)
        _log(f"  Deleted {CLUSTER_AUGMENTED_DIR}/")

    # Module augmentation and strip sentinels
    for _sentinel_dir_name in (MODULE_AUGMENTED_DIR, MODULE_STRIPPED_DIR):
        _sentinel_dir = project_dir / _sentinel_dir_name
        if _sentinel_dir.exists():
            shutil.rmtree(_sentinel_dir)
            _log(f"  Deleted {_sentinel_dir_name}/")

    # Locked module backups (bottom-up integration)
    locked_dir = project_dir / LOCKED_MODULES_DIR
    if locked_dir.exists():
        shutil.rmtree(locked_dir)
        _log(f"  Deleted {LOCKED_MODULES_DIR}/")

    # Per-module generated-file manifests
    for p in project_dir.glob(".generated_files_*.txt"):
        p.unlink()
        _log(f"  Deleted {p.name}")

    # Stale per-run coverage artifacts in /tmp/ — these live outside the
    # workspace dir, so they survive every wipe of tmp/{project}/. Without
    # cleaning them, the monitor reads stale per-file coverage from
    # /tmp/grcov_filecache_*.json (and the lcov / json outputs themselves)
    # and shows misleading carry-over numbers until the next full coverage
    # measurement overwrites them.
    import glob as _glob
    _tmp_patterns = [
        "/tmp/grcov_*.json",
        "/tmp/grcov_filecache_*.json",
        "/tmp/llvmcov_*.lcov",
        "/tmp/llvmcov_*.lcov.json",
    ]
    _tmp_deleted = 0
    for _pat in _tmp_patterns:
        for _p in _glob.glob(_pat):
            try:
                os.unlink(_p)
                _tmp_deleted += 1
            except OSError:
                pass
    for _tarp in _glob.glob("/tmp/tarp_*"):
        try:
            if os.path.isdir(_tarp):
                shutil.rmtree(_tarp)
                _tmp_deleted += 1
        except OSError:
            pass
    if _tmp_deleted:
        _log(f"  Deleted {_tmp_deleted} stale coverage artifact(s) from /tmp/")

    # Strip all gen_tests artifacts from source files: delete sibling files and
    # remove path-mod declarations. Also clean up legacy inline blocks (from
    # pre-sibling-file design) so an old workspace can be salvaged with --clean.
    _legacy_inline_re = re.compile(
        r'\n+(?:// GRCOV_EXCL_START\n)?#\[cfg\(test\)\](?:\n#\[[^\]]*\])*\n(?:pub )?mod gen_\w+\s*\{.*',
        re.DOTALL,
    )
    stripped_files = stripped_siblings = 0
    # Delete sibling files first so the parent loop doesn't re-read them.
    for sibling in sorted(project_dir.rglob("*.gen_tests_*.rs")):
        if re.search(r'\.gen_tests_\d+\.rs$', sibling.name):
            try:
                sibling.unlink()
                stripped_siblings += 1
                _log(f"  Deleted sibling {sibling.relative_to(project_dir)}")
            except OSError:
                pass
    for src in sorted(project_dir.rglob("*.rs")):
        try:
            raw = src.read_text(encoding="utf-8")
            cleaned = _strip_gen_tests_decls(raw)
            cleaned = _legacy_inline_re.sub("", cleaned).rstrip() + "\n"
            if cleaned != raw:
                src.write_text(cleaned, encoding="utf-8")
                _log(f"  Stripped gen_tests artifacts from {src.relative_to(project_dir)}")
                stripped_files += 1
        except Exception:
            pass
    if stripped_files or stripped_siblings:
        _log(f"  Removed {stripped_siblings} sibling file(s); cleaned {stripped_files} source file(s).")

    _log("Clean complete. Starting from scratch.")


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
_TASK1_DEPS = Task1Deps(
    load_api_surface=load_api_surface,
    load_type_names=_load_type_names,
    read=read,
    ask=ask,
    write_fr_preserving_notes=_write_fr_preserving_notes,
    synthesizer_factory=_task1_synthesizer_factory,
    filter_proc_macro_test_files=_filter_proc_macro_test_files,
    proc_macro_fallback_requirements=_proc_macro_fallback_requirements,
    format_api_surface_section=_format_api_surface_section,
    file_format_instruction=FILE_FORMAT_INSTRUCTION,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    profile_cap=_PROFILE_CAP,
    task1_model=_TASK1_MODEL,
)


def load_original_structure(project_dir: Path, module_name: str) -> list[str]:
    """Load the saved pre-strip file list for a module. Returns [] if not found.

    Matches by .txt filename stem (exact), not by content prefix. The stem-based match
    is unambiguous: 'argh' never collides with 'argh_shared', regardless of iteration order.
    """
    out_dir = project_dir / ORIGINAL_STRUCTURE_DIR
    target = module_name.replace("-", "_")
    for candidate in out_dir.glob("*.txt"):
        stem = candidate.stem.replace("-", "_")
        if stem == target:
            content = candidate.read_text()
            return [ln.strip() for ln in content.splitlines() if ln.strip()]
    return []




# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).




def cmd_best_models(vram_gb: float = 32.0) -> None:
    """Print best coding models (Ollama + HuggingFace) annotated for a VRAM budget.

    Composition root: live catalog reads (`adapters.model_catalog`) + pure
    ranking/rendering (`domain.model_report`)."""
    ollama = _model_report.annotate_installed(
        _model_report.build_ollama_rows(vram_gb),
        _model_catalog.ollama_installed_bases())
    print(_model_report.format_model_table(
        ollama, f"\u2500\u2500 Ollama  (ollama pull <name>)  |  VRAM budget: {vram_gb:.0f} GB"))
    print("  Benchmark scores: https://livecodebench.github.io/leaderboard.html")

    try:
        import huggingface_hub  # noqa: F401  (capability probe; the read is in the adapter)
    except ImportError:
        print("\nERROR: huggingface_hub not installed \u2014 skipping HF section.  Run: pip install huggingface_hub")
        print(_model_report.LEGEND)
        return

    print("\nFetching HuggingFace Hub top models \u2026")
    hf = _model_catalog.hf_top_models(vram_gb)
    deduped = _model_report.dedup_hf_rows(_model_report.rank_hf_rows(hf))
    print(_model_report.format_model_table(
        deduped[:30], f"\u2500\u2500 HuggingFace  (HF_MODEL=<id>)  |  VRAM budget: {vram_gb:.0f} GB"))
    print("  Benchmark scores: https://huggingface.co/spaces/bigcode/bigcodebench-leaderboard")
    print(_model_report.LEGEND)


# P6: per-file generation constraints reader (config TOML I/O) — focused adapter,
# composition-injected with the configs dir + the active-cluster predicate + log.
_FILE_CONSTRAINTS = FileConstraintsReader(
    configs_dir=Path(__file__).parent / "project_configs",
    is_nightly=lambda: _SCOPE.active is not None and not _SCOPE.active.is_default,
    log=_log,
)

# slice 5b: deps for the extracted TASK 0c engine (defined before main(), after every
# collaborator). subprocess/git injected as the module; _current_cluster via a call-time
# getter (mutable global); coverage provider as a value.
_TASK0C_DEPS = Task0cDeps(
    BUILD_TIMEOUT=BUILD_TIMEOUT, COVERAGE_TARGET=COVERAGE_AUGMENTATION_TARGET, coverage_ignore_pragma=COVERAGE_IGNORE_PRAGMA, TASK0C_PROMPT_CHAR_CAP=TASK0C_PROMPT_CHAR_CAP, FIX_ERR_CAP=_FIX_ERR_CAP, SIBLING_CAP=_SIBLING_CAP, SRC_SNIP_CAP=_SRC_SNIP_CAP, TASK0C_FIX_THINKING_DIRECTIVE=_TASK0C_FIX_THINKING_DIRECTIVE, TASK0C_MODEL=_TASK0C_MODEL, TASK0C_THINKING_DIRECTIVE=_TASK0C_THINKING_DIRECTIVE, WORKING_CAP=_WORKING_CAP, annotate_uncovered=_annotate_uncovered, block_is_prose=_block_is_prose, build_gen_tests_decl=_build_gen_tests_decl, guard=_guard, cargo_env=_cargo_env, cluster_gate_for=_cluster_gate_for_sibling, condense_rust_to_signatures=_condense_rust_to_signatures, count_test_attrs=_count_test_attrs, derive_gentest_prelude=_derive_gentest_prelude, describe_test_environment=_describe_test_environment, error_signatures_from_output=_error_signatures_from_output, extract_branch_guard=_extract_branch_guard, extract_gen_tests_body=_extract_gen_tests_body, extract_public_symbols=_extract_public_symbols, extract_uncovered_functions=_extract_uncovered_functions, filter_cfg_excluded_lines=_filter_cfg_excluded_lines, filter_cfg_incompatible_lines=_filter_cfg_incompatible_lines, flags_unreachable=_flags_unreachable, gen_tests_path=_gen_tests_path, has_nonempty_test_body=_has_nonempty_test_body, inject_allow_before_any_mod=_inject_allow_before_any_mod, inject_allow_before_gen_mod=_inject_allow_before_gen_mod, inject_gen_prelude_imports=_inject_gen_prelude_imports, is_gen_tests_file=_is_gen_tests_file, is_structural_line=_is_structural_line, load_file_constraints=_FILE_CONSTRAINTS.load, type_signals_write=type_signals_write, mark_phase=_mark_phase, match_gen_tests_sibling=_match_gen_tests_sibling, next_gen_tests_n=_next_gen_tests_n, nightly_rules_for_cluster=_nightly_rules_for_cluster, parse_attempt_number=_parse_attempt_number, parse_retrospective_categories=_parse_retrospective_categories, partition_uncovered_lines=_partition_uncovered_lines, prelude_import_names=_prelude_import_names, quick_file_coverage=_quick_file_coverage, read_all_gen_tests=_read_all_gen_tests, read_state=_read_state, revert_gen_tests_pass=_revert_gen_tests_pass, select_relevant_siblings=_select_relevant_siblings, set_current=_set_current, source_hash=_source_hash, strip_all_gen_tests_artifacts=_strip_all_gen_tests_artifacts, strip_gen_tests_decl_n=_strip_gen_tests_decl_n, strip_gen_tests_decls=_strip_gen_tests_decls, strip_trailing_file_tag=_strip_trailing_file_tag, mutation_harden_file=lambda *a, **k: _MUTATION.harden_file(*a, **k), gen_tests_siblings=_gen_tests_siblings, test_augmentor_factory=_test_augmentor_factory, ask=ask, extract_files=extract_files, extract_test_mod=extract_test_mod, generate_fix_loop=generate_fix_loop, generate_in_parallel=generate_in_parallel, is_test_file=is_test_file, write=write,
    subprocess=subprocess, get_current_cluster=_SCOPE.get_active,
    coverage_provider=_coverage_provider, check_stop=_check_graceful_stop,
)

# P5-S3: deps for the extracted gen_tests reattach block (per-block injection).
_REATTACH_DEPS = ReattachDeps(
    log=_log, emit=_emit, derive_gentest_prelude=_derive_gentest_prelude,
    is_gen_tests_file=_is_gen_tests_file, gen_tests_siblings=_gen_tests_siblings,
    load_gen_tests_origin=_load_gen_tests_origin, gen_tests_path=_gen_tests_path,
    build_gen_tests_decl=_build_gen_tests_decl, cluster_cfg_attr=_cluster_cfg_attr,
    has_gen_tests_decl=_has_gen_tests_decl, cargo_env=_cargo_env,
)

# P6: pre-strip API-surface capture — focused adapter, composition-injected.
_API_SURFACE = ApiSurfaceCapture(
    api_surface_path=_api_surface_path, type_manifest_path=_type_manifest_path,
    condense_to_signatures=_condense_rust_to_signatures,
    strip_gen_tests_decls=_strip_gen_tests_decls,
    enumerate_types=ADAPTERS.extractor.enumerate_types,
    is_test_file=is_test_file, skip_dir_names=frozenset(SKIP_DIR_NAMES), log=_log,
)

# P6: --restart-from-augmented workspace restore — focused adapter, composition-injected.
_RESTORE_FROM_AUGMENTED = RestoreFromAugmented(
    read_state=_read_state, write_state=_write_state, log=_log,
)

# P5-S3: deps for the extracted strip/bake/commit pre-step (per-block injection).
_STRIP_BAKE_DEPS = StripBakeDeps(
    log=_log, phase_valid=_phase_valid, module_augmented_path=_AUGMENT_STORE.module_path,
    capture_api_surface=_API_SURFACE.capture,
    bake_skeleton_artifacts=_bake_skeleton_artifacts,
    git_commit=git_commit, git_tag=git_tag,
    strip_module_source=strip_module_source, commit_and_mark=_commit_and_mark,
    set_current=_set_current, tarpaulin_bin=TARPAULIN_BIN,
    coverage_target=COVERAGE_AUGMENTATION_TARGET,
)

# P5-S3: deps for the lock/complete transition (dedups 3 call sites).
_LOCK_DEPS = _orchestrator.LockDeps(
    commit_and_mark=_commit_and_mark, set_current=_set_current,
    lock_module=lock_module, git_commit=git_commit, log=_log, sys_exit=sys.exit,
)


# P6: the `_ReconstructAdapter` god-object is dissolved — each reconstruction
# role is a focused adapter (adapters.reconstruct_roles), constructor-injected
# with the resident task1-4/fix composition-wiring callables; the use-cases get
# narrow role ports (ISP) instead of one multi-role binding object.
_PHASE_GATE = PhaseGateAdapter(_phase_valid, _commit_and_mark)
_REQUIREMENTS = RequirementsAdapter(task1, validate_task1_output)
_GENERATOR = GeneratorAdapter(
    task2_inner_loop, load_original_structure, validate_task2_output,
    validate_task2_api_surface_conformance,
)
_FIXER = FixerAdapter(task2_fix_loop, _classify_failure, _log, insert_no_run, _FIX_ERR_CAP)
_ANALYST = AnalystAdapter(task4, validate_task4_output, task_genesis_audit)
_COVERAGE_DOCS = CoverageDocsAdapter(
    lambda project_dir, test_files, readme_text, project_profile, clusters: task0b_discovery(
        project_dir / COVERAGE_STRATEGY_NAME, project_dir, test_files, readme_text,
        project_profile, clusters=clusters, force=True),
)
_FLOW_AUDIT = FlowAuditAdapter(_run_flow_audit)
_DOCS_METRICS = DocsMetricsAdapter(_load_model_notes, genesis_rules_count)

# P5: reconstruction use-cases — each depends on narrow role slices.
_FAILURE_JOURNAL = FileFailureJournal()
_WORKSPACE_ARTIFACTS = WorkspaceArtifacts()
_TELEMETRY = LogTelemetry()
_TEST_RUNNER = CallableTestRunner(task3)
_REQUIREMENTS_PHASE = RequirementsPhase(
    requirements=_REQUIREMENTS, gate=_PHASE_GATE,
    telemetry=_TELEMETRY, analyst=_ANALYST,
    journal=_FAILURE_JOURNAL, artifacts=_WORKSPACE_ARTIFACTS,
)
_RECONSTRUCTION_STEP = ReconstructionStep(
    generator=_GENERATOR, tests=_TEST_RUNNER, fixer=_FIXER,
    artifacts=_WORKSPACE_ARTIFACTS, telemetry=_TELEMETRY,
    lock_deps=_LOCK_DEPS, reattach_deps=_REATTACH_DEPS,
    no_improvement_threshold=NO_IMPROVEMENT_THRESHOLD,
)
_FAILURE_ANALYSIS = FailureAnalysisStep(
    analyst=_ANALYST, journal=_FAILURE_JOURNAL, ratchet=BestTreeRatchet(),
    artifacts=_WORKSPACE_ARTIFACTS, coverage_docs=_COVERAGE_DOCS,
    audit=_FLOW_AUDIT, docs=_DOCS_METRICS, telemetry=_TELEMETRY,
    genesis_audit_interval=GENESIS_AUDIT_INTERVAL,
)
_MODULE_RECONSTRUCTOR = ModuleReconstructor(
    step=_RECONSTRUCTION_STEP, analysis=_FAILURE_ANALYSIS, telemetry=_TELEMETRY,
    # debt #18: bound the outer repair loop (> NO_IMPROVEMENT_THRESHOLD so escalation acts first).
    plateau_abort=int(os.getenv("MODULE_PLATEAU_ABORT", "10")),
)


# P6: the `_AugmentAdapter` god-object is dissolved — each TASK 0c augmentation
# role is now a real injected adapter (telemetry/store/engine) or the domain
# ClusterScope; only the git-global-bound commit-and-mark stays a resident callable
# behind a focused CommitGate.
_AUGMENT_ENGINE = CargoAugmentEngine(
    log=_log, cargo_env=_cargo_env, build_timeout=BUILD_TIMEOUT,
    default_setup=Path(__file__).parent / "setup.sh",
    collect_test_files=collect_test_files, augment_module=task0c_augment_module,
)
_CLUSTER_AUGMENTOR = ClusterAugmentor(
    telemetry=_TELEMETRY, scope=_SCOPE, store=_AUGMENT_STORE,
    engine=_AUGMENT_ENGINE, gate=CallableCommitGate(_commit_and_mark),
)


def main():
    parser = _orchestrator.build_arg_parser()
    args = parser.parse_args()

    if args.best_models_list:
        cmd_best_models(vram_gb=args.vram_gb)
        sys.exit(0)

    global _git_managed
    root = Path(__file__).parent
    workspace_dir = root / WORKSPACE_DIR

    if args.repo:
        # Full git-managed workspace: clone/copy into tmp/, checkout pipeline branch.
        project_dir = setup_workspace(args.repo, workspace_dir,
                                      force_fresh=args.clean)
        _git_managed = True
    else:
        # --project: check tmp/ first, then root (backward compat).
        candidate = workspace_dir / args.project
        project_dir = candidate if candidate.exists() else root / args.project
        _git_managed = _is_git_repo(project_dir)

    genesis_path = project_dir / "genesis.md"
    functional_req_path = project_dir / "functional_requirements.md"
    manifest_path = project_dir / MANIFEST_NAME  # legacy whole-workspace manifest

    if not project_dir.exists():
        print(f"ERROR: project directory '{project_dir}' not found.")
        sys.exit(1)

    init_log(root, nolog=args.nolog, progress_mode=args.progress)
    init_events(product=project_dir.name)

    # --audit: periodic + iteration-boundary flow self-audit (read-only).
    global _AUDIT_ENABLED, _AUDIT_PROJECT_DIR
    _AUDIT_ENABLED = bool(args.audit)
    _AUDIT_PROJECT_DIR = project_dir
    if _AUDIT_ENABLED:
        _start_audit_thread()
        _log(f"  --audit: flow self-audit active (every iteration + every "
             f"{os.getenv('AUDIT_INTERVAL_SEC', '900')}s).")

    # P3.13: scan gen_tests siblings inherited from prior runs; quarantine
    # any with unbalanced delimiters and strip their decls from the parent
    # source. Prevents a broken file (an LLM-corrupted task0c output) from
    # blocking every future cargo invocation. The systemic guard at write
    # time lives in _write_gen_tests_pass.
    try:
        _quarantine_broken_gen_tests(project_dir)
    except Exception as _e:
        _log(f"  [gen_tests] startup quarantine scan failed: {_e}")

    _sub_rc = _orchestrator.run_phase_subcommand(args, project_dir, {
        "phase_a_only": _cmd_phase_a_only,
        "phase_b_only": _cmd_phase_b_only,
        "phase_c_only": _cmd_phase_c_only,
        "phase_d_only": _cmd_phase_d_only,
        "skeleton_backend": _cmd_skeleton_backend,
    })
    if _sub_rc is not None:
        sys.exit(_sub_rc)

    # Forward (spec-driven) mode: skip reconstruction-phase tasks. The user
    # provides functional_requirements.md (+ optional API surface) and tests;
    # the pipeline goes straight to TASK 2 → TASK 3 → TASK 4.
    _from_spec = args.from_spec
    if _from_spec:
        _fr_root = project_dir / "functional_requirements.md"
        if not _fr_root.exists():
            print(f"ERROR: --from-spec requires {_fr_root} (project-level FR file).")
            sys.exit(1)
        _log(f"FORWARD MODE: spec-driven generation. "
             f"Skipping task0a/0b/0c/strip/task1. FR={_fr_root}")

    _emit("pipeline.start", data={
        "project_dir": str(project_dir),
        "clean": args.clean,
        "augment_tests": args.augment_tests,
        "from_spec": _from_spec,
        "model": MODEL,
        "backend": SETTINGS.backend,
        "coverage_backend": COVERAGE_BACKEND,
        "context_chars": MODEL_CONTEXT_CHARS,
    })

    _ph = _git_file_hash(root, "pipeline.py")
    _mh = _git_file_hash(root, "monitor.py")
    _params = []
    if args.repo:
        _params.append(f"--repo {args.repo}")
    if args.project and not args.repo:
        _params.append(f"--project {args.project}")
    if args.clean:
        _params.append("--clean")
    if args.augment_tests:
        _params.append("--augment-tests")
    if args.progress:
        _params.append("--progress")
    if args.nolog:
        _params.append("--nolog")
    if args.from_spec:
        _params.append("--from-spec")
    if args.restart_from_augmented:
        _params.append("--restart-from-augmented")
    _log(f"  pipeline.py [{_ph}]  monitor.py [{_mh}]  params: {' '.join(_params) or '(none)'}")

    _backend = SETTINGS.backend
    if _backend is Backend.VLLM:
        provider = f"vLLM ({os.getenv('VLLM_MODEL_NAME', MODEL)})"
    elif _backend is Backend.SGLANG:
        provider = f"SGLang ({os.getenv('SGLANG_MODEL_NAME', MODEL)})"
    elif _backend is Backend.HUGGINGFACE:
        provider = f"HuggingFace ({os.getenv('HF_MODEL', MODEL)})"
    else:
        provider = f"Ollama ({MODEL})"
    _log(f"Using model: {MODEL}  |  Provider: {provider}")
    _log(f"Context budget: {MODEL_CONTEXT_CHARS:,} chars  "
         f"(SINGLE_CALL={SINGLE_CALL_CHAR_LIMIT:,}  FILE={FILE_CHAR_LIMIT:,}  "
         f"INTERMEDIATE={INTERMEDIATE_CHAR_LIMIT:,})")

    if _backend is Backend.VLLM:
        _vllm_bootstrap.ensure_vllm_ready(root, _log)

    if args.clean and args.restart_from_augmented:
        _log("\n-- ERROR: --clean and --restart-from-augmented are mutually exclusive --")
        sys.exit(2)

    if args.clean:
        _log("\n-- --clean requested: wiping all generated state --")
        if _git_managed:
            git_clean_reset(project_dir)
        clean_state(root, project_dir, manifest_path, genesis_path, functional_req_path)
        _state_path(project_dir).unlink(missing_ok=True)

    if args.restart_from_augmented:
        _log("\n-- --restart-from-augmented: restoring workspace to task0c augmented state --")
        try:
            restart_from_augmented(root, project_dir)
        except Exception as e:
            _log(f"  [ABORT] restart-from-augmented failed: {e}")
            sys.exit(2)

    _src_hash = _source_hash(project_dir)
    _log(f"  Source hash: {_src_hash}")
    _log_state_summary(project_dir, _src_hash)

    readme_path = project_dir / "README.md"
    if readme_path.exists():
        readme_text = read(readme_path)
    else:
        _log("WARNING: README.md not found -- proceeding without it.")
        readme_text = ""

    # Cluster scan is pure static analysis (no LLM) — run first so all downstream
    # LLM tasks (discover_test_command, task0b) have cluster context available.
    _clusters = task0_cfg_scan(project_dir)
    _SCOPE.reset(_clusters)
    _log(f"\nCompiler clusters identified: {len(_clusters)}")
    for _c in _clusters:
        _env_desc = ", ".join(f"{k}+={v}" for k, v in _c.env_extra.items()) if _c.env_extra else "default build"
        _log(f"  cluster/{_c.name}  — {_env_desc}")
    _mark_phase(project_dir, "cfg_scan", "done",
                detail=f"{len(_clusters)} clusters: {', '.join(c.name for c in _clusters)}",
                source_hash=_src_hash)

    if _phase_valid(project_dir, "test_cmd"):
        test_command = _read_state(project_dir)["phases"]["test_cmd"].get("detail", "")
        _log(f"Test command: {test_command} (from pipeline state)")
    else:
        test_command = discover_test_command(readme_text, project_dir=project_dir)
        _mark_phase(project_dir, "test_cmd", "done", detail=test_command, source_hash=_src_hash)

    # Normalize: strip leading ./ from the executable so we use the PATH binary.
    parts_check = _parse_cmd(test_command)
    if parts_check and parts_check[0].startswith("./"):
        parts_check[0] = parts_check[0][2:]
        test_command = " ".join(parts_check)
        _log(f"  Normalized test command: {test_command}")

    _log("Collecting test files...")
    test_files = collect_test_files(project_dir)
    existing_test_files = collect_protected_test_files(project_dir)
    _log(f"  Found {len(test_files)} test file(s) ({len(existing_test_files)} write-protected).")

    # TASK 0a: identify project structure once; profile stored per-project (not pipeline root)
    # so running project B never loads project A's cached profile.
    # FORWARD MODE: task0a/0b/0c provide reconstruction-phase context (profile/
    # behavioral summary/coverage strategy). In spec-driven mode the user supplies
    # FR+API directly so this preprocessing is unnecessary — empty strings keep
    # downstream prompts well-formed without triggering a regeneration.
    if _from_spec:
        project_profile = ""
        project_context = ""
        coverage_strategy = ""
        _log("  [from-spec] Skipping task0a/0a-context/0b (FR+API are user-provided).")
    else:
        profile_path = project_dir / PROJECT_PROFILE_NAME
        project_profile = task0_project_profile(profile_path, test_files, readme_text)

        # TASK 0a (context): synthesize behavioral project summary from test content.
        # This is the "big picture" document injected into every task — especially TASK 0c
        # which currently receives no project-level context. Works even without README.
        context_path = project_dir / PROJECT_CONTEXT_NAME
        _emit("task.start", task="task0a")  # debt#7: structured stage event
        project_context = task0_project_context(context_path, test_files, readme_text, project_profile)

        # TASK 0b (discovery): produce coverage_strategy.md — import idioms, type construction
        # patterns, feature flags, test conventions. Injected into all TASK 0c, 1, and 2 prompts.
        # Skip if either pipeline_state marks task0b done, or the artifact already
        # exists from a prior run that predates state tracking (auto-backfill).
        strategy_path = project_dir / COVERAGE_STRATEGY_NAME
        _emit("task.start", task="task0b")  # debt#7: structured stage event
        if _phase_valid(project_dir, "task0b"):
            coverage_strategy = strategy_path.read_text().strip() if strategy_path.exists() else ""
            _log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        elif strategy_path.exists() and strategy_path.read_text().strip():
            # Artifact predates pipeline_state.md tracking — backfill and skip.
            coverage_strategy = strategy_path.read_text().strip()
            _mark_phase(project_dir, "task0b", "done",
                        detail=f"{len(coverage_strategy)} chars (existing artifact)")
            _log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        else:
            coverage_strategy = task0b_discovery(
                strategy_path, project_dir, test_files, readme_text, project_profile,
                clusters=_clusters,
            )
            _commit_and_mark(
                project_dir, "task0b", "done",
                commit_message="pipeline: task0b done",
                detail=f"{len(coverage_strategy)} chars",
            )

    # TASK 0b: build dependency graph for bottom-up module integration.
    # Implementation selected by DEPENDENCY_GRAPH in .env (default: static).
    dep_graph = _dependency_graph_factory(project_dir, _WS_SCAN_DEPS)
    modules = dep_graph.topological_order()
    is_multi_module = len(modules) > 1

    if is_multi_module:
        _log(f"\nDependency graph: {len(modules)} module(s) in topological order:")
        for m in modules:
            deps = dep_graph.workspace_deps(m)
            dep_str = f"  deps: {', '.join(sorted(deps))}" if deps else "  leaf"
            _log(f"  [{dep_graph.crate_type(m):10s}]  {m}  ({dep_str})")
    else:
        _log("  Single module or no dependency graph detected — whole-workspace mode.")

    # -----------------------------------------------------------------------
    # Per-module pipeline.
    # For each module in coverage-descending order (topology respected):
    #   0c. Augment THIS module's tests until 100% coverage (real source present)
    #   Strip. Remove ONLY this module's source — all others remain on disk
    #   1.  Extract requirements from THIS module's tests
    #   2-4. Reconstruct loop until tests pass
    #   Lock. Only when passed > 0 and failed == 0
    # -----------------------------------------------------------------------
    if not modules:
        modules = [project_dir.name]

    locked_modules: set[str] = set()
    if is_multi_module:
        locked_modules = load_locked_modules(project_dir)
        if locked_modules:
            _log(f"\nAlready locked: {', '.join(sorted(locked_modules))}")

    failure_log_path = project_dir / FAILURE_LOG_NAME

    # Measure baseline coverage once to determine processing order.
    # Coverage-first: richest test spec → easiest reconstruction → best context for later modules.
    module_coverage = _orchestrator.measure_baseline_coverage(
        modules, dep_graph, tarpaulin_available=TARPAULIN_BIN.exists(),
        run_tarpaulin=_run_tarpaulin, project_dir=project_dir, log=_log)

    if is_multi_module and module_coverage:
        _log("\nProcessing order (coverage-descending, topology respected):")
        for m in modules:
            _log(f"  {m}: {module_coverage.get(m, 0.0):.1f}%")

    # -----------------------------------------------------------------------
    # TASK 0c — compiler cluster phase
    # _clusters and _current_cluster already set above (moved before task0b so
    # all LLM tasks receive cluster context). Loop below runs one pass per cluster.
    # -----------------------------------------------------------------------

    if _from_spec:
        _log("  [from-spec] Skipping task0c cluster loop (no test augmentation needed).")
        _clusters = []  # ensure the loop below is a no-op
    # TASK 0c cluster augmentation (R2.2 — application.cluster_augment.ClusterAugmentor).
    # Per-module crate dir/type resolved up front; _from_spec leaves _clusters empty so
    # the use-case is a no-op (it still clears the active cluster on exit).
    _module_dirs = {m: (dep_graph.crate_dir(m) if is_multi_module else project_dir)
                    for m in modules}
    _module_types = {m: (dep_graph.crate_type(m) if is_multi_module else "lib")
                     for m in modules}
    _CLUSTER_AUGMENTOR.run(AugmentContext(
        project_dir=project_dir, clusters=_clusters, modules=modules,
        module_coverage=module_coverage, module_dirs=_module_dirs,
        module_types=_module_types, src_hash=_src_hash,
        project_context=project_context, coverage_strategy=coverage_strategy,
        target_pct=COVERAGE_AUGMENTATION_TARGET, tarpaulin_bin=TARPAULIN_BIN,
    ))

    total_iters = 0
    while True:
        module_name = (_orchestrator.next_module(modules, locked_modules, module_coverage)
                       if is_multi_module else (modules[0] if modules else None))
        if module_name is None:
            break

        _desc = _orchestrator.module_descriptor(
            module_name, dep_graph=dep_graph, is_multi_module=is_multi_module,
            project_dir=project_dir, test_command=test_command)
        module_dir = _desc.module_dir
        crate_type = _desc.crate_type
        workspace_deps = _desc.workspace_deps
        module_test_cmd = _desc.module_test_cmd
        module_manifest = _desc.module_manifest
        module_dir_rel = _desc.module_dir_rel

        _log(f"\n{'=' * 60}")
        _log(f"MODULE: {module_name}  [{crate_type}]")
        ready_deps = workspace_deps & locked_modules
        if ready_deps:
            _log(f"  Locked deps  : {', '.join(sorted(ready_deps))}")
        _log(f"{'=' * 60}")

        # --- TASK 0c: all clusters complete — sentinel check only ---
        # All coverage augmentation (across all clusters) was done by the cluster
        # outer loop above. This block just verifies the sentinel and proceeds.
        # FORWARD MODE: skip — there is no augmentation phase in spec-driven mode.
        if not _from_spec:
            if not _AUGMENT_STORE.is_module_augmented(project_dir, module_name):
                _log(f"  WARNING: '{module_name}' has no augmentation sentinel — proceeding anyway.")
            _log(f"  TASK 0c: '{module_name}' all clusters done — proceeding to strip.")

        # Coverage check before strip — warn if below 100% but proceed anyway.
        # A hard stop here would abort the pipeline on coverage tool failures (bad sentinel,
        # grcov error, etc.) even when the test suite is intact.
        if crate_type != "proc-macro" and TARPAULIN_BIN.exists():
            _final_cov = _AUGMENT_STORE.module_augmented_value(project_dir, module_name)
            if _final_cov < COVERAGE_AUGMENTATION_TARGET:
                _log(
                    f"\n  WARNING: '{module_name}' coverage is {_final_cov:.1f}% — "
                    f"proceeding to strip anyway. Reconstruction quality may be lower."
                )

        # --- Collect test files BEFORE stripping ---
        # Must happen before strip so we can accurately identify which tests belong
        # to this module. Strip removes implementation source but test content in
        # lib.rs would also be gone if we checked after.
        test_files_pre_strip = collect_test_files(project_dir)
        module_test_files = _orchestrator.filter_module_test_files(
            test_files_pre_strip, module_dir_rel)
        module_has_tests = len(module_test_files) > 0
        _log(f"  Module test files: {len(module_test_files)} (before strip)")

        # Strip this module's source after baking Hard-Rule-11 artefacts
        # (P5-S3 — extracted to application/strip_bake.py).
        _strip_and_bake_module(
            project_dir, module_name, module_dir, crate_type=crate_type,
            from_spec=_from_spec, src_hash=_src_hash, deps=_STRIP_BAKE_DEPS,
        )
        
        # Re-collect after strip — picks up any tests generated by TASK 0c.
        # test_files: full read set for TASK 1 (includes inline test blocks in src/)
        # existing_test_files: write-protection set for TASK 2 (pure test files only)
        test_files = collect_test_files(project_dir)
        existing_test_files = collect_protected_test_files(project_dir)
        _log(f"  Test files after strip: {len(test_files)} readable, "
             f"{len(existing_test_files)} write-protected.")

        # --- TASK 1: extract requirements from this module's tests ---
        module_req_path = project_dir / f"functional_requirements_{module_name}.md"

        # Initialize iteration counters here so task1's task4 escalation path can use them.
        module_iter = 1
        prev_pass_count = 0
        prev_test_output = ""

        t1_input = module_test_files if module_has_tests else test_files
        if not module_has_tests:
            _log(f"  '{module_name}' has 0 tests — using full project tests for TASK 1.")

        # Append example files (examples/*.rs) as usage signal. They are NOT
        # assertions — the inline header makes that explicit so the LLM doesn't
        # derive requirements from example-only behavior.
        _t1_examples = collect_examples(project_dir, module_dir=module_dir if is_multi_module else None)
        t1_input = _orchestrator.with_example_usage_signal(t1_input, _t1_examples)
        if _t1_examples:
            _log(f"  Appended {len(_t1_examples)} example file(s) to TASK 1 input as usage signal.")

        # FORWARD MODE: copy the user-provided FR into the per-module slot so
        # TASK 2 picks it up via the existing module_req_path. Skip the full
        # TASK 1 inference loop — the user is the source of truth.
        if _from_spec:
            _fr_root = project_dir / "functional_requirements.md"
            if not module_req_path.exists():
                module_req_path.write_text(_fr_root.read_text(encoding="utf-8"),
                                           encoding="utf-8")
                _log(f"  [from-spec] Seeded {module_req_path.name} "
                     f"from {_fr_root.name} ({_fr_root.stat().st_size} chars).")
            else:
                _log(f"  [from-spec] {module_req_path.name} already present — leaving as-is.")

        # TASK 1: generate → validate → retry → escalate (P5 — RequirementsPhase use-case).
        t1_valid = _REQUIREMENTS_PHASE.run(
            project_dir, module_name=module_name, module_req_path=module_req_path,
            genesis_path=genesis_path, failure_log_path=failure_log_path,
            t1_input=t1_input, readme_text=readme_text, project_profile=project_profile,
            project_context=project_context, coverage_strategy=coverage_strategy,
            crate_type=crate_type, from_spec=_from_spec, src_hash=_src_hash,
            iteration=module_iter, prev_pass_count=prev_pass_count,
            prev_test_output=prev_test_output,
        )

        if not t1_valid:
            continue

        task1_static_audit(module_req_path, module_test_files or test_files)

        # Workspace scan (done per-module since strip changes the file layout).
        workspace_structure = scan_workspace_structure(project_dir)
        build_check_cmd = _infer_build_check_command(test_command, project_dir)
        module_build_check = (
            dep_graph.module_build_check_command(build_check_cmd, module_name)
            if is_multi_module else build_check_cmd
        )

        # Slice 9: module-start lifecycle (save siblings → maybe-wipe → stub)
        # lives in application/workspace_lifecycle.py; the wipe is gated by
        # the codified wipe⟺rerun invariant (session-59 t2c→t3 fall-through).
        saved_gen_blocks: dict[str, list[tuple[int, str]]] = \
            _wl_save_gen_tests_siblings(
                module_dir, project_dir, existing_test_files,
                gen_tests_siblings=_gen_tests_siblings,
                is_gen_tests_file=_is_gen_tests_file)
        _wl_wipe_module_sources(
            module_dir, project_dir, existing_test_files,
            regenerating_phase_done=_phase_valid(
                project_dir, f"task2b/{module_name}"),
            write_stub=_partial(_write_workspace_stub, crate_type, module_dir))

        if not module_has_tests:
            _log(f"  '{module_name}' has 0 test files — will use whole-project context in TASK 2.")

        # --- TASK 2-4: reconstruction loop (P5 — ModuleReconstructor use-case). ---
        run_ctx = RunContext(
            project_dir=project_dir, module_name=module_name, module_dir=module_dir,
            crate_type=crate_type, module_manifest=module_manifest,
            module_dir_rel=module_dir_rel, module_test_cmd=module_test_cmd,
            module_build_check=module_build_check, module_has_tests=module_has_tests,
            module_req_path=module_req_path, genesis_path=genesis_path,
            failure_log_path=failure_log_path, is_multi_module=is_multi_module,
            src_hash=_src_hash, workspace_deps=workspace_deps, locked_modules=locked_modules,
            workspace_structure=workspace_structure, project_profile=project_profile,
            project_context=project_context, readme_text=readme_text,
            test_files=test_files, existing_test_files=existing_test_files,
            clusters=_clusters, saved_gen_blocks=saved_gen_blocks,
        )
        total_iters, coverage_strategy = _MODULE_RECONSTRUCTOR.run(
            run_ctx, total_iters=total_iters, coverage_strategy=coverage_strategy)

    # -----------------------------------------------------------------------
    # Final: run full test suite to validate the integrated workspace.
    # Only reached in multi-module mode (single-module exits on pass above).
    # -----------------------------------------------------------------------
    _log(f"\n{'=' * 60}")
    _log("FINAL: Running full test suite to validate integrated workspace")
    _log(f"{'=' * 60}")
    passed, _ = task3(project_dir, test_command, "final", project_dir, genesis_path)
    if passed:
        _log("\nSUCCESS: all tests passed.")
        sys.exit(0)
    else:
        _log("\nWARNING: all modules locked individually but full suite failed.")
        _log("         Integration issues between modules may require attention.")
        sys.exit(1)


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
