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

from pipeline_core.adapters import Adapters
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.tasks.discover_test_command import DiscoverTestCommandDeps
from pipeline_core.application.tasks.genesis_audit import GenesisAuditDeps
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
class App:
    """The wired application: external ports + run-scoped state + boundary
    providers and use-case deps built from them. Grown one S3 slice at a time
    (refactoring.md §Phase 3) as `_X_DEPS` blocks migrate out of pipeline.py."""

    adapters: Adapters
    settings: Settings
    run_state: RunState
    ports: Ports
    # Syn-extractor boundary: the structured-source reader over `adapters.extractor`.
    structure: CliStructureReader
    # Migrated use-case deps:
    genesis_audit_deps: GenesisAuditDeps       # TASK-4 genesis audit (S3 slice 1)
    discover_test_cmd_deps: DiscoverTestCommandDeps  # test-command discovery (S3 slice 2)


def build_app(
    adapters: Adapters,
    settings: Settings,
    run_state: RunState,
    ports: Ports,
    *,
    task4_verbosity_guard: str,
    extra_cap: int,
) -> App:
    """Assemble the App from its ports. Pure wiring (no I/O) — the place a test
    passes fake adapters/ports to get a fake-backed App. (The remaining explicit
    kwargs are pipeline-level constants not yet in `Settings`; fold them in later.)"""
    return App(
        adapters=adapters,
        settings=settings,
        run_state=run_state,
        ports=ports,
        structure=CliStructureReader(adapters.extractor),
        genesis_audit_deps=GenesisAuditDeps(
            ask=ports.ask, read=read, write=write,
            model=settings.task_models.task4,
            verbosity_guard=task4_verbosity_guard,
        ),
        discover_test_cmd_deps=DiscoverTestCommandDeps(
            ask=ports.ask,
            task0_model=settings.task_models.task0,
            model_name=settings.model,
            extra_cap=extra_cap,
            sys_exit=sys.exit,
        ),
    )
