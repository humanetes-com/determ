"""Role-based ports for the phase-C fill engine use-cases.

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
`pipeline.py`-resident (still to be relocated) helpers cross a port.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.crash_site import CrashSites
from pipeline_core.domain.fill_schedule import UnitEvidence
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnFixContext,
    FnLocator,
    FnSite,
    ImplBlock,
    TypeRecord,
)
from pipeline_core.domain.reconstruction import FnLocation, SpliceOutcome
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.test_outcome import PremiseEntry
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
    ) -> FnLocation | None: ...
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
        allow_nonstub: bool = ..., allow_stub_body: bool = ...,
    ) -> SpliceOutcome: ...
    def replace_fn_signature(
        self, file_path: Path, name: str, new_sig: str,
    ) -> bool: ...
    def extract_signature_block(self, response: str) -> str | None: ...
    def lock_for(self, tree: Path) -> threading.Lock: ...
    def parse_validator(self) -> Callable[[Path], bool] | None:
        """The splice gate's parse check (the structure reader's `parse_ok`,
        `(path) -> parses?`), or None when no reader is wired (the fixture
        path). The value the transaction module's `validate_parse` takes:
        a writer that lands text outside the splicers — the deterministic
        import repair, a salvage image, a boundary read before a sweep —
        verifies through the same reader the splicers do."""
        ...


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
        baseline: TestStateReading | None = ...,
    ) -> U2Verdict: ...
    def test_regression_baseline(
        self, test_cmd: str | None, cwd: Path, env: dict[str, str] | None,
        raw_sink: list[str] | None = ...,
    ) -> TestStateReading | None: ...
    def test_regression_gate(
        self, test_cmd: str | None, cwd: Path, env: dict[str, str] | None,
        baseline: TestStateReading | None, *, stub_free: bool = False,
        introduced_stubs: bool = False, hollow_before: int | None = None,
        written: Collection[FnLocator] = (),
    ) -> tuple[RegressionComparison, str] | None: ...
    def focus_errors(self, output: str, file_rel: str,
                     max_chars: int | None = ...) -> str: ...
    def env(self) -> dict[str, str]: ...


@runtime_checkable
class PromptContext(Protocol):
    """The model call plus everything that feeds its prompt context: the static
    per-fn context (types/notes/where), sibling bodies, crate idioms, crate
    macros, cluster macro invariants, and the no_std invariant."""

    def open_conversation(
        self, label: str, *, route: str = ..., sources: object = ...,
        max_pulls: int = ...,
    ) -> Any: ...
    def ask_per_fn_body(
        self, turn: object, fn_name: str, attempts_log: list[str], *,
        conversation: Any, sig_text: str = ...,
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
    tree), all via the syn extractor — never the original, pre-strip source."""

    def scan_stub_records(
        self, target_src_dir: Path, cluster: BuildCluster | None,
    ) -> list[dict[str, Any]] | None: ...
    def scan_stub_tuples(
        self, target_src_dir: Path, cluster: BuildCluster | None,
    ) -> StubInventory: ...
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
    def cluster_dead_sites(
        self, target_src_dir: Path, cluster: BuildCluster | None,
    ) -> CfgDeadSites: ...
    def cluster_filter_type_members(
        self, records: list[TypeRecord], cluster: BuildCluster | None,
    ) -> list[TypeRecord]: ...
    def all_clusters(self) -> list[BuildCluster]: ...
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
        pinned_observables: str = ...,
        compiled: bool = ...,
        later_compile_failures: int = ...,
    ) -> dict[str, Any]: ...
    def append_note(
        self, fr_path: Path, note: str,
        error_codes: list[str] | set[str] | None = ...,
        provenance: Any | None = ...,
    ) -> bool: ...
    def extract_self_note(
        self, failed_body: str, error_excerpt: str, fixed_body: str,
    ) -> str | None: ...
    def record_error_codes(self, output: str) -> None: ...
    def retire_disproved_notes(
        self, fr_path: Path, unit: str, error_codes: frozenset[str],
        shown: str,
    ) -> int: ...


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
    """The per-impl-block fill path the engine delegates to when
    `PHASE_C_FILL_UNIT=impl` (default). `prepare_impl_fill` returns the
    four impl-path routes for the fill schedule; `fill_impl_blocks` is the
    legacy single call (prepare, execute under the schedule, finish)."""

    def prepare_impl_fill(self, **kw: Any) -> Any: ...

    def fill_impl_blocks(
        self, *, project_dir: Path, target_src_dir: Path,
        tests_dir_files: list[Path], module_name: str, cluster_name: str,
        cluster: BuildCluster | None, check_cmd: str,
        cluster_env: dict[str, str], fr_path: Path | None,
        tdd_frontier_keys: set[FnLocator] | None,
        escalated_keys: frozenset[FnLocator] = ...,
        escalated_measured: frozenset[FnLocator] = ...,
        measured_owner_keys: frozenset[FnLocator] = ...,
        ledger_entries: Any = ...,
        oracle_fp: str = ...,
        tdd_frontier_tests: FailureFrontier | None, tdd_test_cmd: str | None,
        parallelism: int = 1,
        crash_evidence: dict[FnLocator, str] | None = None,
        crash_blamed: frozenset[FnLocator] | None = None,
        crash_blocked: dict[str, str] | None = None,
        behaviour_specs: Sequence[BehaviourSpec] = ...,
        dispatch_ranks: dict[FnLocator, int] | None = ...,
        session_slices: Mapping[str, frozenset[FnSite]] | None = ...,
        session_owners: Mapping[str, frozenset[FnSite]] | None = ...,
        on_wave_kept: Callable[[frozenset[FnLocator]], object] | None = ...,
        fill_premise: Mapping[str, Any] | None = ...,
        fill_ground_now: Callable[[], Mapping[str, Any] | None] | None = ...,
    ) -> ClusterFillResult: ...


