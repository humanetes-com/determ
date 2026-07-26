"""Role-based ports for the phase-C fill engine use-cases (P2 / R3.3).

These are the DIP seams `application.reconstruct.fill_one_fn` and
`application.reconstruct.phase_c_engine` depend on. Each Protocol is a narrow
ROLE (ISP): a use-case receives only the roles it needs, never a god-deps.
`pipeline.py` functions satisfy them structurally at the composition root via the
focused role adapters in `adapters.fill_engine_roles` (one narrow adapter per
role, ISP); tests pass fakes.

Pure helpers the engine also uses (splice math, fill rules, cargo-error
parsing, the fix-loop driver, prompt builders) are NOT ports — they live in
inner layers (`domain`, `application.reconstruct.*`, `prompts`) the use-cases
import directly. Only I/O, the LLM call, the extractor, the retro store, and
`pipeline.py`-resident (P4-slated) helpers cross a port.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnFixContext,
    FnLocator,
    FnSite,
    ImplBlock,
    TypeRecord,
)
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.test_report import U2Verdict
from pipeline_core.domain.test_results import RegressionComparison, TestStateReading


@runtime_checkable
class EngineTelemetry(Protocol):
    """Human-facing log line, structured event, and the graceful-stop check."""

    def log(self, msg: str) -> None: ...
    def emit(self, event: str, *, duration_ms: int | None = ...,
             data: dict[str, Any] | None = ...) -> None: ...
    def check_stop(self, context: str) -> None: ...


@runtime_checkable
class Mutator(Protocol):
    """Source-tree mutation: locate a fn by record (byte offsets) or by name
    (regex fallback), splice a new body, swap a signature, and the per-tree
    splice+cargo lock."""

    def locate_fn_by_record(
        self, file_path: Path, name: str, container: str, sig_text: str,
        cfg_predicates: tuple[str, ...] | None = ...,
    ) -> tuple[int, int, int, str] | None: ...
    def locate_fn(
        self, text: str, name: str,
    ) -> tuple[str, int, int, int] | None: ...
    def splice_fn_body_at(
        self, file_path: Path, body_byte_start: int, body_byte_end: int,
        new_body: str, allow_nonstub: bool = ..., allow_stub_body: bool = ...,
        fn_name: str = ...,
    ) -> SpliceOutcome: ...
    def revert_body_scoped(
        self, file_path: Path, name: str, container: str, sig_text: str,
        pre_image: str, cfg_predicates: tuple[str, ...] | None = ...,
    ) -> SpliceOutcome: ...
    def splice_fn_body(
        self, file_path: Path, name: str, new_body: str,
        allow_nonstub: bool = ..., allow_stub_body: bool = ...,
    ) -> SpliceOutcome: ...
    def splice_body_scoped(
        self, file_path: Path, name: str, container: str, sig_text: str,
        new_body: str, cfg_predicates: tuple[str, ...] | None = ...,
        allow_nonstub: bool = ...,
    ) -> SpliceOutcome: ...
    def replace_fn_signature(
        self, file_path: Path, name: str, new_sig: str,
    ) -> bool: ...
    def extract_signature_block(self, response: str) -> str | None: ...
    def lock_for(self, tree: Path) -> threading.Lock: ...


@runtime_checkable
class Cargo(Protocol):
    """The cargo subprocess seam: check/test run, command building, the U2
    per-unit test gate, and rustc-error focusing. `env()` resolves the base
    cargo env once at composition time."""

    def run(self, cmd: str, cwd: Path, env: dict[str, str],
            timeout: int = ...) -> tuple[int, str]: ...
    def check_diag(self, cmd: str, cwd: Path, env: dict[str, str],
                   timeout: int = ...) -> tuple[int, str, list[Any]]: ...
    def build_test_cmd(
        self, test_stems: list[str], cluster: BuildCluster | None,
        *, include_lib: bool = ...,
    ) -> str: ...
    def u2_test_gate(
        self, fn_tests: set[str], unit_sites: frozenset[FnSite],
        test_cmd: str | None,
        target_src_dir: Path, cluster_env: dict[str, str] | None,
    ) -> U2Verdict: ...
    def test_regression_baseline(
        self, test_cmd: str | None, cwd: Path, env: dict[str, str] | None,
    ) -> TestStateReading | None: ...
    def test_regression_gate(
        self, test_cmd: str | None, cwd: Path, env: dict[str, str] | None,
        baseline: TestStateReading | None,
    ) -> tuple[RegressionComparison, str] | None: ...
    def focus_errors(self, output: str, file_rel: str,
                     max_chars: int | None = ...) -> str: ...
    def env(self) -> dict[str, str]: ...


@runtime_checkable
class PromptContext(Protocol):
    """The model call plus everything that feeds its prompt context: the static
    per-fn context (types/notes/where), sibling bodies, crate idioms, crate
    macros, cluster macro invariants, and the no_std invariant."""

    def ask_per_fn_body(
        self, prompt: str, fn_name: str, attempts_log: list[str],
        sig_text: str = ..., answer_request: object = ...,
    ) -> tuple[str | None, str]: ...
    def gather(
        self, *, file_text: str, file_rel: str, fn_name: str, sig_text: str,
        body_text: str, error_excerpt: str, container: str,
        type_records: list[TypeRecord], fr_path: Path | None = ...,
    ) -> FnFixContext: ...
    def collect_sibling_filled_bodies(
        self, file_text: str, target_fn_name: str, target_container: str,
    ) -> str: ...
    def extract_crate_idioms(
        self, impl_blocks: list[ImplBlock], file_rel: str,
        target_sig_text: str = ..., max_idioms: int = ...,
    ) -> str: ...
    def collect_crate_macros(self, src_dir: Path) -> str: ...
    def compute_cluster_macro_invariants(
        self, src_dir: Path, cluster: BuildCluster | None,
    ) -> str: ...
    def detect_crate_no_std_invariant(self, src_dir: Path) -> str: ...


@runtime_checkable
class WorkspaceModel(Protocol):
    """Read-only static model of the workspace source: stub scans, fn/type
    enumeration, impl-block grouping, container index, cluster membership, and
    the leaf-first ordering. The ONLY allowed source read (post-strip target
    tree), all via the syn extractor — no original source (Hard Rule 11)."""

    def scan_stub_records(
        self, target_src_dir: Path, cluster: BuildCluster | None,
    ) -> list[dict[str, Any]] | None: ...
    def scan_stub_tuples(
        self, target_src_dir: Path, cluster: BuildCluster | None,
    ) -> list[FnLocator]: ...
    def enumerate_fns(
        self, src_dir: Path,
    ) -> list[dict[str, Any]] | None: ...
    def enumerate_types(self, src_dir: Path) -> list[TypeRecord] | None: ...
    def collect_impl_blocks(self, src_dir: Path) -> list[ImplBlock] | None: ...
    def collect_impl_blocks_for_cluster(
        self, target_src_dir: Path, cluster: BuildCluster | None,
    ) -> list[ImplBlock] | None: ...
    def fn_container_index(
        self, src_dir: Path,
    ) -> dict[FnLocator, str]: ...
    def fn_belongs_to_cluster(
        self, target_src_dir: Path, file_rel: str, fn_name: str,
        cluster: BuildCluster, fn_cfg_predicates: list[str] | None = ...,
    ) -> bool: ...
    def cluster_filter_type_methods(
        self, records: list[TypeRecord], cluster: BuildCluster | None,
    ) -> list[TypeRecord]: ...
    def is_gen_tests_file(self, name: str) -> bool: ...
    def leaf_first_fn_order(
        self, tuples: list[FnLocator],
        leaf_name_counts: dict[str, int] | None,
    ) -> list[FnLocator]: ...


@runtime_checkable
class Retros(Protocol):
    """The cross-iteration retrospective store + the LLM-driven exhaustion
    diagnosis, and the model's own anti-hallucination notes."""

    def load(self, project_dir: Path) -> dict[str, Any]: ...
    def save(self, project_dir: Path, retros: dict[str, Any]) -> None: ...
    def retro_key(self, file_rel: str, fn_name: str, container: str) -> str: ...
    def is_fn_fully_exhausted(
        self, cache: dict[str, Any], file_rel: str, fn_name: str,
        container: str,
    ) -> bool: ...
    def diagnose(
        self, *, fn_name: str, sig_text: str, container: str, file_rel: str,
        cluster_name: str, last_body: str, error_excerpt: str,
        fix_attempts: int, prior_retrospective: str = ...,
        bound_methods_section: str = ...,
    ) -> dict[str, Any]: ...
    def append_note(
        self, fr_path: Path, note: str,
        error_codes: list[str] | set[str] | None = ...,
    ) -> bool: ...
    def extract_self_note(
        self, failed_body: str, error_excerpt: str, fixed_body: str,
    ) -> str | None: ...
    def record_error_codes(self, output: str) -> None: ...


