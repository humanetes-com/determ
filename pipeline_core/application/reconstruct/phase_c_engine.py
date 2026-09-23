"""PhaseCFillEngine — the per-cluster phase-C fill coordinator.

Ported verbatim from `pipeline.py:_task2_fill_fns_one_by_one`: for each pass it
scans the cluster's `unimplemented!()` stubs, orders them leaf-first into
priority levels, dispatches each level across a worker pool (each worker drives
`FillOneFn`), then runs the pass's cargo test with a test-fix loop. A
pass-level floor-stall gauge ends the cluster early when passes stop making
progress.

Every `pipeline.py` helper it used is now a narrow role port (`fill_engine`);
the only behavioural change is the test-retry reverse-lookup, which dropped its
`re.finditer(r"\\bfn ...")` source scan for the syn enumerator (`WorkspaceModel`)
per the project's no-regex rule (migrate on touch).
"""
from __future__ import annotations

import os
import queue as _queue_mod
import threading as _threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.cluster_membership import (
    collect_impl_refill_targets,
)
from pipeline_core.application.reconstruct.context import PerFnPassCtx
from pipeline_core.application.reconstruct.fill_one_fn import FillOneFn
from pipeline_core.application.reconstruct.fill_routes import (
    FillRoute,
    PreparedFill,
    RouteLedger,
    RouteRunReport,
)
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.application.reconstruct.impl_fill import (
    cluster_inactive_stub_names,
)
from pipeline_core.application.reconstruct.scheduling import (
    group_priority_levels,
    match_records_to_order,
    rank_stubs_by_blocker_counts,
)
from pipeline_core.application.reconstruct.tree_txn import (
    PreImage,
    capture,
    guard_tree_boundary,
    restore,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_select import IdentityMustUnit
from pipeline_core.domain.cfg import cluster_cfg_set
from pipeline_core.domain.cluster_plan import fill_invariants_section
from pipeline_core.domain.dispatch import (
    impl_route_covers,
    impl_route_deferral_applies,
    stubs_deferred_by_impl_route,
)
from pipeline_core.domain.enums import FillUnit, RouteOrder
from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteRun,
    RunClock,
    UnitEvidence,
)
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.last_error import resolve_kind
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FillResult,
    FnLocator,
    FnRecord,
    FnSite,
    ImplBlock,
    Stub,
)
from pipeline_core.domain.pass_payoff import assess_fill_results, combine_payoffs
from pipeline_core.domain.refill_stamp import (
    convicting_tests_for_member,
    stamp_reads_dispatch,
)
from pipeline_core.domain.refine import FixProgress, FloorStall
from pipeline_core.domain.test_outcome import PremiseEntry
from pipeline_core.domain.test_results import parse_failed_tests, still_open_names
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
from pipeline_core.prompts.layout_authoring import identity_must_instruction
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt


def _best_fails(gauge: FloorStall) -> int:
    """The test-fix gauge's floor as a plain count, for telemetry/status that
    needs a number. This gauge only ever sees compiled attempts, so the floor
    is always a test-tier `FixProgress`; None means "no observation yet"."""
    best = gauge.best
    return best.failing_tests if isinstance(best, FixProgress) else 0


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Phase-C engine knobs resolved from SETTINGS/env at the composition root."""
    parallelism: int
    fill_unit: FillUnit
    per_pass_test_fix_hard_cap: int
    per_pass_test_fix_max_stalls: int
    pass_max_stalls: int
    fix_err_cap: int
    # Impl-route unit cap; the engine needs it to know which frontier stubs
    # that route will never attempt (see `stubs_deferred_by_impl_route`).
    per_impl_max_stubs: int = 12
    # Fill-route sequencing. Under FILE_PRIMARY the file-first pass takes
    # every frontier file (free fns included), which changes when the
    # stubs-first deferral below is warranted — see
    # `impl_route_deferral_applies` for the exact hazard it keeps guarding.
    route_order: RouteOrder = RouteOrder.DEFAULT


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
    # F10 Use-B (M5): load the cluster's harvested behaviours for the few-shot
    # USAGE_EXAMPLES channel. Injected in composition (bound to the behaviour
    # store); default returns none so the engine stays additive without it.
    behaviour_loader: Callable[[Path, str], tuple[BehaviourSpec, ...]] = (
        lambda _project_dir, _cluster: ())
    # Where route runs are recorded and ranked from (`adapters/
    # route_ledger_store.py`); None keeps the schedule evidence-free (every
    # run never-served, today's order on ties) and writes nothing.
    route_ledger: RouteLedger | None = None

    def _leaf_first_records(
        self, records: list[Stub],
        leaf_name_counts: dict[str, int] | None,
    ) -> list[Stub]:
        """Leaf-first order for any record batch headed into the level
        grouper — through the `ws` seam so tests that script the ordering
        keep their hook. See `match_records_to_order` for why every batch
        must be sorted, not just the stub half."""
        ordered = self.ws.leaf_first_fn_order(
            [r.key for r in records], leaf_name_counts)
        return match_records_to_order(ordered, records)

    def _collect_refill_targets(
        self, target_src_dir: Path, stubs: list[Stub],
        tdd_frontier_keys: set[FnLocator] | None,
        tdd_frontier_tests: FailureFrontier | None,
        crash_blamed: frozenset[FnLocator] | None = None,
    ) -> list[Stub]:
        """Frontier fns that are already FILLED (not stubs) but have a failing
        attributed test → behavioral re-fill targets (`refill=True`).
        Empty unless the TDD outer loop supplied both the frontier keys and the
        failing-test map (i.e. `driver=tdd`); the legacy `fn_walk` driver has its
        own internal test-fix loop and passes neither.

        Same predicate as ``collect_impl_refill_targets``, including
        ``file_primary``: under that order the file pass writes the block
        and the impl route drops it, so a dispatch-only batch member must
        still stamp or it is stranded (run ``125003-269e2338``). A locator
        in ``crash_blamed`` is a refill even when no test convicts it: a
        crashed binary reports no verdict, and without the stamp the
        blamed conversion never reaches the write list.

        A frontier key that is neither a stub nor an enumerated fn resolves to
        nothing and would drop out here without a trace — the failure mode that
        let a crash-blamed fn sit unfixed for entire runs while its blame line
        was printed every sweep. Such keys are reported, never swallowed."""
        if not (tdd_frontier_keys and tdd_frontier_tests):
            return []
        crashed = crash_blamed or frozenset()
        _stub_keys = {s.key for s in stubs}
        _records = [Stub.from_dict(r)
                    for r in self.ws.enumerate_fns(target_src_dir) or []]
        containers_by_key: dict[FnLocator, list[str]] = {}
        by_block: dict[tuple[str, str], list[Stub]] = defaultdict(list)
        for rec in _records:
            containers_by_key.setdefault(rec.key, []).append(rec.container)
            by_block[(rec.file, rec.container)].append(rec)
        max_stubs = self.config.per_impl_max_stubs
        covers: dict[tuple[str, str], bool] = {}
        for (file, container), recs in by_block.items():
            members = tuple(
                FnRecord(file=r.file, fn_name=r.fn_name,
                         container=r.container, sig_text=r.sig_text)
                for r in recs)
            stub_m = tuple(
                m for m in members
                if FnLocator(m.file, m.fn_name) in _stub_keys)
            dispatched = tuple(
                m for m in members
                if (loc := FnLocator(m.file, m.fn_name)) not in _stub_keys
                and loc in tdd_frontier_keys
                and (tdd_frontier_tests.tests_for(loc) or loc in crashed))
            probe = ImplBlock(
                file=file, container=container, members=members,
                stub_members=stub_m, refill_members=dispatched)
            covers[(file, container)] = impl_route_covers(
                probe, max_stubs=max_stubs)
        out: list[Stub] = []
        _seen: set[tuple[str, str, str]] = set()
        for rec in _records:
            ident = (rec.file, rec.container, rec.fn_name)
            if ident in _seen or rec.key in _stub_keys:
                continue
            if rec.key not in tdd_frontier_keys:
                continue
            if stamp_reads_dispatch(
                    covers=covers.get((rec.file, rec.container), False),
                    file_primary=self.config.route_order is RouteOrder.FILE_PRIMARY):
                tests = set(tdd_frontier_tests.tests_for(rec.key))
            else:
                tests = set(convicting_tests_for_member(
                    tdd_frontier_tests, rec.file, rec.fn_name, rec.container,
                    containers_by_key.get(rec.key, [rec.container])))
            if not tests and rec.key not in crashed:
                continue
            out.append(replace(rec, refill=True))
            _seen.add(ident)
        _wanted = {
            k for k in tdd_frontier_keys
            if k not in _stub_keys and tdd_frontier_tests.convicting_for(k)
        }
        _matched = {rec.key for rec in _records}
        _dangling = sorted(
            f"{k.file}::{k.fn_name}" for k in _wanted - _matched)
        if _dangling:
            self.tel.log(
                f"  [phase-c/per-fn] {len(_dangling)} frontier key(s) match no "
                f"enumerated fn — NOT re-filled: {_dangling[:8]}")
            self.tel.emit("phase_c.refill.dangling_keys",
                          data={"keys": _dangling[:50]})
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
        tdd_frontier_tests: FailureFrontier | None = None,
        # Producer subset of the live futility escalations — see
        # `impl_fill.fill_impl_blocks`. Carried, never recomputed: the fire
        # logs its whole verdict once and a second scan would be a second
        # opinion about a measurement that already happened.
        escalated_keys: frozenset[FnLocator] = frozenset(),
        escalated_measured: frozenset[FnLocator] = frozenset(),
        # Prior FLAT tries of dispatched units, from the proven-body ledger.
        # Default empty: fn_walk and tests that do not pass it leave the
        # refill-evidence section byte-identical to the pre-history render.
        ledger_entries: Sequence[Any] = (),
        # Oracle fingerprint for `try_history`. Default empty refuses every
        # row (same rule as `reseed_allowed`); the live driver passes the
        # iteration's fingerprint beside `ledger_entries`.
        oracle_fp: str = "",
        tdd_test_cmd: str | None = None,
        crash_evidence: dict[FnLocator, str] | None = None,
        crash_blamed: frozenset[FnLocator] | None = None,
        crash_blocked: dict[str, str] | None = None,
        # Leftover tests whose binary did not complete. The fill schedule
        # ranks a run whose every aimed test is in this set, and whose
        # targets miss crash_blamed, as a flat re-buy (weight 0).
        unmeasurable_tests: frozenset[str] = frozenset(),
        # Locators leftover tests convict before owner-map admission
        # (eligible plus hop-pair members). Empty skips coverage-split
        # marking so a call that does not snapshot keeps today's order.
        attributed: Collection[FnLocator] = (),
        # The body ledger's per-unit verdicts, built by the TDD driver at
        # dispatch (`fill_schedule.unit_evidence_from`). None keeps the
        # schedule's route-row reading.
        unit_evidence: UnitEvidence | None = None,
        # How many rows `unit_evidence` was built from, in cluster file
        # order. Recorded on the schedule event; None leaves the replay
        # to its iteration cut.
        body_rows: int | None = None,
        # The TDD driver's final dispatch order as ranks (head highest,
        # `dispatch_batch.dispatch_ranks_of`). When set, stubs are ordered and
        # grouped by these ranks instead of leaf-name frequency, so a
        # constructor that `take_batch` placed in the slice actually OPENS
        # the pass — leaf-first otherwise puts `fmt`/`new` in an earlier
        # barriered level (run `155810-cbb3b9fc` iter 1: constructors in
        # the batch, dispatched last).
        dispatch_ranks: dict[FnLocator, int] | None = None,
        # Failing test -> the crate fns it executes (the sweep's owner map
        # plus its attributed units): the red-slice session's editable set.
        session_slices: Mapping[str, frozenset[FnSite]] | None = None,
        session_owners: Mapping[str, frozenset[FnSite]] | None = None,
        # The sweep's stub hold (`test_outcome.StubHold`), for the SCHEDULE
        # only: the still-stubbed fns failing tests stop in first, and each
        # held test's blockers among them. A run holding one can move those
        # tests, so it is neither an unmeasurable re-buy nor a run the pair
        # must go before; the per-fn and impl routes never see these as
        # blame.
        stub_hold_blockers: frozenset[FnLocator] = frozenset(),
        stub_hold_hollow: Mapping[str, frozenset[FnLocator]] | None = None,
        # The fill schedule's inputs: its wall-clock budget in minutes
        # (`TDD_FILL_BUDGET_MINUTES`; 0 = order only), the iteration the
        # rows are stamped with, and the run id.
        fill_budget_minutes: float = 0.0,
        fill_iteration: int = 0,
        run_id: str = "",
        # How many slice sessions one fill opens (`SLICE_SESSION_MAX_PER_FILL`;
        # 0 = unbounded); the rest are deferred to the next fill by the
        # schedule, never truncated.
        max_sessions_per_fill: int = 0,
        # A session's share floor in turns (`SLICE_SESSION_MIN_TURNS`; 0 = the
        # plain weighted split), priced at its measured turn cost by the
        # schedule.
        session_min_turns: int = 0,
        # `(rel) -> healed?`: the heal hierarchy on one file of the tree,
        # for the boundary read before the per-pass test. None reports the
        # break and lets the pass test read it (the fixture path).
        heal_file: Callable[[str], bool] | None = None,
        # The driver's unit-ledger writer, called by the fill executor after
        # every wave (and on the wave a stop cuts) with the units the wave
        # kept, so a fill that never returns still leaves its `landed` rows.
        on_wave_kept: Callable[[frozenset[FnLocator]], None] | None = None,
        # The ground this fill's plan was made from and a reader of the
        # ground now (`test_outcome.premise_of`); both set, the fill ends at
        # the first claim after a keep that moved it (`fill_scheduler`).
        fill_premise: Mapping[str, PremiseEntry] | None = None,
        fill_ground_now: Callable[[], Mapping[str, PremiseEntry] | None] | None = None,
    ) -> ClusterFillResult:
        """Phase C — every route of one fill, ordered and budgeted by
        `fill_scheduler.execute_fill` (the sessions, pairs, file-first and
        per-impl routes come from `prepare_impl_fill`; the per-fn route is
        this engine's own)."""
        if cluster_env is None:
            cluster_env = self.cargo.env()
        # One load for the pass. The test-fix loop used to re-read the 493 KB
        # suite per prompt (3.3 ms measured); the impl and per-fn routes
        # already loaded it once each. Same bytes, one parse.
        behaviour_specs = self.behaviour_loader(project_dir, cluster_name)
        # Per-impl-block path. New default unit of work: fill all stubs of
        # one impl block in one LLM call. The per-impl path delegates free fns and
        # oversized blocks back to the per-fn path below — but only AFTER it has
        # run, which is the wrong order when one of those stubs is what blocks it:
        #
        # Stubs before re-fills, ACROSS routes. A stub the impl route never
        # attempts (free fn / oversized block) that panics from drop glue or a
        # vtable slot aborts the test binary, so every impl-route re-fill judged
        # against that binary is reverted as a sibling crash no matter how good
        # the body is. Give those stubs this call to themselves; the TDD driver
        # re-enters next iter with them filled and the impl route resumes. Only
        # under the TDD driver — the fn_walk driver gets one call and must not
        # have it narrowed.
        # Union of fns any route genuinely attempted this call — returned to
        # the TDD driver, whose exhaustion charging must not outrun dispatch
        # (an undispatched frontier fn is refused, not judged).
        _dispatched: set[FnLocator] = set()
        _reverted: set[FnLocator] = set()
        # Units the impl/file pass dispatched but never got an answer for
        # (see `ClusterFillResult.answer_lost_fns`). A per-fn attempt below
        # supersedes the loss for its unit.
        _answer_lost: set[FnLocator] = set()
        # One cfg policy across routes. The impl/file routes refuse a splice
        # at a fn whose every stub occurrence is cfg-gated out of this cluster
        # (`cluster_scoped_splice`), but the DISPATCH side never consulted
        # that verdict: on 2026-08-11/12 every A/B/C arm spent 90-147 s
        # filling `src/lib.rs::cap_limit` via the per-fn route (the deferral
        # narrowing had carried it) and the body went into a build that never
        # compiles it — the same run in which the file route refused the
        # identical target as `not_in_cluster_cfg`. Dispatch consults the
        # same verdict now, at the same source of truth.
        _cfg_dead: frozenset[tuple[str, str]] = frozenset()
        if cluster is not None:
            _cfg_dead = cluster_inactive_stub_names(
                target_src_dir, cluster, self.ws.scan_stub_records)

        def _is_cfg_dead(key: FnLocator) -> bool:
            return (norm_src_rel(key.file), key.fn_name) in _cfg_dead

        _impl_deferred_stubs: frozenset[FnLocator] = frozenset()
        _has_refill_targets = False
        if self.config.fill_unit is FillUnit.IMPL and tdd_frontier_keys is not None:
            # Refill members stamped first: the impl route's unit-size cap
            # counts stubs + refills, so a block only looks coverable here if it
            # is still coverable once this iter's re-fill targets are on it.
            _blocks_now = collect_impl_refill_targets(
                self.ws.collect_impl_blocks_for_cluster(target_src_dir, cluster) or [],
                tdd_frontier_keys, tdd_frontier_tests,
                max_stubs=self.config.per_impl_max_stubs,
                crash_blamed=crash_blamed or frozenset(),
                file_primary=self.config.route_order is RouteOrder.FILE_PRIMARY)
            _has_refill_targets = any(b.refill_members for b in _blocks_now)
            _impl_deferred_stubs = frozenset(
                k for k in (stubs_deferred_by_impl_route(
                    _blocks_now, max_stubs=self.config.per_impl_max_stubs,
                ) & tdd_frontier_keys)
                if not _is_cfg_dead(k))
        _impl_payoff = None
        _fill_hist = FillClassHistogram()
        _prepared_fill: PreparedFill | None = None
        _carryover: dict[str, Any] = {}
        if impl_route_deferral_applies(
                _impl_deferred_stubs, _has_refill_targets,
                file_primary=self.config.route_order is RouteOrder.FILE_PRIMARY):
            self.tel.log(
                f"  [phase-c/impl] cluster {cluster_name}: deferring the impl "
                f"route this iter — {len(_impl_deferred_stubs)} frontier stub(s) "
                "it never attempts (free fn / oversized block) must fill first, "
                "or every re-fill is reverted as a sibling crash: "
                + ", ".join(sorted(f"{f}::{fn}" for f, fn in _impl_deferred_stubs)))
            # THE DEFERRAL WITHHOLDS RE-FILLS. IT MUST NOT ALSO WITHHOLD
            # FIRST-FILLS, and replacing the frontier wholesale did.
            #
            # The premise above is about re-fills: a behavioural re-fill landed
            # while a sibling is still `unimplemented!()` is reverted as a
            # sibling crash, so the blockers go first. That argument says
            # nothing about a frontier fn that is ITSELF a stub — filling one
            # cannot be undone by a sibling panic, because there is no body to
            # lose, and it is how a blocker stops being one.
            #
            # Measured on run `1920662-40f711b2` iteration 6 (2026-08-17
            # 05:14Z): the frontier held 41, the deferral rewrote it to the 8
            # free-fn/oversized blockers, all 8 landed and all 8 logged
            # `kept ... (compiles; N attributed test(s) blocked by
            # still-stubbed sibling(s))` — zero verdicts could move by
            # construction, and the sweep read 33/105 twice. The line the
            # driver printed at the end of that iteration names what the 8 were
            # waiting on: `4 frontier stub(s) were dispatched by NO route this
            # iter — src/context.rs::ext_context, src/error.rs::drop,
            # src/error.rs::from, src/kind.rs::new`. All four are stubs, all
            # four are impl members the impl route would have claimed, and none
            # was dispatched once in the run's 7 iterations, 10 hours and 191
            # model calls. `src/kind.rs::new` alone is the named stub target of
            # eight distinct tests.
            #
            # So the rewrite keeps every frontier key that is a live stub and
            # drops only the re-fills, which is what the premise actually
            # licenses. The per-fn route below re-scans and re-filters anyway,
            # so a key that is no longer a stub costs nothing.
            _live_stub_keys = frozenset(
                Stub.from_dict(r).key
                for r in (self.ws.scan_stub_records(target_src_dir, cluster)
                          or ()))
            _also_stubs = frozenset(
                k for k in ((tdd_frontier_keys or set()) & _live_stub_keys)
                if not _is_cfg_dead(k)) - _impl_deferred_stubs
            if _also_stubs:
                self.tel.log(
                    f"  [phase-c/impl] cluster {cluster_name}: the deferral "
                    f"keeps {len(_also_stubs)} further frontier stub(s) in the "
                    "batch — withholding a RE-fill while a sibling is stubbed "
                    "is the rule, withholding a FIRST fill of a stub is not, "
                    "and these are what the blockers are blocked on: "
                    + ", ".join(sorted(f"{f}::{fn}"
                                       for f, fn in _also_stubs)[:8]))
            self.tel.emit("phase_c.dispatch.impl_route_deferred", data={
                "cluster": cluster_name,
                "stubs": sorted(f"{f}::{fn}" for f, fn in _impl_deferred_stubs),
                "kept_stubs": sorted(f"{f}::{fn}" for f, fn in _also_stubs)})
            tdd_frontier_keys = set(_impl_deferred_stubs) | set(_also_stubs)
            _carryover = {}
        elif self.config.fill_unit is FillUnit.IMPL:
            _prepared_fill = self.impl_path.prepare_impl_fill(
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
                tdd_frontier_tests=tdd_frontier_tests,
                escalated_keys=escalated_keys,
                escalated_measured=escalated_measured,
                ledger_entries=ledger_entries,
                oracle_fp=oracle_fp,
                tdd_test_cmd=tdd_test_cmd,
                parallelism=self.config.parallelism,
                crash_evidence=crash_evidence,
                crash_blamed=crash_blamed,
                crash_blocked=crash_blocked,
                behaviour_specs=behaviour_specs,
                dispatch_ranks=dispatch_ranks,
                session_slices=session_slices,
                session_owners=session_owners,
            )
            if _prepared_fill.crash_callees:
                crash_blamed = (crash_blamed or frozenset()) | (
                    _prepared_fill.crash_callees)
                tdd_frontier_keys = set(tdd_frontier_keys or ()) | set(
                    _prepared_fill.crash_callees)
        _per_fn_route = _PerFnRoute(
            self,
            project_dir=project_dir, target_src_dir=target_src_dir,
            tests_dir_files=tests_dir_files, module_name=module_name,
            cluster_name=cluster_name, cluster=cluster, check_cmd=check_cmd,
            cluster_env=cluster_env, max_passes=max_passes,
            leaf_name_counts=leaf_name_counts, fr_path=fr_path,
            tdd_frontier_keys=tdd_frontier_keys,
            skip_end_pass_test=skip_end_pass_test,
            tdd_frontier_tests=tdd_frontier_tests, tdd_test_cmd=tdd_test_cmd,
            crash_evidence=crash_evidence, crash_blamed=crash_blamed,
            crash_blocked=crash_blocked, dispatch_ranks=dispatch_ranks,
            behaviour_specs=behaviour_specs, cfg_dead=_cfg_dead,
            is_cfg_dead=_is_cfg_dead, dispatched=_dispatched,
            reverted=_reverted, answer_lost=_answer_lost,
            landed_before=(_prepared_fill.landed if _prepared_fill is not None
                           else (lambda: 0)),
            heal_file=heal_file)
        routes: list[FillRoute] = []
        if _prepared_fill is not None:
            routes.extend(_prepared_fill.routes)
        routes.append(_per_fn_route)
        _hollow: dict[str, frozenset[FnLocator]] = dict(
            _prepared_fill.hollow if _prepared_fill is not None else {})
        for _t, _locs in (stub_hold_hollow or {}).items():
            _hollow[_t] = _hollow.get(_t, frozenset()) | _locs
        _execution = execute_fill(
            routes,
            hollow=_hollow,
            bodies_sha=(_prepared_fill.bodies_sha if _prepared_fill is not None
                        else (lambda _t: "")),
            deps=FillExecutionDeps(
                log=self.tel.log, emit=self.tel.emit,
                check_stop=self.tel.check_stop,
                ledger=self.route_ledger, project=project_dir.name,
                cluster=cluster_name, run_id=run_id, iteration=fill_iteration,
                oracle_fp=oracle_fp, budget_minutes=fill_budget_minutes,
                scheduled=os.getenv("PHASE_C_FILL_SCHEDULE", "1") != "0",
                parallelism=self.config.parallelism,
                unmeasurable_tests=unmeasurable_tests,
                crash_blamed=(crash_blamed or frozenset()) | stub_hold_blockers,
                attributed=frozenset(attributed),
                unit_evidence=unit_evidence,
                body_rows=body_rows,
                max_per_kind=({RouteKind.SESSION: max_sessions_per_fill}
                              if max_sessions_per_fill > 0 else {}),
                session_min_turns=session_min_turns,
                on_wave_kept=on_wave_kept,
                premise=fill_premise, ground_now=fill_ground_now))
        _impl_stats = (_prepared_fill.finish() if _prepared_fill is not None
                       else None)
        _per_fn = _per_fn_route.result
        if _impl_stats is not None:
            _impl_payoff = _impl_stats.payoff
            _fill_hist = _impl_stats.fill_classes or FillClassHistogram()
            if _impl_stats.dispatched_fns is not None:
                _dispatched.update(
                    FnLocator(*t) for t in _impl_stats.dispatched_fns)
            _reverted.update(FnLocator(*t) for t in _impl_stats.reverted_fns)
            _answer_lost.update(
                FnLocator(*t) for t in _impl_stats.answer_lost_fns)
            _carryover = {
                "impls_filled": _impl_stats.impls_filled,
                "impls_failed": _impl_stats.impls_failed,
                "fns_filled_via_impl": _impl_stats.fns_filled,
                "bytes": _impl_stats.bytes_filled,
                "attempts": _impl_stats.attempts,
                "errors": list(_impl_stats.errors),
            }
        if _per_fn is None and _impl_stats is not None:
            # The per-fn route never opened (deferred, or nothing planned):
            # the impl path's result IS the fill's — returned as-is, the way
            # the impl-only early return always did — and the cluster-done
            # event still closes the fill.
            self.tel.emit("phase_c.cluster.done", data={
                "cluster": cluster_name,
                "fns_filled": _impl_stats.fns_filled,
                "bytes_filled": _impl_stats.bytes_filled,
                "llm_calls": _impl_stats.attempts,
                "remaining_stubs": _impl_stats.remaining,
                "errors": len(_impl_stats.errors),
            })
            return (replace(_impl_stats,
                            premise_moved=_execution.premise_moved)
                    if _execution.premise_moved else _impl_stats)
        if _per_fn is None:
            _per_fn = ClusterFillResult(
                remaining=self.ws.scan_stub_tuples(target_src_dir, cluster).unit_count)
        _fill_hist = _fill_hist.combined_with(_per_fn.fill_classes)
        _result = ClusterFillResult(
            fns_filled=_per_fn.fns_filled + int(_carryover.get("fns_filled_via_impl", 0)),
            bytes_filled=_per_fn.bytes_filled + int(_carryover.get("bytes", 0)),
            attempts=_per_fn.attempts + int(_carryover.get("attempts", 0)),
            errors=(*_carryover.get("errors", []), *_per_fn.errors),
            remaining=_per_fn.remaining,
            impls_filled=int(_carryover.get("impls_filled", 0)),
            impls_failed=int(_carryover.get("impls_failed", 0)),
            dispatched_fns=tuple(sorted(_dispatched)),
            reverted_fns=tuple(sorted(_reverted)),
            answer_lost_fns=tuple(sorted(_answer_lost - _reverted)),
            payoff=combine_payoffs(_impl_payoff, _per_fn.payoff),
            fill_classes=_fill_hist,
            premise_moved=_execution.premise_moved,
        )
        self.tel.emit("phase_c.cluster.done", data={
            "cluster": cluster_name,
            "fns_filled": _result.fns_filled,
            "bytes_filled": _result.bytes_filled,
            "llm_calls": _result.attempts,
            "remaining_stubs": _result.remaining,
            "errors": len(_result.errors),
        })
        return _result

    def _per_fn_passes(
        self,
        *,
        project_dir: Path,
        target_src_dir: Path,
        tests_dir_files: list[Path],
        module_name: str,
        cluster_name: str,
        cluster: BuildCluster | None,
        check_cmd: str,
        cluster_env: dict[str, str],
        max_passes: int,
        leaf_name_counts: dict[str, int] | None,
        fr_path: Path | None,
        tdd_frontier_keys: set[FnLocator] | None,
        skip_end_pass_test: bool,
        tdd_frontier_tests: FailureFrontier | None,
        tdd_test_cmd: str | None,
        crash_evidence: dict[FnLocator, str] | None,
        crash_blamed: frozenset[FnLocator] | None,
        crash_blocked: dict[str, str] | None,
        dispatch_ranks: dict[FnLocator, int] | None,
        behaviour_specs: tuple[BehaviourSpec, ...],
        _cfg_dead: frozenset[tuple[str, str]],
        _is_cfg_dead: Callable[[FnLocator], bool],
        _dispatched: set[FnLocator],
        _reverted: set[FnLocator],
        _answer_lost: set[FnLocator],
        landed_before: Callable[[], int],
        claim: Callable[..., bool],
        heal_file: Callable[[str], bool] | None = None,
    ) -> ClusterFillResult:
        """The per-fn route: passes of priority levels over the frontier's
        stubs and re-fill targets. `claim` is the fill schedule's gate on
        the next unit; `landed_before` is what earlier runs of this fill
        landed (the stale-refill probe's premise)."""
        _fill_hist = FillClassHistogram()
        _last_payoff = None
        target_src_dir.mkdir(parents=True, exist_ok=True)
        bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
        fns_filled = 0
        attempts = 0
        # PIPELINE_PARALLELISM (global knob).
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
        invariants_section = fill_invariants_section(
            cluster, no_std=_no_std_invariant, macro_invariants=_macro_invariants)
        # Crate-wide type records, filtered to what THIS cfg-cluster compiles.
        _crate_type_records = self.ws.cluster_filter_type_members(
            self.ws.enumerate_types(target_src_dir) or [], cluster)

        # Floor-stall against (remaining_stubs + last_pass_failures).
        _pass_max_stalls = self.config.pass_max_stalls
        _pass_gauge = FloorStall()
        _pass_last_fail_count = 0
        # Cumulative body-fills landed in this cluster across all passes — the
        # real-time progress signal emitted as `phase_c.fill.progress`. pass=
        # is only re-measured per multi-hour sweep, so this lets monitoring
        # distinguish a frozen tree from mid-sweep splicing.
        _fills_landed_total = 0
        _last_payoff = None

        for pass_n in range(1, max_passes + 1):
            self.tel.check_stop(f"per-fn pass {pass_n}")
            if pass_n > 1 and not claim():
                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: fill share "
                             "spent and the last pass moved no verdict — "
                             "no further pass opened.")
                break
            # Every pass scans for stubs fresh. Cluster-aware: prefer record-based
            # scan (syn enumerator gives container + sig_text). Falls back to
            # name-only tuples when the extractor isn't available.
            recs = self.ws.scan_stub_records(target_src_dir, cluster)
            if recs is not None:
                stubs = [Stub.from_dict(r) for r in recs]
            else:
                stubs = [Stub(file=f, fn_name=fn)
                         for f, fn in self.ws.scan_stub_tuples(
                             target_src_dir, cluster).unit_list()]
            # True remaining stub count BEFORE the frontier filter.
            _all_stubs_count = len(stubs)
            # Pass-start stub keys, also pre-filter: the sibling-blocked U2
            # check needs every cluster-active stub, INCLUDING the
            # exhaustion-skipped ones the frontier no longer carries — those
            # are exactly the blockers the recorded re-admission deadlock
            # (`259637-36db9aad`) was locked behind.
            _pass_stub_keys = frozenset(s.key for s in stubs)
            # Plan 4 — restrict this pass to the TDD frontier's fns when supplied.
            if tdd_frontier_keys is not None:
                stubs = [s for s in stubs if s.key in tdd_frontier_keys]
            # Same cfg verdict the splice enforces, applied before any call is
            # spent (see `_cfg_dead` above) — a stub this cluster never
            # compiles is refused at dispatch, not after a 90-147 s fill.
            _dead_here = [s for s in stubs if _is_cfg_dead(s.key)]
            if _dead_here:
                self.tel.log(
                    f"  [phase-c/per-fn] cluster {cluster_name}: refused "
                    f"{len(_dead_here)} dispatch(es) at cfg-dead stub(s) — "
                    "every occurrence is gated out of this cluster's build: "
                    + ", ".join(sorted(f"{s.file}::{s.fn_name}"
                                       for s in _dead_here)))
                self.tel.emit("phase_c.dispatch.cfg_dead_refused", data={
                    "cluster": cluster_name,
                    "fns": sorted(f"{s.file}::{s.fn_name}"
                                  for s in _dead_here)})
                stubs = [s for s in stubs if not _is_cfg_dead(s.key)]
            # behavioral RE-FILL targets: frontier fns that are NOT
            # stubs (already filled with a compiling-but-wrong body) yet have a
            # failing attributed test. Under `driver=tdd` these were previously
            # dropped (stub-only enumeration) → wrong bodies were permanent. Emit
            # them as `refill=True` Stubs so the SAME fill unit corrects them,
            # guarded by the regression ratchet. Only when the outer loop passes
            # the frontier's failing-test map.
            _refill_recs = self._collect_refill_targets(
                target_src_dir, stubs, tdd_frontier_keys, tdd_frontier_tests,
                crash_blamed=crash_blamed or frozenset())
            # The re-fill list was computed from the PREVIOUS iteration's test
            # run, and the impl/file routes have already written bodies this
            # call — so a target's failing test may already be fixed. Re-fills
            # dispatched off that stale reading rewrite bodies written seconds
            # earlier: measured on 2026-08-11/12, 11-27% of every A/B/C run's
            # wall (one provable byte-identical 99 s no-op re-fill of
            # `checked_div` behind a file-route fill of the same fn). One
            # cargo test (0.0-0.2 s on the fixtures) is the cheap probe: drop
            # every target whose attributed tests all pass now.
            _landed_before = landed_before() + fns_filled
            if (_refill_recs and _landed_before > 0
                    and tdd_test_cmd and tdd_frontier_tests is not None):
                _, _out_probe = self.cargo.run(
                    tdd_test_cmd, target_src_dir, cluster_env, timeout=300)
                # A crashed binary reports no FAILED line. `parse_failed_tests`
                # is "reported fail"; a SIGABRT is unmeasured, not a pass. Run
                # `1911637-d0fab212` iters 3–5 dropped `downcast_ref` and
                # `object_downcast` while `test_large_alignment` was still
                # dangling. `still_open_names` is that third cargo outcome.
                _still_open = still_open_names(_out_probe)
                _fresh_recs = [
                    r for r in _refill_recs
                    if tdd_frontier_tests.tests_for(r.key) & _still_open]
                _stale = [r for r in _refill_recs if r not in _fresh_recs]
                if _stale:
                    self.tel.log(
                        f"  [phase-c/per-fn] {len(_stale)} re-fill target(s) "
                        "dropped — their attributed test(s) already pass "
                        "after this call's earlier fills: " + ", ".join(
                            sorted(f"{r.file}::{r.fn_name}" for r in _stale)))
                    self.tel.emit("phase_c.refill.stale_dropped", data={
                        "cluster": cluster_name,
                        "targets": sorted(f"{r.file}::{r.fn_name}"
                                          for r in _stale)})
                    _refill_recs = _fresh_recs
            if not stubs and not _refill_recs:
                break
            # D: leaf-first order — most-referenced fns first. Stubs first
            # (fill missing pieces), behavioral re-fills after (correct wrong
            # bodies once their prerequisites exist) — and the re-fill tail is
            # leaf-first sorted TOO: `group_priority_levels` below opens a new
            # barriered level at every ref-count change, so appending the
            # re-fills in their source-scan order shattered them into
            # near-singleton levels and the per-level worker pool ran alone
            # (mean concurrency 1.09 over a 5.0 h per-fn phase, 91 % of it
            # strictly serial — run 1159463-b1e352e5 iter 3, 2026-08-09,
            # whose 33-unit batch was all re-fills).
            # When the TDD driver passed a blocker histogram, that rank
            # replaces leaf-first: otherwise the engine re-sorts the batch
            # and a constructor `take_batch` placed at the head is dispatched
            # last because `fmt`/`new` are high-frequency names (run
            # `155810-cbb3b9fc` iter 1). The histogram does not re-open
            # count-change barriers — unique blocker counts serialised the
            # pool (run `346231-54bc1489` iter 1).
            if dispatch_ranks:
                stubs = rank_stubs_by_blocker_counts(
                    stubs + _refill_recs, dispatch_ranks)
            else:
                stubs = (self._leaf_first_records(stubs, leaf_name_counts)
                         + self._leaf_first_records(_refill_recs, leaf_name_counts))
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
            # Every unit's verdict, for the pass-level payoff assessment
            # — `progress_this_pass` counts bodies that LANDED,
            # which is not the same question as whether any could be judged.
            results_this_pass: list[FillResult] = []
            pass_fail_floor = 0  # this pass's failing-test floor

            # Priority-level batching. Leaf-first: equal ref-counts share
            # a level, a count change barriers (callees before callers).
            # Blocker ranks: one ranked pool — those counts are not a
            # compile-order proxy and almost never tie, so a count-change
            # barrier serialised the pass (run `346231-54bc1489`).
            _levels = group_priority_levels(
                stubs, leaf_name_counts, unit_ranks=dispatch_ranks)
            if _levels:
                _head = len(_levels[0])
                _rank_note = (
                    "; blocker ranks do not barrier" if dispatch_ranks else "")
                self.tel.log(
                    f"  [phase-c/per-fn] pass {pass_n}: {len(stubs)} unit(s) in "
                    f"{len(_levels)} level(s), first holds {_head} "
                    f"(pool width {min(_parallelism, _head)}){_rank_note}.")

            # cross-iter exhausted skip: load the retrospectives map once.
            _retros_cache = self.retros.load(project_dir)

            # De-closure: per-pass ctx for the FillOneFn unit.
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
                    cluster_cfg_set(cluster)
                    if cluster is not None else None
                ),
                behaviour_specs=behaviour_specs,
                crash_evidence=crash_evidence,
                crash_blamed=crash_blamed,
                crash_blocked=crash_blocked,
                still_stubbed=_pass_stub_keys,
                cfg_dead=_cfg_dead,
                # Pass-start, cfg-aware: a stub the cluster never compiles
                # cannot panic in its binaries and does not make the tree
                # hollow for the gate's purpose.
                stub_free=not any(not _is_cfg_dead(k) for k in _pass_stub_keys),
            )

            # Units the fill schedule's gate refused this pass (share spent,
            # or their own tests moved under a keep): named once, below.
            _unclaimed: list[str] = []

            def _worker(work_queue: _queue_mod.Queue[tuple[int, Stub]], *,
                        _pass_ctx: PerFnPassCtx = _pass_ctx,
                        filled_this_pass: list[FnLocator] = filled_this_pass,
                        results_this_pass: list[FillResult] = results_this_pass,
                        _unclaimed: list[str] = _unclaimed,
                        pass_n: int = pass_n) -> None:
                # B023: the per-LEVEL queue AND the per-pass ctx/list/number are
                # passed explicitly — free-var reads would rely on late binding.
                nonlocal attempts, fns_filled, progress_this_pass
                nonlocal _fills_landed_total
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
                    # The fill schedule's gate on THIS unit: no new unit once
                    # the share is spent and the last landing moved no
                    # verdict, and none whose own attributed tests a keep of
                    # this fill moved. A unit whose tests did not move is
                    # still standing on its plan; a unit in flight always
                    # completes.
                    _own = (tdd_frontier_tests.tests_for(
                                FnLocator(rec.file, rec.fn_name))
                            if tdd_frontier_tests is not None else set())
                    if not claim(frozenset(_own)):
                        with _counters_lock:
                            _unclaimed.append(f"{rec.file}::{rec.fn_name}")
                        continue
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
                        results_this_pass.append(res)
                        # A unit is DISPATCHED only when a model attempt was
                        # spent on it (attempts==0 is a pre-call skip gate).
                        if res.attempts > 0 or res.filled:
                            _dispatched.add(FnLocator(res.file, res.fn_name))
                            _answer_lost.discard(
                                FnLocator(res.file, res.fn_name))
                        if res.filled:
                            fns_filled += 1
                            progress_this_pass += 1
                            _fills_landed_total += 1
                            self.tel.emit("phase_c.fill.progress", data={
                                "cluster": cluster_name, "pass_n": pass_n,
                                "fills_landed": _fills_landed_total,
                                "file": res.file, "fn": res.fn_name,
                            })
                            filled_this_pass.append(FnLocator(res.file, res.fn_name))
                        if res.error:
                            errors.append(res.error)

            # Dispatch: process priority levels in order, barrier between them.
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

            if _unclaimed:
                self.tel.log(
                    f"  [phase-c/per-fn] pass {pass_n}: {len(_unclaimed)} "
                    "unit(s) not claimed (fill share spent, or a keep of "
                    "this fill moved the tests they were planned on): "
                    + ", ".join(sorted(_unclaimed)[:8])
                    + (f" (+{len(_unclaimed) - 8} more)"
                       if len(_unclaimed) > 8 else ""))
            if _pass_ctx.retro_gate.total:
                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: "
                             f"{_pass_ctx.retro_gate.render()}")
            _pf = FillClassCounter()
            for _res in results_this_pass:
                if _res.fill_class:
                    with suppress(ValueError):
                        _pf.add(FillOutcomeClass(_res.fill_class))
            _fill_hist = _fill_hist.combined_with(_pf.histogram())

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
                    # The boundary invariant: the tree this pass test scores
                    # must parse — the same reading the TDD driver takes
                    # before every sweep, on the legacy route's own sweep.
                    guard_tree_boundary(
                        target_src_dir,
                        validate_parse=self.mut.parse_validator(),
                        heal_file=heal_file, log=self.tel.log,
                        emit=self.tel.emit, tag="[phase-c/per-fn]",
                        when="before the pass test", iter_no=pass_n,
                        cluster=cluster_name)
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
                        # === Per-pass test fix loop ===
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
                            # Classifier-driven retry candidates. Fires after
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
                                # as candidates. (extractor, not regex.)
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
                            # Pre-splice images of every file this attempt
                            # touches: this loop splices with
                            # allow_nonstub=True under NO gate, and its
                            # check-break exit used to leave the broken tree
                            # in place ("leaving for next pass") — the one
                            # site in the fill machinery that could destroy a
                            # compiling tree and keep the wreck.
                            # Byte pre-images of the files the test-fix
                            # re-splices, restored through the transaction
                            # primitive when the re-splice breaks the check.
                            _touched: dict[Path, PreImage] = {}
                            # Map (file, fn) → container via the syn
                            # enumerator so the self-type's definition gets injected.
                            _retry_containers = self.ws.fn_container_index(target_src_dir)
                            for rel_f, fn in fns_to_retry:
                                fp = target_src_dir / rel_f
                                try:
                                    cur_text = fp.read_text(encoding="utf-8")
                                except Exception:
                                    continue
                                # Container-scoped read: `fn` may be one of
                                # several same-named methods in this file (`from`
                                # across the From impls, `not` across the Bool
                                # impls). A bare-name locate returns the FIRST,
                                # so the prompt would show — and the splice below
                                # would target — the wrong definition. Resolve by
                                # (name, container) via the syn record.
                                container_for_fn = _retry_containers.get(
                                    FnLocator(rel_f, fn), "")
                                rec_loc = None
                                if container_for_fn:
                                    rec_loc = self.mut.locate_fn_by_record(
                                        fp, fn, container_for_fn, "")
                                if rec_loc is not None:
                                    sig_t = rec_loc.sig_text
                                    cur_body = rec_loc.body_text
                                else:
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
                                    container=container_for_fn,
                                    type_records=_crate_type_records,
                                    fr_path=fr_path,
                                )
                                # Container filter disambiguates tests for
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
                                        bound_methods=_ctx.bound_methods_section,
                                        identity_must=identity_must_instruction(
                                            behaviour_specs,
                                            units=(IdentityMustUnit(
                                                fn, container_for_fn, sig_t),))),
                                    fr_path=fr_path,  # note recall
                                    attempt_history=test_fix_history.get((rel_f, fn), []),
                                    kind=resolve_kind(None, error_excerpt_test),
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
                                # Container-scoped so a same-named sibling in
                                # another impl is never overwritten.
                                if fp not in _touched:
                                    with suppress(OSError):
                                        _touched[fp] = capture(fp, None)
                                ok2 = self.mut.splice_body_scoped(
                                    fp, fn, container_for_fn, sig_t, new_body,
                                    allow_nonstub=True).ok
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
                                for _timg in _touched.values():
                                    with suppress(OSError):
                                        restore(_timg)
                                self.tel.log(
                                    f"  [phase-c/per-fn] pass {pass_n}: "
                                    "test-fix broke cargo check; restored "
                                    f"{len(_touched)} pre-splice file(s) "
                                    "and stopping.")
                                return FixOutcome("stop")
                            rc, test_out = self.cargo.run(test_cmd, target_src_dir,
                                                          cluster_env, timeout=300)
                            if rc == 0:
                                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: cargo test "
                                             f"PASSED after test-fix attempt {test_fix_attempt}.")
                                return FixOutcome("accepted")
                            new_fail_count = len(parse_failed_tests(test_out))
                            # Persist this iter's cargo test output for post-hoc.
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
                            return FixOutcome("failed", metric=FixProgress(0, new_fail_count))

                        def _on_test_fix_failure(attempt_n: int, outcome: FixOutcome,
                                                 gauge: FloorStall, *,
                                                 pass_n: int = pass_n,
                                                 _stall_seen: dict[str, int] = _stall_seen,
                                                 _iter_ctx: dict[str, Any] = _iter_ctx) -> None:
                            self.tel.emit("phase_c.test_fix.iter", data={
                                "cluster": cluster_name, "pass_n": pass_n,
                                "iter": attempt_n,
                                "prev_fail_count": _iter_ctx["cur"],
                                "new_fail_count": outcome.metric.failing_tests,
                                "delta": outcome.metric.failing_tests - _iter_ctx["cur"],
                                "best": _best_fails(gauge),
                                "improved": gauge.stalls == 0,
                                "stalled": gauge.stalls,
                                "fns_retried": [f"{r}::{f}" for r, f in _iter_ctx["fns"]],
                            })
                            self.tel.log(f"  [phase-c/per-fn] pass {pass_n} test-fix iter "
                                         f"{attempt_n}: "
                                         f"{_iter_ctx['cur']}→{outcome.metric.failing_tests} failures "
                                         f"(floor={gauge.best}, stall={gauge.stalls}).")
                            _stall_seen["best"] = _best_fails(gauge)
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
                            initial_floor=FixProgress(0, _entry_fail_count),
                            check_stop=self.tel.check_stop,
                        )
                        # Floor for the pass-level gauge.
                        pass_fail_floor = 0 if rc == 0 else int(_stall_seen["best"] or 0)

            # Capture last-pass failure count for the pass-level floor.
            _pass_last_fail_count = pass_fail_floor
            _remaining_pass = self.ws.scan_stub_tuples(target_src_dir, cluster).unit_count
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
            # Bodies landed, but could any of them be JUDGED? `is_zero` is the
            # proof (one site blocked every landed body). `unjudgeable_rate`
            # is the rate that does not need a universal blocker: run
            # `155810-cbb3b9fc` iter 1 landed 23 bodies, all keep_deferred,
            # spanning 7 files — intersection empty, rate 1.0, and printed
            # nothing. Stop FURTHER passes; do not abort the pass that just
            # finished (first-fill put the blockers in this batch on purpose).
            _payoff = assess_fill_results(results_this_pass)
            _last_payoff = _payoff
            if _payoff.unjudgeable_rate == 1.0:
                _pay_data = {
                    "pass_n": pass_n, "cluster": cluster_name, "route": "per-fn",
                    **_payoff.event_data(),
                }
                if _payoff.is_zero:
                    self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: PAYOFF ZERO — "
                                 f"{_payoff.summary}. Stopping further passes; "
                                 "the blocker is the only work that can score.")
                    self.tel.emit("phase_c.pass.payoff_zero", data=_pay_data)
                else:
                    self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: UNJUDGEABLE — "
                                 f"{_payoff.rate_summary}. Stopping further "
                                 "passes; the histogram names what the next "
                                 "dispatch must reach.")
                    self.tel.emit("phase_c.pass.unjudgeable", data=_pay_data)
                break
            if _pass_gauge.stalled(_pass_max_stalls):
                self.tel.log(f"  [phase-c/per-fn] cluster {cluster_name}: pass-level floor "
                             f"stalled at metric={_pass_gauge.best} for {_pass_gauge.stalls} "
                             f"pass(es) — stopping early at pass {pass_n}/{max_passes}.")
                break

        bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
        remaining = self.ws.scan_stub_tuples(target_src_dir, cluster).unit_count
        return ClusterFillResult(
            fns_filled=fns_filled,
            bytes_filled=bytes_after - bytes_before,
            attempts=attempts,
            errors=tuple(errors),
            remaining=remaining,
            dispatched_fns=tuple(sorted(_dispatched)),
            reverted_fns=tuple(sorted(_reverted)),
            answer_lost_fns=tuple(sorted(_answer_lost - _reverted)),
            payoff=_last_payoff,
            fill_classes=_fill_hist,
        )


