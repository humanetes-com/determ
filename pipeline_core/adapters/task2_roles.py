"""Focused role adapters for the TASK-1/TASK-2 generators (P6).

These replace the `_Task2Adapter` god-object: one narrow adapter per
`ports.generator` / `ports.synthesizer` role (ISP), each constructor-injected
with the resident composition-root callables it delegates to. The engine bodies
(fill loops, cargo, gen_tests machinery, …) already live in `pipeline_core` or
are deps-bound shims the composition root owns, so the adapters wrap the resident
callables rather than re-home them. The only piece of real logic that lived ON
the god-adapter — the raw cargo subprocess in `run_raw` — moves into
`CargoBackendAdapter`.

Each adapter is a frozen dataclass; `pipeline.py` builds them at the composition
root and injects them into the generator/synthesizer factories. No adapter
imports `pipeline.py` (import-linter: adapters is inner relative to __main__).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.attribution import AttributionResult
from pipeline_core.domain.models import ClusterFillResult, FnLocator


# --------------------------------------------------------------------------- #
# ports.generator.Telemetry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GeneratorTelemetryAdapter:
    log_fn: Callable[[str], None]
    emit_fn: Callable[..., None]
    check_stop_fn: Callable[[str], None]
    set_current_fn: Callable[[Path, str], None]
    mark_phase_fn: Callable[..., None]
    phase_valid_fn: Callable[[Path, str], bool]

    def log(self, msg: str) -> None:
        self.log_fn(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: Any = None) -> None:
        if duration_ms is None:
            self.emit_fn(event, data=data)
        else:
            self.emit_fn(event, duration_ms=duration_ms, data=data)

    def check_stop(self, context: str) -> None:
        self.check_stop_fn(context)

    def set_current(self, project_dir: Path, label: str) -> None:
        self.set_current_fn(project_dir, label)

    def mark_phase(self, project_dir: Path, key: str, status: str,
                   detail: str = "") -> None:
        self.mark_phase_fn(project_dir, key, status, detail=detail)

    def phase_valid(self, project_dir: Path, key: str) -> bool:
        return self.phase_valid_fn(project_dir, key)


# --------------------------------------------------------------------------- #
# ports.generator.LLM + ports.synthesizer.Task1LLM
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LlmAskAdapter:
    ask_fn: Callable[..., str]

    def ask(self, prompt: str, *, model: str | None = None) -> str:
        return self.ask_fn(prompt, model=model)


# --------------------------------------------------------------------------- #
# ports.generator.Workspace
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class WorkspaceAdapter:
    read_fn: Callable[[Path], str]
    write_source_files_fn: Callable[..., int]
    load_manifest_fn: Callable[[Path], list[str]]
    save_manifest_fn: Callable[[Path, list[str]], None]
    load_original_structure_fn: Callable[[Path, str], Any]
    crate_entry_point_fn: Callable[[Path, str, set[str]], str | None]
    is_proc_macro_crate_fn: Callable[[str], bool]

    def read(self, path: Path) -> str:
        return self.read_fn(path)

    def write_source_files(self, files_dict: Any, project_dir: Path,
                           manifest_path: Path, existing_test_files: Any) -> Any:
        return self.write_source_files_fn(files_dict, project_dir, manifest_path,
                                          existing_test_files)

    def load_manifest(self, manifest_path: Path) -> list[str]:
        return self.load_manifest_fn(manifest_path)

    def save_manifest(self, manifest_path: Path, files: list[str]) -> None:
        self.save_manifest_fn(manifest_path, files)

    def load_original_structure(self, project_dir: Path, module_name: str) -> Any:
        return self.load_original_structure_fn(project_dir, module_name)

    def crate_entry_point(self, cargo_toml_path: Path, module_dir_rel: str,
                          file_path_set: set[str]) -> str | None:
        return self.crate_entry_point_fn(cargo_toml_path, module_dir_rel, file_path_set)

    def is_proc_macro_crate(self, cargo_toml_text: str) -> bool:
        return self.is_proc_macro_crate_fn(cargo_toml_text)


# --------------------------------------------------------------------------- #
# ports.generator.ContextDocs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ContextDocsAdapter:
    scan_downstream_api_fn: Callable[[Path, str], str]
    compile_patterns_read_fn: Callable[[Path, str], str]
    genesis_rules_read_fn: Callable[..., str]
    extract_test_assertions_fn: Callable[..., str]
    format_api_surface_section_fn: Callable[[str], str]
    split_api_surface_by_file_fn: Callable[[str], list[tuple[str, str]]]

    def scan_downstream_api(self, project_dir: Path, module_name: str) -> str:
        return self.scan_downstream_api_fn(project_dir, module_name)

    def compile_patterns_read(self, project_dir: Path, module: str) -> str:
        return self.compile_patterns_read_fn(project_dir, module)

    def genesis_rules_read(self, project_dir: Path, module_scope: str = "*") -> str:
        return self.genesis_rules_read_fn(project_dir, module_scope=module_scope)

    def extract_test_assertions(self, test_files: Any, module_dir_rel: str = "") -> str:
        return self.extract_test_assertions_fn(test_files, module_dir_rel)

    def format_api_surface_section(self, api_surface: str) -> str:
        return self.format_api_surface_section_fn(api_surface)

    def split_api_surface_by_file(self, api_surface: str) -> list[tuple[str, str]]:
        return self.split_api_surface_by_file_fn(api_surface)


# --------------------------------------------------------------------------- #
# ports.generator.Cargo
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CargoBackendAdapter:
    run_fn: Callable[..., tuple[int, str]]
    run_check_diag_fn: Callable[..., tuple[int, str, list[Any]]]
    build_test_cmd_fn: Callable[..., str]
    env_fn: Callable[[], dict[str, str]]
    implicated_fns_from_check_fn: Callable[
        [list[Any], str, Path], list[FnLocator]]
    attribute_failures_fn: Callable[..., AttributionResult]
    filter_test_targets_fn: Callable[[Any, Path], Any]

    def run_raw(self, cmd: list[str], cwd: Path, env: dict[str, str],
                timeout: int) -> tuple[int, str, str]:
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, env=env)
            return r.returncode, r.stdout or "", r.stderr or ""
        except subprocess.TimeoutExpired as e:
            # -1 = timeout sentinel (matches _run_cargo). Keep the partial
            # output — it names the hung test / the last compiling unit.
            def _s(c: object) -> str:
                if c is None:
                    return ""
                return c.decode("utf-8", errors="replace") if isinstance(c, bytes) else str(c)
            return -1, _s(e.stdout), _s(e.stderr) + f"\nTIMEOUT after {timeout}s"

    def run(self, cmd: str, cwd: Path, env: dict[str, str],
            timeout: int = 180) -> tuple[int, str]:
        return self.run_fn(cmd, cwd, env, timeout)

    def run_check_diag(self, cmd: str, cwd: Path, env: dict[str, str],
                       timeout: int = 180) -> tuple[int, str, list[Any]]:
        return self.run_check_diag_fn(cmd, cwd, env, timeout)

    def build_test_cmd(self, test_stems: list[str], cluster: Any,
                        *, include_lib: bool = False) -> str:
        return self.build_test_cmd_fn(test_stems, cluster, include_lib=include_lib)

    def env(self) -> dict[str, str]:
        return self.env_fn()

    def implicated_fns_from_check(self, diagnostics: list[Any],
                                  rendered_output: str,
                                  target_src_dir: Path) -> list[FnLocator]:
        return self.implicated_fns_from_check_fn(
            diagnostics, rendered_output, target_src_dir)

    def attribute_failures(self, test_output: str, src_dir: Path,
                           recently_filled: list[FnLocator],
                           test_reverse_map: dict[str, set[FnLocator]],
                           cluster_name: str) -> AttributionResult:
        res = self.attribute_failures_fn(test_output, src_dir, recently_filled,
                                         test_reverse_map, cluster_name)
        # Both projections go through the SAME oracle filter — a test fn left in
        # `convicting` could revert a unit for a key dispatch already dropped.
        return AttributionResult(
            self.filter_test_targets_fn(res.frontier, src_dir), res.kind,
            self.filter_test_targets_fn(res.convicting, src_dir))


# --------------------------------------------------------------------------- #
# ports.generator.FillEngine
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FillEngineAdapter:
    run_phase_c_tdd_fn: Callable[..., ClusterFillResult]
    fill_fns_one_by_one_fn: Callable[..., ClusterFillResult]
    fill_groups_fn: Callable[..., dict[str, Any]]
    group_tests_fn: Callable[..., list[dict[str, Any]]]
    cleanup_residuals_fn: Callable[..., dict[str, Any]]
    scan_unimplemented_fn: Callable[[Path], list[FnLocator]]
    merge_cluster_outputs_fn: Callable[..., dict[str, Any]]
    generate_fix_loop_fn: Callable[..., tuple[bool, str]]
    reset_exhaustion_flags_fn: Callable[[Path, str], object]  # return discarded
    build_test_reverse_map_fn: Callable[..., dict[str, set[FnLocator]]]

    def run_phase_c_tdd(self, **kw: Any) -> ClusterFillResult:
        return self.run_phase_c_tdd_fn(**kw)

    def fill_fns_one_by_one(self, **kw: Any) -> ClusterFillResult:
        return self.fill_fns_one_by_one_fn(**kw)

    def fill_groups(self, skeleton_dir: Path, target_src_dir: Path,
                    groups: list[dict[str, Any]], project_dir: Path,
                    **kw: Any) -> dict[str, Any]:
        return self.fill_groups_fn(skeleton_dir, target_src_dir, groups, project_dir, **kw)

    def group_tests(self, skeleton_dir: Path, test_files: list[tuple[str, str]],
                    **kw: Any) -> list[dict[str, Any]]:
        return self.group_tests_fn(skeleton_dir, test_files, **kw)

    def cleanup_residuals(self, target_src_dir: Path, project_dir: Path,
                          functional_req: str, test_files: list[tuple[str, str]],
                          **kw: Any) -> dict[str, Any]:
        return self.cleanup_residuals_fn(target_src_dir, project_dir, functional_req,
                                         test_files, **kw)

    def scan_unimplemented_functions(self, src_dir: Path) -> list[FnLocator]:
        return self.scan_unimplemented_fn(src_dir)

    def merge_cluster_outputs(self, **kw: Any) -> dict[str, Any]:
        return self.merge_cluster_outputs_fn(**kw)

    def generate_fix_loop(self, **kw: Any) -> tuple[bool, str]:
        return self.generate_fix_loop_fn(**kw)

    def reset_exhaustion_flags(self, project_dir: Path, reason: str) -> None:
        self.reset_exhaustion_flags_fn(project_dir, reason)

    def build_test_reverse_map(self, test_files: list[Path],
                               src_dir: Path) -> dict[str, set[FnLocator]]:
        return self.build_test_reverse_map_fn(test_files, src_dir)


# --------------------------------------------------------------------------- #
# ports.generator.Clusters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ClustersAdapter:
    load_baked_fn: Callable[[Path], list[Any]]
    load_baked_leaf_order_fn: Callable[[Path, str], dict[str, int]]
    filter_tests_for_cluster_fn: Callable[[Any, Any], list[tuple[str, str]]]
    cluster_cfg_attr_fn: Callable[[Any], str]

    def load_baked(self, project_dir: Path) -> list[Any]:
        return self.load_baked_fn(project_dir)

    def load_baked_leaf_order(self, project_dir: Path,
                              cluster_name: str) -> dict[str, int]:
        return self.load_baked_leaf_order_fn(project_dir, cluster_name)

    def filter_tests_for_cluster(self, test_files: list[tuple[str, str]],
                                 cluster: Any) -> list[tuple[str, str]]:
        return self.filter_tests_for_cluster_fn(test_files, cluster)

    def cluster_cfg_attr(self, cluster: Any) -> str:
        return self.cluster_cfg_attr_fn(cluster)


# --------------------------------------------------------------------------- #
# ports.generator.GenTests
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GenTestsAdapter:
    build_decl_fn: Callable[..., str]
    has_decl_fn: Callable[[str, int], bool]
    strip_decl_n_fn: Callable[[str, int], str]
    derive_prelude_fn: Callable[[Path], tuple[str, str]]
    load_origin_fn: Callable[[Path], dict[str, str]]
    propagate_fn: Callable[..., bool]
    is_gen_tests_file_fn: Callable[[str], bool]

    def build_decl(self, sibling_name: str, n: int | str, allow_line: str,
                   cluster_cfg_attr: str = "") -> str:
        return self.build_decl_fn(sibling_name, n, allow_line,
                                  cluster_cfg_attr=cluster_cfg_attr)

    def has_decl(self, src_text: str, pass_n: int) -> bool:
        return self.has_decl_fn(src_text, pass_n)

    def strip_decl_n(self, parent_text: str, pass_n: int) -> str:
        return self.strip_decl_n_fn(parent_text, pass_n)

    def derive_prelude(self, project_dir: Path) -> tuple[str, str]:
        return self.derive_prelude_fn(project_dir)

    def load_origin(self, project_dir: Path) -> dict[str, str]:
        return self.load_origin_fn(project_dir)

    def propagate_or_quarantine_sibling(self, gt: Path, origin_manifest: dict[str, str],
                                        parent_stem: str, pass_n: int,
                                        tag: str = "") -> bool:
        return self.propagate_fn(gt, origin_manifest, parent_stem, pass_n, tag)

    def is_gen_tests_file(self, name: str) -> bool:
        return self.is_gen_tests_file_fn(name)


# --------------------------------------------------------------------------- #
# ports.generator.SourceScan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SourceScanAdapter:
    rust_delimiters_balanced_fn: Callable[[str], bool]

    def rust_delimiters_balanced(self, text: str) -> bool:
        return self.rust_delimiters_balanced_fn(text)


# --------------------------------------------------------------------------- #
# ports.synthesizer.PromptShaping
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PromptShapingAdapter:
    prompt_budget_fn: Callable[..., int]
    format_test_files_fn: Callable[[Any], str]
    chunk_test_files_by_file_fn: Callable[[Any, int], list[Any]]
    format_api_surface_section_fn: Callable[[str], str]

    def prompt_budget(self, *fixed_parts: str) -> int:
        return self.prompt_budget_fn(*fixed_parts)

    def format_test_files(self, test_files: Any) -> str:
        return self.format_test_files_fn(test_files)

    def chunk_test_files_by_file(self, test_files: Any,
                                 file_char_limit: int) -> list[Any]:
        return self.chunk_test_files_by_file_fn(test_files, file_char_limit)

    def format_api_surface_section(self, api_surface: str) -> str:
        return self.format_api_surface_section_fn(api_surface)


# --------------------------------------------------------------------------- #
# ports.synthesizer.Progress
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ProgressFactoryAdapter:
    make_progress_fn: Callable[[int], Any]

    def make_progress(self, total: int) -> Any:
        return self.make_progress_fn(total)


# --------------------------------------------------------------------------- #
# ports.synthesizer.Requirements
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RequirementsDocAdapter:
    write_fr_preserving_notes_fn: Callable[[Path, str], None]
    type_signals_read_fn: Callable[[Path], str]

    def write_fr_preserving_notes(self, path: Path, new_content: str) -> None:
        self.write_fr_preserving_notes_fn(path, new_content)

    def type_signals_read(self, project_dir: Path) -> str:
        return self.type_signals_read_fn(project_dir)