@runtime_checkable
class TestLinker(Protocol):
    """Link fns to their tests, classify a pass's test failures, and journal
    each test-fix iteration."""

    def find_tests_for_fn(
        self, tests_dir_files: list[Path], fn_name: str, *,
        file_rel: str = ..., src_dir: Path | None = ..., container: str = ...,
    ) -> list[tuple[str, str]]: ...  # (path, content) tuples — NOT FnLocator
    def classify_failures(
        self, test_out: str, filled_this_pass: list[FnLocator],
        failed_tests: list[str], cluster_name: str,
    ) -> dict[str, dict[str, Any]]: ...
    def write_test_fix_log(
        self, *, project_dir: Path, cluster_name: str, pass_n: int,
        fix_iter: int, test_out: str, failed_tests: list[str],
        fns_retried: list[FnLocator], cur_fail_count: int,
        new_fail_count: int, best_fail_count: int, stall: int,
    ) -> None: ...


@runtime_checkable
class StallPolicy(Protocol):
    """The converge-exemption stall budget policy (PIPELINE_CONVERGE_MODULE)."""

    def effective_stalls(self, full_budget: int, module: str | None) -> int: ...
    def converge_in_cluster(
        self, cluster: BuildCluster | None,
    ) -> str | None: ...


@runtime_checkable
class ImplFillPath(Protocol):
    """The alternate per-impl-block fill path the engine delegates to when
    `PHASE_C_FILL_UNIT=impl` (default). Returns the impl-pass stats dict."""

    def fill_impl_blocks(
        self, *, project_dir: Path, target_src_dir: Path,
        tests_dir_files: list[Path], module_name: str, cluster_name: str,
        cluster: BuildCluster | None, check_cmd: str,
        cluster_env: dict[str, str], fr_path: Path | None,
        tdd_frontier_keys: set[FnLocator] | None,
        tdd_frontier_tests: FailureFrontier | None, tdd_test_cmd: str | None,
        parallelism: int = 1,
        crash_evidence: dict[FnLocator, str] | None = None,
    ) -> ClusterFillResult: ...
