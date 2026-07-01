"""Role-based ports for the TASK-2 source generators (P4).

The `application.generators.task2_generator` use-cases depend on these narrow
ROLE Protocols (ISP) instead of reaching into `pipeline.py` globals. `pipeline.py`
binds the concrete functions at the composition root via the focused role
adapters in `adapters.task2_roles` (one narrow adapter per role, ISP); tests
pass fakes.

Pure helpers the generators also use (prompt-section text, file extraction,
truncation, API-surface/topo parsing) are NOT ports — they live in inner layers
(`domain`, `prompts`) the use-cases import directly. Only I/O, the LLM call, the
subprocess/extractor, and `pipeline.py`-resident orchestration cross a port.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..domain.models import BuildCluster, Diagnostic, FnLocator, Frontier


@runtime_checkable
class Telemetry(Protocol):
    """Human-facing log line, structured event, graceful-stop check, and the
    resumable phase markers."""

    def log(self, msg: str) -> None: ...
    def emit(self, event: str, *, duration_ms: int | None = ...,
             data: dict[str, Any] | None = ...) -> None: ...
    def check_stop(self, context: str) -> None: ...
    def set_current(self, project_dir: Path, label: str) -> None: ...
    def mark_phase(self, project_dir: Path, key: str, status: str,
                   detail: str = ...) -> None: ...
    def phase_valid(self, project_dir: Path, key: str) -> bool: ...


@runtime_checkable
class LLM(Protocol):
    """The self-hosted model call (Hard Rule 7: local only)."""

    def ask(self, prompt: str, *, model: str | None = ...) -> str: ...


@runtime_checkable
class Workspace(Protocol):
    """Read/write the workspace tree + manifest + saved structure, and the
    cheap Cargo.toml-shape queries the generators consult."""

    def read(self, path: Path) -> str: ...
    def write_source_files(
        self, files_dict: dict[str, str], project_dir: Path, manifest_path: Path,
        existing_test_files: Any,
    ) -> int: ...
    def load_manifest(self, manifest_path: Path) -> list[str]: ...
    def save_manifest(self, manifest_path: Path, files: list[str]) -> None: ...
    def load_original_structure(
        self, project_dir: Path, module_name: str,
    ) -> list[str]: ...
    def crate_entry_point(
        self, cargo_toml_path: Path, module_dir_rel: str, file_path_set: set[str],
    ) -> str | None: ...
    def is_proc_macro_crate(self, cargo_toml_text: str) -> bool: ...


@runtime_checkable
class ContextDocs(Protocol):
    """The per-module prompt-context documents the generators inject: downstream
    API, known compile pitfalls, structured genesis rules, test assertions, and
    the pre-strip API surface."""

    def scan_downstream_api(self, project_dir: Path, module_name: str) -> str: ...
    def compile_patterns_read(self, project_dir: Path, module: str) -> str: ...
    def genesis_rules_read(
        self, project_dir: Path, module_scope: str = ...,
    ) -> str: ...
    def extract_test_assertions(
        self, test_files: list[tuple[str, str]], module_dir_rel: str = ...,
    ) -> str: ...
    def format_api_surface_section(self, api_surface: str) -> str: ...
    def split_api_surface_by_file(
        self, api_surface: str,
    ) -> list[tuple[str, str]]: ...


# --- Ports for the 4-phase StaticSkeletonTask2Generator (P4 slice 4) ---------
# The skeleton generator orchestrates cargo, the LLM fill engines, the baked
# cluster artefacts, the gen_tests sibling machinery, and the source scanners —
# all `pipeline.py`-resident or adapter-layer. Each crosses one of the roles
# below; the focused `adapters.task2_roles` adapters bind the concrete functions
# at the composition root (one narrow adapter per role).


@runtime_checkable
class Cargo(Protocol):
    """The cargo subprocess + its error-attribution helpers."""

    def run_raw(
        self, cmd: list[str], cwd: Path, env: dict[str, str], timeout: int,
    ) -> tuple[int, str, str]: ...
    def run(
        self, cmd: str, cwd: Path, env: dict[str, str], timeout: int = ...,
    ) -> tuple[int, str]: ...
    def run_check_diag(
        self, cmd: str, cwd: Path, env: dict[str, str], timeout: int = ...,
    ) -> tuple[int, str, list[Diagnostic]]: ...
    def build_test_cmd(
        self, test_stems: list[str], cluster: BuildCluster | None,
    ) -> str: ...
    def env(self) -> dict[str, str]: ...
    def implicated_fns_from_cargo_errors(
        self, cargo_output: str, target_src_dir: Path,
    ) -> list[FnLocator]: ...
    def attribute_failures(
        self, test_output: str, src_dir: Path,
        recently_filled: list[FnLocator],
        test_reverse_map: dict[str, set[FnLocator]], cluster_name: str,
    ) -> tuple[Frontier, str]: ...


@runtime_checkable
class FillEngine(Protocol):
    """The LLM-driven Phase B/C/D fill engines + the cross-cluster merge and the
    shared fix-loop (all reach the model — Hard Rule 7: local only)."""

    def run_phase_c_tdd(
        self, *, project_dir: Path, target_src_dir: Path,
        tests_dir_files: list[Path], cluster_tests: list[tuple[str, str]],
        module: str, cluster: BuildCluster, check_cmd: str,
        cluster_env: dict[str, str], leaf_counts: dict[str, int] | None,
        fr_path: Path | None, test_cmd_builder: Any, tag: str,
    ) -> dict[str, Any]: ...
    def fill_fns_one_by_one(
        self, *, project_dir: Path, target_src_dir: Path,
        tests_dir_files: list[Path], module_name: str, cluster_name: str,
        cluster: BuildCluster | None = ..., check_cmd: str = ...,
        cluster_env: dict[str, str] | None = ..., max_passes: int = ...,
        leaf_name_counts: dict[str, int] | None = ..., fr_path: Path | None = ...,
        tdd_frontier_keys: set[FnLocator] | None = ...,
        skip_end_pass_test: bool = ..., tdd_frontier_tests: Frontier | None = ...,
        tdd_test_cmd: str | None = ...,
    ) -> dict[str, Any]: ...
    def fill_groups(
        self, skeleton_dir: Path, target_src_dir: Path, groups: list[dict[str, Any]],
        project_dir: Path, *, cluster_name: str, module_name: str,
        functional_req: str, genesis: str, cluster: BuildCluster | None = ...,
    ) -> dict[str, Any]: ...
    def group_tests(
        self, skeleton_dir: Path, test_files: list[tuple[str, str]], *,
        cluster_name: str, module_name: str,
    ) -> list[dict[str, Any]]: ...
    def cleanup_residuals(
        self, target_src_dir: Path, project_dir: Path, functional_req: str,
        test_files: list[tuple[str, str]], *, module_name: str,
    ) -> dict[str, Any]: ...
    def scan_unimplemented_functions(
        self, src_dir: Path,
    ) -> list[FnLocator]: ...
    def merge_cluster_outputs(
        self, *, project_dir: Path, cluster_specs: dict[str, BuildCluster],
        successes: list[dict[str, Any]],
    ) -> dict[str, Any]: ...
    def generate_fix_loop(
        self, *, project_dir: Path, check_cmd: str, source_paths: list[Path],
        existing_test_files: set[str], model: str | None = ...,
        max_fix_attempts: int = ..., timeout: int = ...,
    ) -> tuple[bool, str]: ...
    def reset_exhaustion_flags(self, project_dir: Path, reason: str) -> None: ...
    def build_test_reverse_map(
        self, test_files: list[Path], src_dir: Path,
    ) -> dict[str, set[FnLocator]]: ...


@runtime_checkable
class Clusters(Protocol):
    """The baked pre-strip cluster artefacts + cluster-scoped test filtering."""

    def load_baked(self, project_dir: Path) -> list[BuildCluster]: ...
    def load_baked_leaf_order(
        self, project_dir: Path, cluster_name: str,
    ) -> dict[str, int]: ...
    def filter_tests_for_cluster(
        self, test_files: list[tuple[str, str]], cluster: BuildCluster | None,
    ) -> list[tuple[str, str]]: ...
    def cluster_cfg_attr(self, cluster: BuildCluster) -> str: ...


@runtime_checkable
class GenTests(Protocol):
    """The `*.gen_tests_<N>.rs` sibling-declaration machinery + its origin
    manifest (which sibling belongs to which cluster cfg)."""

    def build_decl(
        self, sibling_name: str, n: int | str, allow_line: str,
        cluster_cfg_attr: str = ...,
    ) -> str: ...
    def has_decl(self, src_text: str, pass_n: int) -> bool: ...
    def strip_decl_n(self, parent_text: str, pass_n: int) -> str: ...
    def derive_prelude(self, project_dir: Path) -> tuple[str, str]: ...
    def load_origin(self, project_dir: Path) -> dict[str, str]: ...
    def propagate_or_quarantine_sibling(
        self, gt: Path, origin_manifest: dict[str, str], parent_stem: str,
        pass_n: int, tag: str = ...,
    ) -> bool: ...
    def is_gen_tests_file(self, name: str) -> bool: ...


@runtime_checkable
class SourceScan(Protocol):
    """Pure structural source scanners (`pipeline.py`-resident syn-backed)."""

    def locate_fn(
        self, file_text: str, fn_name: str,
    ) -> tuple[str, int, int, int] | None: ...
    def rust_delimiters_balanced(self, text: str) -> bool: ...
