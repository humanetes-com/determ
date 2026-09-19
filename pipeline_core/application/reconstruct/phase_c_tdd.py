"""Phase-C TDD fill driver — relocated from pipeline.py.

The per-cluster test-driven fill loop: scan stubs → leaf-first frontier → fill via the
PhaseC fn engine → re-run cluster tests → attribute failures → repeat under a FloorStall
budget. Collaborators are injected via `PhaseCTddDeps` (the composition root binds the
concrete pipeline.py helpers, late-bound for the monkeypatch seams); pure domain helpers
(Attempt/LoopState/test parsers) are imported directly. Body relocated verbatim.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, overload

if TYPE_CHECKING:
    from pipeline_core.application.retrigger_layout import RetriggerResult

from pipeline_core.application.reconstruct.attribution import make_fn_site_locator
from pipeline_core.application.reconstruct.body_reseed import (
    ReseedOutcome,
    TreeWriter,
    reseed,
)
from pipeline_core.application.reconstruct.close_stub_account import report_close_stubs
from pipeline_core.application.reconstruct.impl_fill import followup_think_env_on
from pipeline_core.application.reconstruct.tdd_ratchet import (
    breach_withheld,
    closure_stub_sites,
    closure_withheld,
    hollow_count,
    ratchet_breach,
    restore_src_tree,
    snapshot_due,
    snapshot_src_tree,
)
from pipeline_core.application.reconstruct.tree_txn import guard_tree_boundary
from pipeline_core.domain import fill_phase_clock
from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureFrontier,
    locator_of,
    site_payload,
    union_unattributed_stubs,
)
from pipeline_core.domain.best_tree_mark import BestTreeMark
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    SweepView,
    authored_layout_fp,
    body_sha,
    classify,
    flat_standing_unchanged,
    judgment_attributed,
    latest_by_unit,
    rebuy_by_grade,
    rebuy_hits,
    rows_for_units,
    standing_on_tree,
)
from pipeline_core.domain.coverage_slice import (
    bind as bind_coverage_slice,
)
from pipeline_core.domain.coverage_slice import (
    red_dead_units,
    withhold_unmeasured,
)
from pipeline_core.domain.crash_site import (
    blamed_locators,
    callee_subjects,
    chain_derived_site,
    crash_evidence_by_locator,
    crash_signal_hint,
    destruction_path_subjects,
    origin_blamed_locators,
    placeholder_pointer_subjects,
    read_placeholder_pointer_fault,
    vanish_is_stub_confounded,
    vanished_subject_fns,
)
from pipeline_core.domain.destruction_epoch import (
    BoundaryDecision,
    EpochedBaseline,
    readmission_boundary,
)
from pipeline_core.domain.dispatch import (
    blocker_histogram,
    defer_blocked_consumers,
    restrict_to_compile_blockers,
)
from pipeline_core.domain.dispatch_batch import (
    DEFAULT_MAX_DISPATCH,
    CrashCohort,
    drop_standing_flat,
    overlay_crash_on_ranks,
    promote_by_blocker_counts,
    promote_by_service,
    promote_crash_evidence,
    promote_producers,
    promote_product_ahead,
    take_batch,
)
from pipeline_core.domain.escalation_lifetime import (
    FiredProduct,
    escalate_to_measured_owners,
    escalation_fire_due,
    product_addition_keys,
    sustain_escalation_products,
    withholding_paired_tests,
)
from pipeline_core.domain.evidence_premise import evidence_fingerprints
from pipeline_core.domain.fill_class import FillClassHistogram
from pipeline_core.domain.fill_schedule import (
    DEFAULT_UNPAID_GRACE,
    RouteLedgerRow,
    unopened_pair_fills,
    unpaid_pair_buys,
)
from pipeline_core.domain.flat_score import (
    FlatScoreDecision,
    ScoreReading,
    UnmovedVerdicts,
    flat_score,
)
from pipeline_core.domain.fn_execution import FnCoverageRecord
from pipeline_core.domain.graceful_stop import request_graceful_stop
from pipeline_core.domain.installable_fns import producer_dispatch_ranks
from pipeline_core.domain.landed_change import (
    changed_units,
    declined_unchanged,
    pass_unopened,
    render_pass_audit,
    reoffer_unopened,
    split_reverted,
    unattempted_units,
    unit_bodies,
)
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.layout_retrigger import (
    DEFAULT_REFILL_GRACE_MAX_ITERS,
    DEFAULT_REFILL_PER_ITER,
    CompileEvidence,
    CrashEvidence,
    RefillGrace,
)
from pipeline_core.domain.loop_facts import (
    Fact,
    PassClosed,
    SweepMeasured,
)
from pipeline_core.domain.measurement import (
    Measurement,
    VerdictReconciliation,
    code_identity,
    failing_binaries_by_name,
    qualified_verdicts,
    tree_content_sha,
)
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
    FnSite,
    Frontier,
    SiteFrontier,
)
from pipeline_core.domain.oracle_ledger import (
    SweepOracle,
    bare_test_name,
    reconcile_sweep,
)
from pipeline_core.domain.pair_fill import dispatch_including_pairs
from pipeline_core.domain.pass_highwater import PassHighWater
from pipeline_core.domain.plateau import (
    plateau_exit_disabled,
    plateau_exit_due,
)
from pipeline_core.domain.reasoning_cap import (
    collect_stream_meta,
    summarize_capped,
    summarize_followup_salvage,
    units_all_replies_capped,
)
from pipeline_core.domain.reference_floor import (
    ReferenceMeasurement,
    judge_cluster_binaries,
    reference_floor_close_reason,
)
from pipeline_core.domain.refill_stamp import format_traits_used
from pipeline_core.domain.refine import (
    Attempt,
    LoopState,
)
from pipeline_core.domain.scaffold_rewrite import find_definition_span
from pipeline_core.domain.slice_session import (
    attributed_units_for_session,
    recorded_locator_sites,
    session_slices_for_failing,
)
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.tdd_floor import (
    UNSET_PASS,
    TddFloor,
    render_pass_floor,
    resume_floor,
)
from pipeline_core.domain.test_cmd import expected_target_labels
from pipeline_core.domain.test_command import ensure_serial_harness
from pipeline_core.domain.test_outcome import (
    PremiseEntry,
    StubHold,
    names_of,
    outcomes_of,
    premise_moved,
    premise_of,
    stub_hold_of,
    truncated_failing,
)
from pipeline_core.domain.test_report import (
    failure_evidence_by_test,
    merge_observations,
    parse_test_output,
)
from pipeline_core.domain.test_results import (
    BinaryResult,
    TestStateReading,
    build_broke,
    compile_error_digest,
    hung_sections,
    lib_compile_clean,
    parse_binary_results,
    parse_failed_tests,
    parse_test_counts,
    summarize_test_binaries,
    sweep_build,
    uncoded_src_diagnostics,
)
from pipeline_core.domain.zero_payoff import (
    SweepOutcome,
    paid_off,
    unpaid_streak,
    zero_payoff_streak,
)


class FilterTestTargets(Protocol):
    """Preserve key type: dispatch stays Frontier, conviction stays SiteFrontier.

    ``Callable[..., Frontier]`` was a lie that let ``convicting.get(FnLocator)``
    type-check after conviction became site-keyed (commit 59a314c, 2026-08-29).
    ``Callable[..., Frontier | SiteFrontier]`` is the other lie — every
    dispatch assignment then became the union. Overloads are the seam
    ``filter_test_targets`` already has in ``test_targets.py``.
    """

    @overload
    def __call__(self, frontier: Frontier, src_dir: Path) -> Frontier: ...

    @overload
    def __call__(self, frontier: SiteFrontier, src_dir: Path) -> SiteFrontier: ...

    def __call__(
        self, frontier: Frontier | SiteFrontier, src_dir: Path,
    ) -> Frontier | SiteFrontier: ...


@dataclass(frozen=True, slots=True)
class PhaseCTddDeps:
    """Collaborators the TDD fill driver needs, injected by the composition root."""

    attribute_failures: Callable[..., AttributionResult]
    build_test_reverse_map: Callable[..., dict[str, set[FnLocator]]]
    filter_test_targets: FilterTestTargets
    check_graceful_stop: Callable[[str], None]
    check_auto_operator: Callable[[int, str], None]
    converge_in_cluster: Callable[..., str | None]
    count_relevant_notes: Callable[..., int]
    effective_stalls: Callable[..., int]
    emit: Callable[..., None]
    log: Callable[[str], None]
    order_frontier: Callable[..., list[tuple[FnLocator, set[str]]]]
    record_tdd_regression: Callable[..., str]
    reset_exhaustion_flags: Callable[..., list[str]]
    run_cargo: Callable[..., tuple[int, str]]
    # Both scans return a `StubInventory` — sites AND units by name. A
    # consumer that needs a count or a sequence says which cardinality
    # it means; the bare list that repeated a locator once per impl site
    # is what the cluster-entry re-seed iterated on 2026-09-04.
    scan_unimplemented_for_cluster: Callable[..., StubInventory]
    scan_unimplemented_functions: Callable[..., StubInventory]
    task2_fill_fns_one_by_one: Callable[..., ClusterFillResult]
    # Futility escalation: (src_dir, test, futile_units,
    # futile_containers) -> Frontier additions. None disables escalation.
    escalate_to_producers: Callable[..., Frontier] | None = None
    # Suspend the stored retro guidance of units the escalation just declared
    # futile: (project_dir, src_dir, futile_units, cluster_name, tests) ->
    # stamped keys. The record's guidance advises the exact rewrite futility
    # measured as changing nothing — left admissible it steers the fix loop
    # against the escalation (the ChainState `Vec` guidance vs the tree's
    # `vec::IntoIter`, run `2773540-e47c3754`). None disables.
    suspend_futile_guidance: Callable[..., list[str]] | None = None
    # Persist a new pass high-water fill tree to heal's restore tiers:
    # (project_dir, cluster_name, filled_dir, BestTreeMark) -> None.
    # None disables checkpointing (heal then has only excision + skeleton).
    checkpoint_fill_tree: Callable[..., None] | None = None
    # What the durable snapshot already on disk says about itself:
    # (project_dir, cluster) -> BestTreeMark | None. None here means the driver
    # cannot see the slot and every sweep may replace it — the pre-seed
    # behaviour, and what a unit test not exercising the tier wants.
    load_best_tree_mark: Callable[..., Any] | None = None
    # Put the cluster's durable best tree back, BOTH halves:
    # (project_dir, cluster, filled_dir) -> (bodies, layout_files, leftovers)
    # or None when the slot cannot supply a pair. None here (or a None return)
    # means the loop cannot take a failed re-climb back and the flat-score
    # guard stops the run instead — the pre-restore behaviour.
    restore_best_tree_pair: Callable[..., tuple[int, int, list[str]] | None] \
        | None = None
    # Release the disk-persisted exhaustion verdicts whose recorded blocking
    # stubs have filled:
    # (project_dir, {file: still-stubbed fn names}, cluster) -> count.
    # None disables the release, which is the pre-fix behaviour: a unit
    # convicted for calling `unimplemented!()` siblings stays convicted after
    # they fill, and only a cluster-wide score move can free it.
    retire_unblocked_convictions: Callable[..., int] | None = None
    # Release the disk-persisted exhaustion verdicts of a futility escalation's
    # TARGETS: (project_dir, src_dir, targets, reason) -> released store keys.
    # The escalation already re-admits its targets from the process-local set;
    # the store's `fully_exhausted` flag is a second gate on the same unit
    # that the local release never reached — run `1246060-2339497b`
    # (2026-08-21) entered `StdErrorforContextError<C,Error>` into the
    # frontier on every sweep and the impl route skipped it at every dispatch.
    # None disables (the pre-fix behaviour).
    release_escalated_convictions: Callable[..., list[str]] | None = None
    # The cluster's positive census of cfg-dead stubs: (src_dir, cluster) ->
    # frozenset of (file, fn) this cluster's cfg-set never compiles. Threaded
    # to the blocker release so a record `blocked_on` a cfg-dead name is read
    # as unblocked, and so an empty cluster-scoped stub map can be told apart
    # from a failed scan. None keeps the release's old guard.
    cfg_dead_stub_names: Callable[..., frozenset[tuple[str, str]]] | None = None
    # Stage A, the durable loop floor: (project_dir, cluster) -> the persisted
    # mapping or None, and (project_dir, cluster, mapping) -> None. Both None
    # keeps the pre-Stage-A behaviour (a fresh streak every process), which is
    # what every unit test that does not exercise resume wants.
    load_tdd_floor: Callable[..., Any] | None = None
    save_tdd_floor: Callable[..., None] | None = None
    # Layout re-author: (project_dir, module, cluster_name, target_src_dir,
    # error_text) -> RetriggerResult, tried once at the stall-exit before giving
    # up on the cluster. None disables it — the exit is then unconditional.
    retrigger_layout: Callable[..., RetriggerResult] | None = None
    # Oracle reconciliation at the sweep level: (project_dir, cluster) -> (expected_count,
    # expected_names) — the cfg-aware static test inventory. None disables
    # oracle reconciliation — sweeps then trust whatever cargo reported.
    active_test_inventory: Callable[..., tuple[int, frozenset[str]]] | None = None
    # Which FILE declares each active test name: (project_dir, cluster, stems,
    # lib) -> {test name: rel path}. The anchor the subject-synthesis channel
    # needs — its only other anchor is a module path, and the set it is handed
    # is bare by construction, which is why that channel had never fired once
    # since it shipped. None disables the anchor (the module-path parse alone).
    test_file_index: Callable[..., dict[str, str]] | None = None
    # Crash localizer: (sweep_output, test_cmd, workspace_root, env) ->
    # tuple[CrashSite, ...] — gdb fast path + ASAN fallback. None disables
    # localization (no CRASH_EVIDENCE prompts, no crash-first frontier).
    localize_crashes: Callable[..., tuple[Any, ...]] | None = None
    # One-time retro evidence migration: (project_dir) -> (cleared, retired).
    # Clears exhaustion verdicts + retires guidance recorded under the old
    # crash-confounded whole-run gate. None disables.
    migrate_retro_evidence: Callable[..., tuple[int, int]] | None = None
    # One-time retro gate migration: (project_dir) -> (flags, budgets) cleared.
    # Voids convictions earned under the pre-comparative acceptance gate and
    # the pre-group route set. None disables.
    migrate_retro_gate: Callable[..., tuple[int, int]] | None = None
    # Compile-error attribution: (cargo_output, target_src_dir) -> the fns the
    # rustc diagnostics implicate. Used only while the build is RED, to aim the
    # frontier at what the compiler names. None disables the narrowing — a red
    # sweep then dispatches from the behavioural frontier, as it used to.
    implicated_fns: Callable[..., list[FnLocator]] | None = None
    # (project_dir, cluster_root, cluster_name, cluster_env, tests_by_file)
    # -> rendered `observed:` lines per failing test, from the assertion
    # probe (`assert_probe_runner`). None disables the observations.
    assert_probe: Callable[..., Mapping[str, str]] | None = None
    # Every fn the tree declares, as extractor records (`file`, `fn_name`,
    # `body_text`, …): (src_dir) -> records | None. Read for ONE question — the
    # crate's own destruction family, when a crash reads as a double drop and
    # the construction chain therefore cannot contain the fault. None disables
    # that narrowing; the chain-derived pack is then the full producer closure
    # it always was.
    enumerate_fns: Callable[..., list[dict[str, Any]] | None] | None = None
    # Every type the tree declares, as extractor `TypeRecord`s. Read to tell
    # which escalated units CONSTRUCT a crate type (the constructor role), so
    # the escalated-producer ordering can put them ahead of the delegators
    # that merely call them. None disables that ordering; the escalated cohort
    # then dispatches in `order_frontier`'s name-frequency order as before.
    enumerate_types: Callable[..., list[Any] | None] | None = None
    # --- the proven-body ledger (`domain/body_ledger.py`). Every one of these
    # is None-disabled: with none of them wired the loop behaves exactly as it
    # did before the ledger existed, which is what makes the write path
    # shippable on its own.
    # Rows the ledger already holds for this project/cluster.
    ledger_load: Callable[..., list[LedgerEntry]] | None = None
    # Append rows (a landing, or a sweep's verdict on one). Append-only: a body
    # that was proven and later refuted must keep both rows.
    ledger_append: Callable[..., int] | None = None
    # The crate's authored item set, `name -> (kind, definition)`. Used ONLY to
    # know which names are layout items when computing a body's dependencies —
    # never as the layout gate itself, which reads the cluster skeleton (see
    # `body_ledger.authored_layout_fp`).
    layout_items: Callable[..., dict[str, tuple[str, str]]] | None = None
    # Splice/revert/read-signature over a live tree, injected because they are
    # adapters and this is the application layer.
    tree_writer: TreeWriter | None = None
    # `cargo check` over the tree a re-seed just wrote: (project_dir, cluster)
    # -> (ok, error excerpt). Takes the whole cluster, never a loose env
    # mapping, for the reason `compile_skeleton_check` documents: a dict of env
    # vars cannot express "this cluster needs the nightly toolchain". None
    # means the re-seed runs UNCHECKED and says so in its line — never
    # silently.
    check_tree_builds: Callable[..., tuple[bool, str]] | None = None
    # The structure reader's parse check, `(path) -> parses?`. Two duties:
    # the re-seed transaction parse-verifies every touched file with it,
    # and the iteration boundary reads the whole `src/` tree through it
    # before a sweep — a file the extractor cannot parse has no
    # enumerable unit, so no fill route can open it, and run
    # `2973-66e50ad9` spent three sweeps and a cluster abort on one
    # (2026-09-04). None disables both readings.
    parse_ok: Callable[[Path], bool] | None = None
    # `(rows, bytes)` for the cluster-entry size line.
    ledger_size: Callable[..., tuple[int, int]] | None = None
    # This process's run identity, stamped on every ledger append so a later
    # process can tell "a prior process graded this unit" from "this process
    # just wrote the row". Empty in tests that do not exercise the re-buy
    # detector.
    run_id: str = ""
    # The fingerprint of the oracle scoring the cluster tree: `(src_dir,
    # project_dir, cluster) -> str` over the tree's gen_tests siblings, the
    # crate's `tests/` and the baked behaviour suite
    # (`adapters/oracle_fingerprint.py`). Stamped on every row the ledger
    # writes; required by the re-seed gate and the re-buy detector. None
    # stamps "" and the gate then refuses every row — the honest reading of
    # "nobody recorded which tests graded this" (a layout-only ledger would
    # have re-seeded 73 of 85 stable units onto the cold run's regenerated
    # oracle, 2026-08-26).
    oracle_fp: Callable[..., str] | None = None
    # Coverage of the filled tree, taken on a staged copy with its own
    # CARGO_TARGET_DIR (instrumentation must not touch the loop's target
    # dir). Returns a CoverageSlice of dead locators / dead-region
    # sentences / per-failing-test owner sets, or None when the reading
    # cannot be taken. Called with `failing_tests=` so the owner map is
    # measured on the red set only. None here disables dead-body
    # demotion, the coverage instruction AND both reachability refusals
    # (the pre-loop gate in `fill_one_fn`, `screen_guidance`'s
    # `convictors_never_execute`); ranking then uses service/flat only.
    measure_coverage_slice: Callable[..., Any] | None = None
    # (project_dir, test names) -> the recorded executed set per test, from
    # the retrospectives store's conviction-time entry stamps. Read for the
    # red tests the coverage probe could NOT measure this sweep (their
    # process did not complete): the recorded set stands in as their owner
    # set for ranking, admission and the red-slice sessions (positive
    # evidence only), tagged recorded in the owner-map event. `dead` is
    # withheld while any run is unmeasured (`withhold_unmeasured`) whether
    # or not a record exists.
    recorded_entry_sets: Callable[..., Mapping[str, frozenset[FnLocator]]] | None = None
    # (project_dir) -> per-unit text of the retrospectives store's rows about
    # it, the `retro` channel of the evidence premise
    # (`domain/evidence_premise`, `adapters/retros.retro_evidence_texts`).
    # That channel carries both the guidance a prompt may inject and the
    # cross-fill LAST_ERROR the pair route reads off the same record, so
    # without it a unit whose stored diagnosis has moved reads as asked the
    # same question. None drops the channel: the premise then rests on the
    # file the prompt quotes and the crash excerpt alone, which is the honest
    # statement of what such a caller can observe.
    retro_evidence_texts: Callable[..., Mapping[FnLocator, str]] | None = None
    # (project, cluster) -> the route ledger's rows
    # (`adapters/route_ledger_store.py`). The dispatch side reads them for
    # one question the schedule already asks of the same rows: has the hop
    # pair that covers a leftover been bought and returned nothing under
    # this oracle (`domain/fill_schedule.unpaid_pair_buys`)? Without it the
    # measured-owners withholding a pair earns cannot lapse, and a pair
    # that keeps reverting holds the frontier at its own members for as
    # long as the attribution keeps deriving it. None drops the lapse: every
    # paired leftover then withholds, which is the pre-lapse behaviour.
    route_ledger_rows: Callable[..., Sequence[RouteLedgerRow]] | None = None
    # The original crate's verdict vector for this project, so a FAIL
    # sweep whose remaining failures are all ones the original also fails
    # or skips can close the cluster. Task3 already judges this way; t2a
    # used to require rc==0, so a trybuild leftover the original fails on
    # this rustc kept the cluster open (run 216855-03b2a21b, 2026-09-02,
    # nightly at 120/123). None disables — tests that construct
    # PhaseCTddDeps and a REFERENCE_FLOOR=0 launch keep the absolute
    # criterion.
    reference_measurement: Callable[..., Any] | None = None
    # `(project_dir, cluster, module) -> FnCoverageRecord | None`: the
    # function-execution record test augmentation persisted for this cluster.
    # Read once, when the cluster closes green or on the floor, to classify the
    # stubs it closes with (`close_stub_account`). None: the close says nothing
    # about them, as before.
    augmentation_fn_record: Callable[..., Any] | None = None


class SweepReading(NamedTuple):
    """One reconciled cargo-test sweep. Carried as a value object rather than a
    bare tuple because `passing` and `failed` are both name sets over the same
    sweep: positional unpacking of two same-typed neighbours is how a caller
    silently compares the wrong one."""
    fail_count: int
    failed: set[str]
    oracle: SweepOracle
    passing: frozenset[str]


def _log_attribution_gap(
    deps: PhaseCTddDeps, tag: str, iter_n: int, fail_count: int,
    cur_failed: set[str], frontier_size: int, kind: str,
) -> None:
    """Diagnose a give-up iter where tests still fail but no fn was targeted.

    Disambiguates the ways attribution can silently starve the loop:
      parsed==0                → parse_failed_tests couldn't read the names (parser gap)
      parsed>0 & candidates==0 → names parsed fine but NO attribution path
                                 (behavioral/reverse-map) mapped them to a fn
      parsed>0 & candidates>0  → candidates existed but all exhaustion-filtered
                                 (the exhaustion re-entry filter)
    Prints the failing names so we can see which fns should have been re-filled.
    """
    if fail_count <= 0:
        return
    names = sorted(cur_failed)
    sample = ", ".join(names[:12]) + (" …" if len(names) > 12 else "")
    deps.log(
        f"  {tag} [phase-c/tdd] iter {iter_n}: ATTRIBUTION GAP — "
        f"{fail_count} test(s) fail, parsed={len(names)} name(s), "
        f"candidates(pre-exhaust-filter)={frontier_size} (last kind={kind}). "
        f"failing: {sample or '(none parsed)'}")


def _floor_snapshot(
    *, cluster: str, best_pass: int, stall: int, tree_sha: str,
    oracle_size: int, code_id: str,
    attempted: set[FnLocator], landed: set[FnLocator],
    declined: set[FnLocator], served: Iterable[FnLocator],
    served_verified: Iterable[FnLocator] = (),
    futility: dict[str, frozenset[FnLocator]],
    products: Mapping[str, FiredProduct] | None = None,
    producers: Mapping[str, frozenset[FnLocator]] | None = None,
) -> dict[str, Any]:
    """The floor as the loop persists it — ONE spelling for both saves.

    The floor is written twice per iteration: at the sweep boundary and again
    the moment the batch is taken. The cursor (`attempted`) is a DISPATCH
    fact, and a cursor persisted only at the next boundary dies with any stop
    that lands inside the fill phase, which is where the hours are. Run
    `349271-d876e4a9` (2026-08-25): the floor written at iter 2's start read
    `attempted: []`, iter 2 then dispatched 12 of 28 escalated units, the
    process was killed 29 minutes later, and the relaunch's deterministic
    `order_frontier` would have re-sent the same 12 first.

    `futility` is `prev_test_units` as it stands at the call: at the boundary
    save that is the PREVIOUS sweep's attribution (the save runs before this
    sweep's), at the dispatch save it is this sweep's. Either is a valid
    first observation for the resume's first sweep to be the second.

    `products` is the escalation's producer set, keyed by test. A fire that
    happens AFTER the boundary save (the GPU-guard stop on the same sweep)
    would otherwise die in memory; the after-fire save is what carries it.
    """
    _prods = products or {}
    _pset = producers or {}
    _encoded: list[tuple[str, tuple[str, ...],
                         tuple[tuple[str, tuple[str, ...]], ...],
                         tuple[str, ...]]] = []
    for _t, (_earned, _cached) in _prods.items():
        _encoded.append((
            _t,
            tuple(sorted(f"{_u.file}::{_u.fn_name}" for _u in _earned)),
            tuple(sorted(
                (f"{_k.file}::{_k.fn_name}", tuple(sorted(_ts)))
                for _k, _ts in _cached.items())),
            tuple(sorted(f"{_u.file}::{_u.fn_name}"
                         for _u in _pset.get(_t, ())))))
    return TddFloor(
        cluster=cluster, best_pass=best_pass, stall=stall,
        tree_sha=tree_sha, oracle_size=oracle_size, code_id=code_id,
        attempted=tuple(sorted(
            f"{_l.file}::{_l.fn_name}" for _l in attempted)),
        landed=tuple(sorted(
            f"{_l.file}::{_l.fn_name}" for _l in landed)),
        declined=tuple(sorted(
            f"{_l.file}::{_l.fn_name}" for _l in declined)),
        served=tuple(sorted(
            f"{_l.file}::{_l.fn_name}" for _l in served)),
        served_verified=tuple(sorted(
            f"{_l.file}::{_l.fn_name}" for _l in served_verified)),
        futile_units=tuple(sorted(
            (_t, tuple(sorted(f"{_l.file}::{_l.fn_name}" for _l in _us)))
            for _t, _us in futility.items())),
        escalation_products=tuple(sorted(_encoded)),
    ).to_dict()


def _keys_to_locators(keys: Iterable[str]) -> frozenset[FnLocator]:
    """`file::fn` strings from a floor row → locators; drop malformed."""
    out: set[FnLocator] = set()
    for k in keys:
        f, _, fn = k.partition("::")
        if fn:
            out.add(FnLocator(f, fn))
    return frozenset(out)


def _clear_coverage_on_return(
    fn: Callable[..., ClusterFillResult],
) -> Callable[..., ClusterFillResult]:
    """The coverage slice is process-global; a stop after bind must not
    leak it into task2c / task3 or the next walk."""
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> ClusterFillResult:
        try:
            return fn(*args, **kwargs)
        finally:
            bind_coverage_slice(None)
    return wrapped


def _read_max_sessions_per_fill() -> int:
    """`SLICE_SESSION_MAX_PER_FILL`, 0 (unbounded) on unset or garbage.

    Read once at cluster entry like the stops. Not a stop: the sessions
    beyond the cap are DEFERRED by the fill schedule and are the next
    fill's never-served candidates, so a cap prevents no reading — it
    moves the sweep that reads the fill earlier. Sized from run
    `3014175-d98ed09e`'s iter-3 fill (2026-09-08): eight packed sessions
    of 14.3–50.1 min (258 min, 07:32–11:50 UTC) before the file wave (34
    min) and the per-fn route opened."""
    raw = os.getenv("SLICE_SESSION_MAX_PER_FILL", "0").strip() or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _read_fill_budget_minutes() -> float:
    """`TDD_FILL_BUDGET_MINUTES`, 0 on unset or garbage."""
    raw = os.getenv("TDD_FILL_BUDGET_MINUTES", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


@_clear_coverage_on_return
def run_phase_c_tdd(
    deps: PhaseCTddDeps,
    *, project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    cluster_tests: list[tuple[str, str]],
    module: str,
    cluster: BuildCluster,
    check_cmd: str,
    cluster_env: dict[str, str],
    leaf_counts: dict[str, int] | None,
    fr_path: Path | None,
    test_cmd_builder: Callable[..., str],  # (stems, *, include_lib=False) -> cmd
    tag: str,
    # `(rel) -> healed?` over the filled crate — the heal hierarchy
    # (excision, best tree, git ancestor, skeleton) for a file the
    # boundary check finds unparseable. None: the boundary only reports.
    heal_file: Callable[[str], bool] | None = None,
) -> ClusterFillResult:
    """TDD-scoped Phase C driver.

    Outer loop: cargo test → frontier → fill frontier → repeat.

    The loop's settled behaviours (resolved 2026-05-27):
      - the frontier is ordered by ascending panic depth, then descending
        caller count;
      - an exhausted fn is re-entered only if other fills landed, or new
        relevant notes accumulated since its last attempt;
      - a cargo BUILD failure routes into a compile-error frontier rather
        than ending the iteration;
      - each cargo test invocation gets a 120s wall-clock timeout;
      - the iter-0 frontier surfaces the shallowest fn per test as a natural
        consequence of the depth ordering — nothing special-cases it.
    """
    # Frontier extraction expects the cargo project root (NOT project_root/src):
    # cargo's panic messages emit `src/foo.rs` paths relative to the project
    # root, and the syn extractor records `file` fields the same way. Passing
    # `project_root/src` would produce double-prefixed paths and zero matches.
    src_dir = target_src_dir
    # Filter to direct children of tests/ — only those are cargo --test targets.
    test_stems = sorted({
        Path(rel).stem
        for rel, _ in cluster_tests
        if "tests" in Path(rel).parts and Path(rel).parent.name == "tests"
    })
    if not test_stems:
        deps.log(f"  {tag} [phase-c/tdd] no test stems in cluster; skipping TDD.")
        return ClusterFillResult(
            remaining=deps.scan_unimplemented_for_cluster(
                target_src_dir, cluster).unit_count)
    # --lib sweep: also run the crate unit-test binary (src-side `#[cfg(test)]`
    # + the gen_tests siblings the reconstruction preserves as its oracle).
    # Without it phase-c steers against only the tests/ binaries — a subset of
    # the real oracle — and gen_tests failures surface only at task3. When on,
    # the sweep's expected denominator must include the lib sources too, or the
    # lib tests report as an unnamed over-count (see the inventory call below).
    _sweep_lib = os.getenv("PHASE_C_SWEEP_LIB", "1") not in ("", "0", "false")
    # Serial harness: with parallel test threads a crashing binary kills a
    # RANDOM subset of its tests' verdicts each run (whichever threads printed
    # first), making sweep readings and gate baselines non-comparable draws.
    # `--test-threads=1` pins the crash point, so vanished-test sets are
    # deterministic. Flows into every downstream consumer (sweeps, regression
    # gate, scoped u2 runs) via tdd_test_cmd.
    test_cmd = ensure_serial_harness(
        test_cmd_builder(test_stems, include_lib=_sweep_lib))
    # The targets that command asks for, in the label vocabulary cargo prints
    # on `Running <label>`: what `sweep_build` compares a capture's sections
    # against, so the iteration event can name a target that never ran.
    _expected_labels = expected_target_labels(test_stems, include_lib=_sweep_lib)
    test_timeout = int(os.getenv("PHASE_C_TEST_TIMEOUT", "120"))
    # max_iters is legacy — only the 10k backstop in the loop body honors it.
    # The real terminator is floor-stall on pass_count (max_stalls below).
    max_stalls = deps.effective_stalls(int(os.getenv("TDD_MAX_STALLS", "3")),
                                   deps.converge_in_cluster(cluster) or module)
    # The empty-frontier terminator. An iteration that finds nothing
    # dispatchable is not "flat" and `max_stalls` never governed it: with the
    # plateau exit disabled the loop re-ran a cached `cargo test` at ~2
    # iterations a second from iteration 9 to the 10 000 backstop (run
    # `1246060-2339497b`, 2026-08-21: 29 MB of events in 23 minutes, the log
    # rotating every 3 minutes). Nothing dispatchable means nothing can
    # change a verdict, so this stop prevents no reading; it spends the
    # exhausted set's bounded re-admission budget first (the only move left)
    # and fires after this many CONSECUTIVE undispatchable iterations once
    # that budget is spent. `0` disables it and restores the spin.
    _no_frontier_stop_after = int(os.getenv("TDD_NO_FRONTIER_STOP", "2"))
    _reset_cap = int(os.getenv("RETRO_RESET_MAX_ATTEMPTS", "2"))
    # Wall-clock bound on one TDD fill phase (file-first + impl + per-fn).
    # `stall` is written only when cargo test finishes an iteration, so a
    # mid-pass fill that runs all night prints stall=0. `0` disables.
    # Do not set below a healthy fill that must return a sweep (~2.5 h on
    # `463713-b7c666c8` after dropping same-iteration refill-only blocks).
    _fill_phase_max = fill_phase_clock.configured_minutes()
    # The fill schedule's wall-clock budget (`domain/fill_schedule.py`): the
    # minutes one fill's route runs share by weight. A DEFERRAL, not a stop —
    # a run that would start past the budget is recorded deferred and is a
    # never-served candidate of the next fill; a unit, turn or session in
    # flight always completes, so a fill overruns by at most one unit. `0` =
    # order only (no shares, nothing deferred). Read once, like the stops.
    _fill_budget = _read_fill_budget_minutes()
    _max_sessions = _read_max_sessions_per_fill()
    # Re-plan on a moved premise (`fill_scheduler._GroundGate`): a fill ends
    # at the first claim after a keep that changed which tests fail or where
    # they first fail, and the next sweep plans from the tree as it stands.
    # NOT a stop — it prevents no reading, it brings the next one forward.
    # `0` runs every fill's plan to its end (the frozen-plan negative
    # control). Read once, like the stops.
    _replan_on_premise = os.getenv("PHASE_C_REPLAN_ON_PREMISE", "1") != "0"

    def _premise_of_capture(capture: str) -> dict[str, PremiseEntry]:
        """The ground a capture shows (`test_outcome.premise_of`), crate
        sites resolved to their function on the tree as it stands now —
        a fresh locator per call, because the tree moves between calls.
        With no extractor wired a crate site is identified by its file."""
        return premise_of(outcomes_of(capture), make_fn_site_locator(
            SimpleNamespace(enumerate_fns=deps.enumerate_fns), target_src_dir)
            if deps.enumerate_fns is not None else None)

    # The previous judged sweep's ground, for the no-verdict clock: a sweep
    # whose tests fail somewhere else than they did is the loop doing
    # something to the crate even when no verdict flipped.
    _prev_premise: dict[str, PremiseEntry] | None = None

    # The GPU protector (domain/flat_score.py). The plateau budget above charges
    # only on a COMPLETED frontier traversal, which under the dispatch cap is
    # ~5 iterations — run `1064759-0f23fb68` therefore stood at stall 1 of 3
    # after 7h48m of sweeps that all read pass=79 oracle=101, and ended on an
    # operator's SIGTERM rather than on any decision of the loop's. This guard
    # is the other terminator: it stops the RUN before spending model calls on a
    # score the last `window` judged sweeps have already seen `repeats` times.
    # `require_traversal` defaults OFF: the precondition waited on a completed
    # frontier traversal, and runs `1064759-0f23fb68` (7h48m),
    # `1692702-94e6163a` (3h36m) and `1920662-40f711b2` (9h48m) each ended on
    # an operator's stop with the repeat clause satisfied and the traversal
    # index parked. The rule is now a repeat in the window, full stop.
    #
    # A FOLD, NOT A STATEFUL GUARD. `FlatScoreGuard.observed`/`excused` had to
    # be called on exactly the right path in a 3,700-line module, and the
    # recorded cost of that shape is three runs that ended on an operator's
    # SIGTERM with the repeat clause satisfied. The window is now DERIVED from
    # the sweep facts this loop records, so there is no path on which a
    # reading can be missed: `flat_score(_flat_facts, …)` answers from the
    # stream. The fact vocabulary is `domain/loop_facts.py`; the completeness
    # of the recorded stream for THIS gate was measured before the conversion
    # (`scripts/fold_completeness.py`, 2026-08-21: pass_count, oracle_size and
    # the pass closures are rebuildable for every recorded run).
    _flat_window = int(os.getenv("TDD_FLAT_SCORE_WINDOW", "5"))
    _flat_limit = int(os.getenv("TDD_FLAT_SCORE_REPEATS", "2"))
    _flat_require_traversal = os.getenv(
        "TDD_FLAT_SCORE_REQUIRE_TRAVERSAL", "0") == "1"
    _flat_enabled = _flat_limit >= 2 and _flat_window >= _flat_limit
    _flat_facts: list[Fact] = []

    def _flat_decision() -> FlatScoreDecision | None:
        """The guard's answer for the sweep just recorded, or None before the
        first judged one. Recomputed rather than carried: a decision that is a
        function of the facts cannot go stale on a path that forgot it."""
        if not _flat_enabled:
            return None
        return flat_score(_flat_facts, window=_flat_window, limit=_flat_limit,
                          require_traversal=_flat_require_traversal)
    # The protector's SECOND clock, and the reason there is one: the guard
    # above and the plateau budget are both denominated in completed traversals,
    # and on run `1692702-94e6163a` (2026-08-16) that unit stopped ticking —
    # frontier 35 -> 57 -> 63 against a cursor at 12 -> 21 -> 36, no pass
    # closed, `_traversals` stuck at 0, so the guard's `first < traversal` read
    # `0 < 0` and `stall` read 0 of 3 while 79/101 came back three times. This
    # clock counts judged sweeps that landed bodies and moved no test's
    # verdict, which is independent of the frontier, the cap and the pass.
    _unmoved = UnmovedVerdicts(
        limit=int(os.getenv("TDD_NO_VERDICT_SWEEPS", "3")))
    # The previous sweep's per-binary vector and the fill count at that point,
    # for the "N bodies landed and no test changed verdict" line below.
    _prev_bins: list[tuple[str, int, int]] = []
    _filled_at_prev_sweep = 0
    # One durable-tree restore per process (see the guard's trip site).
    _restore_spent = False
    # Armed by the restore block: the pre-restore sweep's binary-qualified
    # verdict pairs and the restored body count, consumed by the NEXT sweep. A
    # restore that replaced >0 files and is followed by identical pairs was
    # never measured — the binaries did not rebuild — and that sweep must not
    # be allowed to judge it. Pairs rather than the per-binary count vector:
    # counts cannot see a swapped verdict, pairs are the repo's own definition
    # of "the same reading" (`tools/pass_delta.py`).
    _restore_probe: (
        tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]], int]
        | None) = None

    deps.log(f"  {tag} [phase-c/tdd] driver=tdd, "
         f"max_stalls={max_stalls}"
         + (" (plateau exit DISABLED)" if plateau_exit_disabled(max_stalls)
            else "")
         + f", timeout={test_timeout}s, "
         f"stems={len(test_stems)}, "
         + (f"gpu-guard={_flat_limit} repeat(s) of a reading in "
            f"{_flat_window}" if _flat_enabled else "gpu-guard=off")
         + (f", no-frontier-stop after {_no_frontier_stop_after} "
            "undispatchable iteration(s)" if _no_frontier_stop_after > 0
            else ", no-frontier-stop=off")
         + (f", fill-phase-stop={_fill_phase_max} min"
            if _fill_phase_max > 0 else ", fill-phase-stop=off")
         + (f", sessions-per-fill={_max_sessions}"
            if _max_sessions > 0 else ", sessions-per-fill=unbounded")
         + f", language-driver={language_driver().name}"
         + f", followup-think={'on' if followup_think_env_on() else 'off'}"
         + f", replan-on-premise={'on' if _replan_on_premise else 'off'}")

    if deps.migrate_retro_evidence is not None:
        _mig_cleared, _mig_retired = deps.migrate_retro_evidence(project_dir)
        if _mig_cleared or _mig_retired:
            deps.log(f"  {tag} [phase-c/tdd] retro evidence migration: "
                     f"cleared {_mig_cleared} exhaustion record(s), retired "
                     f"{_mig_retired} guidance text(s) written under the "
                     "pre-binary-scoped gate (confounded verdicts).")

    if deps.migrate_retro_gate is not None:
        _gate_cleared, _gate_budgets = deps.migrate_retro_gate(project_dir)
        if _gate_cleared or _gate_budgets:
            deps.log(f"  {tag} [phase-c/tdd] retro gate migration: cleared "
                     f"{_gate_cleared} exhaustion verdict(s) and "
                     f"{_gate_budgets} reset budget(s) earned under the "
                     "pre-comparative acceptance gate (a strict improvement "
                     "was reverted then) — one fresh verdict each.")

    permanently_exhausted: set[FnLocator] = set()
    # The empty-frontier terminator's two counters: consecutive iterations
    # that found nothing dispatchable, and how many times the "no other move"
    # re-admission has been spent (bounded by the reset cap, like the store's
    # own per-record budget).
    _no_frontier_streak = 0
    _no_move_readmissions = 0
    # Futility escalation state: per failing test, the frontier
    # unit-set of the previous iteration; a test whose set is UNCHANGED while
    # it kept failing has exhausted its attributed fns — escalate.
    prev_test_units: dict[str, frozenset[FnLocator]] = {}
    # The fire's premise, tracked instead of assumed: the iteration each unit
    # last RETURNED from a fill (`_served_at`), and the iteration each test's
    # current unit-set was first observed (`_set_since`). Dispatch is not
    # service: a kill mid-fill persists the cursor so a relaunch does not
    # re-send the same 12, but those units were never rewritten. The
    # escalation fires only when every attributed unit was served at or after
    # the set's first observation — under the dispatch cap an unchanged set
    # no longer means "served"; see `escalation_fire_due`. A persisted
    # futility seed counts as served at 0.
    _served_at: dict[FnLocator, int] = {}
    # The RANKING's view of service: only stamps earned under the
    # route-scoped rule (`_attempted` = units a route actually opened).
    # `_served_at` is the fire's premise and is deliberately inclusive — it
    # carries every floor stamp ever made, including the pre-2026-08-31
    # batch-wide ones no record can now distinguish (run `9933-efe68f37`:
    # 12 stamped, `wanted` was `{context, msg}`; `never_served=[]` on 24 of
    # 25 rankings across the five runs after the route-scoped fix). A stamp
    # the ranking cannot verify is not evidence the unit was tried, so the
    # never-served bucket reads this set, seeded only from the floor's
    # `served_verified` and re-earned as routes open units.
    _served_verified: set[FnLocator] = set()
    _set_since: dict[str, int] = {}
    # Escalated test -> (the futile unit-set that EARNED its fire, the
    # additions the fire computed). The escalation's product lives exactly as
    # long as this evidence: while a test's attribution still equals the
    # recorded set, the cached additions are re-merged into the frontier
    # every sweep; the moment the set changes, the product dies with the
    # evidence (and the test may re-earn a fire from the new set under the
    # ordinary two-sweep rule). The additions are cached rather than
    # recomputed because `escalate_to_producers` logs its whole verdict —
    # recomputing would print ~2 lines per fired test per sweep — and a
    # resume recomputes fresh at its own fire anyway. Fire-once
    # (`escalated_tests: set`, merged only on the firing sweep) shrank the
    # pass denominator on every later sweep of the same process: run
    # `2953362-63d4b47e` (2026-08-18) escalated 43 units at iter 1 and read
    # a 16-unit frontier at iter 2, so `take_batch` counted the 19 vanished
    # producers as "left the frontier", was one dispatch from closing the
    # pass over a third of it, and would have wiped the cursor and credited
    # a traversal that never touched the escalated slice. Only the
    # flat-score guard's stop scheduling pre-empted it.
    escalation_products: dict[str, FiredProduct] = {}
    #: test -> the PRODUCER subset of its fire (see the fire site below). Lives
    #: and dies with `escalation_products`, so a dropped product cannot leave a
    #: stale ESCALATED grade behind.
    escalation_producers: dict[str, frozenset[FnLocator]] = {}
    last_notes_count: dict[FnLocator, int] = {}
    last_total_filled = 0
    # Self-contained snapshot for the exhaustion-reset gate below — deliberately
    # NOT reusing `total_filled == last_total_filled` (that pair is compared
    # earlier in THIS SAME iteration, before `total_filled`'s one update at the
    # bottom of the loop, so at this call site both already reflect the END of
    # the PREVIOUS iteration and would always compare equal; a fresh snapshot
    # updated only when we actually reset avoids relying on that ordering).
    _filled_at_last_reset = 0
    # The pass count the last re-admission was bought with. Re-admission costs
    # a full extra frontier of LLM calls, so it is spent on EVIDENCE, not on
    # activity: see the gate below.
    _pass_at_last_reset = -1
    # behavioral re-fill targets (frontier fns already filled
    # with a compiling-but-wrong body) are not stubs, so the stub-based
    # exhaustion path never bounds them → an unfixable wrong body would burn one
    # LLM call every iter until the stall budget alone exits. Count per-fn re-fill
    # attempts and mark a fn exhausted once it caps (the exhaustion re-entry
    # filter still re-admits it on new notes / cluster-wide progress — same
    # escape hatch as stubs).
    refill_attempts: dict[FnLocator, int] = {}
    _refill_max = int(os.getenv("TDD_REFILL_MAX_ATTEMPTS", "2"))
    # Destruction epoch: bumped at every deliberate destruction this loop
    # performs (retrigger re-stub, charged ratchet restore), so any baseline
    # taken before it answers INCOMPARABLE instead of NOT_IMPROVED. The
    # re-admission watermark carries the epoch it was set in.
    _destruction_epoch = 0
    _reset_epoch = 0
    # THE PROVEN-BODY LEDGER's process state. `_ledger_rows` are entries this
    # process wrote at a landing and has not yet had a sweep judge; the sweep
    # after them classifies each one and appends the verdict. `_ledger_before`
    # is the reading those bodies landed ON — the "prior" half of a comparison
    # that must refuse to answer across a destruction epoch.
    _ledger_rows: list[LedgerEntry] = []
    _ledger_before: SweepView | None = None
    _ledger_cache: list[LedgerEntry] | None = None

    def _ledger_entries() -> list[LedgerEntry]:
        """Every row this project/cluster has, read once per process."""
        nonlocal _ledger_cache
        if _ledger_cache is None:
            _ledger_cache = (list(deps.ledger_load(project_dir, cluster.name))
                             if deps.ledger_load is not None else [])
        return _ledger_cache

    _oracle_fp_cache: dict[int, str] = {}

    def _oracle_fp(iter_no: int) -> str:
        """The fingerprint of the oracle scoring this tree, computed once per
        iteration and stamped on every row that iteration writes.

        Once per iteration rather than once per process: the gen_tests are
        written by task0c and do not move inside a run, but the set is 34
        files plus `tests/` and the cost of re-reading them at an iteration
        boundary is nothing next to a sweep, whereas a fingerprint cached
        for a whole process would silently outlive a test file any future
        mechanism rewrote mid-run. Once per row would read the set for each
        of a 12-unit batch. `""` when no oracle is wired, which every gate
        reads as "unrecorded" and refuses."""
        if deps.oracle_fp is None:
            return ""
        hit = _oracle_fp_cache.get(iter_no)
        if hit is None:
            hit = str(deps.oracle_fp(target_src_dir, project_dir,
                                     cluster.name) or "")
            _oracle_fp_cache.clear()
            _oracle_fp_cache[iter_no] = hit
        return hit

    def _skeleton_root() -> Path:
        """The cluster's AUTHORED layout crate — what the bodies answer to.

        The crate root rather than its `src/`, because the ledger keys files
        the way the extractor does (`src/error.rs`). Not the filled tree
        either: a fingerprint taken over that would move every time a body
        landed, which is the one event that must NOT refuse a re-seed."""
        return project_dir / ".skeleton" / cluster.name

    _layout_ctx_cache: dict[int, tuple[dict[str, tuple[str, str]],
                                       dict[str, str], dict[str, str]]] = {}

    def _layout_ctx(epoch: int) -> tuple[dict[str, tuple[str, str]],
                                         dict[str, str], dict[str, str]]:
        """`(items, file_of_item, fingerprint_by_file)` for the layout as it
        stands, cached per destruction epoch.

        Keyed on the epoch because that counter is bumped by exactly the event
        that rewrites the skeleton — a retrigger's re-author — so a cache that
        outlived one would stamp new bodies with the old layout's fingerprint
        and re-seed them, later, against a layout they never compiled under."""
        hit = _layout_ctx_cache.get(epoch)
        if hit is not None:
            return hit
        items = (deps.layout_items(project_dir)
                 if deps.layout_items is not None else {})
        texts: dict[str, str] = {}
        skel = _skeleton_root()
        with suppress(OSError):
            for _p in sorted((skel / "src").rglob("*.rs")):
                with suppress(OSError):
                    texts[str(_p.relative_to(skel))] = _p.read_text(
                        encoding="utf-8", errors="replace")
        fps = {rel: authored_layout_fp(txt) for rel, txt in texts.items()}
        file_of: dict[str, str] = {}
        for name, (_kind, definition) in items.items():
            for rel, txt in texts.items():
                if find_definition_span(txt, definition) is not None:
                    file_of[name] = rel
                    break
        _layout_ctx_cache.clear()
        _layout_ctx_cache[epoch] = (items, file_of, fps)
        return _layout_ctx_cache[epoch]

    def _ledger_adopt() -> int:
        """Queue every backfilled row whose body IS the body on the tree, so
        this process's first sweep judges it.

        WITHOUT THIS THE BACKFILL IS INERT, and silently so. Rows indexed from
        an archive or from the live tree enter as `landed` — correctly,
        because promotion needs a verdict vector measured on THIS code — and
        the promotion pass only judges rows this process WROTE. So the 465
        backfilled rows would sit unjudged forever, never reaching `proven` or
        `unmeasured`, and therefore never re-seedable: the ledger would pay
        only for bodies the loop happened to re-land itself, which is the case
        it is least needed for.

        Adoption is one-shot per process and bounded by the tree's own unit
        count, and it claims nothing on its own — a queued row is judged by the
        same rule as a fresh landing, against the same verdict vector. The
        body-identity check is what keeps that honest: a stored body that is
        not the body on the tree cannot be what the sweep is measuring."""
        if (deps.ledger_append is None or deps.tree_writer is None
                or not _ledger_entries()):
            return 0
        for _key, _entry in latest_by_unit(_ledger_entries()).items():
            if _entry.state is not BodyState.LANDED:
                continue
            _unit = FnLocator(_key.file, _key.fn_name)
            if body_sha(deps.tree_writer.bodies(
                    _unit, target_src_dir)) == _entry.body_sha:
                _ledger_rows.append(_entry)
        return len(_ledger_rows)

    def _reseed_site(site: str, iter_no: int) -> ReseedOutcome | None:
        """Put back every body the ledger can vouch for, at one destruction
        seam. Returns None when the ledger is not wired.

        `wanted` is the CURRENT stub set rather than a list handed down by the
        destroyer: a retrigger reports how many bodies it reverted, not which,
        and the stub set answers the question the re-seed actually asks. Units
        whose file was re-authored are refused by the gate, and units the
        ledger never saw are simply absent — so this is correct at every site
        without the site having to describe its own damage."""
        if deps.ledger_append is None or deps.tree_writer is None:
            return None
        # UNITS, collapsed: the ledger key is `(file, fn_name)` with one
        # body per occurrence, so a unit is one key however many sites
        # carry its name.
        stubs = deps.scan_unimplemented_functions(target_src_dir).unit_list()
        if not stubs:
            return None
        _checker = deps.check_tree_builds
        _check = (None if _checker is None
                  else (lambda: _checker(project_dir, cluster)))
        out = reseed(
            site=site, entries=_ledger_entries(), wanted=stubs,
            project=project_dir.name, cluster=cluster.name,
            src_dir=target_src_dir, skeleton_root=_skeleton_root(),
            writer=deps.tree_writer, oracle_fp=_oracle_fp(iter_no),
            check_builds=_check, validate_parse=deps.parse_ok,
            log=deps.log)
        if not (out.seeded or out.rejected or out.skipped):
            return out
        deps.log(f"  {tag} [phase-c/ledger] iter {iter_no}: {site} — "
                 + out.render() + ".")
        deps.emit("phase_c.ledger.reseed",
                  data={"cluster": cluster.name, "iter": iter_no, **out.data})
        return out
    def _heal_unparseable(when: str, iter_no: int,
                          files: tuple[str, ...] | None = None) -> list[str]:
        """The boundary invariant, through the transaction module's one
        reading (`guard_tree_boundary`): the tree a sweep scores must
        parse. `files=None` scans the tree before the sweep; the post-sweep
        caller passes the files the compiler's uncoded diagnostics named.
        Returns the files that were healed."""
        return list(guard_tree_boundary(
            target_src_dir, validate_parse=deps.parse_ok, heal_file=heal_file,
            log=deps.log, emit=deps.emit, tag=f"{tag} [phase-c/tdd]",
            when=when, iter_no=iter_no, cluster=cluster.name,
            files=files).healed)

    # Digests this process already healed on: a file that comes back
    # unparseable with the SAME diagnostic after a heal is not healed
    # again — the red-build streak owns that state.
    _healed_digests: set[str] = set()
    # The fns each fill pass actually dispatched, most recent last — the
    # stall exit may convict only units that were dispatched AND judged
    # inside the stalled window, not everything left standing.
    _dispatched_window: list[frozenset[FnLocator]] = []
    # The resumed floor cursor's units, for the same stall-exit accounting.
    # Under the one-batch-per-relaunch crawl a traversal spans processes, so
    # the units the RECORD says this traversal already dispatched (all judged
    # flat — an improvement clears cursor and streak together) live here, not
    # in the ring above, whose trim to the last `max_stalls` fill passes
    # would drop them. Cleared wherever the cursor is.
    _resumed_cursor_window: frozenset[FnLocator] = frozenset()
    # Consecutive red-build sweeps with an IDENTICAL compile digest. Three in
    # a row means two full fill passes changed nothing about why the cluster
    # cannot compile — spending the stall budget on it buys 0/oracle sweeps
    # (nightly, run `2219473`, 2026-08-10: three iterations at 0/113 under
    # the same two E0605 casts at src/backtrace.rs:21).
    _red_digest = ""
    _red_streak = 0

    total_filled = 0
    total_attempts = 0
    total_bytes = 0
    errors: list[str] = []
    _prev_payoff = None
    _prev_fill_classes = FillClassHistogram()
    _saw_fill = False

    # Improvement-driven loop. Track passing-test count across iters;
    # exit only after `max_stalls` consecutive non-improving iters. The 10k
    # backstop in the loop body is a runaway-safety net only.
    # LoopState is the source of truth for the pass_count floor / stall streak
    # AND the carrier of cumulative meta-awareness (the pass trajectory + the
    # causal "this fill broke these tests" notes) fed back into each fill.
    # `stall`/`best_pass` below are kept as synced mirrors so the existing
    # log/emit lines read unchanged.
    state = LoopState()
    stall = 0
    best_pass = UNSET_PASS
    best_fail = None
    # Dispatch batching. The sweep is the only measurement and it costs ~1s of
    # cargo; what separated two of them was a batch of the WHOLE frontier, which
    # put run `669347-cf33eae3` at one `phase_c.tdd.iter` in 4h17m. The cap
    # slices the frontier so the loop looks every few units at the same model
    # cost — see `domain/dispatch_batch.py` for the measurement and for why the
    # cursor is a set rather than an index. 0 disables batching (pre-cap
    # behaviour); a frontier already under the cap never notices it.
    _dispatch_cap = int(os.getenv("PHASE_C_TDD_MAX_DISPATCH",
                                  str(DEFAULT_MAX_DISPATCH)))
    # Units this PASS has already handed to the engine. Cleared when a pass
    # closes and whenever the score improves — a better tree reopens the whole
    # frontier rather than resuming a traversal taken against the old one.
    _pass_attempted: set[FnLocator] = set()
    # The subset of the cursor on which a SEMANTIC change landed — the unit's
    # body tuple differed after the batch that dispatched it (measured per
    # batch from extractor records, `domain/landed_change.py`). The cursor
    # marks ATTEMPTED at dispatch; a completed pass read off the cursor alone
    # claims every unit was TRIED, and the pass that closed the 2026-08-19
    # crawl dispatched byte-identical re-authors, peeled fills and
    # immutable-by-rule units for roughly half its denominator. Cleared with
    # the cursor, persisted with it (same identity rule), reported when a
    # pass closes so the post-pass flat verdict names its effective coverage.
    _pass_landed: set[FnLocator] = set()
    # The pass's DECLINED units: dispatched, INFERRED, and told to return the
    # body unchanged. They land nothing by instruction, so the cursor's carry
    # rule counts them as tried rather than as failed tries (`domain/
    # tdd_floor.py::cursor_carry`). Same lifetime as `_pass_landed` — cleared
    # with the cursor, persisted with it, reset when the pass closes.
    _pass_declined: set[FnLocator] = set()
    # Units dispatched this pass under the ESCALATED grade. An escalated unit
    # that comes back unchanged is NOT a declined re-check: its instruction was
    # "rewrite", so "unchanged" is a failed try and the flat-sweep refutation
    # must count it as one. Run 55484-84c991f1 (2026-08-22) filed
    # `construct`, `construct_from_std`, `construct_from_adhoc` and
    # `construct_from_display` as "INFERRED, unchanged (not a failed try)" at
    # iter 3 while the prompt that dispatched them graded every one ESCALATED
    # — the audit split by conviction alone and could not see the grade.
    _pass_escalated: set[FnLocator] = set()
    # Units a route actually OPENED this pass (`_attempted` per batch). The
    # cursor marks a unit attempted at dispatch, so a unit every run of
    # which the schedule deferred still closes the traversal; without this
    # set the pass audit filed it under "dispatched with NO landed change"
    # beside real failed tries. Run `11358-e5de094e` iter 3 (2026-09-15):
    # "27 dispatched, 1 landed; 26 dispatched with NO landed change" while
    # the landed-change lines of iters 1–3 named most of those 26 "no route
    # attempted" (their runs were deferred `coverage_split` every fill).
    # Not persisted: a resumed cursor's units are never claimed unopened.
    _pass_opened: set[FnLocator] = set()
    # Units this pass already put back on the cursor because no route opened
    # them (`reoffer_unopened`): the once-per-pass bound. Same lifetime as
    # `_pass_opened`.
    _pass_reoffered: set[FnLocator] = set()
    # The commit-time closure of the current batch, kept so a fill that
    # opened none of some units can take the closure back.
    _closed_fact: PassClosed | None = None
    _cohort_before_close: tuple[FnLocator, ...] | None = None
    # The frontier the current pass OPENED over. A traversal is over this set,
    # not over the frontier as it stands now: escalation and crash evidence add
    # units mid-pass, and a closure condition that waits for them cannot be met
    # while they arrive faster than the cap retires them (`domain/
    # dispatch_batch.py` carries the run that measured it). None between passes
    # — the next iteration opens a cohort on whatever the frontier is then.
    _pass_cohort: tuple[FnLocator, ...] | None = None
    # Did the PREVIOUS iteration's batch close a pass? The sweep at the top of
    # this iteration measures that batch's fills, and only a completed pass may
    # charge the stall budget. True at entry so the first sweep behaves exactly
    # as it did before batching existed.
    _prev_pass_completed = True
    # How many COMPLETE passes over the frontier this cluster has finished. The
    # stall budget above needs only "did the last batch close one"; the GPU
    # guard needs the running count, because its question is whether two
    # identical readings are separated by a traversal or are two slices of the
    # same one (`domain/flat_score.py`). Never reset — the window it stamps is
    # cleared wholesale wherever the lineage changes, so a monotone counter
    # cannot make two unrelated readings look like the same traversal.
    _traversals = 0
    # The high-water's account of WHICH TREE it describes. A retrigger's
    # deliberate re-stub makes `best_pass` a fact about a tree the pipeline
    # deleted, and four consumers read it — see `domain/pass_highwater.py` for
    # what each one does with the stale figure and why the void is recorded here
    # rather than as a fourth per-gate exemption.
    _highwater = PassHighWater()
    # Stage A — the floor a previous process left on disk. Read here, ADOPTED
    # after the first sweep (its guard key needs that sweep's tree_sha and
    # oracle_size). See `domain/tdd_floor.py` for why all three key fields are
    # required and what each one protects.
    _code_id = code_identity(Path.cwd())
    _saved_floor = (TddFloor.from_dict(deps.load_tdd_floor(project_dir, cluster.name))
                    if deps.load_tdd_floor is not None else None)
    _floor_resolved = False
    _code_moved = False
    _floor_persistable = False
    _unowned: tuple[str, ...] = ()
    # TDD-iter batch ratchet: snapshot the src tree at each pass_count
    # high-water; restore it when a later iter's fills net-regress the count.
    _batch_ratchet = os.getenv("PHASE_C_TDD_BATCH_RATCHET", "1") not in ("", "0", "false")
    _src_root = target_src_dir / "src"
    _bratchet_best = UNSET_PASS
    _bratchet_snap: dict[str, str] | None = None
    _bratchet_sha: str | None = None   # tree the snapshot was taken from
    # Tests passing in COMPLETED binaries at the snapshot. The ratchet scores a
    # candidate tree against these, so a crash costs exactly the passes it
    # took and nothing more.
    _bratchet_passing: frozenset[str] = frozenset()
    # The per-binary reading of the snapshot sweep. The closure exemption
    # below forgives a breach only on stub-panic evidence from the binaries
    # that were complete WITH a pass here and crashed since — the same
    # `newly_crashed` set the fill gates judge their crash-only keep over.
    _bratchet_reading: TestStateReading | None = None
    # Persist each new pass high-water to heal's durable restore tiers
    # (best-tree snapshot + workspace-git commit). Tracked independently of
    # the in-memory batch ratchet above so the tiers stay populated even when
    # PHASE_C_TDD_BATCH_RATCHET is off — without this, a parse-corruption heal
    # falls straight through to the skeleton nuke (loses every fill in the file).
    #
    # SEEDED FROM DISK, unlike the in-memory ratchet above, because what these
    # two variables describe also lives on disk and outlives this process.
    # `snapshot_due` reads a `None` sha as "no snapshot yet" and
    # `save_cluster_best_tree` rmtree's the slot before copying, so starting
    # both at their sentinels made the FIRST sweep of every process replace the
    # stored tree however much worse it was. Live on `anyhow`, 2026-08-07: run
    # `241132-f9fae0a6` left `.best_tree/stable` at 81 passing; run
    # `259637-36db9aad` resumed that tree, its crash retrigger deliberately
    # re-stubbed 56 bodies, and the 21-passing sweep that followed — a sweep the
    # batch ratchet WITHHELD from charging, precisely because a deliberate
    # re-stub is not a regression — became the stored tree, then 23, then 26.
    # `_tdd_floor_stable.json` read `best_pass: 81` throughout, so the two
    # durable records describing one cluster disagreed and the one heal reads
    # held the worse tree. `domain/tdd_floor.py` names this gap itself:
    # "Restoring the peak across a death … needs a durable ratchet snapshot,
    # which is a separate piece of work."
    #
    # Seeded on the pass count ALONE — not on `code_id` the way the loop floor
    # is. The floor gates termination, so a code change must not let a stale
    # streak end a cluster the change was meant to unstick; this slot is source
    # text a heal copies back, and which pipeline build measured it does not
    # change how many tests it passes. An oracle change is the one thing that
    # would make the counts incomparable, and the only path to one is task0c
    # re-augmentation under `--clean`, which rmtree's the workspace and the
    # slot with it.
    # Units reverted for a crash-only regression blamed wholly outside them,
    # unit key -> blamed-file fingerprint. Loop-lifetime so the block holds
    # across iterations; the fill units mutate it and clear each entry when
    # the blamed file's content moves. See PerFnPassCtx.crash_blocked.
    crash_blocked_units: dict[str, str] = {}
    _mark = (deps.load_best_tree_mark(project_dir, cluster.name)
             if deps.load_best_tree_mark is not None else None)
    _checkpoint_best = UNSET_PASS if _mark is None else _mark.pass_count
    _checkpoint_sha: str | None = None if _mark is None else _mark.tree_sha
    if _mark is not None:
        deps.log(f"  {tag} [phase-c/tdd] durable best tree already on disk: "
                 f"{_mark.render()} — a sweep replaces it only by beating it.")
    recently_filled: list[FnLocator] = []
    recently_filled_max = int(os.getenv("TDD_RECENTLY_FILLED_MAX", "50"))
    # Hook 3 — causal-regression tracking. `prev_failed` = the failing-test set
    # from the previous iter; `last_newly_filled` = the fns we materially
    # changed last iter. A test that flips pass→fail right after a fill is a
    # regression attributable to that fill.
    prev_failed: set[str] = set()
    # The LAST sweep's verdict sets, carried out on the result. The merge needs
    # them and cannot recompute them — a checkpointed cluster is skipped, so by
    # then only its tree survives, not the evidence for it.
    last_failed: frozenset[str] = frozenset()
    last_ran: frozenset[str] = frozenset()
    last_newly_filled: list[FnLocator] = []
    _regression_notes = os.getenv("TDD_REGRESSION_NOTES", "1") == "1"
    # Build the test reverse map once per cluster — cheap (one extractor
    # invocation, one regex scan of each test file). Used as the LAST
    # attribution fallback when panics/compile errors/classifier all miss.
    try:
        test_reverse_map = deps.build_test_reverse_map(tests_dir_files, src_dir)
    except Exception as _e:
        deps.log(f"  {tag} [phase-c/tdd] reverse-map build failed: {_e}")
        test_reverse_map = {}

    # The statically-expected oracle for this cluster. Vanished tests (expected,
    # but no verdict in the output — their binary crashed or stopped compiling)
    # are counted as FAILING, so the loop can neither call a shrunken suite
    # "done" nor snapshot/checkpoint a tree that silently lost part of its
    # oracle. Scoped to the very stems this loop's `cargo test --test stem…`
    # runs — the sweep's denominator, not the whole-crate inventory walk (which
    # also counts src-side/gen_tests tests this command never executes).
    #
    # Walked over `target_src_dir` — the tree cargo actually builds — and NOT
    # over the workspace root, which cannot answer the question. The workspace
    # root's `src/` holds a one-line `lib.rs` stub and 19 orphaned `gen_tests`
    # siblings; every parent module that carries a sibling's `#[cfg(...)]`
    # cluster gate exists only in the per-cluster tree. With no parent to
    # declare them, all 19 fell through the inventory's undeclared-sibling
    # branch, which counts a sibling unconditionally and never evaluates a gate.
    # Measured on the nightly tree 2026-08-01: the 18 tests in
    # `error.gen_tests_1..4` and the 2 in `wrapper.gen_tests_4` are gated
    # `not(any(error_generic_member_access, feature = "error_generic_member_access"))`
    # and cannot compile under nightly's `--cfg error_generic_member_access`,
    # yet were counted on every sweep — `oracle_static=124`, `missing=26`, of
    # which only 6 were the one real `tests/test_downcast.rs` crash. The other
    # 20 were a denominator the tree never contained, and each sweep aimed a
    # crash retrigger at them.
    _expected_count, _expected_names = (
        deps.active_test_inventory(
            target_src_dir, cluster, frozenset(test_stems), _sweep_lib)
        if deps.active_test_inventory is not None else (0, frozenset()))
    # Walked once per cluster beside the inventory, not per sweep: the same
    # files, and the channel that reads it fires at most a handful of times.
    _test_files: dict[str, str] = (
        deps.test_file_index(target_src_dir, cluster, frozenset(test_stems),
                             _sweep_lib)
        if deps.test_file_index is not None else {})
    # Cause-C (runtime crash) retrigger: consecutive sweeps whose oracle was
    # short; at the threshold the frozen component the crashes implicate gets
    # re-authored (see the trigger block below the stall accounting).
    _vanish_streak = 0
    # Whether the CURRENT crash class has been proved deterministic — by the
    # accelerated re-sweep or by a completed streak. Separate from
    # `_vanish_streak` because the streak is fire budget: a fire resets it to 0,
    # and on run `929616-ee20d540` (2026-08-14) that is what left iter 2 — an
    # ASAN heap-use-after-free with 27 raw frames and none of them in-crate,
    # the exact state chain-derived blame exists for — unable to reach the
    # synthesis at all, because it was nested inside the streak gate. Zero
    # `CHAIN-DERIVED` lines and zero `phase_c.crash.chain_blame` events in that
    # run. Cleared only by a sweep in which every expected test name reported.
    _crash_deterministic = False
    # The chain set the evidence pack was last armed with, so an unchanged
    # inference re-arms silently.
    _chain_blamed_last: frozenset[FnLocator] = frozenset()
    # The grant iter of the last window whose within-grant vanish was logged,
    # so the standing condition prints once per window rather than per sweep.
    _predates_noted_for = -1
    # Shadow zero-payoff detector (domain/zero_payoff.py): the class-general
    # stall condition — same frontier dispatched K sweeps, nothing bought on
    # any axis (pass, crash class, frontier). LOGS ONLY; nothing reads it.
    # `_zp_planned` holds the previous sweep's dispatch set + readings so this
    # sweep can judge what that spend bought; grace-window sweeps are skipped
    # entirely (their deltas measure a deliberate re-stub, the same exemption
    # every score consumer applies). `_zp_fired_on` dedupes the log line per
    # target set — a standing condition is one line, not one per sweep.
    _zp_history: tuple[SweepOutcome, ...] = ()
    _zp_planned: tuple[
        frozenset[str], int, frozenset[str],
        tuple[tuple[str, int, int], ...]] | None = None
    _zp_fired_on: frozenset[str] | None = None
    # Whether the loop destroyed work between the last judged sweep and this
    # one — the only condition under which a moving frontier counts as payoff.
    _zp_restub_since = False
    # One name for the window so the fire line always states the count the
    # streak was actually judged on; tuning happens in step-2 calibration.
    _zp_window = 3
    _crash_retrigger_after = int(
        os.getenv("PHASE_C_CRASH_RETRIGGER_AFTER", "2"))
    # The window after a retrigger attempt in which the deliberate re-stub —
    # not the tree's quality — explains what the sweep reads. ONE object with
    # several read sites, because it is one fact: everything that reads a
    # DEPRESSED SCORE (the ratchet's breach at the top of the sweep, the stall
    # budget, the exit restore) keys on `bodies_in_flight`; the crash streak
    # further down keys on `active`, because the aborted binaries are its
    # evidence and a fired-but-failed attempt must not re-fire on the same sweep
    # it just acted on. Sized to the bodies re-stubbed and progress-gated — see
    # `RefillGrace`. Set by EVERY retrigger site: all three end in the same
    # surgical revert.
    _grace = RefillGrace.none()
    _grace_per_iter = int(os.getenv("PHASE_C_REFILL_GRACE_PER_ITER",
                                    str(DEFAULT_REFILL_PER_ITER)))
    _grace_max_iters = int(os.getenv("PHASE_C_REFILL_GRACE_MAX_ITERS",
                                     str(DEFAULT_REFILL_GRACE_MAX_ITERS)))
    # The window's WALL-CLOCK ceiling, sized off the measured cost of an
    # iteration rather than guessed: run `1920662-40f711b2` (2026-08-16/17) ran
    # 588 minutes for 6 iterations, so 360 minutes is ~3.5 iterations there and
    # a 54-body re-stub's 18-iteration budget stops being ~29 hours of a run
    # with the batch ratchet, the stall budget and the crash streak all
    # disarmed. Set to 0 to restore the iterations-only rule.
    _grace_max_minutes = float(os.getenv("TDD_REFILL_WINDOW_MAX_MINUTES", "360"))
    _run_started_at = time.monotonic()

    def _now_minutes() -> float:
        return (time.monotonic() - _run_started_at) / 60.0

    def _grant_grace(reverted: int,
                     vanished_at_grant: frozenset[str] = frozenset(),
                     ) -> RefillGrace:
        """Open a window for a landed re-stub, and SAY SO. Logged here rather
        than at the three fire sites so the window's size is on the record the
        moment it is granted: without it a window is only visible once it
        withholds something, and a clipped one looks exactly like a sized one.

        This is also where the pass high-water is VOIDED, for the same reason
        the logging lives here: all three fire sites end in the same surgical
        revert, and a void applied at only two of them is the shape of defect
        `PassHighWater` exists to close."""
        nonlocal _highwater
        _g = RefillGrace.for_restub(
            reverted, total_filled, per_iter=_grace_per_iter,
            max_iters=_grace_max_iters, granted_at=iter_n,
            vanished_at_grant=vanished_at_grant,
            granted_at_minute=_now_minutes(),
            max_minutes=_grace_max_minutes,
            # The mark the re-climb has to reach for the window to have been
            # repaid. Read BEFORE `lineage_replaced` below, because that is the
            # call that retires it.
            highwater_at_grant=_highwater.best)
        if _g.restubbed:
            _highwater = _highwater.lineage_replaced(iter_n)
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-fill window open for "
                 f"{_g.iters_left} iter(s) — {_g.restubbed} body(ies) re-stubbed "
                 f"at ~{_grace_per_iter} re-fill(s)/iter"
                 + (f"; CLIPPED by the {_grace_max_iters}-iter cap, so it will "
                    "expire before the bodies land" if _g.clipped else "")
                 + ". The batch ratchet, the stall budget and the crash streak "
                 "stand down while re-fills keep landing"
                 + (f", and no longer than {_grace_max_minutes:.0f} min or "
                    f"until the re-climb reaches {_g.highwater_at_grant} — an "
                    "exemption counted in iterations cannot bound what an "
                    "iteration costs"
                    if _grace_max_minutes > 0 or _g.highwater_at_grant >= 0
                    else "")
                 + ". The flat-score guard is NOT stood down and remains the "
                 "live terminator inside this window.")
        deps.emit("phase_c.tdd.refill_window_open", data={
            "cluster": cluster.name, "iter": iter_n,
            "restubbed": _g.restubbed, "iters_granted": _g.iters_left,
            "per_iter": _grace_per_iter, "clipped": _g.clipped,
            "max_minutes": _grace_max_minutes,
            "highwater_at_grant": _g.highwater_at_grant,
            # The pre-revert vanished reading the window can never explain —
            # a graced sweep vanishing ONLY these names keeps crash evidence
            # armed (see `RefillGrace.explains_vanish`).
            "vanished_at_grant": sorted(_g.vanished_at_grant)[:50],
        })
        return _g

    def _score_grace(iter_n: int, pass_count: int = -1) -> None:
        """Spend one iter of the re-fill window and score it on what landed.

        Called at EVERY point an iter ends having had (or been denied) its fill
        pass, never at the read sites: those are reached at different points of
        the same iter and must agree on which iter they are in, and the progress
        test needs this iter's fills. An iter with no frontier reaches the no-fill
        path and closes the window there — no work in flight, no protection, and
        no window that cannot close.

        A window granted by THIS iter is not spent by it. Every read site sits
        above the fire sites, so an iter that grants can never also read: spending
        it here would expire the window before a single consumer saw it, which is
        how the fired-but-failed grant (the one grant site that does not
        `continue`) was a silent no-op that let the crash retrigger re-fire in
        back-to-back iters — the exact burn it was added to prevent."""
        nonlocal _grace
        if not _grace.active or _grace.granted_at == iter_n:
            return
        # One clock reading for both calls: two `_now_minutes()` reads can
        # straddle the wall-clock ceiling, making the log's reason and
        # `consume`'s decision disagree about the same iter. And `consume`
        # checks the no-fills rule FIRST, so an early reason may be true of a
        # window that actually closed for landing nothing — attribute the
        # close to "early" only when fills kept it alive past that rule.
        _now = _now_minutes()
        _early = _grace.early_close_reason(_now, pass_count)
        if total_filled <= _grace.fills_at_grant:
            _early = ""
        _next = _grace.consume(total_filled, _now, pass_count)
        if not _next.active and _grace.restubbed:
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: re-fill window closed "
                f"({_grace.restubbed} body(ies) re-stubbed, "
                f"{total_filled - _grace.fills_at_grant} re-fill(s) landed this "
                f"iter, {_grace.iters_left - 1} iter(s) of the budget unspent)"
                + (f" — CLOSED EARLY: {_early}." if _early else " —")
                + " The batch ratchet judges the re-author from the next "
                "sweep on.")
            deps.emit("phase_c.tdd.refill_window_closed", data={
                "cluster": cluster.name, "iter": iter_n,
                "restubbed": _grace.restubbed,
                "fills_this_iter": total_filled - _grace.fills_at_grant,
                "iters_unspent": _grace.iters_left - 1,
                "early_close": _early,
            })
        _grace = _next

    # Set ONLY by the green exit. Every other way out of the loop leaves a
    # re-fill window's bet unsettled, and the two are not the same thing.
    _cluster_done = False
    _close_kind = ""
    _bet_restored = False

    def _restore_abandoned_bet(how: str) -> None:
        """Return the baseline when the loop leaves with re-stubbed bodies still
        in flight, whatever route it left by.

        Abandoning a bet mid-flight means the tree holds a re-author whose bodies
        never finished re-filling, so it scores BELOW the high-water by
        construction and the cluster would hand on its worst tree of the run — and
        a resumed run then opens on that tree. `bodies_in_flight`, not `active`: a
        fired-but-failed attempt left the tree exactly as it found it, so there is
        nothing to settle. A GREEN exit is not an abandonment either — that is the
        bet paying off — hence `_cluster_done`.

        Idempotent and called from every exit that can be reached with a window
        open: the loop's own fall-through, plus the two points where a graceful
        stop's `sys.exit(0)` unwinds past it (the iter's stop check, and the fill
        pass, which is where a stop lands in practice because it owns the hours).
        An UNEXPECTED exception from another dep still bypasses this — it also
        ends the run with a traceback, so the low tree is at least loud."""
        nonlocal _bet_restored
        if (_bet_restored or _cluster_done or not _grace.bodies_in_flight
                or not _batch_ratchet or _bratchet_snap is None):
            return
        _bet_restored = True
        _rev = restore_src_tree(_bratchet_snap)
        deps.log(f"  {tag} [phase-c/tdd] {how} with an open re-fill window "
                 f"({_grace.restubbed} body(ies) still re-stubbed) — reverted "
                 f"{_rev} file(s) to the best-known tree (pass={_bratchet_best}).")
        deps.emit("phase_c.tdd.open_window_restored", data={
            "cluster": cluster.name, "restubbed": _grace.restubbed,
            "files_reverted": _rev, "best_pass": _bratchet_best, "how": how,
        })
    _final_swept = False

    def _final_sweep(how: str) -> None:
        """Score the tree the run is about to LEAVE, on the way out.

        A cluster's fills are judged only at the next sweep, and a run that
        stops between a fill pass and that sweep hands its successor a tree
        nobody measured. Run `500301-6a66bf86` (2026-08-15) did exactly that:
        its last sweep read `pass=39 fail=48 oracle=87` with 3 crashed
        binaries, it then landed its behavioural re-fills, and it left on the
        stop sentinel — `[graceful] stop sentinel detected at impl-fill pool
        joined`, `exit=0` — with no sweep in between. Its own tail guard lines
        already carried the damage (`already crashed at baseline` naming TEN
        binaries), but nothing had scored it, so the batch ratchet, whose whole
        job is that drop, never got a reading to fire on. The next run opened
        on the result and read `pass=9 fail=94 oracle=103`.

        Cheap enough to be unconditional: the sweep costs about a second
        (`rc=101 (0.4s)`, `(1.0s)`, `(1.1s)` on the iteration lines) against the
        hours the fill pass just spent. It DELIBERATELY does not act on what it
        finds — no ratchet, no revert, no floor write. The tree is already being
        abandoned and a restore here would race `_restore_abandoned_bet`, which
        has its own rules about which tree is the right one to leave. This
        records the number so the drop is attributable to the run that caused
        it rather than to the one that inherits it.

        Never lets a stop become a crash: a failure to measure is logged and
        swallowed, because this runs inside the `except` on the way to `raise`
        and must not replace the original exception."""
        nonlocal _final_swept
        if _final_swept:
            return
        _final_swept = True
        try:
            _rc, _out = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                       timeout=test_timeout)
            _p, _f, _ = parse_test_counts(_out)
            deps.log(f"  {tag} [phase-c/tdd] final sweep ({how}): "
                     f"rc={_rc} pass={_p} fail={_f} — the tree this run leaves "
                     "behind, measured so the next one does not inherit an "
                     "unscored fill pass.")
            deps.emit("phase_c.tdd.final_sweep", data={
                "cluster": cluster.name, "iter": iter_n, "rc": _rc,
                "pass_count": _p, "fail_count": _f, "how": how,
                "best_pass": best_pass})
        except BaseException as _e:   # noqa: BLE001 - must not mask the stop
            deps.log(f"  {tag} [phase-c/tdd] final sweep ({how}) could not "
                     f"complete: {type(_e).__name__}: {_e}")

    def _emit_retrigger_iter(kind_: str, reverted: int) -> None:
        """Emit this iter's outcome from a retrigger path.

        The loop's normal `phase_c.tdd.iter` emit sits at the bottom, past three
        `continue`s that a fired retrigger takes — so `events.jsonl` silently
        skips exactly the iters the most expensive mechanism fires in. Run
        `1135197` set its peak (112) and both of its 111s in such iters, and every
        one of those pass counts had to be reconstructed from log text;
        `tools/pass_delta.py`, `tools/run_health.py` and the audit's iteration
        series all read this event, so all three were blind to them. Same field
        set as the normal emit, plus the bodies re-stubbed, so no consumer needs
        to know which path produced it."""
        deps.emit("phase_c.tdd.iter", data={
            **_prov,
            "cluster": cluster.name, "iter": iter_n, "rc": rc,
            "frontier_size": len(eligible), "kind": kind_,
            "pass_count": pass_count, "fail_count": fail_count,
            "improved": improved, "stall": stall,
            "binaries": [[lbl, p, f] for lbl, p, f in _bins],
            "build_red": _build.red,
            "build_missing": sorted(_build.missing),
            "build_failed_targets": list(_build.failed_targets),
            "retrigger_reverted": reverted,
        })

    # Cause-A (build failure) retrigger: consecutive sweeps in which the test
    # binaries did not compile. Carried as evidence, never as a threshold — a
    # tree that cannot build produces no verdict at all, so waiting is pure loss.
    _compile_broke_streak = 0
    # The current sweep's build accounting (assigned once per iteration where
    # the sweep output is final); seeded empty so the retrigger-path iteration
    # emit above can carry it before the first sweep assigns it.
    _build = sweep_build("", _expected_labels)
    # Units the fill phase dispatched since a sweep last read the red-build
    # streak. The refusal's sentence is "the fill passes between these sweeps
    # changed nothing", and in run `2131920-86818224` both passes it cited
    # dispatched 0 of 0 — so a repeated digest counts only across a real
    # attempt.
    _dispatched_before_sweep = 0
    # Stub count at the previous sweep, for the plateau check below. -1 means
    # "no previous sweep", which never counts as a decrease.
    _stubs_prev = -1
    # Set only by the compile-blocked exit below, and returned, so the caller
    # can tell "this cluster is finished" from "this tree does not build".
    _baseline_broken = False

    def _reconcile(rc_: int, output_: str, pass_n: int, fail_n: int,
                   failed_: set[str]) -> SweepReading:
        # rc -1 is the caller's timeout. With a test section open at the cut
        # the capture is a reading — that binary HUNG (marked so, its
        # in-flight test named) and the binaries after it never ran — and it
        # is reconciled exactly like a crashed one below: the vanished tests
        # count as failing. Only a cut before any section opened (a build
        # that did not finish) has no oracle to reconcile against.
        _bins = parse_binary_results(output_, timed_out=(rc_ == -1))
        _passing = TestStateReading(_bins).passed_names
        if ((rc_ == -1 and not any(b.hung for b in _bins))
                or deps.active_test_inventory is None):
            return SweepReading(fail_n, failed_, SweepOracle(frozenset(), 0),
                                _passing)
        _ig = parse_test_counts(output_)[2]
        # Per-binary reading supplies what the flat name sets cannot: the
        # NAMES of cluster-ignored tests (so they are not mistaken for
        # crash-vanished), the trailer-authoritative executed total (so tests
        # sharing a bare name across binaries are not deduped out of the
        # denominator), and how many binaries died — which is what separates a
        # crash from a static inventory that over-counts.
        oracle = reconcile_sweep(
            _expected_count, _expected_names,
            parse_test_output(output_).passed, failed_, _ig,
            ignored_names=frozenset(n for b in _bins for n in b.ignored),
            executed_count=sum(b.executed for b in _bins),
            crashed_binaries=sum(1 for b in _bins if not b.completed))
        if not oracle.intact:
            # A cluster that failed to COMPILE ran no binaries at all — the
            # vanished names say nothing about crashes then, and downstream
            # consumers (crash localizer expectation, layout-retrigger streak)
            # must not read them as crash evidence.
            _build_ = sweep_build(output_ or "", _expected_labels)
            _compile_broke = _build_.red
            _err_total, _err_digest = (
                compile_error_digest(_build_.build_output)
                if _compile_broke else (0, []))
            _why = []
            if oracle.vanished:
                # On a compile break every expected name vanishes for ONE
                # reason, so listing the names says nothing and the rustc
                # diagnostics say everything — print those instead. Dumping 100
                # test names while discarding the errors made a 2h burn on a
                # crate whose test binaries could not build read exactly like a
                # hard fill problem.
                if _compile_broke:
                    _why.append(
                        f"{len(oracle.vanished)} named test(s) reported no "
                        "verdict (cluster compile failed — no test binaries "
                        f"ran); {_err_total} distinct rustc error(s)"
                        + (": " + " | ".join(_err_digest) if _err_digest
                           else " — none carried a parseable location"))
                else:
                    # The unnamed remainder belongs to the same crash: a dead
                    # binary's test whose bare name is also defined in a live
                    # binary still shows up as observed, so it is missing from
                    # the total without ever being named. Attributing it to the
                    # inventory would send the next reader after an enumeration
                    # bug that does not exist.
                    _dead = oracle.deficit - len(oracle.vanished)
                    _why.append(
                        f"{len(oracle.vanished)} named test(s) reported no "
                        f"verdict (binary crashed): {sorted(oracle.vanished)[:20]}"
                        + (f"; {_dead} further execution(s) lost to the same "
                           f"{oracle.crashed} crashed binary/binaries, their "
                           "bare names also defined in a surviving binary"
                           if _dead > 0 else ""))
            elif oracle.crashed:
                _why.append(
                    f"{oracle.deficit} execution(s) short with no expected NAME "
                    f"unreported — {oracle.crashed} binary/binaries crashed and "
                    "every bare name they own is also defined elsewhere")
            if oracle.unnamed_shortfall:
                _why.append(
                    f"{oracle.unnamed_shortfall} unnamed test(s) short of the "
                    f"expected total — every expected NAME reported, so the "
                    f"static inventory OVER-COUNTED the oracle (not a crash: "
                    f"no test is missing, the denominator is too big)")
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: ORACLE SHRUNK — "
                f"counting {oracle.missing_count} as failing. "
                + "; ".join(_why))
            deps.emit("phase_c.tdd.oracle_vanished", data={
                "cluster": cluster.name, "iter": iter_n,
                "expected": _expected_count,
                "missing": oracle.missing_count,
                # `vanished: []` with a non-zero `missing` is ambiguous on its
                # own — a reader cannot tell a crash that ate unnamed tests
                # from a static inventory that over-counted. The two have
                # different remedies (hunt a stack vs. fix enumeration), so
                # the cause is carried, never left to be inferred.
                "unnamed_shortfall": oracle.unnamed_shortfall,
                # Diagnostics travel WITH the event: a compile-broken sweep is
                # only actionable if the reader can see what failed to build
                # without re-running cargo by hand against the right tree.
                "compile_errors": _err_total,
                "compile_digest": _err_digest,
                "build_failed_targets": list(_build_.failed_targets),
                "crashed_binaries": oracle.crashed,
                "cause": "+".join(
                    ((["compile_fail"] if _compile_broke else ["crash"])
                     if (oracle.vanished or oracle.crashed) else [])
                    + (["inventory_overcount"] if oracle.unnamed_shortfall
                       else [])),
                "vanished": sorted(oracle.vanished)[:200]})
            fail_n += oracle.missing_count
            failed_ = set(failed_) | set(oracle.vanished)
        return SweepReading(fail_n, failed_, oracle, _passing)

    def _sweep_reconciliation(
            output_: str, pass_n: int, fail_n: int) -> VerdictReconciliation:
        """The counters-vs-qualified-verdicts check for one reading, computed
        where the score is about to be USED rather than where it is logged —
        so the ratchet, the floor, the checkpoint and the refill charge can
        each decline a tainted number instead of acting on it."""
        _pp, _fp, _inc = qualified_verdicts(parse_binary_results(output_))
        return VerdictReconciliation(
            counter_pass=pass_n, qualified_pass=len(_pp),
            counter_fail=fail_n, qualified_fail=len(_fp),
            incomplete=_inc, has_qualified=bool(_pp or _fp))

    iter_n = 0
    # READ PATH: cluster entry. A fresh cluster gets the stub skeleton today;
    # with the ledger it gets the stub skeleton plus every body the ledger can
    # vouch for under the current layout — which is what makes a from-scratch
    # restart cost minutes instead of the fortnight the record measures (the
    # 2026-08-08 relaunch spent 7h45m to move 11 -> 13 on a tree whose 95
    # passing bodies were sitting in an archive nothing could address).
    #
    # This is also where the augmented reset's value is delivered. Re-seeding
    # inside `_archive_reconstruction` would write bodies into a tree the
    # `git reset --hard` then deletes; the ledger lives OUTSIDE the workspace
    # precisely so the next cluster entry — this line — can use it.
    if deps.ledger_size is not None:
        _rows, _bytes = deps.ledger_size(project_dir)
        deps.log(f"  {tag} [phase-c/ledger] {_rows} row(s), "
                 f"{_bytes / 1024:.0f} KiB on disk (append-only, untracked, "
                 "machine-local — a cache of provenance, never the only copy)"
                 + (f"; oracle {_oracle_fp(0)} — rows graded under another "
                    "oracle are refused by the gate and skipped by the "
                    "re-buy detector"
                    if deps.oracle_fp is not None else
                    "; NO oracle fingerprint wired — every row is refused"))
    _reseed_site("cluster entry", 0)
    if _ledger_adopt():
        deps.log(f"  {tag} [phase-c/ledger] {len(_ledger_rows)} backfilled "
                 "body(ies) are the bodies on this tree — this process's "
                 "first sweep judges them, because a `landed` row no sweep "
                 "has judged on THIS code is never re-seeded.")
    # Hard cap removed — the sole non-success terminator is `stall >=
    # max_stalls` on the pass_count floor. The 10k backstop here mirrors
    # `_improvement_loop`'s safety net and should never fire in practice.
    _tdd_backstop = int(os.getenv("PHASE_C_TDD_BACKSTOP", "10000"))

    def _persist_floor() -> None:
        # ONE spelling for every save. The cursor is a dispatch fact (written
        # before fill so a kill does not re-send the same 12). Served is a
        # fill-return fact. Products are an after-fire fact — GPU_GUARD_STOP
        # fires before the dispatch save, so a product computed on the
        # stopping sweep used to die in memory (run `2773540-e47c3754`).
        if not _floor_persistable or deps.save_tdd_floor is None:
            return
        deps.save_tdd_floor(project_dir, cluster.name, _floor_snapshot(
            cluster=cluster.name, best_pass=best_pass, stall=stall,
            tree_sha=_tree_sha, oracle_size=pass_count + fail_count,
            code_id=_code_id, attempted=_pass_attempted,
            landed=_pass_landed, declined=_pass_declined,
            served=_served_at, served_verified=_served_verified,
            futility=prev_test_units,
            products=escalation_products, producers=escalation_producers))

    while True:
        iter_n += 1
        # The coverage binding is NOT cleared here: it stands from its
        # measurement (below, after attribution) until the next measurement
        # replaces it or a deliberate destruction invalidates it (the
        # retrigger re-stubs and the batch-ratchet restore clear it at the
        # site of the destruction — a reading of a discarded tree misleads
        # every consumer). What the carry-over buys is the attribution
        # phase: it runs BEFORE this iteration's own measurement, so the
        # format-hop's measured redirect reads the PREVIOUS sweep's owner
        # map — keyed by test name, conservative on any test it did not
        # measure. A blanket clear here made that redirect structurally
        # unreachable: every attribution saw None (found by the
        # `358833-aa15d93c` walk's binding-order pin, 2026-09-01). Process
        # exit still clears via `_clear_coverage_on_return`.
        if iter_n > _tdd_backstop:
            deps.log(f"  {tag} [phase-c/tdd] backstop {_tdd_backstop} reached — "
                 "stopping. This should never happen — stall budget should "
                 "have fired first.")
            break
        try:
            # Both exit by RAISING (`sys.exit`), so they leave through neither a
            # `break` nor a `return` — BaseException, because SystemExit is not
            # an Exception and that is exactly the case being caught.
            deps.check_graceful_stop(f"TDD iter {iter_n}")
            deps.check_auto_operator(stall, f"TDD iter {iter_n}")
        except BaseException:
            _restore_abandoned_bet("stopped at the iter boundary")
            # The boundary sits BEFORE this iteration's sweep, so the previous
            # iteration's fills are unmeasured here too — the same hole as the
            # fill-pass exit, one statement earlier in the loop.
            _final_sweep("stopped at the iter boundary")
            raise
        # The re-admission boundary decision used to run HERE, before the
        # sweep. That ordering is what let run `58819-ee78a1d1`'s iter-6
        # epoch re-base adopt a pre-destruction 79 while the tree measured
        # 74 — the retrigger fires after a sweep, so at the NEXT boundary
        # `best_pass` still describes the tree it deleted — and the release
        # iter 5's genuine 74 -> 79 re-climb had earned was consumed by
        # bookkeeping (79 > 79 never fires). The decision now runs after this
        # iteration's sweep and after the high-water's own destruction
        # re-base, in `readmission_boundary` — order is that function's
        # documented contract.
        # The boundary invariant: the tree a sweep scores must parse. Every
        # mechanical writer verifies its own transaction, and this is the
        # reading that does not depend on a writer having been migrated.
        _heal_unparseable("before the sweep", iter_n)
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: running cargo test...")
        t0 = time.monotonic()
        rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                  timeout=test_timeout)
        dt = time.monotonic() - t0

        def _hung_of(rc_: int, output_: str) -> tuple[BinaryResult, ...]:
            return hung_sections(output_ or "") if rc_ == -1 else ()

        def _log_hung(hung_: tuple[BinaryResult, ...], *,
                      iter_: int = iter_n) -> None:
            if hung_:
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_}: cargo test rc=-1 HUNG in "
                    f"{hung_[-1].label}::{hung_[-1].dangling or '?'} after "
                    f"{test_timeout}s — the binary did not die, a test never "
                    "returned; its tests and every binary after it count as "
                    "failing (a crash-shaped reading), not as 'no reading'.")

        _hung = _hung_of(rc, output)
        _log_hung(_hung)
        pass_count, fail_count, _ignored = parse_test_counts(output)
        cur_failed = parse_failed_tests(output)
        _sweep = _reconcile(rc, output, pass_count, fail_count, cur_failed)
        fail_count, cur_failed, _oracle = (
            _sweep.fail_count, _sweep.failed, _sweep.oracle)
        # Reconciliation of THIS reading, before anything charges on it. The
        # mismatch line below ("Score claims from this sweep are unsafe") was
        # computed for a session and gated nothing — the ratchet, the floor
        # and the checkpoint all acted on the same score it disclaimed.
        _rec0 = _sweep_reconciliation(output, pass_count, fail_count)
        # Batch ratchet. pass_count reflects the PREVIOUS iter's fills;
        # a drop below the best we've measured means that iter net-regressed the
        # cluster (a regression the per-fill guard couldn't catch while the crate
        # was red mid-batch). Restore the best-known src tree and re-test, so the
        # loop always attacks from — and can only exit at — its high-water mark.
        # Scored on the pass count and the passing NAMES, never on "did a binary
        # start crashing": a binary that was passing nothing costs nothing when
        # it aborts, and convicting on the crash alone makes the gate strictest
        # exactly when the tree is at its best.
        _breach = ratchet_breach(pass_count, _bratchet_best,
                                 _sweep.passing, _bratchet_passing)
        _withheld = breach_withheld(_breach, _grace)
        if _breach and not _withheld and not _rec0.reconciled:
            # A tainted score cannot charge a breach: the counters and the
            # binary-qualified verdicts disagree in the direction no crash
            # explains, so the number about to convict last iter's fills is
            # not a measurement of them.
            _withheld = ("the sweep's counters and its qualified verdicts "
                         f"disagree ({_rec0.note}) — a tainted score cannot "
                         "charge a breach")
        if _breach and not _withheld:
            # Second standing withhold: the fill gates KEEP a body whose only
            # cost is a binary aborted by a still-hollow stub's panic, so a
            # breach whose every lost test crash-vanished under stub-only
            # panics is the component closing, not last iter's fills
            # regressing — but only while the hollow set strictly shrank.
            # Restoring the snapshot here while holes are closing re-opens
            # the very holes the closure just paid to fill. A drop that
            # leaves the hollow set flat (run `1160135-7b10cdfa`: peel put
            # two constructors back, kept ten rewrites, 15 stubs still 15)
            # is last iter's fills, and the charge restores the snapshot.
            _withheld = closure_withheld(
                _breach, _bratchet_passing - _sweep.passing,
                TestStateReading(parse_binary_results(output)).judged_names,
                closure_stub_sites(_bratchet_reading, output),
                hollow_now=hollow_count(snapshot_src_tree(target_src_dir)),
                hollow_at_snapshot=hollow_count(_bratchet_snap),
            )
        if _withheld:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                     f"breach ({_breach}) NOT charged: {_withheld}.")
            deps.emit("phase_c.tdd.batch_withheld", data={
                "cluster": cluster.name, "iter": iter_n,
                "pass_count": pass_count, "best_pass": _bratchet_best,
                "breach": _breach, "reason": _withheld,
                "restubbed": _grace.restubbed,
                "grace_iters_left": _grace.iters_left,
            })
            _breach = ""
        if _batch_ratchet and _bratchet_snap is not None and _breach:
            # A crashing binary drops a nondeterministic subset of tests, so a
            # single breached reading can be pure noise; re-sweep the UNCHANGED
            # tree and only a reproduced breach earns the destructive revert
            # (a spurious one would wipe sibling-blocked fills every iter and
            # lock the plateau).
            rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                        timeout=test_timeout)
            _hung = _hung_of(rc, output)
            _log_hung(_hung)
            pass_count, fail_count, _ignored = parse_test_counts(output)
            cur_failed = parse_failed_tests(output)
            _sweep = _reconcile(rc, output, pass_count, fail_count, cur_failed)
            fail_count, cur_failed, _oracle = (
                _sweep.fail_count, _sweep.failed, _sweep.oracle)
            _confirm = ratchet_breach(pass_count, _bratchet_best,
                                      _sweep.passing, _bratchet_passing)
            if not _confirm:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                         f"breach ({_breach}) NOT reproduced on re-sweep; "
                         "keeping the tree (crash-noise reading).")
            else:
                _rev = restore_src_tree(_bratchet_snap)
                # Deliberately NOT an epoch bump: the restore returns the
                # tree TO the snapshot that set the high-water, so every
                # baseline taken at that high-water describes exactly the
                # tree now on disk. What it destroys is the unbanked fills
                # since the snapshot — progress no baseline ever recorded.
                # The epoch's subjects are the retrigger re-stubs, which
                # leave a tree NO baseline describes.
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                         f"{_confirm} (last iter's fills net-regressed, "
                         f"confirmed on re-sweep); reverted {_rev} file(s) to "
                         "best state, re-testing.")
                deps.emit("phase_c.tdd.batch_revert", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count, "best_pass": _bratchet_best,
                    "breach": _confirm, "files_reverted": _rev,
                })
                # READ PATH: the restore is whole-tree and older by
                # construction, so it re-stubs units the ledger can vouch for
                # under the SAME layout — the restore only rolled the tree
                # back, it did not re-author anything. Deliberately NOT an
                # epoch bump (see the comment above), which is also why the
                # ledger's own refutation rule carries a body-identity clause
                # instead of trusting the epoch alone.
                # The held-over coverage binding described the tree this
                # restore just discarded — a deliberate destruction
                # invalidates the measurement for every consumer, this
                # iteration's attribution included.
                bind_coverage_slice(None)
                _reseed_site("after batch-ratchet restore", iter_n)
                rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                            timeout=test_timeout)
                _hung = _hung_of(rc, output)
                _log_hung(_hung)
                pass_count, fail_count, _ignored = parse_test_counts(output)
                cur_failed = parse_failed_tests(output)
                _sweep = _reconcile(rc, output, pass_count, fail_count, cur_failed)
                fail_count, cur_failed, _oracle = (
                    _sweep.fail_count, _sweep.failed, _sweep.oracle)
        # A HUNG sweep in a process that holds no batch snapshot — the entry
        # sweep of a resumed run — cannot be clawed back by the ratchet above,
        # and the hung tree is what every later sweep would re-measure at the
        # cost of the whole timeout. The durable pair is the tier that
        # survives the process boundary; it used to fire only at the
        # flat-score stop (disabled under `gpu-guard=off`). It fires HERE,
        # once per process, when its mark beats the hung reading's verified
        # floor. Run `3371694-c8835690` (2026-09-08): the entry sweep hung in
        # `tests/ptr_gen_1.rs` at a floor of 27 with `stable:91` on disk one
        # directory away, and the loop left the cluster for nightly.
        if (_hung and _bratchet_snap is None and not _restore_spent
                and deps.restore_best_tree_pair is not None
                and deps.load_best_tree_mark is not None):
            _mark_h = deps.load_best_tree_mark(project_dir, cluster.name)
            if _mark_h is not None and _mark_h.pass_count > pass_count:
                _pre_pairs_h = qualified_verdicts(
                    parse_binary_results(output, timed_out=True))
                _restored_h = deps.restore_best_tree_pair(
                    project_dir, cluster.name, target_src_dir)
                if _restored_h is not None:
                    _restore_spent = True
                    _bodies_h, _layout_h, _left_h = _restored_h
                    _where_h = f"{_hung[-1].label}::{_hung[-1].dangling or '?'}"
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: hung tree restored "
                        f"— the sweep hung inside {_where_h} at a verified floor "
                        f"of {pass_count} and the durable mark is "
                        f"{_mark_h.pass_count}/{_mark_h.oracle_size}; RESTORED "
                        f"that pair ({_bodies_h} body file(s) + {_layout_h} "
                        "layout file(s)). THE COST, stated: every fill landed "
                        "since the mark is discarded"
                        + (f"; {len(_left_h)} file(s) present here and not in "
                           f"the snapshot are left in place ({_left_h[:3]})"
                           if _left_h else "")
                        + ". The restore is spent for this process; re-testing.")
                    deps.emit("phase_c.tdd.hung_tree_restored", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "pass_count": pass_count, "hung_in": _where_h,
                        "mark_pass": _mark_h.pass_count,
                        "mark_oracle": _mark_h.oracle_size,
                        "bodies": _bodies_h, "layout_files": _layout_h,
                        "leftovers": _left_h[:20]})
                    # A deliberate destruction invalidates the measurement for
                    # every consumer: the held-over coverage binding and the
                    # re-seed site described the discarded tree.
                    bind_coverage_slice(None)
                    _reseed_site("after hung-sweep restore", iter_n)
                    rc, output = deps.run_cargo(test_cmd, target_src_dir,
                                                cluster_env, timeout=test_timeout)
                    _hung = _hung_of(rc, output)
                    _log_hung(_hung)
                    pass_count, fail_count, _ignored = parse_test_counts(output)
                    cur_failed = parse_failed_tests(output)
                    _sweep = _reconcile(rc, output, pass_count, fail_count,
                                        cur_failed)
                    fail_count, cur_failed, _oracle = (
                        _sweep.fail_count, _sweep.failed, _sweep.oracle)
                    if _bodies_h > 0 and qualified_verdicts(parse_binary_results(
                            output, timed_out=(rc == -1))) == _pre_pairs_h:
                        deps.log(
                            f"  {tag} [phase-c/tdd] iter {iter_n}: the sweep after "
                            "the hung-tree restore is byte-identical to the "
                            "hung reading — the binaries did not rebuild; this "
                            "reading measured the discarded tree.")
        # Refresh the best-known snapshot on the first iter and each strict
        # improvement (never on a plateau — the existing best-state snapshot is
        # equivalent and re-reading the tree would be wasted I/O). No oracle
        # veto here: control only reaches this point when the tree did NOT
        # breach, i.e. it is not a net loss against the current best, and a
        # tree carrying a costless crash is a legitimate baseline. Vetoing it
        # instead leaves the baseline pinned to the pre-fill tree, so the next
        # real breach reverts the good fill too and the loop re-fills it
        # forever at zero payoff.
        _tree_sha = tree_content_sha(_src_root)
        # Reconciliation of the FINAL reading of this iter (the ratchet may
        # have re-swept above); every consumer below — snapshot, checkpoint,
        # floor, refill charge, next iter's re-admission — reads this one.
        _rec = _sweep_reconciliation(output, pass_count, fail_count)
        if _batch_ratchet \
                and snapshot_due(pass_count, _bratchet_best, _tree_sha, _bratchet_sha):
            _snap = snapshot_src_tree(_src_root)
            if _snap is not None:
                _bratchet_best = pass_count
                _bratchet_snap = _snap
                _bratchet_sha = _tree_sha
                _bratchet_passing = _sweep.passing
                _bratchet_reading = TestStateReading(parse_binary_results(output))
        # Durable checkpoint of the new-best fill tree so a later heal restores
        # THIS good state (best-tree copy / git ancestor) instead of nuking the
        # file to the baked skeleton. Independent of _batch_ratchet. A short
        # oracle blocks only an EQUAL count: missing tests were already counted
        # as failing above, and `parse_test_counts` is trailer-only (a crashed
        # binary contributes zero passes), so pass_count is a verified floor —
        # every counted pass ran green in a COMPLETED binary, and a floor
        # strictly above the stored mark dominates it whatever the missing
        # tests would have said. Requiring a full oracle unconditionally froze
        # the slot under a chronic crasher: 2026-08-09/10 on `anyhow`, the mark
        # held 18/105 (written 08:56 UTC) while sweeps read 81/105 and 88/106
        # with `tests/test_context.rs` crashing every iteration — heal's
        # restore tier sat 70 tests behind the live tree, silently. An equal
        # count still needs a full oracle: an unmeasurable tree at an equal
        # count must not churn the slot through snapshot_due's tree-moved arm.
        _snap_due = snapshot_due(
            pass_count, _checkpoint_best, _tree_sha, _checkpoint_sha)
        if deps.checkpoint_fill_tree is not None and _snap_due \
                and not _rec.reconciled:
            deps.log(
                f"  {tag} [phase-c/tdd] durable best tree NOT replaced: the "
                f"sweep's counters and qualified verdicts disagree "
                f"({_rec.note}) — a tainted score cannot set a floor.")
        elif deps.checkpoint_fill_tree is not None and _snap_due \
                and (_oracle.missing_count == 0
                     or pass_count > _checkpoint_best):
            deps.checkpoint_fill_tree(
                project_dir, cluster.name, target_src_dir,
                BestTreeMark(pass_count=pass_count, iteration=iter_n,
                             tree_sha=_tree_sha,
                             oracle_size=pass_count + fail_count,
                             missing_count=_oracle.missing_count))
            _checkpoint_best = pass_count
            _checkpoint_sha = _tree_sha
        elif deps.checkpoint_fill_tree is not None and _snap_due:
            # A guard that declines silently is how the freeze above went
            # unnoticed for a session — say what was withheld and why.
            deps.log(
                f"  {tag} [phase-c/tdd] durable best tree NOT replaced: sweep "
                f"{pass_count} with {_oracle.missing_count} test(s) unmeasured "
                f"does not beat the stored {_checkpoint_best} — an equal count "
                "needs a full oracle.")
        # F6 — measurement provenance: stamp every iter event with the tree it
        # measured (content hash), the oracle size (denominator), and the actual
        # test names, so cross-run pass-count claims are verifiable and oracle
        # drift is detectable.
        # Binary-qualified verdicts from the SAME parse the counters come from.
        # A bare name is not a verdict here: one name runs in several binaries,
        # and a crashed binary breaks the name<->execution mapping in both
        # directions at once. Pairs are what `tools/pass_delta.py` must diff.
        _p_pairs, _f_pairs, _incomplete = qualified_verdicts(
            parse_binary_results(output))  # same output `_rec` was read from
        _measure = Measurement(
            cluster=cluster.name, iteration=iter_n,
            tree_sha=_tree_sha,
            oracle_size=pass_count + fail_count,
            pass_count=pass_count, fail_count=fail_count,
            passed=parse_test_output(output).passed,
            failed=frozenset(cur_failed),
            passed_pairs=_p_pairs, failed_pairs=_f_pairs,
            incomplete=_incomplete,
            # The STATIC inventory, when one is available. `pass + fail` is
            # derived (`fail_count` is itself `oracle - pass`), so an invariant
            # stated over it is circular and can never catch anything.
            oracle_static=_expected_count)
        last_failed = frozenset(_measure.failed)
        last_ran = frozenset(_measure.passed) | last_failed
        if not _rec.reconciled:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: VERDICT MISMATCH — "
                     f"{_rec.note}. Score claims from this sweep are unsafe.")
        _prov = {"tree_sha": _measure.tree_sha, "oracle_size": _measure.oracle_size,
                 "oracle_static": _expected_count,
                 "passed": sorted(_measure.passed)[:200],
                 "failed": sorted(_measure.failed)[:200],
                 "passed_pairs": [list(p) for p in sorted(_p_pairs)][:200],
                 "failed_pairs": [list(p) for p in sorted(_f_pairs)][:200],
                 "incomplete": sorted(_incomplete),
                 "hung": sorted(b.label for b in _hung),
                 "hung_tests": sorted(f"{b.label}::{b.dangling or '?'}" for b in _hung),
                 "reconciled": _rec.reconciled,
                 "reconcile_note": _rec.note,
                 # The outer-loop budget travels with every iter event so a
                 # dashboard can render stall x/N without hardcoding the cap.
                 "max_stalls": max_stalls}
        # THE LEDGER'S PROMOTION PASS. Every body this process landed since
        # the last sweep is judged HERE, against the verdict vector and
        # nothing else — never a pass count and never a high-water, so the
        # standing rule about baselines taken across a deliberate destruction
        # cannot reach the ledger through a score.
        #
        # Bare test names on both sides: the reverse map is keyed by the bare
        # name (`_bare = _vt.split("::")[-1]` is how every other consumer in
        # this module reads it) while a sweep reports `binary::module::test`.
        # The collapse loses the distinction between two binaries running a
        # same-named test; for "did this unit's tests regress" that is the
        # question anyway, and the pair-level vector stays available to every
        # consumer that needs it.
        # `_measure.failed` carries the crash-vanished names the score counts
        # as failing; `of_sweep` takes them back out, so a test whose binary
        # aborted judges `unmeasured` rather than `lost`.
        _sweep_view = SweepView.of_sweep(
            passed=(bare_test_name(t_) for t_ in _measure.passed),
            failed=(bare_test_name(t_) for t_ in _measure.failed),
            vanished=_oracle.vanished,
            intact=_rec.reconciled, epoch=_destruction_epoch,
            # Where each failing test panicked: the one reading that can
            # tell a body which got a test past an assertion from one that
            # moved nothing, when both leave the verdict vector identical.
            panic_sites={
                bare_test_name(f_.test_name): (f_.panic_file, f_.panic_line)
                for f_ in parse_test_output(output).failures
                if f_.panic_file and f_.panic_line is not None})
        if _ledger_rows and deps.ledger_append is not None:
            _judged: list[LedgerEntry] = []
            for _row in _ledger_rows:
                _unit = FnLocator(_row.key.file, _row.key.fn_name)
                # The reverse map is attribution proper; an escalation-only
                # unit (producer escalation, owner-map admission) is absent
                # from it, and judging on it alone graded every such landing
                # UNMEASURED with its premise erased (`judgment_attributed`
                # carries the run). The row's own premise is joined in.
                _attributed = judgment_attributed({
                    bare_test_name(_t) for _t, _units in test_reverse_map.items()
                    if _unit in _units}, _row)
                _now = (body_sha(deps.tree_writer.bodies(_unit, target_src_dir))
                        if deps.tree_writer is not None else "")
                _state, _delta = classify(
                    _row, attributed=_attributed,
                    before=_ledger_before or _sweep_view, after=_sweep_view,
                    body_now_sha=_now)
                # The oracle is stamped HERE, at the grade, not carried
                # from the landing: an adopted backfilled row arrives with
                # no oracle at all, and a verdict is a claim about the tests
                # that were run to reach it. So is the convicting premise —
                # the same `names & before.failed` the classification is
                # scoped to — which is what lets a flat exclusion later tell
                # "same evidence, do not re-buy" from "a new test convicts
                # this unit now".
                _judged.append(replace(
                    _row, state=_state,
                    verdict_delta=_delta, iteration=iter_n,
                    epoch=_destruction_epoch,
                    run_id=_row.run_id or deps.run_id,
                    oracle_fp=_oracle_fp(iter_n),
                    convicting=tuple(sorted(
                        _attributed
                        & (_ledger_before or _sweep_view).failed))))
            deps.ledger_append(project_dir, _judged)
            if _ledger_cache is not None:
                _ledger_cache.extend(_judged)
            _counts = Counter(str(r.state) for r in _judged)
            deps.log(
                f"  {tag} [phase-c/ledger] iter {iter_n}: judged "
                f"{len(_judged)} landed body(ies) — "
                + ", ".join(f"{n} {s}" for s, n in sorted(_counts.items()))
                + ". `unmeasured` means the attributed tests did not run, "
                  "which is re-seedable and is NOT evidence about the unit.")
            deps.emit("phase_c.ledger.judged", data={
                "cluster": cluster.name, "iter": iter_n,
                "states": dict(_counts),
                "units": sorted(f"{r.key.file}::{r.key.fn_name}: {r.state}"
                                for r in _judged)})
            _ledger_rows = []
        _sweep_prev = _ledger_before
        _ledger_before = _sweep_view
        # Per-binary breakdown so triage can see WHICH test set
        # changed between iters (vanished tests, not just pass→fail flips).
        _bins = summarize_test_binaries(output)
        if _bins:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n} binaries: "
                     + ", ".join(f"{lbl} {p}/{f}" for lbl, p, f in _bins))
        # THE TRIPWIRE FOR AN UNMEASURED RESTORE. The restore replaces dozens
        # of source files; a sweep whose per-binary vector is byte-identical
        # to the PRE-restore sweep's cannot have rebuilt them, whatever the
        # cause — it measured the discarded tree. The mtime bump in
        # `restore_cluster_best_tree` removes the known cause (copy2 kept the
        # snapshot's day-old mtimes, cargo saw nothing newer-than-fingerprint
        # and re-ran the old binaries; run `2620196-de4a7ca2`, 2026-08-17,
        # sweep 0.4s after a 57-file restore). This check is what makes any
        # recurrence loud instead of a silent guard-spend: the reading is
        # excused from the flat window and the high-water re-base is deferred
        # to the first sweep that could actually see the restore.
        _restore_unmeasured = ""
        if _restore_probe is not None:
            _probe_pass, _probe_fail, _probe_bodies = _restore_probe
            _restore_probe = None
            if (_probe_bodies > 0 and (_p_pairs or _f_pairs)
                    and _p_pairs == _probe_pass and _f_pairs == _probe_fail):
                _restore_unmeasured = (
                    f"the best-tree restore replaced {_probe_bodies} body "
                    "file(s) and this sweep's binary-qualified verdict pairs "
                    "are identical to the pre-restore sweep's — the binaries "
                    "never rebuilt, so this is a reading of the DISCARDED "
                    "tree, not of the restore")
                deps.log(f"  {tag} [phase-c/tdd] GUARD iter {iter_n}: "
                         f"{_restore_unmeasured}.")
                deps.emit("phase_c.tdd.restore_unmeasured", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count,
                    "restored_bodies": _probe_bodies})
        # THE CONTRADICTION, printed. A fill pass that landed bodies against a
        # per-binary vector that did not move by one test is the loop's own
        # measurement disagreeing with its own activity, and until now nothing
        # said so on the line where both numbers are in hand: run
        # `1064759-0f23fb68` (2026-08-15/16) landed ~70 fills across six sweeps
        # whose vectors were byte-identical and printed no such line once.
        # Cheap and unconditional — a sweep with nothing landed since the last
        # one says nothing, and the grace window's own sweeps are excluded
        # because a re-stub is expected to move the vector down.
        _landed_since = total_filled - _filled_at_prev_sweep
        # A byte-identical vector can still hide movement: a failing test
        # that now fails somewhere else — a later assertion of its own file
        # (run `11358-e5de094e` iter 5 printed "bought nothing" over a fill
        # that took its aimed test from its first assertion to its second),
        # or a different crate function (run `3383938-c5d24499`: a kept
        # `construct` moved all 141 failing tests off its stub) — is the
        # loop doing something to the crate. One comparison answers both:
        # the ground the previous sweep stood on against this one's.
        _premise_now = _premise_of_capture(output)
        _sites_moved = (
            premise_moved(_prev_premise, _premise_now)
            if _prev_premise is not None and _sweep_prev is not None
            and _sweep_prev.epoch == _sweep_view.epoch else ())
        _prev_premise = _premise_now
        if (_prev_bins and _bins == _prev_bins and _landed_since > 0
                and not _grace.bodies_in_flight and _sites_moved):
            # Movement the vector cannot see — the clock restarts, as for a
            # moved verdict. Only ever LATER than before, so it can prevent
            # no reading a stop was sized to.
            _unmoved = _unmoved.moved()
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: {_landed_since} "
                "body(ies) landed since the last sweep; no test changed "
                f"verdict, but {len(_sites_moved)} failing test(s) now fail "
                "somewhere else: " + names_of(_sites_moved))
            deps.emit("phase_c.tdd.failure_site_moved", data={
                "cluster": cluster.name, "iter": iter_n,
                "filled_since": _landed_since,
                "moved": list(_sites_moved[:50]),
                "moved_count": len(_sites_moved)})
        elif (_prev_bins and _bins == _prev_bins and _landed_since > 0
                and not _grace.bodies_in_flight):
            _unmoved = _unmoved.unmoved(_landed_since)
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: {_landed_since} "
                "body(ies) landed since the last sweep and NO test changed "
                "verdict — the per-binary vector is byte-identical. The fills "
                "compile and are kept; on this evidence they bought nothing."
                + (f" That is {_unmoved.streak} consecutive such sweep(s) "
                   f"({_unmoved.landed} body(ies) in total) against a limit of "
                   f"{_unmoved.limit}." if _unmoved.enabled else ""))
            deps.emit("phase_c.tdd.no_verdict_moved", data={
                "cluster": cluster.name, "iter": iter_n,
                "filled_since": _landed_since,
                "unmoved_streak": _unmoved.streak,
                "unmoved_landed": _unmoved.landed,
                "unmoved_limit": _unmoved.limit,
                "pass_count": pass_count})
        elif (_prev_bins and _bins != _prev_bins
                and not _grace.bodies_in_flight):
            # A verdict moved somewhere. Whichever direction it moved in, the
            # loop is demonstrably doing something to the crate, which is all
            # this clock claims to measure — restart it. A sweep that landed
            # nothing, and a sweep inside the grace window, are NOT judgements
            # of a dispatch and leave the streak untouched (same exemptions the
            # flat-score guard applies).
            _unmoved = _unmoved.moved()
        _prev_bins, _filled_at_prev_sweep = _bins, total_filled
        if rc == 0 and not _oracle.intact:
            # cargo says green but part of the expected oracle never reported —
            # a shrunken suite must not close the cluster. Fall through to the
            # failing path with the vanished tests as failures.
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: rc==0 but the "
                     f"oracle is short {_oracle.missing_count} test(s) — NOT "
                     "declaring the cluster done.")
        if rc == 0 and _oracle.intact:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: all tests pass "
                 f"({pass_count} passed, {dt:.1f}s). Cluster done.")
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": 0,
                "frontier_size": 0, "kind": "pass",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            # The one exit that is not an abandonment: a green cluster with an
            # intact oracle IS the bet paying off, window open or not, so the
            # restore below must not claim it rolled the winning tree back.
            _cluster_done = True
            _close_kind = "green"
            break
        if rc == -1 and not _hung:
            # The timeout cut the run before any test section opened: the
            # build did not finish, so there is no reading of any kind. A
            # cut INSIDE a test is `_hung` and was scored above as a
            # crash-shaped reading; it never reaches this exit.
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test TIMED OUT "
                 f"({test_timeout}s) before any test binary opened a section — "
                 "the build did not finish. Stopping.")
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": -1,
                "frontier_size": 0, "kind": "timeout",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break

        # `output` is final for this iter by here (the ratchet's re-sweeps are
        # all above), so the build verdict is read once and shared by every
        # consumer below rather than re-derived at each. It is cargo's own
        # `could not compile` in the BUILD PHASE of the capture — never a
        # substring over the whole stream, which a test that runs a compiler
        # itself prints into the middle of a sweep whose every binary ran:
        # trybuild's nested build of this crate (`tests/compiletest.rs::ui`,
        # built without the cluster's `--cfg`) relayed `error: could not
        # compile `anyhow` (lib)` into run `2131920-86818224`'s iter-2 sweep,
        # 32 of 32 binaries complete; the substring read it as red, narrowed
        # the frontier from 33 fns to the one the NESTED build implicated,
        # dispatched 0 of 0 twice and closed `nightly` EXHAUSTED at 42/152
        # (2026-09-07). Every diagnostic reader below takes
        # `_build.build_output` for the same reason: the prefix cannot contain
        # a running test's stderr.
        _build = sweep_build(output or "", _expected_labels)
        _sweep_compile_broke = _build.red
        # A diagnostic rustc prints WITHOUT a code is parse-level (every
        # type/borrow/resolution error carries `[E…]`; no parse error does).
        # Confirmed against the extractor before acting — a lint denied
        # to an error is uncoded too and is not a mechanical break — and
        # once per digest: the heal restores an older file, and a second
        # identical break after it belongs to the red-build streak below.
        if (_sweep_compile_broke and heal_file is not None
                and deps.parse_ok is not None):
            _uncoded = tuple(
                f for f in uncoded_src_diagnostics(_build.build_output)
                if (Path(target_src_dir) / f).exists()
                and not deps.parse_ok(Path(target_src_dir) / f))
            _uncoded_digest = "\n".join(
                compile_error_digest(_build.build_output)[1])
            if _uncoded and _uncoded_digest not in _healed_digests:
                _healed_digests.add(_uncoded_digest)
                if _heal_unparseable("in the sweep's diagnostics", iter_n,
                                     files=tuple(_uncoded)):
                    # Nothing charges on this sweep: it measured a
                    # mechanical break, not the bodies. The next
                    # iteration sweeps the healed tree.
                    continue
        # Reference-floor close: a FAIL sweep whose remaining failures are
        # all in the original crate's failed-or-ignored set is the original's
        # leftover, not a reconstruction hole. Task3 already promotes that
        # to PASS; t2a used to require rc==0, so the same leftover kept the
        # cluster open. A generated test the reference never had stays
        # unexcused. A red build and a short oracle cannot take this path.
        if (deps.reference_measurement is not None
                and rc != 0 and fail_count > 0
                and not _sweep_compile_broke and _oracle.intact):
            _refm = deps.reference_measurement(project_dir)
            _floor_verdict = judge_cluster_binaries(
                _refm if isinstance(_refm, ReferenceMeasurement) else None,
                cluster.name, parse_binary_results(output))
            _floor_why = reference_floor_close_reason(
                rc=rc, fail_count=fail_count,
                compile_broke=_sweep_compile_broke,
                oracle_intact=_oracle.intact,
                verdict=_floor_verdict)
            if _floor_why:
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: the {fail_count} "
                    "remaining failure(s) are failures of the ORIGINAL crate "
                    "on this machine, not of the reconstruction — floor met "
                    f"({_floor_why}). Cluster done.")
                deps.emit("phase_c.tdd.iter", data={
                    **_prov,
                    "cluster": cluster.name, "iter": iter_n, "rc": rc,
                    "frontier_size": 0, "kind": "reference_floor",
                    "pass_count": pass_count, "fail_count": fail_count,
                })
                deps.emit("phase_c.tdd.floor_met", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "reason": _floor_why,
                    "fail_count": fail_count,
                })
                _cluster_done = True
                _close_kind = "reference floor"
                break
        # Red-build budget: identical compile digest across three consecutive
        # sweeps that fills were attempted between means the fills moved
        # nothing about why the cluster cannot compile — the failure is
        # attempt-invariant, so it is RED now, not after the stall budget
        # finishes paying for it. A sweep no fill preceded is not a reading
        # of the fills: it keeps the digest and not the count, so the
        # refusal cannot fire on passes that dispatched nothing.
        if _sweep_compile_broke:
            _err_count, _err_lines = compile_error_digest(_build.build_output)
            _digest_now = "\n".join(_err_lines)
            if not _digest_now or _digest_now != _red_digest:
                _red_streak = 1
            elif _dispatched_before_sweep > 0:
                _red_streak += 1
            _red_digest = _digest_now
            if _red_streak >= 3:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: RED — the "
                         f"cluster cannot compile and the {_err_count} "
                         "error(s) are IDENTICAL across 3 consecutive "
                         "sweeps separated by real fill attempts; those "
                         "fills changed nothing about them. Stopping this "
                         "cluster instead of spending the stall budget on "
                         "0-test sweeps. "
                         f"First error: {_err_lines[0] if _err_lines else '?'}")
                deps.emit("phase_c.tdd.red_build_refusal", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "error_count": _err_count,
                    "digest": _err_lines[:10]})
                deps.emit("phase_c.verdict", data={
                    "unit": f"cluster:{cluster.name}", "route": "tdd",
                    "attempt": iter_n, "disposition": "exhaustion",
                    "outcome": "unmeasurable",
                    "reason": "red-build refusal — identical compile digest "
                              "across 3 sweeps",
                    "evidence_keys": _err_lines[:10],
                    "cluster": cluster.name})
                break
        else:
            _red_digest, _red_streak = "", 0
        _dispatched_before_sweep = 0
        # Read once here, not at the dispatch site further down: the plateau
        # check below needs it, and nothing between the two mutates the tree
        # (the ratchet's re-sweeps and every splice are outside this span).
        _stubs_now = deps.scan_unimplemented_for_cluster(target_src_dir, cluster)
        _stub_list = _stubs_now.unit_list()
        _stub_keys = _stubs_now.units
        # Release the disk convictions whose blockers have filled, HERE — the
        # authoritative stub set exists at this point and nowhere else in the
        # iteration, and releasing before the frontier is built makes a unit
        # dispatchable the same sweep its producer filled rather than the next.
        # Independent of the score-gated re-admission above on purpose: a
        # producer filling IS the new evidence, and for the units that hold a
        # cluster's whole test set it is the only evidence that can arrive.
        if deps.retire_unblocked_convictions is not None:
            _by_file: dict[str, set[str]] = {}
            for _loc in _stub_list:
                _by_file.setdefault(str(_loc.file), set()).add(_loc.fn_name)
            # The cfg-dead census rides along: a blocker this cluster never
            # compiles is an empty blocker (`blocked_on: ["provide"]` under
            # `stable`), and a non-empty census is the proof-of-scan that lets
            # an empty map mean "finished tree" rather than "failed scan".
            _cfg_dead_now: frozenset[tuple[str, str]] = frozenset()
            if deps.cfg_dead_stub_names is not None:
                with suppress(Exception):
                    _cfg_dead_now = frozenset(
                        deps.cfg_dead_stub_names(target_src_dir, cluster))
            deps.retire_unblocked_convictions(
                project_dir,
                {f: frozenset(names) for f, names in _by_file.items()},
                cluster.name, cfg_dead=_cfg_dead_now)
        # Stage A — adopt the floor a previous PROCESS left behind, once this
        # sweep has produced the two halves of the key it is guarded on. It
        # cannot happen at loop entry: `tree_sha` and `oracle_size` are results
        # of a measurement, not inputs to one. Runs on the first sweep only.
        if not _floor_resolved and not _sweep_compile_broke:
            _floor_resolved = True
            _oracle_size = pass_count + fail_count
            _resume = resume_floor(
                _saved_floor, cluster=cluster.name, tree_sha=_tree_sha,
                oracle_size=_oracle_size, code_id=_code_id)
            _code_moved = bool(
                _saved_floor is not None
                and _saved_floor.code_id != _code_id)
            if _resume.stall is not None:
                stall = _resume.stall
                # LoopState is the source of truth the mirrors read back
                # from, so the streak has to be restored THERE too.
                state.stalls = stall
            if _resume.best_pass is not None:
                best_pass = _resume.best_pass
                state.best_metric = float(best_pass)
            # The third half: the open pass's cursor. Without it the
            # flat-score guard's stop is a LOCK — it can stop the run having
            # attempted 12 units of 59, and a relaunch with an empty cursor
            # re-sends the same deterministic first 12 and stops again. With
            # it, each relaunch takes the next slice. Malformed entries are
            # dropped rather than failing the resume: this runs at the top of
            # a 10-hour loop and a bad state file must cost a fresh pass, not
            # the run (the same rule `TddFloor.from_dict` is total for).
            if _resume.attempted:
                _pass_attempted = {
                    FnLocator(_f, _fn) for _f, _, _fn
                    in (k.partition("::") for k in _resume.attempted) if _fn}
                # A non-empty cursor IS a mid-pass statement: the previous
                # process stopped between batches of one traversal. Two
                # process-local defaults contradicted that record and together
                # closed a cluster one batch short of its first complete
                # escalated pass (runs `3455951-a98b2182`/`3516315-d13ac998`/
                # `3579062-476abc3b`, 2026-08-18: cursor walked 12 -> 24 -> 36
                # of 43, then the plateau exit fired at the NEXT relaunch's
                # iter 1 printing "0 of 43 ... dispatched in the stalled
                # window" while the floor read 36/43):
                #   - `_prev_pass_completed` initialises True, so the resumed
                #     flat sweep charged the stall budget even though the
                #     fills the question is about (the rest of the cohort)
                #     had not been dispatched yet. Seed it False; the
                #     mid-pass exemption below then restores the streak, and
                #     the budget counts completed traversals across processes
                #     exactly as it does within one. The exemption dies with
                #     the cursor: a completed or abandoned pass persists an
                #     empty `attempted`, which does not reach this branch.
                #   - `_dispatched_window` starts empty, so a stop firing
                #     before this process's first fill pass asserted that
                #     nothing was dispatched in the stalled window — an
                #     untruth about the record. The cursor's units were
                #     dispatched inside this same traversal, by the processes
                #     the one-batch-per-relaunch crawl spreads it over, and
                #     every sweep since judged them flat (an improvement
                #     clears cursor and streak together) — so the stall
                #     exit's accounting unions them in. A separate term
                #     rather than a ring entry: the ring is trimmed to the
                #     last `max_stalls` FILL PASSES and would silently drop
                #     the record it exists to report.
                _prev_pass_completed = False
                _resumed_cursor_window = frozenset(_pass_attempted)
                # The cursor's landed-change record rides with it: which of
                # the resumed units actually took a semantic body change.
                # Without it, a pass spread across the one-batch-per-relaunch
                # crawl closes with only its LAST batch's landed set, and the
                # pass audit under-reports the very coverage it exists to
                # state.
                if _resume.landed:
                    _pass_landed = {
                        FnLocator(_f, _fn) for _f, _, _fn
                        in (k.partition("::") for k in _resume.landed) if _fn}
                # The decline record rides with it for the same reason, and
                # for one more: it is half of what `cursor_carry` reads at
                # the NEXT relaunch to decide which units a code move
                # releases for retry. Dropping it here would make every
                # declined unit look like a failed try one relaunch later.
                if _resume.declined:
                    _pass_declined = {
                        FnLocator(_f, _fn) for _f, _, _fn
                        in (k.partition("::") for k in _resume.declined) if _fn}
            # Units the code move released from the cursor: dispatched by the
            # previous process, landed nothing, and therefore worth offering
            # again now that the fill/route/prompt path has changed under
            # them. Nothing to do — they are simply absent from the resumed
            # cursor — but the count is the whole visible effect of the carry
            # rule and a silent release reads as a cursor that shrank.
            if _resume.released:
                deps.log(
                    f"  {tag} [phase-c/tdd] pass cursor: released "
                    f"{len(_resume.released)} attempted unit(s) for retry — "
                    "the code identity moved and their last attempt landed "
                    "nothing: "
                    + ", ".join(sorted(_resume.released)[:8])
                    + (f" (+{len(_resume.released) - 8} more)"
                       if len(_resume.released) > 8 else ""))
                deps.emit("phase_c.tdd.cursor_released", data={
                    "cluster": cluster.name,
                    "released": sorted(_resume.released),
                    "carried": sorted(_resume.attempted or ()),
                })
            # The FOURTH half: the futility evidence. Without it the
            # escalation needs two judged sweeps of THIS process to detect an
            # unchanged unit-set, and the flat-score guard stops a resumed
            # flat run at exactly its second sweep — so the escalated
            # producer slice was computed on the sweep the stop pre-empted,
            # in every process, forever (run `2773540-e47c3754`). Seeded, the
            # first sweep's own attribution is the second observation, and
            # the escalation can fire before the one batch this process gets.
            # The service record, seeded BEFORE the futility evidence so the
            # first sweep's fire is checked against what this cluster really
            # dispatched, not against the evidence's own say-so: the floor
            # `263474-b0e608d3` wrote carried `test_altdebug: [src/fmt.rs::
            # debug]` from a fire the pre-service rule manufactured on a unit
            # it never dispatched. A floor from before the field attests only
            # its open cursor.
            _served_seed = _resume.served
            if _served_seed is None:
                _served_seed = tuple(
                    set(_resume.attempted or ()) | set(_resume.landed or ())
                    | set(_resume.declined or ()))
            for _sv_key in _served_seed:
                _sv_file, _, _sv_fn = _sv_key.partition("::")
                if _sv_fn:
                    _served_at.setdefault(FnLocator(_sv_file, _sv_fn), 0)
            # The verified subset seeds ONLY from its own field — a floor
            # from before the field starts the ranking's view empty rather
            # than adopting stamps the batch-wide rule may have written.
            for _sv_key in (_resume.served_verified or ()):
                _sv_file, _, _sv_fn = _sv_key.partition("::")
                if _sv_fn:
                    _served_verified.add(FnLocator(_sv_file, _sv_fn))
            if _resume.futility:
                for _fut_test, _fut_units in _resume.futility:
                    _seeded = frozenset(
                        FnLocator(_f, _fn) for _f, _, _fn
                        in (u.partition("::") for u in _fut_units) if _fn)
                    prev_test_units.setdefault(_fut_test, _seeded)
                    # First observation at 0: the resumed process may fire at
                    # its first sweep — for the units the service record
                    # says were dispatched. The others defer until served.
                    _set_since.setdefault(_fut_test, 0)
            # Products resume only under the same judged-service premise as
            # the fire: every earned unit is in `_served_at`. A kill that
            # never returned a rewrite is not service, and seeding the
            # product on it would inject producers at iter 1 of a rewrite
            # that never landed (the hole that shipping the product while
            # served was still a dispatch stamp would open).
            if _resume.products:
                _served_keys = set(_served_at)
                for _t, _earned_s, _adds_s, _prods_s in _resume.products:
                    _earned = _keys_to_locators(_earned_s)
                    if any(u not in _served_keys for u in _earned):
                        continue
                    _cached: dict[FnLocator, set[str]] = {}
                    for _ak, _ats in _adds_s:
                        _af, _, _afn = _ak.partition("::")
                        if _afn:
                            _cached[FnLocator(_af, _afn)] = set(_ats)
                    escalation_products[_t] = (_earned, _cached)
                    escalation_producers[_t] = _keys_to_locators(_prods_s)
            deps.log(f"  {tag} [phase-c/tdd] persisted floor for "
                     f"{cluster.name}: {_resume.note} "
                     f"(stall now {stall}/{max_stalls}, "
                     f"best_pass {render_pass_floor(best_pass)}; "
                     f"tree {_tree_sha}, {_oracle_size}-test oracle, "
                     f"code {_code_id}).")
        # Improvement metric: did pass count strictly increase since best?
        # A compile-broken sweep is NOT a measurement: `pass_count` is 0 because
        # nothing ran, not because the tree scores 0. Recorded as an
        # improvement it sets the floor to 0 and resets the stall budget, so the
        # cluster reads "improving" on the iteration it is most broken —
        # observed live at `nightly` iter 1 (`pass=0 fail=124 improved=True`).
        improved = pass_count > best_pass and not _sweep_compile_broke
        if improved:
            best_fail = fail_count
        # Hook 3 — detect a causal regression BEFORE recording, so the note
        # rides this iter's Attempt (frozen) and surfaces in context(). A test
        # that flipped pass→fail right after last iter's fill is attributable
        # to that fill; write the concrete WRONG-body note for the model.
        _regr_note = ""
        if _regression_notes and fr_path and last_newly_filled:
            _newly_failed = cur_failed - prev_failed
            if _newly_failed:
                _regr_note = deps.record_tdd_regression(
                    fr_path, src_dir, last_newly_filled, _newly_failed,
                    best_pass, pass_count,
                )
        prev_failed = cur_failed
        # Record this iter's measurement on the shared LoopState (metric is
        # pass_count — "higher is better" here, so NO negation). record()
        # updates the floor + stall streak; mirror them into the scalars the
        # log/emit lines below read.
        _stalls_before_record = state.stalls
        state.record(Attempt(metric=float(pass_count), iteration=state.iteration,
                              improved=improved, note=_regr_note))
        # An improvement reopens the frontier: the traversal in progress was
        # planned against a worse tree, and the units it had already spent are
        # worth re-offering now that the evidence moved.
        #
        # Unless the "improvement" is a sweep beating the UNSET sentinel: that
        # is the ABSENCE of a baseline, not evidence the tree moved. The resume
        # path hands the cursor across an `oracle_size` or `tree_sha` change
        # with `best_pass=None` precisely so a relaunch takes the next slice
        # (`resume_floor`: a cursor answers to cluster+code_id, not to the
        # oracle) — and the first sweep then compared its reading against -1,
        # read "improved", and cleared the cursor it had adopted forty lines
        # up, before any dispatch could take a slice from it. The relaunch
        # re-sent the same deterministic first batch and the eager stop became
        # the lock the cursor exists to remove; the floor save below then
        # persisted the EMPTY set, destroying the coverage on disk too. The
        # oracle moves in 26 of 401 consecutive sweep pairs, so this was the
        # common resume, not the odd one.
        if improved and best_pass > UNSET_PASS:
            _pass_attempted.clear()
            _pass_landed.clear()
            _pass_declined.clear()
            _pass_escalated.clear()
            _pass_opened.clear()
            _pass_reoffered.clear()
            _pass_cohort = None
            _resumed_cursor_window = frozenset()
        # `best_metric` is -inf until some iter improves, and a compile-broken
        # sweep no longer does — so a cluster whose FIRST sweep cannot build
        # keeps the loop's own initial floor instead of overflowing on
        # `int(-inf)`. The floor must not move for such a sweep anyway: it
        # measured nothing.
        if state.best_metric > float("-inf"):
            best_pass = int(state.best_metric)
        _highwater = _highwater.observed(best_pass)
        # The high-water describes a tree the retrigger deleted; THIS sweep is
        # the first measurement of the one it made. Re-base every scalar derived
        # from it, in one place, so no consumer is left reading the old lineage
        # — the defect this replaces was exactly one un-updated consumer.
        # A compile-broken sweep measured nothing and cannot be a baseline —
        # and neither is an unmeasured-restore sweep: its count describes the
        # tree the restore discarded, so re-basing to it would seed the whole
        # re-climb from a number about the wrong lineage (the live instance
        # printed `high-water re-based 34 -> 34` for a tree whose mark was
        # 83/106). `rebase_due` stays pending, so the first sweep that really
        # measures the new tree performs the re-base.
        if (_highwater.rebase_due(iter_n) and not _sweep_compile_broke
                and not _restore_unmeasured):
            _stale = _highwater.best
            _highwater = _highwater.rebased(pass_count)
            best_pass = _highwater.best
            state.best_metric = float(best_pass)
            # The re-admission baseline follows, or the release gate stays shut
            # for the whole re-climb: `readmission_warranted` asks for a score
            # move past this number, and the re-stub is what put the score below
            # it. Left at the pre-re-stub figure it took run `259637-36db9aad`
            # (2026-08-07) from iteration 4 to its death printing `NOT
            # re-admitting exhausted fn(s)` on every sweep. An UNSPENT free
            # grant (the negative sentinel) is left alone — it is not a baseline
            # yet and re-basing it would spend it here for nothing.
            if _pass_at_last_reset >= 0:
                _pass_at_last_reset = best_pass
            # Two producers replace the lineage (`lineage_replaced`'s own
            # docstring): the retrigger's re-stub and the best-tree restore.
            # The message used to name the retrigger unconditionally, so a
            # restore printed `the retrigger re-stubbed 0 body(ies)` (run
            # `2620196-de4a7ca2`, 2026-08-17) — name the producer the state
            # can actually distinguish.
            _cause = (f"the retrigger re-stubbed {_grace.restubbed} body(ies)"
                      if _grace.restubbed
                      else "the tree was replaced (best-tree restore, or a "
                           "re-author that re-stubbed nothing)")
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: high-water re-based "
                     f"{render_pass_floor(_stale)} -> {best_pass} — {_cause}, "
                     f"so {render_pass_floor(_stale)} describes a tree this run "
                     "no longer has. The re-climb is judged from here.")
            deps.emit("phase_c.tdd.highwater_rebased", data={
                "cluster": cluster.name, "iter": iter_n,
                "stale_best": _stale, "new_best": best_pass,
                "restubbed": _grace.restubbed,
            })
        stall = state.stalls
        # FOURTH consumer of "have the last N iterations of fills failed to move
        # the score", and the one the dispatch cap creates. With the frontier
        # sliced, an iteration attempts only part of it, so a non-improving sweep
        # that followed a MID-PASS batch has not yet run the fills the question
        # is about. Charging it would fire the plateau exit (frontier/cap) times
        # sooner in wall-clock than the uncapped loop — converting the sampling
        # defect the cap exists to fix into a premature-exit one. Charge only
        # after a completed traversal; the streak is restored, not zeroed, so a
        # genuine plateau still closes at `max_stalls` completed passes.
        if not improved and not _prev_pass_completed:
            state.stalls = _stalls_before_record
            stall = _stalls_before_record
            deps.emit("phase_c.tdd.stall_not_charged_midpass", data={
                "cluster": cluster.name, "iter": iter_n,
                "pass_count": pass_count, "stall": stall})
        if _grace.bodies_in_flight:
            # THIRD consumer of the re-stub's depressed count, and the one that
            # bites only once the window can outlive the stall budget (16 iters
            # vs 3). The budget asks "have the last N iters of fills failed to
            # move the score"; inside a window the score CANNOT move — the
            # re-stubbed bodies have to land first, which is precisely what the
            # window is already progress-gated on. Left accumulating, the loop
            # takes its stall exit mid-window, and that exit is a bare `break`
            # with no tree restore: the run would end at the re-stub's low point,
            # BELOW the high-water the window exists to protect. Reset exactly as
            # every retrigger site does when it fires. Termination is unaffected:
            # the window is hard-bounded and dies the iter re-fills stop landing.
            # `bodies_in_flight`, not `active`: a fired-but-failed attempt froze
            # nothing, so it buys silence from the crash streak but must not buy
            # a free iter of plateau.
            state.stalls = 0
            stall = 0
        elif (not improved and not _sweep_compile_broke
                and _stubs_prev >= 0 and len(_stub_keys) < _stubs_prev):
            # A cluster early in its climb pays in STUBS and is only paid back
            # in TESTS several iterations later: nothing flips until a whole
            # producer chain is complete, and the loop says so itself — the
            # sweep below this one logs `deferred N consumer(s) blocked by
            # still-stubbed producer(s)`. Charging those iterations to a
            # 3-iteration pass_count budget abandons a cluster that is working.
            # Live, cluster `stable` of run 7445-00ee8a3c on 2026-08-07: iters
            # 1-3 filled 36 then 26 fns, taking the stub count 132 -> 118 -> 82
            # -> 75 while the pass count went 11 -> 13 -> 13. Iter 3 was charged
            # `stall=1/3`, so two more such iterations would have ended task2a
            # for that cluster with 75 `unimplemented!()` bodies still on disk.
            # So a sweep that shrank the stub set is not a plateau — it moved
            # the only metric that CAN move yet. Termination is unaffected and
            # needs no cap: the stub set only shrinks (a retrigger's re-stub
            # raises it, which is not a decrease and charges the stall as
            # before), and part of it never fills at all — 13 of `stable`'s 88
            # are cfg-gated out of this cluster — so the count floors, the
            # decreases stop, and the exit below fires on schedule. A RED sweep
            # is excluded: it dispatches only the fns rustc implicates (1 of 85
            # on `stable` at 2026-08-06T13:19Z), so one body an iteration would
            # shrink the set forever while the diagnostics stand still. A tree
            # that does not build has its own terminators — the compile-blocked
            # exit and the cause-A retrigger — and must not borrow this one.
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: pass count flat but "
                     f"the tree is still filling ({_stubs_prev} -> "
                     f"{len(_stub_keys)} stub(s)) — not charging the plateau "
                     "budget for an iteration whose fills no test can score "
                     "yet.")
            deps.emit("phase_c.tdd.stub_progress", data={
                "cluster": cluster.name, "iter": iter_n,
                "stubs_prev": _stubs_prev, "stubs_now": len(_stub_keys),
                "pass_count": pass_count, "stall_before": stall,
            })
            state.stalls = 0
            stall = 0
        # GPU protector, half 1 of 2 — RECORD here, TRIP at the dispatch site.
        # Split on purpose: this is the only place that holds all three reasons
        # a reading cannot judge the last dispatch (grace window, shrinking stub
        # set, a tree that did not compile), while the decision belongs
        # immediately before the model calls are spent — so an iteration that
        # ends in a retrigger's `continue` still puts its reading in the window
        # but never has the run stopped out from under an escape that had not
        # taken its turn yet. On the recorded run that ordering is worth 6.4 GPU
        # hours: iters 1 and 3 both read 79/101 and both left through a crash
        # retrigger, so the window is already at two by iter 4's dispatch.
        _flat_reading = ScoreReading(pass_count, _measure.oracle_size)
        # THE EXEMPTIONS STARVED THE WINDOW, WHICH IS HOW A GUARD WITH A
        # SATISFIED RULE SPENDS A NIGHT. Two of the three reasons a sweep used
        # to be excused — a re-fill grace window, a shrinking stub set — are
        # true of essentially every iteration of a loop that is doing its job:
        # a fill loop lands bodies, landing bodies shrinks the stub set, so the
        # reading was never RECORDED and `repeated()` could never reach 2. On
        # run `1920662-40f711b2` that is the whole mechanism of the burn:
        # `83/106` at iterations 2, 3 and 4, then a grace window opened at
        # iteration 4 for 18 more iterations, and `33/105` at 6 and 7. Nine
        # hours 48 minutes, 191 model calls, and the window held almost
        # nothing.
        #
        # So the rule is now the operator's rule: the same reading twice stops
        # the run, and no argument about WHY the score is standing still buys
        # another sweep. A grace window explains a score FALLING — it does not
        # explain the same number arriving twice, because a re-climb that reads
        # 33/105 at two sweeps is a re-climb that has stopped climbing. A
        # shrinking stub set explains a score standing still while the tree
        # pays in stubs — it does not explain it twice.
        #
        # WHAT THIS COSTS, STATED RATHER THAN HIDDEN. Cluster `stable` of run
        # `7445-00ee8a3c` (2026-08-07) read 11 -> 13 -> 13 while its stub set
        # fell 132 -> 118 -> 82 -> 75, and iteration 3's file-first batch then
        # took the score 13 -> 83 in ONE sweep. Under this rule that run stops
        # on the second 13, one sweep before its payoff. The reason that is now
        # an acceptable price and was not before is `TddFloor.attempted`: the
        # stop leaves a pass cursor on disk, so `bash continue.sh` resumes on
        # the units the stopped run never reached rather than re-sending the
        # ones it did. A false stop costs one relaunch, not a run — which is
        # exactly what this file's own header used to deny.
        #
        # A tree that did not compile stays excused, and the unmeasured-restore
        # tripwire joins it — both are the ABSENCE of a measurement rather than
        # a measurement of nothing: the first ran no tests at all, the second
        # ran the DISCARDED tree's binaries against the restored sources (the
        # red-build budget owns the first case; the mtime bump in
        # `restore_cluster_best_tree` owns the second, this excusal is what
        # keeps any recurrence from spending the flat window on it). Nothing
        # else is excused.
        _flat_excuse = (
            "the sweep did not compile — a 0 nothing ran for is not a "
            "measurement" if _sweep_compile_broke else _restore_unmeasured)
        # The sweep, as a FACT. `judged=False` is the old `excused()`: a
        # sweep that cannot judge the dispatch before it is not evidence in
        # either direction, and the fold counts it as a gap rather than as a
        # reading.
        _flat_facts.append(SweepMeasured(
            iter=iter_n, cluster=cluster.name, pass_count=pass_count,
            fail_count=fail_count, oracle_size=_measure.oracle_size,
            # `kind` is not settled this early in the iteration (the frontier
            # has not been drawn yet) and the fold does not read it — the
            # judged/excused split is what the flat-score rule is made of.
            intact=_rec.reconciled, judged=not _flat_excuse))
        _stubs_prev = len(_stub_keys)

        # The re-admission baseline starts at a sentinel any score beats, so
        # the FIRST re-admission of a process is free. That free grant is the
        # deadlock breaker and must not simply be removed: exhaustion flags
        # persist on disk, so a unit that is itself the blocker could never be
        # re-admitted — un-exhausting it needs a score move, and the score
        # cannot move while it stays exhausted.
        #
        # But "once per PROCESS" is not the right unit. This pipeline restarts
        # on death every few hours for reasons that carry no new evidence, and
        # each restart re-inflated the frontier: live, iter 2 of run 589477
        # re-admitted 7 units off an iter-1 fill phase that `pass_delta`
        # scored `by pair: +0 -0`, taking the frontier 40 -> 73. The unit that
        # earns a free retry is NEW CODE — the same reason `TddFloor` guards
        # on `code_id`. Same code as the process that set these flags means
        # the convictions still stand and a retry must be bought with a score
        # move like any other.
        if (_pass_at_last_reset < 0 and best_pass >= 0
                and _saved_floor is not None
                and _saved_floor.code_id == _code_id):
            _pass_at_last_reset = best_pass

        # One fresh, informed retry per iteration boundary for fns/blocks
        # marked fully_exhausted. TWO stores hold exhaustion: the
        # disk-persisted flags in the retrospective cache (checked inside the
        # fill engines) and this loop's own `permanently_exhausted` +
        # `refill_attempts` (the frontier's eligibility filter). A release
        # must open BOTH: run `2219473` (2026-08-10, stable iter 3) printed
        # "re-admitted 11 previously-exhausted fn(s)/block(s)" and dispatched
        # ZERO fills, because all 15 frontier candidates sat refill-capped in
        # the in-process set the disk reset never reached — the loop's only
        # self-recovery valve, spent on a store nobody consults.
        # Gated on real progress since the LAST reset (not blind per-iter);
        # "progress" is PAYOFF, not activity — the rule and its evidence live
        # in `refine.readmission_warranted`. Decided HERE — after this
        # iteration's sweep and after the high-water's destruction re-base
        # above — because the epoch re-base inside adopts `best_pass`, and
        # before those two steps that number can still describe a tree a
        # retrigger deleted (`readmission_boundary`'s docstring holds the
        # measured instance). The release still lands before this iteration's
        # frontier is built, so re-admitted units dispatch this iteration.
        _new_fills = total_filled - _filled_at_last_reset
        if iter_n > 1:
            _boundary = readmission_boundary(
                best_pass=best_pass,
                baseline=EpochedBaseline(_pass_at_last_reset, _reset_epoch),
                destruction_epoch=_destruction_epoch,
                new_fills=_new_fills,
                sweep_measured=not _sweep_compile_broke,
                sweep_reconciled=_rec.reconciled)
            if _boundary.rebased_from is not None:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-admission "
                         f"baseline {_boundary.rebased_from} predates a "
                         "deliberate destruction — re-based to "
                         f"{_boundary.baseline.value}.")
            if _boundary.decision is BoundaryDecision.DEFER_UNMEASURED:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-admission "
                         "baseline predates a deliberate destruction and this "
                         "sweep measured nothing (compile-broken) — decision "
                         "deferred to the next measured sweep.")
            elif _boundary.decision is BoundaryDecision.DEFER_TAINTED:
                # A tainted score cannot refuse a re-admission: the refusal
                # line would name a number the sweep itself disclaimed. Defer
                # the decision; the watermark and the fill counter stay put.
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: this sweep's "
                         "score was tainted (counter/verdict mismatch) — "
                         "re-admission decision deferred, not refused.")
            elif _boundary.decision is BoundaryDecision.RELEASE:
                _released = deps.reset_exhaustion_flags(
                    project_dir, f"TDD iter {iter_n} boundary — {_new_fills} "
                                 f"new fill(s) and pass {_pass_at_last_reset} "
                                 f"-> {best_pass} since the last check")
                _filled_at_last_reset = total_filled
                # Open the loop-local gates for the released units too —
                # container-blind on purpose (FnLocator has no container),
                # which re-admits strictly more than the disk release, the
                # safe direction.
                _rel_locs = {
                    FnLocator(_parts[0], _parts[1])
                    for _parts in (str(_k).split("::") for _k in _released)
                    if len(_parts) >= 2}
                _reopened = _rel_locs & (permanently_exhausted
                                         | set(refill_attempts))
                permanently_exhausted -= _rel_locs
                for _loc in _rel_locs:
                    refill_attempts.pop(_loc, None)
                if _released:
                    deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-admitted "
                             f"{len(_released)} previously-exhausted "
                             "fn(s)/block(s) for one more informed attempt"
                             + (f" ({len(_reopened)} also reopened in the "
                                "loop-local frontier filter)."
                                if _reopened else "."))
            elif _boundary.decision is BoundaryDecision.REFUSE:
                # Say WHY the budget was withheld: silence here reads as
                # "nothing was exhausted", which is a different fact.
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: {_new_fills} "
                         "fill(s) since the last check moved pass no further "
                         f"than {_boundary.baseline.value} — NOT re-admitting "
                         "exhausted fn(s); a retry with no new evidence is "
                         "the same attempt again.")
                _filled_at_last_reset = total_filled
            _pass_at_last_reset = _boundary.baseline.value
            _reset_epoch = _boundary.baseline.epoch

        # Stage A — persist the floor AFTER every sweep, not only at the exit:
        # the whole point is that the process may not survive to an exit. A
        # compile-broken sweep measured nothing, so it must not overwrite a
        # real floor with its own zero.
        # A tainted sweep cannot set a floor either — same rule as the
        # checkpoint and the ratchet above.
        # The futility evidence saved here is the PREVIOUS sweep's
        # attribution (this save runs before this sweep's): the stopped run's
        # last boundary save carries the unit-sets its final sweep compared
        # equal, and the resume's first sweep is the second observation. A
        # process that resumed the evidence re-saves it unchanged here, so a
        # relaunch can no longer erase the seed at its first boundary.
        _floor_persistable = (deps.save_tdd_floor is not None
                              and not _sweep_compile_broke
                              and _rec.reconciled)
        _persist_floor()

        # Attribute failures via the full chain (panics → compile → behavior →
        # classifier → recently_filled → reverse_map). The cluster's in-memory
        # test pairs feed the deterministic behavior path (s117 stable-51/72).
        _attr = deps.attribute_failures(
            output, src_dir, recently_filled,
            test_reverse_map, cluster.name,
            test_texts=dict(cluster_tests),
            extra_failed=frozenset(_oracle.vanished),
        )
        frontier, kind = _attr.frontier, _attr.kind
        frontier = deps.filter_test_targets(frontier, src_dir)
        # Only the direct-evidence subset may revert a fill. Everything added
        # to `frontier` BELOW this line (crash-localized blame, futility
        # escalation, unattributed stubs) widens the work list without
        # convicting anyone: those units get attempted and are judged on
        # compiling + not regressing the suite, never on a failing test they
        # have no observed link to.
        convicting = deps.filter_test_targets(_attr.convicting, src_dir)

        # Crash localization: the vanished-test oracle names WHICH tests died;
        # the localizer names WHERE (gdb fast path, ASAN fallback with origin
        # frames). Blamed fns enter the frontier directly — stack evidence
        # outranks reverse-map attribution — their fill prompts receive the
        # rendered CRASH_EVIDENCE, and crash evidence voids prior exhaustion
        # verdicts (a localized crash is new information the earlier attempts
        # never saw).
        crash_texts: dict[FnLocator, str] = {}
        # The ORIGIN blame alone, beside the evidence dict: `crash_texts` keys
        # by EVERY frame (access and origin), so the innocent caller that
        # routes execution into the fault is a key too — any "is the blame
        # outside this unit" test needs the blamed locators, not the keys.
        crash_blamed: frozenset[FnLocator] = frozenset()
        _sites: tuple[Any, ...] = ()
        # Sites where an origin-capable detector looked and placed no frame
        # in-crate — the precondition for chain-derived blame synthesis at
        # the determinism gate below. Reset here: a stale list surviving a
        # stub-confounded (localization-deferred) iteration would synthesize
        # blame from a sweep the current tree never produced.
        _unattributed_sites: list[Any] = []
        # Gated on NAMED vanished tests, never the bare missing count: an
        # unnamed shortfall is an inventory gap, and sending gdb + an ASAN
        # rebuild after it burns a sweep to conclude "no native fault".
        # A test whose own isolated process died WITHOUT printing a panic
        # is a native fault by the capture's own evidence (a stub's abort
        # prints the stub's panic in that test's block), so it is localized
        # with no stub-confounding deferral: that deferral guesses from a
        # vanished set, and an isolated capture has nothing to guess.
        _silent_aborts = TestStateReading(
            parse_binary_results(output)).aborted_silent_names
        if ((_oracle.vanished or _silent_aborts)
                and deps.localize_crashes is not None):
            # A second gate on the SAME expensive path: while the fns the
            # vanished tests reach are still >=half stubs, a stub panic (not a
            # native fault) is the likely cause — gdb + an ASAN rebuild would
            # only conclude "no fault". Defer until fills land; the frontier
            # already carries the vanished tests via extra_failed above.
            if not _silent_aborts and vanish_is_stub_confounded(
                    _oracle.vanished, test_reverse_map,
                    deps.scan_unimplemented_functions(src_dir).units):
                deps.log(f"  {tag} [phase-c/crash] {len(_oracle.vanished)} "
                         "test(s) vanished — deferring localization (>=half "
                         "the fns they reach are still stubs; stub panics "
                         "abort binaries too), fills run first.")
            else:
                _sites = tuple(deps.localize_crashes(
                    output, test_cmd, target_src_dir, cluster_env))
                crash_texts = crash_evidence_by_locator(_sites)
                crash_blamed = blamed_locators(_sites)
                _unattributed_sites = [
                    s for s in _sites if s.origin_unattributed]
                for _site in _sites:
                    _blame = (str(_site.blamed_locator)
                              if _site.blamed_locator is not None
                              else "NOBODY (origin not attributed in-crate)")
                    deps.log(f"  {tag} [phase-c/crash] {_site.binary_label}"
                             f"::{_site.test_name or '?'} {_site.signal} — "
                             f"blamed {_blame} "
                             f"(provenance={_site.provenance})")
                    # Access and origin stacks stay SEPARATE on the event: the
                    # old single `frames` list was access+origin concatenated,
                    # so "origin is empty" was only inferrable by counting, and
                    # the detector's own text — the sole record of who freed
                    # the memory — was parsed and thrown away.
                    deps.emit("phase_c.crash.localized", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "binary": _site.binary_label, "signal": _site.signal,
                        "test": _site.test_name,
                        "frames": [[f.file_rel, f.line, f.fn_name]
                                   for f in _site.frames],
                        "origin_frames": [[f.file_rel, f.line, f.fn_name]
                                          for f in _site.origin_frames],
                        "blamed": (None if _site.blamed is None else
                                   [_site.blamed.file_rel, _site.blamed.fn_name]),
                        "origin_unattributed": _site.origin_unattributed,
                        "provenance": _site.provenance,
                        "raw_report": _site.raw_report})
                if not _sites:
                    # Leave the raw symptoms behind — "no native fault" with
                    # no record of what the binary died of is uncheckable.
                    _hint = crash_signal_hint(output)
                    deps.log(f"  {tag} [phase-c/crash] {len(_oracle.vanished)} "
                             "test(s) vanished but no crash could be localized "
                             "(no native fault under gdb, no ASAN report)."
                             + (f" Sweep symptoms: {_hint}" if _hint else ""))
                    deps.emit("phase_c.crash.unlocalized", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "vanished": sorted(_oracle.vanished)[:50],
                        "signal_hint": _hint,
                        "vanish_streak": _vanish_streak})
                if not any(s.all_frames for s in _sites):
                    # Deterministic (second consecutive shrunken sweep) with
                    # EMPTY localization: without this branch the crash
                    # produces no evidence object anywhere — crash-keep,
                    # crash-block and the evidence prompt all key on
                    # localizer output, so an unlocalizable crasher starves
                    # every route at once (run `2219473`: the whole Error/
                    # vtable class sat behind exactly this hole). Attribute
                    # the crash to the vanished tests' SUBJECT fns — module
                    # file + test name, never the whole reverse map — as
                    # EVIDENCE (prompt + exhaustion void + keep), not blame.
                    # Keyed on "no site carries a frame", not "no sites": an
                    # origin-unattributed site with zero in-crate frames now
                    # survives the localizer (it feeds the chain-derived
                    # synthesis below), and it must not silence this weaker
                    # module-anchored channel that fired before it existed.
                    _hint = crash_signal_hint(output)
                    if _vanish_streak >= 1:
                        for _loc, _tsts in vanished_subject_fns(
                                frozenset(_oracle.vanished),
                                test_reverse_map,
                                test_files=_test_files).items():
                            _synth = (
                                f"Test(s) {', '.join(sorted(_tsts))} VANISH "
                                "from the sweep (their binary crashes with "
                                "no verdict) on consecutive sweeps, and "
                                "native localization finds nothing — no gdb "
                                "fault, no ASAN report"
                                + (f"; sweep symptoms: {_hint}" if _hint
                                   else "")
                                + ". This fn is the vanished test's subject. "
                                "Treat the crash as evidence about this "
                                "body: re-derive it from the types, paying "
                                "attention to memory the body allocates, "
                                "casts or frees.")
                            crash_texts.setdefault(_loc, _synth)
                        if crash_texts:
                            deps.log(
                                f"  {tag} [phase-c/crash] localizer empty but "
                                "the shrink is deterministic — synthesized "
                                "crash evidence for subject fn(s): "
                                + ", ".join(
                                    f"{k.file}::{k.fn_name}"
                                    for k in sorted(crash_texts)))
                # PRODUCER AIM, on the route where the localizer DID blame
                # somebody. A frame is where execution faulted; for every fault
                # whose bad value was made elsewhere, the fn to change is one
                # call away and appears in no stack. The narrowing shipped for
                # the blame-NOBODY route does not reach here, and the gap has a
                # measured cost: run `1408670-3c074ece` (2026-08-16) blamed
                # `src/error.rs::downcast_ref` on all five sweeps —
                # frame-derived and correct as a frame — while the fault was
                # `object_downcast` returning `Some(Ref::from_raw(NonNull::
                # dangling()))`, which `downcast_ref` calls on the line above
                # the one that faulted. Six hours, 101 fills, 20 dispatches of
                # the impl block holding the blamed fn, zero verdict changes,
                # and `object_downcast` never dispatched once.
                #
                # EVIDENCE ONLY. These locators get a prompt, a frontier entry
                # and an exhaustion void — the same channels `crash_texts`
                # already carries — and nothing else: `crash_blamed` is left
                # alone, so nothing here can convict, crash-block or revert on
                # an inference. `setdefault` for the same reason: a real frame's
                # evidence outranks a producer guess about the same fn.
                if crash_blamed and deps.enumerate_fns is not None:
                    _recs = deps.enumerate_fns(target_src_dir) or []
                    _placeholder = read_placeholder_pointer_fault(output)
                    _producers: set[FnLocator] = set()
                    for _b in sorted(crash_blamed):
                        _producers |= callee_subjects(_b, _recs)
                    if _placeholder is not None:
                        # The reading names the fault as a fabricated pointer,
                        # so the fns that fabricate one are subjects whether or
                        # not the blamed fn calls them — the sentinel may be
                        # handed through any number of hops.
                        _producers |= placeholder_pointer_subjects(_recs)
                    _producers -= set(crash_texts)
                    _blame_txt = ", ".join(f"{b.file}::{b.fn_name}"
                                           for b in sorted(crash_blamed))
                    for _loc in _producers:
                        crash_texts[_loc] = "\n\n".join(filter(None, (
                            f"A crash was localized to {_blame_txt}, and this "
                            "fn is named as a PRODUCER CANDIDATE for it — it "
                            "is called from the faulting fn, or it hands out a "
                            "pointer of the kind that faulted. It is NOT "
                            "blamed by any stack frame. The faulting frame "
                            "READS a value; whatever fabricated that value is "
                            "what has to change, and it appears in no "
                            "backtrace. Re-derive this body from its types and "
                            "its contract, and check what it returns against "
                            "what its caller does with the result.",
                            _placeholder.render() if _placeholder else "")))
                    if _producers:
                        deps.log(
                            f"  {tag} [phase-c/crash] iter {iter_n}: blame is "
                            f"frame-derived ({_blame_txt}) — the faulting frame "
                            "reads, it does not fabricate. Aimed "
                            f"{len(_producers)} producer candidate(s) as "
                            "evidence (never blame): "
                            + ", ".join(f"{k.file}::{k.fn_name}"
                                        for k in sorted(_producers))
                            + (f" | {_placeholder.render().split(':')[0]} read "
                               f"at {_placeholder.deref_site}"
                               if _placeholder else ""))
                        deps.emit("phase_c.crash.producer_aimed", data={
                            "cluster": cluster.name, "iter": iter_n,
                            "blamed": sorted(f"{b.file}::{b.fn_name}"
                                             for b in crash_blamed),
                            "placeholder_pointer": (
                                None if _placeholder is None else {
                                    "address": _placeholder.address,
                                    "required_align":
                                        _placeholder.required_align,
                                    "deref_site": _placeholder.deref_site}),
                            "subjects": sorted(f"{k.file}::{k.fn_name}"
                                               for k in _producers)})
                for _loc in crash_texts:
                    frontier.setdefault(_loc, set()).update(_oracle.vanished)

        # A STUB THAT HOLDS A FAILING TEST NAMES ITSELF. A failing test whose
        # first failure is inside a still-stubbed fn cannot move until that
        # fn has a body, so the fn is the strongest need the sweep can state
        # (`test_outcome.StubHold`). Read per TEST from the typed outcomes,
        # not per aborted binary: isolation gives every aborted test its own
        # verdict and closes every section, and the binary-level reading this
        # replaces went silent on run `3453127-045b05b1` (2026-09-19) while
        # 82 of 141 failing tests stopped inside one stub that the batch cut
        # for two iterations. The shared-process reading remains inside
        # `stub_hold_of` for a binary isolation could not rebuild.
        # Independent of the localizer on purpose — it defers on a
        # stub-heavy tree and finds no native fault in a stub panic.
        # EVIDENCE, never blame: the stubs get a prompt, a frontier entry, an
        # exhaustion void and the crash cohort's site rank; `crash_blamed`
        # stays the localizer's, so nothing here convicts or reverts a body.
        _stub_hold = StubHold({})
        if not _sweep_compile_broke and deps.enumerate_fns is not None:
            _hollow_now = frozenset(_stub_keys)

            def _is_stub(s_: FnSite, _k: frozenset[FnLocator] = _hollow_now,
                         ) -> bool:
                return locator_of(s_) in _k

            _stub_hold = stub_hold_of(
                output,
                make_fn_site_locator(
                    SimpleNamespace(enumerate_fns=deps.enumerate_fns),
                    target_src_dir),
                is_stub=_is_stub,
                vanished=_oracle.vanished, test_files=_test_files)
        if _stub_hold.by_test:
            for _site in sorted(_stub_hold.blockers):
                _held_tests = _stub_hold.held_by(_site)
                _loc = locator_of(_site)
                crash_texts.setdefault(_loc, (
                    f"This fn is still a stub, and {len(_held_tests)} failing "
                    "test(s) stop at its `unimplemented!()` before they check "
                    "anything else. No other body can move those tests until "
                    "this one exists. Give it a real body."))
                frontier.setdefault(_loc, set()).update(_held_tests)
            deps.log(
                f"  {tag} [phase-c/crash] iter {iter_n}: "
                f"{len(_stub_hold.blockers)} still-stubbed fn(s) hold "
                f"{len(_stub_hold.by_test)} failing test(s) at their first "
                "failure, ranked as crash-site evidence: "
                + ", ".join(
                    f"{s.file}::{s.fn_name} ({len(_stub_hold.held_by(s))})"
                    for s in sorted(_stub_hold.blockers)))
            deps.emit("phase_c.crash.stub_holds", data={
                "cluster": cluster.name, "iter": iter_n,
                "held": {f"{s.file}::{s.fn_name}":
                         sorted(_stub_hold.held_by(s))[:50]
                         for s in sorted(_stub_hold.blockers)},
                "held_tests": len(_stub_hold.by_test)})

        # Futility escalation: a failing test whose attributed
        # unit-set is identical to last iteration's has been fully served by
        # rewrites that changed nothing — its accessors are the wrong target.
        # Escalate to the producers of the exercised types; a fired
        # escalation's additions are then re-merged every sweep for as long
        # as the evidence that earned the fire still holds.
        if deps.escalate_to_producers is not None:
            units_by_test: dict[str, set[FnLocator]] = {}
            for key, tests in frontier.items():
                for tname in tests:
                    units_by_test.setdefault(tname, set()).add(key)
            # Accessors a FIRED escalation declared futile this sweep, and the
            # tests it fired for — fed to the guidance suspension below.
            _futile_fired: set[FnLocator] = set()
            _futile_fired_tests: set[str] = set()
            # Already-fired products first: re-merge each one whose evidence
            # still stands (the product's lifetime IS the evidence's — see
            # `domain/escalation_lifetime`), drop the rest. A sustain is not
            # new evidence: no guidance suspension, no exhaustion
            # re-admission, one summary line for all of them; an escalated
            # unit that exhausts its refill budget leaves through the
            # exhaustion re-entry filter exactly as any frontier unit does.
            _sustain = sustain_escalation_products(
                escalation_products, units_by_test, _stub_keys,
                format_tests=frozenset(
                    t for t in escalation_products
                    if format_traits_used(_attr.source_for(t))))
            for k, ts in _sustain.merges.items():
                frontier.setdefault(k, set()).update(ts)
            for tname, _why in _sustain.dropped:
                del escalation_products[tname]
                escalation_producers.pop(tname, None)
                deps.log(
                    f"  {tag} [attribution/escalate] {tname}: escalation "
                    "product dropped — "
                    + {"stub_accessor":
                        "an attributed unit is a stub again (never served "
                        "by any rewrite, so 'futile' no longer describes "
                        "it)",
                       "not_failing":
                        "the test no longer fails, so there is no "
                        "attribution to hold evidence against",
                       "format_trait_test":
                        "the test's own source renders through a format "
                        "trait, so producers of the accessors' container "
                        "cannot change what it asserts — the "
                        "format-implementer attribution owns this evidence"
                       }[_why]
                    + "; a product outliving its evidence would be frontier "
                    "nobody measured.")
                deps.emit("phase_c.attribution.escalation_dropped",
                          data={"test": tname, "reason": _why})
            if _sustain.sustained:
                deps.log(
                    f"  {tag} [attribution/escalate] "
                    f"{len(_sustain.merges)} escalated addition(s) for "
                    f"{len(_sustain.sustained)} fired test(s) re-merged — "
                    "evidence unchanged since the fire, so the product "
                    "stays in the frontier (and in the pass denominator) "
                    "as long as the evidence stands.")
                deps.emit("phase_c.attribution.escalation_sustained", data={
                    "tests": list(_sustain.sustained),
                    "added": sorted(f"{k.file}::{k.fn_name}"
                                    for k in _sustain.merges)})
            for tname, units in units_by_test.items():
                prev = prev_test_units.get(tname)
                if prev != frozenset(units):
                    _set_since[tname] = iter_n
                if tname in escalation_products or any(
                        tname == _t for _t, _ in _sustain.dropped):
                    # Fired: handled above. A dropped product's test does not
                    # re-fire THIS sweep — the two-sweep rule re-arms it from
                    # the new set, exactly as a never-fired test earns one.
                    prev_test_units[tname] = frozenset(units)
                    continue
                _fire = escalation_fire_due(
                    frozenset(units), prev, _served_at,
                    _set_since.get(tname, iter_n))
                if _fire.unserved:
                    # The old rule fires here: the set is unchanged. Under the
                    # dispatch cap that is not evidence — these units were
                    # never a batch target since the set was observed.
                    deps.log(
                        f"  {tag} [attribution/escalate] {tname}: NOT "
                        f"escalating — unit-set unchanged but "
                        f"{len(_fire.unserved)} attributed unit(s) never "
                        "dispatched since it was observed (parked under the "
                        "dispatch cap, not served): " + ", ".join(
                            f"{u.file}::{u.fn_name}" for u in _fire.unserved))
                    deps.emit("phase_c.attribution.escalation_deferred", data={
                        "test": tname, "iter": iter_n,
                        "unserved": sorted(f"{u.file}::{u.fn_name}"
                                           for u in _fire.unserved)})
                if _fire.due:
                    # An accessor that is STILL A STUB was never served by any
                    # rewrite — it is the target, not futile. On 2026-08-11/12
                    # `display_formats_error` escalated past the still-stubbed
                    # `Display for MathError::fmt` to the already-correct
                    # `checked_div`; three arms spent 172-293 s rewriting it
                    # for zero movement while the real culprit sat unfilled.
                    _stub_accessors = sorted(u for u in units
                                             if u in _stub_keys)
                    if _stub_accessors:
                        deps.log(
                            f"  {tag} [attribution/escalate] {tname}: NOT "
                            "escalating — "
                            f"{len(_stub_accessors)} attributed unit(s) are "
                            "still stubs and no rewrite ever served them; "
                            "the stub is the target: " + ", ".join(
                                f"{u.file}::{u.fn_name}"
                                for u in _stub_accessors))
                        deps.emit(
                            "phase_c.attribution.escalation_withheld", data={
                                "test": tname,
                                "stub_accessors": sorted(
                                    f"{u.file}::{u.fn_name}"
                                    for u in _stub_accessors)})
                        prev_test_units[tname] = frozenset(units)
                        continue
                    # A test whose own source renders through a format trait
                    # does not escalate to producers: the escalation answers
                    # "who builds the accessors' container", and no builder
                    # changes what `{:?}` emits. The attribution's
                    # format-implementer hop owns that evidence and has
                    # already attributed the implementer beside the
                    # identifier owners. Run `114436-9cb17382`: the two
                    # remaining Debug tests' futility fires bought a 42-unit
                    # constructor family, re-merged every sweep, while the
                    # formatter was never dispatched under a rewrite grade.
                    _fmt_traits = format_traits_used(_attr.source_for(tname))
                    if _fmt_traits:
                        deps.log(
                            f"  {tag} [attribution/escalate] {tname}: NOT "
                            "escalating — the test's own source renders via "
                            f"{'/'.join(sorted(_fmt_traits))}; producers of "
                            "the accessors' container cannot change what the "
                            "format trait emits, and the format-implementer "
                            "attribution already owns this evidence.")
                        deps.emit(
                            "phase_c.attribution.escalation_withheld", data={
                                "test": tname,
                                "format_traits": sorted(_fmt_traits)})
                        prev_test_units[tname] = frozenset(units)
                        continue
                    _fired_producers: set[FnLocator] = set()
                    extra = deps.escalate_to_producers(
                        src_dir, tname, set(units), _fired_producers)
                    if extra:
                        escalation_products[tname] = (
                            frozenset(units),
                            {k: set(ts) for k, ts in extra.items()})
                        # Kept APART from the merged additions above: only the
                        # producer subset earns the ESCALATED grade in the
                        # prompt. A traversal step was named because a futile
                        # accessor calls it, which is not a measurement about
                        # it, and the merged set also carries the accessors'
                        # own file.
                        escalation_producers[tname] = frozenset(
                            _fired_producers)
                        _futile_fired.update(units)
                        _futile_fired_tests.add(tname)
                        # The escalation IS new evidence about its targets: a
                        # producer convicted before anyone knew this test
                        # needed it was judged on a different question. An
                        # exhausted target silently swallowed by the exhaustion
                        # re-entry filter is how arm A's hardcrate stable stopped at 46 while
                        # B/C (whose filter never caught it) reached 47.
                        _re_admitted = set(extra) & permanently_exhausted
                        if _re_admitted:
                            permanently_exhausted -= _re_admitted
                            for _k in _re_admitted:
                                refill_attempts.pop(_k, None)
                            deps.log(
                                f"  {tag} [attribution/escalate] {tname}: "
                                f"{len(_re_admitted)} escalation target(s) "
                                "were exhausted — re-admitted on the new "
                                "evidence: " + ", ".join(
                                    sorted(f"{k.file}::{k.fn_name}"
                                           for k in _re_admitted)))
                        # The SAME evidence opens the store's gate on the same
                        # targets. The local release above reaches only the
                        # exhaustion re-entry filter; a target whose disk record is
                        # `fully_exhausted` enters the frontier every sweep
                        # and is skipped at the route every time (the
                        # `<C,Error>` block, run `1246060-2339497b`). Capped
                        # per record by the reset budget, so a test that
                        # escalates to the same target every sweep buys it
                        # two re-attempts, not one per sweep.
                        _disk_released: list[str] = []
                        if deps.release_escalated_convictions is not None:
                            _disk_released = deps.release_escalated_convictions(
                                project_dir, src_dir, frozenset(extra),
                                f"TDD iter {iter_n}: escalation of {tname} "
                                "named the unit — new evidence about it")
                            if _disk_released:
                                deps.log(
                                    f"  {tag} [attribution/escalate] {tname}: "
                                    f"released {len(_disk_released)} stored "
                                    "exhaustion verdict(s) on its target(s): "
                                    + ", ".join(sorted(_disk_released)[:6]))
                        for k, ts in extra.items():
                            frontier.setdefault(k, set()).update(ts)
                        deps.emit("phase_c.attribution.escalated", data={
                            "test": tname,
                            "futile_units": sorted(str(u) for u in units),
                            "added": sorted(str(k) for k in extra),
                            "re_admitted": sorted(
                                str(k) for k in _re_admitted),
                            "disk_released": sorted(_disk_released),
                        })
                prev_test_units[tname] = frozenset(units)
            # A fired escalation is a MEASUREMENT about its accessors: their
            # rewrites change nothing for these tests. The retro store's
            # guidance for those units advises exactly such rewrites, and
            # left admissible it steers the fix loop against the escalation
            # — a stored `WRONG_TYPE_CHOICE` record pushed 14 rounds of
            # `ChainState`-as-`Vec` bodies while the escalation was declaring
            # the accessor futile in the same sweep (run `2773540-e47c3754`).
            # The verdict half of those records is untouched; only guidance
            # injection is suspended, cluster-scoped, on the read side.
            if _futile_fired and deps.suspend_futile_guidance is not None:
                _suspended = deps.suspend_futile_guidance(
                    project_dir, src_dir, frozenset(_futile_fired),
                    cluster.name, frozenset(_futile_fired_tests))
                if _suspended:
                    deps.log(
                        f"  {tag} [attribution/escalate] suspended stored "
                        f"guidance on {len(_suspended)} retro record(s) whose "
                        "accessors this sweep's escalation declared futile — "
                        "a record advising the rewrite futility just measured "
                        "as changing nothing may not steer the next one.")
                # …and on the PRODUCERS the fire names, which the accessor-only
                # suspension above never covered. The prompt is about to tell
                # them "the defect is in what this fn constructs — rewrite it";
                # their stored records say the opposite in the model's own
                # words. On the live tree
                # `src/error.rs::construct_from_std::Error` carries
                # WRONG_TYPE_CHOICE with the guidance "Build a single static
                # per-E `ErrorVTable` as before, then construct the erased
                # `ErrorImpl` …" — the delegation shape the escalation exists
                # to get rewritten. Two instructions in one prompt is not a
                # steer, and the retro one has the history.
                _esc_targets = frozenset(
                    k for ks in escalation_producers.values() for k in ks)
                if _esc_targets:
                    _esc_susp = deps.suspend_futile_guidance(
                        project_dir, src_dir, _esc_targets,
                        cluster.name, frozenset(_futile_fired_tests))
                    if _esc_susp:
                        deps.log(
                            f"  {tag} [attribution/escalate] suspended stored "
                            f"guidance on {len(_esc_susp)} ESCALATED "
                            "producer(s) — the prompt now states the "
                            "measurement that their consumers' rewrites moved "
                            "no verdict, and a stored record advising the "
                            "shape that measurement convicts may not steer "
                            "against it: " + ", ".join(sorted(_esc_susp)[:4]))

        # After-fire persist: GPU_GUARD_STOP fires before the dispatch save,
        # so a product computed on the stopping sweep never reached disk
        # (run `2773540-e47c3754`). This write is what a resume seeds.
        _persist_floor()

        # Union remaining cluster-active stubs into the frontier so a helper
        # that no failing test attributes directly (called only transitively)
        # still fills in phase-c instead of surviving to the post-TASK3 sweep
        # while its tested callers thrash as "blocked by an unfilled sibling
        # stub". order_frontier sorts leaf-first so helpers fill ahead of their
        # callers; the exhaustion re-entry filter + permanently_exhausted still bound re-entry.
        frontier = union_unattributed_stubs(frontier, _stub_list)

        # No frontier key may be a `#[test]` fn — the tests ARE the oracle, and a
        # fill dispatched at one rewrites the thing it is being judged by. The
        # attribution chain filters its own paths, but crash blame and futility
        # escalation ADD keys afterwards from raw fn records (escalation matched
        # `Error::` inside gen_tests bodies and proposed those tests as
        # "producers"), so the filter is re-applied over everything that landed.
        frontier = deps.filter_test_targets(frontier, src_dir)

        # Tests this sweep failed that no frontier unit owns — after
        # attribution, format-only fallback, and escalation. Always on the
        # cargo-test line; the event is the foldable form.
        _owned = {bare_test_name(t) for ts in frontier.values() for t in ts}
        _unowned = tuple(sorted(
            {bare_test_name(t) for t in last_failed} - _owned))
        deps.emit("phase_c.attribution.unattributed", data={
            "cluster": cluster.name, "iter": iter_n,
            "tests": list(_unowned), "count": len(_unowned)})

        # The exhaustion re-entry filter — for fns that already exhausted the
        # per-fn fix loop in a prior iter (avoid burning compute on the same fn
        # with no new context).
        # Crash evidence voids exhaustion: the prior attempts never saw a
        # localized faulting stack, so "nothing left to try" no longer holds.
        permanently_exhausted -= set(crash_texts)
        eligible: Frontier = {}
        skipped_perm: int = 0
        for key, tests in frontier.items():
            if key in permanently_exhausted:
                notes_now = deps.count_relevant_notes(fr_path, key)
                if total_filled == last_total_filled and \
                   notes_now <= last_notes_count.get(key, 0):
                    skipped_perm += 1
                    continue
                permanently_exhausted.discard(key)
            eligible[key] = tests

        # F4 core-first dispatch: a unit whose every failing test panics inside
        # a DIFFERENT still-stubbed producer can't be helped by filling it —
        # defer it (NOT exhaust) so the producer gets the LLM budget first. This
        # is stateless: recomputed each iter from the current stub set, so a
        # deferred consumer is re-admitted the moment its producer fills.
        # (`_stub_keys` is scanned once per iter, up with the sweep verdict, so
        # the plateau check can read the same set this dispatch does.)
        if _sweep_compile_broke and deps.implicated_fns is not None:
            # A red build reports no test verdict, so the frontier above is
            # reachability rather than evidence and dispatching from it burns
            # the budget on fns the compiler never named (every attempt comes
            # back `downstream_only`). Narrow to what the compiler implicates,
            # which is also the only set a fill can possibly fix.
            _blockers = frozenset(
                deps.implicated_fns(_build.build_output, target_src_dir))
            eligible, _unaimed = restrict_to_compile_blockers(
                eligible, _blockers)
            # The narrowing ADDS keys, so it lands them behind the test-oracle
            # filter applied above — and a red build's diagnostics point at the
            # test binaries, i.e. straight at gen_tests fns. Re-apply the filter
            # over what the compiler named: an error INSIDE a test is still not
            # a licence to rewrite the thing the cluster is scored by.
            eligible = deps.filter_test_targets(eligible, src_dir)
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: build is RED — "
                     f"frontier narrowed to the {len(eligible)} fn(s) the "
                     f"compiler implicates ({len(_unaimed)} withheld: no test "
                     "can convict them while nothing runs).")
            deps.emit("phase_c.tdd.compile_blocked_frontier", data={
                "cluster": cluster.name, "iter": iter_n,
                "implicated": len(_blockers), "withheld": len(_unaimed),
                "aimed": len(eligible),
            })
            # Red with nothing to aim at: no body the fill loop can write will
            # change the diagnostics, so iterating is pure burn — that is the
            # state which spent 8.7 h and 354 model calls looking like an
            # ordinary plateau. But an empty result is ALSO what the attribution
            # returns when it cannot see: `implicated_fns` is wired here to the
            # text scan (raw sweep output carries no structured diagnostics),
            # which attributes a macro-expansion error to NOTHING, and any error
            # outside `src/` to nothing as well. So "no fn implicated" is not yet
            # a diagnosis, and stopping on it at iter 1 would preempt the cause-A
            # layout retrigger below — the one mechanism built to repair a red
            # tree, and one that fires on the FIRST broken sweep. Give it its
            # sweep: only a red build that survives a retrigger's turn convicts.
            if not eligible and _compile_broke_streak >= 1:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: STOPPING "
                         "cluster — the build is red and no rustc diagnostic "
                         "resolves to a fillable fn in this tree, for "
                         f"{_compile_broke_streak + 1} consecutive sweep(s), "
                         "and the layout retrigger has had its turn. No fill "
                         "can repair it: the baseline itself is broken (bake "
                         "or seed).")
                deps.emit("phase_c.tdd.compile_blocked_exit", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "broken_sweeps": _compile_broke_streak + 1,
                    "compile_errors": compile_error_digest(output or "")[0],
                })
                _baseline_broken = True
                break
        eligible, _deferred = defer_blocked_consumers(eligible, _stub_keys)
        if _deferred:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: deferred "
                     f"{len(_deferred)} consumer(s) blocked by still-stubbed "
                     "producer(s) (re-admitted when the producer fills).")
            deps.emit("phase_c.dispatch.deferred", data={
                "cluster": cluster.name, "iter": iter_n,
                "deferred": {f"{f}::{fn}": sorted(f"{bf}::{bfn}"
                                                  for bf, bfn in blk)
                             for (f, fn), blk in _deferred.items()}})

        # Cause-C layout retrigger (proactive, not stall-gated): expected
        # tests VANISHING sweep after sweep with a clean compile is the
        # fingerprint of a frozen unsafe scheme whose filled bodies crash the
        # test binaries — per-fn re-fills can't converge on a shared pointer
        # scheme, so re-author the implicated frozen component instead of
        # burning the stall budget on it.
        # "With a clean compile" is load-bearing: a compile-broken sweep ran
        # no binaries, so its vanished names are not crash evidence — counting
        # it would re-author a frozen component off a transient compile error.
        # Such a sweep neither advances nor resets the streak.
        # Keyed on VANISHED NAMES, never on the raw missing count. A count-only
        # shortfall (every expected name reported, the total is still short) is
        # a static-inventory surplus, not a crash — it names no test, so the
        # CrashEvidence below would carry an empty vanished/suspect set and the
        # re-author would be aimed at nothing. Worse, such a shortfall is
        # CONSTANT: gating on it made every single iteration pay an extra full
        # sweep and declare "crash is deterministic" forever.
        # Cause-A layout retrigger, fired on the FIRST broken sweep. A cluster
        # whose test binaries do not build reports no verdict for any test, so
        # every further iteration of fills is spent on a tree that cannot score
        # — the stall budget measures fills nothing can run. `lib_compile_clean`
        # is what makes acting this early safe rather than trigger-happy: bodies
        # live inside the lib, so a build where the lib succeeded and only the
        # binaries failed cannot be any fill's fault, and the frozen/authored
        # shape they link against is the only remaining explanation.
        _compile_broke_streak = _compile_broke_streak + 1 if _sweep_compile_broke else 0
        if (_sweep_compile_broke
                and deps.retrigger_layout is not None
                and lib_compile_clean(_build.build_output)):
            _err_n, _err_lines = compile_error_digest(_build.build_output)
            _retrig_k = deps.retrigger_layout(
                project_dir, module, cluster.name, target_src_dir,
                _build.build_output,
                compile_evidence=CompileEvidence(
                    digest="\n".join(_err_lines),
                    lib_clean=True,
                    consecutive_sweeps=_compile_broke_streak),
                best_pass=best_pass)
            if _retrig_k.fired and _retrig_k.succeeded:
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: BUILD layout "
                    f"retrigger fired (cause={_retrig_k.cause}) — test binaries "
                    f"had not compiled for {_compile_broke_streak} sweep(s) "
                    f"while the lib did ({_err_n} distinct rustc error(s)); "
                    f"reverted {_retrig_k.reverted_count} fn(s) to re-fill "
                    "under the new layout; resetting stall budget.")
                deps.emit("phase_c.layout_retrigger.fired", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "cause": _retrig_k.cause,
                    "reverted_count": _retrig_k.reverted_count,
                    "compile_errors": _err_n,
                    "consecutive_broken_sweeps": _compile_broke_streak,
                })
                _compile_broke_streak = 0
                _destruction_epoch += 1
                # READ PATH: the re-stub is the destruction the ledger exists
                # for. It runs BEFORE the `continue`, so the next sweep
                # measures the re-seeded tree — a sweep of the un-re-seeded
                # one would re-base the high-water onto a tree that existed
                # for milliseconds. Only bodies whose file was NOT re-authored
                # come back; the gate refuses the rest, which is exactly the
                # retrigger's intent.
                # Same rule: the re-stub invalidates the held-over
                # coverage binding (deliberate destruction).
                bind_coverage_slice(None)
                _reseed_site("after retrigger re-stub (build cause)", iter_n)
                _grace = _grant_grace(_retrig_k.reverted_count,
                                      frozenset(_oracle.vanished))
                _emit_retrigger_iter("retrigger_build",
                                     _retrig_k.reverted_count)
                state.stalls = 0
                stall = 0
                continue
            if not _retrig_k.fired:
                # Nothing acts on this cluster: no component's private types
                # appear in the diagnostics. Say so once per broken sweep —
                # silence here is what made a 2h burn look like a plateau.
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: test binaries do not "
                    f"compile (lib does) and no layout component matched the "
                    f"diagnostics ({_retrig_k.reason}) — no repair route; "
                    f"{_err_n} distinct rustc error(s): "
                    + (" | ".join(_err_lines) or "(no parseable location)"))
        if _grace.active:
            # Inside a re-fill window the shrink is the window's OWN doing: the
            # re-author's surgical revert left the component's fns as stubs, and
            # a stub panic inside drop/vtable code aborts the test binary, so
            # every sweep until they re-fill shrinks REGARDLESS of the new
            # layout's quality. Neither consumer may read it — not the streak,
            # not the accelerated confirm — or the retrigger fires on its own
            # re-stub and burns the capped budget it is currently holding
            # (observed live: attempts 1 and 2 in back-to-back iters). The
            # streak is left untouched rather than reset: this sweep is silent
            # about the crash, not proof of its absence.
            #
            # One reading the window can NEVER explain: a vanished set that
            # adds nothing beyond the reading the fire itself acted on. That
            # reading was taken on the pre-revert tree — every re-stubbed body
            # still filled — so a stub cannot be its cause, and it is the
            # deterministic crash still standing, already proven by the streak
            # or re-sweep that authorized the fire. Arm `_crash_deterministic`
            # (the EVIDENCE half only; the streak stays 0, so the fire budget
            # cannot re-fire off this). Without it, a 25-body window starved
            # chain-derived blame for ceil(25/3)=9 iterations on run
            # `3368-753757e6` (2026-08-15) while every graced sweep printed
            # the same 3 binaries `already crashed at baseline` and two
            # consecutive sweeps measured 156 fills moving pass 39 -> 39.
            if (_oracle.vanished and not _sweep_compile_broke
                    and not _grace.explains_vanish(_oracle.vanished)):
                # Once per window (keyed by its grant iter), not per sweep —
                # the condition is standing, the news is the window entering
                # this state.
                if _predates_noted_for != _grace.granted_at:
                    _predates_noted_for = _grace.granted_at
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: vanished set "
                        f"({len(_oracle.vanished)} test(s)) is within the "
                        "reading the re-fill window's own fire acted on — the "
                        "crash predates the re-stub and stubs cannot explain "
                        "it; crash evidence stays armed through the window "
                        "(fires stay suppressed).")
                _crash_deterministic = True
        elif _oracle.vanished and not _sweep_compile_broke:
            _vanish_streak += 1
            # A shrunk oracle with a clean compile is either the frozen-scheme
            # crash class or a transient. Waiting for the streak proves
            # persistence at the cost of a full fill pass (hours of GPU spent
            # on tests that cannot report verdicts); an immediate re-sweep
            # (<1s) answers the same transient-vs-real question now — a crash
            # that reproduces back-to-back is deterministic, so the streak is
            # treated as complete this iter.
            if (_vanish_streak < _crash_retrigger_after
                    and deps.retrigger_layout is not None):
                _rc2, _out2 = deps.run_cargo(
                    test_cmd, target_src_dir, cluster_env, timeout=test_timeout)
                _p2, _f2, _ig2 = parse_test_counts(_out2)
                _oracle2 = _reconcile(
                    _rc2, _out2, _p2, _f2, parse_failed_tests(_out2)).oracle
                if _oracle2.vanished and not build_broke(_out2 or ""):
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: oracle shrink "
                        "reproduced on immediate re-sweep — crash is "
                        "deterministic, retriggering now instead of waiting "
                        "out the sweep streak.")
                    _vanish_streak = _crash_retrigger_after
            if _vanish_streak >= _crash_retrigger_after:
                _crash_deterministic = True
        elif not _oracle.vanished:
            # A compile-broken shrink is neutral; only a sweep in which every
            # expected name reported proves the crash class absent and may
            # reset the streak.
            _vanish_streak = 0
            # Determinism is a property of the CRASH, not of the fire budget,
            # so it is cleared by the same evidence that clears the streak —
            # every expected name reporting — and by nothing else. Keeping it
            # in its own variable is what lets the evidence half of chain
            # blame outlive the fire that resets `_vanish_streak` to 0.
            _crash_deterministic = False
        # Chain-derived blame: the frame-blind crash class. Three preconditions,
        # each load-bearing. `_crash_deterministic` — the shrink reproduced on an
        # immediate re-sweep, or persisted a full streak; a transient must not
        # synthesize blame. `_unattributed_sites` means an origin-capable
        # detector LOOKED this sweep and placed no frame in-crate — for refcount
        # corruption planted in construct/vtable code every ASAN stack resolves
        # to std/test frames (run `635043-5f7c61f5`, 2026-08-14), so no widening
        # of the frame parse can ever name the culprit and waiting for one caps
        # the oracle by design. `not crash_blamed` means no real frame exists —
        # an observed frame decides alone; the inference must not run beside it.
        # The chain is the crashed tests' attributed fns plus the producers of
        # their types (the same escalation the futility path uses): for
        # `test_drop`, `Error::new` → `construct_from_std` → the vtable
        # install/drop family.
        #
        # TWO CONSUMERS, ONE SYNTHESIS, DIFFERENT GATES. The retrigger's AIM
        # (signature, component choice) needs a fire and stays behind the fire
        # gate below. The EVIDENCE PACK — prompt evidence, frontier entry,
        # exhaustion void, dispatch eligibility — steers ordinary fills and
        # needs no fire at all; nesting it inside the fire gate is what left the
        # frame-blind sweep with no crash evidence whatsoever, which is the
        # starvation the whole mechanism was written to end.
        _suspects: set[FnLocator] = set()
        for _vt in _oracle.vanished:
            _bare = _vt.split("::")[-1]
            for _k in (test_reverse_map.get(_bare)
                       or test_reverse_map.get(_vt) or set()):
                _suspects.add(FnLocator(*_k))
        _chain_keys: frozenset[FnLocator] = frozenset()
        # The chain keys NO fault site names — the inferred cohort proper.
        # Recorded beside `_chain_keys` because the batch cursor treats the
        # two provenances differently (`CrashCohort.cursor_exempt`), and a
        # key that is both a chain member and a frame of a localized report
        # keeps its site standing.
        _chain_only: frozenset[FnLocator] = frozenset()
        # `bodies_in_flight`, not `active`: the window suppresses a reading the
        # re-stub itself produced, and a fired-but-REJECTED attempt re-stubbed
        # nothing (`restubbed=0`), so its sweep measures the real tree. That is
        # the distinction `RefillGrace` already draws — the suppression exists
        # to stop a back-to-back FIRE, not to stop prompt evidence. And a
        # reading the window cannot explain (`explains_vanish`: no vanished
        # name beyond the fire's own pre-revert reading) is admissible even
        # with bodies in flight — the re-fills the window protects are exactly
        # the fills this evidence pack should steer.
        if (_crash_deterministic and _unattributed_sites and not crash_blamed
                and (not _grace.bodies_in_flight
                     or (_oracle.vanished
                         and not _grace.explains_vanish(_oracle.vanished)))
                and deps.escalate_to_producers is not None):
            # WHICH TESTS the chain is derived from. The oracle's vanished set
            # and "the test the binary was running when it died" are different
            # questions, and on run `1064759-0f23fb68` they had no answer in
            # common: `vanished` was `test_large_alignment` +
            # `test_no_backtrace_fallback` (the two names the oracle could
            # prove missing) while every site named `test_drop` — the one test
            # whose chain actually holds the fault. The site's name is dropped
            # by the oracle for a good reason (its bare name also exists in a
            # surviving binary, so it is not provably missing) and that reason
            # says nothing about its value as a crash subject. This block's own
            # docstring above works the example through `test_drop`; until now
            # the derivation could not see it.
            _chain_from = frozenset(_oracle.vanished) | frozenset(
                s.test_name for s in _unattributed_sites if s.test_name)
            _chain_fns: set[FnLocator] = set(_suspects)
            for _vt in sorted(_chain_from):
                _bare = _vt.split("::")[-1]
                _units = {FnLocator(*_k) for _k in
                          (test_reverse_map.get(_bare)
                           or test_reverse_map.get(_vt) or set())}
                _chain_fns.update(
                    deps.escalate_to_producers(src_dir, _vt, _units))
            # DOUBLE DROP: narrow to the destruction family instead of widening.
            # A construction chain is the set of fns that BUILD the value, and
            # this fault class is a value destroyed twice — the culprit is on
            # the teardown path, which no producer escalation reaches. Measured
            # on the same run: the chain named 50 fns, nearly the whole crate,
            # and the only member of the drop family among them was
            # `Error::drop`; `object_drop_front` — whose body was `object_drop`
            # with a second parameter it never read — was never dispatched once
            # in 7h48m. Replacing rather than unioning is the point: a 50-fn
            # evidence pack aims at nothing, and the loop spent the night
            # re-filling it.
            _destruction = next((s.destruction for s in _unattributed_sites
                                 if s.destruction is not None), None)
            _dsubjects: frozenset[FnLocator] = frozenset()
            if _destruction is not None and deps.enumerate_fns is not None:
                _dsubjects = destruction_path_subjects(
                    deps.enumerate_fns(target_src_dir) or [])
            if _dsubjects:
                deps.log(
                    f"  {tag} [phase-c/crash] iter {iter_n}: the report reads "
                    f"as a DOUBLE DROP ({_destruction.freed_type if _destruction else '?'} "
                    f"freed at {_destruction.free_site if _destruction else '?'}, "
                    f"touched at {_destruction.access_site if _destruction else '?'}) "
                    f"— narrowing the chain from {len(_chain_fns)} construction "
                    f"fn(s) to the {len(_dsubjects)} fn(s) of this crate's own "
                    "destruction family, which a producer escalation cannot "
                    "reach: " + ", ".join(f"{k.file}::{k.fn_name}"
                                          for k in sorted(_dsubjects)))
                deps.emit("phase_c.crash.destruction_narrowed", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "freed_type": _destruction.freed_type if _destruction else "",
                    "free_site": _destruction.free_site if _destruction else "",
                    "access_site": _destruction.access_site if _destruction else "",
                    "chain_count": len(_chain_fns),
                    "subjects": sorted(f"{k.file}::{k.fn_name}"
                                       for k in _dsubjects)})
                _chain_fns = set(_dsubjects)
            _chain_frontier = deps.filter_test_targets(
                {k: set(_chain_from) for k in _chain_fns}, src_dir)
            _chain_sites = [chain_derived_site(
                s.binary_label, s.test_name, s.signal,
                sorted(_chain_frontier),
                derived_from_tests=sorted(_chain_from),
                raw_report=s.raw_report) for s in _unattributed_sites]
            _chain_texts = crash_evidence_by_locator(tuple(_chain_sites))
            _chain_keys = frozenset(_chain_texts)
            # Read BEFORE the merge below adds the chain keys to `crash_texts`:
            # what is in the dict now is site evidence (frames, producer aim,
            # the vanished test's subject), and only a chain key outside it
            # is cursor-bounded.
            _chain_only = _chain_keys - frozenset(crash_texts)
            for _loc, _text in _chain_texts.items():
                # The full evidence pack, applied here because this
                # branch runs AFTER the per-sweep evidence plumbing
                # (frontier merge, exhaustion void, eligibility) has
                # already consumed `crash_texts` for this iteration:
                # prompt evidence, frontier entry, exhaustion void, and
                # dispatch eligibility — each the same channel a
                # frame-derived blame gets, one grade weaker.
                crash_texts[_loc] = (
                    crash_texts[_loc] + "\n\n" + _text
                    if _loc in crash_texts else _text)
                frontier.setdefault(_loc, set()).update(_oracle.vanished)
                permanently_exhausted.discard(_loc)
                eligible.setdefault(_loc, set()).update(_oracle.vanished)
            # The pack re-arms on every frame-blind sweep (it steers that
            # sweep's fills), but it is only NEWS when the set moves. A line
            # and an event per sweep for an unchanged inference is how a log
            # stops being readable at the iteration boundary.
            if _chain_keys != _chain_blamed_last:
                if _chain_keys:
                    deps.log(
                        f"  {tag} [phase-c/crash] iter {iter_n}: "
                        "deterministic crash with no in-crate frame in any "
                        "stack — synthesized CHAIN-DERIVED blame "
                        "(provenance=chain_derived; ranks above test "
                        f"attribution, below an observed frame) for "
                        f"{len(_chain_keys)} fn(s) on the crashed tests' "
                        "construction chain: " + ", ".join(
                            f"{k.file}::{k.fn_name}"
                            for k in sorted(_chain_keys)[:8])
                        + ("…" if len(_chain_keys) > 8 else "")
                        + ". Evidence pack armed (prompt + exhaustion void "
                        "+ frontier); on a fire it also marks the crash "
                        "signature, which re-opens the component walk.")
                else:
                    deps.log(
                        f"  {tag} [phase-c/crash] iter {iter_n}: "
                        "deterministic crash with no in-crate frame, but the "
                        "construction chain resolved to no fn (no attributed "
                        "units, no producers) — no chain-derived blame, "
                        "signature unchanged.")
                deps.emit("phase_c.crash.chain_blame", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "binaries": sorted({s.binary_label
                                        for s in _unattributed_sites}),
                    "tests": sorted(_oracle.vanished)[:50],
                    "blamed": sorted(f"{k.file}::{k.fn_name}"
                                     for k in _chain_keys)[:50],
                    "provenance": "chain_derived",
                    "suspect_count": len(_suspects),
                    "chain_count": len(_chain_keys)})
            _chain_blamed_last = _chain_keys
        # The AIM half stays behind the fire budget: a signature and a
        # component choice are only meaningful when a re-author is about to
        # be attempted, and every fire costs an authoring call.
        if (_vanish_streak >= _crash_retrigger_after
                and deps.retrigger_layout is not None):
            _vanish_streak = 0   # re-arm only after another full streak
            _retrig_c = deps.retrigger_layout(
                project_dir, module, cluster.name, target_src_dir, output,
                crash_evidence=CrashEvidence(
                    vanished=frozenset(_oracle.vanished),
                    suspect_fns=frozenset(_suspects),
                    consecutive_sweeps=_crash_retrigger_after,
                    # The ORIGIN blame, never the evidence keys.
                    # `crash_texts` is keyed by EVERY frame plus every
                    # synthesized-evidence locator, so what survives a
                    # subtraction of the chain keys is not blame at all: on
                    # run `263217-d41a5bb5` (2026-08-15) it was the crashing
                    # TEST's own two frames — a fn and a `{closure#0}` in
                    # the generated `src/error.gen_tests_4.rs`, from an
                    # `AddressSanitizer: SEGV` whose origin was
                    # unattributed. They graded NONE against all 16
                    # non-GATED components, `crash_candidate_grade` returned
                    # on `blamed_fns` before it could consult the chain, the
                    # candidate list emptied, and the walk fell to its
                    # unlogged "no component matched the evidence" exit for
                    # 308 minutes — zero `[layout-retrigger]` lines — while
                    # the three loud consumers (prompt evidence, exhaustion
                    # void, frontier) kept firing on 67 chain-derived keys.
                    # `origin_blamed_locators` is the origin blame alone
                    # (no conversion-family union). `blamed_locators` still
                    # convicts the family for dispatch and the MUST; the
                    # retrigger's filled-body gate is true only when every
                    # blamed locator has a filled record, so the union
                    # re-opens a type re-author on a stub or undeclared
                    # sibling.
                    #
                    # What each crash class hands the aim, all three
                    # deliberate: frame-derived with an in-crate origin
                    # narrows from access+origin frames to the origin
                    # locators (sharper aim, one component per fault);
                    # frame-blind hands EMPTY, and an empty `blamed_fns`
                    # AIMS NOTHING — `crash_candidate_grade` grades against
                    # observed frames only, so the walk declines (loudly,
                    # with the evidence shape) instead of re-authoring a
                    # component an inference merely reaches. Measured, run
                    # `1577437-c93d5c26` iter 3 (2026-09-06): grading the
                    # 63-fn chain put 8 components in the OWNS tier
                    # (`src/fmt.rs::write_str` alone admitted `Quoted`),
                    # attempt 2 fired on `Quoted`, re-authored a type 0 of
                    # 373 fns reference, and reset the stall budget — for a
                    # fault the component never touched. Unlocalizable
                    # subject-synthesis (~500 lines above) likewise keeps its
                    # synthesized subject fns out of blame, as that branch's
                    # own comment promised ("as EVIDENCE …, not blame").
                    #
                    # Chain keys stay in their OWN field regardless: in
                    # `blamed_fns` they would enter the signature as a fn
                    # list, and that list includes producers matched against
                    # live body text — every landed fill could then change
                    # the signature and re-open the walk on a crash that
                    # never changed. `chain_blamed` contributes a stable
                    # marker instead (§ `crash_signature`) and the prompt
                    # evidence; it is not an aim. The origin blame carries no
                    # chain key by construction, so no subtraction is needed
                    # to keep them out.
                    blamed_fns=origin_blamed_locators(_sites),
                    chain_blamed=_chain_keys),
                best_pass=best_pass)
            if _retrig_c.fired:
                # Fired but did not land: nothing was re-stubbed, so one iter of
                # silence is all this owes — enough to stop a back-to-back
                # re-fire, not enough to excuse a breach or a plateau it cannot
                # explain. `granted_at` is what makes the one iter real: this is
                # the only grant site that does NOT `continue`, so without it
                # this iter's own `_score_grace` spends the window before the
                # next iter's streak check can read it (verified: the crash site
                # then fired in five consecutive iters, one authoring call each).
                _grace = RefillGrace.for_failed_attempt(
                    total_filled, granted_at=iter_n,
                    vanished_at_grant=frozenset(_oracle.vanished))
            if _retrig_c.fired and _retrig_c.succeeded:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: CRASH layout "
                         f"retrigger fired (cause={_retrig_c.cause}) — reverted "
                         f"{_retrig_c.reverted_count} fn(s) to re-fill under "
                         "the new layout; resetting stall budget.")
                deps.emit("phase_c.layout_retrigger.fired", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "cause": _retrig_c.cause,
                    "reverted_count": _retrig_c.reverted_count,
                })
                _destruction_epoch += 1
                # The re-stub invalidates the held-over coverage
                # binding for every consumer (deliberate destruction).
                bind_coverage_slice(None)
                _reseed_site("after retrigger re-stub (crash cause)", iter_n)
                _grace = _grant_grace(_retrig_c.reverted_count,
                                      frozenset(_oracle.vanished))
                _emit_retrigger_iter("retrigger_crash",
                                     _retrig_c.reverted_count)
                state.stalls = 0
                stall = 0
                continue

        # Exit condition: stall budget exhausted on the cluster-level
        # pass_count floor. Fires regardless of whether `eligible` is
        # empty — pass_count is the only metric that captures real-world
        # progress, so when it hasn't moved for `max_stalls` iters the
        # loop isn't making progress no matter how busy the frontier
        # looks. Pre-fix, an unfixable fn that stayed in the frontier
        # forever would override the stall check and burn compute
        # indefinitely. The frontier is now a hint about WHERE to look,
        # not a license to keep iterating. A non-positive budget disables
        # this exit (`domain/plateau.py` owns the semantics — the raw
        # comparison fired on the FIRST sweep at 0); the flat-score guard
        # and the iteration backstop still bound the loop.
        if plateau_exit_due(stall, max_stalls):
            # Before giving up, check whether the plateau is a private
            # LAYOUT problem (t1.5 left a component frozen on the original
            # scaffold, and this cluster's fills are fighting it) rather than
            # N independently-hard fns. A successful re-author + surgical
            # revert resets the stall counter and lets the loop continue
            # against a layout the model can actually work with; a capped or
            # failed attempt falls through to the unchanged exit below.
            if deps.retrigger_layout is not None:
                _retrig = deps.retrigger_layout(
                    project_dir, module, cluster.name, target_src_dir, output,
                    best_pass=best_pass)
                if _retrig.fired and _retrig.succeeded:
                    deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: layout "
                         f"retrigger fired (cause={_retrig.cause}) — reverted "
                         f"{_retrig.reverted_count} fn(s) to re-fill under the "
                         "new layout; resetting stall budget.")
                    deps.emit("phase_c.layout_retrigger.fired", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "cause": _retrig.cause,
                        "reverted_count": _retrig.reverted_count,
                    })
                    _destruction_epoch += 1
                    # Same rule: the re-stub invalidates the
                    # held-over coverage binding.
                    bind_coverage_slice(None)
                    _reseed_site("after retrigger re-stub (stall cause)",
                                 iter_n)
                    _grace = _grant_grace(_retrig.reverted_count,
                                          frozenset(_oracle.vanished))
                    _emit_retrigger_iter("retrigger_stall",
                                         _retrig.reverted_count)
                    state.stalls = 0
                    stall = 0
                    continue
            # Convict only what was DISPATCHED inside the stalled window — a
            # unit the window never attempted was refused, not judged, and a
            # blanket conviction here was the code-surface hole the audit of
            # 2026-08-10 named: 15 fns convicted at run `2219473`'s stable
            # exit, of which iters 3-4 had dispatched zero.
            _window_keys: set[FnLocator] = set(_resumed_cursor_window)
            for _dset in _dispatched_window[-max_stalls:]:
                _window_keys |= _dset
            _convictable = set(eligible) & _window_keys
            _spared = set(eligible) - _convictable
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: pass_count plateau "
                 f"{stall} iter(s) at best={render_pass_floor(best_pass)}; stopping "
                 f"({len(_convictable)} of {len(eligible)} frontier fn(s) "
                 "were dispatched in the stalled window — those will be "
                 "marked exhausted"
                 + (f"; {len(_spared)} never dispatched, left admitted"
                    if _spared else "")
                 + f"). best pass={render_pass_floor(best_pass)} "
                 f"fail={best_fail}.")
            _log_attribution_gap(deps, tag, iter_n, fail_count, cur_failed,
                                 len(frontier), kind)
            for _stuck_key in _convictable:
                permanently_exhausted.add(_stuck_key)
                deps.emit("phase_c.verdict", data={
                    "unit": f"{_stuck_key.file}::{_stuck_key.fn_name}",
                    "route": "tdd", "attempt": iter_n,
                    "disposition": "exhaustion", "outcome": "negative",
                    "reason": "stall exit — dispatched and judged inside "
                              "the stalled window with no score movement",
                    "cluster": cluster.name})
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible), "kind": "stall_exit",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            break
        # THE ONLY MOVE LEFT, for every shape of undispatchable. Both
        # entries below — an empty FRONTIER and an empty BATCH — re-admit
        # what the loop itself holds exhausted before they consider
        # stopping, and they share ONE budget: two counters over one store
        # would let each shape spend the other's, and the store's own
        # per-record cap would then be the only thing bounding a loop that
        # has two ways to ask it.
        def _spend_no_move_readmission(
            reason: str,
            # Bound at definition, this iteration's frontier: the helper is
            # redefined per iteration and must not read the next one's.
            _frontier: Mapping[FnLocator, Any] = frontier,
        ) -> tuple[frozenset[FnLocator], list[str]]:
            """Reopen the held units and the store's verdicts, charge one
            re-admission, and return `(reopened locators, released store
            keys)`. `(frozenset(), [])` when the budget is spent or there
            was nothing to reopen — the caller then has no move left and
            falls through to the stop."""
            nonlocal _no_move_readmissions, permanently_exhausted
            if _no_move_readmissions >= _reset_cap:
                return frozenset(), []
            held_now = set(_frontier) & permanently_exhausted
            released = deps.reset_exhaustion_flags(project_dir, reason)
            released_locs = {
                FnLocator(parts[0], parts[1])
                for parts in (str(k).split("::") for k in released)
                if len(parts) >= 2}
            if not (held_now or released):
                return frozenset(), []
            _no_move_readmissions += 1
            reopened = held_now | released_locs
            permanently_exhausted -= reopened
            for loc in reopened:
                refill_attempts.pop(loc, None)
            return frozenset(reopened), list(released)

        if not eligible:
            _no_frontier_streak += 1
            # Nothing dispatchable. This is not "flat" and `max_stalls` never
            # governed it: the plateau exit above charges a COMPLETED
            # traversal, and an empty frontier completes nothing. First the
            # only move left — re-admit what the loop itself holds exhausted
            # (the exhaustion re-entry filter's set and the store's flags), bounded by the
            # same reset cap the store applies per record; then, with that
            # budget spent and still nothing to dispatch, stop under a name
            # instead of re-running a cached `cargo test` to the backstop.
            # Only where the plateau exit is DISABLED: with a stall budget the
            # empty frontier is charged like any flat sweep and the stall exit
            # ends it within `max_stalls` cached iterations — the documented
            # terminator, and the one the refill cap's attempt bound is pinned
            # against. With `max_stalls=0` nothing ends it.
            _held = set(frontier) & permanently_exhausted
            _spin_regime = plateau_exit_disabled(max_stalls)
            if _spin_regime:
                _reopen, _disk = _spend_no_move_readmission(
                    f"TDD iter {iter_n}: no actionable frontier — no other "
                    f"move (re-admission {_no_move_readmissions + 1}/"
                    f"{_reset_cap})")
                if _reopen:
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: no actionable "
                        f"frontier and no other move — re-admitting "
                        f"{len(_held)} unit(s) the loop held as exhausted and "
                        f"{len(_disk)} stored verdict(s) for one more attempt "
                        f"({_no_move_readmissions}/{_reset_cap}; "
                        f"pass={pass_count}, fail={fail_count}): "
                        + ", ".join(sorted(
                            f"{k.file}::{k.fn_name}" for k in _reopen)[:8]))
                    deps.emit("phase_c.tdd.no_move_readmission", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "held": sorted(f"{k.file}::{k.fn_name}"
                                       for k in _held),
                        "disk_released": sorted(_disk),
                        "attempt": _no_move_readmissions,
                        "cap": _reset_cap,
                    })
                    deps.emit("phase_c.tdd.iter", data={
                        **_prov,
                        "cluster": cluster.name, "iter": iter_n, "rc": rc,
                        "frontier_size": 0, "kind": "no_frontier_readmit",
                        "pass_count": pass_count, "fail_count": fail_count,
                        "improved": improved, "stall": stall,
                    })
                    _score_grace(iter_n, pass_count)
                    continue
            if (_spin_regime and _no_frontier_stop_after > 0
                    and _no_frontier_streak >= _no_frontier_stop_after):
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: NO_FRONTIER_STOP — "
                    f"{_no_frontier_streak} consecutive iteration(s) found "
                    f"nothing dispatchable (frontier={len(frontier)} unit(s), "
                    f"{len(_held)} held as exhausted, {fail_count} test(s) "
                    "still failing) and the no-other-move re-admission budget "
                    f"is spent ({_no_move_readmissions}/{_reset_cap}). "
                    f"Reading pass={pass_count} fail={fail_count}. Stopping "
                    f"the RUN instead of spinning to the {_tdd_backstop}-"
                    "iteration backstop. Nothing is convicted and every fill "
                    "is on disk — `bash continue.sh` resumes here; "
                    "TDD_NO_FRONTIER_STOP=0 disables this stop.")
                _log_attribution_gap(deps, tag, iter_n, fail_count,
                                     cur_failed, len(frontier), kind)
                deps.emit("phase_c.tdd.no_frontier_stop", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count, "fail_count": fail_count,
                    "frontier_size": len(frontier),
                    "held_exhausted": len(_held),
                    "streak": _no_frontier_streak,
                    "readmissions": _no_move_readmissions,
                    "cap": _reset_cap,
                })
                deps.emit("phase_c.tdd.iter", data={
                    **_prov,
                    "cluster": cluster.name, "iter": iter_n, "rc": rc,
                    "frontier_size": 0, "kind": "no_frontier_stop",
                    "pass_count": pass_count, "fail_count": fail_count,
                    "improved": improved, "stall": stall,
                })
                request_graceful_stop()
                break
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: no actionable "
                 f"frontier this iter but stall={stall}/{max_stalls} — "
                 f"continuing (pass={pass_count}, fail={fail_count}, "
                 f"improved={improved}).")
            _log_attribution_gap(deps, tag, iter_n, fail_count, cur_failed,
                                 len(frontier), kind)
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": 0, "kind": "no_frontier_continue",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            # Nothing to do this iter — loop back to re-run cargo test.
            # (Mostly a no-op, but keeps the loop honest: if a flaky
            # failure or test ordering surfaced a fn next iter, we'll
            # pick it up.)
            _score_grace(iter_n, pass_count)
            continue
        # The streak is NOT cleared here. A non-empty frontier used to clear
        # it, which made the terminator above a statement about the frontier
        # rather than about whether anything was dispatched — and the loop's
        # own refusals (the flat exclusion, the pass cursor, a route cap) can
        # empty the BATCH while the frontier still holds units. It is cleared
        # where a unit is actually dispatched, below `take_batch`.

        # The batch is decided BEFORE the guard, and committed after it. Two
        # reasons, both about honesty at the stop: the stop line has to name
        # what it declined to spend (`len(eligible)` is the frontier, and under
        # the cap the next dispatch is at most `PHASE_C_TDD_MAX_DISPATCH` of
        # it), and the traversal cursor must not advance on an iteration that
        # never dispatched. `take_batch` is pure, so computing it early costs
        # nothing and changes nothing.
        ordered = deps.order_frontier(eligible, leaf_counts)
        # CRASH EVIDENCE OUTRANKS EVERYTHING, and until now it outranked
        # nothing. `order_frontier` ranks on static shape — panic depth, how
        # many failing tests attribute a unit, how many callers reference it —
        # which is the right question for an attribution-built frontier and the
        # wrong scale for "this fn is named by a fault the tree produced on THIS
        # sweep". The pack gave its subjects a prompt, a frontier entry, an
        # exhaustion void and eligibility, and then let them queue behind units
        # chosen on a different question: run `1692702-94e6163a` (2026-08-16)
        # named the five-fn destruction family on iterations 2 and 3 and
        # dispatched a batch of 12 out of 57 with `0 skipped — free fns` while
        # four of the five are free fns. Grades keep an observed frame above an
        # inference: blamed frames first, then chain-derived, then any other
        # evidence key (producer aim, synthesized subject).
        #
        # PROVENANCE IS DATA, not a grade integer read back in three places.
        # `sited` is every evidence key a fault SITE produced — the keys
        # `crash_texts` held before the chain merge (all frames, producer aim,
        # synthesized subject); `chain` is the inference. The cohort's
        # `grades` are the ranking above; its `cursor_exempt` is what the
        # pass cursor may re-offer, and that is the site keys ONLY — see
        # `CrashCohort.cursor_exempt` for the 63-unit measurement.
        _crash_cohort: CrashCohort[FnLocator] = CrashCohort(
            blamed=crash_blamed,
            sited=frozenset(crash_texts) - _chain_only,
            chain=_chain_keys)
        _crash_grades: dict[FnLocator, int] = _crash_cohort.grades
        # A unit filed `declined` BEFORE the fault was localized is carrying a
        # record that now contradicts its instruction: `declined` asserts
        # "returning it unchanged was what we asked for", and this sweep is
        # about to ask it to rewrite. Excluding it from the next write is not
        # enough — the set accumulates across batches AND is resumed from the
        # floor (`_resume.declined`), so a stale entry outlives the process
        # that made it and is what `cursor_carry` keeps unconditionally.
        # `src/error.rs::drop` and `src/ptr.rs::as_ptr` were both on the live
        # floor's `declined` list under an active heap-use-after-free blame.
        # Retract on the sweep that re-derives the evidence.
        _crash_retracted = _pass_declined & _crash_grades.keys()
        if _crash_retracted:
            _pass_declined -= _crash_retracted
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: "
                f"{len(_crash_retracted)} unit(s) RETRACTED from the pass's "
                "declined set — a fault now names them, so 'returned it "
                "unchanged as instructed' is no longer what happened: "
                + ", ".join(sorted(f"{k.file}::{k.fn_name}"
                                   for k in _crash_retracted)[:8]))
        # BLOCKER-HISTOGRAM ORDERING. First-fill already admits the live-stub
        # producers into the batch; constructor-first ranking used to wait on
        # `_esc_keys`, which is empty on the first pass because census P15
        # defers escalation until those units have been dispatched (run
        # `155810-cbb3b9fc`). The histogram does not need a fire: post-fill
        # counts from the previous iter's payoff when bodies landed blocked,
        # else the pre-dispatch deferral map. Crash still outranks this.
        _ranks: dict[FnLocator, int] = {}
        if _prev_payoff is not None and _prev_payoff.landed_blocked:
            _ranks = _prev_payoff.locator_counts()
        if not _ranks:
            _ranks = blocker_histogram(_deferred)
        # A stub's measured hold is the direct count of what the histogram
        # infers from attribution: on run `3453127-045b05b1` iter 3 the
        # histogram gave `vtable` 7 deferred consumers while 82 failing tests
        # stopped inside it.
        for _site in _stub_hold.blockers:
            _loc = locator_of(_site)
            _ranks[_loc] = max(_ranks.get(_loc, 0),
                               len(_stub_hold.held_by(_site)))
        if _ranks:
            _before_blk = [k for k, _ in ordered]
            ordered = promote_by_blocker_counts(ordered, _ranks)
            if [k for k, _ in ordered] != _before_blk:
                _top = sorted(_ranks.items(), key=lambda kn: (-kn[1], kn[0]))[:4]
                deps.log(
                    f"  {tag} [phase-c/dispatch] blocker histogram: "
                    + ", ".join(f"{k.file}::{k.fn_name} ({n})"
                                for k, n in _top)
                    + " rank first (crash evidence still outranks)")
        # ESCALATED-PRODUCER ORDERING. Two steps, then crash still wins.
        # (1) The product as a block ranks ahead of attributed accessors —
        # `promote_producers` only shuffles the product's own slots, so a
        # newly attributed accessor kept the head of the batch and persist
        # still spent iter 1 on the re-buy (runs `4911`/`2932`; cap=1
        # replay: accessor at slot 0, producer parked). (2) Inside that
        # block, constructors before delegators, so the constructor reading
        # arrives in the FIRST batch of a producer pass instead of one ~3h
        # sweep later (`40938-f4e8e94b`, `4911-9ab8852e`, `95878-aaf5bf1d`).
        # Crash promotion runs AFTER both, so an observed fault keeps the
        # very front.
        _esc_keys = frozenset(
            k for ks in escalation_producers.values() for k in ks)
        # Rank the cached additions, not the prompt-grade producer subset.
        # `escalation_producers` is filled through `producers_out`; a fire
        # that returned extras without that side channel (and a resume of
        # an empty persisted producer tuple) would skip this lift and
        # spend cap=1 on the new accessor.
        _product_keys = product_addition_keys(escalation_products)
        if _product_keys:
            _before_prod = [k for k, _ in ordered]
            ordered = promote_product_ahead(ordered, _product_keys)
            if [k for k, _ in ordered] != _before_prod:
                deps.log(
                    f"  {tag} [phase-c/dispatch] product ahead: "
                    f"{len(_product_keys)} escalated addition(s) rank before "
                    "attributed accessors (crash evidence still outranks)")
        # The tree as the extractor sees it, read ONCE for this dispatch: the
        # producer ranking below and the evidence premise further down both
        # want it, and two reads of one tree in one iteration are two chances
        # to disagree about it.
        _disp_recs: list[dict[str, Any]] = (
            (deps.enumerate_fns(target_src_dir) or [])
            if deps.enumerate_fns is not None else [])
        if (_esc_keys and deps.enumerate_fns is not None
                and deps.enumerate_types is not None):
            _pr_types = deps.enumerate_types(target_src_dir) or []
            _builders, _callees = producer_dispatch_ranks(
                _esc_keys, _disp_recs, _pr_types)
            _before = [k for k, _ in ordered]
            ordered = promote_producers(ordered, _esc_keys, _builders, _callees)
            if [k for k, _ in ordered] != _before and _builders:
                deps.log(
                    f"  {tag} [phase-c/dispatch] producer order: "
                    f"{len(_builders)} constructor(s) promoted ahead of the "
                    f"delegators in the escalated cohort ({len(_callees)} "
                    "resolved callee(s) next): " + ", ".join(
                        sorted(f"{k.file}::{k.fn_name}" for k in _builders)[:8]))
        # SERVICE / DEAD ranking. Constructor-first above puts the table-
        # builders at the head of the product; the ones the ledger already
        # graded `flat` then occupy the whole cap, and a never-served owner
        # (`src/lib.rs::format_err` at rank 21 on run `2220347-7fa9b90f`)
        # never reaches a batch. This sits AFTER that promotion so the
        # never-served / not-flat / flat / dead buckets actually decide
        # take_batch. Crash still runs after and still wins.
        # STANDING CLAIM ABOUT THE TREE, not the re-seed view. `latest_by_unit`
        # lets an off-tree `proven` mask a later `flat` about the body that is
        # actually on disk (run `9872-51b20f30`: 10 FLAT vs 18 on-tree). Dispatch
        # reads the on-tree view; re-seed keeps `latest_by_unit`.
        _body_now: dict[LedgerKey, str] = {}
        _proj, _cl = project_dir.name, cluster.name
        if deps.tree_writer is not None:
            for _k, _ in ordered:
                with suppress(OSError, TypeError, ValueError):
                    _body_now[LedgerKey(_proj, _cl, _k.file, _k.fn_name)] = (
                        body_sha(deps.tree_writer.bodies(_k, target_src_dir)))
        _stand = (standing_on_tree(_ledger_entries(), _body_now)
                  if _body_now else latest_by_unit(_ledger_entries()))
        # ...and scoped to the claim's PREMISE, not the bare grade: a FLAT
        # row licenses exclusion only while the same oracle scores the tree,
        # no NEW test convicts the unit, AND the unit would be asked the same
        # question (`flat_standing_unchanged`). Keyed on the grade alone the
        # exclusion is a permanent lock, because dispatch is the only
        # producer of the new body that could ever clear it.
        _bare_failed_now = frozenset(bare_test_name(t) for t in cur_failed)
        _convict_now = {
            LedgerKey(_proj, _cl, _u.file, _u.fn_name): frozenset(
                bare_test_name(t) for t in _ts) & _bare_failed_now
            for _u, _ts in eligible.items()}
        # WHAT THE UNIT WOULD BE TOLD, computed here and nowhere else. The
        # three clauses above all describe the world AROUND the unit and on a
        # stalled tree none of them moves — clause 1 holds BECAUSE the flat
        # body was kept — so a unit that lands a compiling, non-regressing,
        # non-paying body is retired by its own success. Run
        # `837207-ce5b1647` (2026-09-15) excluded `src/fmt.rs::debug` from
        # iter 2 and `src/error.rs::fmt` from iter 3 and then read
        # `dispatching 0 of 0 (0/0 this pass)` against `frontier=3 fn(s)` for
        # three hours until an external TERM. This value is BOTH the
        # comparison and the stamp the fill's rows carry, so the question
        # asked and the question recorded cannot drift apart.
        _evidence_now = {
            LedgerKey(_proj, _cl, _u.file, _u.fn_name): _fp
            for _u, _fp in evidence_fingerprints(
                [k for k, _ in ordered], _disp_recs,
                retro=(deps.retro_evidence_texts(project_dir)
                       if deps.retro_evidence_texts is not None else {}),
                crash=crash_texts).items()}
        _evidence_of = {
            (_k.file, _k.fn_name): _fp
            for _k, _fp in _evidence_now.items()}
        _flat_keys = {
            FnLocator(_k.file, _k.fn_name)
            for _k in flat_standing_unchanged(
                _stand, oracle_fp_now=_oracle_fp(iter_n),
                convicting_now=_convict_now,
                evidence_now=_evidence_now)}
        _cov = None
        if deps.measure_coverage_slice is not None:
            # The failing set is what pass C profiles: the owner map exists
            # to answer "can a rewrite of this unit move THIS red test",
            # and a passing test asks nothing. Costs one instrumented run
            # per failing test on a staged copy.
            # A name the sweep failed in several binaries is several failing
            # tests: pass C measures it on each binary that failed it.
            _cov = deps.measure_coverage_slice(
                project_dir, cluster.name,
                failing_tests=frozenset(cur_failed),
                cluster_env=cluster_env,
                timeout=test_timeout,
                hung={b.label: (b.dangling or "?") for b in _hung},
                failing_binaries=failing_binaries_by_name(
                    _measure.failed_pairs))
            if _cov is not None and _cov.hung_tests:
                deps.emit("phase_c.coverage.hung", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "hung_tests": sorted(_cov.hung_tests),
                })
        # UNMEASURED RUNS. A red test whose process did not complete, or that
        # failed at a panic inside the crate, never reached the end of its
        # path; every negative claim the reading makes ("executed by no
        # test", "no failing test enters this body") is withheld while any
        # such run exists (`coverage_slice.withhold_unmeasured`). What those
        # runs DID enter stays positive evidence for admission and ranking:
        # `_owners_positive` is taken before the withholding, and the record
        # of what a crashed test entered when it last ran stands in for it.
        _recorded: dict[str, frozenset[FnLocator]] = {}
        _owners_positive: dict[str, frozenset[FnSite]] = (
            dict(_cov.owners) if _cov is not None else {})
        if _cov is not None:
            _truncated = truncated_failing(outcomes_of(output))
            if _cov.unmeasured_tests and deps.recorded_entry_sets is not None:
                _recorded = dict(deps.recorded_entry_sets(
                    project_dir, sorted(_cov.unmeasured_tests)))
            _cov, _dead_withheld = withhold_unmeasured(_cov, _truncated)
            if _cov.unmeasured_tests:
                deps.log(
                    f"  {tag} [phase-c/coverage] {len(_cov.unmeasured_tests)} "
                    "failing test(s) did not reach the end of their path ("
                    f"{len(_truncated)} stopped inside the crate"
                    + (", binaries did not complete: "
                       + ", ".join(sorted(_cov.unmeasured_binaries))
                       if _cov.unmeasured_binaries else "")
                    + f"); {len(_dead_withheld)} dead / unreached-line "
                    "claim(s) withheld and those tests' owner sets kept as "
                    "positive evidence only")
        bind_coverage_slice(_cov)
        _dead_keys: frozenset[FnLocator] = (
            _cov.dead if _cov is not None else frozenset())
        # The owner sets the ranking and the admission read: measured
        # where the test completed, recorded where it did not. Sessions
        # take unique-container sites from the frontier, not this map.
        # The BOUND slice keeps the measured map only — a recorded set is
        # not a measurement and must not be re-stamped as one.
        _owners_read: dict[str, frozenset[FnSite]] = dict(_owners_positive)
        # Ranking converts sites back to locators, so an empty container
        # here is a (file, fn) stamp and does not fan out. Sessions do
        # not read this map: they take unique-container sites below.
        _recorded_failing = {
            t: us for t, us in _recorded.items() if t in cur_failed}
        _recorded_rank = {
            t: frozenset(FnSite(u.file, u.fn_name, "") for u in us)
            for t, us in _recorded_failing.items()}
        _owners_read.update(_recorded_rank)
        _recorded_sites = recorded_locator_sites(
            _recorded_failing, _attr.site_frontier)
        # The owner map decided a refusal and reached no record: nothing in
        # `events.jsonl` carried it, so every later argument about whether a
        # demotion was right had to re-measure the tree to find out what the
        # loop had read. Emit the reading that justifies the ranking, with
        # the ranking.
        if _cov is not None and _owners_read:
            deps.emit("phase_c.coverage.owner_map", data={
                "cluster": cluster.name, "iter": iter_n,
                "failing": sorted(cur_failed),
                "measured": sorted(_cov.owners),
                "owners": {t: [site_payload(k) for k in sorted(ks)]
                           for t, ks in sorted(_owners_read.items())},
                "unmeasured": sorted(_cov.unmeasured_tests),
                "unmeasured_binaries": sorted(_cov.unmeasured_binaries),
                "recorded_owners": {
                    t: [site_payload(k) for k in sorted(ks)]
                    for t, ks in sorted(_recorded_rank.items())},
                "dead_withheld": sorted(
                    f"{k.file}::{k.fn_name}" for k in _dead_withheld)
                if _cov is not None else [],
            })
        # RED-DEAD DEMOTION. `dead` above is a whole-suite merge and cannot
        # say "no FAILING test enters this body", which is the only question
        # that decides whether a rewrite can move a verdict THIS sweep. The
        # escalated cohort is exempt: an escalated producer is unentered
        # precisely when the chain breaks upstream of it, and on this
        # iteration's own frontier a rule without the exemption sinks 9
        # live escalated units, 5 of them never-served (`red_dead_units`
        # carries the computation). Crash promotion runs after this and
        # re-lifts anything a fault names.
        _red_dead: frozenset[FnLocator] = frozenset()
        if _cov is not None and _owners_read:
            # The withheld map: a failing test whose run stopped short is
            # absent from it, so the demotion declines (`red_dead_units`'s
            # own "every red test measured" rule).
            _red_dead = red_dead_units(
                (k for k, _ in ordered), cur_failed, _cov.owners,
                exempt=_esc_keys | _product_keys | _crash_grades.keys())
        # OWNER-MAP ADMISSION. Ranking can promote a never-served owner but
        # cannot put `error.rs::fmt` on the frontier: run `451330-7e8a7ca9`
        # listed it on all three remaining tests and never dispatched it.
        # Fire when every attributed-or-escalated unit for a measured test
        # has been SERVED (or is dead/red-dead) — grade unanimity cannot
        # fire (run `9872-51b20f30`: never_served=[] from iter 1, FLAT=10,
        # zero `measured_owners` emissions). Dead product members still
        # count as exempt-from-service so an unattempted `write_str` cannot
        # hold the fire. Additions join escalation_producers (ESCALATED
        # grade) and the frontier; they do not replace an existing producer
        # product and they do not re-admit permanently_exhausted.
        _owner_admits: dict[FnLocator, set[str]] = {}
        _units_for_owners: dict[str, set[FnLocator]] = {}
        for _ok, _ots in eligible.items():
            for _ot in _ots:
                _units_for_owners.setdefault(
                    bare_test_name(_ot), set()).add(_ok)
        _pair_locs = frozenset(
            loc
            for p in (_attr.pairs or ())
            if p.cross_file
            for loc in p.member_locators())
        _has_pair = frozenset(
            t for t, us in _units_for_owners.items()
            if us & _pair_locs)
        # Read per ITERATION, not once at cluster entry: the fill appends
        # the rows this asks about, so a cluster-entry read would judge
        # every iteration on the evidence the first one had.
        _route_rows: Sequence[RouteLedgerRow] = (
            deps.route_ledger_rows(project_dir.name, cluster.name)
            if deps.route_ledger_rows is not None and _has_pair else ())
        # ...and the withholding that buys is EARNED, on the pair's own
        # record. Withholding for as long as the pair EXISTS is a lock and
        # not a refusal to spend: the release condition is that the
        # leftover stops having a pair, and no fill can cause that — the
        # attribution re-derives the hop every iteration and a pair that
        # reverts keeps it. Run `1158170-ce307ee7` (2026-09-15) measured
        # the executed slice of its three leftovers at 29 units, dispatched
        # 3, and spent both fills' 54 calls inside the pair's two files for
        # a byte-identical verdict vector. The bound and the evidence are
        # the schedule's own (`consecutive_unpaid` / `DEFAULT_UNPAID_GRACE`,
        # which already ranked that pair `grace_spent`), read here per TEST
        # because a pair's ledger row carries the same-file table slots the
        # crash evidence joined to its write set and its hop does not.
        _pair_unpaid = {
            t: unpaid_pair_buys(_route_rows, t, _oracle_fp(iter_n))
            for t in _has_pair}
        _pair_unopened = {
            t: unopened_pair_fills(_route_rows, t, _oracle_fp(iter_n))
            for t in _has_pair}
        _paired_tests = withholding_paired_tests(
            _has_pair, unpaid_buys=_pair_unpaid,
            unopened_fills=_pair_unopened)
        _pair_lapsed = _has_pair - _paired_tests
        # Snapshot before owner-map admission: spine locators the map
        # would add are coverage, not the attributed write.
        _leftover_bare = {bare_test_name(t) for t in cur_failed}
        _attributed_locs = frozenset(_pair_locs).union(
            *( _units_for_owners.get(_t, ()) for _t in _leftover_bare ))
        if _paired_tests:
            deps.log(
                f"  {tag} [phase-c/dispatch] measured-owners: "
                f"{len(_paired_tests)} leftover test(s) already have a "
                "hop-pair transaction whose buy is unspent — not admitting "
                "coverage owners those tests execute through; the pair is "
                "the fill: "
                + ", ".join(
                    f"{t} (pair unpaid {_pair_unpaid.get(t, 0)}/"
                    f"{DEFAULT_UNPAID_GRACE})"
                    for t in sorted(_paired_tests)[:8]))
        if _pair_lapsed:
            deps.log(
                f"  {tag} [phase-c/dispatch] measured-owners: "
                f"{len(_pair_lapsed)} leftover test(s) have a hop pair that "
                "has been bought and returned nothing, or has not opened, "
                f"in {DEFAULT_UNPAID_GRACE} fill(s) under this oracle (the "
                "bound the schedule already lapses a route's exemptions "
                "on) — the withholding lapses and the coverage owners they "
                "execute through are admissible: "
                + ", ".join(
                    f"{t} (pair unpaid {_pair_unpaid.get(t, 0)}, "
                    f"unopened {_pair_unopened.get(t, 0)})"
                    for t in sorted(_pair_lapsed)[:8]))
            deps.emit("phase_c.dispatch.pair_withhold_lapsed", data={
                "cluster": cluster.name, "iter": iter_n,
                "tests": sorted(_pair_lapsed),
                "unpaid_buys": {t: _pair_unpaid.get(t, 0)
                                for t in sorted(_pair_lapsed)},
                "unopened_fills": {t: _pair_unopened.get(t, 0)
                                   for t in sorted(_pair_lapsed)},
                "grace": DEFAULT_UNPAID_GRACE,
                "still_withholding": sorted(_paired_tests),
            })
        if _cov is not None and _owners_read:
            _owner_admits = escalate_to_measured_owners(
                owners={bare_test_name(t): frozenset(
                    FnLocator(s.file, s.fn_name) for s in ks)
                    for t, ks in _owners_read.items()},
                units_by_test=_units_for_owners,
                graded=_flat_keys | permanently_exhausted,
                unable=_dead_keys | _red_dead,
                already_owned=frozenset(eligible),
                permanently_exhausted=permanently_exhausted,
                served=frozenset(_served_at),
                paired_tests=_paired_tests,
            )
        if _owner_admits:
            _by_test: dict[str, dict[FnLocator, set[str]]] = {}
            for _loc, _ts in _owner_admits.items():
                eligible.setdefault(_loc, set()).update(_ts)
                for _t in _ts:
                    _by_test.setdefault(_t, {}).setdefault(_loc, set()).add(_t)
            for _tname, _cached_add in _by_test.items():
                if _tname in escalation_products:
                    _earned, _cached = escalation_products[_tname]
                    for _loc, _ts in _cached_add.items():
                        _cached.setdefault(_loc, set()).update(_ts)
                else:
                    escalation_products[_tname] = (
                        frozenset(_units_for_owners.get(_tname, ())),
                        dict(_cached_add),
                    )
                escalation_producers[_tname] = frozenset(
                    escalation_producers.get(_tname, ())) | frozenset(
                        _cached_add)
            _have = {k for k, _ in ordered}
            for _loc in _owner_admits:
                if _loc not in _have:
                    ordered.append((_loc, eligible[_loc]))
            _esc_keys = frozenset(
                k for ks in escalation_producers.values() for k in ks)
            deps.log(
                f"  {tag} [phase-c/dispatch] measured-owners: "
                f"{len(_owner_admits)} unit(s) admitted from the owner map "
                "(every attributed-or-escalated unit served or dead): "
                + ", ".join(sorted(
                    f"{k.file}::{k.fn_name}" for k in _owner_admits)[:12]))
            deps.emit("phase_c.attribution.measured_owners", data={
                "cluster": cluster.name, "iter": iter_n,
                "added": sorted(f"{k.file}::{k.fn_name}"
                                for k in _owner_admits),
                "tests": sorted(_by_test),
                "owners": {
                    t: sorted(f"{k.file}::{k.fn_name}" for k in ks)
                    for t, ks in sorted(
                        (t, set(d)) for t, d in _by_test.items())},
            })
        # RED-SLICE SESSIONS. The failing set is the unit of evidence, not
        # the coverage-key subset: iterating owners.items() planned no
        # session for cfg-gated tests an instrumented run omitted (run
        # 216855-03b2a21b, 2026-09-02, nightly iters 4-9: owner map was ui
        # with 0 units). Coverage owners union attributed units, then the
        # formatter pair hop. plan_slices still drops empty and hollow.
        # The per-unit admission above still stands (the slice units are
        # frontier, and a session that does not run leaves them to the
        # other routes); the engine drops what a session served from the
        # same call's other routes.
        _session_slices: dict[str, frozenset[FnSite]] = {}
        _session_owners: dict[str, frozenset[FnSite]] = {}
        if os.getenv("SLICE_SESSION", "1") != "0":
            # Measured owners already carry containers. Recorded locators
            # join only when the site frontier names exactly one container;
            # `_owners_read` still has the empty-container stamp for ranking
            # and must not enter this map (expand_locator_sites would
            # re-admit Display).
            _session_from_cov: dict[str, set[FnSite]] = {}
            if _cov is not None:
                for t, ks in _cov.owners.items():
                    _session_from_cov.setdefault(
                        bare_test_name(t), set()).update(ks)
            for t, ks in _recorded_sites.items():
                _session_from_cov.setdefault(
                    bare_test_name(t), set()).update(ks)
            _cov_owners = {t: frozenset(ks) for t, ks in _session_from_cov.items()}
            _session_owners = dict(_cov_owners)
            # Eligible is the filter, not the source. Walking eligible and
            # inserting FnSite(file, fn, "") when the site frontier had no
            # container made expand_locator_sites name every same-named
            # body, so the coverage slice re-admitted Display and the
            # merge exceeded the cap (run 110993-6efa23be, 2026-09-03).
            # Classifier / reverse-map / lift_sites still put locator-only
            # sites in the frontier; those still expand. Coverage owners
            # stay a separate argument (executed path, already sites).
            _units_for_session = attributed_units_for_session(
                eligible, _attr.site_frontier)
            _session_slices = session_slices_for_failing(
                cur_failed,
                owners=_cov_owners,
                units_by_test=_units_for_session,
                pairs=((p.dispatcher, p.implementer) for p in _attr.pairs),
                paired_tests=_paired_tests)
        _before_svc = [k for k, _ in ordered]
        # The ranking reads the VERIFIED service view, not the fire's
        # inclusive `_served_at`: a floor stamp the route-scoped rule never
        # earned kept `never_served` at [] on 24 of 25 rankings after the
        # decode fix shipped, so never-attempted units could not rank first.
        # A still-stubbed unit is never-served whatever its stamp says: the
        # stamp is about a body that is no longer on disk (`service_bucket`).
        ordered = promote_by_service(
            ordered, set(_served_verified), _flat_keys, _dead_keys, _red_dead,
            hollow=_stub_keys)
        _ranked = [k for k, _ in ordered]
        _never = [k for k in _ranked
                  if (k in _stub_keys or k not in _served_verified)
                  and k not in _dead_keys and k not in _red_dead]
        _flat_n = sum(1 for k in _ranked if k in _flat_keys
                      and k not in _dead_keys and k not in _red_dead)
        _rd_only = _red_dead - _dead_keys
        # RE-BUY DETECTOR, on the ranked frontier, BEFORE the cut.
        # Run `9933-efe68f37` computed this after `take_batch`, so the
        # reading named only units already inside the cap. A ranking
        # bucket that would sink a unit a failing test enters while an
        # unentered unit occupies the cap is refused (computed on that
        # run's service-order prefix: sinking `context`/`msg` leaves the
        # nine never-served and pulls `downcast`/`render`). Detector
        # only: nothing is demoted, removed, or exhausted.
        _rebuy: tuple[tuple[FnLocator, LedgerEntry], ...] = ()
        if not _code_moved:
            _rebuy = rebuy_hits(
                _ranked, latest_by_unit(_ledger_entries()),
                project=project_dir.name, cluster=cluster.name,
                run_id=deps.run_id, oracle_fp=_oracle_fp(iter_n))
        _rebuy_names = [f"{u.file}::{u.fn_name}" for u, _ in _rebuy]
        _order_moved = _ranked != _before_svc
        if _order_moved:
            deps.log(
                f"  {tag} [phase-c/dispatch] service order: "
                f"{len(_never)} never-served unit(s) rank before served; "
                f"{_flat_n} last-graded flat sink"
                + (f"; {len(_dead_keys)} dead body(ies) demoted — "
                   "executed by no test: "
                   + ", ".join(sorted(f"{k.file}::{k.fn_name}"
                                      for k in _dead_keys)[:6])
                   if _dead_keys else "")
                + (f"; {len(_rd_only)} red-dead body(ies) demoted — "
                   "alive, but executed by no FAILING test: "
                   + ", ".join(sorted(f"{k.file}::{k.fn_name}"
                                      for k in _rd_only)[:6])
                   if _rd_only else ""))
        if _order_moved or _rebuy:
            deps.emit("phase_c.dispatch.service_order", data={
                "cluster": cluster.name, "iter": iter_n,
                "never_served": [f"{k.file}::{k.fn_name}" for k in _never],
                "flat": sorted(f"{k.file}::{k.fn_name}" for k in _flat_keys),
                "dead": sorted(f"{k.file}::{k.fn_name}" for k in _dead_keys),
                "red_dead": sorted(f"{k.file}::{k.fn_name}"
                                   for k in _rd_only),
                "rebuy": _rebuy_names,
            })
        if _rebuy:
            # Split by the standing grade, not FLAT vs everything else: an
            # UNMEASURED row says the tests did not run; a PROVEN empty-delta
            # row does not. Run `2965-453dd7a4` iter 1's 16 hits were 4 FLAT
            # and 12 UNMEASURED, so a not-FLAT cut happened to print the
            # right sentence; the detector's own prior-process row is PROVEN.
            _by_grade = rebuy_by_grade(_rebuy)
            _rb_flat = _by_grade.get("flat", ())
            _rb_unm = _by_grade.get("unmeasured", ())

            def _names(locs: Sequence[FnLocator]) -> list[str]:
                return [f"{u.file}::{u.fn_name}" for u in locs]

            _grade_bits = []
            if _rb_flat:
                _grade_bits.append(f"{len(_rb_flat)} graded flat")
            if _rb_unm:
                _grade_bits.append(
                    f"{len(_rb_unm)} unmeasured — tests did not run")
            for _st, _locs in sorted(_by_grade.items()):
                if _st in ("flat", "unmeasured"):
                    continue
                _grade_bits.append(f"{len(_locs)} {_st}")
            _grade_clause = ", ".join(_grade_bits) if _grade_bits else "ungraded"
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: RE-BUY — "
                f"{len(_rebuy)} unit(s) a prior process already tried under "
                f"this oracle with an empty verdict delta ({_grade_clause}): "
                + ", ".join(f"{u.file}::{u.fn_name}" for u, _ in _rebuy[:8])
                + (f" (+{len(_rebuy) - 8} more)" if len(_rebuy) > 8 else ""))
            deps.emit("phase_c.re_buy", data={
                "cluster": cluster.name, "iter": iter_n,
                "units": _rebuy_names,
                "flat": _names(_rb_flat),
                "unmeasured": _names(_rb_unm),
                "grades": {st: _names(locs) for st, locs in _by_grade.items()},
                "prior_run_id": _rebuy[0][1].run_id})
        # Crash evidence outranks everything, including the producer order above.
        ordered = promote_crash_evidence(ordered, _crash_grades)
        # FLAT-UNDER-UNCHANGED-EVIDENCE IS EXCLUSION, not a sink rank.
        # `promote_by_service` parks flats last; the cap still spends on
        # them once never-served is empty. `rebuy_hits` cannot close that:
        # it is off after a code move and skips this run_id, so the
        # same-process re-buy (the `9872` spend) is invisible to it.
        # Crash-blamed units stay — live evidence outranks a prior empty
        # delta.
        # A still-hollow body is never excluded, whatever the ledger says
        # about it: first-fill ranks the live-stub producers first for a
        # reason (run `155810-cbb3b9fc`), and a stub's standing grade is a
        # claim about bytes that are not the body the batch would write.
        _before_excl = [key for key, _ in ordered]
        ordered = drop_standing_flat(
            ordered, _flat_keys, exempt=_crash_grades.keys() | _stub_keys)
        _kept_after_excl = {key for key, _ in ordered}
        _excluded = [k for k in _before_excl if k not in _kept_after_excl]
        if _excluded:
            # The exclusion is a spend decision and has to be readable as
            # one: without this line the only trace was a smaller `flat`
            # count on the service-order line, which cannot be told from a
            # frontier that never held the unit (runs 358833 → 216855,
            # 2026-09-01/02, read `flat` 1–3 with nothing naming the
            # excluded set).
            _excl_fp = {
                f"{k.file}::{k.fn_name}": _evidence_of.get(
                    (k.file, k.fn_name), "")
                for k in _excluded}
            _excl_names = sorted(_excl_fp)
            deps.log(
                f"  {tag} [phase-c/dispatch] iter {iter_n}: {len(_excluded)} "
                "unit(s) EXCLUDED from the batch — standing FLAT under "
                "evidence unchanged since the grade (same oracle, same body, "
                "same convicting set, and the same prompt evidence: file, "
                "retrospective rows, crash excerpt); a refusal to spend, not "
                "a sink rank: "
                + ", ".join(f"{n} (evidence {_excl_fp[n]})"
                            for n in _excl_names[:8])
                + (f" … (+{len(_excl_names) - 8})" if len(_excl_names) > 8 else ""))
            deps.emit("phase_c.dispatch.flat_excluded", data={
                "cluster": cluster.name, "iter": iter_n,
                "units": _excl_names,
                "evidence_fp": _excl_fp,
                "remaining": len(ordered),
            })
        if _pass_cohort is None:
            _pass_cohort = tuple(key for key, _ in ordered)
        # The promotion above is worthless unless the cursor is told to let
        # these through: `take_batch` subtracts `attempted` FIRST, so a graded
        # unit offered once earlier in the pass is dropped before its rank is
        # ever consulted (see `take_batch`'s `exempt`). SITE evidence only:
        # the chain-only cohort keeps its rank at the head of `ordered` but
        # walks the cursor, so a 63-unit inference is traversed across
        # batches instead of its first 12 being re-bought every sweep.
        _batch = take_batch([key for key, _ in ordered], _pass_attempted,
                            _dispatch_cap, cohort=_pass_cohort,
                            exempt=_crash_cohort.cursor_exempt)
        if _crash_grades:
            _aimed = [k for k in _batch.keys if k in _crash_grades]
            _n_site = len(_crash_cohort.cursor_exempt)
            _n_chain = len(_crash_cohort.chain_only)
            # When none are aimed the reason can no longer be rank — crash
            # promotion runs last and outranks everything above it, so there is
            # no such thing as "higher-graded evidence". Saying there was is
            # how this survived five processes unread: the line named a cause
            # that cannot exist, and every session that read it moved on. The
            # only remaining explanation is that the graded units are not in
            # this sweep's frontier at all.
            _off = [k for k in _crash_grades if k not in set(_batch.keys)]
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: "
                f"{len(_crash_grades)} unit(s) carry crash evidence this "
                f"sweep ({_n_site} frame-sited, re-offered within the pass; "
                f"{_n_chain} chain-inferred, cursor-bounded) and rank ahead "
                f"of the frontier; {len(_aimed)} of them "
                "are in this batch"
                + (": " + ", ".join(f"{k.file}::{k.fn_name}"
                                    for k in _aimed[:8]) if _aimed else
                   " — NONE, and not because anything outranks them: they are "
                   "absent from this sweep's frontier (withheld as exhausted, "
                   "or no longer a fill target): " + ", ".join(
                       f"{k.file}::{k.fn_name}" for k in _off[:8])))

        # UNDISPATCHABLE IS A PROPERTY OF THE BATCH, NOT OF THE FRONTIER —
        # and the terminator above could only see the frontier. Every one of
        # the loop's own refusals empties the batch while leaving the
        # frontier populated: the flat exclusion a few lines up, the pass
        # cursor once a traversal is complete, a route cap. Run
        # `837207-ce5b1647` (2026-09-15) read `frontier=3 fn(s)` with
        # `dispatching 0 of 0 (0/0 this pass)` at iterations 3, 4 and 5, so
        # `if not eligible` never fired, the streak was cleared on every one
        # of those iterations, and `TDD_NO_FRONTIER_STOP=2` was structurally
        # unreachable — no `no actionable frontier` line appears anywhere in
        # the run, which went on re-buying one route for three more hours and
        # ended on an external TERM rather than on any stop of its own.
        #
        # `improved` clears the streak as well as a dispatch does, because a
        # route that CASHED is progress whatever the batch held: the
        # condition is "no unit was dispatched and no route cashed", not "the
        # batch was empty". And the stop cannot cost a reading — it needs two
        # consecutive iterations that dispatched nothing and moved nothing, a
        # state in which no verdict can move, so the sweep it would prevent
        # is a sweep of the same tree under the same evidence.
        if _batch.keys or improved:
            _no_frontier_streak = 0
        else:
            _no_frontier_streak += 1
            _held = set(frontier) & permanently_exhausted
            _spin_regime = plateau_exit_disabled(max_stalls)
            _empty_why = (
                f"frontier={len(eligible)} unit(s) but the batch is empty — "
                f"{len(_flat_keys)} standing FLAT under unchanged evidence, "
                f"{_batch.attempted_before} already attempted this pass, "
                f"{len(_held)} held as exhausted")
            if _spin_regime:
                _reopen, _disk = _spend_no_move_readmission(
                    f"TDD iter {iter_n}: nothing dispatched and nothing "
                    f"cashed — no other move (re-admission "
                    f"{_no_move_readmissions + 1}/{_reset_cap})")
                if _reopen:
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: nothing "
                        f"dispatchable and no other move ({_empty_why}) — "
                        f"re-admitting {len(_reopen)} unit(s) "
                        f"({len(_disk)} stored verdict(s)) for one more "
                        f"attempt ({_no_move_readmissions}/{_reset_cap}; "
                        f"pass={pass_count}, fail={fail_count}): "
                        + ", ".join(sorted(
                            f"{k.file}::{k.fn_name}" for k in _reopen)[:8]))
                    deps.emit("phase_c.tdd.no_move_readmission", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "held": sorted(f"{k.file}::{k.fn_name}"
                                       for k in _held),
                        "disk_released": sorted(_disk),
                        "attempt": _no_move_readmissions,
                        "cap": _reset_cap,
                        "why": _empty_why,
                    })
                    deps.emit("phase_c.tdd.iter", data={
                        **_prov,
                        "cluster": cluster.name, "iter": iter_n, "rc": rc,
                        "frontier_size": len(eligible),
                        "kind": "no_batch_readmit",
                        "pass_count": pass_count, "fail_count": fail_count,
                        "improved": improved, "stall": stall,
                    })
                    _score_grace(iter_n, pass_count)
                    continue
            if (_spin_regime and _no_frontier_stop_after > 0
                    and _no_frontier_streak >= _no_frontier_stop_after):
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: NO_FRONTIER_STOP — "
                    f"{_no_frontier_streak} consecutive iteration(s) "
                    f"dispatched no unit and cashed nothing ({_empty_why}; "
                    f"{fail_count} test(s) still failing) and the "
                    "no-other-move re-admission budget is spent "
                    f"({_no_move_readmissions}/{_reset_cap}). Reading "
                    f"pass={pass_count} fail={fail_count}. Stopping the RUN "
                    f"instead of spinning to the {_tdd_backstop}-iteration "
                    "backstop. Nothing is convicted and every fill is on "
                    "disk — `bash continue.sh` resumes here; "
                    "TDD_NO_FRONTIER_STOP=0 disables this stop.")
                _log_attribution_gap(deps, tag, iter_n, fail_count,
                                     cur_failed, len(frontier), kind)
                deps.emit("phase_c.tdd.no_frontier_stop", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count, "fail_count": fail_count,
                    "frontier_size": len(frontier),
                    "held_exhausted": len(_held),
                    "streak": _no_frontier_streak,
                    "readmissions": _no_move_readmissions,
                    "cap": _reset_cap,
                    "why": _empty_why,
                })
                deps.emit("phase_c.tdd.iter", data={
                    **_prov,
                    "cluster": cluster.name, "iter": iter_n, "rc": rc,
                    "frontier_size": len(eligible),
                    "kind": "no_batch_stop",
                    "pass_count": pass_count, "fail_count": fail_count,
                    "improved": improved, "stall": stall,
                })
                request_graceful_stop()
                break
            # Not in the spin regime, or the stop is disabled: the routes
            # below may still have work the batch does not express (a pair,
            # a slice session), so this falls through to the fill rather
            # than re-running a cached sweep. The streak stands, so a
            # second such iteration reaches the stop above.
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: no unit dispatched "
                f"and nothing cashed ({_empty_why}); streak "
                f"{_no_frontier_streak}/{_no_frontier_stop_after or 0} — "
                "the fill's routes still run.")

        # GPU protector, half 2 of 2 — the only thing below this line that costs
        # GPU is the dispatch, and this reading says the last one bought nothing
        # the crate can show. Deliberately NOT a plateau exit: no unit is
        # convicted, no exhaustion flag is written, no tree is restored and
        # `pipeline_state.md` still reads `current: task2a`, because the guard
        # judges the SPEND, not the units — a conviction written here would
        # travel to the next run as evidence nobody produced. It stops the whole
        # run rather than this cluster: a plateau exit hands control to the merge
        # and the merged-tree fill, which spend the GPU this exists to protect.
        # `request_graceful_stop` sets the process-wide event the operator's
        # sentinel sets, so the exit that follows is the one every worker pool
        # already knows how to take.
        # THE PRECONDITION, REPORTED. A guard that is silent when it does not
        # trip cannot be told apart from a guard that has nothing to say, and
        # that is how run `1692702-94e6163a` spent 3h36m with its repeat clause
        # satisfied since iteration 2 and its traversal clause unsatisfiable.
        # One line per sweep, only while the guard is actually withheld.
        _flat_verdict = _flat_decision()
        if (not _flat_excuse and _flat_verdict is not None
                and _flat_verdict.withheld_by_traversal):
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: the flat-score guard is "
                f"ARMED and WITHHELD — {_flat_reading} has been read "
                f"{_flat_verdict.repeats} time(s) (limit "
                f"{_flat_verdict.limit}) but all of them inside traversal "
                f"{_traversals}, and a stop needs one completed traversal "
                "behind the repeat. This pass has attempted "
                f"{_batch.attempted_before + len(_batch.keys)} of "
                f"{len(_pass_cohort or ())} cohort unit(s). The plateau budget "
                f"({stall}/{max_stalls}) charges on the same condition.")
            deps.emit("phase_c.tdd.flat_score_withheld", data={
                "cluster": cluster.name, "iter": iter_n,
                "pass_count": pass_count,
                "oracle_size": _measure.oracle_size,
                "repeats": _flat_verdict.repeats,
                "traversals": _traversals,
                "cohort_size": len(_pass_cohort or ()),
                "attempted_this_pass": (_batch.attempted_before
                                        + len(_batch.keys)),
                "stall": stall})
        # TWO CLOCKS, ONE STOP. The second clock (`UnmovedVerdicts`) asks a
        # question no frontier bookkeeping can disable — have the last `limit`
        # judged sweeps landed bodies and moved not one test's verdict — and on
        # the run it was written for that was true from iteration 3 while every
        # traversal-denominated terminator sat at zero. It routes through THIS
        # branch rather than getting its own: the durable-tree rescue below is
        # the one thing that should happen before any stop, and a second exit
        # that skipped it would be a second way to end a run on a tree worse
        # than one already on disk.
        _stop_clock = (
            "flat_score" if (not _flat_excuse and _flat_verdict is not None
                             and _flat_verdict.stop)
            else "no_verdict" if _unmoved.trips else "")
        if _stop_clock:
            # BEFORE stopping: is there a better tree on disk that this run has
            # not tried? A flat reading is the loop saying its re-climb has
            # failed, and the durable mark is the one number that survives the
            # process boundary the re-base destroys — every instance of this
            # crossed one, and four consecutive runs each re-derived a baseline
            # from the tree they inherited. Measured over runs
            # `1082156-4da4faf7` → `669347-cf33eae3` (2026-08-14/15): a
            # retrigger traded a reproducible 79/101 for two tests that have
            # never passed in any run, eight of the next twelve scored sweeps
            # read exactly 39, and no mechanism could take the 79 back.
            #
            # Once per process. A restore that does not help must not become a
            # loop, and the guard behind it still stops the run on the sweep
            # after — with the restored tree in place, which is the better
            # place to stop.
            _mark_now = (deps.load_best_tree_mark(project_dir, cluster.name)
                         if deps.load_best_tree_mark is not None else None)
            if (not _restore_spent and deps.restore_best_tree_pair is not None
                    and _mark_now is not None
                    and _mark_now.pass_count > pass_count):
                _restored = deps.restore_best_tree_pair(
                    project_dir, cluster.name, target_src_dir)
                if _restored is not None:
                    _restore_spent = True
                    _bodies, _layout_n, _leftover = _restored
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: the re-climb "
                        f"failed — this sweep reads {_flat_reading} for the "
                        f"{_flat_verdict.repeats if _flat_verdict else 0}"
                        "(nd/rd) time "
                        f"and the durable mark is {_mark_now.pass_count}/"
                        f"{_mark_now.oracle_size}. RESTORING that tree "
                        f"({_bodies} body file(s) + {_layout_n} layout "
                        "file(s), the pair). THE COST, stated: every fill "
                        "landed since the mark is discarded"
                        + (f"; {len(_leftover)} file(s) present here and not "
                           f"in the snapshot are left in place ({_leftover[:3]})"
                           if _leftover else "")
                        + ". The guard is now spent for this process: if the "
                        "restored tree also goes flat, the run stops.")
                    deps.emit("phase_c.tdd.best_tree_restored", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "pass_count": pass_count,
                        "oracle_size": _measure.oracle_size,
                        "mark_pass": _mark_now.pass_count,
                        "mark_oracle": _mark_now.oracle_size,
                        "bodies": _bodies, "layout_files": _layout_n,
                        "leftovers": _leftover[:20]})
                    # Arm the unmeasured-restore tripwire: the next sweep must
                    # be able to SEE a >0-file replacement, and this sweep's
                    # verdict pairs are what "did not see it" looks like.
                    _restore_probe = (_measure.passed_pairs,
                                      _measure.failed_pairs, _bodies)
                    # Every consumer of the score, told in the same breath —
                    # the tree under all of them was just replaced. The guard's
                    # window holds readings of a tree that no longer exists;
                    # the convictions in `permanently_exhausted` were earned
                    # against bodies that are gone; the stub set is the old
                    # tree's; the traversal in progress was planned against it.
                    #
                    # AND THE HIGH-WATER, which an earlier version of this block
                    # argued needed no reset because "the restored tree scores
                    # HIGHER, so they read an improvement". That is true against
                    # THIS sweep — the branch is guarded on
                    # `_mark_now.pass_count > pass_count` — and not against
                    # `best_pass`, which is a monotone maximum. Where a
                    # retrigger re-based first (79 → re-stub → 32 → 39 → restore)
                    # it happens to hold; where the drop was an ordinary
                    # regression (79 → 75 → 75 → restore) the post-restore sweep
                    # reads 79 against a `best_pass` of 79, so `improved` is
                    # False, `readmission_warranted`'s
                    # `best_metric > metric_at_last_reset` is unsatisfiable, and
                    # the loop sits in the deadlock `domain/pass_highwater.py`
                    # exists to prevent. A restore is a deliberate replacement of
                    # the lineage exactly as a re-stub is, so it is recorded the
                    # same way and the next sweep re-bases through the one path
                    # every consumer already reads.
                    _highwater = _highwater.lineage_replaced(iter_n)
                    # The restore replaced the lineage, so the readings before
                    # it describe a tree the loop no longer has. Clearing the
                    # FACTS is the same statement the guard's re-construction
                    # used to make, with one fewer way to get it wrong.
                    _flat_facts.clear()
                    permanently_exhausted.clear()
                    _stubs_prev = -1
                    _pass_attempted = set()
                    _pass_landed = set()
                    _pass_declined = set()
                    _pass_escalated = set()
                    _pass_opened = set()
                    _pass_reoffered = set()
                    _pass_cohort = None
                    _resumed_cursor_window = frozenset()
                    _unmoved = _unmoved.moved()
                    _prev_pass_completed = True
                    _zp_history, _zp_planned, _zp_fired_on = (), None, None
                    # AND THE RE-FILL WINDOW. Its grant describes the tree the
                    # restore just replaced — `restubbed`, `fills_at_grant` and
                    # `vanished_at_grant` are all readings of a lineage that no
                    # longer exists, and left open it keeps the ratchet, the
                    # stall budget and the crash streak standing down over a
                    # tree that holds no re-stub, until a regained-mark close
                    # that needs `mark >= highwater_at_grant` or the wall
                    # clock. This combination became reachable when the flat
                    # guard stopped excusing graced sweeps: a trip — and so a
                    # restore — can now happen inside an open window.
                    if _grace.active and _grace.restubbed:
                        deps.log(
                            f"  {tag} [phase-c/tdd] iter {iter_n}: re-fill "
                            f"window closed ({_grace.restubbed} body(ies) "
                            "re-stubbed) — the tree it was granted against was "
                            "replaced by the best-tree restore.")
                        deps.emit("phase_c.tdd.refill_window_closed", data={
                            "cluster": cluster.name, "iter": iter_n,
                            "restubbed": _grace.restubbed,
                            "fills_this_iter": 0,
                            "iters_unspent": _grace.iters_left,
                            "early_close": "tree replaced by best-tree restore",
                        })
                    _grace = RefillGrace.none()
                    continue
            if _stop_clock == "flat_score" and _flat_verdict is not None:
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: GPU_GUARD_STOP — "
                    f"this sweep reads {_flat_reading}, which the last "
                    f"{len(_flat_verdict.observations)} judged sweep(s) have "
                    f"already produced {_flat_verdict.repeats} time(s): "
                    f"{_flat_verdict.render()}"
                    + (f"; {_flat_verdict.gap} sweep(s) in between were not "
                       "judged (grace window / still filling / red build)"
                       if _flat_verdict.gap else "")
                    + f". {_traversals} completed traversal(s) of this "
                    "frontier stand behind that reading. Stopping the RUN "
                    f"before dispatching {_batch.rendered} — the batch it was "
                    "about to spend, out of the frontier it was drawn from. "
                    "Nothing is convicted and every fill is on disk — `bash "
                    "continue.sh` resumes here.")
                deps.emit("phase_c.tdd.flat_score_stop", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count,
                    "oracle_size": _measure.oracle_size,
                    "repeats": _flat_verdict.repeats,
                    "limit": _flat_verdict.limit,
                    "window": _flat_verdict.window,
                    "skipped": _flat_verdict.gap,
                    "readings": [[o.reading.pass_count, o.reading.oracle_size,
                                  o.traversal]
                                 for o in _flat_verdict.observations],
                    "traversals": _traversals,
                    "frontier_size": len(eligible),
                    "withheld_dispatch": len(_batch.keys),
                })
            else:
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: NO_VERDICT_STOP — "
                    f"{_unmoved.streak} consecutive judged sweep(s) landed "
                    f"{_unmoved.landed} body(ies) between them and moved NO "
                    "test's verdict; the per-binary vector is byte-identical "
                    f"across all of them. Reading {_flat_reading}, "
                    f"{_traversals} completed traversal(s) — this stop does "
                    "NOT depend on that number, which is the point of it. "
                    f"Stopping the RUN before dispatching {_batch.rendered}. "
                    "Nothing is convicted and every fill is on disk — `bash "
                    "continue.sh` resumes here.")
                deps.emit("phase_c.tdd.no_verdict_stop", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count,
                    "oracle_size": _measure.oracle_size,
                    "streak": _unmoved.streak, "limit": _unmoved.limit,
                    "landed": _unmoved.landed,
                    "traversals": _traversals,
                    "frontier_size": len(eligible),
                    "withheld_dispatch": len(_batch.keys),
                })
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible),
                "kind": f"{_stop_clock}_stop",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            request_graceful_stop()
            break

        # COMMIT the batch decided above. The slice keeps the ranking untouched
        # — it is a prefix of what this pass has not attempted yet — so a unit's
        # dispatch ORDER is what it always was; only the point at which the loop
        # stops to measure moves. The cursor advances here and not at the
        # decision, so an iteration that stopped or restored above leaves the
        # traversal exactly where it found it.
        # Captured BEFORE the cursor reset below: when this batch closes the
        # pass, the audit after the fill phase reports the WHOLE traversal —
        # everything the pass dispatched (across relaunches, via the resumed
        # cursor), not just this batch.
        # Served is NOT stamped here: dispatch is the cursor, fill-return is
        # service. A kill mid-fill must leave these keys attempted but not
        # served (run `308884` persisted dispatch-time served; resume `2932`
        # fired `test_altdebug` at iter 1).
        _pass_units_at_close = _pass_attempted | set(_batch.keys)
        _cohort_at_close = len(_pass_cohort or ())
        _cohort_before_close = _pass_cohort
        _closed_fact = None
        _dispatched_before_sweep = len(_batch.keys)
        _pass_attempted = (set() if _batch.completes_pass
                           else _pass_attempted | set(_batch.keys))
        # A closed pass releases its cohort; the next iteration opens one over
        # the frontier as it stands then. Units that arrived during this pass
        # are in that next cohort — they were dispatchable all along, they just
        # did not extend the traversal they arrived in.
        _pass_cohort = None if _batch.completes_pass else _pass_cohort
        _prev_pass_completed = _batch.completes_pass
        if _batch.completes_pass:
            _traversals += 1
            # The traversal, as a fact — `flat_score` counts pass closures out
            # of the stream rather than being handed a number by a local, so a
            # gate that both counts and decides stops existing.
            _closed_fact = PassClosed(
                iter=iter_n, cluster=cluster.name,
                cohort_size=_cohort_at_close,
                attempted=_batch.attempted_before + len(_batch.keys))
            _flat_facts.append(_closed_fact)
        # The cursor is on disk BEFORE the fill phase it describes: a stop
        # or a kill inside the hours that follow leaves a floor that says
        # which units this pass already spent a model call on, so the
        # relaunch takes the next slice instead of re-sending this one
        # (`_floor_snapshot`). Same guard as the boundary save above; the
        # score halves are unchanged since it, only the cursor moved.
        # Served is whatever fill-return has already stamped — not this batch.
        _persist_floor()
        frontier_keys = set(_batch.keys)
        _cls_bit = (f" {_prev_fill_classes.rendered()}"
                    if _saw_fill else "")
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test rc={rc} "
             f"({dt:.1f}s); pass={pass_count} fail={fail_count} "
             f"oracle={_measure.oracle_size} "
             f"(improved={improved}, stall={stall}/{max_stalls}); "
             f"frontier={len(eligible)} fn(s) ({kind}), "
             f"{skipped_perm} skipped as exhausted; "
             f"unowned={len(_unowned)}; "
             f"dispatching {_batch.rendered}.{_cls_bit}")
        # The ordered frontier this iteration walked, as one digest. It is a
        # DIAGNOSTIC and never a gate: a hash of the population cannot decide
        # whether a carried cursor is valid, because the population moves for
        # the very reason the cursor exists (bodies land, units leave, the
        # escalation widens the frontier 12 -> 39 mid-pass) and a resumed
        # process re-opens its cohort on whatever the frontier is then. What
        # it is for is reading a stop-and-relaunch pair afterwards —
        # `scripts/cursor_identity_replay.py` can then say what the population
        # actually did across a transition instead of inferring it from which
        # module changed.
        _frontier_fp = hashlib.sha256(
            "\n".join(f"{k.file}::{k.fn_name}" for k, _ in ordered)
            .encode("utf-8")).hexdigest()[:12]
        deps.emit("phase_c.tdd.iter", data={
                **_prov,
            "cluster": cluster.name, "iter": iter_n, "rc": rc,
            "frontier_size": len(eligible), "kind": kind,
            # The four fields the printed line carries and the event did not.
            # Measured 2026-08-21 by `scripts/fold_completeness.py`: a fold
            # over the recorded stream could rebuild every field of the iter
            # vector EXCEPT the sweep's elapsed seconds and the exhausted-skip
            # count, so the log and the fact stream disagreed about what the
            # loop knew. A fold that reads a default where the variable would
            # have read the truth is worse than the variable.
            "elapsed_s": round(dt, 1),
            "skipped_exhausted": skipped_perm,
            "attempted_before": _batch.attempted_before,
            "cohort_size": len(_pass_cohort or ()),
            "frontier_fp": _frontier_fp,
            "cohort_fp": hashlib.sha256(
                "\n".join(f"{k.file}::{k.fn_name}"
                          for k in (_pass_cohort or ())).encode("utf-8")
            ).hexdigest()[:12],
            "pass_count": pass_count, "fail_count": fail_count,
            "improved": improved, "stall": stall,
            "dispatched": len(_batch.keys),
            "pass_completed": _batch.completes_pass,
            "unowned": len(_unowned),
            "binaries": [[lbl, p, f] for lbl, p, f in _bins],
            "build_red": _build.red,
            "build_missing": sorted(_build.missing),
            "build_failed_targets": list(_build.failed_targets),
            "fill_classes": (
                _prev_fill_classes.as_dict() if _saw_fill else None),
        })

        # Shadow zero-payoff reading: judge what the PREVIOUS sweep's dispatch
        # bought, then record this sweep's plan. Grace sweeps neither judge
        # nor plan — their deltas measure the retrigger's deliberate re-stub.
        # The AIM, not the slice. This was built from `frontier_keys` — the
        # dispatched BATCH — which under a dispatch cap rotates every sweep, so
        # the identity rule below could not hold three sweeps running no matter
        # how stuck the loop was. `eligible` is what the sweep decided to go
        # after; the batch is only how much of it fits in one iteration.
        _zp_targets = frozenset(f"{k.file}::{k.fn_name}" for k in eligible)
        _zp_vanished = frozenset(_oracle.vanished)
        # The per-binary verdict vector, which is the axis that survives a
        # frontier the loop is busily re-planning: unchanged exactly when no
        # test anywhere took a different verdict. Six of run
        # `1064759-0f23fb68`'s seven sweeps carried a byte-identical one.
        _zp_bins = tuple((lbl, p, f) for lbl, p, f in _bins)
        if _grace.bodies_in_flight:
            _zp_planned = None
            # The loop deleted work on purpose since the last judged sweep, so
            # the NEXT judged sweep's frontier move is the loop's own doing and
            # is allowed to count. Anywhere else it is re-planning.
            _zp_restub_since = True
        else:
            if _zp_planned is not None:
                _p_targets, _p_pass, _p_vanished, _p_bins = _zp_planned
                _zp_history = (*_zp_history[-7:], SweepOutcome(
                    targets=_p_targets,
                    paid=paid_off(pass_count - _p_pass,
                                  _zp_vanished != _p_vanished,
                                  _zp_bins != _p_bins,
                                  _zp_targets != _p_targets,
                                  restub_between=_zp_restub_since)))
                _zp_hit = zero_payoff_streak(_zp_history, window=_zp_window)
                # Two rules, one report. The strict one asks whether the loop
                # re-aimed at the IDENTICAL set and got nothing; the loose one
                # asks only whether it got nothing, because a capped dispatch
                # rotates its slice and can stand still for hours without ever
                # repeating an aim exactly.
                _zp_unpaid = unpaid_streak(_zp_history, window=_zp_window)
                _zp_now = _zp_hit if _zp_hit is not None else (
                    _zp_targets if _zp_unpaid is not None else None)
                if _zp_now is not None and _zp_now != _zp_fired_on:
                    _zp_fired_on = _zp_now
                    _zp_rule = ("the identical "
                                f"{len(_zp_now)}-fn frontier"
                                if _zp_hit is not None
                                else f"a rotating slice of a {len(_zp_now)}-fn "
                                "frontier")
                    deps.log(
                        f"  {tag} [phase-c/zero-payoff] SHADOW: "
                        f"{_zp_unpaid or _zp_window} judged sweep(s) "
                        f"dispatched {_zp_rule} and none moved the pass "
                        "count, the crash class or any test's verdict — the "
                        "class-general stall condition (scripts/"
                        "pattern_census.py). Shadow mode: logged, no action.")
                    deps.emit("phase_c.tdd.zero_payoff_shadow", data={
                        "cluster": cluster.name, "iter": iter_n,
                        "targets": sorted(_zp_now)[:40],
                        "target_count": len(_zp_now),
                        "rule": ("identical_aim" if _zp_hit is not None
                                 else "unpaid_streak"),
                        "unpaid_sweeps": _zp_unpaid or 0})
                elif _zp_now is None:
                    _zp_fired_on = None
            _zp_planned = (_zp_targets, pass_count, _zp_vanished, _zp_bins)
            _zp_restub_since = False

        # Re-fill targets this iter = frontier fns already filled at iter start
        # (not stubs) → the engine will behaviorally re-fill them.
        _stubs_before = deps.scan_unimplemented_functions(src_dir).units
        _refill_targets = frontier_keys - _stubs_before
        # Body snapshot of the dispatched units, taken now so the post-fill
        # reading below can say which of them actually TOOK a semantic change
        # — the cursor's "attempted" is a dispatch fact, and the pass audit
        # needs the landed fact (`domain/landed_change.py`). None ⇒ the
        # extractor port is absent (legacy/stub engines): the audit is
        # skipped, never guessed.
        _bodies_before = (
            unit_bodies(deps.enumerate_fns(target_src_dir) or [], frontier_keys)
            if deps.enumerate_fns is not None else None)
        # THE UNIT LEDGER'S WRITE PATH, AT THE WAVE. The `landed` rows used
        # to be written only in the post-fill block below, which a fill that
        # ends on a stop never reaches: every stop kind (sentinel, the
        # fill-phase clock, a TERM) unwinds through the `except` around the
        # fill call, so the four fills of 2026-09-08 that ended that way —
        # runs `3400546`, `3500465`, `3625246`, `3698228`, about sixty kept
        # bodies — left the unit ledger without one row (its index was last
        # written 09:31 that morning), and the next process could grade
        # nothing about them and exclude none of their flat re-buys. The
        # fill executor now calls this after every wave and on the wave a
        # stop cuts; the `except` branch calls it once more as the backstop
        # for what the cut wave had already written. The truth is the tree,
        # not the routes' reports: the units are the dispatched ones whose
        # body has moved since the snapshot above, exactly as the post-fill
        # block reads them, keyed by body sha so a row is written once.
        _wave_written: dict[FnLocator, str] = {}

        def _record_landed_rows(
            site: str, kept_hint: frozenset[FnLocator], *,
            # Bound at definition, per iteration: the snapshot, the batch,
            # the premise and the row list this iteration's sweep judges.
            _before: dict[FnLocator, Any] | None = _bodies_before,
            _keys: frozenset[FnLocator] = frozenset(frontier_keys),
            _failed: frozenset[str] = frozenset(cur_failed),
            _elig: Mapping[FnLocator, Any] = eligible,
            _epoch: int = _destruction_epoch,
            _iter: int = iter_n,
            _rows: list[LedgerEntry] = _ledger_rows,
            _written: dict[FnLocator, str] = _wave_written,
            _evidence: Mapping[tuple[str, str], str] = _evidence_of,
        ) -> int:
            if (deps.ledger_append is None or deps.enumerate_fns is None
                    or _before is None):
                return 0
            try:
                recs_now = deps.enumerate_fns(target_src_dir) or []
                landed_now = changed_units(_before, unit_bodies(recs_now, _keys))
                if not landed_now:
                    return 0
                items_w, file_of_w, fps_w = _layout_ctx(_epoch)
                bare_failed_w = frozenset(bare_test_name(t) for t in _failed)
                rows_w = rows_for_units(
                    [(k.file, k.fn_name) for k in landed_now], recs_now,
                    project=project_dir.name, cluster=cluster.name,
                    state=BodyState.LANDED, items=items_w, file_of=file_of_w,
                    file_fp=fps_w, run_id=deps.run_id, iteration=_iter,
                    epoch=_epoch, oracle_fp=_oracle_fp(_iter),
                    convicting_of={
                        (k.file, k.fn_name): sorted(
                            frozenset(bare_test_name(t)
                                      for t in _elig.get(k, ()))
                            & bare_failed_w)
                        for k in landed_now},
                    evidence_of=_evidence)
                fresh = [r for r in rows_w
                         if _written.get(FnLocator(r.key.file, r.key.fn_name))
                         != r.body_sha]
                if not fresh:
                    return 0
                deps.ledger_append(project_dir, fresh)
                _rows.extend(fresh)
                if _ledger_cache is not None:
                    _ledger_cache.extend(fresh)
                for r in fresh:
                    _written[FnLocator(r.key.file, r.key.fn_name)] = r.body_sha
                deps.log(
                    f"  {tag} [phase-c/ledger] iter {_iter}: {len(fresh)} "
                    f"landed body(ies) recorded at {site} "
                    f"({len(kept_hint)} kept by the wave's route(s)) — judged "
                    "at the next sweep, by the next process if this one stops "
                    "first.")
                deps.emit("phase_c.ledger.landed_rows", data={
                    "cluster": cluster.name, "iter": _iter, "site": site,
                    "units": sorted(f"{r.key.file}::{r.key.fn_name}"
                                    for r in fresh)})
                return len(fresh)
            except Exception as exc:  # noqa: BLE001 — a ledger write never ends a fill or masks a stop
                deps.log(f"  {tag} [phase-c/ledger] iter {_iter}: landed rows "
                         f"at {site} could not be written "
                         f"({type(exc).__name__}: {exc})")
                return 0
        # Only the units the escalation MEASURED, scoped to the batch exactly
        # as the two frontier projections are. Computed once here because the
        # landed-change audit below grades an unchanged escalated unit as a
        # failed try, and it must read the same set the prompt did.
        _batch_escalated = frozenset(
            k for ks in escalation_producers.values() for k in ks
            if k in frontier_keys)
        # The offer asks what the fire MEASURED, not which 12 this batch
        # drew. Same expression as the producer-guidance suspension above;
        # no `frontier_keys` intersection. The audit and the prompt grade
        # keep `_batch_escalated`.
        _measured_escalated = frozenset(
            k for ks in escalation_producers.values() for k in ks)
        # Drive the per-fn fill machinery with this frontier. The engine runs its
        # own graceful-stop checks (worker pools included), so this is where a
        # stop lands in practice — it owns the hours of the iter — and it exits by
        # raising past every `break` below.
        # Arm the fill-phase clock for THIS iteration's fill only, with the
        # minutes read once at cluster entry. `0` is a no-op. Disarmed when
        # the fill returns so the subsequent cargo test is not charged.
        fill_phase_clock.arm(
            _fill_phase_max,
            log=deps.log, emit=deps.emit,
            cluster=cluster.name, iter_n=iter_n)
        # The failing tests' own words, with what the assertion probe
        # OBSERVED beneath them when the probe is wired: a one-argument
        # `assert!(s.contains(..))` panics without printing `s`, and six
        # prompts of run `3003-93e9a9bf` carried that panic line while the
        # string under test reached none of them.
        _sweep_evidence = failure_evidence_by_test(output)
        if (deps.assert_probe is not None and cur_failed
                and os.getenv("ASSERT_PROBE", "1") != "0"):
            _probe_by_file: dict[Path, list[str]] = {}
            for _ft in sorted(cur_failed):
                _fpath = _test_files.get(_ft) or _test_files.get(bare_test_name(_ft))
                if not _fpath:
                    continue
                _fp = Path(_fpath)
                if not _fp.is_absolute():
                    _fp = project_dir / _fp
                if _fp.is_file():
                    _probe_by_file.setdefault(_fp, []).append(bare_test_name(_ft))
            if _probe_by_file:
                try:
                    _observed = deps.assert_probe(
                        project_dir, target_src_dir, cluster.name, cluster_env,
                        _probe_by_file)
                except Exception as _pe:  # noqa: BLE001 — never ends a sweep
                    deps.log(f"  {tag} [probe] failed ({type(_pe).__name__}: {_pe})")
                    _observed = {}
                if _observed:
                    _sweep_evidence = merge_observations(_sweep_evidence, _observed)
                    deps.log(f"  {tag} [probe] observed values for "
                             f"{len(_observed)} failing test(s).")
        _fill_meta: list[dict[str, Any]] = []
        # The ground this fill's plan stands on: the sweep's own capture,
        # read into per-test outcomes with crate sites resolved to the
        # function they lie in on the tree the sweep measured. The reader
        # re-runs the same test command on the tree as it stands; the fill
        # executor calls it only after a keep (`fill_scheduler._GroundGate`).
        _plan_premise: dict[str, PremiseEntry] | None = None
        _ground_now: Callable[[], dict[str, PremiseEntry] | None] | None = None
        if _replan_on_premise:
            _plan_premise = _premise_of_capture(output)

            def _ground_now() -> dict[str, PremiseEntry] | None:
                _rc_g, _out_g = deps.run_cargo(
                    test_cmd, target_src_dir, cluster_env,
                    timeout=test_timeout)
                if _rc_g == -1:
                    return None
                return _premise_of_capture(_out_g)
        try:
            with collect_stream_meta() as _fill_meta:
                sub = deps.task2_fill_fns_one_by_one(
                    project_dir=project_dir,
                    target_src_dir=target_src_dir,
                    tests_dir_files=tests_dir_files,
                    module_name=module,
                    cluster_name=cluster.name,
                    cluster=cluster,
                    check_cmd=check_cmd,
                    cluster_env=cluster_env,
                    max_passes=1,
                    fill_budget_minutes=_fill_budget,
                    fill_iteration=iter_n,
                    run_id=deps.run_id,
                    max_sessions_per_fill=_max_sessions,
                    leaf_name_counts=leaf_counts,
                    fr_path=fr_path,
                    tdd_frontier_keys=frontier_keys,
                    # The 12-cap batch is still the dispatch map for every
                    # ordinary unit: a key convicted by tests it was never
                    # dispatched against is the fatal direction
                    # FailureFrontier's docstring names. Pair members are
                    # the exception `pairs_to_dispatch` already made — they
                    # write even when the batch cut dropped them — so they
                    # join this map with the tests the hop attributed to
                    # either endpoint. Without that, the pair opens and
                    # convicting_for both members is empty.
                    tdd_frontier_tests=FailureFrontier.build(
                        dispatch_including_pairs(
                            eligible, frontier_keys, _attr.pairs,
                            convicting),
                        convicting,
                        # The sweep's own words per failing test (panic site +
                        # assertion), so a re-fill prompt can show WHY the test
                        # failed instead of asserting that it did.
                        evidence=_sweep_evidence,
                        # Test-own source: homonym scoring reads `{:?}` here,
                        # never from libtest evidence (run `463713-b7c666c8`).
                        sources=_attr.sources,
                        # The formatter dispatcher/implementer pairs the
                        # attribution hop resolved — the engine's pair route
                        # dispatches a cross-file pair as one transaction.
                        pairs=_attr.pairs),
                    escalated_keys=_batch_escalated,
                    escalated_measured=_measured_escalated,
                    ledger_entries=_ledger_entries(),
                    oracle_fp=_oracle_fp(iter_n),
                    tdd_test_cmd=test_cmd,     # scoped cluster test cmd
                    skip_end_pass_test=True,
                    crash_evidence=crash_texts or None,
                    crash_blamed=crash_blamed or None,
                    crash_blocked=crash_blocked_units,
                    # Tests listed failed whose binary did not complete
                    # (no pair in failed_pairs). The schedule ranks a run
                    # whose every aimed test is in this set, and whose
                    # targets miss crash_blamed, as a flat re-buy.
                    unmeasurable_tests=frozenset(
                        bare_test_name(t) for t in cur_failed
                        if bare_test_name(t) not in {
                            bare_test_name(n) for _b, n in _f_pairs}),
                    attributed=_attributed_locs,
                    dispatch_ranks=(
                        overlay_crash_on_ranks(_ranks, _crash_grades)
                        or None),
                    session_slices=_session_slices or None,
                    session_owners=_session_owners or None,
                    # The stub hold, for the schedule: its blockers are the
                    # locators whose bodies the held tests need first, and its
                    # hollow relation is the one coverage cannot give for a
                    # run that stopped at the stub.
                    stub_hold_blockers=frozenset(
                        locator_of(s_) for s_ in _stub_hold.blockers),
                    stub_hold_hollow={
                        t_: frozenset(locator_of(s_) for s_ in ss_)
                        for t_, ss_ in _stub_hold.by_test.items()},
                    on_wave_kept=lambda kept: _record_landed_rows("wave", kept),
                    fill_premise=_plan_premise,
                    fill_ground_now=_ground_now,
                )
        except BaseException:
            # Before the restore: a restored tree no longer holds these
            # bodies, and the ledger is where a body outlives the tree.
            _record_landed_rows("stopped during the fill pass", frozenset())
            _restore_abandoned_bet("stopped during the fill pass")
            _final_sweep("stopped during the fill pass")
            raise
        finally:
            fill_phase_clock.disarm()
        # What the engine ACTUALLY dispatched. The frontier is what the
        # driver asked for; the engine narrows it (the impl route defers
        # itself behind free-fn blockers, the per-fn route takes only
        # free fns). Run `9933-efe68f37` (2026-08-31) asked for 12, the
        # file-first route attempted `{context, msg}`, and the two sites
        # below graded 8 of the 12 as tried — 7 of them never reached a
        # prompt — because they predated the decode exhaustion charging
        # already used. `None` = engine did not report (legacy/stub
        # engines): fall back to the old frontier==dispatched.
        _sub_dispatched: set[FnLocator] | None = (
            None if sub.dispatched_fns is None
            else {FnLocator(*t) for t in sub.dispatched_fns})
        # A session serves its whole slice, batch cut or not: the units it
        # had in view are served, so they leave the never-served rank.
        _session_units: frozenset[FnLocator] = (
            frozenset(locator_of(u) for units in _session_slices.values()
                      for u in units)
            if _session_slices else frozenset())
        _attempted = (
            frontier_keys if _sub_dispatched is None
            else _sub_dispatched & (frontier_keys | _session_units))
        # A lost answer is not an attempt. `dispatched_fns` is stamped
        # before the model call, so a file-first call that returned
        # nothing routable (LLM error, empty/no FN blocks, nothing
        # spliced) still reports its units dispatched — and grading them
        # fabricates evidence twice over: run `347397-01e2463b` iter 1
        # (2026-08-31) wrote `flat` ledger rows for
        # src/ensure.rs::__dispatch_ensure and ::render off an empty
        # reply, and `render`, an owner of the failing test_trailer, sat
        # in the next two iterations' flat-sink derank on that row. The
        # subtraction (never `-=`: the legacy arm aliases `frontier_keys`)
        # also keeps them off `_served_at` below, so they keep their
        # never-served rank and the next iteration retries them — the only
        # recovery a free-fn refill has, since file-first is its sole
        # planner. The engine already excludes units another route
        # genuinely attempted afterwards, and reverted units.
        _attempted = _attempted - {
            FnLocator(*t) for t in sub.answer_lost_fns}
        # The tree after the fill, read once here: the re-offer below needs
        # the landed set before the cursor is persisted, and the landed-change
        # audit further down reads the same records.
        _recs_after_fill = (
            (deps.enumerate_fns(target_src_dir) or [])
            if _bodies_before is not None and deps.enumerate_fns is not None
            else None)
        _landed_after_fill: frozenset[FnLocator] = (
            frozenset(changed_units(
                _bodies_before, unit_bodies(_recs_after_fill, frontier_keys)))
            if _recs_after_fill is not None and _bodies_before is not None
            else frozenset())
        # A unit no route opened had no turn: put it back on the pass once.
        # If this batch closed the pass, the closure is taken back — the
        # traversal it counted did not happen for these units.
        _reoffer = reoffer_unopened(
            frontier_keys, opened=_attempted, landed=_landed_after_fill,
            already=_pass_reoffered)
        _pass_closed_now = _batch.completes_pass
        if _reoffer:
            # A fill that ended on a moved premise did not judge the units
            # it never opened — the plan changed under them — so their
            # re-offer does not spend the once-per-pass bound.
            if not sub.premise_moved:
                _pass_reoffered |= _reoffer
            if _batch.completes_pass:
                _pass_closed_now = False
                _traversals -= 1
                if _closed_fact is not None and _closed_fact in _flat_facts:
                    _flat_facts.remove(_closed_fact)
                _pass_cohort = _cohort_before_close
                _pass_attempted = set(_pass_units_at_close) - _reoffer
                _prev_pass_completed = False
            else:
                _pass_attempted -= _reoffer
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: pass cursor: "
                f"{len(_reoffer)} dispatched unit(s) no route opened go back "
                "on the pass (once per pass)"
                + ("; the pass this batch closed is REOPENED for them"
                   if _batch.completes_pass else "")
                + ": " + ", ".join(sorted(f"{k.file}::{k.fn_name}"
                                          for k in _reoffer)[:8])
                + (f" (+{len(_reoffer) - 8} more)" if len(_reoffer) > 8
                   else ""))
            deps.emit("phase_c.tdd.pass_reoffer", data={
                "cluster": cluster.name, "iter": iter_n,
                "reopened": bool(_batch.completes_pass),
                "units": sorted(f"{k.file}::{k.fn_name}" for k in _reoffer)})
        # Fill returned — this batch was served, but only the units a
        # route opened. A kill that never returned is not service (the
        # dispatch persist above left these keys off `_served_at` on
        # purpose). A unit the routes never opened keeps its
        # never-served rank instead of silently dropping to bucket 1.
        _served_at.update(dict.fromkeys(_attempted, iter_n))
        _served_verified.update(_attempted)
        _persist_floor()
        _capped_n, _capped_m = summarize_capped(_fill_meta)
        # The forced followup's own outcome, folded beside the main-stream
        # count: run `82157-476ddc16` (2026-08-26) had 8 of 8 file-first
        # replies written by the followup and no record of whether any of them
        # was itself cut — `llm.stream_meta` carries the main finish reason.
        # `(truncated, salvaged)`: truncated covers a server budget AND a
        # client think-cap break, because the controller salvages both.
        _fu_cut, _fu_salv = summarize_followup_salvage(_fill_meta)
        # Filled by the landed-change audit below when it runs; the
        # reasoning-capped emit reads it either way.
        _batch_esc_capped: frozenset[FnLocator] = frozenset()
        total_filled += sub.fns_filled
        total_attempts += sub.attempts
        total_bytes += sub.bytes_filled
        errors.extend(sub.errors)
        _prev_payoff = sub.payoff
        _prev_fill_classes = sub.fill_classes or FillClassHistogram()
        _saw_fill = True
        deps.log(f"  {tag} [phase-c/tdd] fill returned "
                 f"{_prev_fill_classes.rendered()}.")
        _score_grace(iter_n, pass_count)

        # Update recently_filled ring buffer with this iter's actual fills.
        # We don't have the exact list from the sub-result, so use the
        # frontier keys that are NO LONGER in the unimplemented set after
        # the pass — that's the set of fns that materially changed.
        cur_stubs = deps.scan_unimplemented_functions(src_dir).units
        newly_filled = [k for k in frontier_keys if k not in cur_stubs]
        # The landed-change reading: which dispatched units' bodies MOVED.
        # This is the fact the refutation criterion needs and the cursor
        # cannot supply — a byte-identical re-author, a peeled/reverted fill
        # and an immutable-by-rule skip all advanced the cursor while landing
        # nothing, and a completed pass over such units proves nothing about
        # them (the 2026-08-19 crawl's third batch: 4 byte-identical
        # re-authors incl. traversal step `src/error.rs::source`, 2 peeled
        # incl. the severed `src/context.rs::source`).
        if _recs_after_fill is not None and _bodies_before is not None:
            _recs_after = _recs_after_fill
            _batch_landed = _landed_after_fill
            # THE LEDGER'S WRITE PATH. The bodies are in hand at this exact
            # line and were thrown away here for the whole life of the
            # pipeline: `landed_change` emits the unit NAMES, and every
            # destruction afterwards had to re-derive the bodies through the
            # model. One row per landed unit, state `landed` — no sweep has
            # judged it yet, and the sweep below is what turns it into
            # evidence or into nothing.
            #
            # RAW `body_text`, never the folded form `unit_bodies` returns:
            # `" ".join(text.split())` collapses a body onto one line, and a
            # body holding a `//` comment then comments out its own remainder
            # the moment it is spliced back.
            #
            # The grade split comes FIRST because the write path needs it.
            # An unchanged unit that NO failing test convicts and that the
            # escalation did not MEASURE (a traversal step, a name match) was
            # told to return a right body unchanged — `[REFILL?]` — so its
            # "unchanged" is the instructed outcome, not a failed try, and
            # the refutation reading must not count it as one. An ESCALATED
            # unit is the opposite case: its instruction was "rewrite", so an
            # unchanged body is a failed try, and it is reported under its
            # own name so the batch line says which grade declined. The body
            # comparison cannot see WHY nothing landed; the grades can.
            # `_attempted` not `frontier_keys`: an escalated unit no route
            # wrote is not a failed try. Run `9933-efe68f37` iter 1 wrote
            # eight FLAT rows; seven of them were units the file-first
            # `wanted` set never opened (`context` was the one it did).
            # Existing false rows sit until those units are in a `wanted`
            # set; this cut stops new ones. The 12 installable complement
            # names omitted from `dispatched_fns` stay out of `_attempted`
            # on purpose — their prompt sentence is `return it unchanged
            # only if it is already right`, which `declined_unchanged`
            # already protects.
            _batch_unchanged = _attempted - _batch_landed
            # Passengers (landed, never opened) are not unattempted; they
            # still earn no `_served_at` stamp — that update is `_attempted`
            # only, a few lines up.
            _batch_unattempted = unattempted_units(
                frontier_keys, _attempted, _batch_landed)
            # Split the WHOLE unchanged set, not only the escalated slice:
            # a peeled attributed refill (`src/fmt.rs::debug` on run
            # `451330-7e8a7ca9`) is reverted, not an identical reply, and
            # the charger must not treat it as a judged try.
            _batch_flat, _batch_reverted = split_reverted(
                _batch_unchanged, sub.reverted_fns)
            _batch_esc_flat = _batch_flat & _batch_escalated
            _batch_esc_reverted = _batch_reverted & _batch_escalated
            # A truncated call that re-emits the old body is not a measured
            # refusal: grading it FLAT is the same overclaim the FLAT arm
            # below forbids for reverts, and the re-buy detector then stands
            # on a truncated measurement. Iter 3 of run `358833-aa15d93c`
            # (2026-09-01) wrote 8 flat rows in a batch whose own audit said
            # `3 of 4 fill replies reasoning-capped`. A unit whose EVERY
            # joined reply this batch was capped moves out of the flat set —
            # treated like `_batch_esc_reverted`: no row, re-seedable.
            _capped_locs = frozenset(
                FnLocator(*u.rsplit("::", 1))
                for u in units_all_replies_capped(_fill_meta) if "::" in u)
            _batch_esc_capped = frozenset(_batch_esc_flat & _capped_locs)
            _batch_esc_flat = _batch_esc_flat - _batch_esc_capped
            _batch_declined = declined_unchanged(
                _batch_flat, convicting=convicting,
                escalated=_batch_escalated,
                crash_evidence=frozenset(_crash_grades))
            _batch_unchanged = set(_batch_flat - _batch_declined)
            if deps.ledger_append is not None and (_batch_landed
                                                   or _batch_esc_flat):
                _items, _file_of, _fps = _layout_ctx(_destruction_epoch)
                # The premise the rows' grades are scoped to: the tests
                # convicting each unit at DISPATCH (attributed-or-escalated
                # and failing at the pre-fill sweep). Landed rows get the
                # premise re-stamped at judgment; the FLAT rows are graded
                # here, so this stamp is the one `flat_standing_unchanged`
                # will read.
                _bare_failed_dispatch = frozenset(
                    bare_test_name(t) for t in cur_failed)
                _convicting_of = {
                    (k.file, k.fn_name): sorted(
                        frozenset(bare_test_name(t)
                                  for t in eligible.get(k, ()))
                        & _bare_failed_dispatch)
                    for k in (_batch_landed | _batch_esc_flat)}
                _new_rows = rows_for_units(
                    [(k.file, k.fn_name) for k in _batch_landed], _recs_after,
                    project=project_dir.name, cluster=cluster.name,
                    state=BodyState.LANDED, items=_items, file_of=_file_of,
                    file_fp=_fps, run_id=deps.run_id, iteration=iter_n,
                    epoch=_destruction_epoch, oracle_fp=_oracle_fp(iter_n),
                    convicting_of=_convicting_of, evidence_of=_evidence_of)
                # A row the wave writer already appended for this exact body
                # is not written twice; a body a later wave moved again is a
                # new row, as it should be.
                _new_rows = [
                    r for r in _new_rows
                    if _wave_written.get(FnLocator(r.key.file, r.key.fn_name))
                    != r.body_sha]
                if _new_rows:
                    deps.ledger_append(project_dir, _new_rows)
                    _ledger_rows.extend(_new_rows)
                    if _ledger_cache is not None:
                        _ledger_cache.extend(_new_rows)
                # THE FLAT ARM. An escalated unit that came back unchanged
                # is a GRADED try with an empty verdict delta: the rewrite
                # instruction was issued because its convicting tests ran
                # and failed, and the body on the tree is the one the model
                # returned. A peel / compile / U2 / regression revert is
                # NOT that fact — `_batch_esc_reverted` — and writing FLAT
                # on it overclaimed the try (the 2026-08-19 crawl's peeled
                # `src/context.rs::source`). Before this arm the identical-
                # reply set was logged and dropped — run `82157-476ddc16`
                # iter 1 left nine such constructors with no row, so the
                # next process's re-buy detector, which joins on prior
                # graded rows, had nothing to fire on. The row is already
                # graded, so it is appended to the store and the cache but
                # NOT to `_ledger_rows`: the promotion pass would otherwise
                # re-judge it against a sweep that measures a body nothing
                # changed.
                _flat_rows = rows_for_units(
                    [(k.file, k.fn_name) for k in _batch_esc_flat],
                    _recs_after, project=project_dir.name,
                    cluster=cluster.name, state=BodyState.FLAT, items=_items,
                    file_of=_file_of, file_fp=_fps, run_id=deps.run_id,
                    iteration=iter_n, epoch=_destruction_epoch,
                    oracle_fp=_oracle_fp(iter_n),
                    convicting_of=_convicting_of, evidence_of=_evidence_of)
                if _flat_rows:
                    deps.ledger_append(project_dir, _flat_rows)
                    if _ledger_cache is not None:
                        _ledger_cache.extend(_flat_rows)
                    deps.log(
                        f"  {tag} [phase-c/ledger] iter {iter_n}: "
                        f"{len(_flat_rows)} escalated unit(s) came back "
                        "unchanged — recorded as `flat` (a graded try with "
                        "an empty verdict delta; not re-seedable; the next "
                        "process's re-buy detector fires on it).")
            _pass_landed |= _batch_landed
            _pass_declined |= _batch_declined
            _pass_escalated |= _batch_escalated
            _pass_opened |= _attempted
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: landed-change audit — "
                f"{len(_batch_landed)} of {len(frontier_keys)} dispatched "
                "unit(s) landed a semantic change"
                + ("; unchanged: " + ", ".join(sorted(
                    f"{k.file}::{k.fn_name}" for k in _batch_unchanged)[:12])
                   if _batch_unchanged else "")
                + ("; INFERRED, unchanged (not a failed try): " + ", ".join(
                    sorted(f"{k.file}::{k.fn_name}" for k in _batch_declined)[:12])
                   if _batch_declined else "")
                + ("; reverted (tree body is the snapshot, not the reply): "
                   + ", ".join(sorted(
                       f"{k.file}::{k.fn_name}"
                       for k in _batch_reverted)[:12])
                   if _batch_reverted else "")
                + ("; ESCALATED, unchanged (a FAILED try — the instruction "
                   "was rewrite): " + ", ".join(sorted(
                       f"{k.file}::{k.fn_name}"
                       for k in _batch_esc_flat)[:12])
                   if _batch_esc_flat else "")
                + ("; ESCALATED, reverted (tree body is the snapshot, not "
                   "the reply): " + ", ".join(sorted(
                       f"{k.file}::{k.fn_name}"
                       for k in _batch_esc_reverted)[:12])
                   if _batch_esc_reverted else "")
                + (f"; {len(_batch_unattempted)} batch unit(s) no route "
                   "attempted: " + ", ".join(sorted(
                       f"{k.file}::{k.fn_name}"
                       for k in _batch_unattempted)[:12])
                   if _batch_unattempted else "")
                + (f"; {_capped_n} of {_capped_m} fill replies reasoning-capped"
                   + (f" ({_fu_cut} followup replies truncated"
                      + (f", {_fu_salv} salvaged to their last closed block"
                         if _fu_salv else "") + ")" if _fu_cut else "")
                   if _capped_m else "")
                + ("; ESCALATED, unchanged but every reply capped (NOT "
                   "graded flat — a truncated call is not a measured "
                   "refusal): " + ", ".join(sorted(
                       f"{k.file}::{k.fn_name}"
                       for k in _batch_esc_capped)[:12])
                   if _batch_esc_capped else "")
                + ".")
            deps.emit("phase_c.tdd.landed_change", data={
                "cluster": cluster.name, "iter": iter_n,
                "dispatched": len(frontier_keys),
                "attempted": sorted(f"{k.file}::{k.fn_name}"
                                    for k in _attempted),
                "landed": sorted(f"{k.file}::{k.fn_name}"
                                 for k in _batch_landed),
                "unattempted": sorted(f"{k.file}::{k.fn_name}"
                                      for k in _batch_unattempted),
                "unchanged": sorted(f"{k.file}::{k.fn_name}"
                                    for k in _batch_unchanged),
                "reverted": sorted(f"{k.file}::{k.fn_name}"
                                   for k in _batch_reverted),
                "declined": sorted(f"{k.file}::{k.fn_name}"
                                   for k in _batch_declined),
                # The escalated-unchanged set under its own name. It is a
                # subset of `unchanged`, and before this key the nine
                # constructors of `82157-476ddc16` iter 1 sat inside it
                # unlabelled — a consumer of the event could not tell a
                # failed rewrite from an unchanged re-fill.
                "esc_unchanged": sorted(f"{k.file}::{k.fn_name}"
                                        for k in _batch_esc_flat),
                "esc_reverted": sorted(f"{k.file}::{k.fn_name}"
                                       for k in _batch_esc_reverted),
                # Escalated-unchanged units withheld from the FLAT grade
                # because every joined reply was reasoning-capped — a
                # truncated call is not a measured refusal.
                "esc_capped": sorted(f"{k.file}::{k.fn_name}"
                                     for k in _batch_esc_capped)})
            if _pass_closed_now:
                # Dispatched by this process and opened by no route: not a
                # try, so not in the unchanged (failed-try) list either.
                _unopened_pass = pass_unopened(
                    _pass_units_at_close, resumed=_resumed_cursor_window,
                    opened=_pass_opened, landed=_pass_landed)
                _unchanged_pass = (_pass_units_at_close - _pass_landed
                                   - _unopened_pass)
                # The pass's own record FIRST, the current conviction map
                # as the fallback. `_pass_declined` travels with the cursor
                # across a relaunch exactly as `_pass_landed` does, so a
                # pass spread over the one-batch-per-relaunch crawl states
                # the decline its earlier process actually observed; the
                # map is still read for units this process dispatched
                # before the record existed. A unit convicted now was
                # convicted at its dispatch too, barring a verdict move —
                # which would have ended the flat reading this audit
                # qualifies.
                _declined_pass = declined_unchanged(
                    _unchanged_pass, convicting=convicting,
                    escalated=_pass_escalated,
                    already_declined=_pass_declined,
                    crash_evidence=frozenset(_crash_grades))
                _unchanged_pass -= _declined_pass
                deps.log(
                    f"  {tag} [phase-c/tdd] iter {iter_n}: PASS AUDIT — "
                    f"traversal {_traversals} complete: "
                    + render_pass_audit(
                        cohort_size=_cohort_at_close,
                        attempted=len(_pass_units_at_close),
                        landed=frozenset(_pass_landed),
                        unchanged=frozenset(_unchanged_pass),
                        declined=_declined_pass,
                        unopened=_unopened_pass))
                deps.emit("phase_c.tdd.pass_audit", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "traversal": _traversals,
                    "cohort_size": _cohort_at_close,
                    "attempted": sorted(f"{k.file}::{k.fn_name}"
                                        for k in _pass_units_at_close),
                    "landed": sorted(f"{k.file}::{k.fn_name}"
                                     for k in _pass_landed),
                    "unchanged": sorted(f"{k.file}::{k.fn_name}"
                                        for k in _unchanged_pass),
                    "declined": sorted(f"{k.file}::{k.fn_name}"
                                       for k in _declined_pass),
                    "unopened": sorted(f"{k.file}::{k.fn_name}"
                                       for k in _unopened_pass)})
        elif _capped_m:
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: "
                f"{_capped_n} of {_capped_m} fill replies reasoning-capped"
                + (f" ({_fu_cut} followup replies truncated"
                      + (f", {_fu_salv} salvaged to their last closed block"
                         if _fu_salv else "") + ")" if _fu_cut else ""))
        if _capped_m:
            deps.emit("phase_c.batch.reasoning_capped", data={
                "cluster": cluster.name, "iter": iter_n,
                "capped": _capped_n, "replies": _capped_m,
                "followups_truncated": _fu_cut,
                "followups_salvaged": _fu_salv,
                # Escalated-unchanged units every one of whose joined replies
                # was capped — held out of the FLAT grade this iter (see the
                # landed-change audit), so the re-buy detector never stands
                # on a truncated measurement.
                "esc_flat_withheld": sorted(
                    f"{k.file}::{k.fn_name}" for k in _batch_esc_capped)})
        # The landed record's lifetime is the pass's, exactly like the
        # cursor: a closed pass releases both (the audit above has already
        # reported this one), and the next pass accumulates fresh.
        if _pass_closed_now:
            _pass_landed = set()
            _pass_declined = set()
            _pass_escalated = set()
            _pass_opened = set()
            _pass_reoffered = set()
        last_newly_filled = newly_filled   # hook 3: next iter attributes regressions to these
        for k in newly_filled:
            recently_filled.append(k)
        if len(recently_filled) > recently_filled_max:
            recently_filled = recently_filled[-recently_filled_max:]
        # This pass's dispatch set, for the stall exit's conviction window.
        # `_attempted` is the same decode as `_served_at` / the FLAT arm
        # above; `None` engine already fell back to the frontier there.
        _dispatched_window.append(frozenset(_attempted))
        if len(_dispatched_window) > max(max_stalls, 1) + 1:
            _dispatched_window = _dispatched_window[-(max(max_stalls, 1) + 1):]
        # A re-fill attempt is CHARGEABLE only when the sweep that dispatched
        # it was measurable — oracle intact and counters reconciled. A
        # dispatch drawn from a shrunken or self-contradicting reading was
        # never judgeable evidence against the unit.
        _charge_ok = _oracle.intact and _rec.reconciled
        _spared_undispatched: list[FnLocator] = []
        _reverted_now = {FnLocator(f, n) for f, n in sub.reverted_fns}
        for key in frontier_keys:
            if _sub_dispatched is not None and key not in _sub_dispatched:
                # NO route attempted this fn — it was refused (deferred,
                # narrowed out, skip-gated), not judged. Charging it here is
                # what locked every A/B/C cluster out of its impl members on
                # 2026-08-11/12: exhausted at iter 1 undispatched, their block
                # read "not viable" from iter 2 on. Left admitted; the stall
                # budget still bounds the loop if nothing ever dispatches it.
                if key in cur_stubs:
                    _spared_undispatched.append(key)
                continue
            # A lost answer is not a judged try. `_attempted` already
            # subtracted `answer_lost_fns` (the FLAT arm and `_served_at`
            # read that set); this charger was the reader that still keyed
            # on the raw dispatch stamp. Run `347397-01e2463b` iter 1
            # (2026-08-31): src/ensure.rs replied empty, `__dispatch_ensure`
            # and `render` were still in `dispatched_fns`, and a second empty
            # reply would have hit `TDD_REFILL_MAX_ATTEMPTS` (default 2) and
            # locked `render` out of the next-iteration file-first retry that
            # is the only recovery a free-fn refill has. They already
            # surface under "no route attempted" on the landed-change line,
            # so they do not join `_spared_undispatched` (that line is "a
            # route never opened them").
            if key not in _attempted:
                continue
            # A peel / compile / U2 revert restored the snapshot; that is
            # not a judged try of the body the model returned. Charging it
            # locked `src/fmt.rs::debug` at the refill cap after two peels
            # (run `451330-7e8a7ca9`, 2026-08-31). The named trade: a
            # forever-reverting unit never cap-exhausts; attempt caps and
            # the stall / flat stops still bound the spend.
            if key in _reverted_now:
                continue
            if key in cur_stubs:
                # First-fill attempt left it a stub → exhausted (existing).
                permanently_exhausted.add(key)
                last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
            elif key in _refill_targets and _charge_ok:
                # residual: a behavioral re-fill was attempted (fn was
                # filled at iter start). It reappears here only while its test
                # keeps failing → count the attempt and exhaust it at the cap so
                # it stops re-attempting every iter. A re-fill that FIXED its test
                # leaves the frontier next iter and never reaches the cap.
                refill_attempts[key] = refill_attempts.get(key, 0) + 1
                if refill_attempts[key] >= _refill_max:
                    permanently_exhausted.add(key)
                    last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
                    deps.emit("phase_c.verdict", data={
                        "unit": f"{key.file}::{key.fn_name}",
                        "route": "tdd", "attempt": refill_attempts[key],
                        "disposition": "cap_refusal", "outcome": "negative",
                        "reason": "re-fill cap reached on measurable "
                                  "sweeps — exhausted until re-admission",
                        "cluster": cluster.name})
        # THE LINE BELOW CONFLATED TWO POPULATIONS WITH OPPOSITE MEANINGS, and
        # the benign one is 8 of the 10 instances on the retained record.
        # `cur_stubs` comes from `scan_unimplemented_functions`, which takes no
        # cluster and evaluates no cfg, so a stub this cluster's cfg-set
        # excludes is in it — and being cfg-dead is exactly why no route
        # dispatched it (the engine refuses those at dispatch, by design). On
        # the retained logs the line named `src/nightly.rs::fmt` in cluster
        # `stable` eight times and the genuine case — four impl-member producers
        # no route claimed, iteration 6 of run `1920662-40f711b2` — once, with
        # `src/ensure.rs::__dispatch_ensure` (a synthesized dispatch-family
        # name) making up the tenth. The cfg-inactive names were already printed
        # one line above by the per-fn route's own membership report, so the two
        # readings sat adjacent and disagreed.
        #
        # Subtracting them is what makes this line evidence: an undispatched
        # cfg-dead stub is the cfg filter working, and an undispatched LIVE stub
        # is a routing gap. `_stub_keys` is this iteration's CLUSTER-SCOPED stub
        # scan, already taken at the top of the loop — reused rather than
        # re-scanned, so the classification costs nothing and cannot disagree
        # with the frontier that was built from it. It is a pre-fill reading and
        # `cur_stubs` is post-fill, which is sound for exactly this question: a
        # key in `_spared_undispatched` was dispatched by nobody, so nothing
        # could have filled it in between and it is a stub in both readings.
        #
        # FAIL OPEN. An empty cluster-scoped scan against a non-empty stub set
        # would make EVERY stub read as cfg-inactive and silence the line
        # completely — turning a diagnostic into a guarantee of silence, which
        # is the failure mode `ClusterMembershipReport.emptied` names ("a filter
        # with input and no output is a contradiction to locate"). An empty
        # answer is therefore treated as no answer.
        _cfg_inactive = (cur_stubs - set(_stub_keys)) if _stub_keys else set()
        _spared_live = [k for k in _spared_undispatched
                        if k not in _cfg_inactive]
        _spared_dead = len(_spared_undispatched) - len(_spared_live)
        if _spared_live:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: "
                     f"{len(_spared_live)} frontier stub(s) were "
                     "dispatched by NO route this iter — left admitted, no "
                     "exhaustion charge: " + ", ".join(
                         sorted(f"{k.file}::{k.fn_name}"
                                for k in _spared_live))
                     + (f" ({_spared_dead} further stub(s) are cfg-inactive in "
                        "this cluster and are correctly undispatched — not "
                        "counted here)" if _spared_dead else ""))
            deps.emit("phase_c.charge.undispatched_spared", data={
                "cluster": cluster.name, "iter": iter_n,
                "fns": sorted(f"{k.file}::{k.fn_name}" for k in _spared_live),
                "cfg_inactive_excluded": _spared_dead})
        last_total_filled = total_filled

    # Every `break` above lands here — backstop, timeout, stall exit, and the
    # green exit that `_cluster_done` excuses. One place rather than at each
    # `break`, because every exit owes this and a future exit path would silently
    # not; the raising exits are covered at the two points they raise from.
    _restore_abandoned_bet("exited")

    if _cluster_done and deps.augmentation_fn_record is not None:
        _fn_rec = deps.augmentation_fn_record(project_dir, cluster.name, module)
        report_close_stubs(
            stubs=deps.scan_unimplemented_for_cluster(target_src_dir, cluster),
            record=_fn_rec if isinstance(_fn_rec, FnCoverageRecord) else None,
            cluster_name=cluster.name, close_kind=_close_kind, tag=tag,
            log=deps.log, emit=deps.emit)

    return ClusterFillResult(
        fns_filled=total_filled,
        bytes_filled=total_bytes,
        attempts=total_attempts,
        remaining=deps.scan_unimplemented_for_cluster(
            target_src_dir, cluster).unit_count,
        errors=tuple(errors),
        baseline_broken=_baseline_broken,
        failed_tests=last_failed,
        ran_tests=last_ran,
        # Finishing with failing tests is EXHAUSTED, not done — except a
        # reference-floor close, which still has last_failed (the original
        # crate's leftover) but is the same success as rc==0. The checkpoint
        # marker is the same either way and a cluster is never re-opened, so
        # without this the distinction is lost at the boundary and whatever
        # repairs the merged tree starts from a bare failing-test name.
        exhausted=bool(last_failed) and not _cluster_done,
        exhausted_fns=tuple(sorted(permanently_exhausted)),
    )
