"""run_pipeline happy-path coverage.

`main`'s 476-line body was relocated verbatim into `orchestrator.run_pipeline`
behind an injectable `RunDeps` bundle — the whole justification being that the
top-level pipeline run is now unit-testable with fakes. This drives the
single-module forward path: prologue (workspace/setup → cluster scan → test cmd
→ task0a/0b → dep graph → baseline coverage → cluster augment) plus one
reconstruction loop iteration. The single-module loop re-selects `modules[0]`
until the module passes (the real `ModuleReconstructor.run` sys.exit(0)s, so
the fake does the same) or plateaus (converged=False → excluded → FINAL verdict).
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
        restart_from_pre_augment=False, reharvest_behaviours=False,
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
        # No bake artifact → the live scan runs (cluster identity is frozen to
        # the baked manifest as soon as one exists — see the frozen-path test).
        load_baked_clusters=MagicMock(return_value=[]),
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
        # No construction facts: the strategy text passes as the model wrote it.
        construction_facts=None,
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


def test_run_pipeline_plateau_stops_no_reentry(tmp_path: Path) -> None:
    """A non-converged (plateaued) module must end the run — exit 1 after the
    FINAL verdict — not be re-entered by the selection loop (the anyhow run-30425
    unbounded macro-loop: re-entry reset IterationState and looped forever)."""
    from pipeline_core.application.reconstruct.module_reconstructor import ModuleResult
    deps = _run_deps(tmp_path)
    deps.module_reconstructor.run.side_effect = None
    deps.module_reconstructor.run.return_value = ModuleResult(3, "s", converged=False)
    deps.task3.return_value = (False, "test result: FAILED. 51 passed; 19 failed;")
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert exc.value.code == 1
    deps.module_reconstructor.run.assert_called_once()  # not re-entered


def test_run_pipeline_missing_project_dir_exits(tmp_path: Path) -> None:
    deps = replace(_run_deps(tmp_path),
                   setup_workspace=MagicMock(return_value=tmp_path / "absent"))
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert exc.value.code == 1


def test_run_pipeline_baked_clusters_freeze_identity(tmp_path: Path) -> None:
    """Once the bake artifact exists (post-strip), cluster identity comes from
    it — the live cfg re-scan must NOT run. A post-strip re-scan walks the
    reconstructed tree and mints phantom clusters (build-probe/lint cfgs)
    whose task0c then destroys oracle gen_tests it cannot compile."""
    baked = [SimpleNamespace(name="stable", env_extra={}),
             SimpleNamespace(name="nightly", env_extra={"RUSTFLAGS": "x"})]
    deps = replace(_run_deps(tmp_path),
                   load_baked_clusters=MagicMock(return_value=baked))
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert exc.value.code == 0
    deps.task0_cfg_scan.assert_not_called()
    deps.scope.reset.assert_called_once_with(baked)


# --- the pre-augmentation restore point -----------------------------------

def test_run_pipeline_tags_the_pre_augmentation_point_before_task0c(tmp_path: Path) -> None:
    """A fresh git-managed run (repo given → git_managed) writes
    `task0c-pre-<module>` at HEAD before the cluster augmentation runs, and
    only after having listed the existing tags (never re-pointed)."""
    deps = _run_deps(tmp_path)
    deps.list_git_tags.return_value = []
    deps.read_state.return_value = {"phases": {"task0b": {"status": "done"}}, "current": ""}
    order: list[str] = []
    deps.git_tag.side_effect = lambda *_a, **_k: order.append("tag")
    deps.cluster_augmentor.run.side_effect = lambda *_a, **_k: order.append("task0c")
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    deps.git_tag.assert_called_once_with(tmp_path, "task0c-pre-mod")
    deps.list_git_tags.assert_called_once_with(tmp_path, "task0c-pre-*")
    assert order == ["tag", "task0c"]


def test_run_pipeline_does_not_repoint_an_existing_pre_augmentation_tag(tmp_path: Path) -> None:
    deps = _run_deps(tmp_path)
    deps.list_git_tags.return_value = ["task0c-pre-mod"]
    deps.read_state.return_value = {"phases": {"task0b": {"status": "done"}}, "current": ""}
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    deps.git_tag.assert_not_called()


def test_run_pipeline_refuses_to_tag_a_workspace_whose_task0c_already_ran(tmp_path: Path) -> None:
    """The live tmp/anyhow shape: no tag, HEAD a task2a checkpoint. Tagging
    HEAD would point the restore at a stripped tree."""
    deps = _run_deps(tmp_path)
    deps.list_git_tags.return_value = []
    deps.read_state.return_value = {
        "phases": {"task0b": {"status": "done"}, "task0c/stable": {"status": "done"}},
        "current": ""}
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    deps.git_tag.assert_not_called()
    assert any("NOT tagged" in str(c) for c in deps.log.call_args_list)


def test_run_pipeline_off_git_never_lists_or_writes_tags(tmp_path: Path) -> None:
    deps = replace(_run_deps(tmp_path), is_git_repo=MagicMock(return_value=False))
    args = _args()
    args.repo = None                      # --project path → git_managed from is_git_repo
    (tmp_path / "tmp" / "proj").mkdir(parents=True)
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, args, tmp_path)
    deps.git_tag.assert_not_called()
    deps.list_git_tags.assert_not_called()


def test_run_pipeline_restart_from_pre_augment_runs_the_restore(tmp_path: Path) -> None:
    deps = _run_deps(tmp_path)
    args = _args()
    args.restart_from_pre_augment = True
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, args, tmp_path)
    deps.restart_from_pre_augment.assert_called_once_with(tmp_path, tmp_path, reharvest=False)
    deps.restart_from_augmented.assert_not_called()


def test_run_pipeline_restart_from_pre_augment_failure_aborts(tmp_path: Path) -> None:
    deps = _run_deps(tmp_path)
    deps.restart_from_pre_augment.side_effect = RuntimeError("No task0c-pre-* tag")
    args = _args()
    args.restart_from_pre_augment = True
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, args, tmp_path)
    assert exc.value.code == 2
    deps.cluster_augmentor.run.assert_not_called()


@pytest.mark.parametrize("other", ["clean", "restart_from_augmented"])
def test_run_pipeline_pre_augment_is_exclusive_with_the_other_restores(tmp_path: Path, other: str) -> None:
    deps = _run_deps(tmp_path)
    args = _args()
    args.restart_from_pre_augment = True
    setattr(args, other, True)
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, args, tmp_path)
    assert exc.value.code == 2
    deps.restart_from_pre_augment.assert_not_called()
    deps.restart_from_augmented.assert_not_called()
    deps.git_clean_reset.assert_not_called()


def test_run_pipeline_harvests_behaviours_after_the_tag_and_before_task0c(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan's sentence: the behaviour harvest (task0d) reads the ORIGINAL,
    so it runs after the pre-t0c tag and before the cluster augmentation, on
    a module whose task0c has not run (no augmentation sentinel)."""
    deps = _run_deps(tmp_path)
    deps.list_git_tags.return_value = []
    deps.read_state.return_value = {"phases": {"task0b": {"status": "done"}}, "current": ""}
    deps.augment_store.is_module_augmented.return_value = False
    order: list[str] = []
    calls: list[tuple[object, ...]] = []
    deps.git_tag.side_effect = lambda *_a, **_k: order.append("tag")
    deps.cluster_augmentor.run.side_effect = lambda *_a, **_k: order.append("task0c")

    def _fake_t0d(*a: object, **_k: object) -> None:
        order.append("task0d")
        calls.append(a)

    monkeypatch.setattr(orchestrator, "run_task0d", _fake_t0d)
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert order == ["tag", "task0d", "task0c"]
    (t0d_deps, project_dir, module, _clusters, src_dir), = calls
    assert t0d_deps is deps.task0d_deps
    assert (project_dir, module, src_dir) == (tmp_path, "mod", tmp_path / "src")


