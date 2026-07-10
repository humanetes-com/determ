"""build_app — the composition factory (Phase 3).

The composition root, parameterized over its external ports. `main()` builds the
real :class:`Adapters` and calls :func:`build_app`; tests call it with fake
adapters so they inject at the port boundary (LLM / cargo / syn-extractor)
through the constructor instead of monkeypatching `pipeline._x` globals.

`App` is grown stage by stage: it currently holds the injected ports + run-scoped
state + the boundary providers derived from them. Later stages move the
`_X_DEPS` use-case wiring and the `main` run-loop here (see refactoring.md §Phase 3)."""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.adapters import Adapters
from pipeline_core.adapters.artifacts import WorkspaceArtifacts, load_pub_items_manifest
from pipeline_core.adapters.artifacts import (
    load_api_surface as _load_api_surface,
)
from pipeline_core.adapters.artifacts import (
    load_type_names as _load_type_names,
)
from pipeline_core.adapters.ask_client import AskClient
from pipeline_core.adapters.augment_engine import CargoAugmentEngine
from pipeline_core.adapters.augment_store import FileAugmentStore
from pipeline_core.adapters.coverage import CoverageDeps, CoverageProvider
from pipeline_core.adapters.failure_journal import FileFailureJournal
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
from pipeline_core.adapters.retros import (
    is_fn_fully_exhausted as _retros_is_exhausted,
)
from pipeline_core.adapters.retros import (
    load_retrospectives as _retros_load,
)
from pipeline_core.adapters.retros import (
    retro_key as _retros_key,
)
from pipeline_core.adapters.retros import (
    save_retrospectives as _retros_save,
)
from pipeline_core.adapters.state import mark_phase, phase_valid, set_current
from pipeline_core.adapters.structure import CliStructureReader  # noqa: F401 (re-export for composition callers)
from pipeline_core.adapters.telemetry import JsonlEventEmitter, LogTelemetry
from pipeline_core.adapters.test_runner import CallableTestRunner
from pipeline_core.adapters.workspace import WorkspaceDeps
from pipeline_core.adapters.workspace_files import lock_module
from pipeline_core.adapters.workspace_scan import WorkspaceScanDeps
from pipeline_core.application.cluster_augment import ClusterAugmentor
from pipeline_core.application.gen_tests_reattach import ReattachDeps
from pipeline_core.application.orchestrator import LockDeps
from pipeline_core.application.reconstruct.cargo_runner import PortBoundCargoRunner
from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.group_fill import GroupFillDeps
from pipeline_core.application.reconstruct.impl_fill import ImplFillDeps
from pipeline_core.application.reconstruct.module_reconstructor import ModuleReconstructor
from pipeline_core.application.reconstruct.per_fn_retry import (
    RetryDeps,
    Task3Attribution,
    Task3Cargo,
    Task3Enumerate,
    Task3RetryDeps,
)
from pipeline_core.application.reconstruct.phase_c_tdd import PhaseCTddDeps
from pipeline_core.application.reconstruct.reconstruction_step import ReconstructionStep
from pipeline_core.application.reconstruct.requirements_phase import RequirementsPhase
from pipeline_core.application.reconstruct.retrospective import PhaseCRetroDeps
from pipeline_core.application.tasks.discover_test_command import DiscoverTestCommandDeps
from pipeline_core.application.tasks.genesis_audit import GenesisAuditDeps
from pipeline_core.application.tasks.task0_docs import Task0DocsDeps
from pipeline_core.application.tasks.task1 import Task1Deps
from pipeline_core.application.tasks.task4 import Task4Deps
from pipeline_core.application.validate import Task2CheckerDeps
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.impl_routing import impl_block_id as _impl_block_id
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.prompt_budget import PromptBudgets
from pipeline_core.domain.run_state import RunState
from pipeline_core.domain.rust_macros import (
    proc_macro_fallback_requirements as _pm_fallback_reqs,
)
from pipeline_core.io import read, write
from pipeline_core.io import read as _io_read
from pipeline_core.ports.cargo_runner import CargoRunner as CargoRunnerPort
from pipeline_core.ports.structure import StructureReader
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt
from pipeline_core.settings import Settings


