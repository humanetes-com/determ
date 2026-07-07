"""Reconstruction use-cases (P5): RequirementsPhase, ReconstructionStep,
FailureAnalysisStep, ModuleReconstructor — driven through their role ports with
a single recording fake adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application import orchestrator
from pipeline_core.application.gen_tests_reattach import ReattachDeps
from pipeline_core.application.reconstruct.failure_analysis import FailureAnalysisStep
from pipeline_core.application.reconstruct.module_reconstructor import ModuleReconstructor
from pipeline_core.application.reconstruct.reconstruction_step import (
    ReconstructionStep,
    StepOutcome,
)
from pipeline_core.application.reconstruct.requirements_phase import RequirementsPhase
from pipeline_core.application.reconstruct.run_context import RunContext
from pipeline_core.domain.reconstruction import IterationState


class FakeAdapter:
    """Implements every reconstruction role; records calls and returns scripted
    values. Satisfies the Protocols structurally (same as the real facade)."""

    def __init__(self, **scripted: Any) -> None:
        self.s = scripted
        self.calls: list[str] = []
        self.logs: list[str] = []
        self.events: list[str] = []

    # Telemetry
    def log(self, msg: str) -> None: self.logs.append(msg)
    def emit(self, event: str, **f: Any) -> None: self.events.append(event)
    def set_current(self, project_dir: Path, label: str) -> None: ...
    # PhaseGate
    def phase_valid(self, project_dir: Path, key: str) -> bool: return self.s.get("phase_valid", False)
    def commit_and_mark(self, *a: Any, **k: Any) -> None: self.calls.append("commit_and_mark")
    # RequirementsService
    def task1(self, *a: Any, **k: Any) -> bool:
        self.calls.append("task1"); return self.s.get("task1", True)
    def validate_task1_output(self, *a: Any) -> tuple[bool, str]:
        return self.s.get("validate_task1", (True, ""))
    # Generator
    def task2_inner_loop(self, *a: Any, **k: Any) -> bool:
        self.calls.append("task2"); return self.s.get("task2", True)
    def load_original_structure(self, *a: Any) -> list[str]: return []
    def validate_task2_output(self, *a: Any, **k: Any) -> tuple[bool, str]:
        return self.s.get("validate_task2", (True, ""))
    def validate_api_conformance(self, *a: Any) -> tuple[bool, str]:
        return self.s.get("api_conf", (True, ""))
    # TestRunner
    def task3(self, *a: Any, **k: Any) -> tuple[bool, str]:
        self.calls.append("task3")
        seq = self.s.get("task3_seq")
        if seq:
            return seq.pop(0)
        return self.s.get("task3", (True, "5 passed"))
    # Fixer
    def task2_fix_loop(self, *a: Any, **k: Any) -> tuple[bool, str]:
        self.calls.append("fix"); return self.s.get("fix", (False, "still failing"))
    def classify_failure(self, out: str) -> str: return "test"
    def diagnose_no_tests(self, *a: Any) -> tuple[str, str]:
        self.calls.append("diagnose"); return self.s.get("diagnose", ("no_tests", "diag"))
    # Analyst
    def task4(self, *a: Any, **k: Any) -> tuple[bool, int]:
        self.calls.append("task4"); return self.s.get("task4", (True, 3))
    def validate_task4_output(self, *a: Any) -> tuple[bool, str]:
        return self.s.get("validate_task4", (True, ""))
    def genesis_audit(self, *a: Any) -> bool:
        self.calls.append("genesis_audit"); return True
    # FailureJournal
    def load_failure_log(self, *a: Any) -> str: return ""
    def append_failure_log(self, *a: Any) -> None: self.calls.append("append_log")
    # Ratchet
    def best_tree_pass_count(self, *a: Any) -> int | None: return self.s.get("best", None)
    def save_best_tree(self, *a: Any, **k: Any) -> int:
        self.calls.append("save_best"); return 1
    def restore_best_tree(self, *a: Any) -> list[str]:
        self.calls.append("restore_best"); return []
    # Artifacts
    def read(self, p: Path) -> str: return self.s.get("genesis", "g")
    def load_api_surface(self, *a: Any) -> str: return ""
    def load_manifest(self, *a: Any) -> list[str]: return []
    def load_locked_source_context(self, *a: Any) -> str: return ""
    def format_whole_project_context(self, *a: Any) -> str: return "WPC"
    def collect_source_context(self, *a: Any) -> str: return "SRC"
    # CoverageDocs
    def regenerate(self, *a: Any) -> str:
        self.calls.append("regen"); return "new-strategy"
    # FlowAudit
    def run(self, ctx: str) -> None: self.calls.append("audit")
    # DocsMetrics
    def model_notes(self, p: Path) -> list[str]: return []
    def genesis_rules_count(self, *a: Any) -> int: return 0


def _ctx(tmp_path: Path, **over: Any) -> RunContext:
    base: dict[str, Any] = {
        "project_dir": tmp_path, "module_name": "m", "module_dir": tmp_path,
        "crate_type": "lib", "module_manifest": tmp_path / ".gen", "module_dir_rel": "",
        "module_test_cmd": "cargo test", "module_build_check": None,
        "module_has_tests": True, "module_req_path": tmp_path / "fr.md",
        "genesis_path": tmp_path / "genesis.md", "failure_log_path": tmp_path / "fail.log",
        "is_multi_module": True, "src_hash": "h", "workspace_deps": set(),
        "locked_modules": set(), "workspace_structure": "", "project_profile": "",
        "project_context": "", "readme_text": "", "test_files": [],
        "existing_test_files": set(), "clusters": [], "saved_gen_blocks": {}}
    base.update(over)
    return RunContext(**base)


def _lock_deps() -> orchestrator.LockDeps:
    return orchestrator.LockDeps(
        commit_and_mark=lambda *a, **k: None, set_current=lambda *a: None,
        lock_module=lambda *a: 0, git_commit=lambda *a: True, log=lambda *a: None,
        sys_exit=lambda c: (_ for _ in ()).throw(SystemExit(c)),
        scan_unimplemented_functions=lambda *a, **k: [])


def _reattach_deps() -> ReattachDeps:
    return ReattachDeps(
        log=lambda *a: None, emit=lambda *a, **k: None,
        derive_gentest_prelude=lambda p: ("", ""), is_gen_tests_file=lambda n: False,
        gen_tests_siblings=lambda f: [], load_gen_tests_origin=lambda p: {},
        gen_tests_path=lambda s, n: s, build_gen_tests_decl=lambda *a, **k: "",
        cluster_cfg_attr=lambda c: "", has_gen_tests_decl=lambda t, n: False,
        cargo_env=lambda: {})


# --- RequirementsPhase ------------------------------------------------------

def _req_phase(fa: FakeAdapter) -> RequirementsPhase:
    return RequirementsPhase(requirements=fa, gate=fa, telemetry=fa, analyst=fa,
                             journal=fa, artifacts=fa)


def _run_req(fa: FakeAdapter, tmp_path: Path, *, from_spec: bool = False) -> bool:
    return _req_phase(fa).run(
        tmp_path, module_name="m", module_req_path=tmp_path / "fr.md",
        genesis_path=tmp_path / "g.md", failure_log_path=tmp_path / "f.log",
        t1_input=[], readme_text="", project_profile="", project_context="",
        coverage_strategy="", crate_type="lib", from_spec=from_spec, src_hash="h",
        iteration=1, prev_pass_count=0, prev_test_output="")


def test_requirements_from_spec_short_circuits(tmp_path: Path) -> None:
    fa = FakeAdapter()
    assert _run_req(fa, tmp_path, from_spec=True) is True
    assert "task1" not in fa.calls


def test_requirements_success(tmp_path: Path) -> None:
    (tmp_path / "fr.md").write_text("reqs", encoding="utf-8")  # commit/emit stat() it
    fa = FakeAdapter(task1=True, validate_task1=(True, ""))
    assert _run_req(fa, tmp_path) is True
    assert "task1" in fa.calls and "commit_and_mark" in fa.calls


def test_requirements_task1_failure_breaks(tmp_path: Path) -> None:
    fa = FakeAdapter(task1=False)
    assert _run_req(fa, tmp_path) is False
    assert "task4" not in fa.calls  # generation failed before validation/escalation


def test_requirements_invalid_escalates_to_task4(tmp_path: Path) -> None:
    fa = FakeAdapter(task1=True, validate_task1=(False, "bad"))
    assert _run_req(fa, tmp_path) is False
    assert "task4" in fa.calls  # escalation after retries exhausted


# --- ReconstructionStep -----------------------------------------------------

def _step(fa: FakeAdapter) -> ReconstructionStep:
    return ReconstructionStep(generator=fa, tests=fa, fixer=fa, artifacts=fa,
                              telemetry=fa, lock_deps=_lock_deps(),
                              reattach_deps=_reattach_deps())


def test_step_task2_exhausted_is_build_failure(tmp_path: Path) -> None:
    fa = FakeAdapter(task2=False)
    out = _step(fa).run(_ctx(tmp_path), IterationState(), coverage_strategy="")
    assert out == StepOutcome(False, "build", "BUILD FAILED: inner loop exhausted all retries.")


def test_step_validate_task2_failure_is_build(tmp_path: Path) -> None:
    fa = FakeAdapter(task2=True, validate_task2=(False, "missing lib.rs"))
    out = _step(fa).run(_ctx(tmp_path), IterationState(), coverage_strategy="")
    assert out.failure_type == "build" and "missing lib.rs" in out.test_output


def test_step_pass_locks(tmp_path: Path) -> None:
    fa = FakeAdapter(task2=True, task3=(True, "7 passed"))
    out = _step(fa).run(_ctx(tmp_path), IterationState(), coverage_strategy="")
    assert out.locked is True


def test_step_no_files_locks(tmp_path: Path) -> None:
    # module_has_tests=False + 0/0 verdict → no_files → lock on build success.
    fa = FakeAdapter(task2=True, task3=(False, "running 0 tests"))
    out = _step(fa).run(_ctx(tmp_path, module_has_tests=False), IterationState(),
                        coverage_strategy="")
    assert out.locked is True


def test_step_no_tests_diagnoses(tmp_path: Path) -> None:
    # has-tests + 0/0 verdict → no_tests → diagnose, returns its (type, output).
    fa = FakeAdapter(task2=True, task3=(False, "running 0 tests"),
                     diagnose=("build", "diag-out"))
    out = _step(fa).run(_ctx(tmp_path), IterationState(), coverage_strategy="")
    assert "diagnose" in fa.calls
    assert out == StepOutcome(False, "build", "diag-out")


def test_step_fail_then_fix_reverified_locks(tmp_path: Path) -> None:
    # first task3 fails, fix passes, reverify task3 passes → lock.
    fa = FakeAdapter(task2=True, fix=(True, "fixed"),
                     task3_seq=[(False, "test result: FAILED. 1 passed; 1 failed;"),
                                (True, "test result: ok. 2 passed; 0 failed;")])
    out = _step(fa).run(_ctx(tmp_path), IterationState(), coverage_strategy="")
    assert out.locked is True and "fix" in fa.calls


def test_step_fail_then_fix_fails_returns_failure(tmp_path: Path) -> None:
    fa = FakeAdapter(task2=True, fix=(False, "nope"),
                     task3=(False, "test result: FAILED. 1 passed; 2 failed;"))
    out = _step(fa).run(_ctx(tmp_path), IterationState(), coverage_strategy="")
    assert out.locked is False and out.failure_type == "test"


# --- FailureAnalysisStep ----------------------------------------------------

def _analysis(fa: FakeAdapter) -> FailureAnalysisStep:
    return FailureAnalysisStep(analyst=fa, journal=fa, ratchet=fa, artifacts=fa,
                               coverage_docs=fa, audit=fa, docs=fa, telemetry=fa,
                               genesis_audit_interval=5)


def test_analysis_advances_state_and_regens_on_update(tmp_path: Path) -> None:
    fa = FakeAdapter(task4=(True, 4), validate_task4=(True, ""), best=None)
    res = _analysis(fa).run(_ctx(tmp_path), IterationState(iteration=1, prev_pass_count=2),
                            StepOutcome(False, "test", "out"),
                            total_iters=0, coverage_strategy="old")
    assert res.coverage_strategy == "new-strategy"   # regenerated on real update
    assert res.state.iteration == 2 and res.state.prev_pass_count == 4
    assert res.state.no_improvement_streak == 0       # 4 > 2 improved
    assert "save_best" in fa.calls                    # new best (best was None)


def test_analysis_genesis_stall_skips_regen(tmp_path: Path) -> None:
    fa = FakeAdapter(task4=(True, 1), validate_task4=(False, "no change"))
    res = _analysis(fa).run(_ctx(tmp_path), IterationState(prev_pass_count=1),
                            StepOutcome(False, "test", "o"),
                            total_iters=0, coverage_strategy="keep")
    assert res.coverage_strategy == "keep" and "regen" not in fa.calls
    assert res.state.no_improvement_streak == 1       # 1 not > 1


def test_analysis_regression_restores_best(tmp_path: Path) -> None:
    fa = FakeAdapter(task4=(True, 1), validate_task4=(True, ""), best=5)
    res = _analysis(fa).run(_ctx(tmp_path), IterationState(prev_pass_count=5),
                            StepOutcome(False, "test", "o"),
                            total_iters=0, coverage_strategy="x")
    assert "restore_best" in fa.calls                 # current 1 < best 5
    assert res.total_iters == 1
    assert "genesis_audit" in fa.calls                # debt#12: regression fires audit off-interval


def test_analysis_no_regression_off_interval_skips_audit(tmp_path: Path) -> None:
    # current 4 ≥ prev 2 (no regression) and total_iters 1 % interval 5 ≠ 0 → no audit.
    fa = FakeAdapter(task4=(True, 4), validate_task4=(True, ""), best=None)
    _analysis(fa).run(_ctx(tmp_path), IterationState(prev_pass_count=2),
                      StepOutcome(False, "test", "o"),
                      total_iters=0, coverage_strategy="x")
    assert "genesis_audit" not in fa.calls


# --- ModuleReconstructor ----------------------------------------------------

def test_reconstructor_locks_immediately(tmp_path: Path) -> None:
    fa = FakeAdapter(task2=True, task3=(True, "9 passed"))
    coord = ModuleReconstructor(step=_step(fa), analysis=_analysis(fa), telemetry=fa)
    res = coord.run(_ctx(tmp_path), total_iters=0, coverage_strategy="s")
    assert res.total_iters == 0 and res.coverage_strategy == "s"  # locked first iter, no analysis
    assert res.converged
    assert "task4" not in fa.calls


def test_reconstructor_iterates_then_locks(tmp_path: Path) -> None:
    fa = FakeAdapter(task2=True, fix=(False, "x"), validate_task4=(True, ""), best=None,
                     task3_seq=[(False, "test result: FAILED. 0 passed; 1 failed;"),
                                (True, "test result: ok. 1 passed; 0 failed;")])
    coord = ModuleReconstructor(step=_step(fa), analysis=_analysis(fa), telemetry=fa)
    res = coord.run(_ctx(tmp_path), total_iters=0, coverage_strategy="s")
    assert res.total_iters == 1 and "task4" in fa.calls  # one failure → analysis → lock
    assert res.converged


def test_reconstructor_plateau_aborts_nonconverging(tmp_path: Path) -> None:
    """debt #16 follow-up: a module that never improves (constant 0-pass fail) must
    NOT loop forever — the fixed-point terminator breaks after `plateau_abort`
    consecutive non-improving iterations, emitting module.nonconverged."""
    fa = FakeAdapter(task2=True, fix=(False, "x"), validate_task4=(True, ""), best=None,
                     task3=(False, "test result: FAILED. 0 passed; 1 failed;"))
    coord = ModuleReconstructor(step=_step(fa), analysis=_analysis(fa),
                                telemetry=fa, plateau_abort=3)
    res = coord.run(_ctx(tmp_path), total_iters=0, coverage_strategy="s")
    assert "module.nonconverged" in fa.events     # terminated, not hung
    assert not res.converged   # caller must exclude the module, not re-enter it
    # iter 1 establishes the baseline (0→N counts as improvement); then 3
    # consecutive non-improving iters trip the cap → 4 iterations total.
    assert fa.events.count("module.iter") == 4


def test_reconstructor_plateau_disabled_by_default(tmp_path: Path) -> None:
    """plateau_abort=0 (default) keeps the legacy behavior: a converging run still
    locks normally without the terminator firing."""
    fa = FakeAdapter(task2=True, fix=(False, "x"), validate_task4=(True, ""), best=None,
                     task3_seq=[(False, "test result: FAILED. 0 passed; 1 failed;"),
                                (True, "test result: ok. 1 passed; 0 failed;")])
    coord = ModuleReconstructor(step=_step(fa), analysis=_analysis(fa), telemetry=fa)
    coord.run(_ctx(tmp_path), total_iters=0, coverage_strategy="s")
    assert "module.nonconverged" not in fa.events
