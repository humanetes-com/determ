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

from pipeline_core.adapters import Adapters
from pipeline_core.adapters.artifacts import load_api_surface
from pipeline_core.adapters.coverage import CoverageDeps
from pipeline_core.adapters.reconstruct_roles import (
    AnalystAdapter,
    FixerAdapter,
    GeneratorAdapter,
    PhaseGateAdapter,
    RequirementsAdapter,
)
from pipeline_core.adapters.state import mark_phase, phase_valid
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.adapters.workspace import WorkspaceDeps
from pipeline_core.adapters.workspace_scan import WorkspaceScanDeps
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
    # Migrated use-case deps:
    genesis_audit_deps: GenesisAuditDeps       # TASK-4 genesis audit (S3 slice 1)
    discover_test_cmd_deps: DiscoverTestCommandDeps  # test-command discovery (S3 slice 2)
    coverage_deps: CoverageDeps                # coverage I/O (S3 slice 3)
    # ISP reconstruction role adapters (S3 slice 4):
    phase_gate: PhaseGateAdapter
    requirements: RequirementsAdapter
    generator: GeneratorAdapter
    fixer: FixerAdapter
    analyst: AnalystAdapter
    # S3 slices 5-10 — migrated use-case deps:
    ws_deps: WorkspaceDeps                      # git workspace I/O (S3 slice 5)
    ws_scan_deps: WorkspaceScanDeps            # workspace structure scan (S3 slice 6)
    task4_deps: Task4Deps                      # TASK-4 genesis rewrite (S3 slice 7)
    task0_docs_deps: Task0DocsDeps             # TASK-0 profile/context docs (S3 slice 8)
    phase_c_retro_deps: PhaseCRetroDeps        # phase-C retrospective (S3 slice 9)
    validator_deps: Task2CheckerDeps           # TASK-2 output validators (S3 slice 10)


def build_app(
    adapters: Adapters,
    settings: Settings,
    run_state: RunState,
    scope: ClusterScope,
    ports: Ports,
    config: CompositionConfig,
    roles: ReconstructRoles,
    helpers: Helpers,
) -> App:
    """Assemble the App from its ports + run-scoped holders + config. Pure wiring
    (no I/O) — the place a test passes fake adapters/ports to get a fake-backed App."""
    cfg = config
    return App(
        adapters=adapters,
        settings=settings,
        run_state=run_state,
        scope=scope,
        ports=ports,
        config=config,
        structure=CliStructureReader(adapters.extractor),
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
        coverage_deps=CoverageDeps(
            log=ports.log, cargo_env=ports.cargo_env, cluster=scope.get_active,
            tarpaulin_bin=cfg.tarpaulin_bin, grcov_bin=cfg.grcov_bin,
            build_timeout=cfg.build_timeout, ignore_pragma=cfg.ignore_pragma,
        ),
        phase_gate=PhaseGateAdapter(roles.phase_valid, roles.commit_and_mark),
        requirements=RequirementsAdapter(roles.task1, roles.validate_task1_output),
        generator=GeneratorAdapter(
            roles.task2_inner_loop, roles.load_original_structure,
            roles.validate_task2_output, roles.validate_task2_api_conformance,
        ),
        fixer=FixerAdapter(
            roles.task2_fix_loop, roles.classify_failure, ports.log,
            roles.insert_no_run, roles.fix_err_cap,
        ),
        analyst=AnalystAdapter(
            roles.task4, roles.validate_task4_output, roles.task_genesis_audit,
        ),
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
    )
