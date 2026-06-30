"""run_pipeline happy-path coverage (S27/S4).

`main`'s 476-line body was relocated verbatim into `orchestrator.run_pipeline`
behind an injectable `RunDeps` bundle — the whole justification being that the
top-level pipeline run is now unit-testable with fakes. This drives the
single-module forward path: prologue (workspace/setup → cluster scan → test cmd
→ task0a/0b → dep graph → baseline coverage → cluster augment) plus one
reconstruction loop iteration. The single-module loop has no None sentinel
(`modules[0]` is re-selected forever); the real `ModuleReconstructor.run`
sys.exit(0)s on a passing module, so the fake does the same to end the run.
"""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pipeline_core.application import orchestrator
from pipeline_core.domain.enums import Backend


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        repo="dummy-repo", project="proj", clean=False, augment_tests=False,
        progress=False, nolog=False, from_spec=False, restart_from_augmented=False,
        audit=False, best_models_list=False, vram_gb=32.0,
        phase_a_only=False, phase_b_only=False, phase_c_only=False,
        phase_d_only=False, skeleton_backend=False,
    )


def _run_deps(project_dir: Path) -> orchestrator.RunDeps:
    # Every field defaults to a no-op MagicMock; the happy path overrides only
    # the ones whose return shape the real orchestrator code inspects.
    d: dict[str, object] = {f.name: MagicMock(name=f.name) for f in fields(orchestrator.RunDeps)}

    dep_graph = MagicMock(name="dep_graph")
    dep_graph.topological_order.return_value = ["mod"]  # single module → is_multi_module False

    d.update(
        # constants
        WORKSPACE_DIR="tmp", MANIFEST_NAME="manifest.txt", MODEL="m",
        COVERAGE_BACKEND="tarpaulin", MODEL_CONTEXT_CHARS=1000,
        SINGLE_CALL_CHAR_LIMIT=100, FILE_CHAR_LIMIT=100, INTERMEDIATE_CHAR_LIMIT=100,
        COVERAGE_AUGMENTATION_TARGET=98.0, TARPAULIN_BIN=Path("/no/such/tarpaulin"),
        FAILURE_LOG_NAME="fail.log", PROJECT_PROFILE_NAME="profile.md",
        PROJECT_CONTEXT_NAME="context.md", COVERAGE_STRATEGY_NAME="strategy.md",
        SETTINGS=SimpleNamespace(backend=Backend.OLLAMA),
        # run-scoped singletons
        run_state=SimpleNamespace(),
        # resident functions whose return shape matters
        setup_workspace=MagicMock(return_value=project_dir),
        is_git_repo=MagicMock(return_value=False),
        git_file_hash=MagicMock(return_value="abcd1234"),
        source_hash=MagicMock(return_value="srchash"),
        task0_cfg_scan=MagicMock(return_value=[]),
        phase_valid=MagicMock(return_value=False),
        discover_test_command=MagicMock(return_value="cargo test"),
        parse_cmd=MagicMock(return_value=["cargo", "test"]),
        collect_test_files=MagicMock(return_value=[]),
        collect_protected_test_files=MagicMock(return_value=[]),
        task0_project_profile=MagicMock(return_value=""),
        task0_project_context=MagicMock(return_value=""),
        task0b_discovery=MagicMock(return_value=""),
        dependency_graph_factory=MagicMock(return_value=dep_graph),
        infer_build_check_command=MagicMock(return_value="cargo build"),
        wl_save_gen_tests_siblings=MagicMock(return_value={}),
        collect_examples=MagicMock(return_value=[]),
    )

    requirements_phase = MagicMock(name="requirements_phase")
    requirements_phase.run.return_value = True
    d["requirements_phase"] = requirements_phase

    module_reconstructor = MagicMock(name="module_reconstructor")
    # Single-module mode: the real use-case sys.exit(0)s on a passing module,
    # which is the only thing that ends the otherwise-infinite selection loop.
    module_reconstructor.run.side_effect = SystemExit(0)
    d["module_reconstructor"] = module_reconstructor

    return orchestrator.RunDeps(**d)  # type: ignore[arg-type]


def test_run_pipeline_single_module_happy_path(tmp_path: Path) -> None:
    deps = _run_deps(tmp_path)
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert exc.value.code == 0

    # Drove the full forward prologue + one reconstruction iteration.
    deps.setup_workspace.assert_called_once()
    deps.task0_cfg_scan.assert_called_once_with(tmp_path)
    deps.discover_test_command.assert_called_once()
    deps.dependency_graph_factory.assert_called_once()
    deps.cluster_augmentor.run.assert_called_once()
    deps.strip_and_bake_module.assert_called_once()
    deps.requirements_phase.run.assert_called_once()
    deps.module_reconstructor.run.assert_called_once()
    assert deps.run_state.git_managed is True


def test_run_pipeline_missing_project_dir_exits(tmp_path: Path) -> None:
    deps = replace(_run_deps(tmp_path),
                   setup_workspace=MagicMock(return_value=tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert exc.value.code == 1
