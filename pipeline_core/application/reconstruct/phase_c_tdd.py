"""Phase-C TDD fill driver (P4) — relocated from pipeline.py.

The per-cluster test-driven fill loop: scan stubs → leaf-first frontier → fill via the
PhaseC fn engine → re-run cluster tests → attribute failures → repeat under a FloorStall
budget. Collaborators are injected via `PhaseCTddDeps` (the composition root binds the
concrete pipeline.py helpers, late-bound for the monkeypatch seams); pure domain helpers
(Attempt/LoopState/test parsers) are imported directly. Body relocated verbatim.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from pipeline_core.application.retrigger_layout import RetriggerResult

from pipeline_core.application.reconstruct.tdd_ratchet import (
    breach_withheld,
    ratchet_breach,
    restore_src_tree,
    snapshot_due,
    snapshot_src_tree,
)
from pipeline_core.domain.attribution import (
    AttributionResult,
    FailureFrontier,
    union_unattributed_stubs,
)
from pipeline_core.domain.best_tree_mark import BestTreeMark
from pipeline_core.domain.crash_site import (
    crash_evidence_by_locator,
    vanish_is_stub_confounded,
)
from pipeline_core.domain.dispatch import (
    defer_blocked_consumers,
    restrict_to_compile_blockers,
)
from pipeline_core.domain.layout_retrigger import (
    DEFAULT_REFILL_GRACE_MAX_ITERS,
    DEFAULT_REFILL_PER_ITER,
    CompileEvidence,
    CrashEvidence,
    RefillGrace,
)
from pipeline_core.domain.measurement import (
    Measurement,
    code_identity,
    qualified_verdicts,
    tree_content_sha,
)
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    FnLocator,
    Frontier,
)
from pipeline_core.domain.oracle_ledger import SweepOracle, reconcile_sweep
from pipeline_core.domain.pass_highwater import PassHighWater
from pipeline_core.domain.refine import (
    Attempt,
    LoopState,
    readmission_warranted,
)
from pipeline_core.domain.tdd_floor import (
    UNSET_PASS,
    TddFloor,
    render_pass_floor,
    resume_floor,
)
from pipeline_core.domain.test_command import ensure_serial_harness
from pipeline_core.domain.test_report import parse_test_output
from pipeline_core.domain.test_results import (
    TestStateReading,
    compile_error_digest,
    lib_compile_clean,
    parse_binary_results,
    parse_failed_tests,
    parse_test_counts,
    summarize_test_binaries,
)


@dataclass(frozen=True, slots=True)
class PhaseCTddDeps:
    """Collaborators the TDD fill driver needs, injected by the composition root."""

    attribute_failures: Callable[..., AttributionResult]
    build_test_reverse_map: Callable[..., dict[str, set[FnLocator]]]
    filter_test_targets: Callable[..., Frontier]
    check_graceful_stop: Callable[[str], None]
    check_auto_operator: Callable[[int, str], None]
    converge_in_cluster: Callable[..., str | None]
    count_relevant_notes: Callable[..., int]
    effective_stalls: Callable[..., int]
    emit: Callable[..., None]
    log: Callable[[str], None]
    order_frontier: Callable[..., list[tuple[FnLocator, set[str]]]]
    record_tdd_regression: Callable[..., str]
    reset_exhaustion_flags: Callable[..., int]
    run_cargo: Callable[..., tuple[int, str]]
    scan_unimplemented_for_cluster: Callable[..., list[FnLocator]]
    scan_unimplemented_functions: Callable[..., list[FnLocator]]
    task2_fill_fns_one_by_one: Callable[..., ClusterFillResult]
    # Futility escalation: (src_dir, test, futile_units,
    # futile_containers) -> Frontier additions. None disables escalation.
    escalate_to_producers: Callable[..., Frontier] | None = None
    # Persist a new pass high-water fill tree to heal's restore tiers:
    # (project_dir, cluster_name, filled_dir, BestTreeMark) -> None.
    # None disables checkpointing (heal then has only excision + skeleton).
    checkpoint_fill_tree: Callable[..., None] | None = None
    # What the durable snapshot already on disk says about itself:
    # (project_dir, cluster) -> BestTreeMark | None. None here means the driver
    # cannot see the slot and every sweep may replace it — the pre-seed
    # behaviour, and what a unit test not exercising the tier wants.
    load_best_tree_mark: Callable[..., Any] | None = None
    # Release the disk-persisted exhaustion verdicts whose recorded blocking
    # stubs have filled:
    # (project_dir, {file: still-stubbed fn names}, cluster) -> count.
    # None disables the release, which is the pre-fix behaviour: a unit
    # convicted for calling `unimplemented!()` siblings stays convicted after
    # they fill, and only a cluster-wide score move can free it.
    retire_unblocked_convictions: Callable[..., int] | None = None
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
      parsed>0 & candidates>0  → candidates existed but all exhaustion-filtered (D3)
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
            remaining=len(deps.scan_unimplemented_for_cluster(
                target_src_dir, cluster)))
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
    test_timeout = int(os.getenv("PHASE_C_TEST_TIMEOUT", "120"))
    # max_iters is legacy — only the 10k backstop in the loop body honors it.
    # The real terminator is floor-stall on pass_count (max_stalls below).
    max_stalls = deps.effective_stalls(int(os.getenv("TDD_MAX_STALLS", "3")),
                                   deps.converge_in_cluster(cluster) or module)

    deps.log(f"  {tag} [phase-c/tdd] driver=tdd, "
         f"max_stalls={max_stalls}, timeout={test_timeout}s, "
         f"stems={len(test_stems)}")

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
    # Futility escalation state: per failing test, the frontier
    # unit-set of the previous iteration; a test whose set is UNCHANGED while
    # it kept failing has exhausted its attributed fns — escalate once.
    prev_test_units: dict[str, frozenset[FnLocator]] = {}
    escalated_tests: set[str] = set()
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
    # attempts and mark a fn exhausted once it caps (D3 still re-admits it on new
    # notes / cluster-wide progress — same escape hatch as stubs).
    refill_attempts: dict[FnLocator, int] = {}
    _refill_max = int(os.getenv("TDD_REFILL_MAX_ATTEMPTS", "2"))

    total_filled = 0
    total_attempts = 0
    total_bytes = 0
    errors: list[str] = []

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
    # Cause-C (runtime crash) retrigger: consecutive sweeps whose oracle was
    # short; at the threshold the frozen component the crashes implicate gets
    # re-authored (see the trigger block below the stall accounting).
    _vanish_streak = 0
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

    def _grant_grace(reverted: int) -> RefillGrace:
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
            max_iters=_grace_max_iters, granted_at=iter_n)
        if _g.restubbed:
            _highwater = _highwater.voided_by_restub(iter_n)
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-fill window open for "
                 f"{_g.iters_left} iter(s) — {_g.restubbed} body(ies) re-stubbed "
                 f"at ~{_grace_per_iter} re-fill(s)/iter"
                 + (f"; CLIPPED by the {_grace_max_iters}-iter cap, so it will "
                    "expire before the bodies land" if _g.clipped else "")
                 + ". The batch ratchet, the stall budget and the crash streak "
                 "stand down while re-fills keep landing.")
        deps.emit("phase_c.tdd.refill_window_open", data={
            "cluster": cluster.name, "iter": iter_n,
            "restubbed": _g.restubbed, "iters_granted": _g.iters_left,
            "per_iter": _grace_per_iter, "clipped": _g.clipped,
        })
        return _g

    def _score_grace(iter_n: int) -> None:
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
        _next = _grace.consume(total_filled)
        if not _next.active and _grace.restubbed:
            deps.log(
                f"  {tag} [phase-c/tdd] iter {iter_n}: re-fill window closed "
                f"({_grace.restubbed} body(ies) re-stubbed, "
                f"{total_filled - _grace.fills_at_grant} re-fill(s) landed this "
                f"iter, {_grace.iters_left - 1} iter(s) of the budget unspent) — "
                "the batch ratchet judges the re-author from the next sweep on.")
            deps.emit("phase_c.tdd.refill_window_closed", data={
                "cluster": cluster.name, "iter": iter_n,
                "restubbed": _grace.restubbed,
                "fills_this_iter": total_filled - _grace.fills_at_grant,
                "iters_unspent": _grace.iters_left - 1,
            })
        _grace = _next

    # Set ONLY by the green exit. Every other way out of the loop leaves a
    # re-fill window's bet unsettled, and the two are not the same thing.
    _cluster_done = False
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
            "retrigger_reverted": reverted,
        })

    # Cause-A (build failure) retrigger: consecutive sweeps in which the test
    # binaries did not compile. Carried as evidence, never as a threshold — a
    # tree that cannot build produces no verdict at all, so waiting is pure loss.
    _compile_broke_streak = 0
    # Stub count at the previous sweep, for the plateau check below. -1 means
    # "no previous sweep", which never counts as a decrease.
    _stubs_prev = -1
    # Set only by the compile-blocked exit below, and returned, so the caller
    # can tell "this cluster is finished" from "this tree does not build".
    _baseline_broken = False

    def _reconcile(rc_: int, output_: str, pass_n: int, fail_n: int,
                   failed_: set[str]) -> SweepReading:
        _bins = parse_binary_results(output_)
        _passing = TestStateReading(_bins).passed_names
        if rc_ == -1 or deps.active_test_inventory is None:
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
            _compile_broke = "could not compile" in (output_ or "")
            _err_total, _err_digest = (
                compile_error_digest(output_ or "") if _compile_broke else (0, []))
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

    iter_n = 0
    # Hard cap removed — the sole non-success terminator is `stall >=
    # max_stalls` on the pass_count floor. The 10k backstop here mirrors
    # `_improvement_loop`'s safety net and should never fire in practice.
    _tdd_backstop = int(os.getenv("PHASE_C_TDD_BACKSTOP", "10000"))
    while True:
        iter_n += 1
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
            raise
        # One fresh, informed retry per iteration boundary for fns/blocks
        # marked fully_exhausted (a DIFFERENT, disk-persisted gate than this
        # loop's own local `permanently_exhausted` — this one lives in the
        # retrospective cache and is checked INSIDE the per-fn fill engine
        # itself, so `permanently_exhausted`'s own re-admission logic below
        # cannot reach it). Comment-preservation already keeps the failed
        # attempt + cargo error visible in source
        # (fill_one_fn.py's make_failed_attempt_body), and normal per-fn
        # retrospective accounting already re-exhausts on no improvement —
        # this just re-admits them so that machinery gets to run again.
        # Gated on real progress since the LAST reset (not blind per-iter),
        # so a crate that's genuinely plateaued doesn't keep paying for
        # retries with no new context — same bound the debt-46 refill-cap
        # test below this loop protects for a different exhaustion path.
        #
        # "Progress" is PAYOFF, not activity — the rule and its evidence live
        # in `refine.readmission_warranted`.
        _new_fills = total_filled - _filled_at_last_reset
        _warranted = readmission_warranted(
            fills_since_reset=_new_fills, best_metric=best_pass,
            metric_at_last_reset=_pass_at_last_reset)
        if iter_n > 1 and _warranted:
            _cleared = deps.reset_exhaustion_flags(
                project_dir, f"TDD iter {iter_n} boundary — {_new_fills} "
                             f"new fill(s) and pass {_pass_at_last_reset} -> "
                             f"{best_pass} since the last check")
            _filled_at_last_reset = total_filled
            _pass_at_last_reset = best_pass
            if _cleared:
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: re-admitted "
                         f"{_cleared} previously-exhausted fn(s)/block(s) for "
                         "one more informed attempt.")
        elif iter_n > 1 and _new_fills > 0:
            # Say WHY the budget was withheld: silence here reads as "nothing
            # was exhausted", which is a different fact.
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: {_new_fills} fill(s) "
                     f"since the last check moved pass no further than "
                     f"{_pass_at_last_reset} — NOT re-admitting exhausted fn(s); "
                     "a retry with no new evidence is the same attempt again.")
            _filled_at_last_reset = total_filled
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: running cargo test...")
        t0 = time.monotonic()
        rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                  timeout=test_timeout)
        dt = time.monotonic() - t0
        pass_count, fail_count, _ignored = parse_test_counts(output)
        cur_failed = parse_failed_tests(output)
        _sweep = _reconcile(rc, output, pass_count, fail_count, cur_failed)
        fail_count, cur_failed, _oracle = (
            _sweep.fail_count, _sweep.failed, _sweep.oracle)
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
                deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: batch ratchet — "
                         f"{_confirm} (last iter's fills net-regressed, "
                         f"confirmed on re-sweep); reverted {_rev} file(s) to "
                         "best state, re-testing.")
                deps.emit("phase_c.tdd.batch_revert", data={
                    "cluster": cluster.name, "iter": iter_n,
                    "pass_count": pass_count, "best_pass": _bratchet_best,
                    "breach": _confirm, "files_reverted": _rev,
                })
                rc, output = deps.run_cargo(test_cmd, target_src_dir, cluster_env,
                                            timeout=test_timeout)
                pass_count, fail_count, _ignored = parse_test_counts(output)
                cur_failed = parse_failed_tests(output)
                _sweep = _reconcile(rc, output, pass_count, fail_count, cur_failed)
                fail_count, cur_failed, _oracle = (
                    _sweep.fail_count, _sweep.failed, _sweep.oracle)
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
        if _batch_ratchet \
                and snapshot_due(pass_count, _bratchet_best, _tree_sha, _bratchet_sha):
            _snap = snapshot_src_tree(_src_root)
            if _snap is not None:
                _bratchet_best = pass_count
                _bratchet_snap = _snap
                _bratchet_sha = _tree_sha
                _bratchet_passing = _sweep.passing
        # Durable checkpoint of the new-best fill tree so a later heal restores
        # THIS good state (best-tree copy / git ancestor) instead of nuking the
        # file to the baked skeleton. Independent of _batch_ratchet. Never
        # persisted while the oracle is short: a higher pass count over a
        # shrunken suite must not overwrite a full-oracle best tree.
        if deps.checkpoint_fill_tree is not None \
                and snapshot_due(pass_count, _checkpoint_best, _tree_sha, _checkpoint_sha) \
                and _oracle.missing_count == 0:
            deps.checkpoint_fill_tree(
                project_dir, cluster.name, target_src_dir,
                BestTreeMark(pass_count=pass_count, iteration=iter_n,
                             tree_sha=_tree_sha,
                             oracle_size=pass_count + fail_count))
            _checkpoint_best = pass_count
            _checkpoint_sha = _tree_sha
        # F6 — measurement provenance: stamp every iter event with the tree it
        # measured (content hash), the oracle size (denominator), and the actual
        # test names, so cross-run pass-count claims are verifiable and oracle
        # drift is detectable.
        # Binary-qualified verdicts from the SAME parse the counters come from.
        # A bare name is not a verdict here: one name runs in several binaries,
        # and a crashed binary breaks the name<->execution mapping in both
        # directions at once. Pairs are what `tools/pass_delta.py` must diff.
        _p_pairs, _f_pairs, _incomplete = qualified_verdicts(
            parse_binary_results(output))
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
        _rec = _measure.reconcile()
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
                 "reconciled": _rec.reconciled,
                 "reconcile_note": _rec.note,
                 # The outer-loop budget travels with every iter event so a
                 # dashboard can render stall x/N without hardcoding the cap.
                 "max_stalls": max_stalls}
        # Per-binary breakdown so triage can see WHICH test set
        # changed between iters (vanished tests, not just pass→fail flips).
        _bins = summarize_test_binaries(output)
        if _bins:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n} binaries: "
                     + ", ".join(f"{lbl} {p}/{f}" for lbl, p, f in _bins))
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
            break
        if rc == -1:
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test TIMED OUT "
                 f"({test_timeout}s). Stopping.")
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": -1,
                "frontier_size": 0, "kind": "timeout",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break

        # `output` is final for this iter by here (the ratchet's re-sweeps are
        # all above), so the compile verdict is read once and shared by every
        # consumer below rather than re-derived at each.
        _sweep_compile_broke = "could not compile" in (output or "")
        # Read once here, not at the dispatch site further down: the plateau
        # check below needs it, and nothing between the two mutates the tree
        # (the ratchet's re-sweeps and every splice are outside this span).
        _stub_list = deps.scan_unimplemented_for_cluster(target_src_dir, cluster)
        _stub_keys = frozenset(_stub_list)
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
            deps.retire_unblocked_convictions(
                project_dir,
                {f: frozenset(names) for f, names in _by_file.items()},
                cluster.name)
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
            if _resume.stall is not None:
                stall = _resume.stall
                # LoopState is the source of truth the mirrors read back
                # from, so the streak has to be restored THERE too.
                state.stalls = stall
            if _resume.best_pass is not None:
                best_pass = _resume.best_pass
                state.best_metric = float(best_pass)
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
        state.record(Attempt(metric=float(pass_count), iteration=state.iteration,
                              improved=improved, note=_regr_note))
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
        # A compile-broken sweep measured nothing and cannot be a baseline.
        if _highwater.rebase_due(iter_n) and not _sweep_compile_broke:
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
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: high-water re-based "
                     f"{render_pass_floor(_stale)} -> {best_pass} — the "
                     f"retrigger re-stubbed {_grace.restubbed} body(ies), so "
                     f"{render_pass_floor(_stale)} describes a tree this run no "
                     "longer has. The re-climb is judged from here.")
            deps.emit("phase_c.tdd.highwater_rebased", data={
                "cluster": cluster.name, "iter": iter_n,
                "stale_best": _stale, "new_best": best_pass,
                "restubbed": _grace.restubbed,
            })
        stall = state.stalls
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

        # Stage A — persist the floor AFTER every sweep, not only at the exit:
        # the whole point is that the process may not survive to an exit. A
        # compile-broken sweep measured nothing, so it must not overwrite a
        # real floor with its own zero.
        if deps.save_tdd_floor is not None and not _sweep_compile_broke:
            deps.save_tdd_floor(project_dir, cluster.name, TddFloor(
                cluster=cluster.name, best_pass=best_pass, stall=stall,
                tree_sha=_tree_sha, oracle_size=pass_count + fail_count,
                code_id=_code_id).to_dict())

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
        # Gated on NAMED vanished tests, never the bare missing count: an
        # unnamed shortfall is an inventory gap, and sending gdb + an ASAN
        # rebuild after it burns a sweep to conclude "no native fault".
        if _oracle.vanished and deps.localize_crashes is not None:
            # A second gate on the SAME expensive path: while the fns the
            # vanished tests reach are still >=half stubs, a stub panic (not a
            # native fault) is the likely cause — gdb + an ASAN rebuild would
            # only conclude "no fault". Defer until fills land; the frontier
            # already carries the vanished tests via extra_failed above.
            if vanish_is_stub_confounded(
                    _oracle.vanished, test_reverse_map,
                    deps.scan_unimplemented_functions(src_dir)):
                deps.log(f"  {tag} [phase-c/crash] {len(_oracle.vanished)} "
                         "test(s) vanished — deferring localization (>=half "
                         "the fns they reach are still stubs; stub panics "
                         "abort binaries too), fills run first.")
            else:
                _sites = deps.localize_crashes(
                    output, test_cmd, target_src_dir, cluster_env)
                crash_texts = crash_evidence_by_locator(tuple(_sites))
                for _site in _sites:
                    _blame = (str(_site.blamed_locator)
                              if _site.blamed_locator is not None
                              else "NOBODY (origin not attributed in-crate)")
                    deps.log(f"  {tag} [phase-c/crash] {_site.binary_label}"
                             f"::{_site.test_name or '?'} {_site.signal} — "
                             f"blamed {_blame}")
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
                        "raw_report": _site.raw_report})
                if not _sites:
                    deps.log(f"  {tag} [phase-c/crash] {len(_oracle.vanished)} "
                             "test(s) vanished but no crash could be localized "
                             "(no native fault under gdb, no ASAN report).")
                for _loc in crash_texts:
                    frontier.setdefault(_loc, set()).update(_oracle.vanished)

        # Futility escalation: a failing test whose attributed
        # unit-set is identical to last iteration's has been fully served by
        # rewrites that changed nothing — its accessors are the wrong target.
        # Escalate ONCE per test to the producers of the exercised types.
        if deps.escalate_to_producers is not None:
            units_by_test: dict[str, set[FnLocator]] = {}
            for key, tests in frontier.items():
                for tname in tests:
                    units_by_test.setdefault(tname, set()).add(key)
            for tname, units in units_by_test.items():
                prev = prev_test_units.get(tname)
                if (tname not in escalated_tests and prev is not None
                        and prev == frozenset(units)):
                    extra = deps.escalate_to_producers(
                        src_dir, tname, set(units))
                    if extra:
                        escalated_tests.add(tname)
                        for k, ts in extra.items():
                            frontier.setdefault(k, set()).update(ts)
                        deps.emit("phase_c.attribution.escalated", data={
                            "test": tname,
                            "futile_units": sorted(str(u) for u in units),
                            "added": sorted(str(k) for k in extra),
                        })
                prev_test_units[tname] = frozenset(units)

        # Union remaining cluster-active stubs into the frontier so a helper
        # that no failing test attributes directly (called only transitively)
        # still fills in phase-c instead of surviving to the post-TASK3 sweep
        # while its tested callers thrash as "blocked by an unfilled sibling
        # stub". order_frontier sorts leaf-first so helpers fill ahead of their
        # callers; the D3 filter + permanently_exhausted still bound re-entry.
        frontier = union_unattributed_stubs(frontier, _stub_list)

        # No frontier key may be a `#[test]` fn — the tests ARE the oracle, and a
        # fill dispatched at one rewrites the thing it is being judged by. The
        # attribution chain filters its own paths, but crash blame and futility
        # escalation ADD keys afterwards from raw fn records (escalation matched
        # `Error::` inside gen_tests bodies and proposed those tests as
        # "producers"), so the filter is re-applied over everything that landed.
        frontier = deps.filter_test_targets(frontier, src_dir)

        # D3 — re-entry filter for fns that already exhausted the per-fn
        # fix loop in a prior iter (avoid burning compute on the same fn
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
                deps.implicated_fns(output or "", target_src_dir))
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
                and lib_compile_clean(output or "")):
            _err_n, _err_lines = compile_error_digest(output or "")
            _retrig_k = deps.retrigger_layout(
                project_dir, module, cluster.name, target_src_dir, output,
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
                _grace = _grant_grace(_retrig_k.reverted_count)
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
            pass
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
                if (_oracle2.vanished
                        and "could not compile" not in (_out2 or "")):
                    deps.log(
                        f"  {tag} [phase-c/tdd] iter {iter_n}: oracle shrink "
                        "reproduced on immediate re-sweep — crash is "
                        "deterministic, retriggering now instead of waiting "
                        "out the sweep streak.")
                    _vanish_streak = _crash_retrigger_after
        elif not _oracle.vanished:
            # A compile-broken shrink is neutral; only a sweep in which every
            # expected name reported proves the crash class absent and may
            # reset the streak.
            _vanish_streak = 0
        if (_vanish_streak >= _crash_retrigger_after
                and deps.retrigger_layout is not None):
            _vanish_streak = 0   # re-arm only after another full streak
            _suspects: set[FnLocator] = set()
            for _vt in _oracle.vanished:
                _bare = _vt.split("::")[-1]
                for _k in (test_reverse_map.get(_bare)
                           or test_reverse_map.get(_vt) or set()):
                    _suspects.add(FnLocator(*_k))
            _retrig_c = deps.retrigger_layout(
                project_dir, module, cluster.name, target_src_dir, output,
                crash_evidence=CrashEvidence(
                    vanished=frozenset(_oracle.vanished),
                    suspect_fns=frozenset(_suspects),
                    consecutive_sweeps=_crash_retrigger_after,
                    # The localizer ran ~200 lines above on THIS sweep and
                    # named the faulting fns; `_suspects` above is only static
                    # reachability. Withholding the blame here is what let the
                    # retrigger pick a component in record order that the fault
                    # never touched (§ `classify_crash_retrigger`).
                    blamed_fns=frozenset(crash_texts)),
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
                _grace = RefillGrace.for_failed_attempt(total_filled,
                                                        granted_at=iter_n)
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
                _grace = _grant_grace(_retrig_c.reverted_count)
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
        # not a license to keep iterating.
        if stall >= max_stalls:
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
                    _grace = _grant_grace(_retrig.reverted_count)
                    _emit_retrigger_iter("retrigger_stall",
                                         _retrig.reverted_count)
                    state.stalls = 0
                    stall = 0
                    continue
            deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: pass_count plateau "
                 f"{stall} iter(s) at best={render_pass_floor(best_pass)}; stopping "
                 f"({len(eligible)} fn(s) still on frontier — will be "
                 "marked exhausted). best pass="
                 f"{render_pass_floor(best_pass)} fail={best_fail}.")
            _log_attribution_gap(deps, tag, iter_n, fail_count, cur_failed,
                                 len(frontier), kind)
            for _stuck_key in eligible:
                permanently_exhausted.add(_stuck_key)
            deps.emit("phase_c.tdd.iter", data={
                **_prov,
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible), "kind": "stall_exit",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            break
        if not eligible:
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
            _score_grace(iter_n)
            continue

        ordered = deps.order_frontier(eligible, leaf_counts)
        frontier_keys = {key for key, _ in ordered}
        deps.log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test rc={rc} "
             f"({dt:.1f}s); pass={pass_count} fail={fail_count} "
             f"oracle={_measure.oracle_size} "
             f"(improved={improved}, stall={stall}/{max_stalls}); "
             f"frontier={len(eligible)} fn(s) ({kind}), "
             f"{skipped_perm} skipped as exhausted.")
        deps.emit("phase_c.tdd.iter", data={
                **_prov,
            "cluster": cluster.name, "iter": iter_n, "rc": rc,
            "frontier_size": len(eligible), "kind": kind,
            "pass_count": pass_count, "fail_count": fail_count,
            "improved": improved, "stall": stall,
            "binaries": [[lbl, p, f] for lbl, p, f in _bins],
        })

        # Re-fill targets this iter = frontier fns already filled at iter start
        # (not stubs) → the engine will behaviorally re-fill them.
        _stubs_before = set(deps.scan_unimplemented_functions(src_dir))
        _refill_targets = frontier_keys - _stubs_before
        # Drive the per-fn fill machinery with this frontier. The engine runs its
        # own graceful-stop checks (worker pools included), so this is where a
        # stop lands in practice — it owns the hours of the iter — and it exits by
        # raising past every `break` below.
        try:
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
                leaf_name_counts=leaf_counts,
                fr_path=fr_path,
                tdd_frontier_keys=frontier_keys,
                tdd_frontier_tests=FailureFrontier.build(eligible, convicting),
                tdd_test_cmd=test_cmd,     # Plan 10 U2: scoped cluster test cmd
                skip_end_pass_test=True,
                crash_evidence=crash_texts or None,
            )
        except BaseException:
            _restore_abandoned_bet("stopped during the fill pass")
            raise
        total_filled += sub.fns_filled
        total_attempts += sub.attempts
        total_bytes += sub.bytes_filled
        errors.extend(sub.errors)
        _score_grace(iter_n)

        # Update recently_filled ring buffer with this iter's actual fills.
        # We don't have the exact list from the sub-result, so use the
        # frontier keys that are NO LONGER in the unimplemented set after
        # the pass — that's the set of fns that materially changed.
        cur_stubs = set(deps.scan_unimplemented_functions(src_dir))
        newly_filled = [k for k in frontier_keys if k not in cur_stubs]
        last_newly_filled = newly_filled   # hook 3: next iter attributes regressions to these
        for k in newly_filled:
            recently_filled.append(k)
        if len(recently_filled) > recently_filled_max:
            recently_filled = recently_filled[-recently_filled_max:]
        for key in frontier_keys:
            if key in cur_stubs:
                # First-fill attempt left it a stub → exhausted (existing).
                permanently_exhausted.add(key)
                last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
            elif key in _refill_targets:
                # residual: a behavioral re-fill was attempted (fn was
                # filled at iter start). It reappears here only while its test
                # keeps failing → count the attempt and exhaust it at the cap so
                # it stops re-attempting every iter. A re-fill that FIXED its test
                # leaves the frontier next iter and never reaches the cap.
                refill_attempts[key] = refill_attempts.get(key, 0) + 1
                if refill_attempts[key] >= _refill_max:
                    permanently_exhausted.add(key)
                    last_notes_count[key] = deps.count_relevant_notes(fr_path, key)
        last_total_filled = total_filled

    # Every `break` above lands here — backstop, timeout, stall exit, and the
    # green exit that `_cluster_done` excuses. One place rather than at each
    # `break`, because every exit owes this and a future exit path would silently
    # not; the raising exits are covered at the two points they raise from.
    _restore_abandoned_bet("exited")

    return ClusterFillResult(
        fns_filled=total_filled,
        bytes_filled=total_bytes,
        attempts=total_attempts,
        remaining=len(deps.scan_unimplemented_for_cluster(
            target_src_dir, cluster)),
        errors=tuple(errors),
        baseline_broken=_baseline_broken,
        failed_tests=last_failed,
        ran_tests=last_ran,
        # Finishing with failing tests is EXHAUSTED, not done. The checkpoint
        # marker is the same either way and a cluster is never re-opened, so
        # without this the distinction is lost at the boundary and whatever
        # repairs the merged tree starts from a bare failing-test name.
        exhausted=bool(last_failed),
        exhausted_fns=tuple(sorted(permanently_exhausted)),
    )
