"""Focused role adapters for the phase-C fill engine use-cases (P6).

These replace the `_PhaseCFillAdapter` god-object: one narrow adapter per
`ports.fill_engine` role (ISP), each constructor-injected with the resident
composition-root callables it delegates to (via late-binding lambdas at the
composition root, so the scripted-cargo / scripted-model test monkeypatches still
intercept at call time). The engine bodies already live in
`application.reconstruct.*`; these adapters are the thin DIP seam between those
use-cases and the still-resident pipeline.py helpers.

The one structural difference from the other role-adapter families: the
`WorkspaceModelAdapter` carries the constructor-injected
:class:`ports.structure.StructureReader` (the P1 DI seam) for `enumerate_fns` /
`enumerate_types`; tests inject a fake reader via ``dataclasses.replace`` at the
`_build_phase_c_engine(structure=…)` seam.

No adapter imports `pipeline.py` (import-linter: adapters is inner relative to
__main__).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from pipeline_core.domain.models import (
    ClusterFillResult,
    FnFixContext,
    FnLocator,
    FnSite,
)
from pipeline_core.domain.reconstruction import FnLocation, SpliceOutcome
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.test_report import U2Verdict
from pipeline_core.domain.test_results import RegressionComparison, TestStateReading
from pipeline_core.ports.structure import StructureReader


# --------------------------------------------------------------------------- #
# ports.fill_engine.EngineTelemetry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class EngineTelemetryAdapter:
    log_fn: Callable[[str], None]
    emit_fn: Callable[..., None]
    check_stop_fn: Callable[[str], None]

    def log(self, msg: str) -> None:
        self.log_fn(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None:
        if duration_ms is None:
            self.emit_fn(event, data=data)
        else:
            self.emit_fn(event, duration_ms=duration_ms, data=data)

    def check_stop(self, context: str) -> None:
        self.check_stop_fn(context)


# --------------------------------------------------------------------------- #
# ports.fill_engine.Mutator
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class MutatorAdapter:
    locate_fn_by_record_fn: Callable[..., FnLocation | None]
    locate_fn_fn: Callable[[str, str], tuple[str, int, int, int] | None]
    splice_fn_body_at_fn: Callable[..., SpliceOutcome]
    splice_fn_body_fn: Callable[..., SpliceOutcome]
    replace_fn_signature_fn: Callable[[Path, str, str], bool]
    extract_signature_block_fn: Callable[[str], str | None]
    lock_for_fn: Callable[[Path], Lock]
    # The structure reader's parse check the splice functions above are
    # bound to; None when composed without a reader.
    parse_validator_fn: Callable[[Path], bool] | None = None

    def parse_validator(self) -> Callable[[Path], bool] | None:
        return self.parse_validator_fn

    def locate_fn_by_record(self, file_path: Path, name: str, container: str,
                            sig_text: str,
                            cfg_predicates: tuple[str, ...] | None = None
                            ) -> FnLocation | None:
        return self.locate_fn_by_record_fn(file_path, name, container, sig_text,
                                           cfg_predicates)

    def locate_fn(self, text: str, name: str) -> tuple[str, int, int, int] | None:
        return self.locate_fn_fn(text, name)

    def splice_fn_body_at(self, file_path: Path, body_byte_start: int,
                          body_byte_end: int, new_body: str,
                          allow_nonstub: bool = False,
                          allow_stub_body: bool = False,
                          fn_name: str = "") -> SpliceOutcome:
        # `fn_name` is what lets the splice recognise a reply that restated the
        # WHOLE item instead of just the body. Omitting it on a MODEL write
        # silently disables that check: the wrapper is spliced verbatim, the
        # outer body ends up empty, and the fn reads as filled forever.
        return self.splice_fn_body_at_fn(file_path, body_byte_start, body_byte_end,
                                         new_body, allow_nonstub=allow_nonstub,
                                         allow_stub_body=allow_stub_body,
                                         fn_name=fn_name)

    def revert_body_scoped(self, file_path: Path, name: str, container: str,
                           sig_text: str, pre_image: str,
                           cfg_predicates: tuple[str, ...] | None = None,
                           ) -> SpliceOutcome:
        """Undo THIS fn's own edit by writing `pre_image` back at offsets
        re-resolved against the file as it stands now.

        Never restore a whole-file snapshot to revert one fn: under
        parallelism the snapshot is older than the sibling fills that landed
        in the same file meanwhile, so writing it back erases work that has
        already been reported as filled. Reverting is a per-fn operation and
        `pre_image` may legally be a stub.

        The record locator runs FIRST even for a FREE fn (empty container):
        it indexes only top-level/impl fns, so a failed splice that nested a
        duplicate same-name definition inside the body still resolves to the
        one outer fn — exactly the state where the bare-name fallback refuses
        `ambiguous_name` and a broken body would otherwise stay in the tree,
        failing every sibling's compile until something else overwrites it.
        Gated on having a disambiguator (container or sig): with neither, the
        record locator guesses among same-name candidates instead of refusing,
        and a revert must never guess."""
        loc = (self.locate_fn_by_record(file_path, name, container, sig_text,
                                        cfg_predicates)
               if (container or sig_text) else None)
        if loc is not None:
            return self.splice_fn_body_at(
                file_path, loc.body_byte_start, loc.body_byte_end, pre_image,
                allow_nonstub=True, allow_stub_body=True)
        # Same record-locator-then-bare-name ladder as splice_body_scoped: the
        # extractor binary is absent on the fixture path, and a revert that
        # silently does nothing leaves the failed body in place, so the next
        # attempt's splice is refused `target_not_stub`.
        return self.splice_fn_body(file_path, name, pre_image,
                                   allow_nonstub=True, allow_stub_body=True)

    def splice_fn_body(self, file_path: Path, name: str, new_body: str,
                       allow_nonstub: bool = False,
                       allow_stub_body: bool = False) -> SpliceOutcome:
        return self.splice_fn_body_fn(file_path, name, new_body,
                                      allow_nonstub=allow_nonstub,
                                      allow_stub_body=allow_stub_body)

    def splice_body_scoped(self, file_path: Path, name: str, container: str,
                           sig_text: str, new_body: str,
                           cfg_predicates: tuple[str, ...] | None = None,
                           allow_nonstub: bool = False,
                           allow_stub_body: bool = False) -> SpliceOutcome:
        """Container-scoped body splice — the collision-safe entry point.

        When ``container`` is known, locate the fn by (name, container, sig,
        cfg) via the syn record and splice at its exact byte offsets, so a
        same-named fn in a different impl is never touched. Only when no
        container is available does it fall back to the bare-name splice, which
        itself refuses (``ambiguous_name``) if the name is not unique. Either
        way a body meant for one impl can never land in another. A FREE fn
        (empty container) with a known signature also routes through the
        record locator — the bare-name path refuses `ambiguous_name` when a
        broken prior splice left a nested same-name definition, and the
        annotate-stub write must still land on the outer fn then."""
        if container or sig_text:
            loc = self.locate_fn_by_record(file_path, name, container, sig_text,
                                           cfg_predicates)
            if loc is not None:
                return self.splice_fn_body_at(file_path, loc.body_byte_start,
                                              loc.body_byte_end,
                                              new_body, allow_nonstub=allow_nonstub,
                                              allow_stub_body=allow_stub_body,
                                              fn_name=name)
        return self.splice_fn_body(file_path, name, new_body,
                                   allow_nonstub=allow_nonstub,
                                   allow_stub_body=allow_stub_body)

    def replace_fn_signature(self, file_path: Path, name: str, new_sig: str) -> bool:
        return self.replace_fn_signature_fn(file_path, name, new_sig)

    def extract_signature_block(self, response: str) -> str | None:
        return self.extract_signature_block_fn(response)

    def lock_for(self, tree: Path) -> Lock:
        return self.lock_for_fn(tree)


# --------------------------------------------------------------------------- #
# ports.fill_engine.Cargo
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CargoFillAdapter:
    run_fn: Callable[..., tuple[int, str]]
    build_test_cmd_fn: Callable[..., str]
    u2_test_gate_fn: Callable[..., U2Verdict]
    focus_errors_fn: Callable[..., str]
    env_fn: Callable[[], dict[str, str]]
    test_regression_baseline_fn: Callable[..., TestStateReading | None] | None = None
    test_regression_gate_fn: Callable[..., tuple[RegressionComparison, str] | None] | None = None
    check_diag_fn: Callable[..., tuple[int, str, list[Any]]] | None = None

    def run(self, cmd: str, cwd: Path, env: dict[str, str],
            timeout: int = 180) -> tuple[int, str]:
        return self.run_fn(cmd, cwd, env, timeout)

    def check_diag(self, cmd: str, cwd: Path, env: dict[str, str],
                   timeout: int = 180) -> tuple[int, str, list[Any]]:
        if self.check_diag_fn is None:
            # Diagnostics-less fallback (harnesses that bind only run_fn).
            rc, out = self.run_fn(cmd, cwd, env, timeout)
            return rc, out, []
        return self.check_diag_fn(cmd, cwd, env, timeout)

    def build_test_cmd(self, test_stems: list[str], cluster: Any,
                        *, include_lib: bool = False) -> str:
        return self.build_test_cmd_fn(test_stems, cluster, include_lib=include_lib)

    def u2_test_gate(self, fn_tests: set[str], unit_sites: frozenset[FnSite],
                     test_cmd: str | None,
                     target_src_dir: Path, cluster_env: dict[str, str] | None,
                     baseline: TestStateReading | None = None
                     ) -> U2Verdict:
        return self.u2_test_gate_fn(fn_tests, unit_sites, test_cmd,
                                    target_src_dir, cluster_env,
                                    baseline=baseline)

    def test_regression_baseline(self, test_cmd: str | None, cwd: Path,
                                 env: dict[str, str] | None,
                                 raw_sink: list[str] | None = None,
                                 ) -> TestStateReading | None:
        if self.test_regression_baseline_fn is None:
            return None
        return self.test_regression_baseline_fn(test_cmd, cwd, env, raw_sink)

    def test_regression_gate(self, test_cmd: str | None, cwd: Path,
                             env: dict[str, str] | None,
                             baseline: TestStateReading | None,
                             *, stub_free: bool = False,
                             introduced_stubs: bool = False,
                             hollow_before: int | None = None,
                             ) -> tuple[RegressionComparison, str] | None:
        if self.test_regression_gate_fn is None:
            return None
        return self.test_regression_gate_fn(test_cmd, cwd, env, baseline,
                                            stub_free=stub_free,
                                            introduced_stubs=introduced_stubs,
                                            hollow_before=hollow_before)

    def focus_errors(self, output: str, file_rel: str,
                     max_chars: int | None = None) -> str:
        return self.focus_errors_fn(output, file_rel, max_chars=max_chars)

    def env(self) -> dict[str, str]:
        return self.env_fn()


# --------------------------------------------------------------------------- #
# ports.fill_engine.PromptContext
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PromptContextAdapter:
    ask_per_fn_body_fn: Callable[..., tuple[str | None, str]]
    gather_fn: Callable[..., FnFixContext]
    collect_sibling_filled_bodies_fn: Callable[[str, str, str], str]
    extract_crate_idioms_fn: Callable[..., str]
    collect_crate_macros_fn: Callable[[Path], str]
    compute_cluster_macro_invariants_fn: Callable[[Path, Any], str]
    detect_crate_no_std_invariant_fn: Callable[[Path], str]

    def ask_per_fn_body(self, prompt: str, fn_name: str, attempts_log: list[str],
                        sig_text: str = "",
                        answer_request: object = None) -> tuple[str | None, str]:
        return self.ask_per_fn_body_fn(prompt, fn_name, attempts_log,
                                       sig_text=sig_text,
                                       answer_request=answer_request)

    def gather(self, *, file_text: str, file_rel: str, fn_name: str, sig_text: str,
               body_text: str, error_excerpt: str, container: str,
               type_records: list[Any], fr_path: Path | None = None) -> FnFixContext:
        return self.gather_fn(
            file_text=file_text, file_rel=file_rel, fn_name=fn_name,
            sig_text=sig_text, body_text=body_text, error_excerpt=error_excerpt,
            container=container, type_records=type_records, fr_path=fr_path)

    def collect_sibling_filled_bodies(self, file_text: str, target_fn_name: str,
                                      target_container: str) -> str:
        return self.collect_sibling_filled_bodies_fn(file_text, target_fn_name,
                                                     target_container)

    def extract_crate_idioms(self, impl_blocks: list[Any], file_rel: str,
                             target_sig_text: str = "", max_idioms: int = 5) -> str:
        return self.extract_crate_idioms_fn(impl_blocks, file_rel,
                                            target_sig_text=target_sig_text,
                                            max_idioms=max_idioms)

    def collect_crate_macros(self, src_dir: Path) -> str:
        return self.collect_crate_macros_fn(src_dir)

    def compute_cluster_macro_invariants(self, src_dir: Path, cluster: Any) -> str:
        return self.compute_cluster_macro_invariants_fn(src_dir, cluster)

    def detect_crate_no_std_invariant(self, src_dir: Path) -> str:
        return self.detect_crate_no_std_invariant_fn(src_dir)


# --------------------------------------------------------------------------- #
# ports.fill_engine.WorkspaceModel  (carries the StructureReader DI seam)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class WorkspaceModelAdapter:
    structure: StructureReader
    scan_stub_records_fn: Callable[[Path, Any], list[dict[str, Any]] | None]
    scan_stub_tuples_fn: Callable[[Path, Any], StubInventory]
    collect_impl_blocks_fn: Callable[[Path], list[Any] | None]
    collect_impl_blocks_for_cluster_fn: Callable[..., list[Any] | None]
    fn_container_index_fn: Callable[[Path], dict[FnLocator, str]]
    fn_belongs_to_cluster_fn: Callable[..., bool]
    cluster_filter_type_members_fn: Callable[[list[Any], Any], list[Any]]
    is_gen_tests_file_fn: Callable[[str], bool]
    leaf_first_fn_order_fn: Callable[[list[FnLocator], Any], list[FnLocator]]

    def scan_stub_records(self, target_src_dir: Path,
                          cluster: Any) -> list[dict[str, Any]] | None:
        return self.scan_stub_records_fn(target_src_dir, cluster)

    def scan_stub_tuples(self, target_src_dir: Path,
                         cluster: Any) -> StubInventory:
        return self.scan_stub_tuples_fn(target_src_dir, cluster)

    def enumerate_fns(self, src_dir: Path) -> list[dict[str, Any]] | None:
        return self.structure.enumerate_fns(src_dir)

    def enumerate_types(self, src_dir: Path) -> list[Any] | None:
        return self.structure.enumerate_types(src_dir)

    def collect_impl_blocks(self, src_dir: Path) -> list[Any] | None:
        return self.collect_impl_blocks_fn(src_dir)

    def collect_impl_blocks_for_cluster(self, target_src_dir: Path,
                                        cluster: Any) -> list[Any] | None:
        return self.collect_impl_blocks_for_cluster_fn(target_src_dir, cluster)

    def fn_container_index(self, src_dir: Path) -> dict[FnLocator, str]:
        return self.fn_container_index_fn(src_dir)

    def fn_belongs_to_cluster(self, target_src_dir: Path, file_rel: str,
                              fn_name: str, cluster: Any,
                              fn_cfg_predicates: list[str] | None = None) -> bool:
        return self.fn_belongs_to_cluster_fn(target_src_dir, file_rel, fn_name,
                                             cluster, fn_cfg_predicates=fn_cfg_predicates)

    def cluster_filter_type_members(self, records: list[Any],
                                    cluster: Any) -> list[Any]:
        return self.cluster_filter_type_members_fn(records, cluster)

    def is_gen_tests_file(self, name: str) -> bool:
        return self.is_gen_tests_file_fn(name)

    def leaf_first_fn_order(self, tuples: list[FnLocator],
                            leaf_name_counts: dict[str, int] | None
                            ) -> list[FnLocator]:
        return self.leaf_first_fn_order_fn(tuples, leaf_name_counts)


# --------------------------------------------------------------------------- #
# ports.fill_engine.Retros
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RetrosAdapter:
    load_fn: Callable[[Path], dict[str, Any]]
    save_fn: Callable[[Path, dict[str, Any]], None]
    retro_key_fn: Callable[[str, str, str], str]
    is_fn_fully_exhausted_fn: Callable[..., bool]
    diagnose_fn: Callable[..., dict[str, Any]]
    append_note_fn: Callable[..., bool]
    extract_self_note_fn: Callable[[str, str, str], str | None]
    record_error_codes_fn: Callable[[str], object]  # return discarded

    def load(self, project_dir: Path) -> dict[str, Any]:
        return self.load_fn(project_dir)

    def save(self, project_dir: Path, retros: dict[str, Any]) -> None:
        self.save_fn(project_dir, retros)

    def retro_key(self, file_rel: str, fn_name: str, container: str) -> str:
        return self.retro_key_fn(file_rel, fn_name, container)

    def is_fn_fully_exhausted(self, cache: dict[str, Any], file_rel: str,
                              fn_name: str, container: str) -> bool:
        return self.is_fn_fully_exhausted_fn(cache, file_rel, fn_name, container)

    def diagnose(self, *, fn_name: str, sig_text: str, container: str, file_rel: str,
                 cluster_name: str, last_body: str, error_excerpt: str,
                 fix_attempts: int, prior_retrospective: str = "",
                 bound_methods_section: str = "",
                 pinned_observables: str = "") -> dict[str, Any]:
        return self.diagnose_fn(
            fn_name=fn_name, sig_text=sig_text, container=container,
            file_rel=file_rel, cluster_name=cluster_name, last_body=last_body,
            error_excerpt=error_excerpt, fix_attempts=fix_attempts,
            prior_retrospective=prior_retrospective,
            bound_methods_section=bound_methods_section,
            pinned_observables=pinned_observables)

    def append_note(self, fr_path: Path, note: str,
                    error_codes: list[str] | set[str] | None = None,
                    provenance: Any | None = None) -> bool:
        # `provenance` is 204 N1's premise record (`mn.NoteProvenance`); an
        # [E0xxx] lesson without one is kept run-scoped by the store.
        return self.append_note_fn(fr_path, note, error_codes=error_codes,
                                   provenance=provenance)

    def extract_self_note(self, failed_body: str, error_excerpt: str,
                          fixed_body: str) -> str | None:
        return self.extract_self_note_fn(failed_body, error_excerpt, fixed_body)

    def record_error_codes(self, output: str) -> None:
        self.record_error_codes_fn(output)


# --------------------------------------------------------------------------- #
# ports.fill_engine.TestLinker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TestLinkerAdapter:
    find_tests_for_fn_fn: Callable[..., list[tuple[str, str]]]
    classify_failures_fn: Callable[..., dict[str, dict[str, Any]]]
    write_test_fix_log_fn: Callable[..., None]

    def find_tests_for_fn(self, tests_dir_files: list[Path], fn_name: str, *,
                          file_rel: str = "", src_dir: Path | None = None,
                          container: str = "") -> list[tuple[str, str]]:
        return self.find_tests_for_fn_fn(tests_dir_files, fn_name, file_rel=file_rel,
                                         src_dir=src_dir, container=container)

    def classify_failures(self, test_out: str,
                          filled_this_pass: list[FnLocator],
                          failed_tests: list[str],
                          cluster_name: str) -> dict[str, dict[str, Any]]:
        return self.classify_failures_fn(test_out, filled_this_pass, failed_tests,
                                         cluster_name)

    def write_test_fix_log(self, *, project_dir: Path, cluster_name: str, pass_n: int,
                           fix_iter: int, test_out: str, failed_tests: list[str],
                           fns_retried: list[FnLocator], cur_fail_count: int,
                           new_fail_count: int, best_fail_count: int,
                           stall: int) -> None:
        self.write_test_fix_log_fn(
            project_dir=project_dir, cluster_name=cluster_name, pass_n=pass_n,
            fix_iter=fix_iter, test_out=test_out, failed_tests=failed_tests,
            fns_retried=fns_retried, cur_fail_count=cur_fail_count,
            new_fail_count=new_fail_count, best_fail_count=best_fail_count,
            stall=stall)


# --------------------------------------------------------------------------- #
# ports.fill_engine.StallPolicy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class StallPolicyAdapter:
    effective_stalls_fn: Callable[[int, str | None], int]
    converge_in_cluster_fn: Callable[[Any], str | None]

    def effective_stalls(self, full_budget: int, module: str | None) -> int:
        return self.effective_stalls_fn(full_budget, module)

    def converge_in_cluster(self, cluster: Any) -> str | None:
        return self.converge_in_cluster_fn(cluster)


# --------------------------------------------------------------------------- #
# ports.fill_engine.ImplFillPath
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ImplFillPathAdapter:
    fill_impl_blocks_fn: Callable[..., ClusterFillResult]
    prepare_impl_fill_fn: Callable[..., Any] | None = None

    def fill_impl_blocks(self, **kw: Any) -> ClusterFillResult:
        return self.fill_impl_blocks_fn(**kw)

    def prepare_impl_fill(self, **kw: Any) -> Any:
        if self.prepare_impl_fill_fn is None:
            raise RuntimeError("prepare_impl_fill is not wired")
        return self.prepare_impl_fill_fn(**kw)
