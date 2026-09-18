"""RequirementsPhase — TASK 1 as a single-responsibility use-case.

Generate functional_requirements for a module, validate it (anti-corruption),
retry on rejection, and on exhaustion escalate to TASK 4 with a
``requirements_failure``. Returns whether valid requirements exist. Depends only
on the roles it uses (ISP): requirements generation, the phase gate, telemetry,
plus the analyst/journal/artifacts needed for the escalation path.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pipeline_core.domain.generation import Task1Request
from pipeline_core.ports.reconstruct import (
    Analyst,
    Artifacts,
    FailureJournal,
    PhaseGate,
    RequirementsService,
    Telemetry,
)

T1_MAX_RETRIES = 2


class RequirementsPhase:
    def __init__(
        self, *, requirements: RequirementsService, gate: PhaseGate,
        telemetry: Telemetry, analyst: Analyst, journal: FailureJournal,
        artifacts: Artifacts,
    ) -> None:
        self._req = requirements
        self._gate = gate
        self._tel = telemetry
        self._analyst = analyst
        self._journal = journal
        self._artifacts = artifacts

    def run(
        self, project_dir: Path, *, module_name: str, module_req_path: Path,
        genesis_path: Path, failure_log_path: Path, t1_input: Any,
        readme_text: str, project_profile: str, project_context: str,
        coverage_strategy: str, crate_type: str, from_spec: bool,
        src_hash: str | None, iteration: int, prev_pass_count: int,
        prev_test_output: str,
    ) -> bool:
        """Return True when valid requirements exist for the module. Forward
        (spec-driven) mode trusts the user-provided FR and returns True without
        invoking TASK 1 (the FR file is seeded by the caller)."""
        if from_spec:
            return True

        t1_valid = False
        t0 = time.monotonic()
        self._tel.emit("task.start", task="task1", module=module_name)
        for attempt in range(T1_MAX_RETRIES + 1):
            already_done = (self._gate.phase_valid(project_dir, f"task1/{module_name}")
                            and module_req_path.exists() and attempt == 0)
            if already_done:
                self._tel.log(f"\nTASK 1: requirements for '{module_name}' already done — skipping.")
            else:
                if attempt > 0:
                    self._tel.log(f"\nTASK 1: retry {attempt}/{T1_MAX_RETRIES} for '{module_name}'.")
                    module_req_path.unlink(missing_ok=True)
                if not self._req.task1(Task1Request(
                    project_dir=project_dir, genesis_path=genesis_path,
                    functional_req_path=module_req_path, test_files=t1_input,
                    readme_text=readme_text,
                    project_profile=project_profile, crate_type=crate_type,
                    module_name=module_name, project_context=project_context,
                    coverage_strategy=coverage_strategy,
                )):
                    self._tel.log(f"  TASK 1 failed for '{module_name}' — skipping module.")
                    self._tel.emit("task.done", task="task1", module=module_name,
                                   duration_ms=int((time.monotonic() - t0) * 1000),
                                   data={"ok": False, "reason": "task1_failed"})
                    break

            ok, reason = self._req.validate_task1_output(module_req_path, t1_input)
            if ok:
                self._tel.log("  TASK 1 output validated OK.")
                t1_valid = True
                self._gate.commit_and_mark(
                    project_dir, f"task1/{module_name}", "done",
                    commit_message=f"pipeline: task1 — {module_name}",
                    detail=f"{module_req_path.stat().st_size} chars",
                    source_hash=src_hash,
                )
                self._tel.set_current(project_dir, f"task1/{module_name} / done")
                self._tel.emit("task.done", task="task1", module=module_name,
                               duration_ms=int((time.monotonic() - t0) * 1000),
                               data={"ok": True, "retries": attempt,
                                     "req_chars": module_req_path.stat().st_size})
                break
            self._tel.log(f"  TASK 1 anti-corruption FAILED: {reason}")
            if attempt >= T1_MAX_RETRIES:
                self._tel.log(
                    f"  TASK 1 failed validation after {T1_MAX_RETRIES} retries — "
                    f"escalating to TASK 4 with failure_type='requirements_failure'.")
                self._analyst.task4(
                    genesis_path, module_req_path,
                    f"TASK 1 produced invalid requirements after {T1_MAX_RETRIES} retries. "
                    f"Last rejection reason: {reason}",
                    iteration, project_dir,
                    failure_type="requirements_failure",
                    failure_log=self._journal.load_failure_log(failure_log_path),
                    prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
                    api_surface=self._artifacts.load_api_surface(project_dir, module_name),
                )
                break  # do not set t1_valid; caller skips the module
        return t1_valid
