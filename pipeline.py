#!/usr/bin/env python3
"""
Pipeline for the deterministic process loop.

Usage:
  python pipeline.py --project <subfolder>
"""

import dataclasses
import importlib
import json
import os
import shlex
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
from pipeline_core.adapters import vllm_bootstrap as _vllm_bootstrap
from pipeline_core.adapters import workspace as _wsmod
from pipeline_core.adapters import workspace_files as _wsfiles
from pipeline_core.adapters.api_surface import ApiSurfaceCapture
from pipeline_core.adapters.artifacts import (
    api_surface_path as _api_surface_path,
)
from pipeline_core.adapters.artifacts import (
    load_api_surface,
    load_manifest,
)
from pipeline_core.adapters.artifacts import (
    load_type_names as _load_type_names,
)
from pipeline_core.adapters.artifacts import (
    pub_items_manifest_path as _pub_items_manifest_path,
)
from pipeline_core.adapters.artifacts import (
    type_manifest_path as _type_manifest_path,
)
from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.adapters.augment_engine import CargoAugmentEngine, StaticRustTestAugmentor
from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.adapters.build_script import CargoBuildScriptReader
from pipeline_core.adapters.cargo import CargoEnv
from pipeline_core.adapters.cargo import run_build_check as _cargo_run_build_check
from pipeline_core.adapters.cluster_merge import ClusterMerger
from pipeline_core.adapters.cluster_pruning import prune_redundant_clusters as _prune_redundant_clusters
from pipeline_core.adapters.commit_gate import CallableCommitGate
from pipeline_core.adapters.commit_gate import commit_and_mark as _commit_and_mark_impl
from pipeline_core.adapters.downstream_api import scan_downstream_api
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
from pipeline_core.adapters.fix_loop_roles import (
    FixCheckerAdapter as _FixCheckerAdapter,
)
from pipeline_core.adapters.fix_loop_roles import (
    FixModelAdapter as _FixModelAdapter,
)
from pipeline_core.adapters.fix_loop_roles import (
    FixPromptsAdapter as _FixPromptsAdapter,
)
from pipeline_core.adapters.fix_loop_roles import (
    FixSplicerAdapter as _FixSplicerAdapter,
)
from pipeline_core.adapters.fix_loop_roles import (
    FrontierBuilderAdapter as _FrontierBuilderAdapter,
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
from pipeline_core.adapters.gen_tests_storage import (
    next_gen_tests_n as _next_gen_tests_n,
)
from pipeline_core.adapters.gen_tests_storage import (
    quarantine_broken_gen_tests as _quarantine_broken_gen_tests,
)
from pipeline_core.adapters.gen_tests_storage import (
    read_all_gen_tests as _read_all_gen_tests,
)
from pipeline_core.adapters.gen_tests_storage import (
    revert_gen_tests_pass as _revert_gen_tests_pass,
)
from pipeline_core.adapters.gen_tests_storage import (
    strip_all_gen_tests_artifacts as _strip_all_gen_tests_artifacts,
)
from pipeline_core.adapters.model_notes_store import (
    FileModelNotesStore as _FileModelNotesStore,
)
from pipeline_core.adapters.mutation import MutationTester
from pipeline_core.adapters.phase_c_log import write_phase_c_test_fix_log as _write_phase_c_test_fix_log
from pipeline_core.adapters.progress import ChunkProgress
from pipeline_core.adapters.run_banner import banner_lines as _banner_lines
from pipeline_core.adapters.run_registry import FileRunRegistry, RunRegistryEntry
from pipeline_core.adapters.structure import CliStructureReader as _CliStructureReader
from pipeline_core.adapters.structure import fn_name_counts as _fn_name_counts
from pipeline_core.adapters.structure import load_baked_clusters as _load_baked_clusters_impl
from pipeline_core.adapters.structure import source_hash as _source_hash_impl
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
from pipeline_core.adapters.telemetry import JsonlEventEmitter, LogTelemetry
from pipeline_core.adapters.telemetry import emit_per_file_test_overview as _emit_per_file_test_overview
from pipeline_core.adapters.workspace_lifecycle import clean_state as _clean_state_impl
from pipeline_core.adapters.workspace_restore import RestoreFromAugmented
from pipeline_core.adapters.workspace_scan import (
    _dependency_graph_factory,
    _workspace_scanner_factory,
)
from pipeline_core.application import fix_loop as _fix_loop
from pipeline_core.application import orchestrator as _orchestrator
from pipeline_core.application import test_count as _tc_app
from pipeline_core.application.cluster_augment import (
    ClusterAugmentor,
)
from pipeline_core.application.generators.task1_synthesizer import (
    LLMTask1Synthesizer,
    StaticTask1Synthesizer,
    Task1Synthesizer,
)
from pipeline_core.application.parallel import (
    generate_in_parallel as _generate_in_parallel,
)
from pipeline_core.application.reconstruct import attribution as _attribution
from pipeline_core.application.reconstruct import cleanup_residuals as _cleanup_residuals
from pipeline_core.application.reconstruct import cluster_membership as _membership
from pipeline_core.application.reconstruct import fix_diagnostics as _fix_diag
from pipeline_core.application.reconstruct import group_fill as _group_fill
from pipeline_core.application.reconstruct import impl_fill as _impl_fill
from pipeline_core.application.reconstruct import per_fn_retry as _per_fn_retry
from pipeline_core.application.reconstruct import phase_c_tdd as _phase_c_tdd
from pipeline_core.application.reconstruct import source_view as _source_view
from pipeline_core.application.reconstruct import stub_scan as _stub_scan
from pipeline_core.application.reconstruct.failure_diagnosis import (
    classify_phase_c_test_failures as _classify_phase_c_test_failures_impl,
)
from pipeline_core.application.reconstruct.failure_diagnosis import (
    implicated_fns_from_cargo_errors as _implicated_fns_from_cargo_errors_impl,
)
from pipeline_core.application.reconstruct.failure_diagnosis import (
    record_tdd_regression as _record_tdd_regression_impl,
)
from pipeline_core.application.reconstruct.fill_merge import (
    merge_fn_blocks_into_disk as _merge_fn_blocks_into_disk,
)
from pipeline_core.application.reconstruct.fix_unit import FixUnitDeps
from pipeline_core.application.reconstruct.generate_fix import (
    GenerateFixDeps,
    run_generate_fix,
)
from pipeline_core.application.reconstruct.impl_blocks import (
    collect_impl_blocks as _collect_impl_blocks_impl,
)
from pipeline_core.application.reconstruct.macro_invariants import (
    compute_cluster_macro_invariants as _compute_cluster_macro_invariants,
)
from pipeline_core.application.reconstruct.retrospective import (
    diagnose_phase_c as _diagnose_phase_c,
)
from pipeline_core.application.reconstruct.retrospective import (
    whole_tree_retrospective as _whole_tree_retrospective_impl,
)
from pipeline_core.application.reconstruct.source_view import (
    annotate_file_view as _annotate_file_view_impl,
)
from pipeline_core.application.reconstruct.source_view import (
    condense_rust_to_signatures as _condense_rust_to_signatures,
)
from pipeline_core.application.reconstruct.test_grouping import group_tests as _group_tests_impl
from pipeline_core.application.reconstruct.test_locator import (
    find_tests_for_fn as _find_tests_for_fn,
)
from pipeline_core.application.reconstruct.test_locator import (
    find_tests_referencing_fn as _find_tests_referencing_fn,
)
from pipeline_core.application.strip_bake import (
    BakeArtifactsDeps,
    StripBakeDeps,
)
from pipeline_core.application.strip_bake import (
    bake_skeleton_artifacts as _bake_skeleton_artifacts_impl,
)
from pipeline_core.application.strip_bake import (
    strip_and_bake_module as _strip_and_bake_module,
)
from pipeline_core.application.tasks import cfg_scan as _cfg_scan
from pipeline_core.application.tasks.blueprint_phase import BlueprintDeps
from pipeline_core.application.tasks.discover_test_command import (
    discover_test_command as _discover_test_command_impl,
)
from pipeline_core.application.tasks.discover_test_command import (
    infer_build_check_command as _infer_build_check_command,
)
from pipeline_core.application.tasks.genesis_audit import (
    extract_and_store_rules as _extract_and_store_rules,
)
from pipeline_core.application.tasks.genesis_audit import (
    genesis_audit,
)
from pipeline_core.application.tasks.legacy_test_run import (
    LegacyTestRunDeps,
)
from pipeline_core.application.tasks.legacy_test_run import (
    run_legacy_single as _run_legacy_single,
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
from pipeline_core.application.tasks.task4 import task4 as _task4_impl
from pipeline_core.application.workspace_lifecycle import (
    save_gen_tests_siblings as _wl_save_gen_tests_siblings,
)
from pipeline_core.application.workspace_lifecycle import (
    wipe_module_sources as _wl_wipe_module_sources,
)
from pipeline_core.composition import AugmentRoles as _AugmentRoles
from pipeline_core.composition import CompositionConfig as _CompositionConfig
from pipeline_core.composition import FillRoles as _FillRoles
from pipeline_core.composition import Helpers as _Helpers
from pipeline_core.composition import Ports as _Ports
from pipeline_core.composition import ReconstructRoles as _ReconstructRoles
from pipeline_core.composition import build_app as _build_app
from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain import graceful_stop as _graceful_stop
from pipeline_core.domain import test_count as _tc
from pipeline_core.domain.cargo_toml import cargo_default_features as _domain_cargo_default_features
from pipeline_core.domain.cargo_toml import cargo_entry_point_paths as _domain_cargo_entry_point_paths
from pipeline_core.domain.cargo_toml import cargo_is_proc_macro as _domain_cargo_is_proc_macro
from pipeline_core.domain.cargo_toml import cargo_package_name as _domain_cargo_package_name
from pipeline_core.domain.cargo_toml import load_cargo_toml as _domain_load_cargo_toml
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.coverage import CoverageMeasurement
from pipeline_core.domain.coverage_annotation import (
    annotate_uncovered as _annotate_uncovered,
)
from pipeline_core.domain.coverage_annotation import (
    extract_branch_guard as _extract_branch_guard,
)
from pipeline_core.domain.coverage_annotation import (
    extract_uncovered_functions as _extract_uncovered_functions,
)
from pipeline_core.domain.coverage_annotation import (
    filter_cfg_excluded_lines as _filter_cfg_excluded_lines,
)
from pipeline_core.domain.coverage_annotation import (
    filter_cfg_incompatible_lines as _filter_cfg_incompatible_lines,
)
from pipeline_core.domain.coverage_annotation import (
    partition_uncovered_lines as _partition_uncovered_lines,
)
from pipeline_core.domain.crate_name import crate_display_name as _crate_display_name
from pipeline_core.domain.enums import (
    Backend,
    CoverageBackend,
    ModelTier,
    PhaseCDriver,
    PhaseCMode,
    Task1Backend,
    Task2Backend,
)
from pipeline_core.domain.failure_classification import (
    classify_failure as _classify_failure,
)
from pipeline_core.domain.fix_session import FixSession
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
    extract_files,
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
    extract_signature_block as _extract_signature_block,
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
    preserve_model_notes as _preserve_model_notes,
)
from pipeline_core.domain.model_notes import (
    strip_model_notes_section as _strip_model_notes_section,
)
from pipeline_core.domain.models import (
    BuildCluster,
    ConstRecord,
    Diagnostic,
    DispatchFamily,
    FnLocator,
    Frontier,
    ImplBlock,
    ModRecord,
    SkeletoniseResult,
    TypeRecord,
)
from pipeline_core.domain.prompt_budget import PromptBudgets
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refine import (
    parse_retrospective_categories as _parse_retrospective_categories,
)
from pipeline_core.domain.run_state import RunState
from pipeline_core.domain.rust_fn_headers import (
    is_pub_fn as _is_pub_fn,
)
from pipeline_core.domain.rust_fn_headers import (
    locate_fn as _locate_fn,
)
from pipeline_core.domain.rust_idents import (
    camelcase_type_names,
    defined_type_names,
    use_path_requirement_gaps,
)
from pipeline_core.domain.rust_idioms import no_std_invariant_note
from pipeline_core.domain.rust_macros import (
    collect_crate_macros as _collect_crate_macros_pure,
)
from pipeline_core.domain.rust_macros import (
    proc_macro_fallback_requirements as _proc_macro_fallback_requirements,
)
from pipeline_core.domain.rust_scan import (
    count_test_attrs,
    find_mod_opener,
    has_nonempty_test_fn,
    is_structural_brace_line,
    strip_line_block_comments,
)
from pipeline_core.domain.rust_scan import (
    rust_delimiters_balanced as _rust_delimiters_balanced,
)
from pipeline_core.domain.test_grouping import split_oversized_groups as _split_oversized_groups
from pipeline_core.domain.test_results import (
    OUTPUT_SEPARATOR as OUTPUT_SEPARATOR,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.test_results import (
    TestRunOutcome,
    count_passing_tests,
    extract_test_output_from_file,
    insert_no_run,
    is_compiler_output_line,
    rustc_error_codes,
    rustc_error_signatures,
    strip_ansi,
    validate_task3_output_strict,
)
from pipeline_core.domain.test_results import (
    _parse_test_counts as _parse_test_counts,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.text import (
    PromptDigest,
    smart_truncate,
    strip_trailing_file_tag,
)
from pipeline_core.domain.text import (
    block_is_prose as _block_is_prose,
)
from pipeline_core.domain.text import (
    extract_code_content as extract_code_content,  # noqa: PLC0414 (test seam re-export)
)
from pipeline_core.domain.text import (
    flags_unreachable as _flags_unreachable,
)
from pipeline_core.domain.text import (
    parse_attempt_number as _parse_attempt_number,
)
from pipeline_core.domain.text import (
    strip_markdown_fences as _strip_markdown_fences,
)
from pipeline_core.io import read, write
from pipeline_core.ports import structure as _structure_port
from pipeline_core.ports.augment import TestAugmentor
from pipeline_core.prompts import runtime as _prompts_runtime
from pipeline_core.prompts import shaping as _shaping
from pipeline_core.prompts.crate_idioms import (
    extract_crate_idioms as _extract_crate_idioms,
)
from pipeline_core.prompts.per_fn import (  # noqa: F401  (smoke pin: test_pipeline_smoke uses pipeline._build_per_fn_fix_prompt)
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
from pipeline_core.prompts.whole_file_fix import build_whole_file_fix_prompt
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


def _record_tdd_regression(fr_path: Path, src_dir: Path, filled_keys: "list[FnLocator]",
                           broken_tests: "set[str]", prev_best: int, now_pass: int) -> str:
    return _record_tdd_regression_impl(
        fr_path, src_dir, filled_keys, broken_tests, prev_best, now_pass,
        enumerate_fns=_enumerate_fns_via_extractor, append_note=_append_model_note,
        body_cap=_BUDGETS.failed_body)


def _implicated_fns_from_cargo_errors(cargo_output: str, target_src_dir: Path) -> list[FnLocator]:
    return _implicated_fns_from_cargo_errors_impl(
        cargo_output, target_src_dir, enumerate_fns=_enumerate_fns_via_extractor)


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


def _model_tier(model: str) -> ModelTier:
    """Thin shim → pipeline_core.domain.model_caps.model_tier (ModelTier.MID/WEAK)."""
    return _model_tier_pure(model)


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
_EVENT_EMITTER = JsonlEventEmitter(lambda: _EVENTS_LOG, _RUN_CONTEXT)


def _emit(event: str, **fields) -> None:
    _EVENT_EMITTER.emit(event, **fields)


_phase = _EVENT_EMITTER.phase


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
    caller_site=_pclog.caller_site, disclaimer=_from_scratch_disclaimer,
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
    FAILURE_LOG_NAME,
    PROJECT_CONTEXT_NAME,
    PROJECT_PROFILE_NAME,
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
MANIFEST_NAME = ".generated_files.txt"

# Persistent log file written alongside the script.
LOG_NAME = "run.log"


# Workspace directory for cloned/copied project repos (relative to pipeline root).
WORKSPACE_DIR = "tmp"

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


from pipeline_core.domain.generation import (  # noqa: E402
    derive_gentest_prelude as _derive_gentest_prelude_pure,
)
from pipeline_core.domain.generation import extract_test_mod  # noqa: E402
from pipeline_core.domain.generation import (  # noqa: E402
    inject_gen_prelude_imports as _inject_gen_prelude_imports,
)


def generate_in_parallel(
    queries: "list[str]",
    *,
    model: "str | None" = None,
    think: "bool | None" = None,
) -> "list[str]":
    # C11: relocated to application.parallel.generate_in_parallel; shim binds ask + guard.
    return _generate_in_parallel(
        queries, ask=_ASK_CLIENT.ask, guard=_guard, model=model, think=think)


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


def _parse_cmd(cmd: str) -> list[str]:
    """Split a shell command string into a list, falling back to str.split on parse error."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


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
    compile_patterns_read,
    compile_patterns_write,
    genesis_rules_count,
    genesis_rules_read,
    genesis_rules_write,
    type_signals_read,
    type_signals_write,
)
from pipeline_core.application.reconstruct.cargo_errors import (  # noqa: E402
    categorize_compile_error as _categorize_compile_error,
)
from pipeline_core.domain.test_command import (  # noqa: E402
    derive_check_only_cmd as _derive_check_only_cmd,
)

# ---------------------------------------------------------------------------
# BL-023: Test assertion extractor for TASK 2
# ---------------------------------------------------------------------------
from pipeline_core.domain.test_grouping import (  # noqa: E402
    extract_test_assertions as _extract_test_assertions,
)

# Cargo.toml accessors live in domain/cargo_toml.py (pure, tomllib — R8). Re-exported
# under the legacy `_`-names so the ~15 call sites stay unchanged.
_load_cargo_toml = _domain_load_cargo_toml
_cargo_package_name = _domain_cargo_package_name
_cargo_is_proc_macro = _domain_cargo_is_proc_macro
_cargo_default_features = _domain_cargo_default_features

# Workspace/dependency scanners live in adapters/workspace_scan.py (P4). Wire the
# pipeline-bound collaborators into that module here at the composition root.
# #14: explicit constructor injection (was workspace_scan.configure() globals).
def scan_workspace_structure(project_dir: Path) -> str:
    """
    Produce a markdown workspace map for TASK 2.
    Implementation is selected by WORKSPACE_SCANNER in .env (default: llm).
    """
    return _workspace_scanner_factory(_WS_SCAN_DEPS).scan(project_dir)


# ---------------------------------------------------------------------------
# Module lock management
# ---------------------------------------------------------------------------

# load_locked_source_context → pipeline_core.adapters.artifacts.WorkspaceArtifacts.

# ---------------------------------------------------------------------------
# Workspace stub helpers
# ---------------------------------------------------------------------------

_write_workspace_stub = _wsmod.write_workspace_stub


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
from pipeline_core.application.validate import (  # noqa: E402
    validate_task1_output,
    validate_task4_output,
)
from pipeline_core.application.validate import (  # noqa: E402
    validate_task2_api_surface_conformance as _validate_task2_api_impl,
)
from pipeline_core.application.validate import (  # noqa: E402
    validate_task2_output as _validate_task2_output_impl,
)


def validate_task2_output(
    project_dir: Path, expected_paths: list[str], req_path: Path | None = None,
) -> tuple[bool, str]:
    return _validate_task2_output_impl(project_dir, expected_paths, req_path, _VALIDATOR_DEPS)


def validate_task2_api_surface_conformance(
    project_dir: Path, module_name: str, module_dir: Path,
) -> tuple[bool, str]:
    return _validate_task2_api_impl(project_dir, module_name, module_dir, _VALIDATOR_DEPS)


def _test_count_deps() -> "_tc_app.TestCountDeps":
    """Bind the impure collaborators for the relocated cluster test-count
    orchestration (debt #29). Late-bound — the extractor/mod-record/file-cfg
    helpers are defined further down this module."""
    return _tc_app.TestCountDeps(
        enumerate_fns=_enumerate_fns_via_extractor,
        mod_records=_mod_records_cached,
        file_cfg_predicate=_file_cfg_predicate,
        is_gen_tests_file=_is_gen_tests_file,
    )


def _count_test_fns_cfg_aware(
    text: str,
    cfg_true: set[str],
    cfg_false: set[str],
    default_features: "frozenset[str] | set[str]" = frozenset(),
) -> int:
    """Shim → `pipeline_core.domain.test_count.count_test_fns_cfg_aware` (#29)."""
    return _tc.count_test_fns_cfg_aware(text, cfg_true, cfg_false, default_features)






def _count_active_tests_for_cluster(
    project_dir: Path,
    cluster: "BuildCluster | None",
) -> int:
    """Shim → `pipeline_core.application.test_count.count_active_tests_for_cluster`
    (#29). FR-024 strict-verdict gate; impure collaborators injected via
    `_test_count_deps()`."""
    return _tc_app.count_active_tests_for_cluster(
        project_dir, cluster, _test_count_deps())


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
        project_dir, is_gen_tests_decl_only=_is_gen_tests_decl_only_src)


collect_examples = _discovery.collect_examples
collect_protected_test_files = _discovery.collect_protected_test_files


format_test_files = _shaping.format_test_files


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

# Source-file lifecycle (write/delete/lock + manifest/locked ledgers) →
# pipeline_core.adapters.workspace_files; re-bound here by name so the resident
# callers + dep-wiring (e.g. save_manifest_fn) keep resolving unchanged.
delete = _wsfiles.delete
save_manifest = _wsfiles.save_manifest
load_locked_modules = _wsfiles.load_locked_modules
lock_module = _wsfiles.lock_module
write_source_files = _wsfiles.write_source_files
delete_source_files = _wsfiles.delete_source_files


def clean_state(root: Path, project_dir: Path, manifest_path: Path,
                genesis_path: Path, functional_req_path: Path) -> None:
    """Composition-root shim: bind `delete_source_files` into the relocated
    `adapters.workspace_lifecycle.clean_state` (P6)."""
    _clean_state_impl(root, project_dir, manifest_path, genesis_path,
                      functional_req_path, delete_source_files=delete_source_files)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# ---------------------------------------------------------------------------
# Build check (compile-only, no tests)
# ---------------------------------------------------------------------------

def run_build_check(project_dir: Path, build_cmd: str) -> tuple[bool, str]:
    # C7: relocated to adapters.cargo.run_build_check; shim binds env + timeout.
    return _cargo_run_build_check(project_dir, build_cmd,
                                  cargo_env=_cargo_env, build_timeout=BUILD_TIMEOUT)


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

def _is_git_repo(path: Path) -> bool:
    return _wsmod.is_git_repo(path)


# Slice 4b: git workspace I/O relocated to pipeline_core.adapters.workspace. These are
# thin shims delegating to the adapter with injected log/emit and the live `_git_managed`
# flag, until call sites migrate onto a GitWorkspace instance (slice 5).
# S3.5: WorkspaceDeps construction migrated into build_app; re-exported above as _APP.ws_deps.




def git_commit(project_dir: Path, message: str) -> bool:
    return _wsmod.git_commit(_WS_DEPS, project_dir, message, managed=_RUN.git_managed)


def git_tag(project_dir: Path, tag: str) -> None:
    _wsmod.git_tag(_WS_DEPS, project_dir, tag, managed=_RUN.git_managed)


def git_clean_reset(project_dir: Path) -> None:
    _wsmod.git_clean_reset(_WS_DEPS, project_dir)


def setup_workspace(repo: str, workspace_dir: Path, force_fresh: bool = False) -> Path:
    """Composition-root shim → adapters.workspace.setup_workspace (clone/copy a
    repo into tmp/ and check out the pipeline branch). I/O lives in the adapter."""
    return _wsmod.setup_workspace(_WS_DEPS, repo, workspace_dir, force_fresh)


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
)
# Phase 3 S3.5-10: resident pipeline helpers the migrated deps need, late-binding.
_HELPERS = _Helpers(
    parse_cmd=lambda *a, **k: _parse_cmd(*a, **k),
    prompt_budget=lambda *a, **k: prompt_budget(*a, **k),
    ask_retrospective=lambda *a, **k: _ask_retrospective(*a, **k),
    extract_and_store_rules=lambda *a, **k: _task4_extract_and_store_rules(*a, **k),
    enumerate_types=lambda *a, **k: _enumerate_types_via_extractor(*a, **k),
    enumerate_fns=lambda *a, **k: _enumerate_fns_via_extractor(*a, **k),
    git_commit=lambda *a, **k: git_commit(*a, **k),
    task3=lambda *a, **k: task3(*a, **k),
    task0b_discovery=lambda *a, **k: task0b_discovery(*a, **k),
    run_flow_audit=lambda *a, **k: _run_flow_audit(*a, **k),
    load_model_notes=lambda *a, **k: _load_model_notes(*a, **k),
    genesis_rules_count=lambda *a, **k: genesis_rules_count(*a, **k),
    derive_gentest_prelude=lambda *a, **k: _derive_gentest_prelude(*a, **k),
    is_gen_tests_file=lambda *a, **k: _is_gen_tests_file(*a, **k),
    gen_tests_siblings=lambda *a, **k: _gen_tests_siblings(*a, **k),
    load_gen_tests_origin=lambda *a, **k: _load_gen_tests_origin(*a, **k),
    gen_tests_path=lambda *a, **k: _gen_tests_path(*a, **k),
    build_gen_tests_decl=lambda *a, **k: _build_gen_tests_decl(*a, **k),
    cluster_cfg_attr=lambda *a, **k: _cluster_cfg_attr(*a, **k),
    has_gen_tests_decl=lambda *a, **k: _has_gen_tests_decl(*a, **k),
    run_cargo_check_diag=lambda *a, **k: _run_cargo_check_diag(*a, **k),
    attribute_failures=lambda *a, **k: _attribute_failures(*a, **k),
    frontier_from_diags=lambda *a, **k: _frontier_from_diags(*a, **k),
    build_test_reverse_map=lambda *a, **k: _build_test_reverse_map(*a, **k),
    filter_test_targets=lambda *a, **k: _filter_test_targets(*a, **k),
    scan_unimplemented_functions=lambda *a, **k: _scan_unimplemented_functions(*a, **k),
)
# Phase 3 S3.4: TASK-1→4 reconstruction role callables, late-binding (the wrapped
# fns are defined later in this module; lambdas resolve them at call time).
_ROLES = _ReconstructRoles(
    phase_valid=lambda *a, **k: _phase_valid(*a, **k),
    commit_and_mark=lambda *a, **k: _commit_and_mark(*a, **k),
    task1=lambda *a, **k: task1(*a, **k),
    validate_task1_output=lambda *a, **k: validate_task1_output(*a, **k),
    task2_inner_loop=lambda *a, **k: task2_inner_loop(*a, **k),
    load_original_structure=lambda *a, **k: load_original_structure(*a, **k),
    validate_task2_output=lambda *a, **k: validate_task2_output(*a, **k),
    validate_task2_api_conformance=lambda *a, **k: validate_task2_api_surface_conformance(*a, **k),
    task2_fix_loop=lambda *a, **k: task2_fix_loop(*a, **k),
    classify_failure=lambda *a, **k: _classify_failure(*a, **k),
    insert_no_run=lambda *a, **k: insert_no_run(*a, **k),
    fix_err_cap=_FIX_ERR_CAP,
    task4=lambda *a, **k: task4(*a, **k),
    validate_task4_output=lambda *a, **k: validate_task4_output(*a, **k),
    task_genesis_audit=lambda *a, **k: task_genesis_audit(*a, **k),
)
# P1: the real StructureReader — injected into the fill engine (no monkeypatch seam).
_STRUCTURE = _CliStructureReader(ADAPTERS.extractor)
# S21: fill-engine role adapters — focused `adapters.fill_engine_roles`, each
# constructor-injected with late-binding lambdas over the resident engine helpers
# (so the scripted-cargo / scripted-model test monkeypatches still intercept at
# call time). `WorkspaceModelAdapter` carries the P1 constructor-injected
# StructureReader; a test swaps in a fake via `_build_phase_c_engine(structure=fake)`.
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
    test_regression_baseline_fn=lambda tc, cwd, env: _test_regression_baseline(tc, cwd, env),
    test_regression_gate_fn=lambda tc, cwd, env, base: _test_regression_gate(tc, cwd, env, base),
)
_FILL_PROMPT = PromptContextAdapter(
    ask_per_fn_body_fn=lambda prompt, fn, alog, sig_text="", answer_request=None: _ask_per_fn_body(prompt, fn, alog, sig_text=sig_text, answer_request=answer_request),
    gather_fn=lambda **kw: _FIX_CONTEXT.gather(**kw),
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
_FILL_ROLES = _FillRoles(
    tel=_FILL_TEL, mut=_FILL_MUT, cargo=_FILL_CARGO, prompt=_FILL_PROMPT, ws=_FILL_WS,
    retros=_FILL_RETROS, tests=_FILL_TESTS, stall=_FILL_STALL, impl=_FILL_IMPL,
)
# S23: telemetry as a standalone injected value (so module-level consumers like
# _CLUSTER_AUGMENTOR bind it without reading _APP — clears the last _APP-to-end blocker).
_TELEMETRY = LogTelemetry()
# S25: TASK-0c augmentation cluster — built here (resident collaborators) and
# injected into build_app so App.run can drive the augment loop. `augment_module`
# is late-bound (its def is further down this module).
_AUGMENT_STORE = FileAugmentStore()
_AUGMENT_ENGINE = CargoAugmentEngine(
    log=_log, cargo_env=_cargo_env, build_timeout=BUILD_TIMEOUT,
    default_setup=Path(__file__).parent / "setup.sh",
    collect_test_files=collect_test_files,
    augment_module=lambda *a, **k: task0c_augment_module(*a, **k),
)
_CLUSTER_AUGMENTOR = ClusterAugmentor(
    telemetry=_TELEMETRY, scope=_SCOPE, store=_AUGMENT_STORE,
    engine=_AUGMENT_ENGINE,
    gate=CallableCommitGate(lambda *a, **k: _commit_and_mark(*a, **k)),
)
_AUGMENT_ROLES = _AugmentRoles(
    store=_AUGMENT_STORE, engine=_AUGMENT_ENGINE, augmentor=_CLUSTER_AUGMENTOR,
)
# S26: coverage_deps + provider built standalone (no _APP dependency) so module-level
# consumers (_TASK0C_DEPS, the measure shims) reach them before build_app and the
# eventual _APP-to-end move stays unblocked. Injected pre-built into build_app.
_COV_DEPS = _covmod.CoverageDeps(
    log=_log, cargo_env=_cargo_env, cluster=_SCOPE.get_active,
    tarpaulin_bin=TARPAULIN_BIN, grcov_bin=GRCOV_BIN,
    build_timeout=BUILD_TIMEOUT, ignore_pragma=COVERAGE_IGNORE_PRAGMA,
)
_coverage_provider: _covmod.CoverageProvider = _covmod.make_coverage_provider(
    COVERAGE_BACKEND, _COV_DEPS)
_APP = _build_app(ADAPTERS, SETTINGS, _RUN, _SCOPE, _PORTS, _CFG, _ROLES, _HELPERS,
                  _STRUCTURE, _FILL_ROLES, _TELEMETRY, _AUGMENT_ROLES,
                  _COV_DEPS, _coverage_provider)
# S3.5/6/10: these deps are consumed only inside `def` bodies (lazy), so their
# construction migrates into build_app and they're re-exported here by name.
_WS_DEPS = _APP.ws_deps
_WS_SCAN_DEPS = _APP.ws_scan_deps
_VALIDATOR_DEPS = _APP.validator_deps
# #23: build.rs cfg authority — prunes phantom clusters from source `#[cfg]` atoms.
_BUILD_SCRIPT_READER = CargoBuildScriptReader()


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


def _filter_tests_for_cluster(test_files: list[tuple[str, str]],
                              cluster: "BuildCluster | None") -> list[tuple[str, str]]:
    # C4: relocated to cluster_membership.filter_tests_for_cluster (pure).
    return _membership.filter_tests_for_cluster(test_files, cluster)






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
                      cluster: "BuildCluster | None" = None) -> SkeletoniseResult:
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




def _bake_skeleton_artifacts(project_dir: Path, module_name: str) -> dict:
    # C5 (Hard Rule 11): relocated to strip_bake.bake_skeleton_artifacts; shim binds deps.
    return _bake_skeleton_artifacts_impl(project_dir, module_name, _BAKE_ARTIFACTS_DEPS)


def _load_baked_clusters(project_dir: Path) -> list["BuildCluster"]:
    # C5: relocated to adapters.structure.load_baked_clusters.
    return _load_baked_clusters_impl(project_dir)


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


def _task2_group_tests(
    skeleton_dir: Path,
    test_files: list[tuple[str, str]],
    cluster_name: str,
    module_name: str,
) -> list[dict]:
    """Composition-root shim: bind the model/log/skeleton-loader into the
    relocated `application.reconstruct.test_grouping.group_tests` (P6). The `ask`
    module-global is referenced here so its monkeypatch seam survives."""
    return _group_tests_impl(
        skeleton_dir, test_files, cluster_name, module_name,
        ask=_ASK_CLIENT.ask, model=_TASK1_MODEL, log=_log, load_skeleton_files=_load_skeleton_files)


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
def _splice_fn_body_at(file_path: Path, body_byte_start: int, body_byte_end: int,
                       new_body: str, allow_nonstub: bool = False) -> SpliceOutcome:
    return _reconstruct_splice.splice_fn_body_at(
        file_path, body_byte_start, body_byte_end, new_body,
        allow_nonstub=allow_nonstub)


def _locate_fn_by_record(file_path: Path, name: str, container: str, sig_text: str,
                         cfg_predicates: "tuple[str, ...] | None" = None,
                         ) -> "tuple[int, int, int, str] | None":
    return _reconstruct_splice.locate_fn_by_record(
        _enumerate_fns_via_extractor, file_path, name, container, sig_text, cfg_predicates)


def _replace_fn_signature(file_path: Path, fn_name: str, new_sig: str) -> bool:
    return _reconstruct_splice.replace_fn_signature(file_path, fn_name, new_sig)


def _restore_frozen_signature(file_path: Path, fn_name: str, skeleton_sig: str) -> bool:
    return _reconstruct_splice.restore_frozen_signature(file_path, fn_name, skeleton_sig)


def _splice_fn_body(file_path: Path, fn_name: str, new_body: str,
                    allow_nonstub: bool = False) -> SpliceOutcome:
    return _reconstruct_splice.splice_fn_body(
        file_path, fn_name, new_body, allow_nonstub=allow_nonstub)


def _splice_fn_with_where(
    file_path: Path,
    container: str,
    fn_name: str,
    body: str,
    where_clause: str | None = None,
    allow_nonstub: bool = False,
) -> SpliceOutcome:
    return _reconstruct_splice.splice_fn_with_where(
        _enumerate_fns_via_extractor, file_path, container, fn_name, body,
        where_clause, allow_nonstub)




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


def _annotate_file_view(file_text: str, target_fn: str) -> str:
    """Thin shim binding the budget limit; impl in source_view.annotate_file_view."""
    return _annotate_file_view_impl(
        file_text, target_fn, doc_keep_full_limit=_DOC_KEEP_FULL_LIMIT)


def _detect_crate_no_std_invariant(src_dir: Path) -> str:
    # C7: pure decision in domain.rust_idioms.no_std_invariant_note; shim reads lib.rs head.
    lib_rs = src_dir / "src" / "lib.rs" if (src_dir / "src").exists() else src_dir / "lib.rs"
    if not lib_rs.exists():
        return ""
    try:
        head = lib_rs.read_text(encoding="utf-8")[:4000]
    except Exception:
        return ""
    return no_std_invariant_note(head)


def _collect_crate_macros(src_dir: Path, max_chars: int = max(6_000, SINGLE_CALL_CHAR_LIMIT // 25)) -> str:  # Plan 9 F1: budget-derived (was 6000)
    # C7: macro extraction in domain.rust_macros.collect_crate_macros; shim reads the .rs files.
    files: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            files.append((p.name, p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return _collect_crate_macros_pure(files, max_chars)


from pipeline_core.application.reconstruct.scheduling import (  # noqa: E402
    leaf_first_fn_order as _leaf_first_fn_order,
)

# ---------------------------------------------------------------------------
# Model self-correction notes — stored in functional_requirements_{crate}.md
# under the `## Model Notes — Confusions to Avoid` section. The MODEL writes
# entries in its own voice after fix loops; the next attempt reads them as
# part of the per-fn prompt. See CLAUDE.md "The model authors its own
# anti-hallucination notes".
# ---------------------------------------------------------------------------

_MODEL_NOTES_MAX_CHARS = 4000  # cap on the notes section in any one prompt
_MODEL_NOTES_MAX_ENTRIES = 80  # hard cap; older entries dropped LRU on overflow
# Section parse/render (_preserve_model_notes, strip, dedup/rank/promote rules) →
# pipeline_core.domain.model_notes (regex-free). I/O + run-state → the store below.


# Model self-correction notes — the I/O + run-state + LLM-extraction engine now
# lives in adapters/model_notes_store.py (pure rules in domain/model_notes.py).
# The store is built at the composition root; pipeline.py keeps thin
# name-preserving wrappers so the dep-injection sites and test seams are stable.
_MODEL_NOTES_STORE = _FileModelNotesStore(
    _ASK_CLIENT, _TASK2_MODEL, _log,
    max_chars=_MODEL_NOTES_MAX_CHARS,
    max_entries=_MODEL_NOTES_MAX_ENTRIES,
    failed_body_cap=_BUDGETS.failed_body,
    fix_err_cap=_FIX_ERR_CAP,
)

_load_model_notes: Callable[..., list[str]] = _MODEL_NOTES_STORE.load


def _filter_model_notes_for_prompt(
    fr_path: Path,
    file_rel: str,
    fn_name: str,
    file_text: str = "",
    current_error: str = "",
    max_chars: int = _MODEL_NOTES_MAX_CHARS,
) -> str:
    return _MODEL_NOTES_STORE.filter_for_prompt(
        fr_path, file_rel, fn_name, file_text, current_error, max_chars)


def _append_model_note(
    fr_path: Path,
    note: str,
    error_codes: "list[str] | set[str] | None" = None,
) -> bool:
    return _MODEL_NOTES_STORE.append(fr_path, note, error_codes)


def _extract_self_note(
    failed_body: str, error_excerpt: str, fixed_body: str,
) -> "str | None":
    return _MODEL_NOTES_STORE.extract_self_note(failed_body, error_excerpt, fixed_body)


def _record_error_codes_seen(output: str) -> None:
    _MODEL_NOTES_STORE.record_error_codes_seen(output)


def _promoted_error_rules(fr_path: "Path | None" = None) -> str:
    return _MODEL_NOTES_STORE.promoted_error_rules(fr_path)


# Repository Specifications (2026-06-11): the crate's spec-document set —
# functional requirements + genesis (prose + structured rules) + API surface
# — injected into EVERY code-producing prompt from t2 onward via the prompts
# runtime facade. Loaded here (I/O + mtime cache), rendered by the pure
# pipeline_core.prompts.repo_spec selector.
_REPO_SPEC_CACHE: dict[str, tuple[tuple, RepoSpecDocs]] = {}
_FR_PREFIX = "functional_requirements_"


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
            char_budget=char_budget or _BUDGETS.repo_spec,
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


def _whole_tree_retrospective(
    error_excerpt: str,
    history: "list[dict]",
    *,
    name: str = "fix_loop",
) -> str:
    """Thin shim binding the LLM + log + budget; impl in
    application/reconstruct/retrospective.whole_tree_retrospective."""
    return _whole_tree_retrospective_impl(
        error_excerpt, history, name=name,
        ask_retrospective=_ask_retrospective, log=_log, fix_err_cap=_FIX_ERR_CAP)


def _check_graceful_stop(context: str) -> None:
    """Thin delegate to `pipeline_core.domain.graceful_stop` (moved there —
    a worker thread's `sys.exit()` only unwinds itself, so under
    PIPELINE_PARALLELISM>1 the stop signal must be tracked process-wide, not
    just via the sentinel FILE; see that module's docstring)."""
    _graceful_stop.check_graceful_stop(_log, context)


# Cargo diagnostic parsing relocated to application/reconstruct/cargo_errors.py
# (P2 slice 5, regex-free): the rendered-text error-signature fallback is now
# the structured `err_sig_from_text` scan (R7 standing rule).
# JSON-diagnostic cargo check relocated to application/reconstruct/cargo_runner.py
# (P2 slice 6): composes the Cargo PORT + diagnostics_from_json, per the port
# docstring ("JSON-diagnostic checks compose on top in the application layer").
import pipeline_core.application.reconstruct.cargo_runner as _reconstruct_cargo_runner  # noqa: E402
import pipeline_core.application.reconstruct.regression_guard as _regression_guard  # noqa: E402
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
) -> dict[str, set[FnLocator]]:
    """P2: delegates to _attribution.build_test_reverse_map (logic relocated)."""
    return _attribution.build_test_reverse_map(_ATTR_DEPS, test_files, src_dir)


def _filter_test_targets(frontier: Frontier, src_dir: Path) -> Frontier:
    """Drop frontier keys that resolve to a `#[cfg(test)]`/`#[test]` fn (FR-018, #17)."""
    return _reconstruct_test_targets.filter_test_targets(
        frontier, _enumerate_fns_via_extractor(src_dir) or [], log=_log)


def _attribute_failures(
    test_output: str,
    src_dir: Path,
    recently_filled: list[FnLocator],
    test_reverse_map: dict[str, set[FnLocator]],
    cluster_name: str,
    test_texts: "dict[str, str] | None" = None,
) -> tuple[Frontier, str]:
    """P2: delegates to _attribution.attribute_failures (logic relocated)."""
    return _attribution.attribute_failures(
        _ATTR_DEPS, test_output, src_dir, recently_filled, test_reverse_map,
        cluster_name, test_texts=test_texts)


def _run_scoped_tests(
    test_cmd: str, cwd: Path, env: dict, *, timeout: int = 120,
) -> TestRunOutcome:
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


def _test_regression_baseline(tc: "str|None", cwd: Path, env: "dict|None") -> "tuple[int, frozenset]|None":
    return _regression_guard.baseline(_run_cargo, _U2_TEST_TIMEOUT, _UNIFIED_FILL_LOOP, tc, cwd, env)


def _test_regression_gate(tc: "str|None", cwd: Path, env: "dict|None", base: "tuple|None") -> "tuple[frozenset, str]|None":
    return _regression_guard.gate(_run_cargo, _guard, _U2_TEST_TIMEOUT, _UNIFIED_FILL_LOOP, tc, cwd, env, base)


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


# _ask_per_fn_body + _collect_sibling_filled_bodies relocated to pipeline_core (G/P6).
# Tests that patch pipeline._ask_per_fn_body / pipeline._collect_sibling_filled_bodies
# still work: the aliased names remain in this module's namespace.
from pipeline_core.adapters.per_fn_ask import (  # noqa: E402
    ask_per_fn_body as _ask_per_fn_body_impl,
)
from pipeline_core.application.reconstruct.sibling_context import (  # noqa: E402
    collect_sibling_filled_bodies as _collect_sibling_filled_bodies,
)


def _ask_per_fn_body(
    prompt: str, fn_name: str, attempts_log: list[str],
    sig_text: str = "", answer_request: "Any | None" = None,
) -> tuple[str | None, str]:
    return _ask_per_fn_body_impl(
        _ASK_CLIENT, prompt, fn_name, attempts_log,
        sig_text=sig_text, model=_TASK2_MODEL,
        answer_request=answer_request,
    )


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
    bound_methods_section: str = "",
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
        bound_methods_section=bound_methods_section,
    )


# S3.9: PhaseCRetroDeps construction migrated into build_app; re-export by name.
_PHASE_C_RETRO_DEPS = _APP.phase_c_retro_deps


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
    reset_exhaustion_flags=lambda *a, **k: _reset_exhaustion_flags(*a, **k),
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
    extract_impl_where_clause=lambda *a, **k: impl_where_clause(*a, **k),
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
    types_section_for_fix=lambda *a, **k: _FIX_CONTEXT.types_section_for_fix(*a, **k),
    bound_methods_section_for=lambda *a, **k: _FIX_CONTEXT.bound_methods_section_for(*a, **k),
    u2_test_gate=lambda *a, **k: _u2_test_gate(*a, **k),
    test_regression_baseline=lambda *a, **k: _test_regression_baseline(*a, **k),
    test_regression_gate=lambda *a, **k: _test_regression_gate(*a, **k),
    ask=_ASK_CLIENT.ask,
    failed_body_cap=_BUDGETS.failed_body,
    per_fn_fix_hard_cap=_PER_FN_FIX_HARD_CAP,
    per_impl_max_stubs=_PER_IMPL_MAX_STUBS,
    task2_model=_TASK2_MODEL,
)


import pipeline_core.application.reconstruct.cargo_errors as _reconstruct_cargo_errors  # noqa: E402
import pipeline_core.application.reconstruct.fix_context as _fix_context  # noqa: E402
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
from pipeline_core.prompts.per_fn import build_per_fn_prompt as _build_per_fn_prompt  # noqa: E402,F401

# S21: the fill-engine role adapters (`_FILL_*`) + `_STRUCTURE` are built BEFORE
# `_APP` (their lambdas are late-bound so order is irrelevant) and bundled into
# `_APP.fill_roles` — construction stays here (the seam lambdas close over pipeline
# globals); the engine factory below sources them from `_APP`. See composition.FillRoles.


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
    *args: Any,
    structure: "_structure_port.StructureReader | None" = None,
    **kwargs: Any,
) -> dict:
    """Phase C per-cluster fill engine shim → `PhaseCFillEngine.run` (the typed
    signature + pass/worker orchestration live there); `structure` is the P1
    test-injection seam (fake StructureReader)."""
    return _build_phase_c_engine(structure=structure).run(*args, **kwargs)


def _task2_fill_groups(skeleton_dir, target_src_dir, groups, project_dir, **kwargs):
    """P4: delegates to application.reconstruct.group_fill (logic relocated)."""
    return _group_fill.task2_fill_groups(
        _GROUP_FILL_DEPS, skeleton_dir=skeleton_dir, target_src_dir=target_src_dir,
        groups=groups, project_dir=project_dir, **kwargs)


_GROUP_FILL_DEPS = _group_fill.GroupFillDeps(
    log=lambda m: _log(m),
    ask=_ASK_CLIENT.ask,
    extract_fn_blocks=lambda *a, **k: _extract_fn_blocks(*a, **k),
    format_skeleton_index_for_cluster=lambda *a, **k: _format_skeleton_index_for_cluster(*a, **k),
    leaf_first_group_order=lambda *a, **k: _leaf_first_group_order(*a, **k),
    load_skeleton_files=lambda *a, **k: _load_skeleton_files(*a, **k),
    merge_fn_blocks_into_disk=lambda *a, **k: _merge_fn_blocks_into_disk(*a, **k),
    scan_unimplemented_functions=lambda *a, **k: _scan_unimplemented_functions(*a, **k),
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    genesis_cap=_BUDGETS.genesis,
    task2_model=_TASK2_MODEL,
)




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
    # C4: relocated to cluster_membership.cluster_filter_type_methods (pure).
    return _membership.cluster_filter_type_methods(records, cluster)






def _fn_container_index(src_dir: Path) -> dict[FnLocator, str]:
    """(file_rel_to_src, fn_name) → container ("impl … for …" / "trait …" / "").

    Slice 8a (FnContext): the syn enumerator is the one source of truth for
    which impl owns a fn — every retry site needs this to resolve the
    self-type whose definition must be injected (the BoxedError gap). First
    record wins for duplicate names, matching the previous inline builders.
    """
    idx: dict[FnLocator, str] = {}
    for rec in (_enumerate_fns_via_extractor(src_dir) or []):
        idx.setdefault(FnLocator(rec.get("file", ""), rec.get("fn_name", "")),
                       rec.get("container", ""))
    return idx


# Per-fn fix-prompt sections — the assembler use-case (god-file shrink,
# 2026-07-04); lambdas late-bind the notes store + guard for test seams.
_FIX_CONTEXT: _fix_context.FixContextAssembler = _fix_context.FixContextAssembler(
    budgets=_BUDGETS,
    filter_notes=lambda *a, **k: _filter_model_notes_for_prompt(*a, **k),
    guard=lambda *a: _guard(*a),
)




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
def _collect_impl_blocks_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[ImplBlock] | None:
    # C4: relocated to cluster_membership; shim injects extractor + fn_belongs.
    return _membership.collect_impl_blocks_for_cluster(
        target_src_dir, cluster,
        collect_impl_blocks=_collect_impl_blocks, fn_belongs=_fn_belongs_to_cluster)


def _scan_unimplemented_records_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[dict] | None:
    # C4: relocated to cluster_membership; shim injects extractor + fn_belongs + log.
    return _membership.scan_unimplemented_records_for_cluster(
        target_src_dir, cluster,
        scan_records=_scan_unimplemented_records, fn_belongs=_fn_belongs_to_cluster,
        log=_log)


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


def _strip_ansi(text: str) -> str:
    """Drop ANSI SGR colour escapes (cargo's rendered-ansi diagnostics)."""
    return strip_ansi(text or "")


from pipeline_core.application.reconstruct.scheduling import (  # noqa: E402
    order_frontier as _order_frontier,
)


def _scan_unimplemented_functions(src_dir: Path) -> list[FnLocator]:
    """Composition shim → application.reconstruct.stub_scan (extractor injected)."""
    return _stub_scan.scan_unimplemented_functions(
        src_dir, enumerate_fns=_enumerate_fns_via_extractor)


# ---------------------------------------------------------------------------
# Cluster-aware stub filter (P5 Step 1, focused fix). Filters stubs to only
# those whose enclosing cfg evaluates true for the current cluster. Skips
# fns that wouldn't compile in this cluster anyway (e.g., nightly.rs items
# being scanned in the stable cluster). Pure static analysis, no LLM.
# ---------------------------------------------------------------------------

# Inline `#[cfg(X)]` predicate extraction (incl. the spaced skeleton form
# `# [cfg (X)] mod foo ;`) is done via domain.cfg.cfg_inline_predicates.




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
    """Composition shim → application.reconstruct.source_view (extractors injected)."""
    return _source_view.public_api_from_source(
        content,
        enumerate_types=_enumerate_types_via_extractor,
        enumerate_fns=_enumerate_fns_via_extractor,
        enumerate_consts=_enumerate_consts_via_extractor,
    )


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
    """Composition shim → application.reconstruct.cluster_membership
    (mod-records cache injected; falls back to the structured scan)."""
    return _membership.file_cfg_predicate(
        target_src_dir, file_rel, mod_records=_mod_records_cached)


def _invalidate_mod_records_cache() -> None:
    """Clear the mod-records cache. Call after any operation that changes
    the on-disk mod declarations in lib.rs/main.rs (strip, task1 reset,
    task2 regeneration). Cheap enough that callers should err on the side
    of invalidation."""
    _MOD_RECORDS_CACHE.clear()
    _CLUSTER_SPECIFIC_SYMS_CACHE.clear()  # #26 residual: same invalidation lifecycle




def _fn_belongs_to_cluster(
    target_src_dir: Path,
    file_rel: str,
    fn_name: str,
    cluster: "BuildCluster",
    fn_cfg_predicates: "list[str] | None" = None,
) -> bool:
    """Composition shim → application.reconstruct.cluster_membership
    (mod-records cache injected)."""
    return _membership.fn_belongs_to_cluster(
        target_src_dir, file_rel, fn_name, cluster, fn_cfg_predicates,
        mod_records=_mod_records_cached)


def _scan_unimplemented_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[FnLocator]:
    """Composition shim → application.reconstruct.cluster_membership."""
    return _membership.scan_unimplemented_for_cluster(
        target_src_dir, cluster,
        enumerate_fns=_enumerate_fns_via_extractor,
        mod_records=_mod_records_cached, log=_log)


_CLEANUP_RESIDUALS_DEPS = _cleanup_residuals.CleanupResidualsDeps(
    scan_unimplemented=lambda tsd: _scan_unimplemented_functions(tsd),
    load_skeleton_files=lambda tsd: _load_skeleton_files(tsd),
    ask=lambda *a, **k: _ASK_CLIENT.ask(*a, **k),
    task2_model=_TASK2_MODEL,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    file_format_instruction=FILE_FORMAT_INSTRUCTION,
    log=_log,
)


def _task2_cleanup_residuals(
    target_src_dir: Path,
    project_dir: Path,
    functional_req: str,
    test_files: list[tuple[str, str]],
    module_name: str,
) -> dict:
    """Composition shim → application.reconstruct.cleanup_residuals (Phase D)."""
    return _cleanup_residuals.cleanup_residuals(
        target_src_dir, project_dir, functional_req, test_files, module_name,
        deps=_CLEANUP_RESIDUALS_DEPS)










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




# _write_build_configs → pipeline_core.adapters.workspace_files (imported as _wsfiles).
_write_build_configs = _wsfiles.write_build_configs


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
    # C5: relocated to adapters.structure.source_hash; shim injects the gen_tests helpers.
    return _source_hash_impl(
        project_dir,
        is_gen_tests_sibling_name=_is_gen_tests_sibling_name,
        strip_gen_tests_decls=_strip_gen_tests_decls)


# Slice 9: the pipeline_state.md store moved to pipeline_core/adapters/state.py
# (regex-free line parser; same file format, locking, atomic replace). Aliases
# keep every call site + the task-deps records unchanged.
_state_path = _state_mod.state_path
_read_state = _state_mod.read_state
_write_state = _state_mod.write_state
_mark_phase = _state_mod.mark_phase
_set_current = _state_mod.set_current
_phase_valid: Callable[[Path, str], bool] = _state_mod.phase_valid
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
    """Commit-then-mark shim → adapters.commit_gate.commit_and_mark (which
    carries the ordering rationale); binds the live git/state primitives."""
    _commit_and_mark_impl(
        project_dir, phase_key, status, commit_message=commit_message, tag=tag,
        detail=detail, source_hash=source_hash,
        git_commit=git_commit, git_tag=git_tag, mark_phase=_mark_phase)



# slice 5b: deps for the extracted task0 doc tasks (profile + context).
# S3.8: Task0DocsDeps construction migrated into build_app; re-export by name.
_TASK0_DOCS_DEPS = _APP.task0_docs_deps


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
        readme_cap=_BUDGETS.readme,
        strategy_cap=_BUDGETS.strategy,
        chunk_cache_dir=CHUNK_CACHE_DIR,
        parallelism=_pipeline_parallelism(),
    )


def _filter_proc_macro_test_files(test_files, crate_name: str = ""):
    """Keep test files that use #[derive(...)] or the crate's own #[crate_name(...)]
    attribute. Falls back to all files if none match (literal substring scan)."""
    needles = ["#[derive("]
    if crate_name:
        needles.append(f"#[{crate_name.replace('-', '_')}(")
    relevant = [
        (rel, content) for rel, content in test_files
        if any(nd in content for nd in needles)
    ]
    return relevant or test_files




def _new_static_task1_synthesizer() -> StaticTask1Synthesizer:
    return StaticTask1Synthesizer(llm=_GEN_LLM, shaping=_GEN_SHAPING, progress=_GEN_PROGRESS,
                                  requirements=_GEN_REQUIREMENTS, cfg=_build_task1_config())


def _new_llm_task1_synthesizer() -> LLMTask1Synthesizer:
    return LLMTask1Synthesizer(llm=_GEN_LLM, shaping=_GEN_SHAPING, progress=_GEN_PROGRESS,
                               requirements=_GEN_REQUIREMENTS, cfg=_build_task1_config())


def _task1_synthesizer_factory() -> Task1Synthesizer:
    backend = Task1Backend.coerce(os.getenv("TASK1_SYNTHESIZER"), Task1Backend.LLM)
    if backend is Task1Backend.STATIC:
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
from pipeline_core.domain.generation import (  # noqa: E402
    SkeletonConfig,
    Task1Config,
    Task2Config,
)
from pipeline_core.domain.generation import (  # noqa: E402
    build_gen_tests_decl as _build_gen_tests_decl,
)
from pipeline_core.domain.generation import (  # noqa: E402
    extract_gen_tests_body as _extract_gen_tests_body,
)
from pipeline_core.domain.generation import (  # noqa: E402
    gen_tests_origin_cfgs as _gen_tests_origin_cfgs,
)
from pipeline_core.domain.generation import (
    has_gen_tests_mod_decl as _has_gen_tests_mod_decl,
)
from pipeline_core.domain.generation import (
    is_gen_tests_sibling_name as _is_gen_tests_sibling_name,
)
from pipeline_core.domain.generation import (
    prelude_import_names as _prelude_import_names,  # noqa: E402
)
from pipeline_core.domain.generation import (
    strip_all_gen_tests_decls as _strip_all_gen_tests_decls,  # noqa: E402
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
_GEN_LLM = LlmAskAdapter(ask_fn=lambda prompt, *, model=None: _ASK_CLIENT.ask(prompt, model=model))
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
    cargo_env=_cargo_env, ask=_ASK_CLIENT.ask, log=_log,
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
_GEN_PROGRESS = ProgressFactoryAdapter(
    make_progress_fn=lambda total: ChunkProgress(total, progress_mode=_RUN.progress_mode))
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
        genesis_cap=_BUDGETS.genesis,
        strategy_cap=_BUDGETS.strategy,
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
        cfg=_build_skeleton_config(),
        # P3 blueprint deps — ask late-bound over the module global so the
        # scripted-model e2e seam (pipeline._ask_raw) intercepts.
        blueprint_deps=BlueprintDeps(
            ask=lambda *a, **k: _ask_raw(*a, **k),
            log=lambda m: _log(m), emit=lambda *a, **k: _emit(*a, **k),
            model=_TASK2_MODEL,
            dispatch_families=lambda *a, **k: _dispatch_families_via_extractor(*a, **k)))


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
    return len(set(rustc_error_codes(output or "", "E")))


def _task2_generator_factory() -> Task2Generator:
    backend = Task2Backend.coerce(os.getenv("TASK2_GENERATOR"), Task2Backend.FILELIST)
    if backend is Task2Backend.LLM:
        return _new_llm_task2_generator()
    if backend is Task2Backend.STATIC:
        return _new_static_task2_generator()
    if backend is Task2Backend.SKELETON:
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
    """S28: body relocated to fix_loop.generate_fix_loop; this shim binds deps."""
    return _fix_loop.generate_fix_loop(
        _GEN_FIX_LOOP_DEPS, project_dir, check_cmd, source_paths, existing_test_files,
        extra_context=extra_context, model=model, max_fix_attempts=max_fix_attempts,
        timeout=timeout, allowed_dir_prefix=allowed_dir_prefix, functional_req=functional_req,
        fr_path=fr_path, check_fn=check_fn)


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
    genesis_cap=_BUDGETS.genesis,
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
    """Composition-root shim -> per_fn_retry.run_task3_inline_retry (chunk H,
    deps migrated into build_app in S27). The deps' fill-role adapters +
    Helpers lambdas late-bind the module globals, so the scripted-cargo /
    scripted-model monkeypatch seams (test_e2e_build_fix / test_e2e_routing)
    still intercept at call time; env knobs are read per call in pipeline_core.
    """
    return _per_fn_retry.run_task3_inline_retry(
        project_dir, test_command, iteration, initial_test_output, cluster,
        deps=_APP.task3_retry_deps)


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
    # C9: relocated to genesis_audit.extract_and_store_rules; shim binds the DB writer.
    _extract_and_store_rules(project_dir, genesis_text, module_scope, failure_type,
                             iteration, genesis_rules_write=genesis_rules_write)


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
    reqs = read(functional_req_path)
    if not reqs.strip():
        return []

    found_count, gaps = use_path_requirement_gaps(reqs, test_files)

    _log(f"\nTASK 1.5: {found_count} use-paths in tests, {len(gaps)} missing from requirements.")
    if gaps:
        for g in gaps[:10]:
            _log(f"  missing: {g}")
        if len(gaps) > 10:
            _log(f"  ... and {len(gaps) - 10} more")
    return gaps


# ---------------------------------------------------------------------------
# Genesis audit — detect and resolve contradictions in genesis.md
# ---------------------------------------------------------------------------


# Phase 3 S3 slice 1: genesis-audit deps now CONSTRUCTED in composition.build_app;
# pipeline re-exports the name so call sites + tests are unchanged.
_GENESIS_AUDIT_DEPS = _APP.genesis_audit_deps


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
# S3.7: Task4Deps construction migrated into build_app; re-export by name.
_TASK4_DEPS = _APP.task4_deps


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


def _run_tarpaulin(project_dir: Path, module_name: str) -> CoverageMeasurement:
    # Thin shim until call sites migrate (slice 5).
    return _covmod.run_tarpaulin(_COV_DEPS, project_dir, module_name)


def _quick_file_coverage(project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
    """Per-file coverage snapshot. Delegates to the active CoverageProvider."""
    return _coverage_provider.measure_file(project_dir, module_name, rel_path)


def _measure_coverage(project_dir: Path, module_name: str) -> CoverageMeasurement:
    _t0 = time.monotonic()
    pct, files_data, covered, coverable = _coverage_provider.measure(project_dir, module_name)
    _emit("coverage.measured",
          module=module_name,
          cluster=_SCOPE.active.name if _SCOPE.active is not None else None,
          duration_ms=int((time.monotonic() - _t0) * 1000),
          data={"pct": pct, "covered": covered, "coverable": coverable,
                "backend": COVERAGE_BACKEND, "file_count": len(files_data)})
    return CoverageMeasurement(pct, files_data, covered, coverable)


# S26: _coverage_provider constructed standalone above (before _APP); also injected
# into build_app. No longer an _APP re-export → unblocks the _APP-to-end move.




def _strip_gen_tests_decls(src_text: str) -> str:
    """Remove all gen_tests path-mod decls (R7: generation.strip_all_gen_tests_decls)."""
    return _strip_all_gen_tests_decls(src_text)


# Sibling-name parser + predicate live in discovery (P5); shims keep the
# private names the call sites already use.
_match_gen_tests_sibling = _discovery.match_gen_tests_sibling
_is_gen_tests_file: Callable[[str], bool] = _discovery.is_gen_tests_file


def _strip_gen_tests_decl_n(parent_text: str, pass_n: int) -> str:
    """Remove the gen_tests_<N> decl for one pass (R7: generation.strip_gen_tests_decl_n)."""
    return _domain_strip_gen_tests_decl_n(parent_text, pass_n)


def _error_signatures_from_output(cargo_output: str) -> frozenset:
    """Stable rustc error signatures (R7: test_results.rustc_error_signatures)."""
    return rustc_error_signatures(cargo_output)


# Prelude imports injected into generated test modules when absent. Each row is
# (import_stmt, accepted `use`-prefixes, required symbol token): present when a
# `use <prefix>…<symbol>…` statement exists (R7: rust_scan.use_imports_symbol).


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




def _count_test_attrs(block: str) -> int:
    """Count `#[test]` attrs (R7: domain.rust_scan.count_test_attrs)."""
    return count_test_attrs(block)


def _strip_trailing_file_tag(text: str) -> str:
    """Strip a trailing `</FILE>` tag (R7: domain.text.strip_trailing_file_tag)."""
    return strip_trailing_file_tag(text)


def _has_nonempty_test_body(block: str) -> bool:
    """True if the block has at least one `#[test]` fn with a non-trivial body."""
    return has_nonempty_test_fn(block)




def _is_structural_line(text: str) -> bool:
    """True for structural-only Rust lines (`}`, `} else {`, etc.) that LLVM miscounts.
    (Regex in pipeline.)"""
    return is_structural_brace_line(text)


# slice 5b: deps for the extracted TASK 0b discovery (defined after its parsers).
_TASK0B_DEPS = Task0bDeps(
    ask=_ASK_CLIENT.ask,
    write=write,
    is_test_file=is_test_file,
    is_gen_tests_file=_is_gen_tests_file,
    strip_gen_tests_decls=_strip_gen_tests_decls,
    condense_rust_to_signatures=_condense_rust_to_signatures,
    manifest_name=MANIFEST_NAME,
    sibling_cap=_BUDGETS.sibling,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    task0_model=_TASK0_MODEL,
)

# Phase 3 S3 slice 2: constructed in composition.build_app; re-exported by name.
_DISCOVER_TEST_CMD_DEPS = _APP.discover_test_cmd_deps


def _has_gen_tests_decl(src_text: str, pass_n: int) -> bool:
    """True if src_text contains a `mod gen_tests_<pass_n>;` declaration.
    Used by the TASK 2 reattach path to detect when a sibling exists on disk
    but its declaration was dropped during regeneration of the source file."""
    return _has_gen_tests_mod_decl(src_text, pass_n)


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
    # C4: relocated to cluster_membership.cluster_cfg_attr; shim passes _SCOPE.all.
    return _membership.cluster_cfg_attr(cluster, _SCOPE.all)


_CLUSTER_SPECIFIC_SYMS_CACHE: dict[str, frozenset[str]] = {}


def _cluster_specific_symbols(project_dir: "Path | None",
                              contexts: "list") -> frozenset[str]:
    """Crate fn names compiled OUT under some cluster (#26 residual), cached per
    project. Enumerated once via the syn extractor; the non-test fn cfg gates are
    stable across a cluster's augmentation (added gen_tests siblings are
    `#[cfg(test)]`-only → never cluster-specific), so caching is safe. Cleared
    alongside `_MOD_RECORDS_CACHE`."""
    if project_dir is None:
        return frozenset()
    key = str(project_dir)
    if key not in _CLUSTER_SPECIFIC_SYMS_CACHE:
        recs = _enumerate_fns_via_extractor(project_dir) or []
        _CLUSTER_SPECIFIC_SYMS_CACHE[key] = _cfg.cluster_specific_symbol_names(recs, contexts)
    return _CLUSTER_SPECIFIC_SYMS_CACHE[key]


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
        lambda: _cluster_specific_symbols(project_dir, contexts))



def _propagate_or_quarantine_sibling(
    gt: Path, origin_manifest: dict, parent_stem: str, pass_n: int,
    log_fn=None, tag: str = "",
) -> bool:
    # C4: relocated verbatim to cluster_membership.propagate_or_quarantine_sibling.
    return _membership.propagate_or_quarantine_sibling(
        gt, origin_manifest, parent_stem, pass_n, log_fn, tag)








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





def _extract_public_symbols(content: str) -> str:
    """Extract public API from source with full signatures and field types.

    Returns a structured listing that lets the model construct types correctly
    without guessing field names, enum variants, or function parameter types.

    Parsing goes through the syn extractor — NOT regex (Hard Rule: no regex on Rust
    source). See `_public_api_from_source`.
    """
    return _public_api_from_source(content)


# TestAugmentor (ABC) → ports/augment.py; StaticRustTestAugmentor →
# adapters/augment_engine.py (C10). The factory binds the cargo collaborators.
def _new_static_rust_augmentor() -> TestAugmentor:
    return StaticRustTestAugmentor(
        cargo_env=_cargo_env, build_timeout=BUILD_TIMEOUT,
        measure_coverage=_measure_coverage)


def _test_augmentor_factory() -> TestAugmentor:
    backend = os.getenv("TEST_AUGMENTOR", "static").lower()
    if backend != "static":
        _log(f"  WARNING: unknown TEST_AUGMENTOR='{backend}', falling back to static.")
    return _new_static_rust_augmentor()


def _derive_gentest_prelude(project_dir: Path) -> tuple[str, str]:
    # C12: pure logic in domain.generation.derive_gentest_prelude; shim reads lib/main.rs.
    lib_rs = project_dir / "src" / "lib.rs"
    if not lib_rs.exists():
        lib_rs = project_dir / "src" / "main.rs"
    text = lib_rs.read_text(encoding="utf-8") if lib_rs.exists() else ""
    return _derive_gentest_prelude_pure(text)


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
    ask=_ASK_CLIENT.ask,
    write_fr_preserving_notes=_write_fr_preserving_notes,
    synthesizer_factory=_task1_synthesizer_factory,
    filter_proc_macro_test_files=_filter_proc_macro_test_files,
    proc_macro_fallback_requirements=_proc_macro_fallback_requirements,
    format_api_surface_section=_format_api_surface_section,
    file_format_instruction=FILE_FORMAT_INSTRUCTION,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    profile_cap=_BUDGETS.profile,
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




# --best-models-list body relocated to adapters.model_catalog.print_best_models.
cmd_best_models = _model_catalog.print_best_models


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

# S28: deps for the relocated fix-loop body (fix_loop.generate_fix_loop).
_GEN_FIX_LOOP_DEPS = _fix_loop.GenerateFixLoopDeps(
    set_current=_set_current,
    BUILD_TIMEOUT=BUILD_TIMEOUT,
    FILE_FORMAT_INSTRUCTION=FILE_FORMAT_INSTRUCTION,
    FixSession=FixSession,
    FixUnitDeps=FixUnitDeps,
    GenerateFixDeps=GenerateFixDeps,
    MODEL=MODEL,
    settings_now=lambda: SETTINGS,
    SINGLE_CALL_CHAR_LIMIT=SINGLE_CALL_CHAR_LIMIT,
    SOURCE_EXTENSIONS=SOURCE_EXTENSIONS,
    ASK_CLIENT=_ASK_CLIENT,
    FIX_ERR_CAP=_FIX_ERR_CAP,
    FIX_THINKING_DIRECTIVE=_FIX_THINKING_DIRECTIVE,
    FixCheckerAdapter=_FixCheckerAdapter,
    FixModelAdapter=_FixModelAdapter,
    FixPromptsAdapter=_FixPromptsAdapter,
    FixSplicerAdapter=_FixSplicerAdapter,
    FrontierBuilderAdapter=_FrontierBuilderAdapter,
    REPO_SPEC_CAP_WIDE=_BUDGETS.repo_spec_wide,
    SRC_SNIP_CAP=_BUDGETS.src_snippet,
    TASK2_VERBOSITY_GUARD=_TASK2_VERBOSITY_GUARD,
    TYPES_SECTION_CAP=_BUDGETS.types_section,
    behavior_frontier=lambda *a, **k: _behavior_frontier(*a, **k),
    cargo_env=_cargo_env,
    categorize_compile_error=_categorize_compile_error,
    collect_impl_blocks=lambda *a, **k: _collect_impl_blocks(*a, **k),
    compile_error_frontier=lambda *a, **k: _compile_error_frontier(*a, **k),
    derive_check_only_cmd=_derive_check_only_cmd,
    emit=lambda *a, **k: _emit(*a, **k),
    emit_per_file_test_overview=_emit_per_file_test_overview,
    # late-bind: the e2e harness monkeypatches pipeline._enumerate_fns_via_extractor,
    # so resolve it from the module global at call time (not frozen at construction).
    enumerate_fns_via_extractor=lambda *a, **k: _enumerate_fns_via_extractor(*a, **k),
    enumerate_types=lambda *a, **k: _enumerate_types_via_extractor(*a, **k),
    types_section_for_fix=lambda *a, **k: _FIX_CONTEXT.types_section_for_fix(*a, **k),
    check_stop=lambda ctx: _check_graceful_stop(ctx),
    extract_tdd_frontier=lambda *a, **k: _extract_tdd_frontier(*a, **k),
    filter_test_targets=_filter_test_targets,
    fix_diag=_fix_diag,
    frontier_from_diags=lambda *a, **k: _frontier_from_diags(*a, **k),
    gotchas_for_error=_gotchas_for_error,
    gotchas_summary=_gotchas_summary,
    is_pub_fn=_is_pub_fn,
    locate_fn=_locate_fn,
    log=_log,
    parse_cmd=_parse_cmd,
    render_impl_block=_render_impl_block,
    repo_spec_section=_repo_spec_section,
    restore_frozen_signature=_restore_frozen_signature,
    run_cargo_check_diag=lambda *a, **k: _run_cargo_check_diag(*a, **k),
    splice_fn_body=_splice_fn_body,
    splice_fn_with_where=_splice_fn_with_where,
    strip_ansi=_strip_ansi,
    tag_block=_tag_block,
    whole_tree_retrospective=_whole_tree_retrospective,
    build_whole_file_fix_prompt=build_whole_file_fix_prompt,
    compile_patterns_write=compile_patterns_write,
    extract_files=extract_files,
    is_compiler_output_line=is_compiler_output_line,
    run_generate_fix=run_generate_fix,
    write=write,
)

_TASK0C_DEPS = Task0cDeps(
    BUILD_TIMEOUT=BUILD_TIMEOUT, COVERAGE_TARGET=COVERAGE_AUGMENTATION_TARGET, coverage_ignore_pragma=COVERAGE_IGNORE_PRAGMA, TASK0C_PROMPT_CHAR_CAP=TASK0C_PROMPT_CHAR_CAP, FIX_ERR_CAP=_FIX_ERR_CAP, SIBLING_CAP=_BUDGETS.sibling, SRC_SNIP_CAP=_BUDGETS.src_snippet, TASK0C_FIX_THINKING_DIRECTIVE=_TASK0C_FIX_THINKING_DIRECTIVE, TASK0C_MODEL=_TASK0C_MODEL, TASK0C_THINKING_DIRECTIVE=_TASK0C_THINKING_DIRECTIVE, WORKING_CAP=_BUDGETS.working, annotate_uncovered=_annotate_uncovered, block_is_prose=_block_is_prose, build_gen_tests_decl=_build_gen_tests_decl, guard=_guard, cargo_env=_cargo_env, cluster_gate_for=_cluster_gate_for_sibling, condense_rust_to_signatures=_condense_rust_to_signatures, count_test_attrs=_count_test_attrs, derive_gentest_prelude=_derive_gentest_prelude, describe_test_environment=_describe_test_environment, error_signatures_from_output=_error_signatures_from_output, extract_branch_guard=_extract_branch_guard, extract_gen_tests_body=_extract_gen_tests_body, extract_public_symbols=_extract_public_symbols, extract_uncovered_functions=_extract_uncovered_functions, filter_cfg_excluded_lines=_filter_cfg_excluded_lines, filter_cfg_incompatible_lines=_filter_cfg_incompatible_lines, flags_unreachable=_flags_unreachable, gen_tests_path=_gen_tests_path, has_nonempty_test_body=_has_nonempty_test_body, inject_allow_before_any_mod=_inject_allow_before_any_mod, inject_allow_before_gen_mod=_inject_allow_before_gen_mod, inject_gen_prelude_imports=_inject_gen_prelude_imports, is_gen_tests_file=_is_gen_tests_file, is_structural_line=_is_structural_line, load_file_constraints=_FILE_CONSTRAINTS.load, type_signals_write=type_signals_write, mark_phase=_mark_phase, match_gen_tests_sibling=_match_gen_tests_sibling, next_gen_tests_n=_next_gen_tests_n, nightly_rules_for_cluster=_nightly_rules_for_cluster, parse_attempt_number=_parse_attempt_number, parse_retrospective_categories=_parse_retrospective_categories, partition_uncovered_lines=_partition_uncovered_lines, prelude_import_names=_prelude_import_names, quick_file_coverage=_quick_file_coverage, read_all_gen_tests=_read_all_gen_tests, read_state=_read_state, revert_gen_tests_pass=_revert_gen_tests_pass, select_relevant_siblings=_select_relevant_siblings, set_current=_set_current, source_hash=_source_hash, strip_all_gen_tests_artifacts=_strip_all_gen_tests_artifacts, strip_gen_tests_decl_n=_strip_gen_tests_decl_n, strip_gen_tests_decls=_strip_gen_tests_decls, strip_trailing_file_tag=_strip_trailing_file_tag, mutation_harden_file=lambda *a, **k: _MUTATION.harden_file(*a, **k), gen_tests_siblings=_gen_tests_siblings, test_augmentor_factory=_test_augmentor_factory, ask=_ASK_CLIENT.ask, extract_files=extract_files, extract_test_mod=extract_test_mod, generate_fix_loop=generate_fix_loop, generate_in_parallel=generate_in_parallel, is_test_file=is_test_file, write=write,
    subprocess=subprocess, get_current_cluster=_SCOPE.get_active,
    coverage_provider=_coverage_provider, check_stop=_check_graceful_stop,
)

# P6: pre-strip API-surface capture — focused adapter, composition-injected.
_API_SURFACE = ApiSurfaceCapture(
    api_surface_path=_api_surface_path, type_manifest_path=_type_manifest_path,
    condense_to_signatures=_condense_rust_to_signatures,
    strip_gen_tests_decls=_strip_gen_tests_decls,
    enumerate_types=ADAPTERS.extractor.enumerate_types,
    is_test_file=is_test_file, skip_dir_names=frozenset(SKIP_DIR_NAMES), log=_log,
    enumerate_fns=lambda sd: _enumerate_fns_via_extractor(sd),
    pub_items_manifest_path=_pub_items_manifest_path,
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

# S24: the role adapters (_PHASE_GATE/…/_ANALYST) + spine use-cases
# (_LOCK_DEPS/_REATTACH_DEPS/_RECONSTRUCTION_STEP/_FAILURE_ANALYSIS/_COVERAGE_DOCS/
# _FLOW_AUDIT/_DOCS_METRICS/_FAILURE_JOURNAL/_WORKSPACE_ARTIFACTS/_TEST_RUNNER) are
# consumed only INSIDE build_app now → their pipeline re-exports were dead and deleted.
# main() reads only these two:
_REQUIREMENTS_PHASE = _APP.requirements_phase     # S17
_MODULE_RECONSTRUCTOR = _APP.module_reconstructor # S20


# S25: the TASK-0c augmentation cluster (_AUGMENT_STORE/_AUGMENT_ENGINE/
# _CLUSTER_AUGMENTOR) is built before _APP and exposed as _APP.augment_roles.

# C5: the pre-strip bake (Hard Rule 11) lives in strip_bake; this binds its
# pipeline-resident collaborators (cfg scan, cluster pruning, extractor).
_BAKE_ARTIFACTS_DEPS = BakeArtifactsDeps(
    log=_log,
    cfg_scan=task0_cfg_scan,
    prune_clusters=_prune_redundant_clusters,
    fn_name_counts=_fn_name_counts,
    extract_skeleton=_extract_skeleton,
    is_gen_tests_file=_is_gen_tests_file,
    gen_tests_origin_cfgs=_gen_tests_origin_cfgs,
)


# C8: the standalone --phase-*-only / --skeleton-backend handlers live in the
# orchestrator; this binds them to the resident pipeline shims.
_PHASE_SUBCOMMAND_DEPS = _orchestrator.PhaseSubcommandDeps(
    log=_log,
    bake_skeleton_artifacts=_bake_skeleton_artifacts,
    collect_test_files=collect_protected_test_files,
    group_tests=_task2_group_tests,
    fill_groups=_task2_fill_groups,
    cleanup_residuals=_task2_cleanup_residuals,
    new_skeleton_generator=_new_skeleton_task2_generator,
    manifest_name=MANIFEST_NAME,
)


_RUN_DEPS = _orchestrator.RunDeps(
    WORKSPACE_DIR=WORKSPACE_DIR, MANIFEST_NAME=MANIFEST_NAME, MODEL=MODEL,
    COVERAGE_BACKEND=COVERAGE_BACKEND, MODEL_CONTEXT_CHARS=MODEL_CONTEXT_CHARS,
    SINGLE_CALL_CHAR_LIMIT=SINGLE_CALL_CHAR_LIMIT, FILE_CHAR_LIMIT=FILE_CHAR_LIMIT,
    INTERMEDIATE_CHAR_LIMIT=INTERMEDIATE_CHAR_LIMIT,
    COVERAGE_AUGMENTATION_TARGET=COVERAGE_AUGMENTATION_TARGET, TARPAULIN_BIN=TARPAULIN_BIN,
    FAILURE_LOG_NAME=FAILURE_LOG_NAME, PROJECT_PROFILE_NAME=PROJECT_PROFILE_NAME,
    PROJECT_CONTEXT_NAME=PROJECT_CONTEXT_NAME, COVERAGE_STRATEGY_NAME=COVERAGE_STRATEGY_NAME,
    SETTINGS=SETTINGS,
    run_state=_RUN, scope=_SCOPE, augment_store=_AUGMENT_STORE,
    cluster_augmentor=_CLUSTER_AUGMENTOR, strip_bake_deps=_STRIP_BAKE_DEPS,
    ws_scan_deps=_WS_SCAN_DEPS, phase_subcommand_deps=_PHASE_SUBCOMMAND_DEPS,
    requirements_phase=_REQUIREMENTS_PHASE, module_reconstructor=_MODULE_RECONSTRUCTOR,
    log=_log, emit=_emit, setup_workspace=setup_workspace, is_git_repo=_is_git_repo,
    init_log=init_log, init_events=init_events, start_audit_thread=_start_audit_thread,
    quarantine_broken_gen_tests=_quarantine_broken_gen_tests, git_file_hash=_wsmod.git_file_hash,
    ensure_vllm_ready=_vllm_bootstrap.ensure_vllm_ready, git_clean_reset=git_clean_reset,
    clean_state=clean_state, state_path=_state_path,
    restart_from_augmented=restart_from_augmented, source_hash=_source_hash,
    log_state_summary=_log_state_summary, read=read, task0_cfg_scan=task0_cfg_scan,
    load_baked_clusters=_load_baked_clusters,
    mark_phase=_mark_phase, phase_valid=_phase_valid, read_state=_read_state,
    discover_test_command=discover_test_command, parse_cmd=_parse_cmd,
    collect_test_files=collect_test_files,
    collect_protected_test_files=collect_protected_test_files,
    task0_project_profile=task0_project_profile, task0_project_context=task0_project_context,
    task0b_discovery=task0b_discovery, commit_and_mark=_commit_and_mark,
    dependency_graph_factory=_dependency_graph_factory, load_locked_modules=load_locked_modules,
    run_tarpaulin=_run_tarpaulin, strip_and_bake_module=_strip_and_bake_module,
    task1_static_audit=task1_static_audit, scan_workspace_structure=scan_workspace_structure,
    infer_build_check_command=_infer_build_check_command,
    wl_save_gen_tests_siblings=_wl_save_gen_tests_siblings,
    gen_tests_siblings=_gen_tests_siblings, is_gen_tests_file=_is_gen_tests_file,
    wl_wipe_module_sources=_wl_wipe_module_sources, write_workspace_stub=_write_workspace_stub,
    collect_examples=collect_examples, task3=task3,
    scan_unimplemented_functions=lambda sd: _scan_unimplemented_functions(sd),
)

def main():
    parser = _orchestrator.build_arg_parser()
    args = parser.parse_args()

    if args.best_models_list:
        cmd_best_models(vram_gb=args.vram_gb)
        sys.exit(0)

    root = Path(__file__).parent
    _orchestrator.run_pipeline(_RUN_DEPS, args, root)


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
