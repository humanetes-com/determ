"""Focused role adapters for the per-module reconstruction use-cases.

These replace the `_ReconstructAdapter` god-object: one narrow adapter per
`ports.reconstruct` role (ISP), each constructor-injected with the resident
composition-root callables it delegates to. TASK 1–4 logic already lives in
`application.tasks.*`; the resident `task1`/`task2_inner_loop`/`task3`/`task4`/
fix callables are the deps-binding wiring the composition root owns, so the
adapters wrap them rather than re-home them. The one piece of real logic that
lived ON the god-adapter — `diagnose_no_tests` — moves into `FixerAdapter`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.generation import Task1Request


@dataclass(frozen=True, slots=True)
class PhaseGateAdapter:
    phase_valid_fn: Callable[[Path, str], bool]
    commit_and_mark_fn: Callable[..., None]

    def phase_valid(self, project_dir: Path, phase_key: str) -> bool:
        return self.phase_valid_fn(project_dir, phase_key)

    def commit_and_mark(self, project_dir: Path, phase_key: str, status: str, *,
                        commit_message: str, tag: str | None = None,
                        detail: str = "", source_hash: str | None = None) -> None:
        self.commit_and_mark_fn(project_dir, phase_key, status, commit_message=commit_message,
                                tag=tag, detail=detail, source_hash=source_hash)


@dataclass(frozen=True, slots=True)
class RequirementsAdapter:
    task1_fn: Callable[[Task1Request], bool]
    validate_fn: Callable[[Path, Any], tuple[bool, str]]

    def task1(self, req: Task1Request) -> bool:
        return self.task1_fn(req)

    def validate_task1_output(self, req_path: Path, test_files: Any) -> tuple[bool, str]:
        return self.validate_fn(req_path, test_files)


@dataclass(frozen=True, slots=True)
class GeneratorAdapter:
    inner_loop_fn: Callable[..., bool]
    load_structure_fn: Callable[[Path, str], list[str]]
    validate_output_fn: Callable[..., tuple[bool, str]]
    validate_conformance_fn: Callable[[Path, str, Path], tuple[bool, str]]

    def task2_inner_loop(self, project_dir: Path, genesis_path: Path, functional_req_path: Path,
                         manifest_path: Path, existing_test_files: Any, project_profile: str,
                         workspace_structure: str, build_check_cmd: str | None, *,
                         locked_dep_context: str = "", current_module: str = "",
                         module_dir_rel: str = "", whole_project_context: str = "",
                         coverage_strategy: str = "", api_surface: str = "",
                         prev_source_context: str = "") -> bool:
        return self.inner_loop_fn(
            project_dir, genesis_path, functional_req_path, manifest_path,
            existing_test_files, project_profile, workspace_structure, build_check_cmd,
            locked_dep_context=locked_dep_context, current_module=current_module,
            module_dir_rel=module_dir_rel, whole_project_context=whole_project_context,
            coverage_strategy=coverage_strategy, api_surface=api_surface,
            prev_source_context=prev_source_context)

    def load_original_structure(self, project_dir: Path, module_name: str) -> list[str]:
        return self.load_structure_fn(project_dir, module_name)

    def validate_task2_output(self, project_dir: Path, expected_paths: list[str], *,
                              req_path: Path) -> tuple[bool, str]:
        return self.validate_output_fn(project_dir, expected_paths, req_path=req_path)

    def validate_api_conformance(self, project_dir: Path, module_name: str,
                                 module_dir: Path) -> tuple[bool, str]:
        return self.validate_conformance_fn(project_dir, module_name, module_dir)


@dataclass(frozen=True, slots=True)
class FixerAdapter:
    classify_fn: Callable[[str], str]
    log: Callable[[str], None]
    insert_no_run: Callable[[str], str]
    fix_err_cap: int

    def classify_failure(self, test_output: str) -> str:
        return self.classify_fn(test_output)

    def diagnose_no_tests(self, project_dir: Path, test_command: str, reason: str,
                          test_output: str) -> tuple[str, str]:
        """Reclassify a zero-tests-ran result: a failing ``--no-run`` means a build
        error (not a harness issue). Returns (failure_type, test_output)."""
        self.log(f"  WARNING: no_tests — {reason}. Running --no-run to diagnose.")
        no_run_cmd = self.insert_no_run(test_command)
        try:
            nr = subprocess.run(no_run_cmd, shell=True, cwd=str(project_dir),
                                capture_output=True, text=True, errors="replace", timeout=120)
            if nr.returncode != 0:
                self.log("  --no-run failed: build error causes zero tests.")
                return "build", test_output + (
                    f"\n\nDIAGNOSIS (--no-run): build failed — {nr.stderr[:self.fix_err_cap]}")
            return "no_tests", test_output + f"\n\nDIAGNOSIS: {reason}"
        except Exception as nr_err:
            self.log(f"  --no-run check failed: {nr_err}")
            return "no_tests", test_output + f"\n\nDIAGNOSIS: {reason}"


@dataclass(frozen=True, slots=True)
class AnalystAdapter:
    task4_fn: Callable[..., tuple[bool, int]]
    validate_fn: Callable[[str, str, str, str], tuple[bool, str]]
    genesis_audit_fn: Callable[[Path], bool]

    def task4(self, genesis_path: Path, functional_req_path: Path, test_output: str,
              iteration: int | str, output_dir: Path | str, *, failure_type: str = "test",
              failure_log: str = "", prev_pass_count: int = 0, prev_test_output: str = "",
              source_context: str = "", api_surface: str = "") -> tuple[bool, int]:
        return self.task4_fn(genesis_path, functional_req_path, test_output, iteration, output_dir,
                             failure_type=failure_type, failure_log=failure_log,
                             prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
                             source_context=source_context, api_surface=api_surface)

    def validate_task4_output(self, genesis_before: str, genesis_after: str,
                              test_output: str, failure_type: str) -> tuple[bool, str]:
        return self.validate_fn(genesis_before, genesis_after, test_output, failure_type)

    def genesis_audit(self, genesis_path: Path) -> bool:
        return self.genesis_audit_fn(genesis_path)


@dataclass(frozen=True, slots=True)
class CoverageDocsAdapter:
    regenerate_fn: Callable[..., str]

    def regenerate(self, project_dir: Path, test_files: Any, readme_text: str,
                   project_profile: str, clusters: Any) -> str:
        return self.regenerate_fn(project_dir, test_files, readme_text, project_profile, clusters)


@dataclass(frozen=True, slots=True)
class FlowAuditAdapter:
    run_fn: Callable[[str], None]

    def run(self, context: str) -> None:
        self.run_fn(context)


@dataclass(frozen=True, slots=True)
class DocsMetricsAdapter:
    model_notes_fn: Callable[[Path], list[str]]
    genesis_rules_count_fn: Callable[[Path, str], int]

    def model_notes(self, fr_path: Path) -> list[str]:
        return self.model_notes_fn(fr_path)

    def genesis_rules_count(self, project_dir: Path, module_scope: str) -> int:
        return self.genesis_rules_count_fn(project_dir, module_scope)