class RegressionGatePort(Protocol):
    """The global non-regression gate as every fill route calls it
    (`regression_guard.gate` bound by the composition root). Typed so a
    keyword a route passes and the gate does not take fails mypy, not the
    run: ``written`` arrived 2026-10-16 with the crash settlement
    (`crash_settler`), which needs the units the caller's write put on the
    tree."""

    def __call__(
        self, test_cmd: str | None, cwd: Path, env: dict[str, str] | None,
        baseline: TestStateReading | None, /, *, stub_free: bool = ...,
        introduced_stubs: bool = ..., hollow_before: int | None = ...,
        written: Collection[FnLocator] = ...,
    ) -> tuple[RegressionComparison, str] | None: ...


class LocalizeCrashesPort(Protocol):
    """`crash_localizer.localize_crashes` bound to its adapters by the
    composition root: ``(capture, test command, tree, env) -> sites``.
    ``exclude`` names the native crashes (`crash_localizer.crash_key`) the
    caller already saw before the change it judges."""

    def __call__(
        self, sweep_output: str, test_cmd: str, workspace_root: Path,
        env: dict[str, str], /, *, exclude: Collection[str] = ...,
    ) -> tuple[Any, ...]: ...


class FillPassPort(Protocol):
    """One fill pass of one cluster — what the TDD driver calls once per
    iteration (`PhaseCTddDeps.task2_fill_fns_one_by_one`).

    The driver passes some fifty keywords and grows them with every schedule
    input. While the port was `Callable[..., ClusterFillResult]` and the
    composition bound it through `lambda *a, **k`, no checker saw a keyword
    the engine does not take: run `486681-a304b30a` (2026-10-10) passed
    `due_rungs=` to an engine without it, and both clusters ended at their
    first fill with a TypeError, before any model call. Typed here, mypy reads
    every driver call against this signature, and
    `phase_c_engine.engine_run_is_the_fill_pass` binds the engine's `run` to
    it, so a keyword missing on either side fails the gate. Keyword-only: the
    driver names every argument."""

    def __call__(
        self,
        *,
        project_dir: Path,
        target_src_dir: Path,
        tests_dir_files: list[Path],
        module_name: str,
        cluster_name: str,
        cluster: BuildCluster | None = ...,
        check_cmd: str = ...,
        cluster_env: dict[str, str] | None = ...,
        max_passes: int = ...,
        leaf_name_counts: dict[str, int] | None = ...,
        fr_path: Path | None = ...,
        tdd_frontier_keys: set[FnLocator] | None = ...,
        skip_end_pass_test: bool = ...,
        tdd_frontier_tests: FailureFrontier | None = ...,
        escalated_keys: frozenset[FnLocator] = ...,
        escalated_measured: frozenset[FnLocator] = ...,
        measured_owner_keys: frozenset[FnLocator] = ...,
        ledger_entries: Sequence[Any] = ...,
        oracle_fp: str = ...,
        tdd_test_cmd: str | None = ...,
        crash_evidence: dict[FnLocator, str] | None = ...,
        crash_blamed: frozenset[FnLocator] | None = ...,
        crash_memory_blamed: frozenset[FnLocator] | None = ...,
        crash_sites: CrashSites | None = ...,
        crash_blocked: dict[str, str] | None = ...,
        culprits_of: Callable[[str], Collection[FnLocator]] | None = ...,
        unmeasurable_tests: frozenset[str] = ...,
        attributed: Collection[FnLocator] = ...,
        owner_admitted: Collection[FnLocator] = ...,
        owner_ordered: Collection[FnLocator] = ...,
        owner_batch: Collection[FnLocator] = ...,
        owner_starved: Collection[FnLocator] = ...,
        due: Collection[FnLocator] = ...,
        due_rungs: Mapping[FnLocator, str] | None = ...,
        unit_evidence: UnitEvidence | None = ...,
        body_rows: int | None = ...,
        dispatch_ranks: dict[FnLocator, int] | None = ...,
        session_slices: Mapping[str, frozenset[FnSite]] | None = ...,
        session_owners: Mapping[str, frozenset[FnSite]] | None = ...,
        stub_hold_blockers: frozenset[FnLocator] = ...,
        stub_hold_hollow: Mapping[str, frozenset[FnLocator]] | None = ...,
        fill_budget_minutes: float = ...,
        fill_iteration: int = ...,
        run_id: str = ...,
        max_sessions_per_fill: int = ...,
        session_min_turns: int = ...,
        unit_share_multiple: float = ...,
        heal_file: Callable[[str], bool] | None = ...,
        on_wave_kept: Callable[[frozenset[FnLocator]], object] | None = ...,
        fill_premise: Mapping[str, PremiseEntry] | None = ...,
        fill_ground_now: Callable[[], Mapping[str, PremiseEntry] | None] | None = ...,
    ) -> ClusterFillResult: ...
