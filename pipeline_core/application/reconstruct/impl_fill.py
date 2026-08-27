"""Per-impl-block Phase-C fill engine — relocated from pipeline.py.

Concrete behind `_PhaseCFillAdapter.fill_impl_blocks`. ~39 collaborators injected via
`ImplFillDeps` (composition root late-binds the pipeline.py helpers for seams); body
relocated byte-for-byte behind a rebind-prelude. FOLLOW-UP: partition `ImplFillDeps` into
the existing `ports/fill_engine.py` role ports (overlaps `_PhaseCFillAdapter`).
"""
from __future__ import annotations

import os
import queue as _queue_mod
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.cargo_errors import (
    ecodes_in_text,
    excerpt_for_code,
    focus_diagnostics,
)
from pipeline_core.application.reconstruct.cluster_membership import (
    collect_impl_refill_targets,
)
from pipeline_core.application.reconstruct.context import (
    crash_block_mark,
    crash_block_stands,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelResult,
    peel_file_first,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    ask_with_pulls,
    reconstruction_file_reader,
)
from pipeline_core.application.reconstruct.test_locator import (
    attributed_test_blocks,
)
from pipeline_core.application.reconstruct.unit_pool import run_units_concurrently
from pipeline_core.domain.attempt_loss import (
    AttemptLoss,
    escalation_refusal_note,
    invariance_refusal_note,
    may_escalate_to_last_resort,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.blueprint import Blueprint, blueprint_path, parse_blueprint
from pipeline_core.domain.crash_site import blame_outside_unit
from pipeline_core.domain.dispatch import (
    file_route_units,
    impl_route_covers,
    refills_deferred_by_impl_route,
)
from pipeline_core.domain.enums import RouteOrder
from pipeline_core.domain.failure_signature import (
    attempt_invariant,
    structural_group_note,
)
from pipeline_core.domain.fill_groups import BlockGroupInput, coupled_fill_groups
from pipeline_core.domain.identity import ContainerId, RetroKey
from pipeline_core.domain.impl_routing import (
    containers_in_file,
    joint_overwrite_names,
    members_in_file,
    refill_authorized_names,
    route_fn_bodies,
    trait_default_hosts,
)
from pipeline_core.domain.impl_routing import (
    norm_src_rel as _norm_src_rel,
)
from pipeline_core.domain.installable_fns import (
    ProducerBypass,
    installable_fns,
    offer_producers,
    producer_bypasses,
)
from pipeline_core.domain.llm_output import (
    bind_headerless_names,
    extract_fn_bodies,
    fn_tag_occurrences,
    reader_loss,
)
from pipeline_core.domain.model_defaults import resolve_route_order
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    DispatchFamily,
    FnLocator,
    FnSite,
    Frontier,
    ImplBlock,
    TypeRecord,
)
from pipeline_core.domain.pass_payoff import UnitOutcome, assess_pass_payoff
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refill_evidence import (
    attributed_test_leaves,
    collect_refill_evidence,
    container_header,
    escalated_refill_names,
    history_for_block,
    inferred_refill_names,
    pinned_observables_from_tests,
    render_refill_evidence,
    tuple_struct_names,
    wraps_crate_tuple_struct,
)
from pipeline_core.domain.refine import (
    CompilingSalvage,
    FixProgress,
    FloorStall,
    best_progress_fields,
    best_progress_of,
)
from pipeline_core.domain.retro_gate import (
    BlockedOnStubs,
    ConvictionEvidence,
    ConvictionStanding,
    RetroGateTally,
    RetroGateVerdict,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
    evidence_for_record,
    exhaustion_from_splice_diagnostic,
    honoured_guidance_line,
    impossibility_claim_is_unfounded,
    judge_guidance,
    regressed_tests_from_excerpt,
    regression_scoped,
    retired_guidance_line,
    screen_guidance,
    stamp_conviction,
)
from pipeline_core.domain.rust_idents import (
    canon_container,
    fn_reference_counts,
    signature_fixed_by_trait,
)
from pipeline_core.domain.rust_scan import stubbed_fn_names
from pipeline_core.domain.sibling_steer import sibling_steer
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.test_report import (
    JointImprovement,
    U2Decision,
    U2TestGate,
    U2Verdict,
    assess_joint_improvement,
    parse_test_output,
)
from pipeline_core.domain.test_results import (
    RegressionComparison,
    TestStateReading,
    strip_ansi,
)
from pipeline_core.domain.verdict import (
    Disposition,
    GateOutcome,
    ValidityVector,
    VerdictRecord,
)
from pipeline_core.prompts.context import PerImplSections
from pipeline_core.prompts.fix_prompts import (
    BlockFillPlan,
    build_file_fill_prompt,
    build_maximal_impl_prompt,
)
from pipeline_core.prompts.pull_menu import pull_menu_section
from pipeline_core.prompts.sections import gotchas_summary as _gotchas_summary

# The escalated followup's thinking reserve. Default off: Muse@high finishes
# at p90 73 718 reasoning chars against request_max = VLLM_THINKING_BUDGET +
# VLLM_CONTENT_BUDGET (8192+8192 live). Turning this on at that size is the
# starved-rung class that emptied the constructor main stream on run
# `82157-476ddc16` (`finish_reason=length` at 71 584 chars, then
# `fu.result or result` → EMPTY_RESPONSE). Hold until
# `scripts/measure_muse_reasoning.sh` places the reserve above that p90.
_FOLLOWUP_THINK_ENV = "PHASE_C_FOLLOWUP_THINK"


def followup_think_env_on() -> bool:
    """Whether `PHASE_C_FOLLOWUP_THINK` asks for the reserve. Unset / 0 / false
    is off — a cold launch must not ship the starved rung."""
    return os.getenv(_FOLLOWUP_THINK_ENV, "0").strip().lower() not in (
        "", "0", "false", "off")


def escalated_followup_think(grade_on: object) -> bool:
    """Thinking reserve on the forced followup: only when this prompt carries
    an escalated grade AND the env switch is on.

    The two fill sites used to pass `followup_think=bool(_grade_parts)` with
    no off switch, so a cold launch stamped that into `code_id`. Accessor
    and stub calls stay on today's no-think followup either way."""
    return bool(grade_on) and followup_think_env_on()


def _panic_sites_of(raw: Sequence[str]) -> dict[str, tuple[str, int]]:
    """test name -> (panic file, panic line) from a raw cargo-test capture.

    The reading the regression gate returns carries names; the sites live only
    in the text, which `measure_test_state`'s `raw_sink` hands back. Failures
    that printed no location are absent rather than defaulted — the comparison
    this feeds treats a missing site as unmeasured, and a sentinel would read
    as movement."""
    if not raw:
        return {}
    report = parse_test_output(raw[0])
    return {f.test_name: (f.panic_file, f.panic_line)
            for f in report.failures
            if f.panic_file and f.panic_line is not None}

def _note_reader_loss(
    log: Any, emit: Any, *, cluster: str, unit: str, route: str,
    reply: str, parsed: Sequence[Any],
) -> None:
    """Report a reply whose FN tags outnumber the records the reader produced.

    This is the fourth per-fill class — the model answered and the pipeline
    lost it — and it is always a pipeline defect, never a reason to spend
    another call on the unit. The signal it replaces existed and was never
    read: `empty/no FN blocks (N FN tag(s) in reply)` printed 37 times with a
    nonzero N across the retained logs, on `context.rs::StdErrorforContextError
    <C,Error>`, `error.rs::Error`, `chain.rs::ExactSizeIteratorforChain` and
    `kind.rs::Trait` — the units the record keeps re-convicting — while the
    sweeps that contained it were being read as flat scores.

    Three things it does that the old line did not. It fires on PARTIAL loss,
    because every drop site asked only `if not parsed` and a 7-tag reply
    yielding 1 record scored as a success. It carries the response sha, so the
    next reader shape is a lookup in the content-addressed store rather than an
    investigation — the cost of the previous six. And it emits an EVENT, which
    is what lets a run's own record be queried without grepping prose."""
    loss = reader_loss(reply, parsed)
    if loss is None:
        return
    log(f"  HEALTH ‼ [reader_loss] {route} {unit}: {loss.tags} FN tag(s) in "
        f"the reply, {loss.records} record(s) out — {loss.lost} answered "
        f"body(ies) discarded by the READER. Reply sha "
        f"{loss.response_sha[:12]}… (logs/prompts/{loss.response_sha}.txt); "
        "this is a parser defect, NOT a model failure.")
    emit("phase_c.fill.reader_loss", data={
        "cluster": cluster, "unit": unit, "route": route,
        "tags": loss.tags, "records": loss.records, "lost": loss.lost,
        "response_sha": loss.response_sha})


#: How many DISTINCT test files the attributed-test lookup may put in a prompt,
#: and how many leaf names it may walk to find them. The two differ because
#: several attributed tests routinely share one file: with both at 4, the walk
#: stopped after four names that resolved to two files and the third file never
#: reached the prompt.
_ATTRIBUTED_TEST_FILE_CAP = 4
_ATTRIBUTED_TEST_LEAF_CAP = 12


@dataclass(frozen=True, slots=True)
class ImplFillDeps:
    """Collaborators for the per-impl fill engine, injected by the composition root."""

    build_dispatch_family_section: Callable[..., str]
    build_per_impl_prompt: Callable[..., str]
    cargo_env: Callable[..., dict[str, str]]
    cargo_err_sig: Callable[..., frozenset[str]]
    cargo_err_sig_from_diagnostics: Callable[..., frozenset[str]]
    check_graceful_stop: Callable[[str], None]
    cluster_filter_type_methods: Callable[..., list[TypeRecord]]
    collect_crate_macros: Callable[..., str]
    collect_impl_blocks_for_cluster: Callable[..., list[ImplBlock] | None]
    collect_sibling_filled_bodies: Callable[..., str]
    compute_cluster_macro_invariants: Callable[..., str]
    detect_crate_no_std_invariant: Callable[..., str]
    dispatch_families_via_extractor: Callable[..., list[DispatchFamily] | None]
    effective_stalls: Callable[..., int]
    emit: Callable[..., None]
    enumerate_fns_via_extractor: Callable[..., list[dict[str, Any]] | None]
    enumerate_types_via_extractor: Callable[..., list[TypeRecord] | None]
    extract_crate_idioms: Callable[..., str]
    extract_impl_where_clause: Callable[..., str]
    filter_model_notes_for_prompt: Callable[..., str]
    find_tests_referencing_fn: Callable[..., list[tuple[str, str]]]
    focus_cargo_errors: Callable[..., str]
    frontier_from_diags: Callable[..., Frontier]
    impl_block_id: Callable[..., ContainerId]
    # The peel's per-body implication: the same span resolution as
    # `frontier_from_diags`, keeping the container the FnLocator projection
    # erases — (file, fn_name, one-line container) per resolved span.
    implicated_keys_from_diags: Callable[..., frozenset[tuple[str, str, str]]]
    is_fn_fully_exhausted: Callable[..., bool]
    load_retrospectives: Callable[..., dict[str, Any]]
    log: Callable[[str], None]
    module_of: Callable[..., str]
    phase_c_retrospective: Callable[..., dict[str, str]]
    record_error_codes_seen: Callable[..., None]
    retro_key: Callable[..., RetroKey]
    run_cargo_check_diag: Callable[..., tuple[int, str, list[Any]]]
    save_retrospectives: Callable[..., None]
    scan_unimplemented_for_cluster: Callable[..., list[FnLocator]]
    scan_unimplemented_records_for_cluster: Callable[..., list[dict[str, Any]] | None]
    splice_fn_with_where: Callable[..., SpliceOutcome]
    splice_lock_for: Callable[..., threading.Lock]
    types_section_for_fix: Callable[..., str]
    bound_methods_section_for: Callable[..., str]
    u2_test_gate: U2TestGate
    test_regression_baseline: Callable[..., TestStateReading | None]
    test_regression_gate: Callable[..., tuple[RegressionComparison, str] | None]
    ask: Callable[..., str]
    failed_body_cap: Any
    per_fn_fix_hard_cap: Any
    per_impl_max_stubs: Any
    task2_model: Any
    # Mechanical note validation: (fr_path, error_text) -> retired count.
    # None disables (tests / minimal wiring).
    retire_contradicted_notes: Callable[..., int] | None = None


def cluster_inactive_stub_names(
    target_src_dir: Path, cluster: BuildCluster | None,
    scan_records: Callable[..., list[dict[str, Any]] | None],
) -> frozenset[tuple[str, str]]:
    """The ``(file, fn_name)`` stubs this cluster's cfg-set never compiles.

    Read as the DIFFERENCE between every stub record and the cluster-active
    ones, so the cfg evaluation stays in `cluster_membership`, which owns it,
    and this consults its verdict instead of deriving a second one.

    Listed only when EVERY stub occurrence of the name in that file is
    inactive. `error.rs::provide` occurs three times under different gates, and
    refusing on the first inactive occurrence would block the ACTIVE one — the
    same conservative direction `fn_belongs_to_cluster` takes when a predicate
    is undecidable: better a wasted call than a fn nothing can fill."""
    return cluster_stub_census(target_src_dir, cluster, scan_records)[0]


def cluster_stub_census(
    target_src_dir: Path, cluster: BuildCluster | None,
    scan_records: Callable[..., list[dict[str, Any]] | None],
) -> tuple[frozenset[tuple[str, str]], int | None]:
    """`cluster_inactive_stub_names`' verdict AND the number of stubs the
    cluster DOES compile, from the same two scans — the second figure is what
    the regression gate's crash-only branch needs (`stub_free`), and taking it
    here costs no further scan. ``None`` when no scan is available: unknown is
    not zero, and the gate keeps its crash-only keep on unknown."""
    if cluster is None:
        return frozenset(), None
    every = scan_records(target_src_dir, None)
    if every is None:      # extractor unavailable — no verdict to enforce
        return frozenset(), None
    active_recs = scan_records(target_src_dir, cluster) or []
    active = {(_norm_src_rel(r.get("file", "")), r.get("fn_name", ""))
              for r in active_recs}
    inactive = frozenset(
        key for key in ((_norm_src_rel(r.get("file", "")), r.get("fn_name", ""))
                        for r in every)
        if key not in active)
    return inactive, len(active_recs)


def cluster_scoped_splice(
    splice: Callable[..., SpliceOutcome],
    *,
    inactive: frozenset[tuple[str, str]],
    target_src_dir: Path,
    log: Callable[[str], None],
) -> Callable[..., SpliceOutcome]:
    """`splice`, refusing any body aimed at a fn this cluster does not compile.

    `collect_impl_blocks_for_cluster` already makes that decision — it narrows
    `stub_members` to the cluster-active members and the prompt asks for those
    only. The decision never reached the SPLICE: the model is shown the whole
    block, answers a gated sibling too, `route_fn_bodies` routes it to a real
    container, and the splice fills whatever stub it finds. That is how
    `stable` came to hold a `wrapper.rs::provide` inside
    `#[cfg(error_generic_member_access)]` — a body its own build never
    compiled, which the seed then handed to `nightly` as if it were verified.

    Wrapped around the module's ONE splice binding rather than applied at each
    call site, because the call sites are the reason this leaked: the per-fn
    route, the free-fn reroute and the frontier scan all consult membership,
    and the two impl-routed splices did not. A filter written per site is the
    same fix three times and a fourth site silently omits it."""
    if not inactive:
        return splice

    def _guarded(file_path: Path, container: str, fn_name: str, body: str,
                 where_clause: str | None = None, **kwargs: Any) -> SpliceOutcome:
        try:
            rel = _norm_src_rel(str(Path(file_path).relative_to(target_src_dir)))
        except ValueError:
            rel = _norm_src_rel(Path(file_path).name)
        if (rel, fn_name) in inactive:
            log(f"  [phase-c/impl] refused `{rel}::{fn_name}` — every stub "
                "occurrence is cfg-gated out of this cluster, so a body here "
                "is one this build never compiles and the seed would pass on "
                "unverified.")
            return SpliceOutcome(False, "not_in_cluster_cfg", 0)
        return splice(file_path, container, fn_name, body, where_clause, **kwargs)

    return _guarded