@dataclass(frozen=True)
class Ports:
    """Late-binding callables for the cross-cutting boundaries (LLM ask, log,
    event emit, truncation guard, cargo env). During migration the composition
    root supplies lambdas over its module globals so the e2e test seam (which
    patches those globals) is preserved; at the end they become bound adapter
    methods. See the late-binding bridge in refactoring.md §Phase 3."""

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
    stall_refill_iters: int
    test_timeout: int


@dataclass(frozen=True)
class ReconstructRoles:
    """Late-binding callables for the TASK-1→4 reconstruction roles — the resident
    composition-root task/validate fns the ISP role adapters (`PhaseGateAdapter`…
    `AnalystAdapter`) wrap. Supplied as lambdas over pipeline globals so build_app
    can construct the adapters at module-load time even though the wrapped fns are
    defined later in pipeline.py, and the test seam stays live. See refactoring.md
    §Phase 3 late-binding bridge."""

    phase_valid: Callable[..., bool]
    commit_and_mark: Callable[..., None]
    task1: Callable[..., bool]
    validate_task1_output: Callable[..., tuple[bool, str]]
    task2_inner_loop: Callable[..., bool]
    load_original_structure: Callable[..., list[str]]
    validate_task2_output: Callable[..., tuple[bool, str]]
    validate_task2_api_conformance: Callable[..., tuple[bool, str]]
    task2_fix_loop: Callable[..., tuple[bool, str]]
    classify_failure: Callable[[str], str]
    insert_no_run: Callable[[str], str]
    fix_err_cap: int
    task4: Callable[..., tuple[bool, int]]
    validate_task4_output: Callable[..., tuple[bool, str]]
    task_genesis_audit: Callable[..., bool]


