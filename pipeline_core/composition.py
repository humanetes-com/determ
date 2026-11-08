"""build_app — the composition factory.

The composition root, parameterized over its external ports. `main()` builds the
real :class:`Adapters` and calls :func:`build_app`; tests call it with fake
adapters so they inject at the port boundary (LLM / cargo / syn-extractor)
through the constructor instead of monkeypatching `pipeline._x` globals.

`App` is grown stage by stage: it currently holds the injected ports + run-scoped
state + the boundary providers derived from them. Later stages move the
`_X_DEPS` use-case wiring and the `main` run-loop here."""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os as _os
import subprocess as _subprocess_mod
import sys
import time as _time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.adapters import Adapters
from pipeline_core.adapters.api_surface import ApiSurfaceCapture
from pipeline_core.adapters.artifacts import WorkspaceArtifacts, load_pub_items_manifest
from pipeline_core.adapters.artifacts import (
    api_surface_path as _api_surface_path,
)
from pipeline_core.adapters.artifacts import (
    load_api_surface as _load_api_surface,
)
from pipeline_core.adapters.artifacts import (
    load_baked_leaf_order as _load_baked_leaf_order,
)
from pipeline_core.adapters.artifacts import load_manifest as _load_manifest
from pipeline_core.adapters.artifacts import (
    load_original_structure as _load_original_structure,
)
from pipeline_core.adapters.artifacts import (
    load_skeleton_files as _load_skeleton_files,
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
from pipeline_core.adapters.asan_sweep import run_asan_sweep as _asan_run_sweep
from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.adapters.ask_ledger_store import (
    load_ask_ledger as _load_ask_ledger,
)
from pipeline_core.adapters.ask_ledger_store import (
    save_ask_ledger as _save_ask_ledger,
)
from pipeline_core.adapters.augment_engine import (
    CargoAugmentEngine,
    StaticRustTestAugmentor,
)
from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.adapters.auto_operator import (
    check_auto_operator as _auto_op_check,
)
from pipeline_core.adapters.auto_operator import (
    seed_auto_operator_default as _auto_op_seed_default,
)
from pipeline_core.adapters.behaviour_exercises import exercises_by_coverage
from pipeline_core.adapters.behaviour_misses import (
    load_behaviour_misses,
    save_behaviour_misses,
)
from pipeline_core.adapters.behaviour_probe import probe_env, run_probe
from pipeline_core.adapters.behaviour_probe import (
    subprocess_runner as _probe_subprocess_runner,
)
from pipeline_core.adapters.behaviour_store import (
    fill_suite_for_prompt,
    load_behaviour_suite,
    load_fill_specs,
    save_behaviour_suite,
    write_feature_files,
)
from pipeline_core.adapters.body_ledger_store import (
    append_entries as _body_ledger_append,
)
from pipeline_core.adapters.body_ledger_store import (
    default_root as _body_ledger_root,
)
from pipeline_core.adapters.body_ledger_store import (
    load_entries as _body_ledger_load,
)
from pipeline_core.adapters.body_ledger_store import (
    store_size as _body_ledger_size,
)
from pipeline_core.adapters.build_script import CargoBuildScriptReader
from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.adapters.cluster_pruning import (
    prune_redundant_clusters as _prune_redundant_clusters,
)
from pipeline_core.adapters.commit_gate import CallableCommitGate
from pipeline_core.adapters.commit_gate import (
    commit_and_mark as _commit_gate_commit_and_mark,
)
from pipeline_core.adapters.coverage import (
    CoverageDeps,
    CoverageProvider,
    make_coverage_provider,
)
from pipeline_core.adapters.coverage import (
    run_tarpaulin as _cov_run_tarpaulin,
)
from pipeline_core.adapters.coverage_owner import (
    ProbeWorld,
)
from pipeline_core.adapters.coverage_owner import (
    measure_coverage_slice as _measure_coverage_slice,
)
from pipeline_core.adapters.dead_code import measure_world_dead_code
from pipeline_core.adapters.discovery import (
    collect_examples as _disc_collect_examples,
)
from pipeline_core.adapters.discovery import (
    collect_protected_test_files as _collect_protected_test_files,
)
from pipeline_core.adapters.discovery import (
    collect_test_files as _disc_collect_test_files,
)
from pipeline_core.adapters.discovery import (
    is_gen_tests_file as _disc_is_gen_tests_file,
)
from pipeline_core.adapters.discovery import (
    is_test_file as _disc_is_test_file,
)
from pipeline_core.adapters.discovery import (
    match_gen_tests_sibling as _disc_match_gen_tests_sibling,
)
from pipeline_core.adapters.downstream_api import scan_downstream_api as _scan_downstream_api
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
from pipeline_core.adapters.flow_audit import FlowAuditor
from pipeline_core.adapters.fn_reachability import measure_world_reachability
from pipeline_core.adapters.fn_reseed import (
    current_bodies,
    current_signatures,
    splice_bodies,
)
from pipeline_core.adapters.gdb_backtrace import (
    localize_crashed_binary as _gdb_localize_crashed_binary,
)
from pipeline_core.adapters.gen_tests_storage import (
    derive_gentest_prelude as _gts_derive_gentest_prelude,
)
from pipeline_core.adapters.gen_tests_storage import (
    gen_tests_path as _gts_gen_tests_path,
)
from pipeline_core.adapters.gen_tests_storage import (
    gen_tests_siblings as _gts_gen_tests_siblings,
)
from pipeline_core.adapters.gen_tests_storage import (
    next_gen_tests_n as _gts_next_gen_tests_n,
)
from pipeline_core.adapters.gen_tests_storage import (
    quarantine_broken_gen_tests as _gts_quarantine_broken_gen_tests,
)
from pipeline_core.adapters.gen_tests_storage import (
    read_all_gen_tests as _gts_read_all_gen_tests,
)
from pipeline_core.adapters.gen_tests_storage import (
    revert_gen_tests_pass as _gts_revert_gen_tests_pass,
)
from pipeline_core.adapters.gen_tests_storage import (
    strip_all_gen_tests_artifacts as _gts_strip_all_artifacts,
)
from pipeline_core.adapters.held_out_store import (
    load_held_out,
    record_agreement_rows,
    save_held_out,
    scratch_tree,
)
from pipeline_core.adapters.layout_outcome_store import (
    load_layout_records,
    save_layout_records,
)
from pipeline_core.adapters.layout_registry_store import (
    load_registry as load_layout_registry,
)
from pipeline_core.adapters.layout_registry_store import (
    save_registry as save_layout_registry,
)
from pipeline_core.adapters.layout_tree_splice import apply_layout_to_tree
from pipeline_core.adapters.model_conformance import (
    report_at_run_start as _report_model_at_run_start,
)
from pipeline_core.adapters.model_notes_store import (
    FileModelNotesStore,
)
from pipeline_core.adapters.model_notes_store import (
    write_fr_preserving_notes as _write_fr_preserving_notes,
)
from pipeline_core.adapters.mutation import MutationTester
from pipeline_core.adapters.oracle_fingerprint import (
    cluster_oracle_fp as _cluster_oracle_fp,
)
from pipeline_core.adapters.oracle_fingerprint import (
    oracle_tag as _oracle_tag,
)
from pipeline_core.adapters.oracle_fingerprint import (
    workspace_oracle_drift as _workspace_oracle_drift,
)
from pipeline_core.adapters.per_fn_ask import conversation_factory
from pipeline_core.adapters.phase_c_log import (
    write_phase_c_test_fix_log as _write_phase_c_test_fix_log,
)
from pipeline_core.adapters.pin_gaps_store import load_pin_gaps as _load_pin_gaps
from pipeline_core.adapters.pin_gaps_store import save_pin_gaps as _save_pin_gaps
from pipeline_core.adapters.pin_inputs import (
    example_header as _pin_example_header,
)
from pipeline_core.adapters.pin_inputs import route_lines as _pin_route_lines
from pipeline_core.adapters.pin_inputs import signature_text as _pin_signature_text
from pipeline_core.adapters.pin_inputs import worlds_compiling as _pin_worlds_compiling
from pipeline_core.adapters.pin_runs import PIN_STAGE_DIR
from pipeline_core.adapters.pin_runs import run_on_original as _pin_run_on_original
from pipeline_core.adapters.pin_runs import run_on_rebuilt as _pin_run_on_rebuilt
from pipeline_core.adapters.pin_runs import run_original_emptied as _pin_run_original_emptied
from pipeline_core.adapters.pipeline_db import (
    compile_patterns_read as _compile_patterns_read,
)
from pipeline_core.adapters.pipeline_db import compile_patterns_write as _db_compile_patterns_write
from pipeline_core.adapters.pipeline_db import (
    genesis_rules_read as _genesis_rules_read,
)
from pipeline_core.adapters.pipeline_db import (
    genesis_rules_write as _db_genesis_rules_write,
)
from pipeline_core.adapters.pipeline_db import (
    type_signals_read as _type_signals_read,
)
from pipeline_core.adapters.pipeline_db import type_signals_write as _db_type_signals_write
from pipeline_core.adapters.private_surface_store import load_private_surface, save_private_surface
from pipeline_core.adapters.progress import ChunkProgress
from pipeline_core.adapters.reach_store import load_reach, save_reach
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
from pipeline_core.adapters.recorded_entry import (
    recorded_entry_sets_for as _recorded_entry_sets_for,
)
from pipeline_core.adapters.red_ledger_store import (
    load_red_ledger as _load_red_ledger,
)
from pipeline_core.adapters.red_ledger_store import (
    save_red_ledger as _save_red_ledger,
)
from pipeline_core.adapters.repo_spec_docs import (
    make_repo_spec_section as _make_repo_spec_section,
)
from pipeline_core.adapters.retros import (
    escalation_release_keys as _escalation_release_keys,
)
from pipeline_core.adapters.retros import (
    futile_suspension_keys as _futile_suspension_keys,
)
from pipeline_core.adapters.retros import (
    is_fn_fully_exhausted as _retros_is_exhausted,
)
from pipeline_core.adapters.retros import (
    last_compile_errors as _last_compile_errors,
)
from pipeline_core.adapters.retros import (
    load_retrospectives as _retros_load,
)
from pipeline_core.adapters.retros import (
    migrate_absolute_gate_retros as _migrate_absolute_gate_retros,
)
from pipeline_core.adapters.retros import (
    migrate_confounded_retros as _migrate_confounded_retros,
)
from pipeline_core.adapters.retros import (
    release_escalated_convictions as _release_escalated_convictions,
)
from pipeline_core.adapters.retros import (
    reset_exhaustion_flags as _reset_exhaustion_flags,
)
from pipeline_core.adapters.retros import (
    retire_unblocked_convictions as _retire_unblocked_convictions,
)
from pipeline_core.adapters.retros import (
    retro_evidence_texts as _retro_evidence_texts,
)
from pipeline_core.adapters.retros import (
    retro_key as _retros_key,
)
from pipeline_core.adapters.retros import (
    save_retrospectives as _retros_save,
)
from pipeline_core.adapters.retros import (
    suspend_futile_guidance as _suspend_futile_guidance,
)
from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
from pipeline_core.adapters.run_stats_recorder import (
    start_run_stats as _start_run_stats_recorder,
)
from pipeline_core.adapters.run_stats_store import run_stats_path
from pipeline_core.adapters.rust_parse import ParseCheck
from pipeline_core.adapters.skeleton_compile import compile_filled_check
from pipeline_core.adapters.state import (
    log_state_summary as _state_log_state_summary,
)
from pipeline_core.adapters.state import mark_phase, phase_valid, reopen_phase, set_current
from pipeline_core.adapters.state import mark_phase as _state_mark_phase
from pipeline_core.adapters.state import read_state as _state_read_state
from pipeline_core.adapters.state import state_path as _state_path
from pipeline_core.adapters.state import write_state as _state_write_state
from pipeline_core.adapters.strategy_facts import StrategyFacts
from pipeline_core.adapters.strip import strip_module_source as _strip_module_source
from pipeline_core.adapters.structure import CliStructureReader  # noqa: F401 (re-export for composition callers)
from pipeline_core.adapters.structure import (
    collect_crate_macros as _structure_collect_crate_macros,
)
from pipeline_core.adapters.structure import (
    crate_entry_point as _crate_entry_point,
)
from pipeline_core.adapters.structure import (
    detect_crate_no_std_invariant as _detect_crate_no_std_invariant,
)
from pipeline_core.adapters.structure import fn_name_counts as _fn_name_counts
from pipeline_core.adapters.structure import (
    load_baked_clusters as _load_baked_clusters,
)
from pipeline_core.adapters.structure import (
    source_hash as _structure_source_hash,
)
from pipeline_core.adapters.structure_cache import StructureCache
from pipeline_core.adapters.stub_removal import measure_world_removal
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
from pipeline_core.adapters.telemetry import (
    emit_per_file_test_overview as _emit_per_file_test_overview,
)
from pipeline_core.adapters.test_lessons_store import (
    load_lessons as _load_lessons,
)
from pipeline_core.adapters.test_lessons_store import (
    save_lessons as _save_lessons,
)
from pipeline_core.adapters.test_runner import CallableTestRunner
from pipeline_core.adapters.test_worlds import failing_alone, test_passes_in_world
from pipeline_core.adapters.vllm_bootstrap import (
    ensure_vllm_ready as _ensure_vllm_ready,
)
from pipeline_core.adapters.workspace import WorkspaceDeps, git_commit_paths
from pipeline_core.adapters.workspace import git_checkpoint as _ws_git_checkpoint
from pipeline_core.adapters.workspace import (
    git_clean_reset as _ws_git_clean_reset,
)
from pipeline_core.adapters.workspace import git_commit as _ws_git_commit
from pipeline_core.adapters.workspace import git_file_hash as _ws_git_file_hash
from pipeline_core.adapters.workspace import git_tag as _ws_git_tag
from pipeline_core.adapters.workspace import is_git_repo as _ws_is_git_repo
from pipeline_core.adapters.workspace import (
    setup_workspace as _ws_setup_workspace,
)
from pipeline_core.adapters.workspace import (
    write_workspace_stub as _ws_write_workspace_stub,
)
from pipeline_core.adapters.workspace_files import (
    delete_source_files as _wsf_delete_source_files,
)
from pipeline_core.adapters.workspace_files import (
    load_locked_modules as _wsf_load_locked_modules,
)
from pipeline_core.adapters.workspace_files import lock_module
from pipeline_core.adapters.workspace_files import save_manifest as _save_manifest
from pipeline_core.adapters.workspace_files import (
    write_build_configs as _wsf_write_build_configs,
)
from pipeline_core.adapters.workspace_files import (
    write_source_files as _write_source_files,
)
from pipeline_core.adapters.workspace_lifecycle import (
    clean_state as _clean_state_impl,
)
from pipeline_core.adapters.workspace_restore import RestoreFromAugmented, RestoreFromPreAugment
from pipeline_core.adapters.workspace_restore import list_git_tags as _ws_list_git_tags
from pipeline_core.adapters.workspace_scan import (
    WorkspaceScanDeps,
    _dependency_graph_factory,
    _workspace_scanner_factory,
)
from pipeline_core.application import fix_loop as _fix_loop_mod
from pipeline_core.application import test_count as _tc_app
from pipeline_core.application.behaviour_agreement import AgreementDeps, measure_agreement
from pipeline_core.application.behaviour_book_build import BookBudget
from pipeline_core.application.cluster_augment import ClusterAugmentor
from pipeline_core.application.doc_example_tests import (
    DocExampleDeps,
    emit_doc_example_tests,
)
from pipeline_core.application.emit_behaviour_tests import emit_behaviour_tests
from pipeline_core.application.extract_behaviours import harvest_behaviour_suite
from pipeline_core.application.failure_reading import read_alone
from pipeline_core.application.fix_loop import GenerateFixLoopDeps
from pipeline_core.application.gen_tests_reattach import ReattachDeps
from pipeline_core.application.generators.task1_synthesizer import (
    LLMTask1Synthesizer,
    StaticTask1Synthesizer,
    Task1Synthesizer,
)
from pipeline_core.application.generators.task2_generator import (
    LLMTask2Generator,
    StaticFileListTask2Generator,
    StaticSkeletonTask2Generator,
    StaticTask2Generator,
    Task2Generator,
)
from pipeline_core.application.orchestrator import LockDeps, RunDeps
from pipeline_core.application.orchestrator import (
    PhaseSubcommandDeps as _PhaseSubcommandDeps,
)
from pipeline_core.application.parallel import (
    generate_in_parallel as _gen_in_parallel_impl,
)
from pipeline_core.application.pinning_tests import PinningDeps, run_pinning_pass
from pipeline_core.application.pinning_tests import admit_oracle_file as _admit_oracle_file
from pipeline_core.application.preserve_inline_tests import (
    PreserveInlineTestsDeps,
    preserve_inline_tests,
)
from pipeline_core.application.reconstruct import attribution as _attribution
from pipeline_core.application.reconstruct import (
    cleanup_residuals as _cleanup_residuals,
)
from pipeline_core.application.reconstruct import (
    cluster_membership as _membership,
)
from pipeline_core.application.reconstruct import (
    leaf_first_group_order as _leaf_first_group_order,
)
from pipeline_core.application.reconstruct import source_view as _source_view
from pipeline_core.application.reconstruct import splice as _splice_mod
from pipeline_core.application.reconstruct import stub_scan as _stub_scan
from pipeline_core.application.reconstruct import test_targets as _test_targets_mod
from pipeline_core.application.reconstruct.assert_probe_runner import (
    run_assert_probes,
)
from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    escalate_frontier_to_producers,
    extract_tdd_frontier,
    not_body_repairable_tests,
)
from pipeline_core.application.reconstruct.body_reseed import TreeWriter
from pipeline_core.application.reconstruct.cargo_errors import (
    categorize_compile_error as _categorize_compile_error,
)
from pipeline_core.application.reconstruct.cargo_errors import (
    err_sig as _cargo_err_sig,
)
from pipeline_core.application.reconstruct.cargo_errors import (
    err_sig_from_diagnostics as _cargo_err_sig_from_diagnostics,
)
from pipeline_core.application.reconstruct.cargo_errors import (
    focus_cargo_errors as _focus_cargo_errors_impl,
)
from pipeline_core.application.reconstruct.cargo_runner import (
    PortBoundCargoRunner,
    WorldChecks,
)
from pipeline_core.application.reconstruct.cargo_runner import (
    u2_test_gate as _cargo_runner_u2_gate,
)
from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    project_dir_of_tree,
)
from pipeline_core.application.reconstruct.crash_localizer import (
    localize_crashes as _app_localize_crashes,
)
from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.failure_diagnosis import (
    classify_phase_c_test_failures as _classify_failures_impl,
)
from pipeline_core.application.reconstruct.failure_diagnosis import (
    implicated_fns_from_cargo_errors as _implicated_fns_from_cargo_impl,
)
from pipeline_core.application.reconstruct.failure_diagnosis import (
    implicated_fns_from_check as _implicated_fns_from_check_impl,
)
from pipeline_core.application.reconstruct.failure_diagnosis import (
    record_tdd_regression as _record_tdd_impl,
)
from pipeline_core.application.reconstruct.fill_merge import (
    merge_fn_blocks_into_disk as _merge_fn_blocks_into_disk,
)
from pipeline_core.application.reconstruct.fill_rules import (
    extract_fn_blocks as _extract_fn_blocks,
)
from pipeline_core.application.reconstruct.fill_rules import (
    module_of as _module_of,
)
from pipeline_core.application.reconstruct.fix_context import FixContextAssembler
from pipeline_core.application.reconstruct.generate_fix import (
    run_generate_fix as _run_generate_fix,
)
from pipeline_core.application.reconstruct.group_fill import GroupFillDeps
from pipeline_core.application.reconstruct.group_fill import (
    task2_fill_groups as _task2_fill_groups_impl,
)
from pipeline_core.application.reconstruct.impl_blocks import (
    collect_impl_blocks as _collect_impl_blocks_impl,
)
from pipeline_core.application.reconstruct.impl_blocks import (
    fn_container_index as _fn_container_index_impl,
)
from pipeline_core.application.reconstruct.impl_blocks import (
    fn_occurrence_index as _fn_occurrence_index_impl,
)
from pipeline_core.application.reconstruct.impl_fill import (
    ImplFillDeps,
)
from pipeline_core.application.reconstruct.impl_fill import (
    fill_impl_blocks as _fill_impl_blocks,
)
from pipeline_core.application.reconstruct.impl_fill import (
    prepare_impl_fill as _prepare_impl_fill,
)
from pipeline_core.application.reconstruct.macro_invariants import (
    compute_cluster_macro_invariants as _compute_cluster_macro_invariants,
)
from pipeline_core.application.reconstruct.module_reconstructor import ModuleReconstructor
from pipeline_core.application.reconstruct.phase_c_tdd import PhaseCTddDeps
from pipeline_core.application.reconstruct.phase_c_tdd import (
    run_phase_c_tdd as _run_phase_c_tdd_impl,
)
from pipeline_core.application.reconstruct.publish import published_drift
from pipeline_core.application.reconstruct.reconstruction_step import ReconstructionStep
from pipeline_core.application.reconstruct.requirements_phase import RequirementsPhase
from pipeline_core.application.reconstruct.retrospective import (
    PhaseCRetroDeps,
)
from pipeline_core.application.reconstruct.retrospective import (
    diagnose_phase_c as _diagnose_phase_c,
)
from pipeline_core.application.reconstruct.retrospective import (
    whole_tree_retrospective as _whole_tree_retro_impl,
)
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.application.reconstruct.scheduling import (
    leaf_first_fn_order as _leaf_first_fn_order,
)
from pipeline_core.application.reconstruct.scheduling import (
    order_frontier as _order_frontier,
)
from pipeline_core.application.reconstruct.sibling_context import (
    collect_sibling_filled_bodies as _collect_sibling_filled_bodies,
)
from pipeline_core.application.reconstruct.source_view import (
    condense_rust_to_signatures as _condense_rust_to_signatures,
)
from pipeline_core.application.reconstruct.splice import (
    replace_fn_signature as _splice_replace_fn_signature,
)
from pipeline_core.application.reconstruct.splice import (
    restore_frozen_signature as _splice_restore_frozen_sig,
)
from pipeline_core.application.reconstruct.test_grouping import (
    group_tests as _group_tests_impl,
)
from pipeline_core.application.reconstruct.test_lessons import (
    LessonPassDeps,
    run_test_lessons,
)
from pipeline_core.application.reconstruct.test_locator import (
    find_tests_for_fn as _find_tests_for_fn,
)
from pipeline_core.application.reconstruct.test_locator import (
    find_tests_referencing_fn as _find_tests_referencing_fn,
)
from pipeline_core.application.reference_run import (
    ReferenceDeps,
    measure_reference,
)
from pipeline_core.application.strip_bake import (
    BakeArtifactsDeps,
    StripBakeDeps,
)
from pipeline_core.application.strip_bake import (
    _behaviour_sources as _t0d_behaviour_sources,
)
from pipeline_core.application.strip_bake import (
    _test_layout_sources as _t0d_test_sources,
)
from pipeline_core.application.strip_bake import (
    bake_skeleton_artifacts as _bake_artifacts_impl,
)
from pipeline_core.application.strip_bake import (
    strip_and_bake_module as _strip_and_bake_module,
)
from pipeline_core.application.tasks import cfg_scan as _cfg_scan_mod
from pipeline_core.application.tasks.blueprint_phase import BlueprintDeps
from pipeline_core.application.tasks.discover_test_command import DiscoverTestCommandDeps
from pipeline_core.application.tasks.discover_test_command import (
    discover_test_command as _discover_test_command_impl,
)
from pipeline_core.application.tasks.discover_test_command import (
    infer_build_check_command as _infer_build_check_command,
)
from pipeline_core.application.tasks.fn_criterion import FnCriterionInputs, read_world_facts
from pipeline_core.application.tasks.genesis_audit import GenesisAuditDeps
from pipeline_core.application.tasks.genesis_audit import (
    extract_and_store_rules as _ga_extract_and_store_rules,
)
from pipeline_core.application.tasks.genesis_audit import (
    genesis_audit as _genesis_audit_run,
)
from pipeline_core.application.tasks.task0_docs import Task0DocsDeps
from pipeline_core.application.tasks.task0_docs import (
    task0_project_context as _task0_context_impl,
)
from pipeline_core.application.tasks.task0_docs import (
    task0_project_profile as _task0_profile_impl,
)
from pipeline_core.application.tasks.task0b_discovery import Task0bDeps
from pipeline_core.application.tasks.task0b_discovery import (
    task0b_discovery as _task0b_impl,
)
from pipeline_core.application.tasks.task0c import Task0cDeps
from pipeline_core.application.tasks.task0c import (
    select_relevant_siblings as _t0c_select_relevant_siblings,
)
from pipeline_core.application.tasks.task0c import (
    task0c_augment_module as _task0c_augment_impl,
)
from pipeline_core.application.tasks.task0c import (
    task0c_behaviour_coverage as _task0c_behaviour_coverage_impl,
)
from pipeline_core.application.tasks.task0c import (
    task0c_behaviour_pass as _task0c_behaviour_pass_impl,
)
from pipeline_core.application.tasks.task0d import Task0dDeps, build_book_from_source
from pipeline_core.application.tasks.task1 import Task1Deps
from pipeline_core.application.tasks.task1 import (
    filter_proc_macro_test_files as _filter_proc_macro_test_files,
)
from pipeline_core.application.tasks.task1 import task1 as _task1_run
from pipeline_core.application.tasks.task1 import (
    task1_static_audit as _task1_static_audit,
)
from pipeline_core.application.tasks.task2 import Task2Deps
from pipeline_core.application.tasks.task2 import (
    task2_inner_loop as _task2_inner_impl,
)
from pipeline_core.application.tasks.task3 import (
    Task3Deps,
)
from pipeline_core.application.tasks.task3 import (
    alone_reader as task3_alone_reader,
)
from pipeline_core.application.tasks.task3 import (
    cluster_outputs as task3_cluster_outputs,
)
from pipeline_core.application.tasks.task3 import (
    task3 as _task3_impl,
)
from pipeline_core.application.tasks.task4 import Task4Deps
from pipeline_core.application.tasks.task4 import task4 as _task4_run
from pipeline_core.application.test_worlds import (
    TestWorldsDeps,
    gate_line,
    settle_test_gate,
    world_is_measurable,
)
from pipeline_core.application.unreached_stub_reading import (
    UnreachedStubDeps,
    read_unreached_stub_proofs,
)
from pipeline_core.application.validate import Task2CheckerDeps
from pipeline_core.application.validate import (
    validate_task1_output as _validate_task1_output,
)
from pipeline_core.application.validate import (
    validate_task2_api_surface_conformance as _validate_task2_api_impl,
)
from pipeline_core.application.validate import (
    validate_task2_output as _validate_task2_output_impl,
)
from pipeline_core.application.validate import (
    validate_task4_output as _validate_task4_output,
)
from pipeline_core.application.workspace_lifecycle import (
    cluster_best_tree_has_layout,
    load_cluster_best_tree_mark,
    restore_cluster_best_tree,
    save_cluster_best_tree,
)
from pipeline_core.application.workspace_lifecycle import (
    save_gen_tests_siblings as _wl_save_gen_tests_siblings,
)
from pipeline_core.application.workspace_lifecycle import (
    wipe_module_sources as _wl_wipe_module_sources,
)
from pipeline_core.composition_layout import (
    build_layout_phase_runner,
    build_retrigger_layout,
)
from pipeline_core.domain import graceful_stop as _graceful_stop
from pipeline_core.domain.attribution import WriteGround
from pipeline_core.domain.behaviour_probe import perturbed_env
from pipeline_core.domain.best_tree_mark import BestTreeMark
from pipeline_core.domain.cargo_toml import (
    cargo_is_proc_macro as _cargo_is_proc_macro,
)
from pipeline_core.domain.cluster_env import world_key, world_of_env
from pipeline_core.domain.cluster_plan import (
    merged_view_cluster,
    nightly_rules_for,
    one_tree_cluster,
)
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.coverage import CoverageMeasurement
from pipeline_core.domain.coverage_annotation import (
    annotate_uncovered as _cov_annotate_uncovered,
)
from pipeline_core.domain.coverage_annotation import (
    extract_branch_guard as _cov_extract_branch_guard,
)
from pipeline_core.domain.coverage_annotation import (
    extract_uncovered_functions as _cov_extract_uncovered_functions,
)
from pipeline_core.domain.coverage_annotation import (
    filter_cfg_excluded_lines as _cov_filter_cfg_excluded_lines,
)
from pipeline_core.domain.coverage_annotation import (
    filter_cfg_incompatible_lines as _cov_filter_cfg_incompat,
)
from pipeline_core.domain.coverage_annotation import (
    inject_allow_before_mod as _cov_inject_allow_before_mod,
)
from pipeline_core.domain.coverage_annotation import (
    partition_uncovered_lines as _cov_partition_uncovered_lines,
)
from pipeline_core.domain.end_verdict import EndVerdict as _EndVerdict
from pipeline_core.domain.enums import (
    Backend,
    PhaseCDriver,
    PhaseCMode,
    Task1Backend,
    Task2Backend,
)
from pipeline_core.domain.failure_classification import (
    classify_failure as _classify_failure,
)
from pipeline_core.domain.fn_execution import site_label as _site_label
from pipeline_core.domain.generation import (
    SkeletonConfig,
    Task1Config,
    Task2Config,
)
from pipeline_core.domain.generation import (
    build_gen_tests_decl as _gen_build_gen_tests_decl,
)
from pipeline_core.domain.generation import (
    extract_gen_tests_body as _gen_extract_gen_tests_body,
)
from pipeline_core.domain.generation import (
    extract_test_mod as _gen_extract_test_mod,
)
from pipeline_core.domain.generation import (
    gen_tests_origin_cfgs as _gen_tests_origin_cfgs,
)
from pipeline_core.domain.generation import (
    has_gen_tests_mod_decl as _has_gen_tests_mod_decl,
)
from pipeline_core.domain.generation import (
    inject_gen_prelude_imports as _gen_inject_gen_prelude_imports,
)
from pipeline_core.domain.generation import (
    is_gen_tests_decl_only_src as _gen_is_decl_only_src,
)
from pipeline_core.domain.generation import (
    is_gen_tests_sibling_name as _is_gen_tests_sibling_name,
)
from pipeline_core.domain.generation import (
    prelude_import_names as _gen_prelude_import_names,
)
from pipeline_core.domain.generation import (
    strip_all_gen_tests_decls as _gen_strip_all_decls,
)
from pipeline_core.domain.generation import (
    strip_all_gen_tests_decls as _gts_strip_all_decls,
)
from pipeline_core.domain.generation import (
    strip_gen_tests_decl_n as _gen_strip_decl_n,
)
from pipeline_core.domain.generation import (
    strip_gen_tests_decl_n as _strip_gen_tests_decl_n,
)
from pipeline_core.domain.impl_block_ranges import (
    impl_where_clause as _impl_where_clause,
)
from pipeline_core.domain.impl_block_ranges import (
    render_impl_block as _render_impl_block,
)
from pipeline_core.domain.impl_routing import impl_block_id as _impl_block_id
from pipeline_core.domain.language_driver import GenericLanguageDriver as _GenericLanguageDriver
from pipeline_core.domain.language_driver import bind as _bind_language_driver
from pipeline_core.domain.language_driver import language_driver as _language_driver
from pipeline_core.domain.llm_output import extract_files as _extract_files
from pipeline_core.domain.llm_output import (
    extract_signature_block as _extract_signature_block,
)
from pipeline_core.domain.llm_output import (
    split_api_surface_by_file as _split_api_surface_by_file,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.pin_gaps import gaps_left as _gaps_left
from pipeline_core.domain.pin_gaps import (
    gaps_still_unimplemented as _gaps_still_unimplemented,
)
from pipeline_core.domain.prompt_budget import PromptBudgets
from pipeline_core.domain.refine import (
    parse_retrospective_categories as _parse_retro_categories,
)
from pipeline_core.domain.run_state import RunState
from pipeline_core.domain.rust_fn_headers import is_pub_fn as _domain_is_pub_fn
from pipeline_core.domain.rust_fn_headers import locate_fn as _domain_locate_fn
from pipeline_core.domain.rust_macros import (
    proc_macro_fallback_requirements as _pm_fallback_reqs,
)
from pipeline_core.domain.rust_scan import attributed_test_fn_names
from pipeline_core.domain.rust_scan import count_test_attrs as _scan_count_test_attrs
from pipeline_core.domain.rust_scan import (
    has_nonempty_test_fn as _scan_has_nonempty_test_fn,
)
from pipeline_core.domain.rust_scan import (
    is_structural_brace_line as _scan_is_structural_brace_line,
)
from pipeline_core.domain.rust_scan import (
    rust_delimiters_balanced as _rust_delimiters_balanced,
)
from pipeline_core.domain.same_failure import (
    FailureSignature,
    FailureToRead,
    SignatureReader,
)
from pipeline_core.domain.test_cmd import build_test_cmd as _build_test_cmd
from pipeline_core.domain.test_cmd import parse_cmd as _parse_cmd
from pipeline_core.domain.test_command import (
    derive_check_only_cmd as _domain_derive_check_only_cmd,
)
from pipeline_core.domain.test_grouping import (
    extract_test_assertions as _extract_test_assertions,
)
from pipeline_core.domain.test_results import (
    extract_test_output_from_file as _extract_test_output_from_file,
)
from pipeline_core.domain.test_results import insert_no_run as _insert_no_run
from pipeline_core.domain.test_results import (
    is_compiler_output_line as _is_compiler_output_line,
)
from pipeline_core.domain.test_results import (
    parse_failed_tests as _parse_failed_tests,
)
from pipeline_core.domain.test_results import (
    rustc_error_codes as _rustc_error_codes,
)
from pipeline_core.domain.test_results import (
    rustc_error_signatures as _rustc_error_signatures,
)
from pipeline_core.domain.test_results import (
    split_reachable_failures as _split_reachable_failures,
)
from pipeline_core.domain.test_results import strip_ansi as _strip_ansi
from pipeline_core.domain.text import block_is_prose as _text_block_is_prose
from pipeline_core.domain.text import flags_unreachable as _text_flags_unreachable
from pipeline_core.domain.text import parse_attempt_number as _text_parse_attempt_number
from pipeline_core.domain.text import (
    strip_trailing_file_tag as _text_strip_trailing_file_tag,
)
from pipeline_core.domain.workspace_layout import (
    FAILURE_LOG_NAME as _FAILURE_LOG_NAME,
)
from pipeline_core.domain.workspace_layout import MANIFEST_NAME as _MANIFEST_NAME
from pipeline_core.domain.workspace_layout import (
    PROJECT_PROFILE_NAME as _PROJECT_PROFILE_NAME,
)
from pipeline_core.domain.workspace_layout import SKELETON_DIR, SKELETON_FILLED_DIR
from pipeline_core.domain.workspace_layout import WORKSPACE_DIR as _WORKSPACE_DIR
from pipeline_core.domain.world_floor import excused_everywhere, judge_cluster_capture
from pipeline_core.io import read, write
from pipeline_core.io import read as _io_read
from pipeline_core.io import write as _io_write
from pipeline_core.ports.cargo_runner import CargoRunner as CargoRunnerPort
from pipeline_core.ports.fill_engine import (
    FillPassPort,
    RegressionBaselinePort,
    RegressionGatePort,
)
from pipeline_core.ports.measurement import AttributeFailuresPort
from pipeline_core.ports.structure import StructureReader
from pipeline_core.prompts import shaping as _shaping
from pipeline_core.prompts.crate_idioms import (
    extract_crate_idioms as _extract_crate_idioms,
)
from pipeline_core.prompts.output_format import FILE_FORMAT_INSTRUCTION
from pipeline_core.prompts.per_impl import (
    build_per_impl_prompt as _build_per_impl_prompt,
)
from pipeline_core.prompts.sections import (
    build_dispatch_family_section as _build_dispatch_family_impl,
)
from pipeline_core.prompts.sections import (
    format_api_surface_section as _fmt_api_surface_impl,
)
from pipeline_core.prompts.sections import tag_block as _tag_block
from pipeline_core.prompts.skeleton_index import (
    format_skeleton_index_for_cluster as _format_skeleton_index_for_cluster,
)
from pipeline_core.prompts.whole_file_fix import build_whole_file_fix_prompt as _build_wff
from pipeline_core.settings import Settings

# The catalog driver was deleted 2026-10-17 (original-derived guidance one
# env flip from a prompt). A launch that still asks for it is refused, not
# silently given the generic driver; the tag `catalog-driver-last` holds it.
if (_os.getenv("LANGUAGE_DRIVER") or "generic").strip().lower() != "generic":
    raise RuntimeError(
        f"LANGUAGE_DRIVER={_os.getenv('LANGUAGE_DRIVER')!r}: only `generic` "
        "exists; the catalog driver was removed (restore it from the git tag "
        "catalog-driver-last into a branch for an A/B)")
_bind_language_driver(_GenericLanguageDriver())

# Model self-correction notes — prompt-section caps (see CLAUDE.md "The model
# authors its own anti-hallucination notes").
MODEL_NOTES_MAX_CHARS = 4000  # cap on the notes section in any one prompt
MODEL_NOTES_MAX_ENTRIES = 80  # hard cap; older entries dropped LRU on overflow


@dataclass(frozen=True)
class Ports:
    """Late-binding callables for the cross-cutting boundaries (LLM ask, log,
    event emit, truncation guard, cargo env). During migration the composition
    root supplies lambdas over its module globals so the e2e test seam (which
    patches those globals) is preserved; at the end they become bound adapter
    methods."""

    ask: Callable[..., str]
    log: Callable[[str], None]
    emit: Callable[..., None]
    guard: Callable[[str, str], None]
    cargo_env: Callable[[], dict[str, str]]


@dataclass(frozen=True)
class CompositionConfig:
    """Pipeline-level constants the wired deps need that don't (yet) live in
    `Settings` — tool paths, timeouts, char caps, prompt fragments. Bundled so
    `build_app`'s signature doesn't grow a kwarg per migrated block. Fold these
    into `Settings` in a later slice."""

    task4_verbosity_guard: str
    extra_cap: int
    tarpaulin_bin: Path
    grcov_bin: Path
    build_timeout: int
    ignore_pragma: str
    skip_dir_names: frozenset[str]
    intermediate_char_limit: int
    single_call_char_limit: int
    readme_cap: int
    profile_cap: int
    project_context_name: str
    failed_body_cap: int
    fix_err_cap: int
    coverage_strategy_name: str
    no_improvement_threshold: int
    genesis_audit_interval: int
    module_plateau_abort: int
    pin_test_max_attempts: int
    test_timeout: int
    build_check_retries: int
    task2_build_max_stalls: int
    synthesis_group_size: int
    chunk_cache_dir: str
    task1_thinking_directive: str
    task2_thinking_directive: str
    skeleton_extractor_bin: Path
    # Layout authoring (t1.5) + stall-exit re-trigger — env parsed once
    # at the composition root (parse-don't-validate).
    layout_authoring_enabled: bool
    layout_retrigger_max_attempts: int
    # Freeze scope: may t1.5 design the PRIVATE FIELDS of public structs? Baked
    # into the workspace's private-surface artefact + the elided API surface, so
    # a change only takes effect on a re-baked run.
    layout_author_private_fields: bool
    # Repo-root setup.sh — the augment engine's default toolchain bootstrap.
    setup_script: Path


@dataclass(frozen=True)
class Helpers:
    """Late-binding callables for resident pipeline helpers (still defined in
    pipeline.py) that the migrated `_X_DEPS` blocks need. Lambdas over the module
    globals → preserve the test seam and let build_app wire them before they're
    defined. Dissolve a field once its helper relocates into pipeline_core."""

    ask_retrospective: Callable[..., str]
    # The per-fn conversation protocol (prompt → body/pull-request) — the
    # PromptContext role's model seam, routed through App (llm boundary).
    ask_per_fn_body: Callable[..., tuple[str | None, str]]
    # Group-fill leaf collaborators (GroupFillDeps construction):
    # Phase-C TDD driver collaborators (PhaseCTddDeps construction;
    # attribute_failures/build_test_reverse_map/filter_test_targets pre-exist
    # below in the group):
    # Same seams read through the LIVE pipeline._APP (swap-aware: tests
    # replace _APP; composition must not import pipeline, so the module
    # provides these App-reading closures itself):
    # Phase-subcommand collaborators:
    # Task-0c collaborators (Task0cDeps construction):
    describe_test_environment: Callable[..., Any]
    coverage_target: float
    task0c_prompt_char_cap: int
    task0c_fix_thinking_directive: str
    task0c_thinking_directive: str
    # Fix-loop collaborators (GenerateFixLoopDeps construction):
    fix_diag: Any
    settings_now: Callable[..., Any]
    store_prompt_content: Callable[..., Any]
    ask_raw: Callable[..., Any]
    pipeline_parallelism: Callable[[], int]
    task3_legacy_single: Callable[..., Any]
    source_extensions: Any
    fix_thinking_directive: str
    task2_verbosity_guard: str
    # Attribution collaborators (AttributionDeps construction):
    structure_via_app: Any  # StructureReader reading the LIVE pipeline._APP.structure
    # Task-1 collaborators (Task1Deps construction):
    # Impl-fill collaborators (ImplFillDeps construction):
    test_regression_baseline: RegressionBaselinePort
    test_regression_gate: RegressionGatePort
    per_impl_max_stubs: int
    converge_in_cluster: Callable[..., Any]
    count_relevant_notes: Callable[..., Any]
    effective_stalls: Callable[..., Any]
    run_cargo: Callable[..., Any]
    task2_fill_fns_one_by_one: FillPassPort
    # Reconstruction-cluster collaborators:
    load_model_notes: Callable[..., list[str]]
    genesis_rules_count: Callable[..., int]
    derive_gentest_prelude: Callable[..., tuple[str, str]]
    is_gen_tests_file: Callable[[str], bool]
    gen_tests_siblings: Callable[[Path], list[tuple[int, Path]]]
    load_gen_tests_origin: Callable[[Path], dict[str, str]]
    gen_tests_path: Callable[[Path, int], Path]
    build_gen_tests_decl: Callable[..., str]
    # task3 inline-retry collaborators — resident shims over pipeline-side
    # bound deps (_ATTR_DEPS, ADAPTERS.cargo); run_cargo_check_diag is an e2e seam.
    run_cargo_check_diag: Callable[..., tuple[int, str, list[Any]]]
    # Run-loop residents (RunDeps construction): logging/telemetry init — they
    # mutate pipeline-side run globals (_RUN, _RUN_CONTEXT), so they stay
    # resident and late-bind here. (The flow-audit thread moved to
    # adapters.flow_audit.FlowAuditor, constructed in build_app.)
    init_log: Callable[..., None]
    init_events: Callable[..., None]


@dataclass(frozen=True)
class FillRoles:
    """The 9 ISP fill-engine role adapters (`adapters.fill_engine_roles`).
    Constructed in `build_app`: seam-sensitive collaborators bind through
    `Helpers` (late-binding lambdas over the pipeline globals the scripted-cargo/
    scripted-model tests patch) or `_via` (live-App swap); pure relocated fns
    bind direct. The per-call engine factory (`_build_phase_c_engine`) + the
    eventual `App.run` source them from `App.fill_roles`."""

    tel: EngineTelemetryAdapter
    mut: MutatorAdapter
    cargo: CargoFillAdapter
    prompt: PromptContextAdapter
    ws: WorkspaceModelAdapter
    retros: RetrosAdapter
    tests: TestLinkerAdapter
    stall: StallPolicyAdapter
    impl: ImplFillPathAdapter
    # Where the fill schedule's route runs are recorded and ranked from.
    route_ledger: RouteLedgerStore


@dataclass(frozen=True)
class AugmentRoles:
    """TASK-0c augmentation cluster (store + cargo engine + the ClusterAugmentor
    use-case). Constructed in `build_app`; the resident collaborators
    (`collect_test_files`, `task0c_augment_module`, the git-bound commit-gate)
    bind through `Helpers` late-binding lambdas."""

    store: FileAugmentStore
    engine: CargoAugmentEngine
    augmentor: ClusterAugmentor


@dataclass(frozen=True)
class App:
    """The wired application: external ports + run-scoped state + boundary
    providers and use-case deps built from them. Grown incrementally, as
    `_X_DEPS` blocks migrate out of pipeline.py."""

    adapters: Adapters
    settings: Settings
    run_state: RunState
    scope: ClusterScope
    ports: Ports
    config: CompositionConfig
    # Cluster-scoped structure queries: the WorkspaceModel role's
    # derived scans — stub records / impl blocks filtered to a cluster's
    # cfg-set. Explicit App seams so tests script them at the boundary
    # instead of monkeypatching pipeline module fns.
    scan_stub_records_for_cluster: Callable[..., Any]
    collect_impl_blocks_for_cluster: Callable[..., Any]

    # Group-fill deps: constructed here from helpers + ask_client.
    group_fill_deps: GroupFillDeps
    # Phase-C TDD driver deps.
    phase_c_tdd_deps: PhaseCTddDeps
    # Impl-fill deps.
    impl_fill_deps: ImplFillDeps
    # Task-1 deps.
    task1_deps: Task1Deps
    # Attribution deps.
    attribution_deps: AttributionDeps
    # Fix-loop deps.
    gen_fix_loop_deps: GenerateFixLoopDeps
    # The shared whole-file fix loop over those deps (task 2's build check
    # and the post-task-3 path); test seam pipeline.generate_fix_loop.
    generate_fix_loop: Callable[..., tuple[bool, str]]
    # Task-0c deps.
    task0c_deps: Task0cDeps
    # Phase-subcommand deps.
    phase_subcommand_deps: Any

    # Guarded LLM ask client (bridge): constructed by the composition
    # root (its ask_raw/hot-reload lambdas close over pipeline residents) and
    # HELD here so use-case deps bind `app.ask_client.ask` and tests swap it.
    ask_client: AskClient

    # Telemetry boundary: the structured-event sink, constructed
    # HERE from the late-bound events-log path + live run-context dict the
    # composition root owns. `pipeline._emit`/`_phase` delegate to it.
    emitter: JsonlEventEmitter

    # LLM per-fn boundary: the engines' PromptContext role resolves its
    # model seam here at call time — swap via dataclasses.replace to script it.
    ask_per_fn_body: Callable[..., tuple[str | None, str]]
    # Frontier/attribution/impl-scan seams: the ONE seam every
    # consumer reads — bound DIRECT to the pipeline_core impls in build_app
    # (attribution fns over attribution_deps; impl-scan over the enumerate
    # seam); swap via dataclasses.replace like the other boundary fields.
    # Consumers inside build_app reach them through `app_ref` (see build_app)
    # so a test's replace on the live App is seen at call time.
    attribute_failures: AttributeFailuresPort
    behavior_frontier: Callable[..., Any]
    classify_phase_c_test_failures: Callable[..., Any]
    collect_impl_blocks: Callable[..., Any]
    compile_error_frontier: Callable[..., Any]
    extract_tdd_frontier: Callable[..., Any]
    fn_container_index: Callable[..., Any]
    frontier_from_diags: Callable[..., Any]
    # The peel's per-body implication: same span resolution as
    # `frontier_from_diags`, keeping the container the FnLocator projection
    # erases (two impls legally share a method name — run `57025-6ba652f2`).
    implicated_keys_from_diags: Callable[..., Any]
    u2_test_gate: Callable[..., Any]
    # Per-fn exhaustion diagnosis: the retrospective model call, bound
    # over the same PhaseCRetroDeps held at App.phase_c_retro_deps.
    phase_c_retrospective: Callable[..., Any]
    # TASK-0b discovery use-case: deps constructed here, bound direct.
    task0b_discovery: Callable[..., str]
    # TASK-3 verification driver: wraps the relocated task3 with the
    # task.start/task.done events + per-file overview the old shim emitted.
    task3: Callable[..., tuple[Any, str]]

    # Cargo boundary: the application-facing runner over `adapters.cargo`
    # — the semantic (rc, text) / (rc, text, diags) level engines and scripted
    # tests work at. Swap via dataclasses.replace to script it.
    cargo_runner: CargoRunnerPort

    # Syn-extractor boundary: the structured-source reader over `adapters.extractor`.
    # Typed at the PORT so tests swap in a fake via dataclasses.replace.
    structure: StructureReader
    # Fill-engine ISP role adapters — see FillRoles.
    fill_roles: FillRoles
    # Model self-correction notes store: I/O + run-state engine behind the
    # notes prompt sections; test seam pipeline._MODEL_NOTES_STORE re-exports it.
    model_notes_store: FileModelNotesStore
    # TASK-2 residual-cleanup deps.
    cleanup_residuals_deps: _cleanup_residuals.CleanupResidualsDeps
    # Pre-strip API-surface capture; test seam pipeline._API_SURFACE.
    api_surface: ApiSurfaceCapture
    # --restart-from-augmented / --restart-from-pre-augment workspace restores.
    restore_from_augmented: RestoreFromAugmented
    restore_from_pre_augment: RestoreFromPreAugment
    # Strip/bake pre-step deps + pre-strip bake deps.
    strip_bake_deps: StripBakeDeps
    # Per-run structure caches + cluster-membership queries; owns the
    # mod-records invalidation hook (call after strip/task1-reset/task2-regen).
    structure_cache: StructureCache
    bake_artifacts_deps: BakeArtifactsDeps
    # Per-fn fix-prompt section assembler.
    fix_context: FixContextAssembler
    # TASK-0c augmentation cluster — see AugmentRoles.
    augment_roles: AugmentRoles
    # Migrated use-case deps:
    genesis_audit_deps: GenesisAuditDeps       # TASK-4 genesis audit
    discover_test_cmd_deps: DiscoverTestCommandDeps  # test-command discovery
    coverage_deps: CoverageDeps                # coverage I/O
    coverage_provider: CoverageProvider        # backend-selected coverage provider
    # Migrated use-case deps:
    ws_deps: WorkspaceDeps                      # git workspace I/O
    ws_scan_deps: WorkspaceScanDeps            # workspace structure scan
    task4_deps: Task4Deps                      # TASK-4 genesis rewrite
    task0_docs_deps: Task0DocsDeps             # TASK-0 profile/context docs
    phase_c_retro_deps: PhaseCRetroDeps        # phase-C retrospective
    validator_deps: Task2CheckerDeps           # TASK-2 output validators
    # Reconstruction use-case spine. NB: the role adapters +
    # lock/reattach/journal/artifacts/test_runner/coverage_docs/flow_audit/
    # docs_metrics/reconstruction_step/failure_analysis are consumed only as
    # build_app locals (to assemble the phase use-cases) → not exposed on App.
    telemetry: LogTelemetry                    # injected
    requirements_phase: RequirementsPhase      # read by main()
    module_reconstructor: ModuleReconstructor  # read by main()
    # Top-level run-loop deps — read by pipeline main() → run_pipeline.
    run_deps: RunDeps


def _tdd_measure_coverage(
    log: Callable[[str], None],
    enumerate_fns: Callable[[Path], Any],
    worlds: WorldChecks | None,
) -> Callable[..., Any]:
    """The coverage probe bound into run_phase_c_tdd.

    `enumerate_fns` is the same extractor port the fill routes key their
    records with, so the coverage sites and the session's record map are
    one enumeration of one tree; the adapter no longer locates the
    extractor binary on its own.

    Must accept every keyword the driver passes. Run `2629191-02835a4c`
    (2026-08-30) aborted both clusters on the first dispatch ranking:
    composition had `lambda pd, cl: …` and the driver now passes
    `failing_tests=` so pass C profiles the red set, and `cluster_env=`
    so the owner map inherits the sweep's cfg and toolchain. A two-argument
    lambda TypeErrors; functools.partial forwards keywords.

    The worlds are resolved HERE, from the caller's home env, by the same
    `WorldChecks` every compile check and the failure reader use: the home
    world first, then each other world under `env_for`. Callers (the sweep,
    the session turn) pass the home `cluster_env` as before and cannot take
    a one-world reading of a tree judged in several. Run `1947461-b4eb65c3`
    (2026-10-24) probed the home world only and its last owed red test,
    built only under the nightly cfg, had no owner for 14 iterations.
    `PHASE_C_ALL_WORLDS_CHECK=0` (`worlds` None) probes the home world
    only, as every check does under that setting.
    """
    def measure(project_dir: Path, cluster: str, *,
                cluster_env: Mapping[str, str] | None = None,
                **kw: Any) -> Any:
        home_env = dict(cluster_env) if cluster_env else None
        probe = [ProbeWorld(world_of_env(home_env), home_env)]
        if worlds is not None and home_env:
            tree = Path(project_dir) / ".skeleton-filled" / cluster
            probe += [ProbeWorld(str(c.name), worlds.env_for(c, tree, home_env))
                      for c in worlds.others(home_env)]
        return _measure_coverage_slice(project_dir, cluster, worlds=probe,
                                       log=log, enumerate_fns=enumerate_fns,
                                       **kw)
    return measure


def build_app(
    adapters: Adapters,
    settings: Settings,
    run_state: RunState,
    scope: ClusterScope,
    ports: Ports,
    config: CompositionConfig,
    helpers: Helpers,
    structure: StructureReader,
    telemetry: LogTelemetry,
    *,
    ask_client: AskClient,
    events_log: Callable[[], str] = lambda: "logs/events.jsonl",
    prompts_dir: Path = Path("logs/prompts"),
    run_context: dict[str, str] | None = None,
    app_ref: Callable[[], App] | None = None,
) -> App:
    """Assemble the App from its ports + run-scoped holders + config. Pure wiring
    (no I/O) — the place a test passes fake adapters/ports to get a fake-backed App.

    `app_ref` returns the LIVE App the composition root holds (pipeline passes
    `lambda: _APP`). Use-case deps that must stay swappable bind seam fields
    through it, so a test's `dataclasses.replace` on the held App is seen at
    call time — the generalization of the per-seam `*_via_app` helper fields."""
    cfg = config

    def _via_deps_phase_c_tdd() -> PhaseCTddDeps:
        if app_ref is None:
            raise RuntimeError("phase_c_tdd deps resolved without app_ref")
        return app_ref().phase_c_tdd_deps

    def _via(name: str) -> Callable[..., Any]:
        def _call(*a: Any, **k: Any) -> Any:
            if app_ref is None:
                raise RuntimeError(
                    f"App seam {name!r} resolved without app_ref — pass "
                    "app_ref to build_app for swap-aware wiring")
            return getattr(app_ref(), name)(*a, **k)
        return _call

    # Former Helpers pass-throughs over the live App — resolved via
    # _live() so a test's dataclasses.replace on pipeline._APP wins.
    def enumerate_fns(sd: Path) -> Any:
        return _live().structure.enumerate_fns(sd)

    def enumerate_types(sd: Path) -> Any:
        return _live().structure.enumerate_types(sd)

    def enumerate_mods(sd: Path) -> Any:
        return _live().structure.enumerate_mods(sd)

    def enumerate_doc_items(sd: Path) -> Any:
        return _live().structure.enumerate_doc_items(sd)

    def enumerate_uses(sd: Path) -> Any:
        return _live().structure.enumerate_uses(sd)

    def enumerate_dispatch_families(sd: Path) -> Any:
        return _live().structure.enumerate_dispatch_families(sd)

    def scan_stub_records_via_app(*a: Any, **k: Any) -> Any:
        return _live().scan_stub_records_for_cluster(*a, **k)

    def collect_impl_blocks_via_app(*a: Any, **k: Any) -> Any:
        return _live().collect_impl_blocks_for_cluster(*a, **k)

    def bound_methods_section_for(*a: Any, **k: Any) -> Any:
        return _live().fix_context.bound_methods_section_for(*a, **k)

    def types_section_for_fix(*a: Any, **k: Any) -> Any:
        return _live().fix_context.types_section_for_fix(*a, **k)

    def types_section_for_files(*a: Any, **k: Any) -> Any:
        return _live().fix_context.types_section_for_files(*a, **k)

    def task3_via_app(*a: Any, **k: Any) -> Any:
        return _live().task3(*a, **k)

    def task0c_augment_module(pd: Path, mn: str, md: Path, crate_type: str = "",
                              **k: Any) -> Any:
        return _task0c_augment_impl(pd, mn, md, _live().task0c_deps, crate_type, **k)

    def task0c_behaviour_coverage(pd: Path, mn: str, md: Path, /) -> tuple[int, int]:
        return _task0c_behaviour_coverage_impl(pd, mn, md, _live().task0c_deps)

    def task0c_behaviour_pass(pd: Path, mn: str, md: Path, /, *,
                              project_context: str, coverage_strategy: str) -> bool:
        return _task0c_behaviour_pass_impl(
            pd, mn, md, _live().task0c_deps,
            project_context=project_context, coverage_strategy=coverage_strategy)

    def bake_skeleton_artifacts(pd: Path, mn: str) -> Any:
        return _bake_artifacts_impl(pd, mn, _live().bake_artifacts_deps)

    generate_in_parallel = functools.partial(
        _gen_in_parallel_impl, ask=ask_client.ask, guard=ports.guard)
    build_dispatch_family_section = functools.partial(
        _build_dispatch_family_impl, single_call_char_limit=cfg.single_call_char_limit)

    def record_tdd_regression(*a: Any, **k: Any) -> Any:
        return _record_tdd_impl(
            *a, **k, enumerate_fns=enumerate_fns,
            append_note=model_notes_store.append,
            body_cap=PromptBudgets.derive(cfg.single_call_char_limit).failed_body)

    file_constraints = FileConstraintsReader(
        configs_dir=Path(__file__).resolve().parent.parent / "project_configs",
        is_nightly=lambda: scope.active is not None and not scope.active.is_default,
        log=ports.log,
    )

    def _via_deps_impl_fill() -> ImplFillDeps:
        if app_ref is None:
            raise RuntimeError("impl_fill deps resolved without app_ref")
        return app_ref().impl_fill_deps

    def _via_deps_group_fill() -> GroupFillDeps:
        if app_ref is None:
            raise RuntimeError("group_fill deps resolved without app_ref")
        return app_ref().group_fill_deps

    # Former pipeline delegate defs, bound direct. The splice gate + the
    # cluster caches resolve through the LIVE App when app_ref is provided (a
    # test's dataclasses.replace on the held App wins), else the injected values.
    def _parse_ok(path: Path) -> bool:
        """Splice-gate validator: parse-check via the live structure reader —
        only fill-tree files are gated, never the frozen oracle/tests."""
        reader = app_ref().structure if app_ref is not None else structure
        return reader.parse_ok(path)

    # Callable as the gate always was; a refusal also asks it what the parser
    # says about the refused text (`adapters.rust_parse`).
    _validate_parse = ParseCheck(_parse_ok)

    splice_fn_body_at = functools.partial(
        _splice_mod.splice_fn_body_at, validate_parse=_validate_parse)
    splice_fn_body = functools.partial(
        _splice_mod.splice_fn_body, validate_parse=_validate_parse)
    replace_fn_signature = functools.partial(
        _splice_replace_fn_signature, validate_parse=_validate_parse)
    restore_frozen_signature = functools.partial(
        _splice_restore_frozen_sig, validate_parse=_validate_parse)
    locate_fn_by_record = functools.partial(
        _splice_mod.locate_fn_by_record, enumerate_fns)

    def splice_fn_with_where(
        file_path: Path, container: str, fn_name: str, body: str,
        where_clause: str | None = None, allow_nonstub: bool = False,
        sig_text: str = "",
    ) -> Any:
        return _splice_mod.splice_fn_with_where(
            enumerate_fns, file_path, container, fn_name, body,
            where_clause, allow_nonstub, validate_parse=_validate_parse,
            sig_text=sig_text)

    # ONE scan, returning the inventory (sites and units by name); every
    # consumer picks the cardinality it means at its own call site.
    scan_unimplemented_functions = functools.partial(
        _stub_scan.scan_stub_inventory, enumerate_fns=enumerate_fns)
    source_hash = functools.partial(
        _structure_source_hash,
        is_gen_tests_sibling_name=_is_gen_tests_sibling_name,
        strip_gen_tests_decls=_gen_strip_all_decls)

    def count_active_tests_for_cluster(project_dir: Path, cluster: Any) -> int:
        """Strict-verdict gate (application.test_count); the cluster
        caches are re-read per call so a swapped App's fakes win."""
        sc = app_ref().structure_cache if app_ref is not None else structure_cache
        return _tc_app.count_active_tests_for_cluster(
            project_dir, cluster, _tc_app.TestCountDeps(
                enumerate_fns=enumerate_fns,
                mod_records=sc.mod_records,
                file_cfg_predicate=sc.file_cfg_predicate,
                is_gen_tests_file=_disc_is_gen_tests_file,
                enumerate_doc_items=enumerate_doc_items,
            ))

    def active_test_inventory_for_cluster(
            project_dir: Path, cluster: Any,
            tests_dir_stems: frozenset[str] | None = None,
            include_lib_sources: bool = False,
            file_index: dict[str, str] | None = None,
    ) -> tuple[int, frozenset[str]]:
        """Sweep-level strict verdict: (expected_count, expected_names) — same walk
        and deps as `count_active_tests_for_cluster`, scopable to the
        `--test stem…` binaries a TDD sweep runs; `include_lib_sources` adds
        the gen_tests + src-side unit tests a `--lib` sweep also runs.

        `file_index`, when given, is filled with {test name: declaring file} on
        the SAME walk — an out-parameter so the pair every call site
        destructures keeps its shape. It goes through this closure rather than
        through a second one that rebuilds `TestCountDeps` by hand, because the
        anchor's whole promise is that it cannot describe a different set of
        files from the denominator, and two literal dep blocks are two things to
        keep in step."""
        sc = app_ref().structure_cache if app_ref is not None else structure_cache
        return _tc_app.active_test_inventory_for_cluster(
            project_dir, cluster, _tc_app.TestCountDeps(
                enumerate_fns=enumerate_fns,
                mod_records=sc.mod_records,
                file_cfg_predicate=sc.file_cfg_predicate,
                is_gen_tests_file=_disc_is_gen_tests_file,
                enumerate_doc_items=enumerate_doc_items,
            ), tests_dir_stems=tests_dir_stems,
            include_lib_sources=include_lib_sources, file_index=file_index)

    # More former pipeline delegate defs, bound direct.
    def cluster_cfg_attr(cluster: Any) -> str:
        return _membership.cluster_cfg_attr(cluster, scope.all)

    def _world_type_members(records: list[Any], cluster: Any) -> list[Any]:
        """The type table every fill route is shown: fields and methods of
        every world, the gated ones marked (`cluster_filter_type_members`)."""
        return _membership.cluster_filter_type_members(
            records, cluster, list(scope.all))

    # A generated test's worlds are measured, not predicted
    # (`application.test_worlds`): the accepted file is run under every other
    # baked world's env and gated to the worlds it passes in.
    _test_worlds_deps = TestWorldsDeps(
        clusters=lambda: list(scope.all),
        world_gate=cluster_cfg_attr,
        passes_in=functools.partial(
            test_passes_in_world, cluster_env=cluster_cargo_env,
            timeout=cfg.build_timeout, log=ports.log),
        log=ports.log, emit=ports.emit,
        world_key=world_key)

    def _settle_test_gate(project_dir: Path, module_name: str, test_file: Path,
                          block: str, cluster: Any) -> str | None:
        return settle_test_gate(project_dir, module_name, test_file, block,
                                cluster, _test_worlds_deps)

    # The original's doc examples, emitted as oracle tests before strip
    # (`application.doc_example_tests`), each verified and gated by the same
    # per-world measurement as a generated test.
    _doc_example_deps = DocExampleDeps(
        enumerate_doc_items=enumerate_doc_items,
        world_gate=cluster_cfg_attr,
        settle=_settle_test_gate,
        measurable=lambda c: world_is_measurable(c, _test_worlds_deps),
        log=ports.log, emit=ports.emit)

    def _emit_doc_examples(project_dir: Path, module_name: str,
                           clusters: Any) -> Any:
        return emit_doc_example_tests(project_dir, module_name, clusters,
                                      _doc_example_deps)

    def build_test_reverse_map(test_files: Any, src_dir: Path) -> Any:
        deps_a = app_ref().attribution_deps if app_ref is not None else _attr_deps
        return _attribution.build_test_reverse_map(deps_a, test_files, src_dir)

    def filter_test_targets(frontier: Any, src_dir: Path, *, cluster: Any) -> Any:
        """The frontier's admission filter: drop keys that resolve to a
        #[cfg(test)]/#[test] fn (the oracle) and keys at a site `cluster`
        never compiles (`cluster_membership.cluster_dead_sites`), from one
        enumeration of the tree. `cluster` is required so no caller can
        admit a dead site by omission; the dead drops are not logged here —
        a TDD iteration reports what its admission dropped once, with the
        denominator. On run `473471-33301154` (2026-09-29) nothing dropped
        `src/nightly.rs::fmt` from `stable`'s frontier, and it took a
        dispatch slot, 31 route-run targets and held two tests' escalation."""
        recs = enumerate_fns(src_dir) or []
        kept = _test_targets_mod.filter_test_targets(frontier, recs, log=ports.log)
        return structure_cache.dead_sites_among(recs, src_dir, cluster).split(kept)[0]

    def extract_skeleton(input_dir: Path, output_dir: Path,
                         cluster: Any = None) -> Any:
        """Skeletonise a live source tree (bake step only — Hard Rule 11);
        item-level cfg filtering runs under the cluster's cfg-set first."""
        ct = tuple(cluster.cfg_true) if cluster is not None else ()
        cf = tuple(cluster.cfg_false) if cluster is not None else ()
        live = app_ref().adapters if app_ref is not None else adapters
        return live.extractor.skeletonise(
            input_dir, output_dir, cfg_true=ct, cfg_false=cf)

    def public_api_from_source(content: str) -> str:
        return _source_view.public_api_from_source(
            content,
            enumerate_types=enumerate_types,
            enumerate_fns=enumerate_fns,
            enumerate_consts=lambda sd: (
                app_ref().structure if app_ref is not None else structure
            ).enumerate_consts(sd),
        )

    collect_crate_macros = functools.partial(
        _structure_collect_crate_macros,
        max_chars=max(6_000, cfg.single_call_char_limit // 25))
    format_api_surface_section = functools.partial(
        _fmt_api_surface_impl, cap=max(8_000, cfg.single_call_char_limit // 2))
    repo_spec_section = _make_repo_spec_section(
        PromptBudgets.derive(cfg.single_call_char_limit).repo_spec)
    _build_script_reader = CargoBuildScriptReader()

    # The TASK-1→4 reconstruction roles, built here (ReconstructRoles
    # dissolved). Task/validator deps resolve through the LIVE App at call
    # time — same swap semantics the old pipeline shims had via `_APP`.
    def _live() -> App:
        if app_ref is None:
            raise RuntimeError("reconstruction role resolved without app_ref")
        return app_ref()

    def generate_fix_loop(
        project_dir: Path, check_cmd: str, source_paths: list[Path],
        existing_test_files: set[str], **kw: Any,
    ) -> tuple[bool, str]:
        """Shared iterative fix loop; resolves gen_fix_loop_deps through the
        LIVE app so seams swapped after build still intercept. model/timeout
        default inside the impl to deps.MODEL/deps.BUILD_TIMEOUT."""
        return _fix_loop_mod.generate_fix_loop(
            _live().gen_fix_loop_deps, project_dir, check_cmd, source_paths,
            existing_test_files, **kw)

    def git_commit(project_dir: Path, message: str) -> bool:
        return _ws_git_commit(_live().ws_deps, project_dir, message,
                              managed=run_state.git_managed)

    def git_tag(project_dir: Path, tag: str) -> None:
        _ws_git_tag(_live().ws_deps, project_dir, tag,
                    managed=run_state.git_managed)

    def git_checkpoint(project_dir: Path, message: str) -> bool:
        return _ws_git_checkpoint(_live().ws_deps, project_dir, message,
                                  managed=run_state.git_managed)

    collect_test_files = functools.partial(
        _disc_collect_test_files, is_gen_tests_decl_only=_gen_is_decl_only_src)

    def u2_test_gate(ground: WriteGround, unit_fns: Any, test_cmd: str | None,
                     target_src_dir: Path, cluster_env: dict[str, str] | None,
                     baseline: Any = None) -> Any:
        """Test-acceptance gate. run_cargo late-binds the pipeline seam via
        helpers; enabled/timeout re-read settings_now() so a patched SETTINGS
        wins; structure resolves through the LIVE app (fake readers win)."""
        caps = helpers.settings_now().fix_caps
        return _cargo_runner_u2_gate(
            helpers.run_cargo, ground, unit_fns, test_cmd, target_src_dir,
            cluster_env, structure=_live().structure,
            enabled=caps.unified_fill_loop, timeout=caps.u2_test_timeout,
            err_cap=cfg.fix_err_cap, guard=ports.guard, baseline=baseline)


    def commit_and_mark(
        project_dir: Path, phase_key: str, status: str, *,
        commit_message: str, tag: str | None = None, detail: str = "",
        source_hash: str | None = None,
    ) -> None:
        """Commit-then-mark (adapters.commit_gate carries the ordering
        rationale); binds the live git/state primitives."""
        _commit_gate_commit_and_mark(
            project_dir, phase_key, status, commit_message=commit_message,
            tag=tag, detail=detail, source_hash=source_hash,
            git_commit=lambda pd, msg: _ws_git_commit(
                ws_deps, pd, msg, managed=run_state.git_managed),
            git_tag=lambda pd, t: _ws_git_tag(
                ws_deps, pd, t, managed=run_state.git_managed),
            mark_phase=mark_phase)

    def task1(req: Any) -> bool:
        ports.emit("task.start", task="task1")
        return _task1_run(req, _live().task1_deps)

    def task4(genesis_path: Any, functional_req_path: Any, test_output: Any,
              iteration: Any, output_dir: Any, **kw: Any) -> tuple[bool, int]:
        ports.emit("task.start", task="task4")
        return _task4_run(genesis_path, functional_req_path, test_output,
                          iteration, output_dir, _live().task4_deps, **kw)

    def validate_task2_output(project_dir: Path, expected_paths: list[str],
                              req_path: Path | None = None) -> tuple[bool, str]:
        return _validate_task2_output_impl(
            project_dir, expected_paths, req_path, _live().validator_deps)

    def validate_task2_api_conformance(project_dir: Path, module_name: str,
                                       module_dir: Path) -> tuple[bool, str]:
        return _validate_task2_api_impl(
            project_dir, module_name, module_dir, _live().validator_deps)

    def task_genesis_audit(genesis_path: Path) -> bool:
        return _genesis_audit_run(genesis_path, _live().genesis_audit_deps)

    def task0_cfg_scan(project_dir: Path) -> Any:
        """Derive compiler clusters from project source — no LLM, pure static
        scan (application.tasks.cfg_scan + domain.cluster_plan); writes
        build_configs.md for humans."""
        live = app_ref().adapters if app_ref is not None else adapters
        scan = _cfg_scan_mod.scan_build_clusters(
            project_dir, live.extractor, _build_script_reader)
        _wsf_write_build_configs(project_dir, scan.clusters, scan.predicate_counts)
        return scan.clusters

    # Coverage + mutation constructed here (injected them pre-built).
    coverage_deps = CoverageDeps(
        log=ports.log, cargo_env=ports.cargo_env, cluster=scope.get_active,
        tarpaulin_bin=cfg.tarpaulin_bin, grcov_bin=cfg.grcov_bin,
        build_timeout=cfg.build_timeout, ignore_pragma=cfg.ignore_pragma,
    )
    coverage_provider = make_coverage_provider(settings.coverage_backend, coverage_deps)
    mutation = MutationTester(
        cargo_env=ports.cargo_env, ask=ask_client.ask, log=ports.log,
        task0c_model=settings.task_models.task0c,
        single_call_char_limit=cfg.single_call_char_limit,
    )
    # TASK-2 residual-cleanup deps (LLM pass over leftover unimplemented!()).
    cleanup_residuals_deps = _cleanup_residuals.CleanupResidualsDeps(
        scan_unimplemented=scan_unimplemented_functions,
        load_skeleton_files=_load_skeleton_files,
        ask=ask_client.ask,
        task2_model=settings.task_models.task2,
        single_call_char_limit=cfg.single_call_char_limit,
        file_format_instruction=FILE_FORMAT_INSTRUCTION,
        log=ports.log,
    )
    # Former pipeline delegate shims, bound direct (their bodies never
    # touched a patched module seam — the log/emit/deps they need are ports/locals).
    check_graceful_stop = functools.partial(_graceful_stop.check_graceful_stop, ports.log)
    check_auto_operator = functools.partial(_auto_op_check, ports.log)

    def focus_cargo_errors(output: str, file_rel: str, max_chars: int | None = None) -> str:
        return _focus_cargo_errors_impl(
            output, file_rel, max_chars if max_chars is not None else cfg.fix_err_cap)

    def task2_cleanup_residuals(
        target_src_dir: Path, project_dir: Path, functional_req: str,
        test_files: list[tuple[str, str]], module_name: str,
    ) -> dict[str, Any]:
        return _cleanup_residuals.cleanup_residuals(
            target_src_dir, project_dir, functional_req, test_files, module_name,
            deps=cleanup_residuals_deps)

    def task2_fill_groups(skeleton_dir: Any, target_src_dir: Any, groups: Any,
                          project_dir: Any, **kwargs: Any) -> Any:
        return _task2_fill_groups_impl(
            _via_deps_group_fill(), skeleton_dir=skeleton_dir,
            target_src_dir=target_src_dir, groups=groups,
            project_dir=project_dir, **kwargs)

    extract_and_store_rules = functools.partial(
        _ga_extract_and_store_rules, genesis_rules_write=_db_genesis_rules_write)

    def whole_tree_retrospective(error_excerpt: str, history: list[dict[str, Any]], *,
                                 name: str = "fix_loop") -> str:
        return _whole_tree_retro_impl(
            error_excerpt, history, name=name,
            ask_retrospective=helpers.ask_retrospective, log=ports.log,
            fix_err_cap=cfg.fix_err_cap, emit=ports.emit)

    def task2_group_tests(skeleton_dir: Path, test_files: list[tuple[str, str]],
                          cluster_name: str, module_name: str) -> list[dict[str, Any]]:
        return _group_tests_impl(
            skeleton_dir, test_files, cluster_name, module_name,
            ask=ports.ask, model=settings.task_models.task1, log=ports.log,
            load_skeleton_files=_load_skeleton_files)

    def _coverage_event_data(measured: CoverageMeasurement, project_dir: Path) -> dict[str, Any]:
        # `files` is the whole per-file reading, keyed as task0c names files:
        # the monitor's overview and projection are built from it, scoped by
        # run_id, and from nothing on disk.
        return {"pct": measured.pct, "covered": measured.covered,
                "coverable": measured.coverable,
                "backend": settings.coverage_backend,
                "file_count": len(measured.files),
                "files": {rel: list(row) for rel, row
                          in measured.file_lines(project_dir).items()},
                "function_records": (None if measured.functions is None
                                     else len(measured.functions))}

    def measure_coverage(project_dir: Path, module_name: str) -> CoverageMeasurement:
        t0 = _time.monotonic()
        measured = coverage_provider.measure(project_dir, module_name)
        ports.emit(
            "coverage.measured", module=module_name,
            cluster=scope.active.name if scope.active is not None else None,
            duration_ms=int((_time.monotonic() - t0) * 1000),
            data=_coverage_event_data(measured, project_dir))
        # Returned whole: re-packing the four line fields dropped the function
        # counts the augmentation stop now reads.
        return measured

    def quick_file_coverage(project_dir: Path, module_name: str,
                            rel_path: str) -> tuple[int, int] | None:
        """Task0c's quick per-file check. The backend measures the whole crate;
        the whole reading is emitted (`coverage.quick_measured`) and task0c
        gets the row of the file it asked about."""
        t0 = _time.monotonic()
        measured = coverage_provider.measure_quick(project_dir, module_name)
        if measured is None:
            return None
        ports.emit(
            "coverage.quick_measured", module=module_name, file=rel_path,
            cluster=scope.active.name if scope.active is not None else None,
            duration_ms=int((_time.monotonic() - t0) * 1000),
            data=_coverage_event_data(measured, project_dir))
        return measured.file_row(project_dir, rel_path)

    def test_augmentor_factory() -> Any:
        backend = _os.getenv("TEST_AUGMENTOR", "static").lower()
        if backend != "static":
            ports.log(f"  WARNING: unknown TEST_AUGMENTOR='{backend}', falling back to static.")
        return StaticRustTestAugmentor(
            cargo_env=ports.cargo_env, build_timeout=cfg.build_timeout,
            measure_coverage=measure_coverage)

    # Pre-strip API-surface capture + --restart-from-augmented restore.
    api_surface = ApiSurfaceCapture(
        api_surface_path=_api_surface_path, type_manifest_path=_type_manifest_path,
        condense_to_signatures=_condense_rust_to_signatures,
        strip_gen_tests_decls=_gen_strip_all_decls,
        enumerate_types=adapters.extractor.enumerate_types,
        is_test_file=_disc_is_test_file, skip_dir_names=cfg.skip_dir_names,
        log=ports.log,
        enumerate_fns=enumerate_fns,
        pub_items_manifest_path=_pub_items_manifest_path,
        author_private_fields=cfg.layout_author_private_fields,
    )
    restore_from_augmented = RestoreFromAugmented(
        read_state=_state_read_state, write_state=_state_write_state, log=ports.log,
    )
    restore_from_pre_augment = RestoreFromPreAugment(
        read_state=_state_read_state, write_state=_state_write_state, log=ports.log,
    )

    # Per-run structure caches (mod records + cluster-specific symbols)
    # with the cluster-membership queries bound to them. enumerate seams go
    # through helpers (late-bind → live _APP.structure, so fake extractors win);
    # impl-block collection through the swap-aware App seam.
    structure_cache = StructureCache(
        enumerate_fns=enumerate_fns,
        enumerate_mods=enumerate_mods,
        collect_impl_blocks=_via("collect_impl_blocks"),
        log=ports.log,
    )

    # TASK-0c augmentation cluster (injected it pre-built).
    augment_store = FileAugmentStore()
    augment_engine = CargoAugmentEngine(
        log=ports.log, cargo_env=ports.cargo_env, build_timeout=cfg.build_timeout,
        default_setup=cfg.setup_script,
        collect_test_files=collect_test_files,
        augment_module=task0c_augment_module,
        behaviour_coverage=task0c_behaviour_coverage,
        behaviour_pass=task0c_behaviour_pass,
    )
    augment_roles = AugmentRoles(
        store=augment_store, engine=augment_engine,
        augmentor=ClusterAugmentor(
            telemetry=telemetry, scope=scope, store=augment_store,
            engine=augment_engine,
            gate=CallableCommitGate(commit_and_mark),
        ),
    )

    def _typed_fn_records(sd: Path) -> list[FnRecord]:
        return [FnRecord.from_dict(d) for d in (enumerate_fns(sd) or [])]

    # t1.5: layout phase runner + stall-exit re-trigger.
    run_layout_phase = build_layout_phase_runner(
        enabled=cfg.layout_authoring_enabled,
        ask=ask_client.ask, task2_model=settings.task_models.task2,
        load_baked_clusters=_load_baked_clusters,
        fn_name_counts=_fn_name_counts, log=ports.log,
        author_private_fields=cfg.layout_author_private_fields,
        lanes=settings.layout_lanes,
    )
    retrigger_layout = build_retrigger_layout(
        ask=ask_client.ask, task2_model=settings.task_models.task2,
        load_baked_clusters=_load_baked_clusters,
        enumerate_fns=_typed_fn_records,
        enumerate_types=enumerate_types,
        git_commit=git_commit, log=ports.log,
        max_attempts=cfg.layout_retrigger_max_attempts,
        parse_ok=_validate_parse,
    )
    _preserve_inline_tests_deps = PreserveInlineTestsDeps(
        log=ports.log, emit=ports.emit,
        next_gen_tests_n=_gts_next_gen_tests_n,
        gen_tests_path=helpers.gen_tests_path,
        build_gen_tests_decl=helpers.build_gen_tests_decl,
        derive_gentest_prelude=helpers.derive_gentest_prelude,
        is_gen_tests_file=helpers.is_gen_tests_file,
    )
    def _seal_oracle(project_dir: Path, module: str) -> None:
        """Tag the strip commit as the oracle's seal."""
        git_tag(project_dir, _oracle_tag(module))
        ports.log(f"  [oracle] sealed '{module}' at {_oracle_tag(module)}")

    # Strip/bake/commit pre-step deps + the pre-strip bake (Hard Rule 11).
    strip_bake_deps = StripBakeDeps(
        log=ports.log, phase_valid=phase_valid,
        module_augmented_path=augment_roles.store.module_path,
        capture_api_surface=api_surface.capture,
        bake_skeleton_artifacts=bake_skeleton_artifacts,
        git_commit=git_commit, git_tag=git_tag,
        strip_module_source=_strip_module_source,
        commit_and_mark=commit_and_mark,
        set_current=set_current, tarpaulin_bin=cfg.tarpaulin_bin,
        coverage_target=helpers.coverage_target,
        run_layout_phase=run_layout_phase,
        invalidate_structure=structure_cache.invalidate,
        preserve_inline_tests=lambda p: preserve_inline_tests(
            p / "src", p, _preserve_inline_tests_deps),
        seal_oracle=_seal_oracle,
    )
    bake_artifacts_deps = BakeArtifactsDeps(
        log=ports.log,
        cfg_scan=task0_cfg_scan,
        prune_clusters=_prune_redundant_clusters,
        fn_name_counts=_fn_name_counts,
        extract_skeleton=extract_skeleton,
        is_gen_tests_file=helpers.is_gen_tests_file,
        gen_tests_origin_cfgs=_gen_tests_origin_cfgs,
        save_behaviour_suite=save_behaviour_suite,
        # The bake MERGES into the suite task0d wrote before t0c (existing
        # entries win) instead of re-harvesting the doc fences over it.
        load_behaviour_suite=load_behaviour_suite,
        enumerate_types=enumerate_types,
        enumerate_fns=_typed_fn_records,
        enumerate_mods=enumerate_mods,
        enumerate_uses=enumerate_uses,
        save_private_surface=save_private_surface,
        author_private_fields=cfg.layout_author_private_fields,
        # Late-bound: `task0d_deps` is built below; the bake runs long after.
        build_book=lambda pd, mod, cls, suites, src: build_book_from_source(
            task0d_deps, pd, mod, cls, suites, src),
    )

    def _t0d_public_surface(pd: Path, _cluster: str) -> str:
        # The probe prompt needs the public API before the bake has captured
        # it: capture it here from the ORIGINAL source (the same adapter the
        # bake runs later, so the artefact is byte-identical), then read it.
        # Single-module workspaces only — `module_dir` is the project dir, as
        # `bake_skeleton_artifacts(project_dir, project_dir.name)` assumes.
        try:
            api_surface.capture(pd, pd, pd.name)
        except Exception as exc:  # pragma: no cover - adapter failure is logged, not fatal
            ports.log(f"  [task0d] API surface capture failed (probes get an "
                      f"empty surface): {exc}")
        return _load_api_surface(pd, pd.name)

    # The behaviour harvest BEFORE task0c: test assertions, doc-tests and
    # layout pins from the original, probes executed on it, exercises by
    # coverage. Harvested 2026-08-26 over anyhow's tests/: 203 specs, 197
    # with a runtime then, against 0 in the doc-fence-only suite on disk.
    def _verify_behaviour_tests(
        pd: Path, cl: Any, crate: str, stem: str, extra: Mapping[str, str],
    ) -> tuple[bool, str]:
        """Compile and run ONE emitted behaviour-test binary against the crate
        at `pd`, under `cl`'s env plus `extra`.

        Same shape as task0c's `_engine_runtime_gate`: scoped to this file's
        cargo `--test` binary and `--test-threads=1`, so a missing verdict reads
        as "this binary died" rather than as someone else's failure. It runs in
        the pre-strip window, where `pd` still holds the ORIGINAL crate — the
        tree the captured values came from — which is what makes a red verdict
        here a statement about the capture and not about a reconstruction."""
        env = probe_env(cl, pd)
        env.update(extra)
        cmd = ["cargo", "test", "--test", stem, "--", "--test-threads=1"]
        if crate:
            cmd[2:2] = ["-p", crate]
        rc, out, err = _probe_subprocess_runner(cmd, Path(pd), env, cfg.test_timeout)
        return rc == 0, (out or "") + "\n" + (err or "")

    # The ORIGINAL crate's construction facts (`domain.fn_routes`), from the
    # same reading t0c takes per cluster; the strategy file's computed part
    # and t0d's reach probes read them.
    construction_facts = StrategyFacts(
        read_world=functools.partial(read_world_facts, inputs=FnCriterionInputs(
            enumerate_fns=enumerate_fns, enumerate_mods=enumerate_mods,
            measure_reachability=functools.partial(
                measure_world_reachability, cluster_env=cluster_cargo_env,
                timeout=cfg.test_timeout),
            enumerate_types=enumerate_types)),
        log=ports.log)
    task0d_deps = Task0dDeps(
        log=ports.log, emit=ports.emit,
        mark_phase=mark_phase, phase_valid=phase_valid,
        commit_and_mark=commit_and_mark,
        read_sources=_t0d_behaviour_sources,
        read_test_sources=_t0d_test_sources,
        harvest=harvest_behaviour_suite,
        save_suite=save_behaviour_suite, load_suite=load_behaviour_suite,
        # The probe and rule-probe program calls reason only under
        # `TASK0D_PROGRAM_THINK`.
        ask=lambda prompt: str(ports.ask(
            prompt, model=settings.task_models.task0c,
            think=settings.task0d_program_think)),
        # The book judge's calls are a measurement: bound at their own
        # temperature, forwarded only when set (unset keeps the server's).
        # Author and judge both reason only under `BEHAVIOUR_BOOK_THINK`.
        book_predict=lambda prompt: str(ports.ask(
            prompt, model=settings.task_models.task0c,
            think=settings.behaviour_book_think,
            **({} if settings.behaviour_book_predict_temperature is None
               else {"temperature": settings.behaviour_book_predict_temperature}))),
        book_ask=lambda prompt: str(ports.ask(
            prompt, model=settings.task_models.task0c,
            think=settings.behaviour_book_think)),
        public_surface=_t0d_public_surface,
        # The same (empty) inventory t1.5 receives (`composition_layout`).
        symbol_inventory=lambda _pd, _c: "",
        enumerate_fns=_typed_fn_records,
        enumerate_mods=enumerate_mods,
        enumerate_types=enumerate_types,
        run_probe=run_probe,
        exercises_by_coverage=lambda pd, cl, specs, recs, **kw: exercises_by_coverage(
            pd, cl, specs, recs, log=ports.log, **kw),
        # The reach round: routes from the facts t0b stored for each world,
        # outcomes beside the suite for t0c.
        load_construction_facts=lambda pd, c: next(
            (w for w in (construction_facts.load(pd) or ("", ()))[1] if w.cluster == c), None),
        save_reach=save_reach,
        record_misses=save_behaviour_misses,
        load_misses=load_behaviour_misses,
        emit_tests=functools.partial(
            emit_behaviour_tests, balanced=_rust_delimiters_balanced),
        verify_tests=_verify_behaviour_tests,
        settle_test_gate=lambda pd, crate, f, body, cl: _settle_test_gate(
            Path(pd), crate, Path(f), body, cl),
        tests_failing_alone=lambda pd, cl, crate, stem, names, extra=None: failing_alone(
            Path(pd), crate, stem, names, env={**probe_env(cl, pd), **dict(extra or {})},
            timeout=cfg.test_timeout),
        perturb_env=lambda c: perturbed_env(probe_env(c)),
        cluster_cfg_attr=cluster_cfg_attr,
        emit_doc_examples=_emit_doc_examples,
        PROBE_MAX_PER_CLUSTER=settings.task0d_probe_max,
        PROBE_BLOCKS_PER_REPLY=settings.task0d_blocks_per_reply,
        PROBE_REPAIR_ATTEMPTS=settings.task0d_probe_repairs,
        PROBE_CLASSES_PER_CLUSTER=settings.task0d_classes_per_cluster,
        EXERCISES_ENABLED=settings.task0d_exercises,
        PIN_ENV_DEBUG=settings.behaviour_pin_env_debug,
        PROBE_EDGES_PER_CLUSTER=settings.task0d_probe_edges,
        RULE_PROBES_MAX=settings.behaviour_rule_probes,
        RULE_PROBE_PROGRAMS=settings.behaviour_rule_probe_programs,
        LANES=settings.behaviour_book_lanes,
        HELD_OUT_SHARE=settings.behaviour_held_out_share,
        save_held_out=save_held_out, load_held_out=load_held_out,
        BOOK_BUDGET=BookBudget(
            rules=settings.behaviour_book_rules,
            max_features=settings.behaviour_book_max_features,
            examples_per_prompt=settings.behaviour_book_examples_per_prompt,
            drafts_per_reply=settings.behaviour_book_drafts_per_reply,
            lanes=settings.behaviour_book_lanes,
            predict_temperature=settings.behaviour_book_predict_temperature,
            think=settings.behaviour_book_think,
            sibling_chunks=settings.behaviour_book_sibling_chunks,
            given_phrases=settings.behaviour_book_given_phrases),
        write_features=write_feature_files,
    )

    model_notes_store = FileModelNotesStore(
        ask_client, settings.task_models.task2, ports.log,
        max_chars=MODEL_NOTES_MAX_CHARS,
        max_entries=MODEL_NOTES_MAX_ENTRIES,
        failed_body_cap=cfg.failed_body_cap,
        fix_err_cap=cfg.fix_err_cap,
    )
    # filter_notes stays helpers-bound (late-binds the pipeline delegate a test
    # can patch); guard is the truncation-warning port.
    fix_context = FixContextAssembler(
        budgets=PromptBudgets.derive(cfg.single_call_char_limit),
        filter_notes=model_notes_store.filter_for_prompt,
        guard=ports.guard,
    )
    # The 9 fill-engine ISP role adapters. Seam-sensitive collaborators go
    # through helpers (late-binding over the pipeline globals raw-scripting
    # tests patch) or _via (live-App swap); pure relocated fns bind direct.
    fill_roles = FillRoles(
        tel=EngineTelemetryAdapter(
            log_fn=ports.log,
            emit_fn=ports.emit,
            check_stop_fn=check_graceful_stop,
        ),
        mut=MutatorAdapter(
            locate_fn_by_record_fn=locate_fn_by_record,
            locate_fn_fn=_domain_locate_fn,
            splice_fn_body_at_fn=splice_fn_body_at,
            splice_fn_body_fn=splice_fn_body,
            replace_fn_signature_fn=replace_fn_signature,
            extract_signature_block_fn=_extract_signature_block,
            lock_for_fn=_splice_mod.splice_lock_for,
            parse_validator_fn=_validate_parse,
        ),
        cargo=CargoFillAdapter(
            run_fn=helpers.run_cargo,
            build_test_cmd_fn=_build_test_cmd,
            u2_test_gate_fn=_via("u2_test_gate"),
            focus_errors_fn=focus_cargo_errors,
            env_fn=ports.cargo_env,
            test_regression_baseline_fn=helpers.test_regression_baseline,
            test_regression_gate_fn=helpers.test_regression_gate,
            check_diag_fn=helpers.run_cargo_check_diag,
        ),
        prompt=PromptContextAdapter(
            ask_per_fn_body_fn=_via("ask_per_fn_body"),
            open_conversation_fn=conversation_factory(
                ask_client, model=settings.task_models.task2, log=ports.log,
                emit=ports.emit,
                max_transcript_chars=cfg.single_call_char_limit),
            gather_fn=fix_context.gather,
            collect_sibling_filled_bodies_fn=_collect_sibling_filled_bodies,
            extract_crate_idioms_fn=_extract_crate_idioms,
            collect_crate_macros_fn=collect_crate_macros,
            compute_cluster_macro_invariants_fn=_compute_cluster_macro_invariants,
            detect_crate_no_std_invariant_fn=_detect_crate_no_std_invariant,
        ),
        ws=WorkspaceModelAdapter(
            structure=structure,
            scan_stub_records_fn=_via("scan_stub_records_for_cluster"),
            scan_stub_tuples_fn=structure_cache.scan_unimplemented_for_cluster,
            collect_impl_blocks_fn=_via("collect_impl_blocks"),
            collect_impl_blocks_for_cluster_fn=_via("collect_impl_blocks_for_cluster"),
            fn_container_index_fn=_via("fn_container_index"),
            fn_belongs_to_cluster_fn=structure_cache.fn_belongs_to_cluster,
            cluster_dead_sites_fn=structure_cache.cluster_dead_sites,
            cluster_filter_type_members_fn=_world_type_members,
            all_clusters_fn=lambda: list(scope.all),
            is_gen_tests_file_fn=helpers.is_gen_tests_file,
            leaf_first_fn_order_fn=_leaf_first_fn_order,
        ),
        retros=RetrosAdapter(
            load_fn=_retros_load,
            save_fn=_retros_save,
            retro_key_fn=_retros_key,
            is_fn_fully_exhausted_fn=_retros_is_exhausted,
            diagnose_fn=_via("phase_c_retrospective"),
            append_note_fn=model_notes_store.append,
            extract_self_note_fn=model_notes_store.extract_self_note,
            record_error_codes_fn=model_notes_store.record_error_codes_seen,
            retire_disproved_notes_fn=model_notes_store.retire_disproved,
        ),
        tests=TestLinkerAdapter(
            find_tests_for_fn_fn=_find_tests_for_fn,
            classify_failures_fn=_via("classify_phase_c_test_failures"),
            write_test_fix_log_fn=_write_phase_c_test_fix_log,
        ),
        stall=StallPolicyAdapter(
            effective_stalls_fn=helpers.effective_stalls,
            converge_in_cluster_fn=helpers.converge_in_cluster,
        ),
        impl=ImplFillPathAdapter(
            fill_impl_blocks_fn=lambda **kw: _fill_impl_blocks(_via_deps_impl_fill(), **kw),
            prepare_impl_fill_fn=lambda **kw: _prepare_impl_fill(_via_deps_impl_fill(), **kw),
        ),
        route_ledger=RouteLedgerStore(),
    )

    # ---- the held-out agreement reading --------------------------------------
    # Bound once; both readers (a cluster's fill closing, task3's aggregate)
    # call the same use case. Share 0 takes no reading at all.
    _agreement_deps = AgreementDeps(
        load_held_out=load_held_out, scratch_tree=scratch_tree, run_probe=run_probe,
        record_rows=lambda module, cluster, where, rows: record_agreement_rows(
            _body_ledger_root(), module, cluster, where, rows),
        log=ports.log, emit=ports.emit)

    def _measure_agreement(**kw: Any) -> Any:
        if settings.behaviour_held_out_share <= 0:
            return None
        return measure_agreement(_agreement_deps, **kw)

    # ---- the reference floor -------------------------------------------------
    # Measured ONCE per run and memoized: `run_clusters_strict` is called by
    # task3, by the post-task3 fix loop's check, and by every re-verify, and the
    # measurement is a `git worktree add` plus one `cargo test` per cluster
    # (0.4 s and 1.1 s on `anyhow`). `None` means no floor could be measured, and
    # every consumer then falls back to the absolute criterion.
    #
    # REFERENCE_FLOOR=0 disables it. The escape hatch exists because this changes
    # what "done" MEANS: with it off, the run is judged by "every test passes and
    # no fn is hollow" exactly as before.
    _reference_cache: dict[Path, Any] = {}

    def _reference(project_dir: Path) -> Any:
        if _os.getenv("REFERENCE_FLOOR", "1") == "0":
            return None
        if project_dir not in _reference_cache:
            _reference_cache[project_dir] = measure_reference(
                project_dir, _load_baked_clusters(project_dir),
                ReferenceDeps(cargo_env=ports.cargo_env, parse_cmd=_parse_cmd,
                              enumerate_fns=enumerate_fns,
                              test_timeout=cfg.test_timeout,
                              cluster_env=cluster_cargo_env))
        return _reference_cache[project_dir]

    def _reference_hollow(project_dir: Path) -> frozenset[tuple[str, str]]:
        measured = _reference(project_dir)
        return measured.hollow if measured is not None else frozenset()

    _unreached_stub_deps = UnreachedStubDeps(
        enumerate_fns=enumerate_fns, enumerate_mods=enumerate_mods,
        measure_dead=functools.partial(
            measure_world_dead_code, cluster_env=cluster_cargo_env,
            timeout=cfg.test_timeout),
        read_compiler_view=functools.partial(
            measure_world_reachability, cluster_env=cluster_cargo_env,
            timeout=cfg.test_timeout, test_profile_mir=True),
        measure_removal=functools.partial(
            measure_world_removal, cluster_env=cluster_cargo_env,
            timeout=cfg.test_timeout),
        log=ports.log)

    def _unreached_stub_proofs(project_dir: Path, clusters: Any) -> dict[FnSite, str]:
        return read_unreached_stub_proofs(project_dir, clusters, _unreached_stub_deps)

    def _reference_excused(project_dir: Path, test_output: str) -> frozenset[str]:
        """Bare names of the failures in task 3's aggregated output that the
        reference floor excuses in EVERY cluster they failed in: the same
        failure as the original's under that cluster's cfg, or a test the
        original skipped there. It used to be every name the original failed
        or skipped in ANY cluster, so a test the original fails only in
        `nightly` was out of reach in `stable` too, and any failure of that
        name, whatever it said, ended the repair loop."""
        measured = _reference(project_dir)
        if measured is None:
            return frozenset()
        by_name = {str(c.name): c for c in (_load_baked_clusters(project_dir) or [])}
        judged = {
            name: (text, judge_cluster_capture(
                measured, name, text,
                task3_alone_reader(project_dir, cluster_cargo_env(by_name[name]),
                                   cfg.test_timeout))
                if name in by_name else None)
            for name, text in task3_cluster_outputs(test_output).items()}
        return excused_everywhere(judged)

    def _out_of_reach(ctx: Any, test_output: str) -> str:
        """Why no remaining failure can be repaired by rewriting a body, or "".

        Two sources of "cannot": the test's assertion is not about a body
        (`not_body_repairable_tests` — the taxonomy that already prunes the
        task3 retry frontier and logged `not body-repairable, excluded from the
        frontier: ui` live on 2026-08-06), or the pristine crate fails it here
        too. Answering "" whenever ANY failure is still reachable keeps this
        strictly a stopping rule: it can end a loop early, never continue one.
        """
        failed = _parse_failed_tests(test_output)
        if not failed:
            return ""
        tests_dir = ctx.project_dir / "tests"
        unrepairable = (not_body_repairable_tests(list(tests_dir.rglob("*.rs")))
                        if tests_dir.is_dir() else frozenset())
        reachable, blocked = _split_reachable_failures(
            failed, unrepairable, _reference_excused(ctx.project_dir, test_output))
        if reachable or not blocked:
            return ""
        return (f"the {len(blocked)} remaining failure(s) "
                f"({', '.join(sorted(blocked))}) are not repairable by rewriting "
                "a body — the assertion is about compiler output, or the "
                "original crate fails them on this machine too")

    _emitter = JsonlEventEmitter(events_log, run_context if run_context is not None else {})
    def _published_drift(project_dir: Path) -> tuple[str, ...]:
        loop = one_tree_cluster(_load_baked_clusters(project_dir) or [])
        if loop is None:
            return ()
        return published_drift(
            project_dir / ".skeleton-filled" / loop.name, project_dir)

    _task3_deps = Task3Deps(
        measure_agreement=lambda pd, cl, passed, expected: _measure_agreement(
            project_dir=pd, tree_dir=pd, module=Path(pd).name, cluster=cl, where="task3",
            oracle_pass=passed, oracle_total=expected),
        phase=_emitter.phase,
        load_baked_clusters=_load_baked_clusters,
        task3_legacy_single=helpers.task3_legacy_single,
        cargo_env=ports.cargo_env,
        parse_cmd=_parse_cmd,
        count_active_tests_for_cluster=count_active_tests_for_cluster,
        store_prompt_content=helpers.store_prompt_content,
        read=read,
        write=write,
        test_timeout=cfg.test_timeout,
        reference=_reference,
        cluster_env=cluster_cargo_env,
        # The one-tree loop writes ONE red ledger, under the tree's name, for
        # every world's red tests; each world's task 3 verdict prints it.
        red_ledger=lambda pd, _world: _load_red_ledger(
            pd, getattr(one_tree_cluster(_load_baked_clusters(pd) or []),
                        "name", _world)),
        published_drift=_published_drift,
    )

    def _task3(project_dir: Path, test_command: str, iteration: Any,
               output_dir: Any, genesis_path: Path) -> tuple[Any, str]:
        ports.emit("task.start", task="task3")
        passed, test_output = _task3_impl(
            project_dir, test_command, iteration, output_dir, genesis_path, _task3_deps)
        ports.emit("task.done", task="task3", data={"passed": bool(passed)})
        _emit_per_file_test_overview(
            Path(project_dir), _extract_test_output_from_file(test_output),
            context=f"task3 iter {iteration}")
        return passed, test_output



    gen_telemetry = GeneratorTelemetryAdapter(
        log_fn=ports.log,
        emit_fn=ports.emit,
        check_stop_fn=check_graceful_stop,
        set_current_fn=set_current,
        mark_phase_fn=lambda pd, key, status, detail="": _state_mark_phase(pd, key, status, detail=detail),
        phase_valid_fn=phase_valid,
    )
    gen_llm = LlmAskAdapter(ask_fn=lambda prompt, *, model=None: ask_client.ask(prompt, model=model))
    gen_workspace = WorkspaceAdapter(
        read_fn=read,
        write_source_files_fn=_write_source_files,
        load_manifest_fn=_load_manifest,
        save_manifest_fn=_save_manifest,
        load_original_structure_fn=_load_original_structure,
        crate_entry_point_fn=_crate_entry_point,
        is_proc_macro_crate_fn=_cargo_is_proc_macro,
    )
    gen_docs = ContextDocsAdapter(
        scan_downstream_api_fn=_scan_downstream_api,
        compile_patterns_read_fn=_compile_patterns_read,
        genesis_rules_read_fn=_genesis_rules_read,
        extract_test_assertions_fn=_extract_test_assertions,
        format_api_surface_section_fn=format_api_surface_section,
        split_api_surface_by_file_fn=_split_api_surface_by_file,
    )
    gen_cargo = CargoBackendAdapter(
        run_fn=lambda cmd, cwd, env, timeout=180: helpers.run_cargo(cmd, cwd, env, timeout),
        run_check_diag_fn=lambda cmd, cwd, env, timeout=180: helpers.run_cargo_check_diag(cmd, cwd, env, timeout),
        build_test_cmd_fn=_build_test_cmd,
        env_fn=ports.cargo_env,
        cluster_env_fn=cluster_cargo_env,
        implicated_fns_from_check_fn=lambda diags, out, tsd: (
            _implicated_fns_from_check_impl(
                diags, out, tsd, enumerate_fns=enumerate_fns)),
        attribute_failures_fn=lambda to, sd, rf, trm, cn, **kw: _via(
            "attribute_failures")(to, sd, rf, trm, cn, **kw),
        filter_test_targets_fn=filter_test_targets,
    )
    gen_fill = FillEngineAdapter(
        run_phase_c_tdd_fn=lambda **kw: _run_phase_c_tdd_impl(_via_deps_phase_c_tdd(), **kw),
        fill_fns_one_by_one_fn=lambda **kw: helpers.task2_fill_fns_one_by_one(**kw),
        fill_groups_fn=task2_fill_groups,
        group_tests_fn=task2_group_tests,
        cleanup_residuals_fn=task2_cleanup_residuals,
        scan_unimplemented_fn=scan_unimplemented_functions,
        reset_exhaustion_flags_fn=_reset_exhaustion_flags,
        build_test_reverse_map_fn=build_test_reverse_map,
    )
    gen_clusters = ClustersAdapter(
        load_baked_fn=_load_baked_clusters,
        load_baked_leaf_order_fn=_load_baked_leaf_order,
        filter_tests_for_cluster_fn=_membership.filter_tests_for_cluster,
        cluster_cfg_attr_fn=cluster_cfg_attr,
    )
    gen_gentests = GenTestsAdapter(
        build_decl_fn=helpers.build_gen_tests_decl,
        has_decl_fn=_has_gen_tests_mod_decl,
        strip_decl_n_fn=_strip_gen_tests_decl_n,
        derive_prelude_fn=helpers.derive_gentest_prelude,
        load_origin_fn=helpers.load_gen_tests_origin,
        propagate_fn=lambda gt, om, ps, pn, tag="": _membership.propagate_or_quarantine_sibling(
            gt, om, ps, pn, ports.log, tag),
        is_gen_tests_file_fn=helpers.is_gen_tests_file,
    )
    gen_scan = SourceScanAdapter(rust_delimiters_balanced_fn=_rust_delimiters_balanced)
    prompt_budget = functools.partial(_shaping.prompt_budget, cfg.single_call_char_limit)
    gen_shaping = PromptShapingAdapter(
        prompt_budget_fn=prompt_budget,
        format_test_files_fn=_shaping.format_test_files,
        chunk_test_files_by_file_fn=_shaping.chunk_test_files_by_file,
        format_api_surface_section_fn=format_api_surface_section,
    )
    gen_progress = ProgressFactoryAdapter(
        make_progress_fn=lambda total: ChunkProgress(total, progress_mode=run_state.progress_mode))
    gen_requirements = RequirementsDocAdapter(
        write_fr_preserving_notes_fn=_write_fr_preserving_notes,
        type_signals_read_fn=_type_signals_read,
    )

    def _build_task1_config() -> Task1Config:
        return Task1Config(
            model=settings.model,
            task1_model=settings.task_models.task1,
            thinking_directive=cfg.task1_thinking_directive,
            file_format_instruction=FILE_FORMAT_INSTRUCTION,
            single_call_char_limit=cfg.single_call_char_limit,
            synthesis_group_size=cfg.synthesis_group_size,
            readme_cap=cfg.readme_cap,
            strategy_cap=PromptBudgets.derive(cfg.single_call_char_limit).strategy,
            chunk_cache_dir=cfg.chunk_cache_dir,
            parallelism=helpers.pipeline_parallelism(),
        )

    def _task1_synthesizer_factory() -> Task1Synthesizer:
        backend = Task1Backend.coerce(_os.getenv("TASK1_SYNTHESIZER"), Task1Backend.LLM)
        kind = StaticTask1Synthesizer if backend is Task1Backend.STATIC else LLMTask1Synthesizer
        return kind(llm=gen_llm, shaping=gen_shaping, progress=gen_progress,
                    requirements=gen_requirements, cfg=_build_task1_config())

    def _build_task2_config() -> Task2Config:
        return Task2Config(
            file_format_instruction=FILE_FORMAT_INSTRUCTION,
            task2_model=settings.task_models.task2,
            thinking_directive=cfg.task2_thinking_directive,
            verbosity_guard=helpers.task2_verbosity_guard,
            genesis_cap=PromptBudgets.derive(cfg.single_call_char_limit).genesis,
            strategy_cap=PromptBudgets.derive(cfg.single_call_char_limit).strategy,
            single_call_char_limit=cfg.single_call_char_limit,
            model_label=settings.model,
        )

    def _build_skeleton_config() -> SkeletonConfig:
        live = helpers.settings_now()
        return SkeletonConfig(
            task2_model=live.task_models.task2,
            parallelism=helpers.pipeline_parallelism(),
            phase_c_per_fn=live.phase_c_mode is PhaseCMode.PER_FN,
            phase_c_tdd=live.phase_c_driver is PhaseCDriver.TDD,
            phase_c_max_passes=int(_os.getenv("PHASE_C_MAX_PASSES", "3")),
            validate_iters=int(_os.getenv("PHASE_C_VALIDATE_ITERS", "3")),
            recently_filled_max=int(_os.getenv("TDD_RECENTLY_FILLED_MAX", "50")),
        )

    def _new_skeleton_generator() -> StaticSkeletonTask2Generator:
        return StaticSkeletonTask2Generator(
            tel=gen_telemetry, cargo=gen_cargo, fill=gen_fill, clusters=gen_clusters,
            gentests=gen_gentests, scan=gen_scan, ws=gen_workspace,
            cfg=_build_skeleton_config(),
            structure=structure,
            layout_sync=(load_private_surface, load_layout_records,
                         apply_layout_to_tree),
            layout_registry=load_layout_registry,
            save_layout_registry=save_layout_registry,
            save_layout_records=save_layout_records,
            reference_measurement=_reference,
            agreement=_measure_agreement,
            # Resolved at call time: `_localize_crashes` is bound further
            # down this function, before any generator is built.
            localize_crashes=lambda *a, **k: _localize_crashes(*a, **k),
            blueprint_deps=BlueprintDeps(
                ask=lambda *a, **k: helpers.ask_raw(*a, **k),
                log=ports.log, emit=ports.emit,
                model=settings.task_models.task2,
                dispatch_families=lambda *a, **k: enumerate_dispatch_families(*a, **k)))

    def _task2_backend() -> Task2Backend:
        return Task2Backend.coerce(_os.getenv("TASK2_GENERATOR"), Task2Backend.FILELIST)

    def _task2_generator_factory() -> Task2Generator:
        backend = _task2_backend()
        common: dict[str, Any] = {"tel": gen_telemetry, "llm": gen_llm, "ws": gen_workspace,
                                  "docs": gen_docs, "cfg": _build_task2_config()}
        if backend is Task2Backend.LLM:
            return LLMTask2Generator(**common)
        if backend is Task2Backend.STATIC:
            return StaticTask2Generator(**common)
        if backend is Task2Backend.SKELETON:
            return _new_skeleton_generator()
        return StaticFileListTask2Generator(**common)


    _task2_deps = Task2Deps(
        generator_factory=_task2_generator_factory,
        load_manifest=_load_manifest,
        read=read,
        generate_fix_loop=generate_fix_loop,
        store_prompt_content=helpers.store_prompt_content,
        is_proc_macro_crate=_cargo_is_proc_macro,
        count_error_codes=lambda output: len(set(_rustc_error_codes(output or "", "E"))),
        build_check_retries=cfg.build_check_retries,
        build_max_stalls=cfg.task2_build_max_stalls,
        build_timeout=cfg.build_timeout,
        task2_model=settings.task_models.task2,
        generator_publishes=lambda: _task2_backend() is Task2Backend.SKELETON,
        check_build=lambda cmd, cwd, timeout: helpers.run_cargo(
            cmd, cwd, ports.cargo_env(), timeout),
    )

    def _task2_inner_loop(project_dir: Path, genesis_path: Path,
                          functional_req_path: Path, manifest_path: Path,
                          existing_test_files: Any, project_profile: str,
                          workspace_structure: str, build_check_cmd: str | None,
                          **k: Any) -> bool:
        ports.emit("task.start", task="task2")
        return _task2_inner_impl(
            project_dir, genesis_path, functional_req_path, manifest_path,
            existing_test_files, project_profile, workspace_structure,
            build_check_cmd, _task2_deps, **k)

    _task0b_deps = Task0bDeps(
        ask=ask_client.ask,
        write=write,
        is_test_file=_disc_is_test_file,
        is_gen_tests_file=helpers.is_gen_tests_file,
        strip_gen_tests_decls=_gts_strip_all_decls,
        condense_rust_to_signatures=_condense_rust_to_signatures,
        manifest_name=_MANIFEST_NAME,
        sibling_cap=PromptBudgets.derive(cfg.single_call_char_limit).sibling,
        single_call_char_limit=cfg.single_call_char_limit,
        task0_model=settings.task_models.task0,
        computed_part=construction_facts.computed_part,
    )

    # Role adapters + spine objects are referenced by the phase use-cases below,
    # so build them as locals first (a field can't reference a sibling field in
    # the same App(...) call), then thread them into the use-cases.
    phase_gate = PhaseGateAdapter(phase_valid, commit_and_mark)
    requirements = RequirementsAdapter(task1, _validate_task1_output)
    generator = GeneratorAdapter(
        _task2_inner_loop, _load_original_structure,
        validate_task2_output, validate_task2_api_conformance,
    )
    fixer = FixerAdapter(
        _classify_failure, ports.log,
        _insert_no_run, settings.fix_caps.fix_err_cap,
    )
    analyst = AnalystAdapter(
        task4, _validate_task4_output, task_genesis_audit,
    )
    failure_journal = FileFailureJournal()
    workspace_artifacts = WorkspaceArtifacts()
    lock_deps = LockDeps(
        commit_and_mark=commit_and_mark, set_current=set_current,
        lock_module=lock_module, git_commit=git_commit, log=ports.log,
        sys_exit=sys.exit,
        scan_unimplemented_functions=scan_unimplemented_functions,
        load_baked_clusters=_load_baked_clusters,
        reference_hollow=_reference_hollow,
        augmentation_fn_records=augment_store.all_fn_records,
        unreached_stub_proofs=_unreached_stub_proofs,
    )
    reattach_deps = ReattachDeps(
        log=ports.log, emit=ports.emit, derive_gentest_prelude=helpers.derive_gentest_prelude,
        is_gen_tests_file=helpers.is_gen_tests_file, gen_tests_siblings=helpers.gen_tests_siblings,
        load_gen_tests_origin=helpers.load_gen_tests_origin, gen_tests_path=helpers.gen_tests_path,
        build_gen_tests_decl=helpers.build_gen_tests_decl, cluster_cfg_attr=cluster_cfg_attr,
        has_gen_tests_decl=_has_gen_tests_mod_decl, cargo_env=ports.cargo_env,
    )
    test_runner = CallableTestRunner(task3_via_app)
    coverage_docs = CoverageDocsAdapter(
        lambda project_dir, test_files, readme_text, project_profile, clusters:
            _via("task0b_discovery")(
                project_dir / cfg.coverage_strategy_name, project_dir, test_files,
                readme_text, project_profile, clusters=clusters, force=True),
    )
    auditor = FlowAuditor(
        run_state=run_state, events_log=events_log, prompts_dir=prompts_dir,
        log=ports.log, emit=ports.emit,
        plateau_n=int(_os.getenv("AUDIT_PLATEAU_N", "3")),
        interval_sec=int(_os.getenv("AUDIT_INTERVAL_SEC", "900")))
    flow_audit = FlowAuditAdapter(auditor.report)
    docs_metrics = DocsMetricsAdapter(helpers.load_model_notes, helpers.genesis_rules_count)
    requirements_phase = RequirementsPhase(
        requirements=requirements, gate=phase_gate, telemetry=telemetry,
        analyst=analyst, journal=failure_journal, artifacts=workspace_artifacts,
    )
    reconstruction_step = ReconstructionStep(
        generator=generator, tests=test_runner, fixer=fixer,
        artifacts=workspace_artifacts, telemetry=telemetry,
        lock_deps=lock_deps, reattach_deps=reattach_deps,
        no_improvement_threshold=cfg.no_improvement_threshold,
    )
    def _lesson_test_files(project_dir: Path, cluster: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for root in (project_dir / "tests", project_dir / "src"):
            for f in sorted(root.rglob("*.rs")) if root.is_dir() else ():
                with contextlib.suppress(OSError):
                    out[str(f)] = f.read_text(encoding="utf-8", errors="replace")
        return out

    def _lesson_unit_bodies(project_dir: Path, cluster: str) -> dict[str, str]:
        tree = project_dir / ".skeleton-filled" / cluster
        if not tree.is_dir():
            return {}
        return {f"{r.file}::{r.fn_name}": r.body_text
                for r in (enumerate_fns(tree) or [])}

    _lessons_deps = LessonPassDeps(
        ask=lambda prompt: ports.ask(prompt, model=settings.task_models.task4),
        load_red_ledger=_load_red_ledger, load_lessons=_load_lessons,
        save_lessons=_save_lessons, test_files=_lesson_test_files,
        unit_bodies=_lesson_unit_bodies, log=ports.log,
        emit=lambda ev, **k: telemetry.emit(ev, **k))
    # The end gate's retro (`application/pinning_tests`): a pinning test per
    # blocking body, judged on the original crate and on the rebuilt tree.
    def _pin_deps(ctx: RunContext, *,
                  on_pinned: Callable[[tuple[FnSite, ...]], None] = lambda _sites: None,
                  ) -> PinningDeps:
        clusters = list(ctx.clusters)
        worlds = construction_facts.facts(ctx.project_dir, clusters, may_measure=False)
        return PinningDeps(
            ask=lambda prompt: ports.ask(prompt, model=settings.task_models.task0c),
            worlds_for=lambda pd, site: _pin_worlds_compiling(
                site, scan_unimplemented_functions(pd).site_preds(), clusters),
            routes_for=lambda pd, world, site: _pin_route_lines(
                worlds, str(getattr(world, "name", "")), site),
            signature_of=lambda pd, site: _pin_signature_text(
                helpers.structure_via_app.enumerate_fns(pd) or [], pd, site),
            api_surface=lambda pd, module: _load_api_surface(pd, module),
            example_header=lambda pd, world: _pin_example_header(
                pd, gate_line(cluster_cfg_attr(world))),
            gate_line=lambda world: gate_line(cluster_cfg_attr(world)),
            run_original=lambda pd, module, stem, text, world: _pin_run_on_original(
                pd, module, stem, text, world, cluster_env=cluster_cargo_env,
                timeout=cfg.build_timeout),
            run_rebuilt=lambda pd, module, stem, text, world, sites: _pin_run_on_rebuilt(
                pd, module, stem, text, world, sites, cluster_env=cluster_cargo_env,
                timeout=cfg.build_timeout,
                stub_panic_sites=lambda out, root: extract_tdd_frontier(
                    _attr_deps, out, root)[1]),
            run_original_emptied=lambda pd, module, stem, text, world, sites: (
                _pin_run_original_emptied(
                    pd, module, stem, text, world, sites, cluster_env=cluster_cargo_env,
                    timeout=cfg.build_timeout,
                    fn_records=lambda root: helpers.structure_via_app.enumerate_fns(root)
                    or [])),
            admit=_admit_pinning_test, settle=_settle_pin_on_original,
            log=ports.log, emit=ports.emit,
            max_attempts=cfg.pin_test_max_attempts, on_pinned=on_pinned)

    def _settle_pin_on_original(project_dir: Path, module: str, stem: str,
                                text: str, world: Any) -> str | None:
        """The world settle (`settle_test_gate`) for a pin, read on the
        ORIGINAL crate: after strip the workspace holds the reconstruction,
        so each world's run is the pin reader's own (`pin_runs.run_on_original`,
        each test alone included) over the file as the settle has it now."""
        scratch = project_dir / PIN_STAGE_DIR / "settle" / "tests" / f"{stem}.rs"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(text, encoding="utf-8")

        def _passes_on_original(_pd: Path, _m: str, _stem: str, _block: str,
                                cluster: Any) -> bool:
            cur = scratch.read_text(encoding="utf-8")
            run = _pin_run_on_original(project_dir, module, stem, cur, cluster,
                                       cluster_env=cluster_cargo_env,
                                       timeout=cfg.build_timeout)
            declared = frozenset(attributed_test_fn_names(cur))
            return run.ran and bool(declared) and declared <= run.passed

        try:
            gate = settle_test_gate(
                project_dir, module, scratch, text, world,
                dataclasses.replace(_test_worlds_deps,
                                    passes_in=_passes_on_original))
            return None if gate is None else scratch.read_text(encoding="utf-8")
        finally:
            with contextlib.suppress(OSError):
                scratch.unlink()

    def _admit_pinning_test(project_dir: Path, module: str, rel: str, text: str) -> bool:
        """The one writer of a test into the oracle after strip: the file,
        its commit, and the seal moved to that commit, so the launch-time
        drift check reads the extended oracle as sealed and every clock
        keyed on the oracle identity restarts by rule."""
        def _seal_clean() -> bool:
            drift = _workspace_oracle_drift(project_dir, module)
            return drift is not None and drift.clean
        return _admit_oracle_file(
            project_dir, rel, text,
            commit=lambda message: git_commit(project_dir, message),
            tag=lambda: git_tag(project_dir, _oracle_tag(module)),
            seal_clean=_seal_clean, log=ports.log)

    def _run_pins(ctx: RunContext, it: int, verdict: Any) -> int:
        pinned: list[FnSite] = []
        n = run_pinning_pass(ctx.project_dir, ctx.module_name, verdict,
                             _pin_deps(ctx, on_pinned=pinned.extend),
                             crate=ctx.module_name.replace("-", "_"))
        left = _gaps_left(verdict.blocking, pinned)
        _save_pin_gaps(ctx.project_dir, left)
        if left:
            ports.log(
                f"  [t4/pin] {len(left)} blocking body(ies) still unpinned — "
                "the next iteration asks again: "
                + ", ".join(_site_label(s) for s in left))
        return n

    def _open_gaps(ctx: RunContext) -> _EndVerdict | None:
        """Blocking bodies a pin pass left unpinned, still unimplemented.

        Asked on a red iteration: the pin that landed may have changed
        which body a call panics in. A body the fill has since written
        leaves the set. Nothing is asked before the end gate has named
        a gap — an empty store is not a scan of every stub."""
        pending = _load_pin_gaps(ctx.project_dir)
        if not pending:
            return None
        live = [s.site for s in scan_unimplemented_functions(ctx.project_dir).sites]
        still = _gaps_still_unimplemented(pending, live)
        if len(still) != len(pending):
            _save_pin_gaps(ctx.project_dir, still)
        if not still:
            return None
        ports.log(
            f"  [t4/pin] {len(still)} blocking body(ies) a pass left unpinned "
            "are still unimplemented — asking again: "
            + ", ".join(_site_label(s) for s in still))
        return _EndVerdict(blocking=still)

    def _run_lessons(ctx: RunContext, it: int) -> int:
        return run_test_lessons(ctx.project_dir, [c.name for c in ctx.clusters],
                                _lessons_deps, iteration=str(it))

    failure_analysis = FailureAnalysisStep(
        analyst=analyst, journal=failure_journal,
        artifacts=workspace_artifacts, coverage_docs=coverage_docs,
        audit=flow_audit, docs=docs_metrics, telemetry=telemetry,
        genesis_audit_interval=cfg.genesis_audit_interval,
        render_strategy=lambda ctx, text: construction_facts.rendered(
            ctx.project_dir, text,
            construction_facts.facts(ctx.project_dir, ctx.clusters, may_measure=False)),
        lessons=_run_lessons,
        pins=_run_pins,
        open_gaps=_open_gaps,
    )
    def _reenter(project_dir: Path, module: str, artifacts: int) -> bool:
        # Same keys restart_from.sh task2a clears — the manual escape hatch,
        # fired by the module loop after every failed iteration whose retro
        # gave the writer something new. Without one the next t2a would send
        # the prompts it already sent.
        if (helpers.settings_now().phase_c_driver is PhaseCDriver.TDD
                and artifacts <= 0):
            # The TDD driver writes the red ledger the lessons are read
            # from and owns the pinned tests; the legacy driver writes
            # neither, so it keeps re-entering.
            return False
        # With every sub-key: a phase that checkpoints its parts is re-opened
        # only when each part is (`reopen_phase`).
        for _key in (f"task2a/{module}", f"task2b/{module}", f"task2c/{module}"):
            reopen_phase(project_dir, _key,
                         detail="auto-reset: the retro admitted new artifacts")
        return True

    module_reconstructor = ModuleReconstructor(
        step=reconstruction_step, analysis=failure_analysis, telemetry=telemetry,
        plateau_abort=cfg.module_plateau_abort,
        reenter=_reenter,
        out_of_reach=_out_of_reach,
    )
    # Attribution deps built as a local first — the TDD driver's futility
    # escalation closes over it.
    _attr_deps = AttributionDeps(
        structure=helpers.structure_via_app,
        classify=_via("classify_phase_c_test_failures"),
        locate_fn=_domain_locate_fn,
        run_check_diag=helpers.run_cargo_check_diag,
        derive_check_only_cmd=_domain_derive_check_only_cmd,
        is_gen_tests_file=helpers.is_gen_tests_file,
        log=ports.log,
    )

    _retro_deps = PhaseCRetroDeps(
        ask_retrospective=helpers.ask_retrospective, log=ports.log,
        failed_body_cap=cfg.failed_body_cap, fix_err_cap=cfg.fix_err_cap,
        emit=ports.emit,
    )

    def _checkpoint_fill_tree(
        project_dir: Path, cluster: str, filled_dir: Path, mark: BestTreeMark,
    ) -> None:
        """Persist a new pass high-water fill tree to heal's durable restore
        tiers so a later parse-corruption heal restores this good state
        (best-tree copy → git ancestor) instead of nuking the file to the
        baked skeleton."""
        try:
            # The LAYOUT half travels with the bodies. Without it the slot is a
            # per-file donor for heal and nothing more: t1.5 re-authors the
            # skeleton every run, so bodies restored against a different
            # layout do not compile, and the restore this enables would be a
            # second destruction rather than a recovery.
            n = save_cluster_best_tree(
                project_dir, cluster, filled_dir, mark,
                layout_dir=project_dir / SKELETON_DIR / cluster)
        except Exception as e:  # snapshot must never break the fill loop
            ports.log(f"  [ratchet] cluster best-tree snapshot failed: {e}")
            n = 0
        # The commit message must name WHERE the tree it scores actually is.
        # It did not, and on 2026-08-08 that cost a day: commit `1f4099b`
        # ("task2a checkpoint — stable:95 passing") holds the best tree this
        # project has produced, and the obvious check — `git ls-tree <sha> --
        # src` — returns a one-line `lib.rs`, because the workspace `src/` is a
        # stub shell after strip and the reconstruction lives under
        # `.skeleton-filled/<cluster>/src`. The tree was written up as
        # unrecoverable and a restart was run over it while it sat in the object
        # store the whole time. The pair matters too: `.skeleton/<cluster>` is
        # the layout these bodies answer to, and t1.5 re-authors it per run.
        rel = f"{SKELETON_FILLED_DIR}/{cluster}"
        committed = git_commit_paths(
            WorkspaceDeps(log=ports.log, emit=ports.emit), project_dir,
            [rel],
            f"pipeline: task2a checkpoint — {cluster}:{mark.pass_count} passing "
            f"(iter {mark.iteration})\n\n"
            f"Tree: {rel}/src (NOT src/, which is a stub shell after strip).\n"
            f"Restore with the matching layout or the bodies will not compile:\n"
            f"  git archive <sha> {SKELETON_DIR}/{cluster} {rel} | tar -x -C <dest>",
            managed=run_state.git_managed)
        if committed:
            ports.log(f"  [ratchet] checkpoint — {cluster}:{mark.pass_count} "
                      f"passing, tree at {rel}/src")
        ports.emit("phase_c.checkpoint", data={
            "cluster": cluster, "pass_count": mark.pass_count,
            "tree_sha": mark.tree_sha, "oracle_size": mark.oracle_size,
            "best_tree_files": n, "git_committed": committed,
            "tree_path": f"{rel}/src",
            "layout_path": f"{SKELETON_DIR}/{cluster}/src"})

    def _assert_probe_for_tdd(
        project_dir: Path, cluster_root: Path, cluster_name: str,
        cluster_env: Mapping[str, str] | None,
        tests_by_file: Mapping[Path, Sequence[str]],
    ) -> Mapping[str, str]:
        """The sweep's observed assertion values (`assert_probe_runner`),
        rendered per failing test, on the tree as it stands."""
        return run_assert_probes(
            project_dir=project_dir, cluster_root=cluster_root,
            cluster_name=cluster_name, cluster_env=cluster_env,
            tests_by_file=tests_by_file, run_cargo=helpers.run_cargo,
            log=ports.log).rendered

    def _test_file_index(
        project_dir: Path, cluster: Any, stems: Any = None, lib: bool = False,
    ) -> dict[str, str]:
        """{test name: the file that declares it}, from the SAME walk the
        oracle inventory uses — literally the same function, so the anchor and
        the denominator cannot describe different sets of files."""
        idx: dict[str, str] = {}
        try:
            active_test_inventory_for_cluster(
                project_dir, cluster, tests_dir_stems=stems,
                include_lib_sources=lib, file_index=idx)
        except Exception as e:  # an anchor is never worth failing a sweep for
            ports.log(f"  [phase-c/crash] test file index unavailable: {e}")
            return {}
        return idx

    def _restore_best_tree_pair(
        project_dir: Path, cluster: str, filled_dir: Path,
    ) -> tuple[int, int, list[str]] | None:
        """Put the cluster's durable best tree back, bodies AND layout.

        Returns None when the slot cannot supply a PAIR — a slot written before
        the layout half was stored holds bodies that would not compile against
        the live skeleton, and declining is the only safe answer there; the
        operator lever (`scripts/restore_cluster_tree_from_sha.sh`) takes the
        pair out of the object store instead."""
        if not cluster_best_tree_has_layout(project_dir, cluster):
            return None
        try:
            return restore_cluster_best_tree(
                project_dir, cluster, filled_dir,
                layout_dir=project_dir / SKELETON_DIR / cluster)
        except Exception as e:  # a failed restore must never break the loop
            ports.log(f"  [ratchet] best-tree restore failed: {e}")
            return None

    def _tdd_floor_path(project_dir: Path, cluster: str) -> Path:
        return project_dir / ".skeleton" / f"_tdd_floor_{cluster}.json"

    def _load_tdd_floor(project_dir: Path, cluster: str) -> Any | None:
        """The loop floor a previous PROCESS left for this cluster, or None.

        Never raises: a corrupt or unreadable state file must cost a fresh
        stall streak, never the run."""
        p = _tdd_floor_path(project_dir, cluster)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_tdd_floor(project_dir: Path, cluster: str,
                        floor: dict[str, Any]) -> None:
        """Write the floor after every sweep. Atomic via a same-dir temp file:
        the process this exists to survive can die mid-write, and a truncated
        floor would be read back as a fresh start on the next boot — silently
        undoing the thing it is for."""
        p = _tdd_floor_path(project_dir, cluster)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(floor, indent=1), encoding="utf-8")
            tmp.replace(p)
        except OSError as e:
            ports.log(f"  [phase-c/tdd] could not persist the loop floor "
                      f"for {cluster}: {e}")

    ws_deps = WorkspaceDeps(log=ports.log, emit=ports.emit)
    ws_scan_deps = WorkspaceScanDeps(
        ask=ports.ask, task0_model=settings.task_models.task0,
        skip_dir_names=cfg.skip_dir_names, parse_cmd=_parse_cmd,
    )
    discover_test_cmd_deps = DiscoverTestCommandDeps(
        ask=ports.ask,
        task0_model=settings.task_models.task0,
        model_name=settings.model,
        extra_cap=cfg.extra_cap,
        sys_exit=sys.exit,
    )
    task0_docs_deps = Task0DocsDeps(
        phase_valid=phase_valid, mark_phase=mark_phase, ask=ports.ask, write=write,
        prompt_budget=prompt_budget, task0_model=settings.task_models.task0,
        model=settings.model, readme_cap=cfg.readme_cap, profile_cap=cfg.profile_cap,
        project_context_name=cfg.project_context_name,
    )
    phase_subcommand_deps = _PhaseSubcommandDeps(
        log=ports.log,
        bake_skeleton_artifacts=bake_skeleton_artifacts,
        collect_test_files=_collect_protected_test_files,
        group_tests=task2_group_tests,
        fill_groups=task2_fill_groups,
        cleanup_residuals=task2_cleanup_residuals,
        new_skeleton_generator=_new_skeleton_generator,
        manifest_name=_MANIFEST_NAME,
    )
    # The top-level run-loop deps (orchestrator.run_pipeline). Constants
    # come from settings/cfg/domain vocabulary; drivers bind DIRECT to the
    # pipeline_core impls; only logging/telemetry init + the audit thread stay
    # helpers-late-bound (they mutate pipeline-side run globals).
    def _start_run_stats(root: Path, project_dir: Path, crate: str) -> None:
        _ctx = run_context if run_context is not None else {}
        _start_run_stats_recorder(
            store_path=run_stats_path(events_log()), observe=_emitter.observe,
            root=root, project_dir=project_dir, crate=crate,
            run_id=_ctx.get("run_id", ""), commit=_ctx.get("pipeline_sha"),
            env=_os.environ, argv=sys.argv)
        if settings.backend is Backend.VLLM:
            # The model profile line (family, each calibrated knob and its
            # source) and the launch's conformance verdict as this run's
            # `model.conformance` event.
            _report_model_at_run_start(
                _os.environ, log=ports.log, emit=ports.emit,
                report_path=Path(events_log()).parent / "model_conformance.json")

    run_deps = RunDeps(
        WORKSPACE_DIR=_WORKSPACE_DIR,
        MANIFEST_NAME=_MANIFEST_NAME,
        MODEL=settings.model,
        COVERAGE_BACKEND=settings.coverage_backend,
        MODEL_CONTEXT_CHARS=settings.model_context_chars,
        SINGLE_CALL_CHAR_LIMIT=cfg.single_call_char_limit,
        FILE_CHAR_LIMIT=min(settings.safe_input_char_limit,
                            settings.model_context_chars * 85 // 100),
        INTERMEDIATE_CHAR_LIMIT=cfg.intermediate_char_limit,
        COVERAGE_AUGMENTATION_TARGET=helpers.coverage_target,
        TARPAULIN_BIN=cfg.tarpaulin_bin,
        FAILURE_LOG_NAME=_FAILURE_LOG_NAME,
        PROJECT_PROFILE_NAME=_PROJECT_PROFILE_NAME,
        PROJECT_CONTEXT_NAME=cfg.project_context_name,
        COVERAGE_STRATEGY_NAME=cfg.coverage_strategy_name,
        SETTINGS=settings,
        run_state=run_state,
        scope=scope,
        augment_store=augment_roles.store,
        cluster_augmentor=augment_roles.augmentor,
        strip_bake_deps=strip_bake_deps,
        task0d_deps=task0d_deps,
        ws_scan_deps=ws_scan_deps,
        phase_subcommand_deps=phase_subcommand_deps,
        requirements_phase=requirements_phase,
        module_reconstructor=module_reconstructor,
        log=ports.log,
        emit=ports.emit,
        setup_workspace=functools.partial(_ws_setup_workspace, ws_deps),
        is_git_repo=_ws_is_git_repo,
        init_log=helpers.init_log,
        init_events=helpers.init_events,
        start_audit_thread=auditor.start_thread,
        quarantine_broken_gen_tests=_gts_quarantine_broken_gen_tests,
        seed_auto_operator_default=_auto_op_seed_default,
        git_file_hash=_ws_git_file_hash,
        ensure_vllm_ready=_ensure_vllm_ready,
        git_clean_reset=functools.partial(_ws_git_clean_reset, ws_deps),
        clean_state=functools.partial(
            _clean_state_impl, delete_source_files=_wsf_delete_source_files),
        state_path=_state_path,
        restart_from_augmented=restore_from_augmented.run,
        restart_from_pre_augment=restore_from_pre_augment.run,
        git_tag=git_tag,
        list_git_tags=_ws_list_git_tags,
        git_checkpoint=git_checkpoint,
        oracle_drift=_workspace_oracle_drift,
        set_current=set_current,
        start_run_stats=_start_run_stats,
        construction_facts=construction_facts,
        source_hash=source_hash,
        log_state_summary=_state_log_state_summary,
        read=_io_read,
        task0_cfg_scan=task0_cfg_scan,
        load_baked_clusters=_load_baked_clusters,
        mark_phase=mark_phase,
        phase_valid=phase_valid,
        read_state=_state_read_state,
        discover_test_command=lambda readme_text, project_dir=None: (
            _discover_test_command_impl(readme_text, project_dir, discover_test_cmd_deps)),
        parse_cmd=_parse_cmd,
        collect_test_files=collect_test_files,
        collect_protected_test_files=_collect_protected_test_files,
        task0_project_profile=lambda path, test_files, readme: (
            _task0_profile_impl(path, test_files, readme, task0_docs_deps)),
        task0_project_context=lambda path, test_files, readme, profile: (
            _task0_context_impl(path, test_files, readme, profile, task0_docs_deps)),
        task0b_discovery=_via("task0b_discovery"),
        commit_and_mark=commit_and_mark,
        dependency_graph_factory=_dependency_graph_factory,
        load_locked_modules=_wsf_load_locked_modules,
        run_tarpaulin=functools.partial(_cov_run_tarpaulin, coverage_deps),
        strip_and_bake_module=_strip_and_bake_module,
        task1_static_audit=_task1_static_audit,
        scan_workspace_structure=lambda project_dir: (
            _workspace_scanner_factory(ws_scan_deps).scan(project_dir)),
        infer_build_check_command=_infer_build_check_command,
        wl_save_gen_tests_siblings=_wl_save_gen_tests_siblings,
        gen_tests_siblings=helpers.gen_tests_siblings,
        is_gen_tests_file=helpers.is_gen_tests_file,
        wl_wipe_module_sources=_wl_wipe_module_sources,
        write_workspace_stub=_ws_write_workspace_stub,
        collect_examples=_disc_collect_examples,
        task3=_via("task3"),
        scan_unimplemented_functions=scan_unimplemented_functions,
        reference_hollow=_reference_hollow,
        augmentation_fn_records=augment_store.all_fn_records,
        unreached_stub_proofs=_unreached_stub_proofs,
    )

    _crash_localizer_deps = CrashLocalizerDeps(
        gdb_localize=_gdb_localize_crashed_binary,
        asan_sweep=_asan_run_sweep,
        log=ports.log)

    def _localize_crashes(sweep_output: str, test_cmd: str,
                          workspace_root: Path, env: dict[str, str], /, *,
                          exclude: Collection[str] = ()) -> tuple[Any, ...]:
        """`env` is the home env the sweep ran under; a crash of another
        world is re-run under that world's own env, the one the sweep ran it
        under (`WorldChecks.env_of`, the same env `_failure_reader` reads a
        failing test in). With the every-world check off the capture holds
        the home world only."""
        def _world_env(world: str) -> dict[str, str] | None:
            if _worlds is None:
                return dict(env)
            return _worlds.env_of(world, workspace_root, env)
        project = project_dir_of_tree(workspace_root)
        cache: dict[str, frozenset[FnLocator] | None] = {}

        def executed(name: str) -> frozenset[FnLocator] | None:
            if name not in cache:
                cache[name] = _recorded_entry_sets_for(project, [name]).get(name)
            return cache[name]

        return _app_localize_crashes(
            _crash_localizer_deps, sweep_output, test_cmd, workspace_root,
            _world_env, exclude=exclude, executed=executed)

    # Every compile check of a cluster tree also builds the other worlds
    # (`cargo_runner.WorldChecks`). `PHASE_C_ALL_WORLDS_CHECK=0` checks the
    # home world only — the pre-2026-10-02 gate, kept as the negative control
    # of a reading.
    _worlds = (WorldChecks(
        clusters=lambda: list(scope.all),
        cluster_env=cluster_cargo_env,
        world_key=world_key, log=ports.log)
        if _os.getenv("PHASE_C_ALL_WORLDS_CHECK", "1") != "0" else None)

    def _failure_reader(world: str, tree: Path,
                        home_env: dict[str, str]) -> SignatureReader:
        """The TDD sweep's reader of how a failing test fails: run alone in
        `tree` under the env of the world it failed in (the same env the
        sweep ran that world under)."""
        env = (_worlds.env_of(world, tree, home_env) if _worlds is not None
               else None) or dict(home_env)

        def read(todo: Sequence[FailureToRead]) -> dict[tuple[str, str], FailureSignature | None]:
            return read_alone(todo, cwd=tree, env=env, timeout=cfg.test_timeout)
        return read

    _run_id = (run_context or {}).get("run_id") or ""
    return App(
        adapters=adapters,
        settings=settings,
        run_state=run_state,
        scope=scope,
        ports=ports,
        config=config,
        structure=structure,
        cargo_runner=PortBoundCargoRunner(
            adapters.cargo,
            worlds=_worlds),
        ask_client=ask_client,
        scan_stub_records_for_cluster=structure_cache.scan_unimplemented_records_for_cluster,
        collect_impl_blocks_for_cluster=structure_cache.collect_impl_blocks_for_cluster,
        group_fill_deps=GroupFillDeps(
            log=ports.log,
            ask=ask_client.ask,
            extract_fn_blocks=_extract_fn_blocks,
            format_skeleton_index_for_cluster=_format_skeleton_index_for_cluster,
            leaf_first_group_order=_leaf_first_group_order,
            load_skeleton_files=_load_skeleton_files,
            merge_fn_blocks_into_disk=_merge_fn_blocks_into_disk,
            scan_unimplemented_functions=scan_unimplemented_functions,
            single_call_char_limit=cfg.single_call_char_limit,
            genesis_cap=PromptBudgets.derive(cfg.single_call_char_limit).genesis,
            task2_model=settings.task_models.task2,
        ),
        phase_c_tdd_deps=PhaseCTddDeps(
            attribute_failures=_via("attribute_failures"),
            build_test_reverse_map=build_test_reverse_map,
            filter_test_targets=filter_test_targets,
            check_graceful_stop=check_graceful_stop,
            check_auto_operator=check_auto_operator,
            converge_in_cluster=helpers.converge_in_cluster,
            count_relevant_notes=helpers.count_relevant_notes,
            effective_stalls=helpers.effective_stalls,
            emit=ports.emit,
            log=ports.log,
            order_frontier=_order_frontier,
            record_tdd_regression=record_tdd_regression,
            reset_exhaustion_flags=_reset_exhaustion_flags,
            retire_unblocked_convictions=_retire_unblocked_convictions,
            run_cargo=helpers.run_cargo,
            scan_unimplemented_for_cluster=structure_cache.scan_unimplemented_for_cluster,
            scan_unimplemented_functions=scan_unimplemented_functions,
            task2_fill_fns_one_by_one=helpers.task2_fill_fns_one_by_one,
            escalate_to_producers=functools.partial(
                escalate_frontier_to_producers, _attr_deps),
            suspend_futile_guidance=lambda pd, sd, units, cl, tests:
                _suspend_futile_guidance(
                    pd, _futile_suspension_keys(
                        _fn_occurrence_index_impl(
                            sd, enumerate_fns=enumerate_fns), units),
                    cluster=cl, tests=tests),
            release_escalated_convictions=lambda pd, sd, targets, reason:
                _release_escalated_convictions(
                    pd, _escalation_release_keys(
                        _fn_occurrence_index_impl(
                            sd, enumerate_fns=enumerate_fns), targets),
                    reason=reason),
            cfg_dead_sites=structure_cache.cluster_dead_sites,
            checkpoint_fill_tree=_checkpoint_fill_tree,
            load_best_tree_mark=load_cluster_best_tree_mark,
            restore_best_tree_pair=_restore_best_tree_pair,
            load_tdd_floor=_load_tdd_floor,
            save_tdd_floor=_save_tdd_floor,
            retrigger_layout=retrigger_layout,
            active_test_inventory=active_test_inventory_for_cluster,
            test_file_index=_test_file_index,
            assert_probe=_assert_probe_for_tdd,
            localize_crashes=_localize_crashes,
            migrate_retro_evidence=_migrate_confounded_retros,
            migrate_retro_gate=_migrate_absolute_gate_retros,
            implicated_fns=lambda out, tsd: _implicated_fns_from_cargo_impl(
                out, tsd, enumerate_fns=enumerate_fns),
            enumerate_fns=enumerate_fns,
            enumerate_types=enumerate_types,
            # --- the proven-body ledger. The store lives OUTSIDE the
            # workspace (`logs/body_ledger/<project>/`) because every
            # workspace-level destruction can reach inside it: the augmented
            # restart's `git reset --hard` deleted 5,137 tracked files on
            # 2026-08-08, and `.phase_c_logs` — where the retro store lives —
            # is tracked.
            ledger_load=lambda pd, cl: _body_ledger_load(
                _body_ledger_root(), pd.name, cluster=cl),
            ledger_append=lambda pd, rows: _body_ledger_append(
                _body_ledger_root(), pd.name,
                [replace(r, run_id=r.run_id or _run_id) for r in rows]),
            ledger_size=lambda pd: _body_ledger_size(
                _body_ledger_root(), pd.name),
            layout_items=lambda pd: {
                name: (item.kind, item.definition)
                for name, item in load_layout_registry(pd).items()},
            tree_writer=TreeWriter(
                splice=splice_bodies,
                signatures=current_signatures, bodies=current_bodies),
            check_tree_builds=lambda pd, cl: compile_filled_check(
                pd, cl, worlds=_load_baked_clusters(pd) or []),
            parse_ok=_validate_parse,
            run_id=_run_id,
            # The oracle scoring the cluster tree, `(src_dir, project_dir,
            # cluster) -> fingerprint`: the tree's gen_tests siblings, the
            # crate's `tests/` and the baked behaviour suite. The driver
            # caches it per iteration (`_oracle_fp` in `phase_c_tdd.py`);
            # this is the uncached adapter.
            oracle_fp=lambda sd, pd, cl: _cluster_oracle_fp(sd, pd, cl),
            measure_coverage_slice=_tdd_measure_coverage(ports.log, enumerate_fns, _worlds),
            recorded_entry_sets=_recorded_entry_sets_for,
            # The `retro` channel of the evidence premise: what the store
            # holds about each unit, which is both the guidance a prompt may
            # inject and the cross-fill LAST_ERROR the pair route reads off
            # the same record.
            retro_evidence_texts=_retro_evidence_texts,
            # The same rows the fill schedule ranks on, read at dispatch for
            # one question: has the hop pair covering a leftover been bought
            # and returned nothing under this oracle? Without it the
            # measured-owners withholding a pair earns never lapses.
            route_ledger_rows=fill_roles.route_ledger.load,
            reference_measurement=_reference,
            failure_reader=_failure_reader,
            augmentation_fn_record=augment_store.cluster_fn_record,
            # The baked suite, for the tiers a red test owes; and the ask
            # ledger, so a resumed process does not re-owe what the last one
            # already asked.
            behaviour_specs_for=lambda pd, cl: load_fill_specs(pd, cl, enumerate_fns),
            load_ask_ledger=_load_ask_ledger,
            save_ask_ledger=_save_ask_ledger,
            # The red ledger every cluster end writes; task 3 prints it and
            # task 4's per-test pass reads it.
            save_red_ledger=_save_red_ledger,
            last_compile_errors=_last_compile_errors,
            load_lessons=_load_lessons,
        ),
        impl_fill_deps=ImplFillDeps(
            build_dispatch_family_section=build_dispatch_family_section,
            build_per_impl_prompt=_build_per_impl_prompt,
            cargo_env=ports.cargo_env,
            cargo_err_sig=_cargo_err_sig,
            cargo_err_sig_from_diagnostics=_cargo_err_sig_from_diagnostics,
            check_graceful_stop=check_graceful_stop,
            cluster_filter_type_members=_world_type_members,
            all_clusters=lambda: list(scope.all),
            collect_crate_macros=collect_crate_macros,
            collect_impl_blocks_for_cluster=collect_impl_blocks_via_app,
            collect_sibling_filled_bodies=_collect_sibling_filled_bodies,
            compute_cluster_macro_invariants=_compute_cluster_macro_invariants,
            detect_crate_no_std_invariant=_detect_crate_no_std_invariant,
            dispatch_families_via_extractor=enumerate_dispatch_families,
            effective_stalls=helpers.effective_stalls,
            emit=ports.emit,
            enumerate_fns_via_extractor=enumerate_fns,
            enumerate_types_via_extractor=enumerate_types,
            extract_crate_idioms=_extract_crate_idioms,
            extract_impl_where_clause=_impl_where_clause,
            filter_model_notes_for_prompt=model_notes_store.filter_for_prompt,
            retire_contradicted_notes=model_notes_store.retire_contradicted,
            find_tests_referencing_fn=_find_tests_referencing_fn,
            focus_cargo_errors=focus_cargo_errors,
            frontier_from_diags=_via("frontier_from_diags"),
            implicated_keys_from_diags=_via("implicated_keys_from_diags"),
            impl_block_id=_impl_block_id,
            is_fn_fully_exhausted=_retros_is_exhausted,
            load_retrospectives=_retros_load,
            log=ports.log,
            module_of=_module_of,
            phase_c_retrospective=_via("phase_c_retrospective"),
            record_error_codes_seen=model_notes_store.record_error_codes_seen,
            retro_key=_retros_key,
            run_cargo_check_diag=helpers.run_cargo_check_diag,
            save_retrospectives=_retros_save,
            scan_unimplemented_for_cluster=structure_cache.scan_unimplemented_for_cluster,
            scan_unimplemented_records_for_cluster=scan_stub_records_via_app,
            cluster_dead_sites=structure_cache.cluster_dead_sites,
            splice_fn_with_where=splice_fn_with_where,
            splice_lock_for=_splice_mod.splice_lock_for,
            parse_ok=_validate_parse,
            types_section_for_fix=types_section_for_fix,
            types_section_for_files=types_section_for_files,
            bound_methods_section_for=bound_methods_section_for,
            u2_test_gate=_via("u2_test_gate"),
            test_regression_baseline=helpers.test_regression_baseline,
            test_regression_gate=helpers.test_regression_gate,
            ask_turns=ask_client.ask_turns,
            fit_turn=ask_client.fit,
            run_cargo=helpers.run_cargo,
            measure_coverage_slice=_tdd_measure_coverage(ports.log, enumerate_fns, _worlds),
            failed_body_cap=cfg.failed_body_cap,
            per_fn_fix_hard_cap=settings.fix_caps.per_fn_fix_hard_cap,
            per_impl_max_stubs=helpers.per_impl_max_stubs,
            task2_model=settings.task_models.task2,
            single_call_char_limit=cfg.single_call_char_limit,
        ),
        phase_subcommand_deps=phase_subcommand_deps,
        task0c_deps=Task0cDeps(
            BUILD_TIMEOUT=cfg.build_timeout,
            COVERAGE_TARGET=helpers.coverage_target,
            BEHAVIOUR_COVERAGE_TARGET=settings.behaviour_coverage_target,
            BEHAVIOUR_CAPTURE_SPLIT=settings.behaviour_capture_split,
            load_behaviour_suite=load_behaviour_suite,
            TASK0C_PROMPT_CHAR_CAP=helpers.task0c_prompt_char_cap,
            FIX_ERR_CAP=cfg.fix_err_cap,
            SIBLING_CAP=PromptBudgets.derive(cfg.single_call_char_limit).sibling,
            SRC_SNIP_CAP=PromptBudgets.derive(cfg.single_call_char_limit).src_snippet,
            TASK0C_FIX_THINKING_DIRECTIVE=helpers.task0c_fix_thinking_directive,
            TASK0C_MODEL=settings.task_models.task0c,
            TASK0C_THINKING_DIRECTIVE=helpers.task0c_thinking_directive,
            WORKING_CAP=PromptBudgets.derive(cfg.single_call_char_limit).working,
            annotate_uncovered=_cov_annotate_uncovered,
            block_is_prose=_text_block_is_prose,
            build_gen_tests_decl=_gen_build_gen_tests_decl,
            guard=ports.guard,
            cargo_env=ports.cargo_env,
            world_gate=cluster_cfg_attr,
            settle_test_gate=_settle_test_gate,
            condense_rust_to_signatures=_condense_rust_to_signatures,
            count_test_attrs=_scan_count_test_attrs,
            derive_gentest_prelude=_gts_derive_gentest_prelude,
            describe_test_environment=helpers.describe_test_environment,
            error_signatures_from_output=_rustc_error_signatures,
            extract_branch_guard=_cov_extract_branch_guard,
            extract_gen_tests_body=_gen_extract_gen_tests_body,
            extract_public_symbols=public_api_from_source,
            extract_uncovered_functions=_cov_extract_uncovered_functions,
            filter_cfg_excluded_lines=_cov_filter_cfg_excluded_lines,
            filter_cfg_incompatible_lines=_cov_filter_cfg_incompat,
            flags_unreachable=_text_flags_unreachable,
            gen_tests_path=_gts_gen_tests_path,
            has_nonempty_test_body=_scan_has_nonempty_test_fn,
            inject_allow_before_any_mod=functools.partial(
                _cov_inject_allow_before_mod, gen_only=False),
            inject_allow_before_gen_mod=functools.partial(
                _cov_inject_allow_before_mod, gen_only=True),
            inject_gen_prelude_imports=_gen_inject_gen_prelude_imports,
            is_gen_tests_file=_disc_is_gen_tests_file,
            is_structural_line=_scan_is_structural_brace_line,
            load_file_constraints=file_constraints.load,
            type_signals_write=_db_type_signals_write,
            mark_phase=_state_mark_phase,
            match_gen_tests_sibling=_disc_match_gen_tests_sibling,
            next_gen_tests_n=_gts_next_gen_tests_n,
            nightly_rules_for_cluster=lambda: nightly_rules_for(scope.active),
            parse_attempt_number=_text_parse_attempt_number,
            parse_retrospective_categories=_parse_retro_categories,
            partition_uncovered_lines=_cov_partition_uncovered_lines,
            prelude_import_names=_gen_prelude_import_names,
            quick_file_coverage=quick_file_coverage,
            read_all_gen_tests=_gts_read_all_gen_tests,
            read_state=_state_read_state,
            revert_gen_tests_pass=_gts_revert_gen_tests_pass,
            select_relevant_siblings=_t0c_select_relevant_siblings,
            set_current=set_current,
            source_hash=source_hash,
            strip_all_gen_tests_artifacts=_gts_strip_all_artifacts,
            strip_gen_tests_decl_n=_gen_strip_decl_n,
            strip_gen_tests_decls=_gen_strip_all_decls,
            strip_trailing_file_tag=_text_strip_trailing_file_tag,
            mutation_harden_file=mutation.harden_file,
            gen_tests_siblings=_gts_gen_tests_siblings,
            test_augmentor_factory=test_augmentor_factory,
            ask=ask_client.ask,
            extract_files=_extract_files,
            extract_test_mod=_gen_extract_test_mod,
            generate_in_parallel=generate_in_parallel,
            is_test_file=_disc_is_test_file,
            write=_io_write,
            subprocess=_subprocess_mod,
            get_current_cluster=scope.get_active,
            coverage_provider=coverage_provider,
            check_stop=check_graceful_stop,
            # The function-execution criterion: the same extractor seams the
            # fill routes key their records with, over the pre-strip crate,
            # and the store that persists the per-cluster record beside the
            # augmentation sentinel.
            enumerate_fns=enumerate_fns,
            enumerate_mods=enumerate_mods,
            enumerate_types=enumerate_types,
            load_reach=load_reach,
            load_fn_record=augment_store.cluster_fn_record,
            save_fn_record=augment_store.save_cluster_fn_record,
            other_fn_records=augment_store.other_cluster_fn_records,
            COVERAGE_FILE_FLOOR=settings.coverage_file_floor,
            load_file_floor=augment_store.cluster_file_floor,
            save_file_floor=augment_store.save_cluster_file_floor,
            other_file_floors=augment_store.other_cluster_file_floors,
            # The functions no test can execute are proved from rustc's own
            # MIR and public index for the cluster's world, not asked of a model.
            measure_reachability=functools.partial(
                measure_world_reachability, cluster_env=cluster_cargo_env,
                timeout=cfg.test_timeout),
        ),
        generate_fix_loop=generate_fix_loop,
        gen_fix_loop_deps=GenerateFixLoopDeps(
            set_current=set_current,
            BUILD_TIMEOUT=cfg.build_timeout,
            FILE_FORMAT_INSTRUCTION=FILE_FORMAT_INSTRUCTION,
            MODEL=settings.model,
            settings_now=helpers.settings_now,
            SINGLE_CALL_CHAR_LIMIT=cfg.single_call_char_limit,
            SOURCE_EXTENSIONS=helpers.source_extensions,
            ASK_CLIENT=ask_client,
            FIX_ERR_CAP=cfg.fix_err_cap,
            FIX_THINKING_DIRECTIVE=helpers.fix_thinking_directive,
            REPO_SPEC_CAP_WIDE=PromptBudgets.derive(cfg.single_call_char_limit).repo_spec_wide,
            SRC_SNIP_CAP=PromptBudgets.derive(cfg.single_call_char_limit).src_snippet,
            TASK2_VERBOSITY_GUARD=helpers.task2_verbosity_guard,
            TYPES_SECTION_CAP=PromptBudgets.derive(cfg.single_call_char_limit).types_section,
            behavior_frontier=_via("behavior_frontier"),
            cargo_env=ports.cargo_env,
            categorize_compile_error=_categorize_compile_error,
            collect_impl_blocks=_via("collect_impl_blocks"),
            compile_error_frontier=_via("compile_error_frontier"),
            derive_check_only_cmd=_domain_derive_check_only_cmd,
            emit=ports.emit,
            emit_per_file_test_overview=_emit_per_file_test_overview,
            enumerate_fns_via_extractor=enumerate_fns,
            enumerate_types=enumerate_types,
            types_section_for_fix=types_section_for_fix,
            check_stop=check_graceful_stop,
            extract_tdd_frontier=_via("extract_tdd_frontier"),
            filter_test_targets=filter_test_targets,
            repair_cluster=lambda: merged_view_cluster(scope.all),
            fix_diag=helpers.fix_diag,
            frontier_from_diags=_via("frontier_from_diags"),
            gotchas_for_error=lambda e: _language_driver().gotchas_for_error(e),
            gotchas_summary=lambda: _language_driver().gotchas_summary(),
            is_pub_fn=_domain_is_pub_fn,
            locate_fn=_domain_locate_fn,
            log=ports.log,
            parse_cmd=_parse_cmd,
            render_impl_block=_render_impl_block,
            repo_spec_section=repo_spec_section,
            restore_frozen_signature=restore_frozen_signature,
            run_cargo_check_diag=helpers.run_cargo_check_diag,
            splice_fn_body=splice_fn_body,
            splice_body_scoped=fill_roles.mut.splice_body_scoped,
            splice_fn_with_where=splice_fn_with_where,
            strip_ansi=_strip_ansi,
            tag_block=_tag_block,
            whole_tree_retrospective=whole_tree_retrospective,
            build_whole_file_fix_prompt=_build_wff,
            compile_patterns_write=_db_compile_patterns_write,
            extract_files=_extract_files,
            is_compiler_output_line=_is_compiler_output_line,
            run_generate_fix=_run_generate_fix,
            write=_io_write,
            # Resolved per cluster. The raw loader leaves the identity
            # subject as the text harvest, and this loop's instruction is
            # built from the union of what this dep returns.
            load_behaviour_suite=lambda pd, name: fill_suite_for_prompt(
                pd, name, enumerate_fns),
        ),
        attribution_deps=_attr_deps,
        task1_deps=Task1Deps(
            load_api_surface=_load_api_surface,
            load_type_names=_load_type_names,
            read=_io_read,
            ask=ask_client.ask,
            write_fr_preserving_notes=_write_fr_preserving_notes,
            synthesizer_factory=_task1_synthesizer_factory,
            filter_proc_macro_test_files=_filter_proc_macro_test_files,
            proc_macro_fallback_requirements=_pm_fallback_reqs,
            format_api_surface_section=format_api_surface_section,
            file_format_instruction=FILE_FORMAT_INSTRUCTION,
            single_call_char_limit=cfg.single_call_char_limit,
            profile_cap=cfg.profile_cap,
            task1_model=settings.task_models.task1,
        ),
        emitter=_emitter,
        ask_per_fn_body=helpers.ask_per_fn_body,
        # Seam fields bound DIRECT to the pipeline_core impls (consumers
        # go through _via(...) so a test's replace on the live App wins).
        attribute_failures=functools.partial(_attribution.attribute_failures, _attr_deps),
        behavior_frontier=functools.partial(_attribution.behavior_frontier, _attr_deps),
        classify_phase_c_test_failures=functools.partial(
            _classify_failures_impl, ask=ask_client.ask, log=ports.log,
            model=settings.task_models.task0c, err_cap=cfg.fix_err_cap),
        collect_impl_blocks=functools.partial(
            _collect_impl_blocks_impl, enumerate_fns=enumerate_fns),
        compile_error_frontier=functools.partial(
            _attribution.compile_error_frontier, _attr_deps),
        extract_tdd_frontier=functools.partial(_attribution.extract_tdd_frontier, _attr_deps),
        fn_container_index=functools.partial(
            _fn_container_index_impl, enumerate_fns=enumerate_fns),
        frontier_from_diags=functools.partial(_attribution.frontier_from_diags, _attr_deps),
        implicated_keys_from_diags=functools.partial(
            _attribution.implicated_fn_keys, _attr_deps),
        u2_test_gate=u2_test_gate,
        fill_roles=fill_roles,
        model_notes_store=model_notes_store,
        cleanup_residuals_deps=cleanup_residuals_deps,
        api_surface=api_surface,
        restore_from_augmented=restore_from_augmented,
        restore_from_pre_augment=restore_from_pre_augment,
        strip_bake_deps=strip_bake_deps,
        bake_artifacts_deps=bake_artifacts_deps,
        structure_cache=structure_cache,
        fix_context=fix_context,
        augment_roles=augment_roles,
        genesis_audit_deps=GenesisAuditDeps(
            ask=ports.ask, read=read, write=write,
            model=settings.task_models.task4,
            verbosity_guard=cfg.task4_verbosity_guard,
        ),
        discover_test_cmd_deps=discover_test_cmd_deps,
        coverage_deps=coverage_deps,
        coverage_provider=coverage_provider,
        ws_deps=ws_deps,
        ws_scan_deps=ws_scan_deps,
        task4_deps=Task4Deps(
            ask=ports.ask, read=read, write=write,
            extract_and_store_rules=extract_and_store_rules,
            model=settings.model, task4_model=settings.task_models.task4,
            verbosity_guard=cfg.task4_verbosity_guard,
            intermediate_char_limit=cfg.intermediate_char_limit,
            single_call_char_limit=cfg.single_call_char_limit,
        ),
        task0_docs_deps=task0_docs_deps,
        phase_c_retro_deps=_retro_deps,
        phase_c_retrospective=functools.partial(_diagnose_phase_c, deps=_retro_deps),
        task0b_discovery=functools.partial(_task0b_impl, deps=_task0b_deps),
        task3=_task3,
        validator_deps=Task2CheckerDeps(
            enumerate_types=enumerate_types, enumerate_fns=enumerate_fns,
            load_pub_items_manifest=load_pub_items_manifest,
        ),
        telemetry=telemetry,
        requirements_phase=requirements_phase,
        module_reconstructor=module_reconstructor,
        run_deps=run_deps,
    )
