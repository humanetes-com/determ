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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
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
from pipeline_core.application.reconstruct.unit_conversation import TranscriptBound
from pipeline_core.domain.attribution import (
    FailureFrontier,
    canon_site,
    frontier_for_run,
    locator_of,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_select import IdentityMustUnit
from pipeline_core.domain.cfg import cluster_cfg_set
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.cluster_plan import fill_invariants_section
from pipeline_core.domain.crash_settlement import with_held_stub_evidence
from pipeline_core.domain.crash_site import CrashSites
from pipeline_core.domain.dispatch import (
    impl_route_covers,
)
from pipeline_core.domain.enums import FillUnit, RouteOrder
from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
)
from pipeline_core.domain.fill_schedule import (
    HolderAdmission,
    HolderQueue,
    IdleMove,
    RouteKind,
    RouteRun,
    RunClock,
    UnitEvidence,
)
from pipeline_core.domain.joint_write import DEFAULT_JOINT_MAX
from pipeline_core.domain.last_error import resolve_kind
from pipeline_core.domain.live_stubs import LiveStubs
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
    refill_admission,
    stamp_reads_dispatch,
)
from pipeline_core.domain.refine import FixProgress, FloorStall
from pipeline_core.domain.refuted_body import refuted_text_for
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_outcome import PremiseEntry
from pipeline_core.domain.test_results import (
    native_crashes,
    parse_binary_results,
    parse_failed_tests,
    still_open_names,
)
from pipeline_core.ports.fill_engine import (
    Cargo,
    EngineTelemetry,
    FillPassPort,
    ImplFillPath,
    Mutator,
    PromptContext,
    Retros,
    StallPolicy,
    TestLinker,
    WorkspaceModel,
)
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.layout_authoring import behaviour_must_instruction
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt
from pipeline_core.prompts.retry_turn import (
    BODY_OUTPUT,
    AttemptFeedback,
    EvidenceSection,
    refuted_feedback,
    types_new_to,
)
from pipeline_core.threads import context_thread


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
    # Impl-route unit cap (see `impl_route_covers`).
    per_impl_max_stubs: int = 12
    # Fill-route sequencing. Under FILE_PRIMARY the file-first pass takes
    # every frontier file (free fns included).
    route_order: RouteOrder = RouteOrder.DEFAULT
    # How many other functions of its file one per-fn reply may write with
    # its target (`domain.joint_write`); `PER_FN_JOINT_MAX=0` restores the
    # single-body contract — the negative control of the joint write.
    per_fn_joint_max: int = field(default_factory=lambda: int(
        os.getenv("PER_FN_JOINT_MAX", str(DEFAULT_JOINT_MAX))))


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
    # Behaviour few-shot: load the cluster's harvested behaviours for the few-shot
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
        *,
        every_stub_key: frozenset[FnLocator],
        crash_sites: CrashSites | None = None,
    ) -> list[Stub]:
        """Frontier fns that are already FILLED (not stubs) but have a failing
        attributed test → behavioral re-fill targets (`refill=True`).

        "Filled" is read against ``every_stub_key`` — every stub in the tree,
        under no cluster — never against ``stubs`` alone, which the pass has
        already narrowed to the cluster's cfg-set and the frontier. Absence
        from that narrowed list is not evidence of a body: a stub this
        cluster never compiles is absent from it too, and was admitted here
        as a re-fill (run `473471-33301154`, 2026-09-29, `src/nightly.rs::fmt`
        ×2, `refill: true`, 31.6 min). A filled body at a cfg-dead site is
        still returned; the caller refuses stubs and re-fills together
        against one dead-site set (`_per_fn_targets`).
        Empty unless the TDD outer loop supplied both the frontier keys and the
        failing-test map (i.e. `driver=tdd`); the legacy `fn_walk` driver has its
        own internal test-fix loop and passes neither.

        Same predicate as ``collect_impl_refill_targets``, including
        ``file_primary``: under that order the file pass writes the block
        and the impl route drops it, so a dispatch-only batch member must
        still stamp or it is stranded (run ``125003-269e2338``). A locator
        in ``crash_blamed`` is a refill even when no test convicts it: a
        crashed binary reports no verdict, and without the stamp the
        blamed conversion never reaches the write list. The blame admits the
        container a culprit frame resolved to (``crash_sites``), not every
        homonym of the locator.

        A frontier key that is neither a stub nor an enumerated fn resolves to
        nothing and would drop out here without a trace — the failure mode that
        let a crash-blamed fn sit unfixed for entire runs while its blame line
        was printed every sweep. Such keys are reported, never swallowed."""
        if not (tdd_frontier_keys and tdd_frontier_tests):
            return []
        crashed = crash_blamed or frozenset()
        at_site = crash_sites or CrashSites()
        _stub_keys = set(every_stub_key) | {s.key for s in stubs}
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
                and refill_admission(tdd_frontier_tests.tests_for(loc),
                                     crash_blamed=at_site.blames(
                                         crashed, m.file, m.fn_name,
                                         m.container)) is not None)
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
            if refill_admission(tests, crash_blamed=at_site.blames(
                    crashed, rec.file, rec.fn_name, rec.container)) is None:
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

    def _cluster_stubs(self, target_src_dir: Path, cluster: Any) -> list[Stub]:
        """Every stub the cluster compiles on the tree as it is now, by
        record when the extractor runs (container + sig_text), by name
        otherwise. The one scan the pass start, the run plan and the live
        stub reader share."""
        recs = self.ws.scan_stub_records(target_src_dir, cluster)
        if recs is not None:
            return [Stub.from_dict(r) for r in recs]
        return [Stub(file=f, fn_name=fn)
                for f, fn in self.ws.scan_stub_tuples(
                    target_src_dir, cluster).unit_list()]

    def _every_stub_key(self, target_src_dir: Path) -> frozenset[FnLocator]:
        """Every stub of the tree under no cluster filter — what "not a
        stub" is read against when the re-fill collector infers a body."""
        recs = self.ws.scan_stub_records(target_src_dir, None)
        if recs is not None:
            return frozenset(Stub.from_dict(r).key for r in recs)
        return frozenset(FnLocator(f, fn) for f, fn in self.ws.scan_stub_tuples(
            target_src_dir, None).unit_list())

    def _per_fn_targets(
        self, target_src_dir: Path, stubs: list[Stub],
        tdd_frontier_keys: set[FnLocator] | None,
        tdd_frontier_tests: FailureFrontier | None,
        *,
        crash_blamed: frozenset[FnLocator],
        is_cfg_dead: Callable[[FnLocator], bool],
        crash_sites: CrashSites | None = None,
    ) -> tuple[list[Stub], list[Stub], list[Stub]]:
        """The per-fn route's `(stubs, re-fills, refused)`: the re-fills the
        collector admits, then ONE cfg-dead refusal over stubs and re-fills
        together. The route plan and every pass read their targets here, so
        a site the cluster never compiles cannot be a target of either,
        whether or not a body sits in it."""
        refills = self._collect_refill_targets(
            target_src_dir, stubs, tdd_frontier_keys, tdd_frontier_tests,
            crash_blamed=crash_blamed,
            every_stub_key=self._every_stub_key(target_src_dir),
            crash_sites=crash_sites)
        dead = [s for s in (*stubs, *refills) if is_cfg_dead(s.key)]
        if not dead:
            return stubs, refills, []
        return ([s for s in stubs if not is_cfg_dead(s.key)],
                [r for r in refills if not is_cfg_dead(r.key)], dead)

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
        # Units the owner map admitted (the red tests executed them): graded
        # as coverage, never with the escalation's futility sentence.
        measured_owner_keys: frozenset[FnLocator] = frozenset(),
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
        # The subset of crash_blamed an invalid-memory crash blamed
        # (`crash_site.memory_blamed_locators`): the only units the file
        # plan's ownership MUST is about.
        crash_memory_blamed: frozenset[FnLocator] | None = None,
        # The blame and evidence at the bodies the frames ran
        # (`crash_site.crash_sites_of`): the refill admission and the
        # routes' member grades narrow a locator blame to that container.
        crash_sites: CrashSites | None = None,
        crash_blocked: dict[str, str] | None = None,
        culprits_of: Callable[[str], Collection[FnLocator]] | None = None,
        # Leftover tests whose binary did not complete. The fill schedule
        # ranks a run whose every aimed test is in this set, and whose
        # targets miss crash_blamed, as a flat re-buy (weight 0).
        unmeasurable_tests: frozenset[str] = frozenset(),
        # Locators leftover tests convict before owner-map admission
        # (eligible plus hop-pair members). Empty skips coverage-split
        # marking so a call that does not snapshot keeps today's order.
        attributed: Collection[FnLocator] = (),
        # The four facts the schedule check turns into owed owners
        # (`schedule_invariants.violations` calls `measured_owners_owed`).
        # A precomputed owner set is not an argument: the whole frontier
        # named owners the rank had left inside its bound.
        owner_admitted: Collection[FnLocator] = (),
        owner_ordered: Collection[FnLocator] = (),
        owner_batch: Collection[FnLocator] = (),
        owner_starved: Collection[FnLocator] = (),
        # What the red tests owe this fill (`candidate_tiers.due_reading`):
        # each gets an opened writer whatever deferred it.
        due: Collection[FnLocator] = (),
        # Each owed function's ladder rung (`candidate_tiers.LADDER`): one
        # owed on the session rung is covered only by an opened session run.
        # None: every owed function is owed on the unit rung.
        due_rungs: Mapping[FnLocator, str] | None = None,
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
        # constructor the rank placed in the slice actually OPENS
        # the pass — leaf-first otherwise puts `fmt`/`new` in an earlier
        # barriered level (run `155810-cbb3b9fc` iter 1: constructors in
        # the batch, dispatched last).
        dispatch_ranks: dict[FnLocator, int] | None = None,
        # Failing test -> the crate fns it executes (the sweep's owner map
        # plus its attributed units): the red-slice session's editable set.
        session_slices: Mapping[str, frozenset[FnSite]] | None = None,
        joint_slices: Sequence[SessionSlice] | None = None,
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
        # Each claimed unit's bound as a multiple of its run's share
        # (`FILL_UNIT_SHARE_MULTIPLE`; `unit_clock`); 0 = unbounded.
        unit_share_multiple: float = 0.0,
        # `(rel) -> healed?`: the heal hierarchy on one file of the tree,
        # for the boundary read before the per-pass test. None reports the
        # break and lets the pass test read it (the fixture path).
        heal_file: Callable[[str], bool] | None = None,
        # The driver's unit-ledger writer, called by the fill executor after
        # every wave (and on the wave a stop cuts) with the units the wave
        # kept, so a fill that never returns still leaves its `landed` rows.
        on_wave_kept: Callable[[frozenset[FnLocator]], object] | None = None,
        # The ground this fill's plan was made from and a reader of the
        # ground now (`test_outcome.premise_of`); both set, the fill ends at
        # the first claim after a keep that moved it (`fill_scheduler`).
        fill_premise: Mapping[str, PremiseEntry] | None = None,
        fill_ground_now: Callable[[], Mapping[str, PremiseEntry] | None] | None = None,
        fill_held_now: Callable[[], Mapping[str, frozenset[FnSite]] | None] | None = None,
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
        # oversized blocks back to the per-fn path below.
        #
        # Stubs before re-fills is the SCHEDULE's order, never a narrowing of
        # the fill here. This engine used to replace a whole TDD fill with the
        # frontier's stubs whenever one was a free fn / oversized block the
        # impl route never attempts and a re-fill target was also on the
        # frontier, on the premise that such a stub aborts the test binary and
        # every re-fill judged against it is reverted as a sibling crash. It
        # never read whether any stub did. Run `1038428-3372223f` (2026-09-23,
        # cluster `stable`) spent iterations 4-6 at pass 161 fail 23 on
        # `per-fn 6 / 4 / 2 stub(s), 0 re-fill(s)` with every session, pair,
        # file and impl run withheld, while the sweep's stub hold named no
        # stub (no failing test first-failed at one) and each landing read
        # `executed 184 -> 184; no regression`. Both halves of the premise
        # have owners now: isolation re-runs an aborted binary one test per
        # process, so a stub abort cannot erase a sibling's verdict, and a
        # stub that does hold a test reaches `execute_fill` as a blocker
        # (`stub_hold_blockers`, `stub_hold_hollow`), whose run the schedule
        # opens first.
        # Union of fns any route genuinely attempted this call — returned to
        # the TDD driver, whose exhaustion charging must not outrun dispatch
        # (an undispatched frontier fn is refused, not judged).
        _dispatched: set[FnLocator] = set()
        _reverted: set[FnLocator] = set()
        # Units whose per-fn fill LANDED (the target, or a joint extra it
        # wrote). The per-fn route's report is read from it: a dispatched
        # unit that is not here came back with its pre-fill body, and was
        # reported "kept" until 2026-10-16 — run `1057870-b427a3a2` fill 2
        # withheld the reverted `vtable` from the file run as "an earlier run
        # of this fill kept a body for".
        _landed_fn: set[FnLocator] = set()
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
        # same verdict now, at the same source of truth. The verdict is a
        # property of the SITE (`cluster_membership.cluster_dead_sites`), not
        # of the stub: a dead fn some route already filled is still dead.
        _cfg_dead = self.ws.cluster_dead_sites(target_src_dir, cluster)

        def _is_cfg_dead(key: FnLocator) -> bool:
            return key in _cfg_dead

        _impl_payoff = None
        _fill_hist = FillClassHistogram()
        _prepared_fill: PreparedFill | None = None
        _carryover: dict[str, Any] = {}
        if self.config.fill_unit is FillUnit.IMPL:
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
                measured_owner_keys=measured_owner_keys,
                ledger_entries=ledger_entries,
                oracle_fp=oracle_fp,
                tdd_test_cmd=tdd_test_cmd,
                parallelism=self.config.parallelism,
                crash_evidence=crash_evidence,
                crash_blamed=crash_blamed,
                crash_memory_blamed=crash_memory_blamed,
                crash_sites=crash_sites,
                crash_blocked=crash_blocked,
                culprits_of=culprits_of,
                behaviour_specs=behaviour_specs,
                dispatch_ranks=dispatch_ranks,
                session_slices=session_slices,
                joint_slices=joint_slices,
                session_owners=session_owners,
                held=stub_hold_hollow,
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
            crash_sites=crash_sites,
            ledger_entries=ledger_entries, oracle_fp=oracle_fp,
            crash_blocked=crash_blocked, culprits_of=culprits_of,
            dispatch_ranks=dispatch_ranks,
            behaviour_specs=behaviour_specs, cfg_dead=_cfg_dead,
            is_cfg_dead=_is_cfg_dead, dispatched=_dispatched,
            landed_fn=_landed_fn,
            reverted=_reverted, answer_lost=_answer_lost,
            landed_before=(_prepared_fill.landed if _prepared_fill is not None
                           else (lambda: 0)),
            heal_file=heal_file)
        routes: list[FillRoute] = []
        if _prepared_fill is not None:
            routes.extend(_prepared_fill.routes)
        routes.append(_per_fn_route)
        # The held view (`held_stubs`) is the only hollow relation: a test is
        # hollow when its first failure sits in a still-stubbed unit, never
        # because its coverage slice passes through one.
        _hollow: dict[str, frozenset[FnLocator]] = dict(stub_hold_hollow or {})
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
                crash_blamed=(crash_sites or CrashSites()).blame_map(
                    (crash_blamed or frozenset()) | stub_hold_blockers),
                attributed=frozenset(attributed),
                owner_admitted=frozenset(owner_admitted),
                owner_ordered=tuple(owner_ordered),
                owner_batch=frozenset(owner_batch),
                owner_starved=frozenset(owner_starved),
                due=frozenset(due),
                due_rungs=dict(due_rungs or {}),
                unit_evidence=unit_evidence,
                body_rows=body_rows,
                max_per_kind=({RouteKind.SESSION: max_sessions_per_fill}
                              if max_sessions_per_fill > 0 else {}),
                session_min_turns=session_min_turns,
                unit_share_multiple=unit_share_multiple,
                on_wave_kept=on_wave_kept,
                premise=fill_premise, ground_now=fill_ground_now,
                held_now=fill_held_now))
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
        culprits_of: Callable[[str], Collection[FnLocator]] | None,
        dispatch_ranks: dict[FnLocator, int] | None,
        behaviour_specs: tuple[BehaviourSpec, ...],
        _cfg_dead: CfgDeadSites,
        _is_cfg_dead: Callable[[FnLocator], bool],
        _dispatched: set[FnLocator],
        _reverted: set[FnLocator],
        _answer_lost: set[FnLocator],
        landed_before: Callable[[], int],
        claim: Callable[..., bool],
        heal_file: Callable[[str], bool] | None = None,
        claim_refusal: Callable[..., str] | None = None,
        claim_release: Callable[[], None] | None = None,
        crash_sites: CrashSites | None = None,
        _landed_fn: set[FnLocator] | None = None,
        write_sites: frozenset[FnSite] | None = None,
        take_holders: Callable[[], list[HolderAdmission]] | None = None,
        holders: HolderQueue | None = None,
        admit_measured: Callable[[], None] | None = None,
        ledger_entries: Sequence[Any] = (),
        oracle_fp: str = "",
    ) -> ClusterFillResult:
        """The per-fn route: passes of priority levels over the frontier's
        stubs and re-fill targets. `claim` is the fill schedule's gate on
        the next unit; `landed_before` is what earlier runs of this fill
        landed (the stale-refill probe's premise). `write_sites`, when set,
        are the only bodies the passes may write, container-exact: a run
        opened on bodies the fill measured writes those, not every body of
        their names (`_PerFnRoute.run`). `take_holders` drains the holders
        the claim gate handed this run (`RunClock.take_holders`): when a
        claim is answered "take", the worker writes those bodies first —
        judged on the tests the fill measured in them — and asks for its
        own unit again. `holders` is that run's queue: with it, a worker
        whose plan queue is empty stays a claimer of the level
        (`HolderQueue.idle_move`), asking the gate itself
        (`admit_measured`, `RunClock.admit_measured`) and taking the front
        body, until the front is empty and no worker is inside a claim;
        the level then has the run's full width, since a holder admitted
        in it is written before its join."""
        _fill_hist = FillClassHistogram()
        _last_payoff = None
        # Write sets replies named as one (`FillResult.joint_named`).
        _joint_sets: list[tuple[FnLocator, ...]] = []
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
        # The run's holder queue keeps the pool's claimers; without a way to
        # take a body from it, a worker returns when its plan queue is empty.
        _claimer = holders if take_holders is not None else None

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
            cluster, no_std=_no_std_invariant, macro_invariants=_macro_invariants,
            worlds=self.ws.all_clusters())
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
        # Every unit the claim gate refused in any pass, by its last reason.
        _unclaimed_all: dict[str, str] = {}

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
            stubs = self._cluster_stubs(target_src_dir, cluster)
            # True remaining stub count BEFORE the frontier filter.
            _all_stubs_count = len(stubs)
            # Every cluster-active stub by body, before any filter: a holder
            # the claim gate hands this run is one of them, wherever the
            # frontier or the write set left it.
            _stub_by_site = {_site_of(st): st for st in stubs}
            # The judges that ask "is this sibling still a stub" (the
            # sibling-blocked U2 check, the regression gate's `stub_free`)
            # read the tree when they judge, through the same pre-filter
            # cluster scan: every cluster-active stub, INCLUDING the
            # exhaustion-skipped ones the frontier no longer carries — those
            # are exactly the blockers the recorded re-admission deadlock
            # (`259637-36db9aad`) was locked behind. Never this pass-start
            # list: a fill runs for hours (`domain/live_stubs.py`).
            _live_stubs = LiveStubs(
                scan=lambda: frozenset(
                    s.key for s in self._cluster_stubs(target_src_dir, cluster)),
                cfg_dead=_cfg_dead)
            # Restrict this pass to the TDD frontier's fns when supplied.
            if tdd_frontier_keys is not None:
                stubs = [s for s in stubs if s.key in tdd_frontier_keys]
            # behavioral RE-FILL targets: frontier fns that are NOT
            # stubs (already filled with a compiling-but-wrong body) yet have a
            # failing attributed test. Under `driver=tdd` these were previously
            # dropped (stub-only enumeration) → wrong bodies were permanent. Emit
            # them as `refill=True` Stubs so the SAME fill unit corrects them,
            # guarded by the regression ratchet. Only when the outer loop passes
            # the frontier's failing-test map.
            # Then the cfg verdict the splice enforces, applied before any call
            # is spent (see `_cfg_dead` above), to stubs AND re-fills from the
            # same set — a site this cluster never compiles is refused at
            # dispatch, not after a 90-147 s fill.
            stubs, _refill_recs, _dead_here = self._per_fn_targets(
                target_src_dir, stubs, tdd_frontier_keys, tdd_frontier_tests,
                crash_blamed=crash_blamed or frozenset(),
                is_cfg_dead=_is_cfg_dead, crash_sites=crash_sites)
            if write_sites is not None:
                _off_site = [s for s in (*stubs, *_refill_recs)
                             if _site_of(s) not in write_sites]
                if _off_site:
                    self.tel.log(
                        f"  [phase-c/per-fn] cluster {cluster_name}: "
                        f"{len(_off_site)} body(ies) of the opened names are "
                        "not the bodies the fill measured, not written in "
                        "this run: " + ", ".join(sorted(
                            f"{s.file}::{s.fn_name} [{s.container}]"
                            for s in _off_site)))
                stubs = [s for s in stubs if _site_of(s) in write_sites]
                _refill_recs = [s for s in _refill_recs
                                if _site_of(s) in write_sites]
            if _dead_here:
                self.tel.log(
                    f"  [phase-c/per-fn] cluster {cluster_name}: refused "
                    f"{len(_dead_here)} dispatch(es) at cfg-dead site(s) — "
                    "every occurrence is gated out of this cluster's build: "
                    + ", ".join(sorted(f"{s.file}::{s.fn_name}"
                                       + (" (re-fill)" if s.refill else "")
                                       for s in _dead_here)))
                self.tel.emit("phase_c.dispatch.cfg_dead_refused", data={
                    "cluster": cluster_name,
                    "fns": sorted(f"{s.file}::{s.fn_name}"
                                  for s in _dead_here)})
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
                # The collector admitted on failing tests OR crash blame; the
                # probe re-asks the same question of its own reading. Blame
                # holds while the probe still shows a native crash, by the
                # localizer's own definition (`native_crashes`): the probe's
                # capture is isolated, so its aborting binaries complete and
                # "a binary did not complete" can never hold on it.
                _crash_now = bool(native_crashes(
                    parse_binary_results(_out_probe)))
                _blamed = crash_blamed or frozenset()
                _at_site = crash_sites or CrashSites()
                _fresh_recs = [
                    r for r in _refill_recs
                    if refill_admission(
                        tdd_frontier_tests.tests_for(r.key) & _still_open,
                        crash_blamed=_crash_now and _at_site.blames(
                            _blamed, r.file, r.fn_name, r.container))
                    is not None]
                _stale = [r for r in _refill_recs if r not in _fresh_recs]
                if _stale:
                    _was_blamed = sorted(f"{r.file}::{r.fn_name}"
                                         for r in _stale if r.key in _blamed)
                    self.tel.log(
                        f"  [phase-c/per-fn] {len(_stale)} re-fill target(s) "
                        "dropped — no attributed test still open and no crash "
                        "still observed after this call's earlier fills: "
                        + ", ".join(sorted(f"{r.file}::{r.fn_name}"
                                           for r in _stale))
                        + (f" (crash-blamed, crash gone: {', '.join(_was_blamed)})"
                           if _was_blamed else ""))
                    self.tel.emit("phase_c.refill.stale_dropped", data={
                        "cluster": cluster_name,
                        "targets": sorted(f"{r.file}::{r.fn_name}"
                                          for r in _stale),
                        "crash_blamed": _was_blamed})
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
            # and a constructor the rank placed at the head is dispatched
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
            _all_recs: list[dict[str, Any]] = []
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
                ledger_entries=ledger_entries,
                oracle_fp=oracle_fp,
                crash_blamed=crash_blamed,
                crash_sites=crash_sites,
                crash_blocked=crash_blocked,
                culprits_of=culprits_of,
                stubs=_live_stubs,
                cfg_dead=_cfg_dead,
                fn_records=tuple(_all_recs),
                joint_max=self.config.per_fn_joint_max,
            )

            # Units the fill schedule's gate refused this pass, by the
            # clock's reason (`RunClock.refusal`): named once, below.
            _unclaimed: dict[str, list[str]] = {}
            # Bodies a worker has claimed or taken this pass: a queued unit
            # the claim gate already handed a worker as a holder is not
            # written twice.
            _taken: set[FnSite] = set()
            _taken_idx = [len(stubs)]

            def _admitted_ctx(adm: HolderAdmission, *,
                              _pass_ctx: PerFnPassCtx = _pass_ctx
                              ) -> PerFnPassCtx:
                """The pass context a handed holder is prompted and judged
                with: the tests the fill measured in its body join the run's
                frontier, and its held-stub sentence the crash evidence —
                the same two readings an opened run is given
                (`_PerFnRoute.run`)."""
                return replace(
                    _pass_ctx,
                    tdd_frontier_tests=frontier_for_run(
                        _pass_ctx.tdd_frontier_tests, adm.held_tests),
                    crash_evidence=with_held_stub_evidence(
                        _pass_ctx.crash_evidence,
                        ((locator_of(st), t) for st, t in adm.held_tests)))

            # `_unit` and `_take` call each other (a handed holder's own
            # claim can be answered "take"); the cell is filled below.
            _take_ref: list[Callable[[HolderAdmission], None]] = []

            def _unit(idx: int, rec: Stub, ctx: PerFnPassCtx, *,
                      _take_ref: list[Callable[[HolderAdmission], None]] = _take_ref,
                      _unclaimed: dict[str, list[str]] = _unclaimed,
                      filled_this_pass: list[FnLocator] = filled_this_pass,
                      results_this_pass: list[FillResult] = results_this_pass,
                      pass_n: int = pass_n) -> None:
                """Claim one body and, when granted, fill it. A claim
                answered "take" writes the holders handed to this run first,
                then asks again."""
                nonlocal attempts, fns_filled, progress_this_pass
                nonlocal _fills_landed_total
                # The fill schedule's gate on THIS unit: no new unit once
                # the share is spent and the last landing moved no
                # verdict, and none whose own attributed tests a keep of
                # this fill moved. A unit whose tests did not move is
                # still standing on its plan; a unit in flight always
                # completes.
                _unit_loc = frozenset({FnLocator(rec.file, rec.fn_name)})
                # The tests the keep will run for this body, not the
                # locator's dispatch pool. When the sweep told two
                # containers apart, a test measured in the other body
                # is not this claim's ground (`ground_for_body`).
                _front = ctx.tdd_frontier_tests
                _own = (_front.ground_for_body(
                            rec.file, rec.fn_name, rec.container).judged
                        if _front is not None else frozenset())
                while True:
                    _why = (claim_refusal(frozenset(_own), _unit_loc)
                            if claim_refusal is not None
                            else ("" if claim(frozenset(_own), _unit_loc)
                                  else "refused"))
                    if _why != "take" or take_holders is None:
                        break
                    # Another worker may have drained the front first: the
                    # claim is then asked again with nothing pending.
                    for _adm in take_holders():
                        _take_ref[0](_adm)
                if _why:
                    with _counters_lock:
                        _unclaimed.setdefault(_why, []).append(
                            f"{rec.file}::{rec.fn_name}")
                    return
                try:
                    res = self.fill_one.run(idx, rec, ctx=ctx)
                except Exception as _e:
                    # Isolate worker failures so one bad fn doesn't kill the
                    # worker thread (others keep draining the queue).
                    self.tel.log(f"  [phase-c/per-fn] worker error on "
                                 f"{rec.file}::{rec.fn_name}: {_e}")
                    with _counters_lock:
                        errors.append(f"{rec.file}::{rec.fn_name}: "
                                      f"worker exception: {_e}")
                    return
                finally:
                    # The unit returned: the call it reserved on the
                    # run's share is spent, and in the clock's elapsed.
                    if claim_release is not None:
                        claim_release()
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
                        if _landed_fn is not None:
                            _landed_fn.add(FnLocator(res.file, res.fn_name))
                        fns_filled += 1
                        progress_this_pass += 1
                        _fills_landed_total += 1
                        self.tel.emit("phase_c.fill.progress", data={
                            "cluster": cluster_name, "pass_n": pass_n,
                            "fills_landed": _fills_landed_total,
                            "file": res.file, "fn": res.fn_name,
                        })
                        filled_this_pass.append(FnLocator(res.file, res.fn_name))
                    # A joint write's extras are bodies this unit landed:
                    # dispatched like the target, so the landed-change
                    # audit and the unit ledger grade them.
                    _joint_sets.extend(res.joint_named)
                    for _extra in res.joint:
                        _dispatched.add(_extra)
                        _answer_lost.discard(_extra)
                        if _landed_fn is not None:
                            _landed_fn.add(_extra)
                    if res.error:
                        errors.append(res.error)

            def _take(adm: HolderAdmission, *, pass_n: int = pass_n,
                      _admitted_ctx: Callable[[HolderAdmission], PerFnPassCtx] = _admitted_ctx,
                      _stub_by_site: dict[FnSite, Stub] = _stub_by_site,
                      _taken: set[FnSite] = _taken,
                      _taken_idx: list[int] = _taken_idx,
                      _unit: Callable[[int, Stub, PerFnPassCtx], None] = _unit,
                      ) -> None:
                """Write the holders one admission hands this run, each on
                the context that carries the tests measured in it. A holder
                that is no cluster-active stub at pass start, or a site this
                cluster never compiles, is named and not written."""
                ctx = _admitted_ctx(adm)
                for st, _t in adm.held_tests:
                    site = canon_site(st)
                    rec = _stub_by_site.get(site)
                    if rec is None or _is_cfg_dead(rec.key):
                        self.tel.log(
                            f"  [phase-c/per-fn] pass {pass_n}: holder "
                            f"{st.file}::{st.fn_name} [{st.container}] "
                            "handed to this run is "
                            + ("cfg-dead in this cluster"
                               if rec is not None else
                               "not a stub of this cluster's pass")
                            + " — not written")
                        continue
                    with _counters_lock:
                        if site in _taken:
                            continue
                        _taken.add(site)
                        _taken_idx[0] += 1
                        idx = _taken_idx[0]
                    _unit(idx, rec, ctx)

            _take_ref.append(_take)

            def _worker(work_queue: _queue_mod.Queue[tuple[int, Stub]], *,
                        _pass_ctx: PerFnPassCtx = _pass_ctx,
                        _taken: set[FnSite] = _taken,
                        _unit: Callable[[int, Stub, PerFnPassCtx], None] = _unit,
                        _take: Callable[[HolderAdmission], None] = _take,
                        _claimer: HolderQueue | None = _claimer,
                        pass_n: int = pass_n) -> None:
                # B023: the per-LEVEL queue AND the per-pass ctx/number are
                # passed explicitly — free-var reads would rely on late binding.
                try:
                    while True:
                        # Stop pulling NEW work once another worker (or the
                        # main thread) has seen a graceful-stop request —
                        # raises in THIS thread immediately, same as the
                        # per-fn check inside `fill_one.run` below; the main
                        # thread's own check (after the join below) is what
                        # actually ends the process.
                        self.tel.check_stop(
                            f"per-fn pass {pass_n} worker dispatch")
                        if _claimer is None:
                            try:
                                item: tuple[int, Stub] | None = (
                                    work_queue.get_nowait())
                            except _queue_mod.Empty:
                                return
                        else:
                            item = _claimer.claim_plan(
                                lambda: _pull_plan(work_queue))
                        if item is not None:
                            try:
                                idx, rec = item
                                with _counters_lock:
                                    if _site_of(rec) in _taken:
                                        continue
                                    _taken.add(_site_of(rec))
                                _unit(idx, rec, _pass_ctx)
                            finally:
                                if _claimer is not None:
                                    _claimer.leave_claim()
                            continue
                        assert _claimer is not None and take_holders is not None
                        # The plan queue is empty: still a claimer while a
                        # sibling is inside a claim or a body waits at the
                        # front.
                        move = _claimer.idle_move(admit_measured)
                        if move is IdleMove.EXIT:
                            return
                        if move is IdleMove.TAKE:
                            try:
                                for _adm in take_holders():
                                    _take(_adm)
                            finally:
                                _claimer.leave_claim()
                finally:
                    if _claimer is not None:
                        _claimer.worker_ended()

            # Dispatch: process priority levels in order, barrier between them.
            for _level in _levels:
                _work_queue: _queue_mod.Queue[tuple[int, Stub]] = _queue_mod.Queue()
                for _item in _level:
                    _work_queue.put(_item)
                _nw = min(_parallelism, len(_level))
                if _claimer is not None:
                    # Holders admitted in this level are written in it, side
                    # by side: the pool is the run's width, not the plan
                    # units', and an idle worker parks instead of leaving.
                    _nw = max(_nw, _parallelism, 1)
                    _claimer.arm(_nw)
                if _nw > 1:
                    _workers = [
                        context_thread(_worker, args=(_work_queue,),
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

            for _why_u, _units_u in _unclaimed.items():
                for _name_u in _units_u:
                    _unclaimed_all[_name_u] = _why_u
            _why_text = {
                "premise": "a keep of this fill moved the tests they were "
                           "planned on",
                "share": "the run's share is spent and its last landing "
                         "bought nothing",
            }
            for _why, _units in sorted(_unclaimed.items()):
                self.tel.log(
                    f"  [phase-c/per-fn] pass {pass_n}: {len(_units)} "
                    f"unit(s) not claimed ({_why_text.get(_why, _why)}): "
                    + ", ".join(sorted(_units)[:8])
                    + (f" (+{len(_units) - 8} more)"
                       if len(_units) > 8 else ""))
            if _unclaimed:
                self.tel.emit("phase_c.fn.unclaimed", data={
                    "cluster": cluster_name, "pass_n": pass_n,
                    "by_reason": {k: sorted(v)
                                  for k, v in sorted(_unclaimed.items())}})
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
            # TDD driver runs the cluster test in its OUTER loop; skip when asked.
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
                        # One conversation per (file, fn) across this pass's
                        # test-fix iterations: the first re-ask opens it with
                        # the fix prompt, every later one is a turn carrying
                        # the new test output.
                        test_fix_convs: dict[tuple[str, str], Any] = {}
                        _entry_fail_count = len(parse_failed_tests(test_out))
                        _stall_seen: dict[str, int] = {
                            "best": _entry_fail_count, "stalls": 0}
                        _iter_ctx: dict[str, Any] = {"cur": 0, "fns": []}

                        def _attempt_test_fix(test_fix_attempt: int, *,
                                              pass_n: int = pass_n,
                                              filled_this_pass: list[FnLocator] = filled_this_pass,
                                              test_cmd: str = test_cmd,
                                              test_fix_convs: dict[tuple[str, str], Any] = test_fix_convs,
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
                                # A crash settlement's row quotes the body the
                                # stack ran; the note states only that
                                # history, whatever body is on disk now.
                                _refuted = refuted_text_for(
                                    ledger_entries, oracle_fp, ((rel_f, fn),))
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
                                _conv = test_fix_convs.get((rel_f, fn))
                                if _conv is None:
                                    _conv = test_fix_convs[(rel_f, fn)] = (
                                        self.prompt.open_conversation(
                                            f"{rel_f}::{fn}", route="test-fix",
                                            sources=None))
                                _kind = resolve_kind(None, error_excerpt_test)
                                turn: str | AttemptFeedback = AttemptFeedback(
                                    kind=_kind, excerpt=error_excerpt_test,
                                    targets=(fn,), output=BODY_OUTPUT,
                                    sections=(
                                        EvidenceSection(
                                            "TYPES_IN_SCOPE", types_new_to(
                                                _conv.text(), error_excerpt_test,
                                                _crate_type_records,
                                                viewer_file=rel_f),
                                            "Types this result names that were "
                                            "not shown above:\n"),
                                        EvidenceSection(
                                            "MODEL_NOTES", _ctx.notes_section),
                                        *refuted_feedback(_refuted))
                                ) if _conv.opened else build_per_fn_fix_prompt(
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
                                        refuted_body=_refuted,
                                        behaviour_must=behaviour_must_instruction(
                                            behaviour_specs,
                                            units=(IdentityMustUnit(
                                                fn, container_for_fn, sig_t, rel_f),))),
                                    fr_path=fr_path,  # note recall
                                    kind=_kind,
                                )
                                attempts += 1
                                attempts_log_t: list[str] = []
                                try:
                                    new_body, _ = self.prompt.ask_per_fn_body(
                                        turn, fn, attempts_log_t, conversation=_conv,
                                        sig_text=sig_t)
                                except TranscriptBound:
                                    continue
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
                            if not any_progress:
                                self.tel.log(f"  [phase-c/per-fn] pass {pass_n}: no fn could "
                                             f"be re-spliced for failing tests — stopping.")
                                return FixOutcome("stop")

                            # Re-run cargo check (cheap) then cargo test.
                            # Through the check port, not a bare run: that port is
                            # where the tree is also built in the other cfg worlds.
                            rc_c, out_c, _ = self.cargo.check_diag(
                                check_cmd, target_src_dir, cluster_env)
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
            joint_sets=tuple(dict.fromkeys(_joint_sets)),
            unclaimed_fns=tuple(sorted(
                (n.rsplit("::", 1)[0], n.rsplit("::", 1)[-1], why)
                for n, why in _unclaimed_all.items())),
        )


def _fold_per_fn_results(prev: ClusterFillResult | None,
                         new: ClusterFillResult) -> ClusterFillResult:
    """One fill's per-fn result over every per-fn run it opened.

    A fill can open the per-fn route more than once: a carved re-buy piece,
    a yielded piece queued again, and a run the fill opens on a blocker its
    own keeps measured (`fill_scheduler._open_for_blockers`). The route kept
    the last run's result only, so the fill's counts, errors, payoff and
    class histogram described its last per-fn run. Counts add, the lists
    concatenate, the stub count and the dispatch sets are the latest (the
    passes read them off the tree and the shared sets, which already span
    the fill)."""
    if prev is None:
        return new
    hist = (prev.fill_classes or FillClassHistogram()).combined_with(
        new.fill_classes)
    return replace(
        new,
        fns_filled=prev.fns_filled + new.fns_filled,
        bytes_filled=prev.bytes_filled + new.bytes_filled,
        attempts=prev.attempts + new.attempts,
        errors=(*prev.errors, *new.errors),
        payoff=combine_payoffs(prev.payoff, new.payoff),
        fill_classes=hist,
        joint_sets=tuple(dict.fromkeys((*prev.joint_sets, *new.joint_sets))),
        unclaimed_fns=tuple(sorted(set(prev.unclaimed_fns)
                                   | set(new.unclaimed_fns))))


def _site_of(stub: Stub) -> FnSite:
    """A stub record's body, in the canonical form sites compare in."""
    return canon_site(FnSite(stub.file, stub.fn_name, stub.container))


def _pull_plan[T](work_queue: _queue_mod.Queue[T]) -> T | None:
    """The next plan unit of a level, or None once the level queue is empty."""
    try:
        return work_queue.get_nowait()
    except _queue_mod.Empty:
        return None


class _PerFnRoute:
    """The engine's own route: the per-fn passes, as ONE run of the fill
    schedule (its levels are a compile-order barrier inside the route, not
    a scheduling unit, and their composition is only known once the earlier
    runs have landed)."""

    kind = RouteKind.PER_FN
    parallel = False
    #: Its units claim one by one, so a holder a keep of the fill measures
    #: is written at the next claim instead of after the run
    #: (`HolderQueue`, `_per_fn_passes`' ``take_holders``).
    takes_holders = True

    def __init__(self, engine: PhaseCFillEngine, **kw: Any) -> None:
        self._engine = engine
        # The sweep's frontier is held apart from the pass arguments: the
        # plan reads it, and a run is handed only the frontier
        # `frontier_for_run` builds for that run, so no run can be judged on
        # the sweep's owner sets while it carries tests the fill measured.
        self._sweep_frontier: FailureFrontier | None = kw.pop(
            "tdd_frontier_tests")
        self._kw = kw
        self.result: ClusterFillResult | None = None

    def plan(self) -> Sequence[RouteRun]:
        eng = self._engine
        kw = self._kw
        keys: set[FnLocator] | None = kw["tdd_frontier_keys"]
        stubs = eng._cluster_stubs(kw["target_src_dir"], kw["cluster"])
        if keys is not None:
            stubs = [st for st in stubs if st.key in keys]
        # A cfg-dead site — stub or filled — is refused by the pass itself,
        # with its own event (`phase_c.dispatch.cfg_dead_refused`): it stays
        # a reason to open the run and never a target the run may write.
        live, refills, dead = eng._per_fn_targets(
            kw["target_src_dir"], stubs, keys, self._sweep_frontier,
            crash_blamed=kw["crash_blamed"] or frozenset(),
            is_cfg_dead=kw["is_cfg_dead"], crash_sites=kw.get("crash_sites"))
        stub_locs = frozenset(st.key for st in live)
        targets = stub_locs | frozenset(r.key for r in refills)
        if not targets and not dead:
            return []
        frontier = self._sweep_frontier
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
        # No opening claim over every target at once: each unit claims on
        # its own (`_per_fn_passes`), so a premise move, a share or a wait
        # behind a writer is decided per body, and a holder the gate hands
        # this run is written at the first claim after the keep that
        # measured it. A run every unit of which is refused makes no call
        # and is reported empty below.
        eng = self._engine
        kw = dict(self._kw)
        if run.parent:
            # A carved run: the schedule split the planned targets (re-buys,
            # or a coverage split's attributed write and its other-file
            # extras). The passes write only what this run holds.
            kw["tdd_frontier_keys"] = set(run.targets)
        if run.held_tests:
            # A run opened on blockers the fill measured: the sweep held no
            # test in them, so its crash evidence says nothing; the run's own
            # ground states how many tests now stop in each stub.
            kw["crash_evidence"] = with_held_stub_evidence(
                kw["crash_evidence"],
                ((locator_of(s), t) for s, t in run.held_tests))
        # The frontier THIS run is judged and prompted with: the sweep's,
        # plus the tests the fill measured in the bodies the run was opened
        # on. Every gate, prompt and verdict of the passes reads it.
        run_frontier = frontier_for_run(self._sweep_frontier, run.held_tests)
        # And it writes the bodies it was opened on, not every body of their
        # names: a homonym no held test stops in was not measured, and its
        # gate would read a set the ground put on another body.
        write_sites = (frozenset(canon_site(s) for s, _t in run.held_tests)
                       if run.held_tests else None)
        before_disp = frozenset(kw["dispatched"])
        before_rev = frozenset(kw["reverted"])
        before_landed = frozenset(kw["landed_fn"])
        res = eng._per_fn_passes(
            project_dir=kw["project_dir"], target_src_dir=kw["target_src_dir"],
            tests_dir_files=kw["tests_dir_files"], module_name=kw["module_name"],
            cluster_name=kw["cluster_name"], cluster=kw["cluster"],
            check_cmd=kw["check_cmd"], cluster_env=kw["cluster_env"],
            max_passes=kw["max_passes"], leaf_name_counts=kw["leaf_name_counts"],
            fr_path=kw["fr_path"], tdd_frontier_keys=kw["tdd_frontier_keys"],
            skip_end_pass_test=kw["skip_end_pass_test"],
            tdd_frontier_tests=run_frontier,
            tdd_test_cmd=kw["tdd_test_cmd"], crash_evidence=kw["crash_evidence"],
            ledger_entries=kw.get("ledger_entries", ()),
            oracle_fp=kw.get("oracle_fp", ""),
            crash_blamed=kw["crash_blamed"], crash_blocked=kw["crash_blocked"],
            culprits_of=kw.get("culprits_of"),
            dispatch_ranks=kw["dispatch_ranks"],
            behaviour_specs=kw["behaviour_specs"], _cfg_dead=kw["cfg_dead"],
            _is_cfg_dead=kw["is_cfg_dead"], _dispatched=kw["dispatched"],
            _reverted=kw["reverted"], _answer_lost=kw["answer_lost"],
            landed_before=kw["landed_before"], claim=clock.may_claim,
            heal_file=kw["heal_file"],
            claim_refusal=clock.refusal, claim_release=clock.release,
            crash_sites=kw.get("crash_sites"),
            _landed_fn=kw["landed_fn"], write_sites=write_sites,
            take_holders=clock.take_holders, holders=clock.holders,
            admit_measured=clock.admit_measured)
        self.result = _fold_per_fn_results(self.result, res)
        disp = frozenset(kw["dispatched"]) - before_disp
        kept = frozenset(kw["landed_fn"]) - before_landed
        rev = (frozenset(kw["reverted"]) - before_rev) | (disp - kept)
        return RouteRunReport(
            model_calls=res.attempts, kept=frozenset(kept - rev),
            reverted=frozenset(rev), dispatched=frozenset(disp),
            classes={k: v for k, v in (res.fill_classes or FillClassHistogram()
                                       ).as_dict().items() if v},
            empty=(res.attempts == 0 and not disp),
            joint_sets=res.joint_sets,
            filtered={FnLocator(f, n): why
                      for f, n, why in res.unclaimed_fns
                      if FnLocator(f, n) not in disp})


def engine_run_is_the_fill_pass(engine: PhaseCFillEngine) -> FillPassPort:
    """The engine's `run`, as the TDD driver's fill port. Never called for its
    value: mypy checks the return, so a keyword the port declares and `run`
    does not take (or one `run` requires and the port omits) fails the type
    gate instead of the first fill of a live run."""
    return engine.run