@dataclass(frozen=True)
class Helpers:
    """Late-binding callables for resident pipeline helpers (still defined in
    pipeline.py) that the migrated `_X_DEPS` blocks need. Lambdas over the module
    globals → preserve the test seam and let build_app wire them before they're
    defined. Dissolve a field once its helper relocates into pipeline_core."""

    parse_cmd: Callable[[str], list[str]]
    prompt_budget: Callable[..., int]
    ask_retrospective: Callable[..., str]
    # The per-fn conversation protocol (prompt → body/pull-request) — the
    # PromptContext role's model seam, routed through App (P3-S2 llm boundary).
    ask_per_fn_body: Callable[..., tuple[str | None, str]]
    # Group-fill leaf collaborators (P3-S3, GroupFillDeps construction):
    extract_fn_blocks: Callable[..., Any]
    format_skeleton_index_for_cluster: Callable[..., Any]
    leaf_first_group_order: Callable[..., Any]
    load_skeleton_files: Callable[..., Any]
    merge_fn_blocks_into_disk: Callable[..., Any]
    # Phase-C TDD driver collaborators (P3-S3, PhaseCTddDeps construction;
    # attribute_failures/build_test_reverse_map/filter_test_targets pre-exist
    # below in the S27 group):
    check_graceful_stop: Callable[..., Any]
    scan_stub_records_for_cluster: Callable[..., Any]
    collect_impl_blocks_for_cluster: Callable[..., Any]
    # Same seams read through the LIVE pipeline._APP (swap-aware: tests
    # replace _APP; composition must not import pipeline, so the module
    # provides these App-reading closures itself):
    scan_stub_records_via_app: Callable[..., Any]
    collect_impl_blocks_via_app: Callable[..., Any]
    # Task-1 collaborators (P3-S3, Task1Deps construction):
    write_fr_preserving_notes: Callable[..., Any]
    task1_synthesizer_factory: Callable[..., Any]
    filter_proc_macro_test_files: Callable[..., Any]
    format_api_surface_section: Callable[..., Any]
    file_format_instruction: str
    # Impl-fill collaborators (P3-S3, ImplFillDeps construction):
    build_dispatch_family_section: Callable[..., Any]
    build_per_impl_prompt: Callable[..., Any]
    cargo_err_sig: Callable[..., Any]
    cargo_err_sig_from_diagnostics: Callable[..., Any]
    cluster_filter_type_methods: Callable[..., Any]
    collect_crate_macros: Callable[..., Any]
    collect_sibling_filled_bodies: Callable[..., Any]
    compute_cluster_macro_invariants: Callable[..., Any]
    detect_crate_no_std_invariant: Callable[..., Any]
    dispatch_families: Callable[..., Any]
    extract_crate_idioms: Callable[..., Any]
    extract_impl_where_clause: Callable[..., Any]
    filter_model_notes_for_prompt: Callable[..., Any]
    find_tests_referencing_fn: Callable[..., Any]
    focus_cargo_errors: Callable[..., Any]
    module_of: Callable[..., Any]
    phase_c_retrospective: Callable[..., Any]
    record_error_codes_seen: Callable[..., Any]
    splice_fn_with_where: Callable[..., Any]
    splice_lock_for: Callable[..., Any]
    types_section_for_fix: Callable[..., Any]
    bound_methods_section_for: Callable[..., Any]
    u2_test_gate: Callable[..., Any]
    test_regression_baseline: Callable[..., Any]
    test_regression_gate: Callable[..., Any]
    per_impl_max_stubs: int
    converge_in_cluster: Callable[..., Any]
    count_relevant_notes: Callable[..., Any]
    effective_stalls: Callable[..., Any]
    order_frontier: Callable[..., Any]
    record_tdd_regression: Callable[..., Any]
    reset_exhaustion_flags: Callable[..., Any]
    run_cargo: Callable[..., Any]
    scan_unimplemented_for_cluster: Callable[..., Any]
    task2_fill_fns_one_by_one: Callable[..., Any]
    extract_and_store_rules: Callable[..., bool]
    enumerate_types: Callable[[Path], list[TypeRecord] | None]
    enumerate_fns: Callable[[Path], list[dict[str, object]] | None]
    # Reconstruction-cluster collaborators (S3 slices 11-20):
    git_commit: Callable[..., bool]
    task3: Callable[..., tuple[bool, str]]
    task0b_discovery: Callable[..., str]
    run_flow_audit: Callable[[str], None]
    load_model_notes: Callable[..., list[str]]
    genesis_rules_count: Callable[..., int]
    derive_gentest_prelude: Callable[..., tuple[str, str]]
    is_gen_tests_file: Callable[[str], bool]
    gen_tests_siblings: Callable[[Path], list[tuple[int, Path]]]
    load_gen_tests_origin: Callable[[Path], dict[str, str]]
    gen_tests_path: Callable[[Path, int], Path]
    build_gen_tests_decl: Callable[..., str]
    cluster_cfg_attr: Callable[..., str]
    has_gen_tests_decl: Callable[..., bool]
    # task3 inline-retry collaborators (S27) — resident shims over pipeline-side
    # bound deps (_ATTR_DEPS, ADAPTERS.cargo); the first three are e2e seams.
    run_cargo_check_diag: Callable[..., tuple[int, str, list[Any]]]
    attribute_failures: Callable[..., Any]
    frontier_from_diags: Callable[..., Any]
    build_test_reverse_map: Callable[..., Any]
    filter_test_targets: Callable[..., Any]
    scan_unimplemented_functions: Callable[..., Any]


@dataclass(frozen=True)
class FillRoles:
    """The 9 ISP fill-engine role adapters (`adapters.fill_engine_roles`).
    Constructed pipeline-side from late-binding lambdas over the resident engine
    helpers (the scripted-cargo/scripted-model TEST SEAM — these are genuine
    boundaries, so the lambdas stay) and injected into `App` so the per-call
    engine factory (`_build_phase_c_engine`) + the eventual `App.run` source them
    from one place. Same inject-pre-built pattern as `Adapters`/`Ports`."""

    tel: EngineTelemetryAdapter
    mut: MutatorAdapter
    cargo: CargoFillAdapter
    prompt: PromptContextAdapter
    ws: WorkspaceModelAdapter
    retros: RetrosAdapter
    tests: TestLinkerAdapter
    stall: StallPolicyAdapter
    impl: ImplFillPathAdapter