def file_first_guidance_keys(
    file_rel: str,
    blocks: Sequence[ImplBlock],
    extra_plans: Sequence[Any] = (),
) -> tuple[tuple[str, str, str], ...]:
    """Retro-store lookups file-first must screen: the `*impl*` record per
    block AND each fill-plan unit (fn_name + container) the per-fn route
    already stamps.

    File-first used to look up only `file::*impl*::container`. The live
    store (2026-08-26) held 42 records, 32 of them `*impl*` and 10 keyed
    like the per-fn route (`construct_from_std` under `impl Error`, …).
    Those 10 could neither honour nor poison the constructor prompt
    because this route never asked for them. `extra_plans` are the free-fn
    / installable BlockFillPlan lines of the same file."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(file: str, fn: str, container: str) -> None:
        key = (file, fn, container)
        if key not in seen:
            seen.add(key)
            out.append(key)

    for b in blocks:
        add(b.file, "*impl*", b.container)
        for m in (*b.stub_members, *b.refill_members):
            add(b.file, m.fn_name, b.container)
    for p in extra_plans:
        header = getattr(p, "header", "") or ""
        for n in (
            *getattr(p, "stubs", ()),
            *getattr(p, "refills", ()),
            *getattr(p, "escalated", ()),
            *getattr(p, "installable", ()),
        ):
            if n:
                add(file_rel, n, header)
    return tuple(out)


def fill_impl_blocks(
    deps: ImplFillDeps,
    project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    module_name: str,
    cluster_name: str,
    cluster: BuildCluster | None,
    check_cmd: str,
    cluster_env: dict[str, str] | None,
    fr_path: Path | None,
    tdd_frontier_keys: set[FnLocator] | None,
    tdd_frontier_tests: FailureFrontier | None = None,
    # Units a futility escalation added after MEASURING their consumers'
    # rewrites as changing nothing — the producer subset of the fire, never the
    # merged frontier it returns (the merge also carries traversal steps and
    # the accessors' own file, which must keep the weaker grade).
    escalated_keys: frozenset[FnLocator] = frozenset(),
    # The escalation's full producer set, NOT intersected with this batch.
    # `installable_fns` asks what the fire MEASURED; `escalated_keys` stays
    # batch-scoped for the prompt grade and the landed-change audit.
    escalated_measured: frozenset[FnLocator] = frozenset(),
    # Prior FLAT tries of dispatched units, from the proven-body ledger.
    # Threaded from `phase_c_tdd._ledger_entries` so `render_refill_evidence`
    # can name the runs that rewrote a body identically. Default empty: a
    # caller that does not pass it (fn_walk, tests) leaves the prompt
    # byte-identical to the pre-history render.
    ledger_entries: Sequence[Any] = (),
    # Fingerprint of the oracle scoring this tree, same value the ledger
    # stamps on every row this iteration. `try_history` refuses a row
    # graded under another (including the empty fingerprint of a row
    # written before the field). Default empty: no history line, prompt
    # byte-identical to the pre-history render.
    oracle_fp: str = "",
    tdd_test_cmd: str | None = None,
    parallelism: int = 1,
    crash_evidence: dict[FnLocator, str] | None = None,
    # The ORIGIN blame only — see PerFnPassCtx.crash_blamed: evidence KEYS
    # include access frames, so key-membership reads the blame as inside the
    # innocent caller.
    crash_blamed: frozenset[FnLocator] | None = None,
    # Shared crash-block map (unit key -> blamed-file fingerprint), mutated
    # here and read across iterations — see PerFnPassCtx.crash_blocked.
    crash_blocked: dict[str, str] | None = None,
) -> ClusterFillResult:
    """Per-impl-block Phase C fill — alternative to `_task2_fill_fns_one_by_one`.

    Collects impl blocks active in this cluster, optionally narrows to
    those that contain a frontier fn, then for each block runs one LLM
    call that fills every stub member of the block in one shot. Splices
    all bodies, runs cargo check; on failure reverts and re-prompts with
    the focused errors. Each impl block has its own floor-stall on rustc
    error count.

    Returns the same stats dict shape as `_task2_fill_fns_one_by_one` so
    callers don't need to branch:
        {fns_filled, bytes_filled, attempts, errors, remaining,
         impls_filled, impls_failed}.

    Free fns (container == "") and impl blocks with more than
    `_PER_IMPL_MAX_STUBS` stubs are left untouched — the caller's per-fn
    fallback handles them.

    `parallelism > 1` dispatches independent FILES to a thread pool — blocks
    from the same file are never run concurrently with each other, since
    sibling impls on one type are frequently co-designed (e.g. an inherent
    impl's `new` plus a trait impl's methods) and a concurrent sibling
    attempt sees a mid-flux copy of the file, prompting the model to emit
    fixes spanning multiple impls that the splice router then drops for
    every target except its own. LLM calls for DIFFERENT files run
    concurrently; `_splice_check_lock` (already shared with the per-fn path)
    serializes every splice+cargo-check, and `_counters_lock` protects the
    plain-Python aggregate counters and the
    on-disk retrospective cache save that fall outside that lock.
    """
    # Rebind prelude — collaborators localised so the 8 nested closures stay byte-identical.
    _build_dispatch_family_section = deps.build_dispatch_family_section
    _build_per_impl_prompt = deps.build_per_impl_prompt
    _cargo_env = deps.cargo_env
    _cargo_err_sig = deps.cargo_err_sig
    _cargo_err_sig_from_diagnostics = deps.cargo_err_sig_from_diagnostics
    _check_graceful_stop = deps.check_graceful_stop
    _cluster_filter_type_methods = deps.cluster_filter_type_methods
    _collect_crate_macros = deps.collect_crate_macros
    _collect_impl_blocks_for_cluster = deps.collect_impl_blocks_for_cluster
    _collect_sibling_filled_bodies = deps.collect_sibling_filled_bodies
    _compute_cluster_macro_invariants = deps.compute_cluster_macro_invariants
    _detect_crate_no_std_invariant = deps.detect_crate_no_std_invariant
    _dispatch_families_via_extractor = deps.dispatch_families_via_extractor
    _effective_stalls = deps.effective_stalls
    _emit = deps.emit
    _enumerate_fns_via_extractor = deps.enumerate_fns_via_extractor
    _enumerate_types_via_extractor = deps.enumerate_types_via_extractor
    _extract_crate_idioms = deps.extract_crate_idioms
    _extract_impl_where_clause = deps.extract_impl_where_clause
    _filter_model_notes_for_prompt = deps.filter_model_notes_for_prompt
    _find_tests_referencing_fn = deps.find_tests_referencing_fn
    _focus_cargo_errors = deps.focus_cargo_errors
    _frontier_from_diags = deps.frontier_from_diags
    _impl_block_id = deps.impl_block_id
    _implicated_keys_from_diags = deps.implicated_keys_from_diags
    _is_fn_fully_exhausted = deps.is_fn_fully_exhausted
    _load_retrospectives = deps.load_retrospectives
    _log = deps.log
    _module_of = deps.module_of
    _phase_c_retrospective = deps.phase_c_retrospective
    _record_error_codes_seen = deps.record_error_codes_seen
    _retro_key = deps.retro_key
    _run_cargo_check_diag = deps.run_cargo_check_diag
    _save_retrospectives = deps.save_retrospectives
    _scan_unimplemented_for_cluster = deps.scan_unimplemented_for_cluster
    _scan_unimplemented_records_for_cluster = deps.scan_unimplemented_records_for_cluster
    _splice_fn_with_where = deps.splice_fn_with_where
    _splice_lock_for = deps.splice_lock_for
    _types_section_for_fix = deps.types_section_for_fix
    _bound_methods_section_for = deps.bound_methods_section_for
    _u2_test_gate = deps.u2_test_gate
    _test_regression_baseline = deps.test_regression_baseline
    _test_regression_gate = deps.test_regression_gate
    ask = deps.ask
    # Sieve counters for this whole impl-fill batch (workers share it).
    _retro_gate = RetroGateTally()
    _FAILED_BODY_CAP = deps.failed_body_cap
    _PER_FN_FIX_HARD_CAP = deps.per_fn_fix_hard_cap
    _PER_IMPL_MAX_STUBS = deps.per_impl_max_stubs
    _TASK2_MODEL = deps.task2_model
    if cluster_env is None:
        cluster_env = _cargo_env()
    # Absent frontier (fn_walk driver) = nothing dispatched by test, nothing
    # convicted by test; the compile gate and the regression ratchet still run.
    _frontier_tests = tdd_frontier_tests or FailureFrontier.of_dispatch({})

    def _convicting_for_block(b: ImplBlock) -> frozenset[str]:
        """Tests with DIRECT evidence at one of this block's members — the only
        ones that may revert it, and the only ones an exhaustion verdict about
        it may rest on. One definition for both, or the acceptance gate and the
        skip gate drift apart on what this block answers for."""
        out: set[str] = set()
        for _m in (*b.stub_members, *b.refill_members):
            out |= _frontier_tests.convicting_for(FnLocator(b.file, _m.fn_name))
        return frozenset(out)

    def _answers_for_block(b: ImplBlock) -> frozenset[str]:
        """Every test attributed to one of this block's members — DIRECT and
        ESCALATED, the dispatch projection. This is what a record earned from
        TEST outcomes is stamped with and read under (`evidence_for_record`),
        and what the guidance screen judges every record against. The
        convicting subset above stays the acceptance gate's definition; the
        two differ exactly on escalated tests, and the live
        `src/ensure.rs::*impl*::BothDebugfor(A,B)` record (`error_count 0,
        convicted_by []`) was supported by `[] <= {}` under the convicting
        reading while thirteen escalated ensure tests sat on the block."""
        out: set[str] = set()
        for _m in (*b.stub_members, *b.refill_members):
            out |= _frontier_tests.tests_for(FnLocator(b.file, _m.fn_name))
        return frozenset(out)

    target_src_dir.mkdir(parents=True, exist_ok=True)
    # Rebound BEFORE any nested route defines itself, so every splice this
    # module performs — block, file-first, free-fn reroute, last-resort — goes
    # through the cluster's own membership verdict exactly once.
    _cfg_dead_pairs, _active_stub_count = cluster_stub_census(
        target_src_dir, cluster, _scan_unimplemented_records_for_cluster)
    # The cluster-active tree holds no hollow body as this call begins: the
    # regression gate's crash-only keep has no hollow sibling to defer to,
    # so a fill that newly crashes binaries is reverted at once (see
    # `cargo_runner.test_regression_gate`, `stub_free`). Unknown ⇒ False.
    stub_free = _active_stub_count == 0
    _splice_fn_with_where = cluster_scoped_splice(
        _splice_fn_with_where,
        inactive=_cfg_dead_pairs,
        target_src_dir=target_src_dir, log=_log)

    def _cfg_dead_fns(file_rel: str) -> frozenset[str]:
        """The fns of `file_rel` this cluster never compiles — handed to the
        retro gate so a conviction blocked on one of them can expire."""
        rel = _norm_src_rel(file_rel)
        return frozenset(fn for f, fn in _cfg_dead_pairs if f == rel)
    # This tree's splice+cargo lock (per-tree: parallel clusters don't share).
    _splice_check_lock = _splice_lock_for(target_src_dir)
    # Guards the plain-Python aggregate counters (`attempts_total`,
    # `impls_failed`, `errors`) and the retrospective-cache save — the parts
    # of a block's fill that touch SHARED state but fall outside the
    # per-attempt `_splice_check_lock` critical section.
    _counters_lock = threading.Lock()
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    impls_filled = 0
    impls_failed = 0
    fns_filled_total = 0
    attempts_total = 0
    errors: list[str] = []
    # Fns a route genuinely attempted (recorded at the two points a unit is
    # handed to a fill cycle, AFTER the skip gates) — the caller's exhaustion
    # charging keys on this, so a member skipped by a refusal gate is spared
    # the strike a member that was tried and failed earns.
    dispatched_keys: set[FnLocator] = set()
    # Units spliced then restored to the pre-fill snapshot (peel / compile /
    # U2 / regression). The TDD audit's FLAT arm subtracts this set so a
    # reverted fill is not recorded as "the model returned this body".
    reverted_keys: set[FnLocator] = set()

    def _note_reverted(file_rel: str, names: Sequence[str]) -> None:
        with _counters_lock:
            reverted_keys.update(FnLocator(file_rel, n) for n in names if n)

    # One entry per block that LANDED, carrying the still-stubbed sites that
    # held its tests (empty when the block was judged on its own merits). Read
    # together at the end of the sweep: when one site blocked every landed
    # block, this route provably moved no verdict.
    block_outcomes: list[UnitOutcome] = []
    # Failing signatures shared ACROSS blocks this pass (guarded by
    # `_counters_lock`). Three sibling `From<Error>` impls each burned a full
    # episode plus a ~48k-char last resort on the same single failing test in
    # one pass, twice in consecutive runs (2026-08-10) — a set that many
    # units share implicates what they share, not any member, so the third
    # arrival makes the signature structural and later arrivals stop at
    # their first failed round: no retro, no conviction, no escalation.
    _shared_failing: dict[frozenset[str], set[str]] = {}
    _structural_sigs: set[frozenset[str]] = set()

    def _verdict_row(unit: str, disposition: Disposition, reason: str, *,
                     outcome: GateOutcome | None = None, attempt: int = 0,
                     evidence: tuple[str, ...] = (),
                     validity: ValidityVector | None = None,
                     model_calls: int = 0, prompt_chars: int = 0,
                     route: str = "impl") -> None:
        _emit("phase_c.verdict", data={
            **VerdictRecord(
                unit=unit, route=route, attempt=attempt,
                disposition=disposition, reason=reason, outcome=outcome,
                validity=validity or ValidityVector(),
                evidence_keys=evidence, model_calls=model_calls,
                prompt_chars=prompt_chars).to_json_dict(),
            "cluster": cluster_name,
        })

    blocks = _collect_impl_blocks_for_cluster(target_src_dir, cluster) or []
    if not blocks:
        return ClusterFillResult(dispatched_fns=())

    # Stamp `refill_members` onto blocks that have an
    # already-filled, compiling member with a failing attributed test, so the
    # co-designed block is re-attacked WHOLE in one call instead of leaving
    # wrong bodies to a per-fn refill that can only touch one member at a time.
    blocks = collect_impl_refill_targets(blocks, tdd_frontier_keys, tdd_frontier_tests)

    # Every impl in the cluster, kept whole. Narrowing below answers "what do we
    # fill?"; routing a file-wide model reply asks the different question "what
    # impls EXIST in this file?", and must not be answered with the fill set.
    all_blocks = blocks

    # If a TDD frontier is provided, narrow to blocks whose stub_members OR
    # refill_members intersect the frontier. Otherwise process all blocks with
    # stubs (refill_members is always empty without a frontier — same guard).
    if tdd_frontier_keys is not None:
        narrowed: list[ImplBlock] = []
        for b in blocks:
            keys = {(b.file, m.fn_name) for m in (*b.stub_members, *b.refill_members)}
            if keys & tdd_frontier_keys:
                narrowed.append(b)
        blocks = narrowed

    # Drop empty / free-fn / oversized blocks (handled by per-fn fallback).
    viable_blocks = [b for b in blocks
                     if impl_route_covers(b, max_stubs=_PER_IMPL_MAX_STUBS)]
    _refill_block_count = sum(1 for b in viable_blocks if b.refill_members)
    _log(f"  [phase-c/impl] cluster {cluster_name}: "
         f"{len(viable_blocks)} impl block(s) viable "
         f"({len(blocks) - len(viable_blocks)} skipped — free fns / "
         "oversized)."
         + (f" {_refill_block_count} behavioral re-fill block(s) "
            "(filled bodies with failing tests)." if _refill_block_count else ""))
    _deferred_refills = refills_deferred_by_impl_route(
        blocks, max_stubs=_PER_IMPL_MAX_STUBS)
    if _deferred_refills:
        _log(f"  [phase-c/impl] cluster {cluster_name}: "
             f"{len(_deferred_refills)} re-fill target(s) the per-impl route "
             "never attempts (free fn / oversized) — carried to the file-first "
             "route, the only one that can name them: "
             + ", ".join(sorted(f"{f}::{fn}" for f, fn in _deferred_refills)))
        _emit("phase_c.dispatch.refills_deferred", data={
            "cluster": cluster_name,
            "targets": sorted(f"{f}::{fn}" for f, fn in _deferred_refills)})

    # Crate-level context (shared by every block we visit).
    _src_for_macros = target_src_dir / "src" if (target_src_dir / "src").exists() else target_src_dir
    _macro_invariants = _compute_cluster_macro_invariants(_src_for_macros, cluster)
    _no_std_invariant = _detect_crate_no_std_invariant(target_src_dir)
    invariants_section = "\n\n".join(
        s for s in (_no_std_invariant, _macro_invariants) if s)
    _crate_type_records = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(target_src_dir) or [], cluster)
    # The crate's tuple structs — the wrappers a body can construct by
    # `Name(…)` — for the wrap reading that gates the TypeId sentence.
    _tuple_structs = tuple_struct_names(_crate_type_records)
    macros_section = _collect_crate_macros(_src_for_macros)

    # The crate blueprint — the model's own design doc, sliced per block
    # so every local fill sees the global scheme it committed to.
    _blueprint = Blueprint()
    if fr_path is not None:
        _bp_path = blueprint_path(fr_path)
        if _bp_path.exists():
            _blueprint = parse_blueprint(
                _bp_path.read_text(encoding="utf-8", errors="replace"))
            if not _blueprint.empty:
                _log(f"  [phase-c/impl] blueprint loaded: "
                     f"{len(_blueprint.sections)} section(s).")

    # Pull protocol: the model may request ONE typed artifact instead of
    # guessing (bounded round-trips; menu appended to every fill prompt).
    # All sources are post-strip-legal: the reconstruction tree, the tests,
    # the enumerated type records, the last cargo output.
    _pulls_on = os.getenv("CONTEXT_PULLS", "1") != "0"
    _max_pulls = int(os.getenv("CONTEXT_PULLS_MAX", "2"))
    _last_full_out: list[str] = [""]  # set by _splice_and_verify per check
    # One reader for both crate trees (the reconstruction's `src/`, the
    # oracle's `tests/`): the pull route and the evidence block's panic-site
    # quote read the same files under the same containment.
    _read_source = reconstruction_file_reader(target_src_dir, project_dir)
    _pull_sources = PullSources(
        read_file=_read_source,
        find_tests=lambda name: "\n\n".join(
            f"// {p}\n{t}" for p, t in _find_tests_referencing_fn(
                tests_dir_files, name, max_blocks=4)),
        type_def=lambda name: (_types_section_for_fix(
            name, "", "", "", "", _crate_type_records)
            # Crate records win; well-known unstable std APIs answer as a
            # last fallback so the pull never starves on a std type.
            or std_api_def(name)),
        full_error=lambda: strip_ansi(_last_full_out[0]),
    )
    _pull_menu = (pull_menu_section(_pull_sources.available_kinds(), _max_pulls)
                  if _pulls_on else "")

    def _ask_fill(prompt: str, *, followup_think: bool = False) -> str:
        """The shared ask seam: pull round-trips when enabled, plain ask
        otherwise. Deliverable-first — FN bodies short-circuit.

        `followup_think` is the escalated grade's completion policy, gated
        by `PHASE_C_FOLLOWUP_THINK` (default off). On the strength dialect
        the thinking-exhausted followup otherwise runs at the lowest rung
        with no thinking reserve, and on run `82157-476ddc16` (2026-08-26)
        every one of the 8 file-first replies — the constructor rewrite
        included — was written by that followup (guard
        `thinking_len=71584 result_len=8192`). Accessor and stub calls keep
        today's followup; an escalated write list asks for the reserve only
        when the env switch is on (after the reserve sits above Muse@high's
        measured p90)."""
        if not _pulls_on:
            return str(ask(prompt, model=_TASK2_MODEL,
                           followup_think=followup_think))
        return ask_with_pulls(
            lambda p: str(ask(p, model=_TASK2_MODEL,
                              followup_think=followup_think)),
            prompt + _pull_menu,
            sources=_pull_sources,
            has_deliverable=lambda r: bool(extract_fn_bodies(r)),
            log=_log, emit=_emit, max_pulls=_max_pulls)

    # Dispatch families: autoref-specialization trait sets dispatched
    # by a macro. Their correctness is a cross-impl invariant, so when a block
    # belongs to one we co-design the whole family — broaden related_blocks to
    # every family-trait block and inject the dispatching macro as the spec.
    _families = _dispatch_families_via_extractor(target_src_dir) or []
    # A block belongs to a family if its container names a family trait OR one
    # of the dispatched method's return ("tag") types — the latter pulls the
    # tag's inherent impl (where the chained `new` lives) into co-design.
    _name_to_family: dict[str, DispatchFamily] = {}
    for _fam in _families:
        for _nm in list(_fam.traits) + list(_fam.return_types):
            _name_to_family[_nm] = _fam
    if _families:
        _log(f"  [phase-c/impl] {len(_families)} dispatch family(ies): "
             + "; ".join(
                 f"{f.method}←{f.traits}"
                 f"+tags{f.return_types}.{f.chained_methods}"
                 for f in _families))

    def _container_names(container: str) -> set[str]:
        return set(container.replace("<", " ").replace(">", " ")
                   .replace("+", " ").split())

    def _family_for_container(container: str) -> DispatchFamily | None:
        toks = _container_names(container)
        for _nm, _f in _name_to_family.items():
            if _nm in toks:
                return _f
        return None

    retros_cache = _load_retrospectives(project_dir)

    _impl_full_stalls = int(os.getenv("PHASE_C_IMPL_MAX_STALLS", "3"))

    # Retries here are FEEDBACK-CARRYING, not blind: every revert path below
    # sets `prior_error` (focused cargo diagnostics, the U2 self-failure
    # detail, or the regressed sibling test) and the next attempt's prompt
    # renders it. A BLIND immediate retry is the thing that fails — live-
    # observed, a re-ask with identical context reproduced the same
    # cross-wiring drop pattern — so this budget is only worth spending
    # because attempt N+1 sees what attempt N broke. It matters most for the
    # jointly-coupled files this route exists for: a whole-file scheme (a
    # vtable built by one fn and dispatched through by its siblings) cannot
    # be repaired one fn at a time, so if the joint attempt dies on a compile
    # error the per-block fallback cannot recover it either. Whatever is still
    # stubbed after this budget falls through to the per-block loop (the
    # freshness gate skips names this pass already completed) and to the retro
    # round at the end of the TDD iteration.
    _FILE_FIRST_MAX_ATTEMPTS = int(os.getenv("PHASE_C_FILE_MAX_ATTEMPTS", "3"))

    def _fill_file_first(file_rel: str, fblocks: list[ImplBlock]) -> None:
        """File-first pass: ONE prompt fills every stub of a multi-block
        file so the impls are designed together (run-187469 evidence: the
        Own/Ref/Mut family in src/ptr.rs diverged when filled per block).
        Strict gate — kept only on rc==0 (+U2 with comparative banking), else
        reverted verbatim and handed to the per-block loop.

        A file too big for one prompt is no longer skipped wholesale — that
        fragmented the coupled vtable family back to per-fn fills exactly when
        it was most-stubbed. It is partitioned into TEST-WELDED block groups
        (blocks whose attributed frontier tests overlap must be designed
        together), each within the prompt bound, each filled jointly."""
        file_path = target_src_dir / file_rel
        if not file_path.exists():
            return
        wanted_all = [m.fn_name for b in fblocks
                      for m in (*b.stub_members, *b.refill_members)]
        if not wanted_all:
            return
        # Bounds how many fns one prompt asks for; counts re-fills too — they
        # are bodies the model must write. The stubs-era env var is still
        # honoured so an existing .env keeps working.
        max_fns = int(os.getenv("PHASE_C_FILE_MAX_FNS")
                      or os.getenv("PHASE_C_FILE_MAX_STUBS") or "20")
        if len(wanted_all) <= max_fns:
            _fill_block_group(file_rel, fblocks, file_rel)
            return
        # Welding is keyed on CONVICTING tests: the coupling claim is "one
        # validation run judges both blocks", which only a test that can
        # actually reject them supports. An inferred attribution welds
        # unrelated blocks into one oversized joint prompt.
        _binputs = [BlockGroupInput(
            idx=_i,
            fn_count=len(b.stub_members) + len(b.refill_members),
            tests=frozenset(
                t for m in (*b.stub_members, *b.refill_members)
                for t in (_frontier_tests.convicting_for(
                    FnLocator(file_rel, m.fn_name)))))
            for _i, b in enumerate(fblocks)]
        _packed = coupled_fill_groups(_binputs, max_fns)
        # Blocks NO other route can take. "Handed to the per-block loop" is a
        # real handoff for a block that loop iterates and a silent drop for one
        # it does not: `viable_blocks` excludes free-fn and oversized blocks by
        # construction, which is the whole reason those two shapes arrive here.
        # Live anyhow/nightly 2026-08-04 12:12 UTC, with the frontier finally
        # naming free fns again: src/error.rs packed 35 re-fill fns into one
        # welded group of 20 and `+12 uncoupled block(s) → per-block loop`, and
        # the free-fn block holding `object_downcast`, `object_ref`,
        # `context_downcast` and `context_chain_downcast` — where the crate's
        # heap-use-after-free lives — was one of the 12. A free-fn block cannot
        # weld to anything either: welding is keyed on CONVICTING tests and an
        # escalated producer is attributed by inference, so its test set is
        # empty and it is always its own singleton component.
        _orphans = {i for i, b in enumerate(fblocks)
                    if not impl_route_covers(b, max_stubs=_PER_IMPL_MAX_STUBS)}
        # A singleton group runs only when it is one of those. Otherwise it is
        # exactly the per-block route's unit and running it in both routes
        # doubles its attempts. A single block larger than the bound keeps the
        # old skip semantics either way.
        _joint = [g for g in _packed
                  if (len(g) >= 2 or _orphans.intersection(g))
                  and sum(_binputs[i].fn_count for i in g) <= max_fns]
        _solo = len(_packed) - len(_joint)
        _stranded = sorted(_orphans - {i for g in _joint for i in g})
        if _stranded:
            # Nothing downstream will attempt these; say so where it is read,
            # rather than let the count above imply the per-block loop has them.
            _log(f"  [phase-c/file] {file_rel}: {len(_stranded)} block(s) that "
                 "NO route covers exceed the bound alone and are left unfilled: "
                 + ", ".join(
                     f"{fblocks[i].container.splitlines()[0] if fblocks[i].container else 'free fns'}"
                     f" ({_binputs[i].fn_count} fn(s))" for i in _stranded)
                 + f" — raise PHASE_C_FILE_MAX_FNS (now {max_fns}) to reach them.")
        if not _joint:
            _log(f"  [phase-c/file] {file_rel}: skipped — {len(wanted_all)} "
                 f"fn(s) exceeds PHASE_C_FILE_MAX_FNS={max_fns} and no "
                 "test-welded multi-block group fits the bound; per-block "
                 "fills will handle the blocks that route covers.")
            return
        _sizes = ", ".join(
            str(sum(_binputs[i].fn_count for i in g)) for g in _joint)
        _log(f"  [phase-c/file] {file_rel}: {len(wanted_all)} fn(s) exceeds "
             f"PHASE_C_FILE_MAX_FNS={max_fns} — split into {len(_joint)} "
             f"group(s) of {_sizes} fn(s) (test-welded, plus any block no "
             f"other route covers) (+{_solo} uncoupled block(s) → "
             "per-block loop).")
        for _gi, _g in enumerate(_joint, 1):
            _fill_block_group(file_rel, [fblocks[i] for i in _g],
                              f"{file_rel}[group {_gi}/{len(_joint)}]")

    def _fill_block_group(file_rel: str, fblocks: list[ImplBlock],
                          glabel: str) -> None:
        """One joint fill cycle over ``fblocks`` — the whole file, or one
        test-welded group of its blocks: prompt → splice → compile → U2 over
        the group's attributed tests (with comparative banking of strict
        self-failure improvement) → global regression gate."""
        nonlocal fns_filled_total, attempts_total
        file_path = target_src_dir / file_rel
        stub_names = [m.fn_name for b in fblocks for m in b.stub_members]
        refill_names = [m.fn_name for b in fblocks for m in b.refill_members]
        wanted = [*stub_names, *refill_names]
        if not wanted:
            return
        with _counters_lock:
            dispatched_keys.update(FnLocator(file_rel, n) for n in wanted)
        snapshot = file_path.read_text(encoding="utf-8", errors="replace")
        test_blocks: list[tuple[str, str]] = []
        # The evidence behind each re-fill in this file: attributed tests,
        # DIRECT vs INFERRED, same-named candidates, and what the tests said.
        # The attributed tests' SOURCE goes in first — they are the tests the
        # "rewrite" instruction is about; the name-match heuristic below
        # found `tests/test_source.rs` for `source` and nothing for
        # `ext_context` while the frontier named `test_iter`/`test_len`
        # (run `902902-daee577d`, 2026-08-20).
        _file_refill_evs = {
            _impl_block_id(b.file, b.container):
                collect_refill_evidence(b, fblocks, _frontier_tests,
                                        escalated=escalated_keys,
                                        read_source=_read_source,
                                        history=history_for_block(
                                            b, ledger_entries,
                                            oracle_fp=oracle_fp))
            for b in fblocks}
        # …capped by distinct FILES, not by leaf names (`attributed_test_blocks`
        # carries the measurement).
        test_blocks = attributed_test_blocks(
            attributed_test_leaves(
                (e for evs in _file_refill_evs.values() for e in evs),
                cap=_ATTRIBUTED_TEST_LEAF_CAP),
            lambda leaf: _find_tests_referencing_fn(
                tests_dir_files, leaf, max_blocks=1),
            file_cap=_ATTRIBUTED_TEST_FILE_CAP,
            into=test_blocks)
        # Pins PER BLOCK, from that block's own attributed tests. One
        # file-wide pin list handed to every block put `test_downcast`'s
        # `downcast::<&str>` on the `BothDebug` plan line of run
        # `82157-476ddc16`'s ensure prompt (`logs/prompts/97e9b596….txt`,
        # line 6): the constructors' pins on a unit whose tests pin `ensure!`
        # messages. The test texts are the file-capped union; a block's leaf
        # is sliced out of them by NAME, so a sibling block's file
        # contributes nothing to this block's pins, and a block with no
        # attributed test gets none.
        _test_texts = tuple(t for _, t in test_blocks)
        _block_pins = {
            bid: pinned_observables_from_tests(
                attributed_test_leaves(evs, cap=_ATTRIBUTED_TEST_LEAF_CAP),
                _test_texts)
            for bid, evs in _file_refill_evs.items()}
        for fn_name in wanted[:6]:
            for path_str, txt in _find_tests_referencing_fn(
                    tests_dir_files, fn_name, max_blocks=2):
                if not any(p == path_str for p, _ in test_blocks):
                    test_blocks.append((path_str, txt))
        _bp_slice = "" if _blueprint.empty else _blueprint.slice_for(
            frozenset([*wanted,
                       *(t for b in fblocks
                         for t in _container_names(b.container)),
                       Path(file_rel).stem]),
            cap=int(os.getenv("BLUEPRINT_SLICE_CAP", "2500")))
        # Any localized crash inside THIS file applies: the prompt renders the
        # whole file, so a crashing body is visible as an idiom to imitate.
        # (The notes read happens BELOW the offer computation: 204 N2 needs to
        # know whether this prompt carries a measured producer grade before it
        # can decide which remembered lessons may enter.)
        _file_crash = "\n\n".join(dict.fromkeys(
            txt for loc, txt in (crash_evidence or {}).items()
            if loc.file == file_rel))
        # The writable COMPLEMENT of this file's escalated producers: fns the
        # producer could INSTALL into a table it builds. They are not
        # `refill_members` and never can be — a free fn with a compiling body
        # and no attributed test is in no frontier, so no batch can contain
        # it, and across three runs they were writable slots in 0 of 39
        # file-scope prompts while being pasted into the same prompts as
        # read-only text. The splicer was never the barrier:
        # `joint_overwrite_names` already lands any fn of the file the reply
        # answers once the reply answers a re-fill target, so this is a PLAN
        # entry, not a dispatch change. Computed ONCE per file: the tree the
        # attempts below see is the same snapshot (a failed attempt reverts),
        # and the extractor run is not free.
        _all_fn_records = _enumerate_fns_via_extractor(target_src_dir) or []
        # Whether an ESCALATED member of each block WRAPS — constructs a
        # crate tuple struct inside another call's argument list
        # (`wraps_crate_tuple_struct`) — read off the extractor's body of that
        # member, fresh from disk (the same records `installable_fns` reads;
        # the block's own member record is the fallback). Gates the TypeId
        # sentence on the plan line AND the evidence block: the pins say what
        # the tests observe, this says what the body does.
        _bodies_here = {
            (str(r.get("fn_name") or ""),
             canon_container(str(r.get("container") or ""))):
                str(r.get("body_text") or "")
            for r in _all_fn_records if r.get("file") == file_rel}

        def _wraps(b: ImplBlock) -> bool:
            names = escalated_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)])
            return any(
                wraps_crate_tuple_struct(
                    _bodies_here.get(
                        (m.fn_name, canon_container(b.container)), m.body_text),
                    _tuple_structs)
                for m in b.refill_members if m.fn_name in names)

        _block_wraps = {_impl_block_id(b.file, b.container): _wraps(b)
                        for b in fblocks}
        _file_refill_evidence = "\n".join(
            f"in `{container_header(b.container) or 'free fns'}`:\n"
            + render_refill_evidence(
                _file_refill_evs[_impl_block_id(b.file, b.container)],
                pins=_block_pins[_impl_block_id(b.file, b.container)],
                wraps=_block_wraps[_impl_block_id(b.file, b.container)])
            for b in fblocks
            if _file_refill_evs[_impl_block_id(b.file, b.container)])
        # The bypass-only and installable-only plans render no ESCALATED
        # clause, so their pins reach no line today; they carry the pins of
        # the block(s) whose escalated producer they complement, so a
        # renderer that grows one reads the right tests. Block pin strings
        # are unioned whole — a literal pin may itself contain ", ".
        _union_pins = ", ".join(dict.fromkeys(
            s for s in _block_pins.values() if s))

        def _pins_for_bypass(entries: list[ProducerBypass]) -> str:
            producers = {e.producer for e in entries}
            return ", ".join(dict.fromkeys(
                _block_pins[_impl_block_id(b.file, b.container)]
                for b in fblocks
                if producers & escalated_refill_names(
                    _file_refill_evs[_impl_block_id(b.file, b.container)])
                and _block_pins[_impl_block_id(b.file, b.container)]))
        _offer_keys = offer_producers(escalated_measured, escalated_keys)
        _installable = installable_fns(
            _all_fn_records, _crate_type_records, _offer_keys, file_rel,
            reference_counts=fn_reference_counts(snapshot))
        # The closure's OTHER population: fns of THIS file that construct a
        # wrapper an escalated producer erases while bypassing that producer
        # (`Error::context` building `ContextError` through `Self::new`, run
        # `40938-f4e8e94b`). Keyed on the wrapper TYPE, not on the producer's
        # file: the producer is pub(crate), and the record shows the bypass
        # class crosses files (`src/context.rs::ext_context`, both impls).
        _bypasses = producer_bypasses(
            _all_fn_records, _crate_type_records, _offer_keys, file_rel)
        # The plan carries the DOMAIN objects: the renderer reads
        # `erases_generic`/`same_file`/`producer_file` off one source of
        # truth (`ProducerBypass`) instead of a parallel tuple encoding —
        # the display prose, call-form-only, is derived in `_plan_line`.
        _bypass_by_container: dict[str, list[ProducerBypass]] = {}
        for _bp in _bypasses:
            _bypass_by_container.setdefault(_bp.container, []).append(_bp)

        def _bypass_for(container: str) -> tuple[ProducerBypass, ...]:
            return tuple(_bypass_by_container.get(
                " ".join(container.split("{", 1)[0].split()), ()))

        _free_bypass = _bypass_for("")
        _free_plan = ([BlockFillPlan(
            "", (),
            tuple(dict.fromkeys((*(i.fn_name for i in _installable),
                                 *(x.fn_name for x in _free_bypass)))),
            installable=tuple(i.fn_name for i in _installable),
            bypass=_free_bypass, pins=_union_pins)]
            if _installable or _free_bypass else [])
        # The evidence section's entry for them: the relation, stated per fn,
        # so the model can see WHICH table field each one answers to. The
        # refill-evidence renderer is per impl block and these are free fns.
        _install_evidence = ""
        if _installable:
            _install_evidence = "in `free fns`:\n" + "\n".join(
                f"`{i.fn_name}` — INSTALLABLE: an escalated producer in this "
                f"file builds a `{i.type_name}` literal whose field "
                f"`{i.field}` has this fn's signature"
                + (" (the name occurs nowhere in the file but its own "
                   "declaration)" if i.references <= 1 else
                   f" ({i.references} occurrences in the file)")
                + ". Rewrite it together with the producer if it is not "
                "right for the type the producer erases."
                for i in _installable)
            _log(f"  [phase-c/file] {file_rel}: {len(_installable)} "
                 "declared fn(s) an escalated producer could install are "
                 "writable in this prompt: "
                 + ", ".join(f"{i.fn_name}->{i.type_name}.{i.field}"
                             for i in _installable[:5]))
            _emit("phase_c.file.installable_offered", data={
                "cluster": cluster_name, "file": file_rel,
                "fns": [i.fn_name for i in _installable]})
        _bypass_evidence = ""
        if _bypasses:
            # The evidence line carries the same instruction split as the plan
            # line: the producer's name appears ONLY for a generic-self hit
            # (an evidence sentence naming the call reads as an instruction to
            # make it — the compliance-over-compiler class this split
            # removes), and the table form splits on `same_file` for the same
            # reason the plan line does: the table machinery carries no `pub`,
            # so "build it in this body" is only a possible instruction in
            # the file that declares it. No claim about what the wrapped
            # value IS — `erases_generic=False` covers the undecidable cases.
            def _bp_evidence(_bp: ProducerBypass) -> str:
                if _bp.erases_generic:
                    return (
                        f"`{_bp.fn_name}` — BYPASS: constructs "
                        f"`{_bp.wrapper}` without invoking its typed producer "
                        f"`{_bp.producer}` "
                        f"({'this file' if _bp.same_file else _bp.producer_file}), "
                        "so the per-type table that producer builds is "
                        "installed on nothing this body creates — the failing "
                        "downcasts walk that table.")
                if _bp.same_file:
                    return (
                        f"`{_bp.fn_name}` — BYPASS: constructs "
                        f"`{_bp.wrapper}` and hands it to the generic "
                        "construction path, so the per-type table its values "
                        "need is installed on nothing this body creates. "
                        f"Build `{_bp.wrapper}`'s own table in this body from "
                        "the table fns of this file; a producer-call "
                        "instruction on a different impl in this prompt does "
                        "not apply here.")
                return (
                    f"`{_bp.fn_name}` — BYPASS: constructs `{_bp.wrapper}` "
                    "and hands it to the generic construction path, so the "
                    "per-type table its values need is installed on nothing "
                    "this body creates. The machinery that builds that table "
                    f"lives in `{_bp.producer_file}`, not this file — do not "
                    f"construct `{_bp.wrapper}` here; delegate the wrap to "
                    "the path that file declares for its inner value. A "
                    "producer-call instruction on a different impl in this "
                    "prompt does not apply here.")

            _bypass_evidence = "\n".join(
                f"in `{_bp.container or 'free fns'}`:\n" + _bp_evidence(_bp)
                for _bp in _bypasses)
            _log(f"  [phase-c/file] {file_rel}: {len(_bypasses)} fn(s) "
                 "construct a wrapper an escalated producer erases without "
                 "invoking its typed producer — writable in this prompt: "
                 + ", ".join(
                     f"{_bp.fn_name}->{_bp.wrapper} "
                     + (f"via {_bp.producer}" if _bp.erases_generic
                        else "(build the per-type table)" if _bp.same_file
                        else "(route to the per-type table)")
                     for _bp in _bypasses[:5]))
            _emit("phase_c.file.producer_bypass_offered", data={
                "cluster": cluster_name, "file": file_rel,
                "fns": [_bp.fn_name for _bp in _bypasses],
                "forms": ["call" if _bp.erases_generic
                          else "table-build" if _bp.same_file
                          else "table-route"
                          for _bp in _bypasses],
                # Producer names only for the call form: putting them on a
                # table-form-only offer reintroduces the name the instruction
                # exists to keep out of that prompt (events never reach a
                # prompt, but the record should read as the prompt does). The
                # wrapper names keep the RELATION auditable when the producer
                # set is empty — which wrapper's census fired, per fn.
                "producers": sorted({_bp.producer for _bp in _bypasses
                                     if _bp.erases_generic}),
                "wrappers": [_bp.wrapper for _bp in _bypasses]})
        # The full plan is loop-invariant: per-block entries carry their bypass
        # complement, and a bypass whose impl block is not in this dispatch
        # gets its own entry — the splice's joint-overwrite widening already
        # lands any declared member the reply answers once a re-fill target
        # answers, so this is a PLAN entry, not a dispatch change.
        _block_plans = [BlockFillPlan(
            b.container,
            tuple(m.fn_name for m in b.stub_members),
            tuple(dict.fromkeys((*(m.fn_name for m in b.refill_members),
                                 *(x.fn_name
                                   for x in _bypass_for(b.container))))),
            inferred=tuple(sorted(inferred_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            escalated=tuple(sorted(escalated_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            bypass=_bypass_for(b.container),
            pins=_block_pins[_impl_block_id(b.file, b.container)],
            wraps_pinned_type=_block_wraps[_impl_block_id(b.file, b.container)])
            for b in fblocks]
        _covered = {" ".join(b.container.split("{", 1)[0].split())
                    for b in fblocks} | {""}
        _extra_plans = [BlockFillPlan(
            _c, (), tuple(x.fn_name for x in _entries),
            bypass=tuple(_entries), pins=_pins_for_bypass(_entries))
            for _c, _entries in sorted(_bypass_by_container.items())
            if _c not in _covered]
        # The grade label naming what this prompt's write list actually
        # carries so the exclusion log reads true on every file — a context.rs
        # prompt can be bypass-only with zero installables, and a bypass
        # prompt can carry either instruction form (or both: the two
        # ext_context impls split call/table on the same file).
        _grade_parts = [g for g, present in (
            ("REWRITE WITH THE PRODUCER", bool(_installable)),
            ("REWRITE TO INVOKE THE PRODUCER",
             any(_bp.erases_generic for _bp in _bypasses)),
            ("BUILD THE PER-TYPE TABLE",
             any(not _bp.erases_generic and _bp.same_file
                 for _bp in _bypasses)),
            ("ROUTE TO THE PER-TYPE TABLE",
             any(not _bp.erases_generic and not _bp.same_file
                 for _bp in _bypasses)),
        ) if present]
        if not _grade_parts and any(p.escalated for p in _block_plans):
            _grade_parts = ["ESCALATED REWRITE"]
        notes_section = ""
        if fr_path is not None:
            notes_section = _filter_model_notes_for_prompt(
                fr_path, file_rel=file_rel, fn_name=wanted[0],
                file_text=snapshot, current_error="",
                escalated_fns=frozenset(
                    k.fn_name for k in escalated_keys if k.file == file_rel),
                escalated_grade=" + ".join(_grade_parts),
                writable_fns=frozenset(
                    n for p in (*_free_plan, *_block_plans, *_extra_plans)
                    for n in (*p.stubs, *p.refills)))
        # Screen every retro key this prompt's units can honour: the *impl*
        # record per block AND each fill-plan unit the per-fn route stamps.
        # Honour/retire lines keep the STORE key so a grep lands on the
        # record that steered this prompt. Duplicate texts (the same lesson
        # under two keys) join once.
        _file_retro_parts: list[str] = []
        _seen_retro: set[str] = set()
        _by_container = {b.container: b for b in fblocks}
        for _rf, _rfn, _rcont in file_first_guidance_keys(
                file_rel, fblocks, extra_plans=(*_free_plan, *_extra_plans)):
            _fk = _retro_key(_rf, _rfn, _rcont)
            _frec = retros_cache.get(_fk, {})
            _blk = _by_container.get(_rcont)
            _fverdict = screen_guidance(
                _frec, snapshot,
                evidence=ConvictionEvidence(
                    _answers_for_block(_blk) if _blk is not None else frozenset()),
                anchor_types=anchor_types_for_container(_rcont),
                cluster=cluster_name, cfg_dead=_cfg_dead_fns(_rf))
            if (_hline := honoured_guidance_line(_fk, _frec, _fverdict.text)):
                _log(f"  {_hline}")
            if (_rline := retired_guidance_line(_fk, _frec, _fverdict)):
                _log(f"  {_rline}")
            _text = _fverdict.text.strip()
            if _text and _text not in _seen_retro:
                _seen_retro.add(_text)
                _file_retro_parts.append(_fverdict.text)
        _file_retro = "\n\n".join(_file_retro_parts)
        prior_error = ""
        _prev_prompt: str | None = None
        for _attempt in range(1, _FILE_FIRST_MAX_ATTEMPTS + 1):
            # TYPES_IN_SCOPE selected over the WHOLE file, which is this
            # route's unit — the per-impl path searches one block's member
            # signatures because that is all it may write.
            _file_types = _types_section_for_fix(
                "", snapshot, prior_error, "", "", _crate_type_records)
            prompt = build_file_fill_prompt(
                file_rel=file_rel, file_text=snapshot,
                fill_plan=[*_free_plan, *_block_plans, *_extra_plans],
                tests_block="\n\n".join(f"// {p}\n{t}" for p, t in test_blocks),
                refill_evidence="\n".join(
                    s for s in (_file_refill_evidence, _install_evidence,
                                _bypass_evidence) if s),
                blueprint=_bp_slice, notes=notes_section, prior_error=prior_error,
                crash_evidence=_file_crash,
                retrospective_guidance=_file_retro,
                module_name=module_name, cluster_name=cluster_name,
                gotchas=_gotchas_summary(), types_section=_file_types,
                invariants_section=invariants_section)
            # A rebuilt prompt byte-identical to the previous attempt's is a
            # temperature resample, not a retry: every input — the snapshot,
            # the plan, and above all `prior_error` — came out the same, so no
            # new information reaches the model. This route retries on the
            # feedback in `prior_error` and has no repeated-signature exit, so
            # an identical revert reason looped it verbatim:
            # `src/ensure.rs::render` sent the SAME prompt sha on attempts 2
            # and 3 (2026-08-19 19:45:30 and 19:53:41 UTC; attempt 1
            # differed), reverted all three times on the identical 11-test
            # newly-red list, then fell back to per-block anyway.
            if prompt == _prev_prompt:
                _log(f"  [phase-c/file] {glabel}: attempt {_attempt} rebuilt "
                     "a byte-identical prompt — the revert feedback did not "
                     "change, so another call buys a temperature resample of "
                     "the same question; falling back to per-block fills.")
                _emit("phase_c.file.fill.resample_skipped", data={
                    "cluster": cluster_name, "file": file_rel, "group": glabel,
                    "attempt": _attempt})
                return
            _prev_prompt = prompt
            _log(f"  [phase-c/file] {glabel}: file-first fill "
                 f"({len(fblocks)} block(s), {len(stub_names)} stub(s), "
                 f"{len(refill_names)} re-fill(s), "
                 f"attempt {_attempt}/{_FILE_FIRST_MAX_ATTEMPTS}).")
            _emit("phase_c.file.fill.start", data={
                "cluster": cluster_name, "file": file_rel, "group": glabel,
                "blocks": len(fblocks), "stubs": len(stub_names),
                "refills": len(refill_names)})
            attempts_total += 1
            try:
                _file_reply = _ask_fill(
                    prompt, followup_think=escalated_followup_think(_grade_parts))
                parsed = extract_fn_bodies(_file_reply)
            except Exception as e:
                _log(f"  [phase-c/file] {file_rel}: LLM error: {e}")
                return
            _note_reader_loss(_log, _emit, cluster=cluster_name,
                              unit=file_rel, route="file",
                              reply=_file_reply, parsed=parsed)
            if not parsed:
                _log(f"  [phase-c/file] {file_rel}: empty/no FN blocks "
                     f"({fn_tag_occurrences(_file_reply)} FN tag(s) in reply) — "
                     "falling back to per-block fills.")
                return
            with _splice_check_lock:
                # Runtime test state for the monotone ratchet, captured NOW
                # (inside the lock, right before this splice) rather than
                # before acquiring it — under PIPELINE_PARALLELISM>1 several
                # workers queue for this same lock, and a baseline captured
                # before that queueing goes stale by the time this worker's
                # turn arrives (other queued workers' accepted changes land
                # first), misattributing THEIR regressions to whichever fn
                # happens to check right after. None ⇒ crate not
                # test-runnable now → gate self-disables, verbatim revert
                # unchanged.
                _base_raw: list[str] = []
                _baseline_test = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env, _base_raw)
                # The pre-fill panic SITES. The reading carries names only, and
                # without a site the non-regression keep below cannot tell the
                # correct body from the one that walks downhill — both are red
                # on the same tests.
                _base_sites = _panic_sites_of(_base_raw)
                containers = [b.container for b in fblocks]
                # Route-only widening: `fblocks` is only this file's
                # STILL-NEEDS-FILLING blocks, but the model sees the WHOLE file
                # text and can (correctly, for genuinely coupled types) answer
                # an already-complete sibling too — route those bodies there
                # instead of dropping them; the freshness gate downstream makes
                # an already-filled target a safe no-op. Reads the FULL block
                # list: the fill set cannot widen routing beyond itself. Kept
                # out of `containers[0]`/target selection, which stays exactly
                # as before.
                _extra_containers = [c for c in containers_in_file(all_blocks, file_rel)
                                     if c not in containers]
                # The member map is what lets a body the model tagged with
                # something other than an impl header still reach its
                # declaration — a `// free function` comment, or the fn's own
                # signature line. Read off the FULL block list for the same
                # reason `_extra_containers` is: the tier fires on a name the
                # file declares exactly once, and a narrowed list makes names
                # look unique that are not.
                routing = route_fn_bodies(
                    parsed, target=containers[0],
                    related=containers[1:] + _extra_containers,
                    trait_defaults=trait_default_hosts(all_blocks, file_rel),
                    members=members_in_file(all_blocks, file_rel))
                for _db in routing.dropped:
                    _log(f"  [phase-c/file] {file_rel}: dropped `{_db.fn_name}` — "
                         f"block header does not match any impl in this file. "
                         f"raw_block={_db.block_attr!r} raw_canon={_db.attr_canon!r}")
                spliced_now = 0
                _landed: list[LandedBody] = []
                # Refill targets are already-filled-but-WRONG bodies; the splice
                # must be told to overwrite a non-stub for exactly those names,
                # or the model's correction is refused `target_not_stub` and
                # silently dropped. Authorization spans the whole file because
                # this route routes bodies file-wide, and — once the reply
                # answers a refill target at all — the file's OTHER members it
                # answered too, because a soundness defect is a property of a
                # scheme rather than of the one fn a failing test convicts.
                # This route stands or reverts as one, so the gates below are
                # what protect the tree; the authorization only decides whether
                # a joint correction may be joint.
                _refill_names = joint_overwrite_names(
                    all_blocks, file_rel,
                    (rb.fn_data["fn_name"] for rb in routing.routed))
                for rb in routing.routed:
                    wc = rb.fn_data["where_clause"]
                    if wc and (rb.via_trait_default
                               or signature_fixed_by_trait(rb.container)):
                        wc = None
                    _fn = rb.fn_data["fn_name"]
                    ok, reason, _delta = _splice_fn_with_where(
                        file_path, rb.container, _fn,
                        rb.fn_data["body"], wc,
                        allow_nonstub=_fn in _refill_names,
                        sig_text=rb.sig_text)
                    if ok:
                        spliced_now += 1
                        _landed.append(LandedBody(
                            _fn, rb.container, rb.sig_text,
                            rb.fn_data["body"], wc,
                            overwrite_authorized=_fn in _refill_names))
                        if rb.via_member_name:
                            _log(f"  [phase-c/file] {file_rel}: `{_fn}` was "
                                 f"tagged {rb.fn_data['block']!r}, which names "
                                 "no impl — routed to the one declaration this "
                                 "file has of that name"
                                 + (" (a free fn)." if not rb.container
                                    else f", in `{rb.container.splitlines()[0]}`."))
                        if rb.via_trait_default:
                            _log(f"  [phase-c/file] {file_rel}: `{_fn}` was "
                                 f"tagged {rb.fn_data['block']!r} (an empty "
                                 "override) — routed to its default body in "
                                 f"`{rb.container}`.")
                        if reason == "ok_xml_unescaped":
                            _log(f"  [phase-c/file] {file_rel}: `{_fn}` landed "
                                 "only after resolving XML entities in the body "
                                 "— the reply used the escaped rendering.")
                        if reason == "ok_own_brace_dropped":
                            _log(f"  [phase-c/file] {file_rel}: `{_fn}` landed "
                                 "only after dropping the fn's own closing "
                                 "brace — the reply closed the body itself.")
                    else:
                        # `target_not_stub` is unfalsifiable from the log alone —
                        # the body is reverted before anyone reads it. Name the
                        # container it routed to and whether an EARLIER body in
                        # this same reply already filled the fn, which is the
                        # difference between a duplicate answer and a stale
                        # stub record.
                        _dup = [b.container for b in _landed
                                if b.fn_name == _fn]
                        _log(f"  [phase-c/file] {file_rel}: splice "
                             f"`{_fn}` failed: {reason} "
                             f"[routed to `{rb.container}`, "
                             + (f"already filled earlier in this reply via "
                                f"`{_dup[0]}`" if _dup else
                                f"overwrite {'allowed' if _fn in _refill_names else 'not authorized'}")
                             + "]")
                # File-first is the ATOMIC co-dependent fill: it must land a
                # file's FREE functions together with its impls, else a
                # producer/consumer cycle (the free `not(cond)` that the `Bool`
                # impls' tests call) can never validate — each half reverts
                # while the other is a stub. Routing's member-identity tier now
                # reaches a free fn the file declares exactly ONCE; what still
                # arrives here is the ambiguous remainder — a name declared in
                # several of the file's inline mods, or one the block records
                # do not cover. Route each such dropped body onto a still-stubbed
                # module-level fn, disambiguating by the stub's verbatim
                # signature (a free fn has no impl container to narrow on).
                _free_stubs: dict[str, list[dict[str, Any]]] = {}
                for r in (_scan_unimplemented_records_for_cluster(
                        target_src_dir, cluster) or []):
                    # Full normalized rel-path equality only — a basename
                    # fallback can hit a same-named file in another directory
                    # (two `mod.rs`). Extractor paths may carry a `src/`
                    # prefix the block records lack; strip it on both sides.
                    if _norm_src_rel(r.get("file", "")) != _norm_src_rel(file_rel):
                        continue
                    # Only module-level stubs may receive a rerouted body:
                    # free fns ("" or `mod …`) and trait default methods
                    # (`trait …`). Impl methods are container-scoped by the
                    # main routing and must never be reachable by bare name.
                    _cont = str(r.get("container", "")).strip()
                    if _cont and not _cont.startswith(("mod ", "trait ")):
                        continue
                    _free_stubs.setdefault(r["fn_name"], []).append(r)
                if _free_stubs and routing.dropped:
                    _dropped_keys = {(_db.fn_name, _db.block_attr)
                                     for _db in routing.dropped}
                    for _p in parsed:
                        if (_p["fn_name"], _p["block"]) not in _dropped_keys:
                            continue
                        # A dropped body ADDRESSED TO AN IMPL header was aimed
                        # at an impl method, never the free fn — landing it in
                        # the free stub by shared name is the same-name
                        # corruption container scoping exists to prevent
                        # (WRONG: `!self` from `impl Bool for bool` spliced
                        # into the self-less free `fn not(cond)` → E0424).
                        if _p["block"].lstrip().startswith("impl"):
                            continue
                        _cands = _free_stubs.get(_p["fn_name"], [])
                        if len(_cands) > 1:
                            # Same fn name stubbed in several mods of this
                            # file: bind only on an exact container match from
                            # the body's own block attr; guessing splices into
                            # the wrong mod's stub.
                            _blk = _p["block"].strip()
                            _cands = [c for c in _cands
                                      if str(c.get("container", "")).strip()
                                      in (_blk, f"mod {_blk}", f"trait {_blk}")]
                        if len(_cands) != 1:
                            if _p["fn_name"] in _free_stubs:
                                _log(f"  [phase-c/file] {file_rel}: free-fn "
                                     f"reroute of `{_p['fn_name']}` refused — "
                                     f"{len(_cands) or 'no unambiguous'} "
                                     "matching module-level stub(s).")
                            continue
                        _rec = _cands[0]
                        _ok, _reason, _d = _splice_fn_with_where(
                            file_path, str(_rec.get("container", "")),
                            _p["fn_name"], _p["body"], _p["where_clause"],
                            sig_text=str(_rec.get("sig_text", "")))
                        if _ok:
                            spliced_now += 1
                            _landed.append(LandedBody(
                                _p["fn_name"],
                                str(_rec.get("container", "")),
                                str(_rec.get("sig_text", "")),
                                _p["body"], _p["where_clause"]))
                            _log(f"  [phase-c/file] {file_rel}: routed free fn "
                                 f"`{_p['fn_name']}` to its module-level stub.")
                        else:
                            _log(f"  [phase-c/file] {file_rel}: free-fn splice "
                                 f"`{_p['fn_name']}` failed: {_reason}")
                if spliced_now == 0:
                    _log(f"  [phase-c/file] {glabel}: nothing spliced — "
                         "falling back to per-block fills.")
                    return
                _attempted_names = [b.fn_name for b in _landed]
                rc, cargo_out, _diags = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                if rc != 0:
                    _rep = repair_missing_imports(
                        _run_cargo_check_diag, check_cmd, target_src_dir,
                        cluster_env or {}, file_rel, _diags, _log,
                        "[phase-c/file]")
                    if _rep is not None:
                        rc, cargo_out, _diags = _rep
                if rc != 0 and len(_landed) > 1:
                    # All-or-nothing at this width never lands anything: see
                    # `peel_revert`. Revert only the bodies the compiler
                    # actually blames and re-check, so the reply's correct
                    # bodies survive its wrong ones. On a refusal nothing was
                    # kept back and the whole-file revert below runs
                    # unchanged — but the refusal is logged and emitted:
                    # run `57025-6ba652f2`'s three error.rs reverts left no
                    # record of their peel path, and which branch declined
                    # was unrecoverable afterwards.
                    _peel, rc, _p_out, _p_diags = peel_file_first(
                        file_path=file_path, snapshot=snapshot,
                        file_rel=file_rel, landed=list(_landed), diags=_diags,
                        target_src_dir=target_src_dir, check_cmd=check_cmd,
                        cluster_env=cluster_env,
                        implicated_keys_from_diags=_implicated_keys_from_diags,
                        run_cargo_check_diag=_run_cargo_check_diag,
                        splice_fn_with_where=_splice_fn_with_where,
                        norm_src_rel=_norm_src_rel)
                    if isinstance(_peel, PeelResult):
                        cargo_out, _diags = _p_out, list(_p_diags)
                        _landed = list(_peel.kept)
                        spliced_now = len(_landed)
                        _note_reverted(file_rel, _peel.reverted)
                        _log(f"  [phase-c/file] {glabel}: PEELED "
                             f"{len(_peel.reverted)} body(ies) the compiler "
                             f"blamed ({', '.join(sorted(_peel.reverted))}) — "
                             f"the remaining {spliced_now} compile "
                             f"(round {_peel.rounds}); judged below as usual.")
                        _emit("phase_c.file.fill.peeled", data={
                            "cluster": cluster_name, "file": file_rel,
                            "group": glabel,
                            "reverted": sorted(_peel.reverted),
                            "kept": spliced_now, "rounds": _peel.rounds})
                    else:
                        _log(f"  [phase-c/file] {glabel}: peel refused "
                             f"({_peel.reason}) after {_peel.rounds} "
                             "round(s)"
                             + (f" — {len(_peel.dropped)} body(ies) dropped "
                                "before refusing"
                                if _peel.dropped else "")
                             + "; whole-file revert stands.")
                        _emit("phase_c.file.fill.peel_refused", data={
                            "cluster": cluster_name, "file": file_rel,
                            "group": glabel, "reason": _peel.reason,
                            "rounds": _peel.rounds,
                            "dropped": sorted(_peel.dropped)})
                _u2_file: U2Verdict | None = None
                if rc == 0:
                    # Stubs AND re-fills. A behavioral re-fill block has no stub
                    # left, so keying this gate on stubs alone hands an all-refill
                    # file an EMPTY test set — and an empty set is an
                    # unconditional ACCEPT, meaning the failing tests the re-fill
                    # exists to fix are never run and the bodies are kept on
                    # compile alone.
                    # Plus everything this reply actually LANDED. The plan is
                    # built from the file's fillable impl blocks, which exclude
                    # its free fns; the model answers for them anyway and
                    # routing now lands them, so a gate keyed to the plan alone
                    # judges a src/error.rs rewrite of `object_downcast` and
                    # `vtable` by the tests of the impl members only — no
                    # credit for fixing them, no blame for breaking them.
                    # Judge what was written.
                    _unit_fns = frozenset(
                        FnLocator(file_rel, m.fn_name)
                        for b in fblocks
                        for m in (*b.stub_members, *b.refill_members)
                    ) | frozenset(FnLocator(file_rel, b.fn_name)
                                  for b in _landed)
                    # Conviction-scoped, NOT the dispatch set: a test with no
                    # observed link to this file can never be fixed here, so
                    # letting it reject would revert every attempt forever.
                    _file_tests: set[str] = set()
                    for _key in _unit_fns:
                        _file_tests |= _frontier_tests.convicting_for(_key)
                    # Container-qualified sites so a panic in a same-named
                    # sibling (another impl's fn) is not charged to this file.
                    _unit_sites = frozenset(
                        FnSite(file_rel, m.fn_name, b.container)
                        for b in fblocks
                        for m in (*b.stub_members, *b.refill_members)
                    ) | frozenset(FnSite(file_rel, b.fn_name, b.container)
                                  for b in _landed)
                    _u2_file = _u2_test_gate(_file_tests, _unit_sites, tdd_test_cmd,
                                             target_src_dir, cluster_env)
                # ACCEPT_EXTERNAL_ONLY (all failures blocked by another file's
                # stub) is NOT a revert — keep the fill and defer to the outer
                # loop. A REJECT may still BANK: a joint rewrite that strictly
                # shrinks the group's own failing set is progress only this
                # route can make, and demanding perfection here discarded it.
                _u2_reject = (_u2_file is not None
                              and _u2_file.decision is U2Decision.REJECT)
                _bank: JointImprovement | None = None
                _joint: JointImprovement | None = None
                if _u2_reject and _u2_file is not None and _baseline_test is not None:
                    _joint = assess_joint_improvement(
                        frozenset(_file_tests),
                        _baseline_test.passed_names,
                        _baseline_test.ignored_names,
                        _u2_file, _base_sites)
                    _bank = _joint if _joint.banks else None
                    if _joint.site_movement:
                        _log(f"  [phase-c/file] {glabel}: panic sites moved — "
                             f"{_joint.site_movement}")
                # An attempt dispatched on crash evidence is judged by a
                # metric that cannot credit it — a correct fix of a latent
                # fault moves no test while its activating sibling is
                # crash-reverted beside it. Keep on NON-REGRESSION (no NEW
                # failures against baseline; the regression gate below still
                # holds) and let the next sweep judge the joint tree.
                _crash_keep_file = (rc == 0 and _u2_reject
                                    and bool(_file_crash)
                                    and _joint is not None
                                    and not _joint.new_failures)
                if _crash_keep_file:
                    _log(f"  [phase-c/file] GUARD {glabel}: crash-evidence "
                         "re-fill KEPT on non-regression — judged for real "
                         "at the next sweep, in the joint tree.")
                # The same keep WITHOUT crash evidence: a REJECT whose every
                # self-failure was already failing at baseline is not
                # implicating — the attempt regressed nothing and the old
                # bodies fail the same tests, so reverting to them buys
                # nothing and re-charges the group for the tree's standing
                # debt. Run `2219473` (2026-08-10) reverted `src/error.rs
                # [group 1/3]` (20 re-fills) three times and `src/lib.rs`,
                # `src/chain.rs`, `src/kind.rs` behind it, all "U2 test
                # failure (no strict improvement)", all with zero newly-red —
                # 72 model calls, 137 net bytes, pass 88 → 88. Keep the
                # bodies; the next sweep judges the joint tree, and a body
                # that actually costs tests is clawed back by the batch
                # ratchet from its pre-fill snapshot.
                # …with the one gradient a set of names cannot carry. On a
                # defect where every attributed test is red before AND after,
                # `new_failures` is empty for the right body and the wrong one
                # alike, and this keep is what let the 2026-08-20 delegation
                # land over a working per-type vtable. An EARLIER panic line in
                # the same test is strictly fewer assertions passed, which is a
                # regression the name set cannot see.
                _nonregr_keep_file = (rc == 0 and _u2_reject
                                      and not _crash_keep_file
                                      and _joint is not None
                                      and not _joint.new_failures
                                      and not _joint.regressed_sites)
                if _nonregr_keep_file:
                    _log(f"  [phase-c/file] GUARD {glabel}: re-fill KEPT on "
                         "non-regression — no self-failure is newly red "
                         "(all were failing at baseline) and no attributed "
                         "test panics earlier than it did; judged at the "
                         "next sweep in the joint tree.")
                elif (rc == 0 and _u2_reject and not _crash_keep_file
                        and _joint is not None and not _joint.new_failures
                        and _joint.regressed_sites):
                    _log(f"  [phase-c/file] GUARD {glabel}: re-fill REVERTED "
                         "— nothing newly red, but "
                         f"{_joint.site_movement}. Same verdict vector, fewer "
                         "assertions reached: the old bodies were closer.")
                _regr_file = (_test_regression_gate(
                    tdd_test_cmd, target_src_dir, cluster_env, _baseline_test,
                    stub_free=stub_free)
                    if rc == 0 else None)
                if rc == 0 and (not _u2_reject or _bank is not None
                                or _crash_keep_file or _nonregr_keep_file
                                ) and _regr_file is None:
                    fns_filled_total += spliced_now
                    _log(f"  [phase-c/file] {glabel}: FILE FILLED "
                         f"{spliced_now} fn(s) in one pass"
                         + (f" — banked improvement: {_bank.summary}."
                            if _bank is not None else "."))
                    # Trust-but-verify: a splice can report ok while the fn
                    # stays a stub on disk (observed: "FILE FILLED 5" with one
                    # fn still `unimplemented!()`). A silent loss here reads
                    # as "filled" forever — re-scan and name the leftovers.
                    _left = sorted({
                        str(r.get("fn_name", "")) for r in
                        (_scan_unimplemented_records_for_cluster(
                            target_src_dir, cluster) or [])
                        if _norm_src_rel(r.get("file", ""))
                        == _norm_src_rel(file_rel)
                        and str(r.get("fn_name", "")) in set(stub_names)})
                    if _left:
                        _log(f"  [phase-c/file] {glabel}: POST-FILL MISMATCH "
                             f"— still stubbed after accept: {_left}.")
                        _emit("phase_c.file.fill.mismatch", data={
                            "cluster": cluster_name, "file": file_rel,
                            "group": glabel, "still_stubbed": _left})
                    _emit("phase_c.file.fill.done", data={
                        "cluster": cluster_name, "file": file_rel,
                        "group": glabel, "fns_filled": spliced_now,
                        "banked": _bank is not None})
                    # POSITIVE needs a test observed PASSING (see the per-fn
                    # site for the run this cost). `_bank` is the joint route's
                    # own positive-evidence object; its `resolved` set is the
                    # same question asked one level up, so the ledger reads it
                    # rather than restating the rule.
                    _passed_f = (frozenset(_bank.resolved) if _bank is not None
                                 else (_u2_file.self_passed
                                       if _u2_file is not None else frozenset()))
                    _kept_f = _crash_keep_file or _nonregr_keep_file
                    _verdict_row(
                        glabel,
                        (Disposition.KEEP_DEFERRED if _kept_f
                         else Disposition.ACCEPT),
                        ("kept on non-regression — no newly-red self-failure"
                         if _kept_f
                         else f"accepted — {len(_passed_f)} attributed test(s) "
                         "observed passing" if _passed_f
                         else "kept — compiles, but no attributed test was "
                         "observed passing (none were failing, or the scoped "
                         "run was inconclusive)"),
                        outcome=(GateOutcome.POSITIVE
                                 if _passed_f and not _kept_f
                                 else GateOutcome.UNMEASURABLE),
                        evidence=tuple(sorted(_passed_f)),
                        attempt=_attempt,
                        validity=ValidityVector(binary_completed=True),
                        model_calls=_attempt, route="file")
                    return
                file_path.write_text(snapshot, encoding="utf-8")
                _note_reverted(file_rel, _attempted_names)
                if _regr_file is not None:
                    _revert_reason = f"regressed sibling test(s): {_regr_file[0].summary}"
                    prior_error = _regr_file[1]
                elif rc == 0:
                    _revert_reason = "U2 test failure (no strict improvement)"
                    prior_error = _u2_file.detail if _u2_file is not None else ""
                else:
                    _revert_reason = "compile failed"
                    prior_error = _focus_cargo_errors(cargo_out, file_rel)
                _verdict_row(
                    glabel, Disposition.REVERT, _revert_reason,
                    outcome=GateOutcome.NEGATIVE,
                    attempt=_attempt,
                    validity=ValidityVector(binary_completed=rc == 0),
                    model_calls=_attempt, route="file")
                _more = _attempt < _FILE_FIRST_MAX_ATTEMPTS
                # The codes and the primary diagnostic go in the LOG LINE, not
                # only the event: the pattern census reads logs, and its
                # compliance-over-compiler detector keys on the same unit
                # failing consecutive attempts with an IDENTICAL primary
                # diagnostic. The class's costliest instance was this route —
                # three `reverted (compile failed)` on src/error.rs, all the
                # same `E0277` on the same instructed call (run
                # `57025-6ba652f2`) — and the bare line made those three
                # indistinguishable from three different failures.
                _first_diag = next(
                    (ln.strip() for ln in prior_error.splitlines()
                     if ln.lstrip().startswith("error[")), "")
                _log(f"  [phase-c/file] {glabel}: reverted "
                     f"({_revert_reason}"
                     + (f"; codes={sorted(ecodes_in_text(prior_error))}"
                        + (f" first: {_first_diag}" if _first_diag else "")
                        if _revert_reason == "compile failed" else "")
                     + ") — "
                     + ("retrying file-first once more."
                        if _more else "falling back to per-block fills."))
                # The route emits `start`, `done` and `mismatch` and emitted
                # NOTHING here, so a group that burned all three attempts left
                # no trace in events.jsonl at all — the only record was the
                # prose line above, which names the verdict and not the
                # evidence. src/error.rs[group 1/2] on run `360236-3e83b9d1`
                # reverted `compile failed` three times (15:56 → 16:12 UTC,
                # 2026-08-04) and neither the rustc codes nor how many bodies
                # had landed was recoverable afterwards from any log. Carry
                # the codes: `compile failed` with E0308 is the model writing
                # the wrong types, with E0119 it is a splice that duplicated an
                # item, and those are different defects.
                _emit("phase_c.file.fill.revert", data={
                    "cluster": cluster_name, "file": file_rel, "group": glabel,
                    "attempt": _attempt, "max_attempts": _FILE_FIRST_MAX_ATTEMPTS,
                    "reason": _revert_reason, "spliced": spliced_now,
                    "planned": len(stub_names) + len(refill_names),
                    "banked": _bank is not None,
                    "error_codes": sorted(ecodes_in_text(prior_error)),
                    "retrying": _more})

    def _fill_one_block(block: ImplBlock) -> None:
        """One impl block's fill: context assembly, the `_attempt_impl_fill`
        unit on `drive_fix_loop` (slice 8a f3 — replaces the hand-rolled
        while loop), and exhausted-block bookkeeping (retro + fail emit)."""
        nonlocal attempts_total, fns_filled_total, impls_filled, impls_failed
        block_id = _impl_block_id(block.file, block.container)
        # Per-file converge keying: only the converge module keeps full budget.
        block_max_stalls = _effective_stalls(_impl_full_stalls, _module_of(block.file))
        file_path = target_src_dir / block.file
        if not file_path.exists():
            return
        # Crash evidence keyed to this block's members (faulting/origin
        # stacks from a localized binary crash) — rendered into the prompt
        # and an override for the exhaustion skip: a localized crash is
        # evidence no prior attempt ever saw.
        _crash_txts = tuple(dict.fromkeys(
            txt for loc, txt in (crash_evidence or {}).items()
            if loc.file == block.file
            and loc.fn_name in {m.fn_name for m in block.stub_members}
            | {m.fn_name for m in block.refill_members}))
        _block_crash_evidence = "\n\n".join(_crash_txts)
        # Honor cross-iter exhaustion — if a prior retrospective marked
        # this block GENUINELY_HARD it's skipped. A crash-implicated block is
        # NEVER skipped: the crash frames are new information. Nor is a block
        # whose own guidance contradicts the current layout: those attempts
        # were spent chasing a field this layout does not have, so the
        # conviction counted no fair sample.
        _cur_text = ""
        with suppress(Exception):
            _cur_text = file_path.read_text(encoding="utf-8")
        _block_anchors = anchor_types_for_container(block.container)
        _retro_rec = retros_cache.get(
            _retro_key(block.file, "*impl*", block.container), {}) or {}
        # Two readings of what this block answers for: the convicting tests
        # (the acceptance gate's definition) and every attributed test,
        # escalated included. A record earned from test outcomes is stamped
        # with and read under the second; see `evidence_for_record`.
        _block_evidence = ConvictionEvidence(_convicting_for_block(block))
        _block_answers = ConvictionEvidence(_answers_for_block(block))
        _poisoned_retro = _retro_gate.record(judge_guidance(
            _retro_rec.get("guidance", ""), _cur_text,
            anchor_types=_block_anchors)) is RetroGateVerdict.RETIRED
        if _poisoned_retro:
            _log(f"  [phase-c/impl] {block_id}: retro guidance contradicts the "
                 "current layout — guidance withheld, exhaustion not honoured "
                 "(layout-contradicted).")
        # The record was written from a REGRESSION of tests this block does
        # not answer for: its lesson explains what un-reddens those, which
        # on a re-fill for other tests can be the removal of the observable
        # they pin (the ensure `SmallBuf` record, `regression_scoped`). The
        # exhaustion it carries counted attempts judged on the wrong tests.
        elif regression_scoped(_retro_rec, _block_answers):
            _poisoned_retro = True
            _log(f"  [phase-c/impl] {block_id}: retro guidance was written "
                 "from a regression of test(s) this block does not answer "
                 "for — retro guidance withheld (regression_scoped), "
                 "exhaustion not honoured (regression-scoped).")
        # The second way stored guidance goes bad, and the one the layout sieve
        # above cannot see: a claim that the crate CANNOT do this, made against
        # a tree that has since moved (or against no recorded tree at all). This
        # route is where it matters — the store's `*impl*` keys are dispatched
        # from here, and all three live instances on `anyhow`/`stable` are
        # `*impl*` records that steered every re-fill of a whole-impl block.
        elif impossibility_claim_is_unfounded(
                _retro_rec, stubbed_fn_names(_cur_text)):
            _poisoned_retro = True
            _log(f"  [phase-c/impl] {block_id}: retro guidance asserts this "
                 "cannot be done, on a tree premise that has since moved (or "
                 "that it never recorded) — guidance withheld, exhaustion not "
                 "honoured (impossibility-unfounded).")
        # A verdict earned in another context (other cluster's layout, the
        # absolute acceptance gate, the pre-group route set) sampled a
        # different problem — it cannot convict this block. Neither can one
        # drawn from tests that no longer convict these members.
        _standing = conviction_standing(
            _retro_rec, RetroProvenance.current(cluster_name, _cur_text),
            evidence_for_record(_retro_rec, convicting=_block_evidence,
                                answers=_block_answers))
        _stale_conviction = (bool(_retro_rec.get("fully_exhausted"))
                             and _standing is not ConvictionStanding.CURRENT)
        if _stale_conviction:
            _log(f"  [phase-c/impl] {block_id}: exhaustion verdict was earned "
                 f"in a different context — not honoured ({_standing.value}).")
        if (_is_fn_fully_exhausted(retros_cache, block.file, "*impl*",
                                   block.container)
                and not _block_crash_evidence and not _poisoned_retro
                and not _stale_conviction):
            _log(f"  [phase-c/impl] {block_id}: skipped (prior retro "
                 "marked exhausted).")
            return
        # Crash-blocked: a previous attempt of THIS block was reverted for a
        # crash-only regression blamed wholly outside it. While the blamed
        # file is unchanged, re-attempting buys the identical crash
        # (2026-08-10: `src/context.rs::StdErrorforE`, five reverts of
        # `binary crashed: ['tests/test_context.rs'] (net -6)`, ~4-6 model
        # calls each, ASAN blaming `src/error.rs::object_drop` throughout).
        if crash_blocked is not None:
            _cb_key = _retro_key(block.file, "*impl*", block.container)
            _cb_mark = crash_blocked.get(_cb_key)
            if _cb_mark is not None:
                if crash_block_stands(target_src_dir, _cb_mark):
                    _log(f"  [phase-c/impl] {block_id}: skipped — "
                         f"crash-blocked by a sibling's fault ({_cb_mark}); "
                         "re-admitted when the blamed file changes. No "
                         "attempt charged.")
                    return
                crash_blocked.pop(_cb_key, None)
                _log(f"  [phase-c/impl] {block_id}: blamed file moved since "
                     f"the crash-block ({_cb_mark}) — re-admitted.")
        # `stub_members` is pass-scan-time state — earlier blocks'
        # family/sibling routing may have already filled members of THIS block.
        # Re-scan before spending an LLM call; a block with nothing left is
        # COMPLETE, not failed (run 187469: `impl NotBothDebug` prompted for an
        # already-filled `__dispatch_ensure` → target_not_stub → false failure).
        _fresh_recs = _scan_unimplemented_records_for_cluster(target_src_dir, cluster)
        if _fresh_recs is not None:
            _block_canon = canon_container(block.container)
            _still_stub = {
                r["fn_name"] for r in _fresh_recs
                if r.get("file") == block.file
                and canon_container(r.get("container", "")) == _block_canon}
            _fresh_members = tuple(m for m in block.stub_members
                                   if m.fn_name in _still_stub)
            # refill_members carry no "still a stub" signal (they were never
            # stubs) — the frontier+failing-test snapshot that produced them is
            # fixed for this whole call, so they stay valid re-fill targets
            # unchanged; the eventual cargo-check + U2 gate is what actually
            # re-validates them, regardless of why they were targeted.
            if not _fresh_members and not block.refill_members:
                _log(f"  [phase-c/impl] {block_id}: already complete — all "
                     f"{len(block.stub_members)} stub member(s) filled by "
                     "earlier sibling/family fills.")
                _emit("phase_c.impl.fill.already_complete", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "members": len(block.stub_members)})
                return
            if len(_fresh_members) < len(block.stub_members):
                block = replace(block, stub_members=_fresh_members)
        # Past every skip gate — this block's members are now genuinely being
        # attempted, so they are chargeable if they end the pass still stubs.
        with _counters_lock:
            dispatched_keys.update(
                FnLocator(block.file, m.fn_name)
                for m in (*block.stub_members, *block.refill_members))
        # Related blocks (same trait, different generic) for the prompt.
        all_blocks_by_id = {_impl_block_id(b.file, b.container): b for b in blocks}
        related_blocks: list[ImplBlock] = []
        for rc in block.related_containers:
            rb = all_blocks_by_id.get(_impl_block_id(block.file, rc))
            if rb is not None:
                related_blocks.append(rb)
        # Dispatch-family co-design: if this block implements a family trait,
        # surface EVERY family-trait block (not just same-name relatives) plus
        # the dispatching macro, so the model designs the family consistently.
        _family = _family_for_container(block.container)
        dispatch_family_section = ""
        if _family is not None:
            _fam_names = set(_family.traits) | set(_family.return_types)
            _seen = {_impl_block_id(block.file, block.container)}
            _seen |= {_impl_block_id(rb.file, rb.container) for rb in related_blocks}
            for b2 in blocks:
                bid = _impl_block_id(b2.file, b2.container)
                if bid in _seen:
                    continue
                # Pull in every block of this family — the dispatch traits AND
                # the tag types' inherent impls (where the chained `new` lives).
                if _container_names(b2.container) & _fam_names:
                    related_blocks.append(b2)
                    _seen.add(bid)
            dispatch_family_section = _build_dispatch_family_section(_family)
        # Route-only widening: bodies tagged for ANY real impl in
        # the SAME FILE should splice there — an already-filled target is a
        # safe no-op via the #42 freshness gate — rather than being dropped,
        # even when that container isn't a formal trait/family match. Live
        # evidence: `impl Error`/`impl ErrorImpl` in anyhow's error.rs are
        # tightly coupled (one wraps the other) but share no trait, so
        # neither `related_containers` nor the dispatch-family heuristic ever
        # pairs them — the model still answers both together (it can see the
        # whole file), and every such body was silently dropped. Kept
        # SEPARATE from `related_blocks` (the curated PROMPT context) so
        # widening the ROUTING match set doesn't bloat every per-block
        # prompt with the rest of the file's unrelated impls.
        _route_candidates = list(related_blocks)
        _route_seen = {_impl_block_id(rb.file, rb.container) for rb in _route_candidates}
        _route_seen.add(_impl_block_id(block.file, block.container))
        for b2 in blocks:
            _bid2 = _impl_block_id(b2.file, b2.container)
            if b2.file == block.file and _bid2 not in _route_seen:
                _route_candidates.append(b2)
                _route_seen.add(_bid2)
        snapshot = file_path.read_text(encoding="utf-8")
        # Per-block context.
        stub_fn_names = [m.fn_name for m in block.stub_members]
        # Refill members need their OWN failing test(s) and
        # blueprint/notes context surfaced too, not just the stubs'.
        refill_fn_names = [m.fn_name for m in block.refill_members]
        _fill_target_names = stub_fn_names + refill_fn_names
        test_blocks: list[tuple[str, str]] = []
        # What the record supports for each re-fill member — DIRECT vs
        # INFERRED, the attributed tests, same-named candidates in the file,
        # and the tests' own failure text (`domain.refill_evidence`). The
        # attributed tests' SOURCE is looked up first: the name-match below
        # found `tests/test_source.rs` for a `source` member and nothing for
        # `ext_context`, while the frontier that stamped the re-fill named
        # `test_iter`/`test_len`/`test_rev` (run `902902-daee577d`).
        _refill_evs = collect_refill_evidence(block, blocks, _frontier_tests,
                                              escalated=escalated_keys,
                                              read_source=_read_source,
                                              history=history_for_block(
                                                  block, ledger_entries,
                                                  oracle_fp=oracle_fp))
        _inferred_refill = inferred_refill_names(_refill_evs)
        # Capped by distinct FILES, as on the file route.
        test_blocks = attributed_test_blocks(
            attributed_test_leaves(_refill_evs, cap=_ATTRIBUTED_TEST_LEAF_CAP),
            lambda leaf: _find_tests_referencing_fn(
                tests_dir_files, leaf, max_blocks=1),
            file_cap=_ATTRIBUTED_TEST_FILE_CAP,
            into=test_blocks)
        _pins = pinned_observables_from_tests(
            attributed_test_leaves(_refill_evs, cap=_ATTRIBUTED_TEST_LEAF_CAP),
            (t for _, t in test_blocks))
        # The block's own escalated bodies, as the frontier recorded them
        # (this route is the file-first fallback; a member the file route
        # landed is no longer a refill here).
        _esc_names = escalated_refill_names(_refill_evs)
        _block_wraps_here = any(
            wraps_crate_tuple_struct(m.body_text, _tuple_structs)
            for m in block.refill_members if m.fn_name in _esc_names)
        _refill_evidence_text = render_refill_evidence(
            _refill_evs, pins=_pins, wraps=_block_wraps_here)
        # Reuse the per-fn test finder by unioning tests touching any
        # stub/refill target.
        for fn_name in _fill_target_names[:4]:
            for path_str, txt in _find_tests_referencing_fn(
                    tests_dir_files, fn_name, max_blocks=2):
                if not any(p == path_str for p, _ in test_blocks):
                    test_blocks.append((path_str, txt))
        # Types in scope: union over all stub signatures in this block,
        # plus the impl header and its where clause — the self-type
        # (e.g. `impl Display for BoxedError`) appears in no member sig,
        # so without the container its definition is never injected.
        _block_container = block.container
        _block_where = _extract_impl_where_clause(snapshot, _block_container)
        _member_sigs_text = " ".join(
            (m.sig_text) for m in block.members)
        # What this block ASKED for. A reply that names its container instead
        # of a member identifies no fn, and with one target there is only one
        # fn it can be (`bind_headerless_names`).
        _block_target_names = [m.fn_name for m in
                               (*block.stub_members, *block.refill_members)]
        # Crate idioms — surfaces THIS file's repeating patterns (Self::
        # constructor family, vtable dispatch, wrapper construction,
        # unsafe chains) from already-filled fn bodies. The target
        # sig_text drives the relevance ranking so the model sees the
        # patterns most likely to apply to the fns it's about to write.
        _target_sig_concat = " ".join(
            (m.sig_text) for m in (*block.stub_members, *block.refill_members))
        crate_idioms_section = _extract_crate_idioms(
            blocks, block.file,
            target_sig_text=_target_sig_concat,
            max_idioms=5,
        )
        # sibling reference: union over stub/refill member names so e.g.
        # a stub `fmt` sees an already-filled `fmt` from another impl.
        _sib_parts: list[str] = []
        for fn_name in _fill_target_names[:4]:
            _s = _collect_sibling_filled_bodies(
                snapshot, fn_name, block.container)
            if _s:
                _sib_parts.append(_s)
        siblings_section = "\n\n".join(_sib_parts)
        # This block's blueprint slice — ranked by overlap with the
        # stub/refill names, the container tokens, and the file stem.
        _blueprint_slice = "" if _blueprint.empty else _blueprint.slice_for(
            frozenset([*_fill_target_names, *_container_names(block.container),
                       Path(block.file).stem]),
            cap=int(os.getenv("BLUEPRINT_SLICE_CAP", "2500")))
        # Judged against THIS block's file text — the retro store is shared by
        # clusters with different private layouts, so guidance naming a field
        # this layout lacks is withheld rather than shown (it would steer every
        # attempt into the same E0609/E0560 on the field that does not exist).
        # Honoured records are LOGGED: the store steers prompts silently, and a
        # poisoned record's only other trace is its downstream test failures.
        _prior_key = _retro_key(block.file, "*impl*", block.container)
        _prior_rec = retros_cache.get(_prior_key, {})
        _prior_verdict = screen_guidance(
            _prior_rec, snapshot, evidence=_block_answers,
            anchor_types=anchor_types_for_container(block.container),
            cluster=cluster_name, cfg_dead=_cfg_dead_fns(block.file))
        prior_retro = _prior_verdict.text
        # The line carries the STORE key so the grep lands on the record.
        if (_hline := honoured_guidance_line(
                _prior_key, _prior_rec, prior_retro)):
            _log(f"  {_hline}")
        if (_rline := retired_guidance_line(
                _prior_key, _prior_rec, _prior_verdict)):
            _log(f"  {_rline}")
        notes_section = ""
        # Computed OUTSIDE the notes branch: the escalated set also decides
        # the completion policy of the ask below (`followup_think`), which
        # must not depend on whether an FR file exists.
        _esc_targets = escalated_refill_names(_refill_evs)
        if fr_path is not None:
            # The notes filter applies on the per-block route too — this is
            # the FALLBACK the file-first takes after a compile-failed attempt
            # (run `40938-f4e8e94b` iter 2: three reverts, then per-block
            # fills whose prompts re-injected the `[E0425]` lesson beside the
            # same ESCALATED rewrite instruction). A route-scoped rule would
            # survive on exactly that path.
            notes_section = _filter_model_notes_for_prompt(
                fr_path, file_rel=block.file,
                fn_name=_fill_target_names[0] if _fill_target_names else "",
                file_text=snapshot, current_error="",
                escalated_fns=frozenset(
                    k.fn_name for k in escalated_keys if k.file == block.file),
                escalated_grade="ESCALATED REWRITE" if _esc_targets else "",
                writable_fns=frozenset(_block_target_names),
            )
        last_error_excerpt = ""
        # The last failed attempt's REAL two-tier metric. Re-deriving it from
        # `last_error_excerpt` scores a compiled-but-red block as 0 errors —
        # test output carries no rustc E-codes — and 0 never beats a prior 0,
        # so a block was parked the round after it started compiling.
        last_metric: FixProgress | None = None
        # Best COMPILING file image this block produced. `unimplemented!()` is
        # not a neutral fallback — it panics and aborts the test binary, so
        # every sibling test in it goes unjudged. On exhaustion we restore this
        # rather than leaving the stubs.
        salvage = CompilingSalvage()
        last_response = ""
        members_filled_this_block = 0
        block_already_complete = False  # all-target_not_stub ⇒ done
        # Why the most recent attempt landed nothing. Gates the budget-off
        # last-resort below: a routing drop or a splice refusal means the model
        # already answered, so a bigger prompt buys nothing. A list
        # so the nested attempt closures write without `nonlocal` chains.
        _last_loss: list[AttemptLoss] = []
        # Non-empty iff the latest revert was a crash-only regression blamed
        # wholly outside this block — holds the crash-block mark to persist.
        _crash_blocked_mark: list[str] = []
        # Failing signature per judged failed attempt (compile `code:message`
        # set, U2 failing-test-name set, or regression signature), in attempt
        # order. Read for two structural checks: two consecutive identical
        # entries withhold the budget-off escalation, and a signature three
        # blocks share this pass stops the group (`_shared_failing`).
        _sig_history: list[frozenset[str]] = []
        # B3: baseline = the crate's error signature with THIS block's
        # stubs still unimplemented. A fill is "non-regressing" if it adds no
        # error beyond this baseline and none on the filled fns — then we keep
        # it even when the crate doesn't fully compile, so prerequisites of an
        # interdependent web accumulate instead of being reverted forever.
        # Gated by PHASE_C_KEEP_NONREGRESSING (instant rollback); captured once
        # per block (the snapshot is pristine here).
        _b3_keep = os.getenv("PHASE_C_KEEP_NONREGRESSING", "0").strip() not in ("", "0", "false")
        _baseline_sig: frozenset[str] = frozenset()
        if _b3_keep:
            with _splice_check_lock:
                _brc, _bout, _bdiag = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
            _baseline_sig = _cargo_err_sig_from_diagnostics(_bdiag)
        _t0 = time.monotonic()
        _emit("phase_c.impl.fill.start", data={
            "cluster": cluster_name,
            "file": block.file,
            "container": block.container,
            "stub_count": len(block.stub_members),
            "refill_count": len(block.refill_members),
        })

        def _attempt_impl_fill(fix_attempt: int) -> FixOutcome:
            nonlocal attempts_total, last_response
            with _counters_lock:
                attempts_total += 1
            # Default this attempt's loss to the model's. Only the two
            # positively-identified pipeline-side losses below overwrite it, so
            # the escalation stays available exactly where it was before.
            _last_loss[:] = [AttemptLoss.MODEL_FAILURE]
            # Slice 8a: TYPES_IN_SCOPE re-selected each attempt from the
            # member sigs + impl header + where clause + the current error
            # (E0277/E0599/E0046/E0053 routinely name a trait
            # the impl block's signatures don't mention).
            _types_section_eff = _types_section_for_fix(
                _member_sigs_text, "", last_error_excerpt or "",
                _block_container, _block_where, _crate_type_records)
            # Attempt 2+: does the sibling reference carry the expression the
            # previous attempt was rejected on? Then the two sections
            # contradict each other and the prefix must say so — run
            # `902902-daee577d` (2026-08-20), prompt `11899c17478d`:
            # `Some(&self.error)` quoted in LAST_ERROR's E0277 AND in the
            # sibling introduced with "follow the same structural pattern";
            # the model followed the sibling and hit the identical error.
            _steer = sibling_steer(siblings_section, last_error_excerpt or "")
            if _steer.contradicts:
                _shared_txt = ", ".join(f"`{e}`" for e in _steer.shared)
                _log(f"  [phase-c/impl] {block_id}: SIBLING_REFERENCE contradicts "
                     f"LAST_ERROR — the sibling body carries {_shared_txt}, the "
                     "expression the previous attempt was rejected on; its "
                     "prefix now reads 'do NOT follow' instead of 'follow'.")
                _emit("phase_c.impl.sibling_contradicts_last_error", data={
                    "block_id": block_id, "file": block.file,
                    "container": block.container,
                    "shared": list(_steer.shared),
                    "error_codes": sorted(ecodes_in_text(last_error_excerpt)),
                })
            prompt = _build_per_impl_prompt(
                block=block,
                related_blocks=related_blocks,
                test_blocks=test_blocks,
                module_name=module_name,
                cluster_name=cluster_name,
                sections=PerImplSections(
                    types=_types_section_eff, invariants=invariants_section,
                    notes=notes_section, retrospective_guidance=prior_retro,
                    crate_idioms=crate_idioms_section, macros=macros_section,
                    dispatch_family=dispatch_family_section, siblings=siblings_section,
                    blueprint=_blueprint_slice,
                    crash_evidence=_block_crash_evidence,
                    # The previous attempt's own errors, as a section the model
                    # reads — not just as TYPES_IN_SCOPE re-selection input.
                    # Without it, an error naming no new types rebuilt attempt
                    # 1's prompt byte-identically and attempt 2 was a
                    # temperature resample (both E0277 units of run
                    # `275772-425da59c`, 2026-08-19: same prompt sha on both
                    # attempts, identical error, whole fix loop spent).
                    last_error=last_error_excerpt or "",
                    bound_methods=_bound_methods_section_for(
                        _block_where, _crate_type_records),
                    refill_evidence=_refill_evidence_text,
                    inferred_refill_names=_inferred_refill,
                    sibling_rejected=_steer.shared),
                fr_path=fr_path,
            )
            try:
                response = _ask_fill(
                    prompt, followup_think=escalated_followup_think(_esc_targets))
            except Exception as e:
                _log(f"  [phase-c/impl] {block_id}: LLM error: {e}")
                return FixOutcome("stop")
            last_response = response
            parsed = bind_headerless_names(
                extract_fn_bodies(response), _block_target_names)
            _note_reader_loss(_log, _emit, cluster=cluster_name,
                              unit=str(block_id), route="impl",
                              reply=response, parsed=parsed)
            if not parsed:
                _log(f"  [phase-c/impl] {block_id}: empty/no FN blocks "
                     f"(attempt {fix_attempt}; "
                     f"{fn_tag_occurrences(response)} FN tag(s) in reply).")
                return FixOutcome("stop")
            return _splice_and_verify(parsed, fix_attempt)

        def _splice_and_verify(parsed: list[dict[str, Any]],
                               fix_attempt: int) -> FixOutcome:
            """Route → splice → cargo-verify one parsed reply. Shared by the
            budgeted attempts and the maximal last-resort attempt."""
            nonlocal fns_filled_total, impls_filled, last_error_excerpt, \
                members_filled_this_block, block_already_complete, last_metric
            # Hold the global splice+cargo lock while we mutate and check.
            with _splice_check_lock:
                # Monotone ratchet baseline: the cluster's runtime test state
                # right NOW, before this attempt's own splice — captured
                # inside the lock, not before it. Several blocks/fns queue
                # for this same shared lock under PIPELINE_PARALLELISM>1; a
                # baseline captured before that queueing goes stale by the
                # time this attempt's own turn arrives (other queued workers'
                # accepted changes land first), misattributing THEIR
                # regressions to whichever attempt happens to check right
                # after. None ⇒ crate not test-runnable now → gate
                # self-disables for this attempt. Tests failing only because
                # THIS block is still an `unimplemented!()` stub are already
                # in the baseline, so filling them green is never mistaken
                # for a regression.
                _impl_base_raw: list[str] = []
                _baseline_test = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env, _impl_base_raw)
                _impl_base_sites = _panic_sites_of(_impl_base_raw)
                # Splice each parsed body into the impl indicated by its
                # `block` attribute. Match against TARGET container OR any
                # related container. Route by canonical impl-header identity
                # (exact first) — a multi-line where-clause container must
                # match the model's single-line `block` attribute.
                # `members` for the same reason the file-first route passes it:
                # the last-resort prompt this same splice serves shows the
                # model the ENTIRE file, so a reply can carry a body for a free
                # fn or for a sibling impl's member tagged with something that
                # is not an impl header at all.
                routing = route_fn_bodies(
                    parsed, target=block.container,
                    related=[rb.container for rb in _route_candidates],
                    trait_defaults=trait_default_hosts(blocks, block.file),
                    members=members_in_file(blocks, block.file))
                routed = list(routing.routed)
                if routing.dropped:
                    # The dropped VO carries the ACTUAL rejected block header —
                    # a name re-lookup would collapse two same-named bodies in
                    # different impls and print the wrong one.
                    target_canon = canon_container(block.container)
                    for _db in routing.dropped:
                        _log(f"  [phase-c/impl] {block_id}: dropped "
                             f"`{_db.fn_name}` — block header "
                             "does not match any TARGET or RELATED. "
                             f"raw_block={_db.block_attr!r} "
                             f"raw_canon={_db.attr_canon!r} "
                             f"target_canon={target_canon!r}")
                spliced_now = 0
                _impl_spliced: list[str] = []
                refusals: list[str] = []
                # Refill members are already-filled (not `unimplemented!()`) —
                # the splice must be told to overwrite a non-stub body for
                # exactly these fn names, same as the per-fn refill path's
                # `allow_nonstub=rec.refill` (fill_one_fn.py). Scoped to the
                # FILE, not `block`: routing widens to related/sibling
                # containers, so a body landing on a refill target owned by
                # another block in this file must still be authorized.
                _refill_names = refill_authorized_names(blocks, block.file)
                # Apply each splice. Operating on the same file in
                # sequence keeps byte offsets consistent because we
                # re-locate after each splice via the syn enumerator.
                for _rb in routed:
                    fn_data, container = _rb.fn_data, _rb.container
                    _via_default = _rb.via_trait_default
                    target_file = target_src_dir / block.file
                    # Trait-owned signature (impl member OR the trait's own
                    # default): a model-proposed where-clause would splice E0276
                    # corruption into the frozen sig. Drop it (align to fix-loop
                    # policy). A body routed via the trait-default tier was
                    # WRITTEN against an impl header, so its clause is even less
                    # likely to hold for every implementor.
                    wc = fn_data["where_clause"]
                    if wc and (_via_default or signature_fixed_by_trait(container)):
                        _log(f"  [phase-c/impl] {block_id}: dropped where-clause "
                             f"on trait-impl member `{fn_data['fn_name']}`")
                        wc = None
                    ok, reason, delta = _splice_fn_with_where(
                        target_file, container, fn_data["fn_name"],
                        fn_data["body"], wc,
                        allow_nonstub=fn_data["fn_name"] in _refill_names,
                        sig_text=_rb.sig_text,
                    )
                    if ok:
                        spliced_now += 1
                        _impl_spliced.append(fn_data["fn_name"])
                        if reason == "ok_xml_unescaped":
                            _log(f"  [phase-c/impl] {block_id}: "
                                 f"`{fn_data['fn_name']}` landed only after "
                                 "resolving XML entities in the body — the "
                                 "reply used the escaped rendering.")
                        if reason == "ok_own_brace_dropped":
                            _log(f"  [phase-c/impl] {block_id}: "
                                 f"`{fn_data['fn_name']}` landed only after "
                                 "dropping the fn's own closing brace — the "
                                 "reply closed the body itself.")
                    else:
                        refusals.append(reason)
                        _log(f"  [phase-c/impl] {block_id}: splice "
                             f"`{fn_data['fn_name']}` failed: {reason}")
                if spliced_now == 0:
                    # every routed body refused as target_not_stub
                    # ⇒ the members are already filled — the block is
                    # COMPLETE, not failed (don't burn retries or a retro).
                    if routed and all(r == "target_not_stub" for r in refusals):
                        block_already_complete = True
                        _log(f"  [phase-c/impl] {block_id}: all {len(routed)} "
                             "routed body(ies) already filled "
                             "(target_not_stub) — block complete.")
                        _emit("phase_c.impl.fill.already_complete", data={
                            "cluster": cluster_name, "file": block.file,
                            "container": block.container,
                            "members": len(routed)})
                        return FixOutcome("accepted")
                    # The model answered — bodies were parsed — and not one
                    # reached the file. Name which side lost it: a header that
                    # matched no container, or a write the splice refused.
                    # Either way this is not evidence the prompt was too small.
                    _last_loss[:] = [AttemptLoss.ROUTING_DROP if routing.dropped
                                     else AttemptLoss.SPLICE_REFUSAL]
                    _log(f"  [phase-c/impl] {block_id}: nothing spliced "
                         f"({_last_loss[0].value}, attempt {fix_attempt}).")
                    return FixOutcome("stop")
                # Run cargo check via the structured-diagnostics wrapper
                # so error counts come from rustc's own classification
                # (JSON `compiler-message`s with `level == "error"`) and
                # not from regex over rendered text. The regex path
                # produced false zeros for non-E-coded failures (linker,
                # build script, cargo wrapper) — the source of the
                # recurring "compile failed but errs=0" stall bug.
                # Run cargo check via the structured-diagnostics wrapper
                # so error counts come from rustc's own classification
                # (JSON `compiler-message`s with `level == "error"`) and
                # not from regex over rendered text.
                rc, cargo_out, diagnostics = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                if rc != 0:
                    # Deterministic repair: a body cannot add `use` items, so
                    # apply rustc's own single-candidate import suggestions to
                    # the block's file and re-check before charging the model.
                    _rep = repair_missing_imports(
                        _run_cargo_check_diag, check_cmd, target_src_dir,
                        cluster_env or {}, block.file, diagnostics, _log,
                        "[phase-c/impl]")
                    if _rep is not None:
                        rc, cargo_out, diagnostics = _rep
                _last_full_out[0] = cargo_out  # `full_error` pull source
                _u2_impl: U2Verdict | None = None
                if rc == 0:
                    # Shared gate: accept the block only if its
                    # members' own tests pass too — not just compile.
                    # Conviction-scoped (see the file-first gate above): only
                    # tests with direct evidence at a member may revert the block.
                    _block_tests = set(_block_evidence.tests)
                    # Container-qualified sites: this block's members share
                    # block.container, so a same-named method in ANOTHER impl
                    # (e.g. a different `From` variant's `from`, the free `not`
                    # vs this Bool impl's `not`) whose stub the test panics in
                    # is correctly external, not this block's own failure.
                    _block_sites = frozenset(
                        FnSite(block.file, _bm.fn_name, block.container)
                        for _bm in (*block.stub_members, *block.refill_members))
                    _u2_impl = _u2_test_gate(_block_tests, _block_sites,
                                             tdd_test_cmd, target_src_dir,
                                             cluster_env)
                # Only a REAL self-failure reverts. ACCEPT_EXTERNAL_ONLY (every
                # failing test blocked by another unit's stub) keeps the fill.
                _u2_impl_reject = (_u2_impl is not None
                                   and _u2_impl.decision is U2Decision.REJECT)
                _u2_external_only = (
                    _u2_impl is not None
                    and _u2_impl.decision is U2Decision.ACCEPT_EXTERNAL_ONLY)
                # Comparative banking (joint route): a whole-block rewrite that
                # strictly shrinks its own failing set is kept, not reverted —
                # vanished tests judged by prior state, previously-passing
                # losses still revert via the regression gate below.
                _bank_impl: JointImprovement | None = None
                if (_u2_impl_reject and _u2_impl is not None
                        and _baseline_test is not None):
                    _jb = assess_joint_improvement(
                        frozenset(_block_tests),
                        _baseline_test.passed_names,
                        _baseline_test.ignored_names,
                        _u2_impl, _impl_base_sites)
                    _bank_impl = _jb if _jb.banks else None
                    if _jb.site_movement:
                        _log(f"  [phase-c/impl] {block_id}: panic sites moved "
                             f"— {_jb.site_movement}")
                # Global monotone ratchet: a compiled fill is rejected if it
                # reddened ANY previously-passing cluster test — even when the
                # block's own U2 tests pass (or it would be B3-kept). This is
                # the guard the pass-count oscillation was slipping through:
                # both accept paths below verified compile/own-tests only. When
                # rc==0 the crate compiles clean, so B3's compile-signature is
                # empty and one of the two paths WILL accept — hence the check
                # runs exactly once per green attempt (never wasted). Red
                # attempts skip it (tests can't run; deferred to the TDD loop).
                _regr = (_test_regression_gate(
                    tdd_test_cmd, target_src_dir, cluster_env, _baseline_test,
                    stub_free=stub_free)
                    if rc == 0 else None)
                if _regr is not None:
                    _cmp, _regr_detail = _regr
                    file_path.write_text(snapshot, encoding="utf-8")
                    _note_reverted(block.file, _impl_spliced)
                    _log(f"  [phase-c/impl] {block_id}: REVERTED {spliced_now} "
                         f"fn(s) — regressed sibling test(s): "
                         f"{_cmp.summary} (attempt {fix_attempt}).")
                    last_error_excerpt = _regr_detail
                    _rsig = _cmp.signature
                    last_metric = FixProgress(0, max(1, len(_rsig)))
                    # Crash-only regression (no newly-red verdict in a
                    # completed binary) with the localized blame wholly
                    # outside this block: the fill routes execution into a
                    # sibling's fault. The revert above STAYS — a memory
                    # fault does not exonerate the fill the way a stub panic
                    # does — but re-prompting, diagnosing and convicting this
                    # block charges it for the sibling's defect.
                    if (_cmp.newly_crashed and not _cmp.newly_red
                            and blame_outside_unit(
                                crash_blamed or frozenset(), block.file,
                                frozenset(m.fn_name for m in block.members))):
                        _crash_blocked_mark[:] = [crash_block_mark(
                            target_src_dir,
                            [b.file for b in (crash_blamed or frozenset())])]
                        _log(f"  [phase-c/impl] GUARD {block_id}: kept "
                             "reverted and STOPPING — the crash is a "
                             "sibling's localized fault; re-prompting this "
                             "block buys the same crash until the blamed "
                             "file changes.")
                        _verdict_row(
                            block_id, Disposition.REVERT,
                            "crash-only regression, blame wholly outside "
                            "the block — revert stands, unit crash-blocked",
                            outcome=GateOutcome.UNMEASURABLE,
                            attempt=fix_attempt,
                            validity=ValidityVector(
                                binary_completed=False,
                                blame_outside_unit=True),
                            evidence=tuple(sorted(_cmp.newly_crashed)))
                        return FixOutcome("stop")
                    _verdict_row(
                        block_id, Disposition.REVERT,
                        f"regressed sibling test(s): {_cmp.summary}",
                        outcome=GateOutcome.NEGATIVE,
                        attempt=fix_attempt,
                        validity=ValidityVector(
                            binary_completed=not _cmp.newly_crashed),
                        evidence=tuple(sorted(_rsig)))
                    # rc==0 got us here, so the block COMPILES — its regression
                    # is a test-tier loss, never a compile-tier one.
                    return FixOutcome("failed", err_sig=_rsig,
                                      metric=FixProgress(
                                          0, max(1, len(_rsig))))
                # A REJECT on an attempt dispatched with crash evidence is
                # judged by a metric that cannot credit it: a correct fix of
                # a LATENT fault moves no test while the activating sibling
                # fill is crash-reverted beside it (2026-08-09/10: eight
                # crash-evidence re-fills of the block holding `object_drop`
                # all reverted for no strict improvement). Keep on
                # NON-REGRESSION — no self-failure that was passing in a
                # completed binary at baseline; the regression gate above
                # already held — and let the next sweep judge the joint tree.
                _crash_keep = (
                    rc == 0 and _u2_impl_reject and bool(_block_crash_evidence)
                    and _u2_impl is not None and _baseline_test is not None
                    and not (_u2_impl.self_failures
                             & _baseline_test.passed_names))
                if _crash_keep and _u2_impl is not None:
                    _log(f"  [phase-c/impl] GUARD {block_id}: crash-evidence "
                         "re-fill KEPT on non-regression "
                         f"({len(_u2_impl.self_failures)} self-failure(s), "
                         "none passing at baseline) — judged for real at the "
                         "next sweep, in the joint tree.")
                if rc == 0 and (not _u2_impl_reject or _bank_impl is not None
                                or _crash_keep):
                    if _u2_external_only and _u2_impl is not None:
                        _emit("phase_c.u2.external_only", data={
                            "cluster": cluster_name, "file": block.file,
                            "container": block.container,
                            "blocking_fns": sorted(
                                f"{s.file}::{s.fn_name}" for s in _u2_impl.blocking_fns),
                            "unjudged": sorted(_u2_impl.no_verdict)})
                    members_filled_this_block = spliced_now
                    fns_filled_total += spliced_now
                    impls_filled += 1
                    block_outcomes.append(UnitOutcome(
                        landed=True,
                        blocked_by=(tuple(sorted(_u2_impl.blocking_fns))
                                    if _u2_external_only and _u2_impl is not None
                                    else ())))
                    _log(f"  [phase-c/impl] {block_id}: "
                         f"FILLED {spliced_now} fn(s) "
                         f"(attempt {fix_attempt})"
                         + (f" — banked improvement: {_bank_impl.summary}."
                            if _bank_impl is not None else "."))
                    _passed_i = (frozenset(_bank_impl.resolved)
                                 if _bank_impl is not None
                                 else (_u2_impl.self_passed
                                       if _u2_impl is not None else frozenset()))
                    _kept_i = _u2_external_only or _crash_keep
                    _verdict_row(
                        block_id,
                        (Disposition.KEEP_DEFERRED if _kept_i
                         else Disposition.ACCEPT),
                        ("kept deferred — every failing test blocked or "
                         "crash-evidenced" if _kept_i
                         else f"accepted — {len(_passed_i)} attributed test(s) "
                         "observed passing" if _passed_i
                         else "kept — compiles, but no attributed test was "
                         "observed passing (none were failing, or the scoped "
                         "run was inconclusive)"),
                        outcome=(GateOutcome.POSITIVE
                                 if _passed_i and not _kept_i
                                 else GateOutcome.UNMEASURABLE),
                        evidence=tuple(sorted(_passed_i)),
                        attempt=fix_attempt,
                        validity=ValidityVector(binary_completed=True),
                        model_calls=fix_attempt)
                    _emit("phase_c.impl.fill.done", duration_ms=int(
                        (time.monotonic() - _t0) * 1000),
                        data={
                            "cluster": cluster_name,
                            "file": block.file,
                            "container": block.container,
                            "members_filled": spliced_now,
                            "members_failed": 0,
                            "fix_attempts": fix_attempt,
                        })
                    return FixOutcome("accepted")
                # B3: keep a NON-REGRESSING fill even when the crate
                # doesn't fully compile — it adds no error beyond baseline and
                # none on the filled fns, so it's a correct prerequisite of the
                # interdependent web, not a failure to discard. This dissolves
                # the convergence deadlock where every correct piece is reverted
                # for not single-handedly making rc==0.
                # B3 keeps ONLY while the crate is RED (rc!=0),
                # its actual purpose — accumulating prerequisites of a web that
                # can't yet compile. When rc==0 the crate compiles and tests CAN
                # run, so reaching here means U2 FAILED (own tests red): keeping a
                # green-but-behaviorally-wrong body just masks it as "filled".
                # Fall through to revert (→ stub) so the per-fn behavioral re-fill
                # retries it cleanly with the failing test.
                if _b3_keep and rc != 0:
                    _cur_sig = _cargo_err_sig_from_diagnostics(diagnostics)
                    _new_errs = _cur_sig - _baseline_sig
                    _frontier = _frontier_from_diags(diagnostics, target_src_dir)
                    _filled = {rb.fn_data["fn_name"] for rb in routed}
                    _on_fill = any(f == block.file and fn in _filled
                                   for (f, fn) in _frontier)
                    if not _new_errs and not _on_fill:
                        members_filled_this_block = spliced_now
                        fns_filled_total += spliced_now
                        impls_filled += 1
                        # B3 keeps land while the crate is RED, so no test ran
                        # and no site can be blamed. Recorded as landed with no
                        # blocker, which makes the payoff verdict conservative:
                        # one such block is enough to withhold "payoff zero".
                        block_outcomes.append(UnitOutcome(landed=True))
                        _log(f"  [phase-c/impl] {block_id}: KEPT {spliced_now} "
                             f"fn(s) (B3 non-regressing; {len(_cur_sig)} crate "
                             f"err(s) remain elsewhere) (attempt {fix_attempt}).")
                        _emit("phase_c.impl.fill.done", duration_ms=int(
                            (time.monotonic() - _t0) * 1000),
                            data={
                                "cluster": cluster_name,
                                "file": block.file,
                                "container": block.container,
                                "members_filled": spliced_now,
                                "members_failed": 0,
                                "fix_attempts": fix_attempt,
                                "b3_kept": True,
                                "crate_errs_remaining": len(_cur_sig),
                            })
                        return FixOutcome("accepted")
                if _u2_impl_reject and _u2_impl is not None:
                    cargo_out = _u2_impl.detail   # route test-failure detail as the error
                # Compile failed (or U2 self-test failure). Structured diagnostics
                # give the error signature; a U2 test failure uses the
                # failing-test set (test output has no rustc E-codes).
                # rc != 0 establishes a minimum count of 1.
                cur_err_sig = (_u2_impl.self_failures
                               if _u2_impl_reject and _u2_impl is not None
                               else _cargo_err_sig_from_diagnostics(diagnostics))
                # Two tiers, never one count: a U2 self-failure means the block
                # COMPILED, so its compile tier is 0 and it strictly beats every
                # red attempt regardless of how many tests are still failing.
                cur_metric = (
                    FixProgress(0, max(1, len(cur_err_sig))) if _u2_impl_reject
                    else FixProgress(max(1, len(cur_err_sig)), 0))
                last_metric = cur_metric
                # Whole rustc diagnostics (count-capped) when the JSON
                # path yielded them; the char-budgeted text focus is fallback.
                last_error_excerpt = (
                    focus_diagnostics(diagnostics, block.file)
                    or _focus_cargo_errors(cargo_out, block.file))
                _record_error_codes_seen(cargo_out)
                if deps.retire_contradicted_notes is not None and fr_path:
                    deps.retire_contradicted_notes(fr_path, cargo_out)
                # The fill regressed (a new error, or an error on the filled
                # fns) — B3 already accepted non-regressing fills
                # above. Revert; the driver decides whether to retry.
                # A compiling attempt is banked first: the regression gate above
                # already cleared this file image against the pre-splice
                # baseline, so it is a strictly better end state than the stubs
                # if the loop later exhausts (see `_salvage` restore below).
                if cur_metric.compiles:
                    salvage.offer(
                        file_path.read_text(encoding="utf-8", errors="replace"),
                        cur_metric)
                file_path.write_text(snapshot, encoding="utf-8")
                _note_reverted(block.file, _impl_spliced)
                return FixOutcome("failed", err_sig=cur_err_sig,
                                   metric=cur_metric)

        def _on_impl_failure(attempt_n: int, outcome: FixOutcome,
                             gauge: FloorStall) -> None:
            _codes = sorted(ecodes_in_text(last_error_excerpt))
            # TEST-tier signatures only (failing-test sets; no rustc E-codes
            # in the excerpt). A compile-tier repeat stays OUT of the
            # invariance history on purpose: "the model was shown too
            # little" is real at the compile tier — a type or sibling
            # defined outside the budgeted window — and the wider window is
            # exactly the fix, so the escalation must stay available there.
            # A failing-TEST set that survives attempts unchanged is the
            # tier where the wider window re-purchases the same answer.
            if outcome.err_sig and not _codes:
                _sig_history.append(frozenset(outcome.err_sig))
            _kind = "U2 test" if not _codes and last_error_excerpt else "compile"
            _log(f"  [phase-c/impl] {block_id}: {_kind} failed "
                 f"(attempt {attempt_n}); errs={outcome.metric} "
                 f"codes={_codes or '—'} "
                 f"floor={gauge.best} stall={gauge.stalls}.")
            # The codes alone are not diagnosable. src/error.rs::Error logged
            # `codes=['E0277','E0308','E0425','E0603']` on every attempt of
            # runs 2370111-1800b8b9 and 2452699-e19bc335 (2026-08-17) while the
            # block failed to compile for over an hour in each, and neither run
            # reached a second TDD sweep — with no message in the log, nothing
            # said WHICH name was unresolved or WHICH item was private, so the
            # cause could not be read back afterwards. The excerpt is already in
            # hand here; one headline per distinct code is what makes the next
            # occurrence a lookup instead of a re-run. Headline only (the code's
            # `error[...]:` line), so a wide error set cannot flood the log.
            for _c in _codes:
                _exc = excerpt_for_code(last_error_excerpt, _c)
                if _exc:
                    _log(f"  [phase-c/impl] {block_id}:   {_exc.splitlines()[0]}")

        def _on_impl_stall(attempt_n: int, gauge: FloorStall) -> None:
            _codes = sorted(ecodes_in_text(last_error_excerpt))
            _log(f"  [phase-c/impl] {block_id}: floor stalled "
                 f"at {gauge.best} for {gauge.stalls} iter(s) "
                 f"codes={_codes or '—'} — stopping this block.")

        def _on_impl_repeated(attempt_n: int, gauge: FloorStall) -> None:
            _codes = sorted(ecodes_in_text(last_error_excerpt))
            _log(f"  [phase-c/impl] {block_id}: identical errors "
                 f"after attempt {attempt_n} codes={_codes or '—'} — stopping.")

        # Retro-informed retry loop (parity with the per-fn path): a failed
        # fix loop gets a retrospective and, while each round STRICTLY
        # improves the block's best error count, re-enters the fix loop with
        # that guidance in the prompt — instead of parking the block until a
        # later iteration. Strictly decreasing error counts terminate on
        # their own; the round cap is a runaway backstop.
        _retro_rounds_max = int(os.getenv("PER_FN_RETRO_ROUNDS_MAX", "25"))
        # A block that has never once compiled has nothing the loop can keep:
        # this route splices its members as a SET and reverts the whole file on
        # any error, so a strictly-improving error floor (4 -> 3 -> 2) buys
        # zero landed bodies while reading to every gate as progress. Runs
        # 2370111-1800b8b9, 2452699-e19bc335 and 2509252-f1f8c11b (2026-08-17)
        # each spent their whole lives inside iteration 1 on
        # src/error.rs::Error, and the third left the file with the same 39
        # `unimplemented!()` it started with after 89 minutes — the floor moved
        # 4 -> 3 and not one body landed. The stubs this loop gives up on fall
        # through to the per-fn route in the same iteration (see
        # `_impl_stats.remaining` in `phase_c_engine`), which gates each body
        # on its own `cargo check` and so lands the majority that compile
        # instead of reverting them with the few that do not.
        _noncompiling_rounds_max = int(
            os.getenv("PHASE_C_IMPL_NONCOMPILING_ROUNDS_MAX", "2"))
        _retro_round = 0
        fix_attempt = 0
        while True:
            _retro_round += 1
            _, fix_attempt = drive_fix_loop(
                _attempt_impl_fill,
                hard_cap=_PER_FN_FIX_HARD_CAP,
                max_stalls=block_max_stalls,
                stop_context=f"per-impl fix for {block_id}",
                on_failure=_on_impl_failure,
                on_stall=_on_impl_stall,
                on_repeated=_on_impl_repeated,
                check_stop=_check_graceful_stop,
            )
            if members_filled_this_block > 0 or block_already_complete:
                break
            if _crash_blocked_mark:
                # No last-resort escalation, no retrospective, no conviction:
                # the failure was never about these bodies, and a bigger
                # prompt buys the identical sibling crash.
                break
            # Cross-block structural breaker: a failing signature that a
            # third block has now produced in this same pass implicates the
            # shared tree, not any of them. Stop this block's episode here —
            # before the escalation and before the retrospective — so the
            # group's members are not each convicted for one shared blocker.
            # The failing tests stay red, so the next sweep's attribution
            # gets the set with no conviction standing in its way.
            _cur_sig = _sig_history[-1] if _sig_history else frozenset()
            if _cur_sig:
                with _counters_lock:
                    _shared_failing.setdefault(_cur_sig, set()).add(block_id)
                    _is_structural = (_cur_sig in _structural_sigs
                                      or len(_shared_failing[_cur_sig]) >= 3)
                    if _is_structural:
                        _structural_sigs.add(_cur_sig)
                        _group = frozenset(_shared_failing[_cur_sig])
                if _is_structural:
                    _log(f"  [phase-c/impl] GUARD {block_id}: "
                         + structural_group_note(_cur_sig, _group))
                    _emit("phase_c.impl.structural_group", data={
                        "cluster": cluster_name, "file": block.file,
                        "container": block.container,
                        "signature": sorted(_cur_sig),
                        "group": sorted(_group)})
                    _verdict_row(
                        block_id, Disposition.KEEP_DEFERRED,
                        "structural: failing signature shared by "
                        f"{len(_group)} block(s) this pass",
                        outcome=GateOutcome.UNMEASURABLE,
                        attempt=fix_attempt,
                        evidence=tuple(sorted(_cur_sig)),
                        model_calls=fix_attempt)
                    break
            # Maximal last-resort — ONE attempt with budgets OFF (round 1
            # only; later rounds change guidance, not budgets): the WHOLE
            # current file, every referencing test verbatim, and the full
            # focused error. A defect outside the budgeted window (sibling
            # member, type elsewhere in the file) is invisible to the normal
            # prompt; cost is one LLM call on an already-failed block.
            _loss = _last_loss[0] if _last_loss else None
            _maximal_on = (_retro_round == 1
                           and os.getenv("PHASE_C_MAXIMAL_LAST_RESORT", "1") != "0")
            _sig_unchanged = attempt_invariant(_sig_history)
            if _maximal_on and not may_escalate_to_last_resort(
                    _loss, signature_unchanged=_sig_unchanged):
                _note = (invariance_refusal_note(fix_attempt)
                         if _sig_unchanged
                         else escalation_refusal_note(
                             _loss if _loss is not None
                             else AttemptLoss.MODEL_FAILURE))
                _log(f"  [phase-c/impl] {block_id}: maximal last-resort "
                     f"WITHHELD — {_note}.")
                _emit("phase_c.impl.fill.maximal_withheld", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "loss": _loss.value if _loss is not None else "",
                    "signature_unchanged": _sig_unchanged})
                _verdict_row(
                    block_id, Disposition.CAP_REFUSAL,
                    f"maximal last-resort withheld — {_note}",
                    attempt=fix_attempt, model_calls=fix_attempt)
            elif _maximal_on:
                _max_cap = int(os.getenv("PHASE_C_MAXIMAL_CHAR_CAP", "120000"))
                _cur_text = file_path.read_text(encoding="utf-8", errors="replace")
                _max_tests: list[tuple[str, str]] = []
                _max_target_names = _block_target_names
                for _fn in _max_target_names:
                    for _pth, _txt in _find_tests_referencing_fn(
                            tests_dir_files, _fn, max_blocks=4):
                        if not any(p == _pth for p, _ in _max_tests):
                            _max_tests.append((_pth, _txt))
                _tests_text = "\n\n".join(
                    f"// {p}\n{t}" for p, t in _max_tests)
                _max_prompt = build_maximal_impl_prompt(
                    file_rel=block.file, container=block.container,
                    stub_names=_max_target_names,
                    file_text=_cur_text, tests_block=_tests_text,
                    errs=last_error_excerpt, notes=notes_section)
                if len(_max_prompt) > _max_cap:
                    _log(f"  [phase-c/impl] {block_id}: maximal last-resort "
                         f"skipped ({len(_max_prompt)} chars > cap {_max_cap}).")
                else:
                    _log(f"  [phase-c/impl] {block_id}: maximal last-resort "
                         f"attempt (budgets off, {len(_max_prompt)} chars).")
                    _emit("phase_c.impl.fill.maximal", data={
                        "cluster": cluster_name, "file": block.file,
                        "container": block.container,
                        "prompt_chars": len(_max_prompt)})
                    try:
                        _max_reply = _ask_fill(_max_prompt)
                        _max_parsed = bind_headerless_names(
                            extract_fn_bodies(_max_reply), _max_target_names)
                        _note_reader_loss(
                            _log, _emit, cluster=cluster_name,
                            unit=str(block_id), route="impl-maximal",
                            reply=_max_reply, parsed=_max_parsed)
                        if _max_parsed:
                            _splice_and_verify(_max_parsed, fix_attempt + 1)
                        else:
                            _log(f"  [phase-c/impl] {block_id}: maximal attempt "
                                 "returned no FN blocks "
                                 f"({fn_tag_occurrences(_max_reply)} FN tag(s) "
                                 "in reply).")
                    except Exception as _me:
                        _log(f"  [phase-c/impl] {block_id}: maximal attempt "
                             f"error: {_me}")
                if members_filled_this_block > 0 or block_already_complete:
                    break
            # Per-impl retrospective — runs on EVERY failed round so the
            # improvement gate below always has a fresh measurement. Uses the
            # existing per-fn retro helper with the impl id as the fn key so
            # the on-disk store layout stays uniform. Transient LLM failures
            # (llm_error/empty_response) update no state and never retry.
            _retry_informed = False
            try:
                # The attempt's own metric, not a re-parse of its text.
                cur_progress = last_metric or FixProgress(
                    len(_cargo_err_sig(last_error_excerpt)), 0)
                prior = retros_cache.get(
                    _retro_key(block.file, "*impl*", block.container), {})
                prior_best = best_progress_of(prior)
                retro = _phase_c_retrospective(
                    fn_name="*impl*",
                    sig_text=block.container,
                    container=block.container,
                    file_rel=block.file,
                    cluster_name=cluster_name,
                    last_body=last_response[:_FAILED_BODY_CAP],
                    error_excerpt=last_error_excerpt,
                    fix_attempts=fix_attempt,
                    prior_retrospective=admissible_guidance(
                        prior, snapshot, evidence=_block_answers,
                        anchor_types=anchor_types_for_container(block.container),
                        cluster=cluster_name,
                        cfg_dead=_cfg_dead_fns(block.file)),
                    # The pins of the tests this block answers for: the
                    # diagnosis may not prescribe away an observable they
                    # name (`build_phase_c_retro_prompt`).
                    pinned_observables=_pins,
                )
                _signal = retro.get("signal", "")
                if _signal in ("llm_error", "empty_response"):
                    raise RuntimeError(f"transient retro signal: {_signal}")
                # Errors all in OTHER files → the block is blocked by a break
                # elsewhere; its error count reflects the blocker, not these
                # bodies. Record the diagnosis but never exhaust, never move
                # the baseline, and don't retry now (mirrors the per-fn rule).
                _downstream = _signal == "downstream_only"
                if _downstream:
                    new_best = prior_best
                    improved = False
                    full_exhaust = False
                elif prior_best is None:
                    new_best = cur_progress
                    improved = True
                    full_exhaust = retro.get("category") in (
                        "GENUINELY_HARD", "FROZEN_BOUND_MISMATCH")
                else:
                    new_best = min(prior_best, cur_progress)
                    improved = cur_progress < prior_best
                    full_exhaust = (
                        retro.get("category") in ("GENUINELY_HARD",
                                                  "FROZEN_BOUND_MISMATCH")
                        or not improved
                    )
                # A conviction is a verdict on the model's bodies. A last error
                # that is a splice/nesting diagnostic — a member's own
                # `fn source(&self)` line spliced inside a body, a
                # `parse_broken` — is a verdict on how the pipeline placed the
                # text and bought no evidence about the block. The record that
                # held `StdErrorforContextError<C,Error>` out of two dispatches
                # in run `1246060-2339497b` was stamped on exactly that.
                if full_exhaust and exhaustion_from_splice_diagnostic(
                        last_error_excerpt):
                    full_exhaust = False
                    _log(f"  [phase-c/impl/retro] {block_id}: NOT exhausting "
                         "— the last error is a splice/nesting diagnostic, "
                         "not a verdict on the block's bodies.")
                # The dict write + on-disk save must be atomic together —

                # two blocks finishing concurrently each write their OWN key
                # (safe unlocked, per-block-unique), but an unlocked
                # save-after-write race could have one thread's
                # `_save_retrospectives` persist a snapshot taken before the
                # other thread's key landed, silently losing that entry.
                # Stamped with the context it was earned in, the convicting
                # tests it was drawn from AND the hollow siblings the attempt
                # ran against: a later reader honours the verdict only where all
                # three still hold. The third is the one a block like
                # `impl Error` needs — nine members failing on `E0425: cannot
                # find value object_drop_rest` is a verdict about the tree the
                # attempt compiled in, not about the block.
                # `exclude` is empty on purpose: a block's key is `*impl*`, so
                # there is no single fn of its own to leave out, and its members
                # ARE among the stubs that release it.
                _blockers = BlockedOnStubs.at_conviction(
                    last_response[:_FAILED_BODY_CAP], snapshot)
                _prov = RetroProvenance.current(cluster_name, snapshot)
                with _counters_lock:
                    retros_cache[
                        _retro_key(block.file, "*impl*", block.container)
                    ] = stamp_conviction({
                        "category": retro.get("category", ""),
                        "guidance": retro.get("guidance", ""),
                        "signal": _signal,
                        "retrospective_count": int(prior.get("retrospective_count", 0)) + 1,
                        "error_count": cur_progress.compile_errors,
                        "failing_test_count": cur_progress.failing_tests,
                        **(best_progress_fields(new_best)
                           if new_best is not None else {}),
                        "fully_exhausted": full_exhaust,
                        "last_error_preview": last_error_excerpt[:240],
                    }, _prov,
                        # A compiled attempt learned from test outcomes, and
                        # is answerable to every test the block answers for;
                        # a compile failure is the compiler's verdict and is
                        # stamped with the convicting tests of the moment.
                        (_block_answers if cur_progress.compile_errors == 0
                         else _block_evidence),
                        _blockers,
                        # From the FULL excerpt: the 240-char preview above
                        # cuts a regression's test list mid-name.
                        regressed=regressed_tests_from_excerpt(
                            last_error_excerpt))
                    _save_retrospectives(project_dir, retros_cache)
                _verdict_row(
                    block_id,
                    (Disposition.CONVICTION if full_exhaust
                     else Disposition.REJECT),
                    f"retro round {_retro_round}: "
                    f"category={retro.get('category', '')!r} "
                    f"signal={_signal!r} improved={improved}",
                    outcome=(GateOutcome.UNMEASURABLE if _downstream
                             else GateOutcome.NEGATIVE),
                    attempt=fix_attempt,
                    evidence=tuple(sorted(_sig_history[-1]))
                    if _sig_history else (),
                    model_calls=fix_attempt)
                _retry_informed = not full_exhaust and not _downstream
            except Exception as _re:
                _log(f"  [phase-c/impl/retro] {block_id}: {_re}")
            if not (_retry_informed and _retro_round < _retro_rounds_max):
                break
            if (salvage.best is None
                    and _retro_round >= _noncompiling_rounds_max):
                _log(f"  [phase-c/impl] {block_id}: retro round {_retro_round} "
                     "improved the error floor but the block has never once "
                     "compiled, so nothing can be kept from it — handing its "
                     "stub(s) to the per-fn route, which gates each body "
                     "separately.")
                _emit("phase_c.impl.noncompiling_handoff", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "retro_round": _retro_round,
                    "attempts": fix_attempt,
                })
                break
            # Fresh guidance for the next round's prompt (the closure reads
            # this enclosing binding).  Logged like the pass-start read: this
            # is the record that steers the retry the line below announces.
            _prior_key = _retro_key(block.file, "*impl*", block.container)
            _prior_rec = retros_cache.get(_prior_key, {})
            _prior_verdict = screen_guidance(
                _prior_rec, snapshot, evidence=_block_answers,
                anchor_types=anchor_types_for_container(block.container),
                cluster=cluster_name, cfg_dead=_cfg_dead_fns(block.file))
            prior_retro = _prior_verdict.text
            if (_hline := honoured_guidance_line(
                    _prior_key, _prior_rec, prior_retro)):
                _log(f"  {_hline}")
            if (_rline := retired_guidance_line(
                    _prior_key, _prior_rec, _prior_verdict)):
                _log(f"  {_rline}")
            _log(f"  [phase-c/impl] {block_id}: retro round {_retro_round} "
                 "kept the error floor improving — re-entering the fix loop "
                 "with the new guidance.")
            _emit("phase_c.impl.retro_retry", data={
                "cluster": cluster_name, "file": block.file,
                "container": block.container, "retro_round": _retro_round,
            })

        if (members_filled_this_block == 0 and not block_already_complete
                and _crash_blocked_mark):
            # Plain reverted exit: no salvage (the best compiling image IS
            # the crash-activating one), no failure charge, no conviction.
            # Attempts stay in the returned telemetry — real model calls.
            if crash_blocked is not None:
                crash_blocked[
                    _retro_key(block.file, "*impl*", block.container)
                ] = _crash_blocked_mark[0]
            _log(f"  [phase-c/impl] GUARD {block_id}: crash-blocked "
                 f"({_crash_blocked_mark[0]}) — no attempt charged, no "
                 "retrospective written; re-admitted when the blamed file "
                 "changes.")
            _emit("phase_c.impl.crash_blocked", data={
                "cluster": cluster_name, "file": block.file,
                "container": block.container,
                "mark": _crash_blocked_mark[0],
            })
            _verdict_row(
                block_id, Disposition.KEEP_DEFERRED,
                "crash-blocked on a sibling's fault — no charge, "
                "re-admitted when the blamed file changes",
                outcome=GateOutcome.UNMEASURABLE, attempt=fix_attempt,
                validity=ValidityVector(binary_completed=False,
                                        blame_outside_unit=True),
                evidence=(_crash_blocked_mark[0],),
                model_calls=fix_attempt)
            return
        if members_filled_this_block == 0 and not block_already_complete:
            _salvaged = salvage.best
            if _salvaged is not None:
                with _splice_check_lock:
                    _sv_base = _test_regression_baseline(
                        tdd_test_cmd, target_src_dir, cluster_env)
                    _stubbed = file_path.read_text(encoding="utf-8",
                                                   errors="replace")
                    file_path.write_text(_salvaged.body, encoding="utf-8")
                    _sv_rc, _, _ = _run_cargo_check_diag(
                        check_cmd, target_src_dir, cluster_env)
                    _sv_regr = (_test_regression_gate(
                        tdd_test_cmd, target_src_dir, cluster_env, _sv_base,
                        stub_free=stub_free)
                        if _sv_rc == 0 else None)
                    if _sv_rc != 0 or _sv_regr is not None:
                        file_path.write_text(_stubbed, encoding="utf-8")
                        _log(f"  [phase-c/impl] {block_id}: best compiling "
                             "attempt NOT salvaged ("
                             + ("no longer compiles here" if _sv_regr is None
                                else f"would regress {_sv_regr[0].summary}")
                             + ") — stubs kept.")
                        _verdict_row(
                            block_id, Disposition.SALVAGE_REFUSAL,
                            "best compiling image not salvaged — "
                            + ("no longer compiles" if _sv_regr is None
                               else "would regress"),
                            outcome=GateOutcome.NEGATIVE,
                            attempt=fix_attempt)
                    else:
                        _log(f"  [phase-c/impl] {block_id}: fix loop exhausted "
                             f"— KEEPING best compiling image "
                             f"({_salvaged.metric}); stubs would abort the test "
                             "binary and un-judge their siblings. Deferred to "
                             "the outer loop.")
                        _emit("phase_c.impl.fill.salvaged", data={
                            "cluster": cluster_name, "file": block.file,
                            "container": block.container,
                            "failing_tests": _salvaged.metric.failing_tests,
                        })
                        _verdict_row(
                            block_id, Disposition.KEEP_DEFERRED,
                            "fix loop exhausted — best compiling image kept, "
                            "judged at the next sweep",
                            outcome=GateOutcome.UNMEASURABLE,
                            attempt=fix_attempt, model_calls=fix_attempt)
            with _counters_lock:
                impls_failed += 1
                errors.append(f"{block.file}::{block.container}: "
                              f"impl-fill failed after {fix_attempt} attempt(s)")
            _emit("phase_c.impl.fill.fail", duration_ms=int(
                (time.monotonic() - _t0) * 1000),
                data={
                    "cluster": cluster_name,
                    "file": block.file,
                    "container": block.container,
                    "members_failed": len(block.stub_members) + len(block.refill_members),
                    "fix_attempts": fix_attempt,
                    "retro_rounds": _retro_round,
                    "last_error_preview": last_error_excerpt[:200],
                    "error_codes": sorted(ecodes_in_text(last_error_excerpt)),
                })

    # File-first pass — one prompt designs a file's frontier together;
    # the per-block loop below handles the remainder (the freshness gate
    # skips blocks the file pass completed). Unit selection is
    # `file_route_units` (domain): the historical multi-block-only selection
    # by default; every frontier file — free fns and single-block files
    # included — under file_primary. Unset env resolves to the model's
    # validated default (`model_defaults.py`), explicit env always wins.
    _route_env = os.getenv("PHASE_C_ROUTE_ORDER")
    _route_order = resolve_route_order(_route_env, _TASK2_MODEL)
    if os.getenv("PHASE_C_FILE_FIRST", "1") != "0":
        _ff_units = file_route_units(
            blocks, max_stubs=_PER_IMPL_MAX_STUBS,
            file_primary=_route_order is RouteOrder.FILE_PRIMARY)
        if _route_order is RouteOrder.FILE_PRIMARY and _ff_units:
            _src = "env" if (_route_env or "").strip() else (
                f"model default for {_TASK2_MODEL}")
            _log(f"  [phase-c/file] route order file_primary ({_src}): "
                 f"{len(_ff_units)} file unit(s) dispatch first "
                 f"({sum(len(bs) for _, bs in _ff_units)} block(s), "
                 "free fns and single-block files included).")
        # One FILE is this route's unit, so independent files dispatch to a
        # pool for exactly the reasons the per-block route below already
        # states: `_splice_check_lock` and `_counters_lock` serialise
        # everything touching shared state, and no two units name the same
        # file, so their splices cannot collide.
        #
        # It ran serially on the MAIN THREAD until 2026-08-09, and that is
        # where run `491052-1af6b4f4`'s wall clock went. Paired from
        # `logs/events.jsonl`: 47 % of the run's 5-second slots had a
        # MainThread call in flight, and across those slots mean concurrency
        # was **1.03** — the worker pools sat idle behind it — against 2.85
        # while MainThread was free. 20 of the run's 68 MainThread calls came
        # from this loop, at a mean of 193 s each.
        if _ff_units and min(max(1, parallelism), len(_ff_units)) > 1:
            _log(f"  [phase-c/file] parallelism="
                 f"{min(max(1, parallelism), len(_ff_units))} over "
                 f"{len(_ff_units)} file(s) (one prompt per file, "
                 "splice+cargo serialised per tree).")
        run_units_concurrently(
            _ff_units, workers=parallelism,
            name_prefix=f"file-first-{cluster_name}",
            run_unit=lambda u: _fill_file_first(u[0], u[1]),
            describe=lambda u: u[0], log=_log,
            check_stop=_check_graceful_stop,
        )
    # Independent FILES dispatch to a thread pool — the LLM call inside
    # each block's fix loop is the slow part; `_splice_check_lock` +
    # `_counters_lock` (above) already serialize everything that touches
    # shared state, so concurrent files are safe the same way the per-fn
    # engine's worker pool is (phase_c_engine.py). Blocks WITHIN a file are
    # never split across workers — sibling impls on one type are frequently
    # co-designed, so a worker processes its file's blocks one at a time.
    _by_file_q: dict[str, list[ImplBlock]] = {}
    for _b in viable_blocks:
        _by_file_q.setdefault(_b.file, []).append(_b)
    _file_units = list(_by_file_q.values())
    _nw = min(max(1, parallelism), len(_file_units)) if _file_units else 0
    if _nw > 1:
        _log(f"  [phase-c/impl] parallelism={_nw} (LLM calls concurrent "
             "across files, serial within a file; splice+cargo serialised per tree).")
        _file_queue: _queue_mod.Queue[list[ImplBlock]] = _queue_mod.Queue()
        for _fb in _file_units:
            _file_queue.put(_fb)

        def _impl_worker() -> None:
            while True:
                # Stop pulling NEW files once another worker (or the main
                # thread) has seen a graceful-stop request — raises in THIS
                # thread immediately, same as the per-block check inside
                # `_fill_one_block` below; the main thread's own check (after
                # the join below) is what actually ends the process.
                _check_graceful_stop("impl-fill worker dispatch")
                try:
                    _fblocks = _file_queue.get_nowait()
                except _queue_mod.Empty:
                    return
                for _blk in _fblocks:
                    try:
                        _fill_one_block(_blk)
                    except Exception as _we:
                        _log(f"  [phase-c/impl] worker error on "
                             f"{_blk.file}::{_blk.container}: {_we}")

        _impl_workers = [
            threading.Thread(target=_impl_worker,
                              name=f"impl-fill-{cluster_name}-w{i}")
            for i in range(_nw)
        ]
        for _t in _impl_workers:
            _t.start()
        for _t in _impl_workers:
            _t.join()
        # Main-thread checkpoint: if a worker set the graceful-stop event,
        # this is the only context where raising it actually ends the
        # process (`sys.exit()` in a worker thread only kills that thread).
        _check_graceful_stop("impl-fill pool joined")
    else:
        for block in viable_blocks:
            _fill_one_block(block)
    if _retro_gate.total:
        _log(f"  [phase-c/impl] cluster {cluster_name}: {_retro_gate.render()}")
    # Did this sweep move anything, or did every landed block stall behind the
    # same stub? The route has no next pass to refuse, so this reports rather
    # than stops — but it names the one fn the outer loop must reach, and the
    # event is the counter-part of the 50 `u2.external_only` events that went
    # out unread in run `2879-66905005`.
    _payoff = assess_pass_payoff(block_outcomes)
    if _payoff.is_zero:
        _log(f"  [phase-c/impl] cluster {cluster_name}: PAYOFF ZERO — "
             f"{_payoff.summary}.")
        _emit("phase_c.pass.payoff_zero", data={
            "cluster": cluster_name, "route": "impl",
            "landed": _payoff.landed,
            "blocking_fns": sorted(
                f"{s.file}::{s.fn_name}" for s in _payoff.blockers),
        })
    bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    remaining_records = _scan_unimplemented_records_for_cluster(target_src_dir, cluster)
    # None = extractor absent → text-scan fallback; 0 here would make the
    # caller skip the per-fn cleanup pass entirely.
    remaining_n = (len(remaining_records) if remaining_records is not None
                   else len(_scan_unimplemented_for_cluster(target_src_dir, cluster)))
    return ClusterFillResult(
        fns_filled=fns_filled_total,
        bytes_filled=max(0, bytes_after - bytes_before),
        attempts=attempts_total,
        errors=tuple(errors),
        remaining=remaining_n,
        impls_filled=impls_filled,
        impls_failed=impls_failed,
        dispatched_fns=tuple(sorted(dispatched_keys)),
        reverted_fns=tuple(sorted(reverted_keys)),
    )