def test_run_pipeline_refuses_the_harvest_once_the_module_is_augmented(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: a module whose task0c already ran (the augmentation
    sentinel is set — the live `tmp/anyhow` on a `restart_from.sh task2a`) is
    never harvested: its src/ is the stripped shell and its generated tests
    would enter the spec. The refusal is logged, task0c still runs."""
    deps = _run_deps(tmp_path)
    deps.list_git_tags.return_value = []
    deps.read_state.return_value = {"phases": {"task0b": {"status": "done"}}, "current": ""}
    deps.augment_store.is_module_augmented.return_value = True
    fake = MagicMock(name="run_task0d")
    monkeypatch.setattr(orchestrator, "run_task0d", fake)
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    fake.assert_not_called()
    deps.cluster_augmentor.run.assert_called_once()
    logged = " ".join(str(c.args[0]) for c in deps.log.call_args_list)
    assert "[task0d] 'mod' already augmented" in logged


# --- the behaviour restore point (task0d-done-<module>) ----------------------

def _harvested_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    order: list[str]) -> orchestrator.RunDeps:
    """Two baked clusters whose harvest is done, task0c not started."""
    clusters = [SimpleNamespace(name="stable", env_extra={}),
                SimpleNamespace(name="nightly", env_extra={})]
    deps = replace(_run_deps(tmp_path), load_baked_clusters=MagicMock(return_value=clusters))
    deps.list_git_tags.return_value = []
    deps.read_state.return_value = {"phases": {
        "task0b": {"status": "done"}, "task0d/stable": {"status": "done"},
        "task0d/nightly": {"status": "done"}, "task0d/mod/book": {"status": "done"}},
        "current": ""}
    deps.augment_store.is_module_augmented.return_value = False
    deps.git_tag.side_effect = lambda _p, tag: order.append(f"tag {tag}")
    deps.git_checkpoint.side_effect = lambda *_a: order.append("checkpoint") or True
    deps.cluster_augmentor.run.side_effect = lambda *_a, **_k: order.append("task0c")
    monkeypatch.setattr(orchestrator, "run_task0d", lambda *_a, **_k: order.append("task0d"))
    return deps


def test_run_pipeline_writes_the_behaviour_point_after_task0d_and_before_task0c(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tree is committed whole (the doc tests and the book task0d leaves
    uncommitted) before the tag, and both happen before task0c writes a test."""
    order: list[str] = []
    deps = _harvested_deps(tmp_path, monkeypatch, order)
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert order == ["tag task0c-pre-mod", "task0d", "checkpoint",
                     "tag task0d-done-mod", "task0c"]


def test_run_pipeline_does_not_tag_a_tree_it_could_not_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    deps = _harvested_deps(tmp_path, monkeypatch, order)
    deps.git_checkpoint.side_effect = None
    deps.git_checkpoint.return_value = False
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert "tag task0d-done-mod" not in order
    logged = " ".join(str(c.args[0]) for c in deps.log.call_args_list)
    assert "[task0d-done] NOT tagged" in logged


def test_run_pipeline_does_not_tag_a_partial_harvest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    deps = _harvested_deps(tmp_path, monkeypatch, order)
    deps.read_state.return_value["phases"]["task0d/nightly"] = {"status": "failed"}
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert "checkpoint" not in order and "tag task0d-done-mod" not in order


def test_run_pipeline_does_not_tag_a_harvest_whose_book_stage_is_open(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    deps = _harvested_deps(tmp_path, monkeypatch, order)
    del deps.read_state.return_value["phases"]["task0d/mod/book"]
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert "checkpoint" not in order and "tag task0d-done-mod" not in order
    logged = " ".join(str(c.args[0]) for c in deps.log.call_args_list)
    assert "[task0d-done] NOT tagged: the behaviour stage is not done for task0d/mod/book" in logged


def test_run_pipeline_refuses_task0d_once_any_task0c_entry_exists(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live tmp/anyhow on 2026-10-04: task0c/nightly in progress, so no
    module sentinel yet, with generated tests in the tree. A book stage
    resumed there would read them; task0d is refused and task0c resumes.
    Negative control: the same state without the task0c entry runs task0d."""
    order: list[str] = []
    deps = _harvested_deps(tmp_path, monkeypatch, order)
    del deps.read_state.return_value["phases"]["task0d/mod/book"]
    deps.read_state.return_value["phases"]["task0c/nightly"] = {"status": "in_progress"}
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert "task0d" not in order and "task0c" in order
    logged = " ".join(str(c.args[0]) for c in deps.log.call_args_list)
    assert "task0c has started in this workspace" in logged

    order.clear()
    del deps.read_state.return_value["phases"]["task0c/nightly"]
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert "task0d" in order


def test_run_pipeline_reharvest_reaches_the_restore(tmp_path: Path) -> None:
    deps = _run_deps(tmp_path)
    args = _args()
    args.restart_from_pre_augment = True
    args.reharvest_behaviours = True
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, args, tmp_path)
    deps.restart_from_pre_augment.assert_called_once_with(tmp_path, tmp_path, reharvest=True)


def test_run_pipeline_reharvest_without_the_pre_augment_restore_aborts(tmp_path: Path) -> None:
    deps = _run_deps(tmp_path)
    args = _args()
    args.reharvest_behaviours = True
    with pytest.raises(SystemExit) as exc:
        orchestrator.run_pipeline(deps, args, tmp_path)
    assert exc.value.code == 2
    deps.cluster_augmentor.run.assert_not_called()


# --- the strategy file's computed part, on every path that binds its text -----

class _Facts:
    """Records how the orchestrator asked for the construction facts and
    renders by prefixing, so the text a phase receives shows it was rendered."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def facts(self, project_dir: Path, clusters: object, *, may_measure: bool) -> tuple[str, ...]:
        self.calls.append(("facts", may_measure))
        return ("world",)

    def rendered(self, project_dir: Path, text: str, worlds: object) -> str:
        self.calls.append(("rendered", bool(worlds)))
        return "COMPUTED\n" + text


def _with_facts(tmp_path: Path, *, done: set[str]) -> tuple[orchestrator.RunDeps, _Facts]:
    facts = _Facts()
    deps = replace(_run_deps(tmp_path), construction_facts=facts,
                   phase_valid=MagicMock(side_effect=lambda _p, key: key in done))
    return deps, facts


def test_the_skip_branch_hands_the_rendered_strategy_to_the_requirements_phase(tmp_path: Path) -> None:
    (tmp_path / "strategy.md").write_text("MODEL TEXT", encoding="utf-8")
    deps, facts = _with_facts(tmp_path, done={"task0b"})
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    deps.task0b_discovery.assert_not_called()
    assert deps.requirements_phase.run.call_args.kwargs["coverage_strategy"] == "COMPUTED\nMODEL TEXT"
    assert facts.calls[0] == ("facts", True)   # no module stripped: the tree is original


def test_a_fresh_discovery_measures_the_facts_before_the_model_call(tmp_path: Path) -> None:
    deps, facts = _with_facts(tmp_path, done=set())
    order: list[str] = []
    deps.task0b_discovery.side_effect = lambda *a, **k: order.append("discovery") or "FRESH"
    real = facts.facts

    def facts_first(project_dir: Path, clusters: object, *, may_measure: bool) -> tuple[str, ...]:
        order.append("facts")
        return real(project_dir, clusters, may_measure=may_measure)

    facts.facts = facts_first  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert order[:2] == ["facts", "discovery"]
    assert deps.requirements_phase.run.call_args.kwargs["coverage_strategy"] == "COMPUTED\nFRESH"


def test_a_stripped_tree_is_rendered_from_stored_facts_never_measured(tmp_path: Path) -> None:
    (tmp_path / "strategy.md").write_text("MODEL TEXT", encoding="utf-8")
    deps, facts = _with_facts(tmp_path, done={"task0b", "strip/mod"})
    with pytest.raises(SystemExit):
        orchestrator.run_pipeline(deps, _args(), tmp_path)
    assert ("facts", True) not in facts.calls
    assert facts.calls[0] == ("facts", False)
