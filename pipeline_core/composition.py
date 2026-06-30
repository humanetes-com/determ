"""build_app — the composition factory (Phase 3).

The composition root, parameterized over its external ports. `main()` builds the
real :class:`Adapters` and calls :func:`build_app`; tests call it with fake
adapters so they inject at the port boundary (LLM / cargo / syn-extractor)
through the constructor instead of monkeypatching `pipeline._x` globals.

`App` is grown stage by stage: it currently holds the injected ports + run-scoped
state + the boundary providers derived from them. Later stages move the
`_X_DEPS` use-case wiring and the `main` run-loop here (see refactoring.md §Phase 3)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipeline_core.adapters import Adapters
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.tasks.genesis_audit import GenesisAuditDeps
from pipeline_core.domain.run_state import RunState
from pipeline_core.io import read, write
from pipeline_core.settings import Settings


@dataclass(frozen=True)
class App:
    """The wired application: external ports + run-scoped state + boundary
    providers and use-case deps built from them. Grown one S3 slice at a time
    (refactoring.md §Phase 3) as `_X_DEPS` blocks migrate out of pipeline.py."""

    adapters: Adapters
    settings: Settings
    run_state: RunState
    # Syn-extractor boundary: the structured-source reader over `adapters.extractor`.
    structure: CliStructureReader
    # TASK-4 genesis-audit use-case deps (S3 slice 1).
    genesis_audit_deps: GenesisAuditDeps


def build_app(
    adapters: Adapters,
    settings: Settings,
    run_state: RunState,
    *,
    ask: Callable[..., str],
    task4_verbosity_guard: str,
) -> App:
    """Assemble the App from its ports. Pure wiring (no I/O) — the place a test
    passes fake adapters/ports to get a fake-backed App. Boundary callables (e.g.
    ``ask``) are passed late-binding so the migration preserves the test seam."""
    return App(
        adapters=adapters,
        settings=settings,
        run_state=run_state,
        structure=CliStructureReader(adapters.extractor),
        genesis_audit_deps=GenesisAuditDeps(
            ask=ask, read=read, write=write,
            model=settings.task_models.task4,
            verbosity_guard=task4_verbosity_guard,
        ),
    )