class _PerFnRoute:
    """The engine's own route: the per-fn passes, as ONE run of the fill
    schedule (its levels are a compile-order barrier inside the route, not
    a scheduling unit, and their composition is only known once the earlier
    runs have landed)."""

    kind = RouteKind.PER_FN
    parallel = False

    def __init__(self, engine: PhaseCFillEngine, **kw: Any) -> None:
        self._engine = engine
        self._kw = kw
        self.result: ClusterFillResult | None = None

    def plan(self) -> Sequence[RouteRun]:
        eng = self._engine
        kw = self._kw
        keys: set[FnLocator] | None = kw["tdd_frontier_keys"]
        recs = eng.ws.scan_stub_records(kw["target_src_dir"], kw["cluster"])
        if recs is not None:
            stubs = [Stub.from_dict(r) for r in recs]
        else:
            stubs = [Stub(file=f, fn_name=fn)
                     for f, fn in eng.ws.scan_stub_tuples(
                         kw["target_src_dir"], kw["cluster"]).unit_list()]
        if keys is not None:
            stubs = [st for st in stubs if st.key in keys]
        # A cfg-dead stub is refused by the pass itself, with its own event
        # (`phase_c.dispatch.cfg_dead_refused`): it stays a reason to open
        # the run and never a target the run may write.
        live = [st for st in stubs if not kw["is_cfg_dead"](st.key)]
        refills = eng._collect_refill_targets(
            kw["target_src_dir"], live, keys, kw["tdd_frontier_tests"],
            crash_blamed=kw["crash_blamed"] or frozenset())
        stub_locs = frozenset(st.key for st in live)
        targets = stub_locs | frozenset(r.key for r in refills)
        if not targets and not stubs:
            return []
        frontier: FailureFrontier | None = kw["tdd_frontier_tests"]
        unit_tests = tuple(
            (loc, frozenset(str(t).rsplit("::", 1)[-1]
                            for t in (frontier.tests_for(loc)
                                      if frontier is not None else ())))
            for loc in sorted(targets, key=lambda k: (k.file, k.fn_name)))
        return [RouteRun(
            kind=self.kind,
            label=f"per-fn {len(stub_locs)} stub(s), {len(refills)} re-fill(s)",
            targets=targets,
            target_tests=frozenset().union(*(ts for _, ts in unit_tests)),
            stubs_in_targets=stub_locs,
            files=tuple(sorted({k.file for k in targets})),
            # The passes claim unit by unit, so the schedule may hand this
            # route any subset of the targets (`carve_rebuys`).
            unit_tests=unit_tests)]

    def run(self, run: RouteRun, clock: RunClock) -> RouteRunReport:
        if not clock.may_claim():
            return RouteRunReport(empty=True)
        eng = self._engine
        kw = dict(self._kw)
        if run.parent:
            # A carved run: the schedule deferred the rest of the planned
            # targets as re-buys. The passes write only what this run holds.
            kw["tdd_frontier_keys"] = set(run.targets)
        before_disp = frozenset(kw["dispatched"])
        before_rev = frozenset(kw["reverted"])
        self.result = eng._per_fn_passes(
            project_dir=kw["project_dir"], target_src_dir=kw["target_src_dir"],
            tests_dir_files=kw["tests_dir_files"], module_name=kw["module_name"],
            cluster_name=kw["cluster_name"], cluster=kw["cluster"],
            check_cmd=kw["check_cmd"], cluster_env=kw["cluster_env"],
            max_passes=kw["max_passes"], leaf_name_counts=kw["leaf_name_counts"],
            fr_path=kw["fr_path"], tdd_frontier_keys=kw["tdd_frontier_keys"],
            skip_end_pass_test=kw["skip_end_pass_test"],
            tdd_frontier_tests=kw["tdd_frontier_tests"],
            tdd_test_cmd=kw["tdd_test_cmd"], crash_evidence=kw["crash_evidence"],
            crash_blamed=kw["crash_blamed"], crash_blocked=kw["crash_blocked"],
            dispatch_ranks=kw["dispatch_ranks"],
            behaviour_specs=kw["behaviour_specs"], _cfg_dead=kw["cfg_dead"],
            _is_cfg_dead=kw["is_cfg_dead"], _dispatched=kw["dispatched"],
            _reverted=kw["reverted"], _answer_lost=kw["answer_lost"],
            landed_before=kw["landed_before"], claim=clock.may_claim,
            heal_file=kw["heal_file"])
        res = self.result
        disp = frozenset(kw["dispatched"]) - before_disp
        rev = frozenset(kw["reverted"]) - before_rev
        return RouteRunReport(
            model_calls=res.attempts, kept=frozenset(disp - rev),
            reverted=frozenset(rev), dispatched=frozenset(disp),
            classes={k: v for k, v in (res.fill_classes or FillClassHistogram()
                                       ).as_dict().items() if v},
            empty=(res.attempts == 0 and not disp))