@dataclass(frozen=True)
class AugmentRoles:
    """TASK-0c augmentation cluster (store + cargo engine + the ClusterAugmentor
    use-case). Constructed pipeline-side (resident collaborators: `collect_test_files`,
    `task0c_augment_module`, the git-bound commit-gate) and injected into `App` so
    the eventual `App.run` augment loop sources them from one place (S25)."""

    store: FileAugmentStore
    engine: CargoAugmentEngine
    augmentor: ClusterAugmentor


@dataclass(frozen=True)
class App:
    """The wired application: external ports + run-scoped state + boundary
    providers and use-case deps built from them. Grown one S3 slice at a time
    (refactoring.md §Phase 3) as `_X_DEPS` blocks migrate out of pipeline.py."""

    adapters: Adapters
    settings: Settings
    run_state: RunState
    scope: ClusterScope
    ports: Ports
    config: CompositionConfig
    # Cluster-scoped structure queries (P3-S3): the WorkspaceModel role's
    # derived scans — stub records / impl blocks filtered to a cluster's
    # cfg-set. Explicit App seams so tests script them at the boundary
    # instead of monkeypatching pipeline module fns.
    scan_stub_records_for_cluster: Callable[..., Any]
    collect_impl_blocks_for_cluster: Callable[..., Any]

    # Group-fill deps (P3-S3): constructed here from helpers + ask_client.
    group_fill_deps: GroupFillDeps
    # Phase-C TDD driver deps (P3-S3).
    phase_c_tdd_deps: PhaseCTddDeps
    # Impl-fill deps (P3-S3).
    impl_fill_deps: ImplFillDeps
    # Task-1 deps (P3-S3).
    task1_deps: Task1Deps

    # Guarded LLM ask client (P3-S3 bridge): constructed by the composition
    # root (its ask_raw/hot-reload lambdas close over pipeline residents) and
    # HELD here so use-case deps bind `app.ask_client.ask` and tests swap it.
    ask_client: AskClient

    # Telemetry boundary (P3-S2/S3): the structured-event sink, constructed
    # HERE from the late-bound events-log path + live run-context dict the
    # composition root owns. `pipeline._emit`/`_phase` delegate to it.
    emitter: JsonlEventEmitter

    # LLM per-fn boundary (P3-S2): the engines' PromptContext role resolves its
    # model seam here at call time — swap via dataclasses.replace to script it.
    ask_per_fn_body: Callable[..., tuple[str | None, str]]

    # Cargo boundary (P3-S2): the application-facing runner over `adapters.cargo`
    # — the semantic (rc, text) / (rc, text, diags) level engines and scripted
    # tests work at. Swap via dataclasses.replace to script it.
    cargo_runner: CargoRunnerPort

    # Syn-extractor boundary: the structured-source reader over `adapters.extractor`.
    # Typed at the PORT so tests swap in a fake via dataclasses.replace (P3-S2).
    structure: StructureReader
    # Fill-engine ISP role adapters (S21) — see FillRoles.
    fill_roles: FillRoles
    # TASK-0c augmentation cluster (S25) — see AugmentRoles.
    augment_roles: AugmentRoles
    # Migrated use-case deps:
    genesis_audit_deps: GenesisAuditDeps       # TASK-4 genesis audit (S3 slice 1)
    discover_test_cmd_deps: DiscoverTestCommandDeps  # test-command discovery (S3 slice 2)
    coverage_deps: CoverageDeps                # coverage I/O (S3 slice 3)
    coverage_provider: CoverageProvider        # backend-selected coverage provider (S22)
    # S3 slices 5-10 — migrated use-case deps:
    ws_deps: WorkspaceDeps                      # git workspace I/O (S3 slice 5)
    ws_scan_deps: WorkspaceScanDeps            # workspace structure scan (S3 slice 6)
    task4_deps: Task4Deps                      # TASK-4 genesis rewrite (S3 slice 7)
    task0_docs_deps: Task0DocsDeps             # TASK-0 profile/context docs (S3 slice 8)
    phase_c_retro_deps: PhaseCRetroDeps        # phase-C retrospective (S3 slice 9)
    validator_deps: Task2CheckerDeps           # TASK-2 output validators (S3 slice 10)
    # Reconstruction use-case spine (S3 slices 11-20). NB: the role adapters +
    # lock/reattach/journal/artifacts/test_runner/coverage_docs/flow_audit/
    # docs_metrics/reconstruction_step/failure_analysis are consumed only as
    # build_app locals (to assemble the phase use-cases) → not exposed on App (S24).
    telemetry: LogTelemetry                    # injected (S23)
    requirements_phase: RequirementsPhase      # (S17) — read by main()
    module_reconstructor: ModuleReconstructor  # (S20) — read by main()
    task3_retry_deps: Task3RetryDeps           # task3 inline retry (S27)


