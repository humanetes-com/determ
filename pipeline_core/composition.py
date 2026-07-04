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
from pipeline_core.adapters.artifacts import WorkspaceArtifacts, load_api_surface
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
from pipeline_core.adapters.state import mark_phase, phase_valid, set_current
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.adapters.telemetry import LogTelemetry
from pipeline_core.adapters.test_runner import CallableTestRunner
from pipeline_core.adapters.workspace import WorkspaceDeps
from pipeline_core.adapters.workspace_files import lock_module
from pipeline_core.adapters.workspace_scan import WorkspaceScanDeps
from pipeline_core.application.cluster_augment import ClusterAugmentor
from pipeline_core.application.gen_tests_reattach import ReattachDeps
from pipeline_core.application.orchestrator import LockDeps
from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.module_reconstructor import ModuleReconstructor
from pipeline_core.application.reconstruct.per_fn_retry import (
    RetryDeps,
    Task3Attribution,
    Task3Cargo,
    Task3Enumerate,
    Task3RetryDeps,
)
from pipeline_core.application.reconstruct.reconstruction_step import ReconstructionStep
from pipeline_core.application.reconstruct.requirements_phase import RequirementsPhase
from pipeline_core.application.reconstruct.retrospective import PhaseCRetroDeps
from pipeline_core.application.tasks.discover_test_command import DiscoverTestCommandDeps
from pipeline_core.application.tasks.genesis_audit import GenesisAuditDeps
from pipeline_core.application.tasks.task0_docs import Task0DocsDeps
from pipeline_core.application.tasks.task4 import Task4Deps
from pipeline_core.application.validate import Task2CheckerDeps
from pipeline_core.domain.cluster_scope import ClusterScope
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.run_state import RunState
from pipeline_core.io import read, write
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
    # Syn-extractor boundary: the structured-source reader over `adapters.extractor`.
    structure: CliStructureReader
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
    structure: CliStructureReader,
    fill_roles: FillRoles,
    telemetry: LogTelemetry,
    augment_roles: AugmentRoles,
    coverage_deps: CoverageDeps,
    coverage_provider: CoverageProvider,
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
    module_reconstructor = ModuleReconstructor(
        step=reconstruction_step, analysis=failure_analysis, telemetry=telemetry,
        plateau_abort=cfg.module_plateau_abort,
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
            load_api_surface=load_api_surface,
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
