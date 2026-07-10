"""PhaseCFillEngine — the per-cluster phase-C fill coordinator (P2 / R3.3).

Ported verbatim from `pipeline.py:_task2_fill_fns_one_by_one`: for each pass it
scans the cluster's `unimplemented!()` stubs, orders them leaf-first into
priority levels, dispatches each level across a worker pool (each worker drives
`FillOneFn`), then runs the pass's cargo test with a test-fix loop. A
pass-level floor-stall gauge ends the cluster early when passes stop making
progress.

Every `pipeline.py` helper it used is now a narrow role port (`fill_engine`);
the only behavioural change is the test-retry reverse-lookup, which dropped its
`re.finditer(r"\\bfn ...")` source scan for the syn enumerator (`WorkspaceModel`)
per the project's no-regex rule (R7 migrate-on-touch).
"""
from __future__ import annotations

import queue as _queue_mod
import threading as _threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.context import PerFnPassCtx
from pipeline_core.application.reconstruct.fill_one_fn import FillOneFn
from pipeline_core.application.reconstruct.scheduling import group_priority_levels
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.enums import FillUnit
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
    Frontier,
    Stub,
)
from pipeline_core.domain.refine import FloorStall
from pipeline_core.domain.test_results import parse_failed_tests
from pipeline_core.ports.fill_engine import (
    Cargo,
    EngineTelemetry,
    ImplFillPath,
    Mutator,
    PromptContext,
    Retros,
    StallPolicy,
    TestLinker,
    WorkspaceModel,
)
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Phase-C engine knobs resolved from SETTINGS/env at the composition root."""
    parallelism: int
    fill_unit: FillUnit
    per_pass_test_fix_hard_cap: int
    per_pass_test_fix_max_stalls: int
    pass_max_stalls: int
    fix_err_cap: int


@dataclass(frozen=True, slots=True)
class PhaseCFillEngine:
    """Per-cluster phase-C fill engine over narrow role ports (ISP)."""
    tel: EngineTelemetry
    mut: Mutator
    cargo: Cargo
    prompt: PromptContext
    ws: WorkspaceModel
    retros: Retros
    tests: TestLinker
    stall: StallPolicy
    impl_path: ImplFillPath
    fill_one: FillOneFn
    config: EngineConfig

    def _collect_refill_targets(
        self, target_src_dir: Path, stubs: list[Stub],
        tdd_frontier_keys: set[FnLocator] | None,
        tdd_frontier_tests: Frontier | None,
    ) -> list[Stub]:
        """debt #46 — frontier fns that are already FILLED (not stubs) but have a
        failing attributed test → behavioral re-fill targets (`refill=True`).
        Empty unless the TDD outer loop supplied both the frontier keys and the
        failing-test map (i.e. `driver=tdd`); the legacy `fn_walk` driver has its
        own internal test-fix loop and passes neither."""
        if not (tdd_frontier_keys and tdd_frontier_tests):
            return []
        _stub_keys = {s.key for s in stubs}
        _refill_keys = {
            k for k in tdd_frontier_keys
            if k not in _stub_keys and tdd_frontier_tests.get(k)
        }
        if not _refill_keys:
            return []
        out: list[Stub] = []
        _seen: set[FnLocator] = set()
        for r in self.ws.enumerate_fns(target_src_dir) or []:
            rec = Stub.from_dict(r)
            if rec.key in _refill_keys and rec.key not in _seen:
                out.append(replace(rec, refill=True))
                _seen.add(rec.key)
        return out

    def run(
        self,
        project_dir: Path,
        target_src_dir: Path,
        tests_dir_files: list[Path],
        module_name: str,
        cluster_name: str,
        cluster: BuildCluster | None = None,
        check_cmd: str = "cargo check",
        cluster_env: dict[str, str] | None = None,
        max_passes: int = 3,
        leaf_name_counts: dict[str, int] | None = None,
        fr_path: Path | None = None,
        tdd_frontier_keys: set[FnLocator] | None = None,
        skip_end_pass_test: bool = False,
        tdd_frontier_tests: Frontier | None = None,
        tdd_test_cmd: str | None = None,
    ) -> ClusterFillResult:
        """Phase C — Flavour B with full validation loops."""
        if cluster_env is None:
            cluster_env = self.cargo.env()
        # P5 — per-impl-block path. New default unit of work: fill all stubs of
        # one impl block in one LLM call. The per-impl path delegates free fns and
        # oversized blocks back to the per-fn path below.
        if self.config.fill_unit is FillUnit.IMPL:
            _impl_stats = self.impl_path.fill_impl_blocks(
                project_dir=project_dir,
                target_src_dir=target_src_dir,
                tests_dir_files=tests_dir_files,
                module_name=module_name,
                cluster_name=cluster_name,
                cluster=cluster,
                check_cmd=check_cmd,
                cluster_env=cluster_env,
                fr_path=fr_path,
                tdd_frontier_keys=tdd_frontier_keys,
                tdd_frontier_tests=tdd_frontier_tests,   # Plan 10 U2
                tdd_test_cmd=tdd_test_cmd,               # Plan 10 U2
                parallelism=self.config.parallelism,     # debt #51
            )
            # If there are still stub members the per-impl path didn't cover
            # (free fns or oversized blocks), fall through to the per-fn path.
            if _impl_stats.remaining == 0:
                return _impl_stats
            _carryover = {
                "impls_filled": _impl_stats.impls_filled,
                "impls_failed": _impl_stats.impls_failed,
                "fns_filled_via_impl": _impl_stats.fns_filled,
            }
        else:
            _carryover = {}
        target_src_dir.mkdir(parents=True, exist_ok=True)
        bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
        fns_filled = 0
        attempts = 0
        # P4: PIPELINE_PARALLELISM (global knob).
        _parallelism = self.config.parallelism
        if _parallelism > 1:
            self.tel.log(f"  [phase-c/per-fn] parallelism={_parallelism} (LLM calls "
                         f"concurrent; splice+cargo serialised per tree).")
        # Per-tree splice+cargo lock, shared with the per-impl path so the two
        # fill paths never race on the same file; parallel clusters get their own.
        _splice_check_lock = self.mut.lock_for(target_src_dir)
        _counters_lock = _threading.Lock()
        errors: list[str] = []

        failures_dir = project_dir / ".skeleton" / "_failures"
        failures_dir.mkdir(parents=True, exist_ok=True)

        # C: crate-wide macros (cached across passes — file set doesn't change).
        _src_for_macros = (target_src_dir / "src"
                           if (target_src_dir / "src").exists() else target_src_dir)
        macros_section = self.prompt.collect_crate_macros(_src_for_macros)
        # Per-cluster macro invariants + crate-level no_std warning (A).
        _macro_invariants = self.prompt.compute_cluster_macro_invariants(
            _src_for_macros, cluster)
        _no_std_invariant = self.prompt.detect_crate_no_std_invariant(target_src_dir)
        invariants_section = "\n\n".join(
            s for s in (_no_std_invariant, _macro_invariants) if s)
        # P5.1: crate-wide type records, cluster-filtered (Hard Rule 8).
        _crate_type_records = self.ws.cluster_filter_type_methods(
            self.ws.enumerate_types(target_src_dir) or [], cluster)

        # P7.6: floor-stall against (remaining_stubs + last_pass_failures).
        _pass_max_stalls = self.config.pass_max_stalls
        _pass_gauge = FloorStall()  # slice 8a
        _pass_last_fail_count = 0

        for pass_n in range(1, max_passes + 1):
            self.tel.check_stop(f"per-fn pass {pass_n}")
            # Every pass scans for stubs fresh. Cluster-aware: prefer record-based
            # scan (syn enumerator gives container + sig_text). Falls back to
            # name-only tuples when the extractor isn't available.
            recs = self.ws.scan_stub_records(target_src_dir, cluster)
            if recs is not None:
                stubs = [Stub.from_dict(r) for r in recs]
            else:
                stubs = [Stub(file=f, fn_name=fn)
                         for f, fn in self.ws.scan_stub_tuples(target_src_dir, cluster)]
            # True remaining stub count BEFORE the frontier filter.
            _all_stubs_count = len(stubs)
            # Plan 4 — restrict this pass to the TDD frontier's fns when supplied.
            if tdd_frontier_keys is not None:
                stubs = [s for s in stubs if s.key in tdd_frontier_keys]
            # debt #46 — behavioral RE-FILL targets: frontier fns that are NOT
            # stubs (already filled with a compiling-but-wrong body) yet have a
            # failing attributed test. Under `driver=tdd` these were previously
            # dropped (stub-only enumeration) → wrong bodies were permanent. Emit
            # them as `refill=True` Stubs so the SAME fill unit corrects them,
            # guarded by the regression ratchet. Only when the outer loop passes
            # the frontier's failing-test map.
            _refill_recs = self._collect_refill_targets(
                target_src_dir, stubs, tdd_frontier_keys, tdd_frontier_tests)
            if not stubs and not _refill_recs:
                break
            # D: leaf-first order — most-referenced fns first.
            _tuples = [r.key for r in stubs]
            _ordered_tuples = self.ws.leaf_first_fn_order(_tuples, leaf_name_counts)
            # Reorder records to match the leaf-first tuple order.
            _seen: dict[FnLocator, int] = {}
            _reordered: list[Stub] = []
            for tup in _ordered_tuples:
                matches = [r for r in stubs if r.key == tup]
                taken = _seen.get(tup, 0)
                if taken < len(matches):
                    _reordered.append(matches[taken])
                    _seen[tup] = taken + 1
            # Stubs first (fill missing pieces), behavioral re-fills after (correct
            # wrong bodies once their prerequisites exist).
            stubs = _reordered + _refill_recs
            if _refill_recs:
                self.tel.log(f"  [phase-c/per-fn] {len(_refill_recs)} behavioral "
                             "re-fill target(s) (filled bodies with failing tests).")
            # Crate idioms source: impl-block records snapshot for this pass.
            _idiom_blocks = self.ws.collect_impl_blocks(target_src_dir) or []
            # Count total fns in the cluster (regardless of body state) — the
            # monitor renders a cumulative progress bar. Excludes gen_tests.
            _total_fns_in_cluster = 0
            try:
                _all_recs = self.ws.enumerate_fns(target_src_dir) or []
                _all_recs = [r for r in _all_recs
                             if not self.ws.is_gen_tests_file(r.get("file", ""))]
                if cluster is not None:
                    _all_recs = [r for r in _all_recs
                                 if self.ws.fn_belongs_to_cluster(
                                     target_src_dir, r["file"], r["fn_name"], cluster,
                                     fn_cfg_predicates=list(r.get("cfg_predicates") or []))]
                _total_fns_in_cluster = len(_all_recs)
            except Exception:
                _total_fns_in_cluster = len(stubs)  # fallback
            # Cluster-honest denominator: cfg-gated stubs can NEVER fill in
            # this cluster — a total that includes them freezes below 100% by
            # design and reads as a stall (operator confusion, 2026-07-10).
            try:
                _stubs_any_cluster = self.ws.scan_stub_records(target_src_dir, None)
                _stubs_all_count = (len(_stubs_any_cluster)
                                    if _stubs_any_cluster is not None
                                    else _all_stubs_count)
            except Exception:
                _stubs_all_count = _all_stubs_count
            _gated = max(0, _stubs_all_count - _all_stubs_count)
            _filled_count = max(0, _total_fns_in_cluster - _stubs_all_count)
            _fillable_total = _filled_count + _all_stubs_count
            _gated_suffix = (f", {_gated} cfg-gated to other clusters"
                             if _gated else "")
            _frontier_suffix = (
                f", {len(stubs)} in frontier this pass"
                if tdd_frontier_keys is not None and len(stubs) != _all_stubs_count
                else ""
            )
            self.tel.log(f"  [phase-c/per-fn] pass {pass_n}/{max_passes}: "
                         f"{_all_stubs_count} stub(s) remaining in cluster "
                         f"'{cluster_name}' "
                         f"({_filled_count}/{_fillable_total} fillable filled"
                         f"{_gated_suffix}{_frontier_suffix}).")
            _pass_t0 = time.monotonic()
            self.tel.emit("phase_c.pass.start", data={
                "pass_n": pass_n, "cluster": cluster_name, "stubs": len(stubs),
                "max_passes": max_passes,
                "total_fns_in_cluster": _total_fns_in_cluster,
                "all_stubs_remaining": _all_stubs_count,
                "filled_count": _filled_count,
            })
            progress_this_pass = 0
            filled_this_pass: list[FnLocator] = []  # [(file_rel, fn_name)]
            pass_fail_floor = 0  # P7.6: this pass's failing-test floor

            # P8: priority-level batching — `stubs` is leaf-first sorted, so
            # equal-priority stubs are contiguous. Each level runs in parallel
            # with a barrier between levels.
            _levels = group_priority_levels(stubs, leaf_name_counts)

            # FR-024 cross-iter exhausted skip: load the retrospectives map once.
            _retros_cache = self.retros.load(project_dir)

            # P2 de-closure: per-pass ctx for the FillOneFn unit.
            _pass_ctx = PerFnPassCtx(
                project_dir=project_dir, target_src_dir=target_src_dir,
                module_name=module_name, cluster_name=cluster_name,
                check_cmd=check_cmd, cluster_env=cluster_env, fr_path=fr_path,
                tests_dir_files=tests_dir_files, macros_section=macros_section,
                invariants_section=invariants_section,
                crate_type_records=_crate_type_records,
                src_for_macros=_src_for_macros, failures_dir=failures_dir,
                tdd_frontier_tests=tdd_frontier_tests, tdd_test_cmd=tdd_test_cmd,
                splice_check_lock=_splice_check_lock, retros_cache=_retros_cache,
                idiom_blocks=_idiom_blocks, pass_n=pass_n, total=len(stubs),
                cluster_cfg_set=(
                    CfgSet.of(cluster.cfg_true, cluster.cfg_false)
                    if cluster is not None else None
                ),
            )

            def _worker(work_queue: _queue_mod.Queue[tuple[int, Stub]], *,
                        _pass_ctx: PerFnPassCtx = _pass_ctx,
                        filled_this_pass: list[FnLocator] = filled_this_pass,
                        pass_n: int = pass_n) -> None:
                # B023: the per-LEVEL queue AND the per-pass ctx/list/number are
                # passed explicitly — free-var reads would rely on late binding.
                nonlocal attempts, fns_filled, progress_this_pass
                while True:
                    # Stop pulling NEW work once another worker (or the main
                    # thread) has seen a graceful-stop request — raises in
                    # THIS thread immediately, same as the per-fn check inside
                    # `fill_one.run` below; the main thread's own check (after
                    # the join below) is what actually ends the process.
                    self.tel.check_stop(f"per-fn pass {pass_n} worker dispatch")
                    try:
                        idx, rec = work_queue.get_nowait()
                    except _queue_mod.Empty:
                        return
                    try:
                        res = self.fill_one.run(idx, rec, ctx=_pass_ctx)
                    except Exception as _e:
                        # Isolate worker failures so one bad fn doesn't kill the
                        # worker thread (others keep draining the queue).
                        self.tel.log(f"  [phase-c/per-fn] worker error on "
                                     f"{rec.file}::{rec.fn_name}: {_e}")
                        with _counters_lock:
                            errors.append(f"{rec.file}::{rec.fn_name}: "
                                          f"worker exception: {_e}")
                        continue
                    with _counters_lock:
                        attempts += res.attempts
                        if res.filled:
                            fns_filled += 1
                            progress_this_pass += 1
                            filled_this_pass.append(FnLocator(res.file, res.fn_name))
                        if res.error:
                            errors.append(res.error)

            # P8 dispatch: process priority levels in order, barrier between them.
            for _level in _levels:
                _work_queue: _queue_mod.Queue[tuple[int, Stub]] = _queue_mod.Queue()
                for _item in _level:
                    _work_queue.put(_item)
                _nw = min(_parallelism, len(_level))
                if _nw > 1:
                    _workers = [
                        _threading.Thread(target=_worker, args=(_work_queue,),
                                          name=f"per-fn-{cluster_name}-w{i}")
                        for i in range(_nw)
                    ]
                    for _t in _workers:
                        _t.start()
                    for _t in _workers:
                        _t.join()
                    # Main-thread checkpoint: if a worker set the
                    # graceful-stop event, this is the only context where
                    # raising it actually ends the process (`sys.exit()` in
                    # a worker thread only kills that thread).
                    self.tel.check_stop(f"per-fn pass {pass_n} level joined")
                else:
                    _worker(_work_queue)

            # === Per-pass cargo test ===
            # Always run at end of every pass, even when filled_this_pass is empty.
            # Plan 4 — TDD driver runs the cluster test in its OUTER loop; skip when asked.
            if not skip_end_pass_test:
                self.tel.check_stop(f"per-fn pass {pass_n} test phase")
                # Union of test files referenced by ANY fn filled this pass. Only
                # direct children of `tests/` are cargo --test targets.
                test_paths: set[Path] = set()
                for rel_f, fn in filled_this_pass:
                    for tf in self.tests.find_tests_for_fn(
                        tests_dir_files, fn,
                        file_rel=rel_f, src_dir=_src_for_macros,
                    ):
                        p = Path(tf[0])
                        if p.parent.name == "tests":
                            test_paths.add(p)
                # Fall back to the cluster's complete tests/ surface when nothing
                # was filled / linked.
                if not test_paths:
                    for p in tests_dir_files:
                        if p.parent.name == "tests":
                            test_paths.add(p)
                test_stems = sorted({p.stem for p in test_paths})
                if test_stems:
                    test_cmd = self.cargo.build_test_cmd(test_stems, cluster)
                    self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: cargo test "
                                 f"{len(test_stems)} test file(s): {', '.join(test_stems[:5])}"
                                 + ("..." if len(test_stems) > 5 else ""))
                    _test_t0 = time.monotonic()
                    rc, test_out = self.cargo.run(test_cmd, target_src_dir, cluster_env,
                                                  timeout=300)
                    _test_dur_ms = int((time.monotonic() - _test_t0) * 1000)
                    self.tel.emit("phase_c.pass.test", duration_ms=_test_dur_ms, data={
                        "cluster": cluster_name, "pass_n": pass_n,
                        "stems": test_stems, "rc": rc,
                        "failed_tests": sorted(parse_failed_tests(test_out))
                                        if rc != 0 else [],
                    })
                    if rc == 0:
                        self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: cargo test PASSED.")
                    else:
                        self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: cargo test FAILED "
                                     f"(rc={rc}).")
                        # === Per-pass test fix loop (slice 8a f4) ===
                        test_fix_history: dict[tuple[str, str], list[dict[str, Any]]] = {}
                        _entry_fail_count = len(parse_failed_tests(test_out))
                        _stall_seen: dict[str, int] = {
                            "best": _entry_fail_count, "stalls": 0}
                        _iter_ctx: dict[str, Any] = {"cur": 0, "fns": []}

                        def _attempt_test_fix(test_fix_attempt: int, *,
                                              pass_n: int = pass_n,
                                              filled_this_pass: list[FnLocator] = filled_this_pass,
                                              test_cmd: str = test_cmd,
                                              test_fix_history: dict[tuple[str, str], list[dict[str, Any]]] = test_fix_history,
                                              _stall_seen: dict[str, int] = _stall_seen,
                                              _iter_ctx: dict[str, Any] = _iter_ctx) -> FixOutcome:
                            nonlocal attempts, rc, test_out
                            failed_tests = parse_failed_tests(test_out)
                            if not failed_tests:
                                return FixOutcome("stop")  # rc!=0 but no parseable failed tests
                            cur_fail_count = len(failed_tests)
                            # P3: classifier-driven retry candidates. Fires after
                            # one naive iteration stalls, OR on iter 1 when nothing
                            # was filled (no naive baseline exists).
                            classification: dict[str, dict[str, Any]] = {}
                            if _stall_seen["stalls"] >= 1 or not filled_this_pass:
                                classification = self.tests.classify_failures(
                                    test_out=test_out,
                                    filled_this_pass=filled_this_pass,
                                    failed_tests=sorted(failed_tests),
                                    cluster_name=cluster_name,
                                )
                            fns_to_retry: list[FnLocator] = []
                            if classification:
                                wanted: set[FnLocator] = set()
                                for _test, info in classification.items():
                                    if info["category"] not in (
                                            "BAD_FILL", "TEST_PRECONDITION_MISSING"):
                                        continue
                                    ref = info["fn"]
                                    if "::" in ref:
                                        rel_f, fn = ref.split("::", 1)
                                        wanted.add(FnLocator(rel_f.strip(), fn.strip()))
                                if filled_this_pass:
                                    in_scope = set(filled_this_pass)
                                    fns_to_retry = [p for p in wanted if p in in_scope]
                                else:
                                    fns_to_retry = list(wanted)
                                _cat_counts = Counter(
                                    v["category"] for v in classification.values())
                                self.tel.log(f"  [phase-c/classifier] pass {pass_n} iter "
                                             f"{test_fix_attempt}: "
                                             + ", ".join(f"{c}={n}"
                                                         for c, n in _cat_counts.items()))
                            if not fns_to_retry and filled_this_pass:
                                # Naive fallback: token match in test output.
                                for (rel_f, fn) in filled_this_pass:
                                    if any(fn in t for t in failed_tests) or fn in test_out:
                                        fns_to_retry.append(FnLocator(rel_f, fn))
                            if not fns_to_retry and not filled_this_pass:
                                # Reverse-lookup fallback: for each failing test
                                # binary, enumerate fns it exercises and feed those
                                # as candidates. (R7: extractor, not regex.)
                                test_fname_set = {
                                    t.split("::")[0].split("__")[0]
                                    for t in failed_tests
                                }
                                for r in (self.ws.enumerate_fns(target_src_dir) or []):
                                    rel_path = r.get("file", "")
                                    fn = r.get("fn_name", "")
                                    if not fn:
                                        continue
                                    for tf in self.tests.find_tests_for_fn(
                                        tests_dir_files, fn,
                                        file_rel=rel_path, src_dir=_src_for_macros,
                                    ):
                                        if Path(tf[0]).stem in test_fname_set:
                                            fns_to_retry.append(FnLocator(rel_path, fn))
                                            break
                                fns_to_retry = list(dict.fromkeys(fns_to_retry))
                            if not fns_to_retry:
                                # Conservative: retry all fns filled this pass.
                                fns_to_retry = list(filled_this_pass)

                            any_progress = False
                            # P5.1 parity: map (file, fn) → container via the syn
                            # enumerator so the self-type's definition gets injected.
                            _retry_containers = self.ws.fn_container_index(target_src_dir)
                            for rel_f, fn in fns_to_retry:
                                fp = target_src_dir / rel_f
                                try:
                                    cur_text = fp.read_text(encoding="utf-8")
                                except Exception:
                                    continue
                                sloc = self.mut.locate_fn(cur_text, fn)
                                if sloc is None:
                                    continue
                                sig_t = sloc[0]
                                cur_body = cur_text[sloc[2]:sloc[3]]
                                error_excerpt_test = self.cargo.focus_errors(
                                    test_out, rel_f, max_chars=self.config.fix_err_cap)
                                _ctx = self.prompt.gather(
                                    file_text=cur_text, file_rel=rel_f, fn_name=fn,
                                    sig_text=sig_t, body_text=cur_body,
                                    error_excerpt=error_excerpt_test,
                                    container=_retry_containers.get(FnLocator(rel_f, fn), ""),
                                    type_records=_crate_type_records,
                                    fr_path=fr_path,
                                )
                                # P5.4: container filter disambiguates tests for
                                # generic fn names (fmt/new/source).
                                t_blocks = self.tests.find_tests_for_fn(
                                    tests_dir_files, fn,
                                    file_rel=rel_f, src_dir=_src_for_macros,
                                    container=_ctx.container,
                                )
                                prompt = build_per_fn_fix_prompt(
                                    file_text=cur_text, fn_name=fn, sig_text=sig_t,
                                    test_blocks=t_blocks, module_name=module_name,
                                    cluster_name=cluster_name, file_rel=rel_f,
                                    prev_body=cur_body, error_excerpt=error_excerpt_test,
                                    sections=PerFnFixSections(
                                        macros=macros_section, notes=_ctx.notes_section,
                                        container=_ctx.container, types=_ctx.types_section,
                                        invariants=invariants_section,
                                        retrospective_guidance="", siblings="", crate_idioms="",
                                        bound_methods=_ctx.bound_methods_section),
                                    fr_path=fr_path,  # P6.4 note recall
                                    attempt_history=test_fix_history.get((rel_f, fn), []),  # P2
                                )
                                attempts += 1
                                attempts_log_t: list[str] = []
                                new_body, _ = self.prompt.ask_per_fn_body(
                                    prompt, fn, attempts_log_t, sig_text=sig_t)
                                if not new_body:
                                    continue
                                # allow_nonstub: every test-retry candidate is an
                                # already-FILLED fn — without it the splice is
                                # always rejected and the whole loop is a no-op.
                                ok2 = self.mut.splice_fn_body(
                                    fp, fn, new_body, allow_nonstub=True).ok
                                if ok2:
                                    any_progress = True
                                    self.tel.log(f"  [phase-c/per-fn] test-fix: re-spliced "
                                                 f"{rel_f}::{fn} (attempt {test_fix_attempt}).")
                                    test_fix_history.setdefault(
                                        (rel_f, fn), []).append({
                                            "attempt": test_fix_attempt,
                                            "body": new_body,
                                            "error_sig": self.cargo.focus_errors(
                                                test_out, rel_f, max_chars=self.config.fix_err_cap),
                                            "outcome": "test_fail",
                                        })
                            if not any_progress:
                                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: no fn could "
                                             f"be re-spliced for failing tests — stopping.")
                                return FixOutcome("stop")

                            # Re-run cargo check (cheap) then cargo test.
                            rc_c, out_c = self.cargo.run(check_cmd, target_src_dir, cluster_env)
                            if rc_c != 0:
                                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: test-fix "
                                             f"broke cargo check; leaving for next pass.")
                                return FixOutcome("stop")
                            rc, test_out = self.cargo.run(test_cmd, target_src_dir,
                                                          cluster_env, timeout=300)
                            if rc == 0:
                                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: cargo test "
                                             f"PASSED after test-fix attempt {test_fix_attempt}.")
                                return FixOutcome("accepted")
                            new_fail_count = len(parse_failed_tests(test_out))
                            # P4: persist this iter's cargo test output for post-hoc.
                            self.tests.write_test_fix_log(
                                project_dir=project_dir,
                                cluster_name=cluster_name,
                                pass_n=pass_n,
                                fix_iter=test_fix_attempt,
                                test_out=test_out,
                                failed_tests=sorted(parse_failed_tests(test_out)),
                                fns_retried=fns_to_retry,
                                cur_fail_count=cur_fail_count,
                                new_fail_count=new_fail_count,
                                best_fail_count=min(_stall_seen["best"], new_fail_count),
                                stall=_stall_seen["stalls"],
                            )
                            _iter_ctx["cur"] = cur_fail_count
                            _iter_ctx["fns"] = list(fns_to_retry)
                            return FixOutcome("failed", err_count=new_fail_count)

                        def _on_test_fix_failure(attempt_n: int, outcome: FixOutcome,
                                                 gauge: FloorStall, *,
                                                 pass_n: int = pass_n,
                                                 _stall_seen: dict[str, int] = _stall_seen,
                                                 _iter_ctx: dict[str, Any] = _iter_ctx) -> None:
                            self.tel.emit("phase_c.test_fix.iter", data={
                                "cluster": cluster_name, "pass_n": pass_n,
                                "iter": attempt_n,
                                "prev_fail_count": _iter_ctx["cur"],
                                "new_fail_count": outcome.err_count,
                                "delta": outcome.err_count - _iter_ctx["cur"],
                                "best": gauge.best,
                                "improved": gauge.stalls == 0,
                                "stalled": gauge.stalls,
                                "fns_retried": [f"{r}::{f}" for r, f in _iter_ctx["fns"]],
                            })
                            self.tel.log(f"  [phase-c/per-fn] pass {pass_n} test-fix iter "
                                         f"{attempt_n}: "
                                         f"{_iter_ctx['cur']}→{outcome.err_count} failures "
                                         f"(floor={gauge.best}, stall={gauge.stalls}).")
                            _stall_seen["best"] = gauge.best or 0
                            _stall_seen["stalls"] = gauge.stalls

                        def _on_test_fix_stall(attempt_n: int, gauge: FloorStall, *,
                                               pass_n: int = pass_n) -> None:
                            self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: failure "
                                         f"floor stalled at {gauge.best} for "
                                         f"{gauge.stalls} iter(s) — stopping test-fix loop.")

                        drive_fix_loop(
                            _attempt_test_fix,
                            hard_cap=self.config.per_pass_test_fix_hard_cap,
                            max_stalls=self.stall.effective_stalls(
                                self.config.per_pass_test_fix_max_stalls,
                                self.stall.converge_in_cluster(cluster) or module_name),
                            stop_context="test fix",
                            on_failure=_on_test_fix_failure,
                            on_stall=_on_test_fix_stall,
                            initial_floor=_entry_fail_count,
                            check_stop=self.tel.check_stop,
                        )
                        # P7.6 floor for the pass-level gauge.
                        pass_fail_floor = 0 if rc == 0 else int(_stall_seen["best"] or 0)

            # P7.6: capture last-pass failure count for the pass-level floor.
            _pass_last_fail_count = pass_fail_floor
            _remaining_pass = len(self.ws.scan_stub_tuples(target_src_dir, cluster))
            _pass_metric = _remaining_pass + _pass_last_fail_count
            _pass_gauge.observe(_pass_metric)
            self.tel.emit("phase_c.pass.done", duration_ms=int(
                    (time.monotonic() - _pass_t0) * 1000),
                  data={
                "pass_n": pass_n, "cluster": cluster_name,
                "fns_filled": progress_this_pass, "errors_so_far": len(errors),
                "remaining": _remaining_pass,
                "pass_metric": _pass_metric,
                "pass_metric_best": _pass_gauge.best,
                "pass_stalled": _pass_gauge.stalls,
            })
            if progress_this_pass == 0:
                self.tel.log(f"  [phase-c/per-fn] pass {pass_n} made no progress; stopping.")
                break
            if _pass_gauge.stalled(_pass_max_stalls):
                self.tel.log(f"  [phase-c/per-fn] cluster {cluster_name}: pass-level floor "
                             f"stalled at metric={_pass_gauge.best} for {_pass_gauge.stalls} "
                             f"pass(es) — stopping early at pass {pass_n}/{max_passes}.")
                break

        bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
        remaining = len(self.ws.scan_stub_tuples(target_src_dir, cluster))
        self.tel.emit("phase_c.cluster.done", data={
            "cluster": cluster_name,
            "fns_filled": fns_filled,
            "bytes_filled": bytes_after - bytes_before,
            "llm_calls": attempts,
            "remaining_stubs": remaining,
            "errors": len(errors),
        })
        return ClusterFillResult(
            fns_filled=fns_filled + _carryover.get("fns_filled_via_impl", 0),
            bytes_filled=bytes_after - bytes_before,
            attempts=attempts,
            errors=tuple(errors),
            remaining=remaining,
            impls_filled=_carryover.get("impls_filled", 0),
            impls_failed=_carryover.get("impls_failed", 0),
        )