def build_app(
    adapters: Adapters,
    settings: Settings,
    run_state: RunState,
    scope: ClusterScope,
    ports: Ports,
    config: CompositionConfig,
    roles: ReconstructRoles,
    helpers: Helpers,
    structure: StructureReader,
    fill_roles: FillRoles,
    telemetry: LogTelemetry,
    augment_roles: AugmentRoles,
    coverage_deps: CoverageDeps,
    coverage_provider: CoverageProvider,
    *,
    ask_client: AskClient,
    events_log: Callable[[], str] = lambda: "logs/events.jsonl",
    run_context: dict[str, str] | None = None,
) -> App:
    """Assemble the App from its ports + run-scoped holders + config. Pure wiring
    (no I/O) — the place a test passes fake adapters/ports to get a fake-backed App."""
    cfg = config
    # Role adapters + spine objects are referenced by the phase use-cases below,
    # so build them as locals first (a field can't reference a sibling field in
    # the same App(...) call), then thread them into the use-cases.
    phase_gate = PhaseGateAdapter(roles.phase_valid, roles.commit_and_mark)
    requirements = RequirementsAdapter(roles.task1, roles.validate_task1_output)
    generator = GeneratorAdapter(
        roles.task2_inner_loop, roles.load_original_structure,
        roles.validate_task2_output, roles.validate_task2_api_conformance,
    )
    fixer = FixerAdapter(
        roles.task2_fix_loop, roles.classify_failure, ports.log,
        roles.insert_no_run, roles.fix_err_cap,
    )
    analyst = AnalystAdapter(
        roles.task4, roles.validate_task4_output, roles.task_genesis_audit,
    )
    failure_journal = FileFailureJournal()
    workspace_artifacts = WorkspaceArtifacts()
    lock_deps = LockDeps(
        commit_and_mark=roles.commit_and_mark, set_current=set_current,
        lock_module=lock_module, git_commit=helpers.git_commit, log=ports.log,
        sys_exit=sys.exit,
        scan_unimplemented_functions=helpers.scan_unimplemented_functions,
    )
    reattach_deps = ReattachDeps(
        log=ports.log, emit=ports.emit, derive_gentest_prelude=helpers.derive_gentest_prelude,
        is_gen_tests_file=helpers.is_gen_tests_file, gen_tests_siblings=helpers.gen_tests_siblings,
        load_gen_tests_origin=helpers.load_gen_tests_origin, gen_tests_path=helpers.gen_tests_path,
        build_gen_tests_decl=helpers.build_gen_tests_decl, cluster_cfg_attr=helpers.cluster_cfg_attr,
        has_gen_tests_decl=helpers.has_gen_tests_decl, cargo_env=ports.cargo_env,
    )
    test_runner = CallableTestRunner(helpers.task3)
    coverage_docs = CoverageDocsAdapter(
        lambda project_dir, test_files, readme_text, project_profile, clusters:
            helpers.task0b_discovery(
                project_dir / cfg.coverage_strategy_name, project_dir, test_files,
                readme_text, project_profile, clusters=clusters, force=True),
    )
    flow_audit = FlowAuditAdapter(helpers.run_flow_audit)
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
    failure_analysis = FailureAnalysisStep(
        analyst=analyst, journal=failure_journal, ratchet=BestTreeRatchet(),
        artifacts=workspace_artifacts, coverage_docs=coverage_docs,
        audit=flow_audit, docs=docs_metrics, telemetry=telemetry,
        genesis_audit_interval=cfg.genesis_audit_interval,
    )
    def _stall_refill(project_dir: Path, module: str) -> None:
        # Same keys restart_from.sh task2a clears — the manual escape hatch,
        # now fired automatically on a stalled repair loop.
        for _key in (f"task2a/{module}", f"task2b/{module}", f"task2c/{module}"):
            mark_phase(project_dir, _key, "pending",
                       detail="auto-reset: stalled repair iterations")

    module_reconstructor = ModuleReconstructor(
        step=reconstruction_step, analysis=failure_analysis, telemetry=telemetry,
        plateau_abort=cfg.module_plateau_abort,
        stall_refill_iters=cfg.stall_refill_iters,
        stall_refill=_stall_refill,
    )
    # S26: coverage_deps + provider are injected pre-built (constructed standalone in
    # pipeline.py before _APP) so module-level consumers reach them without _APP.
    return App(
        adapters=adapters,
        settings=settings,
        run_state=run_state,
        scope=scope,
        ports=ports,
        config=config,
        structure=structure,
        cargo_runner=PortBoundCargoRunner(adapters.cargo),
        ask_client=ask_client,
        scan_stub_records_for_cluster=helpers.scan_stub_records_for_cluster,
        collect_impl_blocks_for_cluster=helpers.collect_impl_blocks_for_cluster,
        group_fill_deps=GroupFillDeps(
            log=ports.log,
            ask=ask_client.ask,
            extract_fn_blocks=helpers.extract_fn_blocks,
            format_skeleton_index_for_cluster=helpers.format_skeleton_index_for_cluster,
            leaf_first_group_order=helpers.leaf_first_group_order,
            load_skeleton_files=helpers.load_skeleton_files,
            merge_fn_blocks_into_disk=helpers.merge_fn_blocks_into_disk,
            scan_unimplemented_functions=helpers.scan_unimplemented_functions,
            single_call_char_limit=cfg.single_call_char_limit,
            genesis_cap=PromptBudgets.derive(cfg.single_call_char_limit).genesis,
            task2_model=settings.task_models.task2,
        ),
        phase_c_tdd_deps=PhaseCTddDeps(
            attribute_failures=helpers.attribute_failures,
            build_test_reverse_map=helpers.build_test_reverse_map,
            filter_test_targets=helpers.filter_test_targets,
            check_graceful_stop=helpers.check_graceful_stop,
            converge_in_cluster=helpers.converge_in_cluster,
            count_relevant_notes=helpers.count_relevant_notes,
            effective_stalls=helpers.effective_stalls,
            emit=ports.emit,
            log=ports.log,
            order_frontier=helpers.order_frontier,
            record_tdd_regression=helpers.record_tdd_regression,
            reset_exhaustion_flags=helpers.reset_exhaustion_flags,
            run_cargo=helpers.run_cargo,
            scan_unimplemented_for_cluster=helpers.scan_unimplemented_for_cluster,
            scan_unimplemented_functions=helpers.scan_unimplemented_functions,
            task2_fill_fns_one_by_one=helpers.task2_fill_fns_one_by_one,
        ),
        impl_fill_deps=ImplFillDeps(
            build_dispatch_family_section=helpers.build_dispatch_family_section,
            build_per_impl_prompt=helpers.build_per_impl_prompt,
            cargo_env=ports.cargo_env,
            cargo_err_sig=helpers.cargo_err_sig,
            cargo_err_sig_from_diagnostics=helpers.cargo_err_sig_from_diagnostics,
            check_graceful_stop=helpers.check_graceful_stop,
            cluster_filter_type_methods=helpers.cluster_filter_type_methods,
            collect_crate_macros=helpers.collect_crate_macros,
            collect_impl_blocks_for_cluster=helpers.collect_impl_blocks_via_app,
            collect_sibling_filled_bodies=helpers.collect_sibling_filled_bodies,
            compute_cluster_macro_invariants=helpers.compute_cluster_macro_invariants,
            detect_crate_no_std_invariant=helpers.detect_crate_no_std_invariant,
            dispatch_families_via_extractor=helpers.dispatch_families,
            effective_stalls=helpers.effective_stalls,
            emit=ports.emit,
            enumerate_types_via_extractor=helpers.enumerate_types,
            extract_crate_idioms=helpers.extract_crate_idioms,
            extract_impl_where_clause=helpers.extract_impl_where_clause,
            filter_model_notes_for_prompt=helpers.filter_model_notes_for_prompt,
            find_tests_referencing_fn=helpers.find_tests_referencing_fn,
            focus_cargo_errors=helpers.focus_cargo_errors,
            frontier_from_diags=helpers.frontier_from_diags,
            impl_block_id=_impl_block_id,
            is_fn_fully_exhausted=_retros_is_exhausted,
            load_retrospectives=_retros_load,
            log=ports.log,
            module_of=helpers.module_of,
            phase_c_retrospective=helpers.phase_c_retrospective,
            record_error_codes_seen=helpers.record_error_codes_seen,
            retro_key=_retros_key,
            run_cargo_check_diag=helpers.run_cargo_check_diag,
            save_retrospectives=_retros_save,
            scan_unimplemented_for_cluster=helpers.scan_unimplemented_for_cluster,
            scan_unimplemented_records_for_cluster=helpers.scan_stub_records_via_app,
            splice_fn_with_where=helpers.splice_fn_with_where,
            splice_lock_for=helpers.splice_lock_for,
            types_section_for_fix=helpers.types_section_for_fix,
            bound_methods_section_for=helpers.bound_methods_section_for,
            u2_test_gate=helpers.u2_test_gate,
            test_regression_baseline=helpers.test_regression_baseline,
            test_regression_gate=helpers.test_regression_gate,
            ask=ask_client.ask,
            failed_body_cap=cfg.failed_body_cap,
            per_fn_fix_hard_cap=settings.fix_caps.per_fn_fix_hard_cap,
            per_impl_max_stubs=helpers.per_impl_max_stubs,
            task2_model=settings.task_models.task2,
        ),
        task1_deps=Task1Deps(
            load_api_surface=_load_api_surface,
            load_type_names=_load_type_names,
            read=_io_read,
            ask=ask_client.ask,
            write_fr_preserving_notes=helpers.write_fr_preserving_notes,
            synthesizer_factory=helpers.task1_synthesizer_factory,
            filter_proc_macro_test_files=helpers.filter_proc_macro_test_files,
            proc_macro_fallback_requirements=_pm_fallback_reqs,
            format_api_surface_section=helpers.format_api_surface_section,
            file_format_instruction=helpers.file_format_instruction,
            single_call_char_limit=cfg.single_call_char_limit,
            profile_cap=cfg.profile_cap,
            task1_model=settings.task_models.task1,
        ),
        emitter=JsonlEventEmitter(events_log, run_context if run_context is not None else {}),
        ask_per_fn_body=helpers.ask_per_fn_body,
        fill_roles=fill_roles,
        augment_roles=augment_roles,
        genesis_audit_deps=GenesisAuditDeps(
            ask=ports.ask, read=read, write=write,
            model=settings.task_models.task4,
            verbosity_guard=cfg.task4_verbosity_guard,
        ),
        discover_test_cmd_deps=DiscoverTestCommandDeps(
            ask=ports.ask,
            task0_model=settings.task_models.task0,
            model_name=settings.model,
            extra_cap=cfg.extra_cap,
            sys_exit=sys.exit,
        ),
        coverage_deps=coverage_deps,
        coverage_provider=coverage_provider,
        ws_deps=WorkspaceDeps(log=ports.log, emit=ports.emit),
        ws_scan_deps=WorkspaceScanDeps(
            ask=ports.ask, task0_model=settings.task_models.task0,
            skip_dir_names=cfg.skip_dir_names, parse_cmd=helpers.parse_cmd,
        ),
        task4_deps=Task4Deps(
            ask=ports.ask, read=read, write=write,
            extract_and_store_rules=helpers.extract_and_store_rules,
            model=settings.model, task4_model=settings.task_models.task4,
            verbosity_guard=cfg.task4_verbosity_guard,
            intermediate_char_limit=cfg.intermediate_char_limit,
            single_call_char_limit=cfg.single_call_char_limit,
        ),
        task0_docs_deps=Task0DocsDeps(
            phase_valid=phase_valid, mark_phase=mark_phase, ask=ports.ask, write=write,
            prompt_budget=helpers.prompt_budget, task0_model=settings.task_models.task0,
            model=settings.model, readme_cap=cfg.readme_cap, profile_cap=cfg.profile_cap,
            project_context_name=cfg.project_context_name,
        ),
        phase_c_retro_deps=PhaseCRetroDeps(
            ask_retrospective=helpers.ask_retrospective, log=ports.log,
            failed_body_cap=cfg.failed_body_cap, fix_err_cap=cfg.fix_err_cap,
        ),
        validator_deps=Task2CheckerDeps(
            enumerate_types=helpers.enumerate_types, enumerate_fns=helpers.enumerate_fns,
            load_pub_items_manifest=load_pub_items_manifest,
        ),
        telemetry=telemetry,
        requirements_phase=requirements_phase,
        module_reconstructor=module_reconstructor,
        # S27: task3 inline-retry deps — composed from the fill-role adapters
        # (whose lambdas late-bind the pipeline globals, so the scripted-cargo/
        # scripted-model e2e seams intercept at call time) + the Helpers shims
        # for the attribution/diag collaborators the fill roles don't carry.
        task3_retry_deps=Task3RetryDeps(
            cargo=Task3Cargo(
                env=fill_roles.cargo.env,
                run=fill_roles.cargo.run,
                check_diag=helpers.run_cargo_check_diag,
            ),
            attribution=Task3Attribution(
                attribute_failures=helpers.attribute_failures,
                build_test_reverse_map=helpers.build_test_reverse_map,
                frontier_from_diags=helpers.frontier_from_diags,
                filter_test_targets=helpers.filter_test_targets,
            ),
            enumerator=Task3Enumerate(
                cluster_filter_type_methods=fill_roles.ws.cluster_filter_type_methods,
                enumerate_types=helpers.enumerate_types,
                fn_container_index=fill_roles.ws.fn_container_index,
            ),
            retry_fix=RetryDeps(
                locate_fn=fill_roles.mut.locate_fn,
                gather_fix_context=fill_roles.prompt.gather,
                build_per_fn_fix_prompt=build_per_fn_fix_prompt,
                ask_per_fn_body=fill_roles.prompt.ask_per_fn_body,
                splice_fn_body=fill_roles.mut.splice_fn_body,
                find_tests_for_fn=fill_roles.tests.find_tests_for_fn,
                focus_cargo_errors=lambda out, rel_f: fill_roles.cargo.focus_errors(
                    out, rel_f, max_chars=cfg.fix_err_cap),
                is_gen_tests_file=fill_roles.ws.is_gen_tests_file,
                log=ports.log,
            ),
            log=ports.log,
            test_timeout=cfg.test_timeout,
        ),
    )
