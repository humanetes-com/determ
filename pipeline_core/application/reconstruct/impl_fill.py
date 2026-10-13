"""Per-impl-block Phase-C fill engine — relocated from pipeline.py.

Concrete behind `_PhaseCFillAdapter.fill_impl_blocks`. ~39 collaborators injected via
`ImplFillDeps` (composition root late-binds the pipeline.py helpers for seams); body
relocated byte-for-byte behind a rebind-prelude. FOLLOW-UP: partition `ImplFillDeps` into
the existing `ports/fill_engine.py` role ports (overlaps `_PhaseCFillAdapter`).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.assert_probe_runner import (
    run_assert_probes,
)
from pipeline_core.application.reconstruct.attribution import make_fn_site_locator
from pipeline_core.application.reconstruct.block_evidence import (
    assemble_block_evidence,
    render_prior_regression,
)
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
from pipeline_core.application.reconstruct.fill_reading import (
    last_comparison,
    note_kept,
    note_kept_comparison,
)
from pipeline_core.application.reconstruct.fill_routes import (
    FillRoute,
    PreparedFill,
    RouteLedger,
    RouteRunReport,
    RouteSnap,
)
from pipeline_core.application.reconstruct.fill_scheduler import (
    FillExecutionDeps,
    execute_fill,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.application.reconstruct.peel_revert import (
    LandedBody,
    PeelResult,
    peel_file_first,
    peel_keep_allowed,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
    reconstruction_file_reader,
)
from pipeline_core.application.reconstruct.routes import (
    FileRoute,
    ImplRoute,
    PairRoute,
    SessionRoute,
)
from pipeline_core.application.reconstruct.session_write_set import session_write_set
from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.application.reconstruct.tdd_ratchet import (
    hollow_count,
    snapshot_src_tree,
)
from pipeline_core.application.reconstruct.test_locator import (
    attributed_test_blocks,
)
from pipeline_core.application.reconstruct.tree_txn import (
    PreImage,
    TreeTxn,
    capture,
    restore,
)
from pipeline_core.application.reconstruct.unit_conversation import (
    ConversationPorts,
    TranscriptBound,
    UnitConversation,
)
from pipeline_core.domain import unit_clock
from pipeline_core.domain.attempt_loss import (
    AttemptLoss,
    escalation_refusal_note,
    invariance_refusal_note,
    may_escalate_to_last_resort,
)
from pipeline_core.domain.attribution import (
    FailureFrontier,
    canon_site,
    locator_of,
    records_by_site,
    site_label,
    site_payload,
)
from pipeline_core.domain.behaviour import BehaviourSpec
from pipeline_core.domain.behaviour_select import IdentityMustInput, IdentityMustUnit
from pipeline_core.domain.blamed_type_api import with_blamed_type_api
from pipeline_core.domain.blueprint import Blueprint, blueprint_path, parse_blueprint
from pipeline_core.domain.cfg import cluster_cfg_set, filter_cfg_inactive_branches
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.cluster_plan import fill_invariants_section
from pipeline_core.domain.coverage_slice import entered_by_test
from pipeline_core.domain.coverage_slice import instructions_for as coverage_must_for
from pipeline_core.domain.coverage_slice import unreached_by as coverage_unreached_by
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.crash_site import (
    CrashSites,
    blame_outside_unit,
    crash_evidence_for_units,
)
from pipeline_core.domain.declined import declined_guidance, note_declined
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
from pipeline_core.domain.fill_attempts import (
    FillAttempts,
    current_run_label,
)
from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
    class_for_kept,
    keep_reading,
    note_fill_class,
    tests_could_not_run,
)
from pipeline_core.domain.fill_groups import BlockGroupInput, coupled_fill_groups
from pipeline_core.domain.fill_schedule import (
    RunClock,
    bodies_sha_on_tree,
)
from pipeline_core.domain.held_stubs import HeldStubs
from pipeline_core.domain.identity import ContainerId, RetroKey
from pipeline_core.domain.ignored_inputs import (
    declined_by_ignored_inputs,
    identity_name,
    ignored_inputs_feedback,
    is_declined,
    vouched_for_keep,
)
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
    crash_vtable_callees_from_store,
    installable_fns,
    offer_producers,
    producer_bypasses,
)
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.last_error import (
    LAST_ERROR_KIND,
    LastErrorKind,
    kind_from_record,
    kind_of_compiled_run,
)
from pipeline_core.domain.llm_output import (
    ReplyAudit,
    bind_headerless_names,
    extract_fn_bodies,
    fn_tag_occurrences,
    reply_audit,
)
from pipeline_core.domain.model_defaults import resolve_route_order
from pipeline_core.domain.model_notes import src_text_declaring
from pipeline_core.domain.models import (
    BuildCluster,
    ClusterFillResult,
    DispatchFamily,
    FnLocator,
    FnRecord,
    FnSite,
    Frontier,
    ImplBlock,
    TypeRecord,
)
from pipeline_core.domain.pair_fill import (
    PAIR_CRASH_CALLEES,
    PairDispatch,
    blocks_after_pair_fill,
    crash_named_extras,
    extra_write_members,
    pair_dispatches,
    pair_last_error,
    pair_record_container,
    pair_required_files,
    pairs_to_dispatch,
    quote_dispatcher_when_implementer_entered,
    remembered_pair_callees,
    union_pair_callees,
)
from pipeline_core.domain.pass_payoff import UnitOutcome, assess_pass_payoff
from pipeline_core.domain.reasoning_cap import (
    CallCapReading,
    observe_call,
    read_call,
    serving_units,
)
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refill_evidence import (
    aimed_refill_names,
    asserted_output_literals_from_tests,
    attributed_test_leaves,
    collect_refill_evidence,
    container_header,
    crashed_refill_names,
    escalated_refill_names,
    history_for_block,
    inferred_refill_names,
    measured_owner_refill_names,
    observed_stub_excerpts,
    pinned_observables_from_tests,
    quote_panic_site,
    render_refill_evidence,
    tuple_struct_names,
    unit_notes_text,
    wraps_crate_tuple_struct,
)
from pipeline_core.domain.refill_stamp import (
    blocks_after_file_first,
    container_line_window,
    convicting_tests_for_member,
    homonym_containers,
)
from pipeline_core.domain.refine import (
    CompilingSalvage,
    FixProgress,
    FloorStall,
    best_progress_fields,
    best_progress_of,
)
from pipeline_core.domain.retro_gate import (
    JOINT_BODY_SHA,
    JOINT_MEMBERS,
    BlockedOnStubs,
    ConvictionEvidence,
    ConvictionStanding,
    RetroGateTally,
    RetroGateVerdict,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
    convictor_entry_moved,
    crash_earned_conviction,
    evidence_for_record,
    exhaustion_from_splice_diagnostic,
    honoured_guidance_line,
    impossibility_claim_is_unfounded,
    informed_reentry,
    judge_guidance,
    pins_for_contradiction_guard,
    prescription_removes_pinned_observable,
    regressed_tests_from_excerpt,
    regression_scoped,
    retired_guidance_line,
    screen_guidance,
    screening_unit,
    stamp_conviction,
    tree_premise_absent,
)
from pipeline_core.domain.rust_idents import (
    canon_container,
    fn_reference_counts,
    signature_fixed_by_trait,
)
from pipeline_core.domain.rust_scan import stubbed_fn_names
from pipeline_core.domain.sibling_steer import sibling_steer
from pipeline_core.domain.slice_session import (
    SessionSlice,
    SliceCosts,
    expand_locator_sites,
    locator_only_expansions,
    plan_slices,
    slice_plan_gaps,
    slice_plan_payload,
)
from pipeline_core.domain.splice_feedback import reply_refusal_feedback
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.test_report import (
    JointImprovement,
    U2Decision,
    U2TestGate,
    U2Verdict,
    judge_refill,
    turned_green,
)
from pipeline_core.domain.test_results import (
    REGRESSED_ATTEMPT,
    REGRESSED_OUTPUT,
    RegressedAttempt,
    RegressionComparison,
    TestStateReading,
    regressed_attempt_of,
    regression_detail_kind,
    strip_ansi,
)
from pipeline_core.domain.type_refs import hidden_symbols_from_types
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
    restrict_fill_plan,
)
from pipeline_core.prompts.layout_authoring import BEHAVIOUR_MUST_MAX_CHARS, behaviour_must_instruction
from pipeline_core.prompts.per_impl import sibling_rejected_note
from pipeline_core.prompts.pull_menu import pull_menu_section
from pipeline_core.prompts.retry_turn import (
    AttemptFeedback,
    EvidenceSection,
    fn_output,
    types_new_to,
)
from pipeline_core.prompts.slice_session import (
    DEFAULT_FILE_CHARS as SESSION_FILE_CHARS,
)
from pipeline_core.prompts.slice_session import (
    build_slice_session_prompt,
    render_slice_file,
)

# The rendering thresholds a session's turn 0 is sized at, widest first.
# The widest is the renderer's own default — every file whole below it,
# condensed above. Each halving condenses the next tier of files, and the
# narrowest condenses all but the smallest: that is the room a slice no
# packing can split has to fit under SLICE_SESSION_TURN0_CHARS, and
# condensing costs the session nothing it may write (the editable fns
# render in full at every threshold; the non-editable siblings become
# signatures). Four tiers because on the live tree of run
# `32959-4a2ff1ab` the 20-unit slice's file term measured 70,117 at the
# default and 22,883 three halvings down, and nothing narrower changed it
# — the remaining files are already smaller than the threshold.
_SESSION_FILE_THRESHOLDS: tuple[int, ...] = tuple(
    SESSION_FILE_CHARS >> n for n in range(4))

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


def _audit_reply(
    log: Any, emit: Any, *, cluster: str, unit: str, route: str,
    reply: str, parsed: Sequence[Any],
) -> ReplyAudit | None:
    """Report what a reply's FN tags became when they outnumber the records.

    Two classes, counted apart because they have different owners:

    DECLINED — a tag whose body slot was empty. The model answered the unit
    with nothing; the reader had nothing to lose. Owner: the prompt (what it
    failed to give the model) or the model. Logged by name, emitted as
    `phase_c.fill.declined`, and stamped on each unit's retrospective record
    by the caller so the unit's next prompt carries the fact.

    READER LOSS — a tag with a body that the reader turned into no record.
    Always a pipeline defect, never a reason to spend another call on the
    unit. Logged as HEALTH with the response sha, emitted as
    `phase_c.fill.reader_loss`. It fires on PARTIAL loss, because every drop
    site asked only `if not parsed` and a 7-tag reply yielding 1 record scored
    as a success.

    Run `959654-1e96b403` (2026-09-06) is why the two are separate: a
    file-first reply carried three EMPTY tags for the vtable drop helpers and
    the single reader-loss line called it "a parser defect, NOT a model
    failure" — an accusation the artefact itself refuted."""
    audit = reply_audit(reply, parsed)
    if audit is None:
        return None
    if audit.empty:
        log(f"  [phase-c/declined] {route} {unit}: declined {len(audit.empty)} "
            f"unit(s) — the model answered an empty body: "
            f"{', '.join(audit.empty)} (reply sha {audit.response_sha[:12]}…, "
            f"logs/prompts/{audit.response_sha}.txt)")
        emit("phase_c.fill.declined", data={
            "cluster": cluster, "unit": unit, "route": route,
            "names": list(audit.empty), "response_sha": audit.response_sha})
    if audit.lost > 0:
        log(f"  HEALTH ‼ [reader_loss] {route} {unit}: {audit.tags} FN tag(s) in "
            f"the reply, {audit.records} record(s) out"
            + (f", {len(audit.empty)} declined" if audit.empty else "")
            + f" — {audit.lost} answered body(ies) discarded by the READER. "
            f"Reply sha {audit.response_sha[:12]}… "
            f"(logs/prompts/{audit.response_sha}.txt); this is a parser "
            "defect, NOT a model failure.")
        emit("phase_c.fill.reader_loss", data={
            "cluster": cluster, "unit": unit, "route": route,
            "tags": audit.tags, "records": audit.records, "lost": audit.lost,
            "declined": list(audit.empty),
            "response_sha": audit.response_sha})
    return audit


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
    cluster_filter_type_members: Callable[..., list[TypeRecord]]
    # The run's whole world plan, for the invariants section.
    all_clusters: Callable[[], list[Any]]
    # `(target_src_dir, cluster) -> CfgDeadSites`: the sites this cluster
    # never compiles, stub or filled (`cluster_membership.cluster_dead_sites`).
    cluster_dead_sites: Callable[..., CfgDeadSites]
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
    scan_unimplemented_for_cluster: Callable[..., StubInventory]
    scan_unimplemented_records_for_cluster: Callable[..., list[dict[str, Any]] | None]
    splice_fn_with_where: Callable[..., SpliceOutcome]
    splice_lock_for: Callable[..., threading.Lock]
    types_section_for_fix: Callable[..., str]
    bound_methods_section_for: Callable[..., str]
    u2_test_gate: U2TestGate
    test_regression_baseline: Callable[..., TestStateReading | None]
    test_regression_gate: Callable[..., tuple[RegressionComparison, str] | None]
    # The transcript model call (`AskClient.ask_turns`): every fill route
    # talks to the model through a `UnitConversation` on it, and so does the
    # red-slice session.
    ask_turns: Callable[..., str]
    failed_body_cap: Any
    per_fn_fix_hard_cap: Any
    per_impl_max_stubs: Any
    task2_model: Any
    # Mechanical note validation: (fr_path, error_text) -> retired count.
    # None disables (tests / minimal wiring).
    retire_contradicted_notes: Callable[..., int] | None = None
    # The single-call character limit the session's turn-0 prompt is sized
    # against (`SliceCosts.max_chars` = `SLICE_SESSION_TURN0_CHARS` or 45%
    # of it). 0 disables the char capacity (unit-count packing only).
    single_call_char_limit: int = 0
    # A conversation's opening turn fitted to the context
    # (`AskClient.fit`); None sends it as built.
    fit_turn: Callable[[str], str] | None = None
    # (cmd, cwd, env, timeout) -> (rc, output): the plain cargo runner the
    # assertion probe builds its scratch crate with; None disables the probe.
    run_cargo: Callable[..., tuple[int, str]] | None = None
    # (project_dir, cluster_name, failing_tests=) -> CoverageSlice | None:
    # the owner-map probe, so a session can tell the model which of its
    # changed units the still-failing targets execute; None disables it.
    measure_coverage_slice: Callable[..., Any] | None = None
    # The structure reader's parse check, `(path) -> parses?` — the splice
    # gate's validator. Handed to the deterministic import repair so a
    # rustc-suggested `use` that would leave the file unparseable is
    # refused by the transaction, not discovered by the next check; and
    # to the salvage write. None disables (fixture path).
    parse_ok: Callable[[Path], bool] | None = None


def cluster_active_stub_count(
    target_src_dir: Path, cluster: BuildCluster | None,
    scan_records: Callable[..., list[dict[str, Any]] | None],
) -> int | None:
    """The number of stubs the cluster compiles — what the regression gate's
    crash-only branch needs (`stub_free`). ``None`` when there is no cluster
    or no scan: unknown is not zero, and the gate keeps its crash-only keep
    on unknown.

    Which sites the cluster never compiles is NOT read off the stub records
    here any more: a stub difference forgets a dead fn once a route fills it.
    That verdict is `cluster_membership.cluster_dead_sites`, over every fn
    record."""
    if cluster is None:
        return None
    active_recs = scan_records(target_src_dir, cluster)
    if active_recs is None:   # extractor unavailable
        return None
    return len(active_recs)


def cluster_scoped_splice(
    splice: Callable[..., SpliceOutcome],
    *,
    inactive: CfgDeadSites,
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
    same fix three times and a fourth site silently omits it.

    `inactive` is the cluster's dead SITES, stub or filled
    (`cluster_membership.cluster_dead_sites`): a dead fn some route already
    filled is refused the same as a stub, so a body the build never compiles
    cannot land by first landing one."""
    if not inactive:
        return splice

    def _guarded(file_path: Path, container: str, fn_name: str, body: str,
                 where_clause: str | None = None, **kwargs: Any) -> SpliceOutcome:
        try:
            rel = _norm_src_rel(str(Path(file_path).relative_to(target_src_dir)))
        except ValueError:
            rel = _norm_src_rel(Path(file_path).name)
        if (rel, fn_name) in inactive:
            log(f"  [phase-c/impl] refused `{rel}::{fn_name}` — every "
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


def prepare_impl_fill(
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
    # Units the owner map admitted: the red tests executed them before
    # failing. Graded MEASURED_OWNER (coverage), never ESCALATED: the
    # futility sentence is a claim about consumers measured flat, which no
    # one measured for these (run `3076-051d385a`, 2026-10-13).
    measured_owner_keys: frozenset[FnLocator] = frozenset(),
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
    # The subset of crash_blamed an invalid-memory crash blamed. The plan
    # line's ownership MUST and family promotion attach to these only.
    crash_memory_blamed: frozenset[FnLocator] | None = None,
    # The blame and evidence at the bodies the frames ran
    # (`crash_site.crash_sites_of`): the refill admission and the member
    # grades narrow a locator blame to the container a frame resolved to.
    crash_sites: CrashSites | None = None,
    # Shared crash-block map (unit key -> blamed-file fingerprint), mutated
    # here and read across iterations — see PerFnPassCtx.crash_blocked.
    crash_blocked: dict[str, str] | None = None,
    # (cargo test output) -> culprits of the crashes still in it, read by
    # `SameCrashJudge` beside `crash_blamed`. None is "not localized": a
    # keep that wrote a crash_blamed unit whose aimed tests still crash is
    # then refused, never silently kept.
    culprits_of: Callable[[str], Collection[FnLocator]] | None = None,
    # Cluster behaviour suite — identity Then on first-pass write: and
    # per-impl instructions. Default empty: callers that do not pass it
    # (fn_walk, tests) leave those prompts byte-identical to the pre-instruction
    # render.
    behaviour_specs: Sequence[BehaviourSpec] = (),
    dispatch_ranks: dict[FnLocator, int] | None = None,
    # Failing test -> every crate fn it executes (the sweep's coverage owner
    # map plus the attributed units). The red-slice session's editable set;
    # None or empty runs no session.
    session_slices: Mapping[str, frozenset[FnSite]] | None = None,
    # Failing test -> the units the sweep's coverage MEASURED it executes
    # (the owner map alone, without attributed additions): the session's
    # per-unit executed marks. None leaves the marks off.
    session_owners: Mapping[str, frozenset[FnSite]] | None = None,
    # Held test -> the still-stubbed units its first failure sits in
    # (`held_stubs.HeldStubs.hollow`): the only hollow relation this fill's
    # schedule and session planner read. None is "nothing held".
    held: Mapping[str, frozenset[FnLocator]] | None = None,
) -> PreparedFill:
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
    _cluster_filter_type_members = deps.cluster_filter_type_members
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
    _parse_ok = deps.parse_ok
    _save_retrospectives = deps.save_retrospectives
    _scan_unimplemented_for_cluster = deps.scan_unimplemented_for_cluster
    _scan_unimplemented_records_for_cluster = deps.scan_unimplemented_records_for_cluster
    _splice_fn_with_where = deps.splice_fn_with_where
    _splice_lock_for = deps.splice_lock_for
    _types_section_for_fix = deps.types_section_for_fix
    _bound_methods_section_for = deps.bound_methods_section_for
    _u2_test_gate = deps.u2_test_gate
    _fill_classes = FillClassCounter()
    # Created before ``_cls`` so every classification (file-first on the
    # main thread, impl workers) takes the same lock. ``add`` is a
    # non-atomic read-modify-write; under PIPELINE_PARALLELISM>1 the
    # impl route called it off workers with no lock while other sites
    # held two different ones.
    _counters_lock = threading.Lock()
    # The same classes split by the route run that classified them, keyed
    # by the run label bound on the classifying thread ("" outside any
    # run). A route row's `classes` is the run's own histogram delta; the
    # fill-wide counter above is one window over a concurrent wave, and a
    # row read off it named its siblings' outcomes.
    _classes_by_run: dict[str, FillClassCounter] = {}

    def _cls(kind: FillOutcomeClass, unit: str, route: str) -> None:
        with _counters_lock:
            note_fill_class(_emit, _fill_classes, kind,
                            cluster=cluster_name, unit=unit, route=route)
            _classes_by_run.setdefault(
                current_run_label(), FillClassCounter()).add(kind)

    def _note_unit_end(reason: str, route: str, unit: str, attempts: int,
                       **extra: object) -> None:
        """One line and one event for a unit that may buy no further call
        (`unit_clock.ended`): past its bound, or its claim's ground moved."""
        line, event, data = unit_clock.end_note(reason, attempts=attempts)
        _log(f"  [phase-c/{route}] {unit}: {line}"
             + "".join(f", {k.replace('_', ' ')} {v}" for k, v in extra.items())
             + "; no last-resort, no retrospective.")
        _emit(event, data={"cluster": cluster_name, "route": route,
                           "unit": unit, **extra, **data})

    def _classes_of_run() -> FillClassHistogram:
        """This run's classes plus the unlabelled ones — the keep-log rule
        (an unlabelled note counts for any run). Caller holds the lock."""
        label = current_run_label()
        own = _classes_by_run.get(label)
        hist = own.histogram() if own is not None else FillClassHistogram()
        unbound = _classes_by_run.get("") if label else None
        return hist.combined_with(
            unbound.histogram() if unbound is not None else None)

    def _record_regression_evidence(
        keys: Iterable[RetroKey], *,
        newly_red: Iterable[str] | None = None,
        newly_red_output: Mapping[str, str] | None = None,
    ) -> None:
        """A kept landing whose gate reading forgave newly-red tests (the
        sweep's rule: strict net gain) stamps those tests and what they
        printed on every record the next prompt for these units reads —
        the pair route already did this on its own record; the file and
        impl routes did not, so their forgiven losses were silent
        leftovers. The session route stamps its OWN slice key
        (`*session*`), which only the next session on the same slice reads:
        `_prior_regression_for` reads UNIT keys, so a session kept with
        newly-red tests (run `1092886-60870fdd`, 2026-09-06: two tests
        forgiven on a net +5 keep) left every later file-first and per-impl
        prompt for those units without `<PRIOR_REGRESSION>` — zero of the
        prompts on disk carried the tag and zero of 40 records carried
        `regressed`. The session passes its own reading here explicitly
        because its comparisons are not the route gate's `last_comparison`.

        Every caller is a KEEP, so the stamp also says what became of the
        attempt behind a record's `regressed`: a keep that forgave losses is
        `KEPT`; a clean keep over a record a revert stamped makes it
        `REVERTED_THEN_KEPT`, so the next prompt stops calling that revert
        "the PREVIOUS attempt"."""
        keys = list(keys)
        if newly_red is None:
            cmp = last_comparison()
            if cmp is not None and cmp.newly_red:
                newly_red = cmp.newly_red
                newly_red_output = cmp.newly_red_output
        red = sorted(str(n) for n in (newly_red or ()))
        with _counters_lock:
            changed = False
            for key in keys:
                rec = dict(retros_cache.get(key, {}) or {})
                if red:
                    rec["regressed"] = red
                    rec[REGRESSED_OUTPUT] = dict(newly_red_output or {})
                    rec[REGRESSED_ATTEMPT] = RegressedAttempt.KEPT.value
                elif (rec.get("regressed")
                      and regressed_attempt_of(rec)
                      is RegressedAttempt.REVERTED):
                    rec[REGRESSED_ATTEMPT] = (
                        RegressedAttempt.REVERTED_THEN_KEPT.value)
                else:
                    continue
                retros_cache[key] = rec
                changed = True
            if changed:
                _save_retrospectives(project_dir, retros_cache)

    def _prior_regression_for(keys: Iterable[RetroKey], pins: str) -> str:
        """The rendered PRIOR_REGRESSION section for a prompt over `keys`:
        the union of what those records say went red, with what it printed."""
        # Grouped by what became of the attempt that reddened them: one
        # section per claim, so a kept unit's forgiven losses are not
        # labelled as a sibling's revert.
        names: dict[RegressedAttempt, list[str]] = {}
        outputs: dict[str, str] = {}
        for key in keys:
            rec = retros_cache.get(key, {}) or {}
            group = names.setdefault(regressed_attempt_of(rec), [])
            for n in (rec.get("regressed") or []):
                if str(n) not in group:
                    group.append(str(n))
            outputs.update({str(k): str(v) for k, v in dict(
                rec.get(REGRESSED_OUTPUT) or {}).items()})
        return "\n".join(
            render_prior_regression(tuple(ns), pins, outputs, attempt)
            for attempt, ns in names.items() if ns)

    def _note_declined_units(
        file_rel: str, names: Sequence[str],
        container_of: Callable[[str], str], *,
        response_sha: str, route: str,
    ) -> None:
        """A declined unit is classified DECLINED and its retrospective
        record is stamped, so the next prompt for it (any route) carries
        the fact that its previous reply was an empty body."""
        with _counters_lock:
            for _dn in names:
                note_declined(
                    retros_cache,
                    _retro_key(file_rel, _dn, container_of(_dn) or ""),
                    response_sha=response_sha)
            _save_retrospectives(project_dir, retros_cache)
        for _dn in names:
            _cls(FillOutcomeClass.DECLINED, f"{file_rel}::{_dn}", route)

    def _site_locator() -> Callable[[str, int], FnSite | None]:
        """Locator from the tree as it is NOW — not a fill-phase snapshot.

        ``_make_record_locator`` memoizes records and file text on first
        use of a closure. A locator built once at ``fill_impl_blocks``
        entry then overrode ``container_line_window`` with a
        pre-splice container, demoting the target's own error to
        ``cross``. Built at the post-splice ``focus_diagnostics`` call.
        """
        return make_fn_site_locator(
            SimpleNamespace(enumerate_fns=_enumerate_fns_via_extractor),
            target_src_dir)

    _test_regression_baseline = deps.test_regression_baseline
    _test_regression_gate = deps.test_regression_gate
    # Sieve counters for this whole impl-fill batch (workers share it).
    _retro_gate = RetroGateTally()
    _FAILED_BODY_CAP = deps.failed_body_cap
    _PER_FN_FIX_HARD_CAP = deps.per_fn_fix_hard_cap
    _PER_IMPL_MAX_STUBS = deps.per_impl_max_stubs
    _TASK2_MODEL = deps.task2_model
    # Resolved once: the stamp below and the file-first unit list must agree
    # on which planner writes the block. Unset env is the model's default
    # (Muse → file-primary).
    _route_env = os.getenv("PHASE_C_ROUTE_ORDER")
    _route_order = resolve_route_order(_route_env, _TASK2_MODEL)
    _file_primary = _route_order is RouteOrder.FILE_PRIMARY
    if cluster_env is None:
        cluster_env = _cargo_env()
    # Absent frontier (fn_walk driver) = nothing dispatched by test, nothing
    # convicted by test; the compile gate and the regression ratchet still run.
    _frontier_tests = tdd_frontier_tests or FailureFrontier.of_dispatch({})
    # Populated after `all_blocks` is known — same-named members in other
    # containers of the file, so conviction is container-qualified.
    _homonym_map: dict[tuple[str, str], tuple[str, ...]] = {}

    def _behaviour_must(*items: IdentityMustInput) -> str:
        """The behaviour book's instruction for these units (identity MUST
        and aimed rules), or "" when the book has nothing for them.

        Items are :class:`IdentityMustUnit` or :class:`FnRecord`.
        Names-only is the un-aimed path and is not used here —
        Display::fmt would get a MUST it cannot satisfy.
        """
        return behaviour_must_instruction(behaviour_specs, units=items)

    def _coverage_must(file_rel: str, *names: str) -> str:
        return coverage_must_for(file_rel, names)

    def _convicting_for_block(b: ImplBlock) -> frozenset[str]:
        """Tests with DIRECT evidence at this block's members — U2's
        revert set. The frontier key is ``(file, fn_name)``; U2 already
        splits homonyms on ``FnSite(file, name, container)`` via
        ``partition_blame``. Filtering here with the homonym scorer
        emptied the set when every container scored 0 (``impl Debug for
        Error`` on run ``463713-b7c666c8``) and ``u2_test_gate`` accepts
        on no tests. The stamp/skip still uses
        ``convicting_tests_for_member``; this gate must not."""
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
    _cfg_dead_pairs = deps.cluster_dead_sites(target_src_dir, cluster)
    _active_stub_count = cluster_active_stub_count(
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
        return _cfg_dead_pairs.fns_in(file_rel)
    # This tree's splice+cargo lock (per-tree: parallel clusters don't share).
    _splice_check_lock = _splice_lock_for(target_src_dir)
    # Guards the plain-Python aggregate counters (`impls_failed`, `errors`)
    # and the retrospective-cache save — the parts of a block's fill that
    # touch SHARED state but fall outside the per-attempt
    # `_splice_check_lock` critical section. The lock itself is created
    # next to `_cls` above so classification shares it. The model-call
    # count is NOT one of these: it lives on `attempts` (`note_call`),
    # under that object's lock and stamped with the run that made it.
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    impls_filled = 0
    impls_failed = 0
    fns_filled_total = 0
    errors: list[str] = []
    # Per-unit attempt bookkeeping (`domain/fill_attempts.py`): dispatched
    # (stamped at the points a unit is handed to a fill cycle, AFTER the skip
    # gates — the caller's exhaustion charging keys on this, so a member a
    # refusal gate skipped is spared the strike a tried-and-failed member
    # earns), and each unit's LAST outcome — kept, reverted (spliced then
    # restored to the pre-fill snapshot: peel / compile / U2 / regression),
    # or lost (the model was paid and no routable reply came back: run
    # `347397-01e2463b`, 2026-08-31, graded src/ensure.rs::render `flat` off
    # an empty reply and deranked it into the next iteration's flat sink).
    # The route rows, the TDD audit's FLAT arm and the fill result all read
    # these views, so a unit reverted on one attempt and kept on the next is
    # kept everywhere (run `1577437-c93d5c26` iter 4 graded such a run
    # REVERTED off a revert mark no keep ever cleared).
    attempts = FillAttempts()
    # Files whose file-first call actually LANDED bodies this call (a kept
    # splice — peel survivors and non-regression keeps included). The
    # same-call refill-only drop below keys on this, never on the dispatch
    # list: of 23 drops across the 2026-08-31 logs, 8 suppressed the
    # per-block fallback its own log line had just promised, for files
    # whose call produced nothing.
    ff_produced_files: set[str] = set()

    def _note_reverted(file_rel: str, names: Sequence[str], *,
                       collateral: bool = False) -> None:
        """`collateral`: the names were restored as one transaction whose
        evidence did not narrow to any of them (a whole-file or whole-block
        restore). A peel's reverts are the ones the compiler implicated."""
        attempts.note_reverted((FnLocator(file_rel, n) for n in names if n),
                               collateral=collateral)

    def _note_kept(file_rel: str, names: Sequence[str]) -> None:
        attempts.note_kept(FnLocator(file_rel, n) for n in names if n)

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
                     route: str = "impl",
                     extra: Mapping[str, object] | None = None) -> None:
        _payload: dict[str, object] = {
            **VerdictRecord(
                unit=unit, route=route, attempt=attempt,
                disposition=disposition, reason=reason, outcome=outcome,
                validity=validity or ValidityVector(),
                evidence_keys=evidence, model_calls=model_calls,
                prompt_chars=prompt_chars).to_json_dict(),
            "cluster": cluster_name,
        }
        if extra:
            _payload.update(extra)
        _emit("phase_c.verdict", data=_payload)

    blocks = _collect_impl_blocks_for_cluster(target_src_dir, cluster) or []
    if not blocks:
        return PreparedFill(
            routes=(), hollow={},
            finish=lambda: ClusterFillResult(
                dispatched_fns=(), fill_classes=_fill_classes.histogram()),
            bodies_sha=lambda _t: "")

    # A compiling pair crash-revert leaves no sweep crash to localize
    # (the tree rolled back). Recover the table fns the filled body
    # called from the retro store. Same-file slots join the pair write
    # set (an independently judged file of them on the rolled-back tree
    # cannot pay — run `10959-9c95ea89`). Other-file slots stay
    # crash-blamed so pair_wait does not park a run the pair cannot
    # write. Run `269600-aa403030` fill 1 recovered the class.
    retros_cache = _load_retrospectives(project_dir)
    _pair_crash_callees = crash_vtable_callees_from_store(
        retros_cache,
        _enumerate_fns_via_extractor(target_src_dir) or [],
        _cluster_filter_type_members(
            _enumerate_types_via_extractor(target_src_dir) or [], cluster))
    _hop_pairs = pairs_to_dispatch(_frontier_tests.pairs)
    _pair_files = {
        s.file for p in _hop_pairs for s in (p.dispatcher, p.implementer)}
    _stored_callees: set[FnLocator] = set()
    for _p in _hop_pairs:
        _stored_callees |= remembered_pair_callees(
            retros_cache.get(_retro_key(
                _p.dispatcher.file, "*pair*", pair_record_container(_p)),
                {}) or {},
            _p.member_locators())
    # Same-file slots join the pair write set and are NOT crash-blamed:
    # the CRASHED / faulting-frame grade is a lie for a table fn the
    # pair called (run `100178` fill 1: that grade rewrote the slots,
    # compiled, and reddened ten already-passing Debug tests). A
    # third-file slot stays crash-blamed so pair_wait cannot park it.
    _same_callees = frozenset(
        k for k in (_pair_crash_callees | _stored_callees)
        if k.file in _pair_files)
    _other_callees = frozenset(
        k for k in _pair_crash_callees if k.file not in _pair_files)
    _pair_extra_locs = _same_callees
    # Passing extras the *current* crash extract named are aimed refills;
    # remembered-only extras stay quoted. Using the union as crash_named
    # would re-force every remembered slot (run 171582). Extras the last
    # failed reply already emitted stay quoted too: run 609480 fill 1
    # aimed them, the reply rewrote them, the keep crash-reverted, and
    # fill 2's rematch then used a different panic stem and un-aimed.
    _failed_bodies = []
    for _p in _hop_pairs:
        _failed_bodies.append(str((
            retros_cache.get(_retro_key(
                _p.dispatcher.file, "*pair*", pair_record_container(_p)),
                {}) or {}).get("last_failed_body") or ""))
    _extract_named = frozenset(
        k for k in _pair_crash_callees if k.file in _pair_files)
    _crash_named = crash_named_extras(
        _extract_named, failed_body="\n".join(_failed_bodies))
    _quoted_emitted = _extract_named - _crash_named
    if _quoted_emitted:
        _log("  [phase-c/pair] "
             + str(len(_quoted_emitted))
             + " same-file slot(s) already emitted in the last failed "
               "body — quoted, not aimed: "
             + ", ".join(sorted(
                 f"{k.file}::{k.fn_name}" for k in _quoted_emitted)[:8]))
    if _other_callees:
        crash_blamed = (crash_blamed or frozenset()) | _other_callees
        tdd_frontier_keys = set(tdd_frontier_keys or ()) | set(
            _other_callees)
        _ev = dict(crash_evidence or {})
        _why = (
            "A compiling pair fill crash-reverted with a panic outside "
            "the pair write set. This fn is the table slot that fill "
            "called, in a file the pair does not write.")
        for _loc in _other_callees:
            _ev.setdefault(_loc, _why)
        crash_evidence = _ev
    if _same_callees or _other_callees:
        _same_s = sorted(f"{k.file}::{k.fn_name}" for k in _same_callees)
        _other_s = sorted(f"{k.file}::{k.fn_name}" for k in _other_callees)
        _log("  [phase-c/pair] "
             + str(len(_pair_crash_callees | _stored_callees))
             + " vtable callee(s) of a compiling pair crash-revert"
             + ((" — " + str(len(_same_s))
                 + " same-file slot(s) join the pair write set: "
                 + ", ".join(_same_s[:8])) if _same_s else "")
             + ((" — " + str(len(_other_s))
                 + " other-file slot(s) admitted as crash-blamed "
                 "(pair cannot write them): "
                 + ", ".join(_other_s[:8])) if _other_s else ""))
        _emit("phase_c.pair.crash_callees", data={
            "cluster": cluster_name,
            "callees": sorted(f"{k.file}::{k.fn_name}"
                              for k in (_pair_crash_callees | _stored_callees)),
            "same_file": _same_s,
            "other_file": _other_s})

    # Stamp `refill_members` onto blocks that have an
    # already-filled, compiling member with a failing attributed test, so the
    # co-designed block is re-attacked WHOLE in one call instead of leaving
    # wrong bodies to a per-fn refill that can only touch one member at a time.
    blocks = collect_impl_refill_targets(
        blocks, tdd_frontier_keys, tdd_frontier_tests,
        max_stubs=_PER_IMPL_MAX_STUBS,
        crash_blamed=crash_blamed or frozenset(),
        crash_sites=crash_sites,
        file_primary=_file_primary)

    # Every impl in the cluster, kept whole. Narrowing below answers "what do we
    # fill?"; routing a file-wide model reply asks the different question "what
    # impls EXIST in this file?", and must not be answered with the fill set.
    all_blocks = blocks
    _homonym_map.update(homonym_containers(all_blocks))

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
    # The route's unit COUNT, as a fact rather than a log sentence. A fill step
    # is a sequence of routes and each route a sequence of units, and until
    # this was emitted the only unit count in the stream was the per-fn route's
    # (`phase_c.pass.start.stubs`) — so an observer could say a fill was in the
    # impl route and never how far through it. On run `1383168-eb151d09`
    # (2026-08-29) that gap covered 21:41 -> 22:30 UTC, the longest single
    # stretch of the iteration. `units` is what this route will dispatch;
    # `skipped` is what it hands to the per-fn fallback, which is the other
    # route's work and never this one's denominator.
    _emit("phase_c.fill.route_plan", data={
        "cluster": cluster_name, "route": "impl",
        "units": len(viable_blocks),
        "skipped": len(blocks) - len(viable_blocks),
        "refill_units": _refill_block_count})
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
    invariants_section = fill_invariants_section(
        cluster, no_std=_no_std_invariant, macro_invariants=_macro_invariants,
        worlds=deps.all_clusters())
    _crate_type_records = _cluster_filter_type_members(
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
    # Fill file for the type_def pull: workers run different files
    # concurrently, so a shared holder would leak another file's visibility
    # into this one. `visible_from` is how a `pub trait` behind `mod ext`
    # in context.rs is dropped from an error.rs pull (same wall as
    # TYPES_IN_SCOPE; run `155810-cbb3b9fc`).
    _pull_file = threading.local()
    # One reader for both crate trees (the reconstruction's `src/`, the
    # oracle's `tests/`): the pull route and the evidence block's panic-site
    # quote read the same files under the same containment.
    _read_source = reconstruction_file_reader(target_src_dir, project_dir)

    def _cluster_view(text: str) -> str:
        """A file as this cluster compiles it — what the prompts render."""
        if cluster is None:
            return text
        return filter_cfg_inactive_branches(text, cluster_cfg_set(cluster))

    def _has_fn_bodies(reply: str) -> bool:
        return bool(extract_fn_bodies(reply))
    _pull_sources = PullSources(
        read_file=_read_source,
        find_tests=lambda name: "\n\n".join(
            f"// {p}\n{t}" for p, t in _find_tests_referencing_fn(
                tests_dir_files, name, max_blocks=4)),
        type_def=lambda name: (_types_section_for_fix(
            name, "", "", "", "", _crate_type_records,
            file_rel=getattr(_pull_file, "file", "") or "")
            # Crate records win; well-known unstable std APIs answer as a
            # last fallback so the pull never starves on a std type.
            or std_api_def(name)),
        full_error=lambda: strip_ansi(_last_full_out[0]),
    )
    _pull_menu = (pull_menu_section(_pull_sources.available_kinds(), _max_pulls)
                  if _pulls_on else "")

    def _fill_turns(messages: list[dict[str, Any]], *,
                    followup_think: bool = False) -> str:
        """The fill routes' model call, one transcript at a time.

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
        return str(deps.ask_turns(messages, model=_TASK2_MODEL,
                                  followup_think=followup_think))

    def _open_conversation(label: str, route: str) -> UnitConversation:
        """The conversation one fill unit (a block, a file group, a pair)
        talks to the model through, from its turn-0 prompt to its last
        retry. Requests are answered on it when `CONTEXT_PULLS` is on."""
        return UnitConversation(
            ConversationPorts(
                ask_turns=_fill_turns, log=_log, emit=_emit,
                fit=deps.fit_turn or (lambda t: t),
                max_transcript_chars=int(deps.single_call_char_limit or 0)),
            label=label, route=route,
            requests=(make_request_answerer(
                _pull_sources, log=_log, emit=_emit, max_pulls=_max_pulls)
                if _pulls_on else None),
            menu=_pull_menu)

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

    def _fill_file_first(file_rel: str, fblocks: list[ImplBlock],
                         may_claim: Callable[[], bool] | None = None,
                         withheld: frozenset[str] = frozenset()) -> None:
        """File-first pass: ONE prompt fills every stub of a multi-block
        file so the impls are designed together (run-187469 evidence: the
        Own/Ref/Mut family in src/ptr.rs diverged when filled per block).
        Strict gate — kept only on rc==0 (+U2 with comparative banking), else
        reverted verbatim and handed to the per-block loop.

        A file too big for one prompt is no longer skipped wholesale — that
        fragmented the coupled vtable family back to per-fn fills exactly when
        it was most-stubbed. It is partitioned into TEST-WELDED block groups
        (blocks whose attributed frontier tests overlap must be designed
        together), each within the prompt bound, each filled jointly.

        ``withheld`` names the file's members the schedule deferred as
        re-buys this fill (`narrow_blocks_to`): `fblocks` no longer asks for
        them, and a body the reply writes for one anyway is not spliced."""
        file_path = target_src_dir / file_rel
        if not file_path.exists():
            return
        plan = _file_fill_plan(
            file_rel, fblocks,
            frontier_tests=_frontier_tests,
            per_impl_max_stubs=_PER_IMPL_MAX_STUBS)
        if plan.groups is None:
            if plan.wanted:
                _fill_block_group(file_rel, fblocks, file_rel, withheld)
            return
        if not plan.groups:
            # Decided — and logged — at plan time by `_file_route_admits`,
            # so this run was never scheduled; reached only when the file's
            # blocks changed between the plan and the run.
            return
        _sizes = ", ".join(str(n) for n in plan.sizes)
        _log(f"  [phase-c/file] {file_rel}: {plan.wanted} fn(s) exceeds "
             f"PHASE_C_FILE_MAX_FNS={plan.max_fns} — split into "
             f"{len(plan.groups)} group(s) of {_sizes} fn(s) (test-welded, "
             "plus any block no other route covers) "
             f"(+{plan.solo} uncoupled block(s) → per-block loop).")
        for _gi, _g in enumerate(plan.groups, 1):
            if _gi > 1 and may_claim is not None and not may_claim():
                _log(f"  [phase-c/file] {file_rel}: fill share spent after "
                     f"{_gi - 1} of {len(plan.groups)} group(s) and the last "
                     "landed nothing — the remaining groups are left to the "
                     "next fill.")
                break
            _fill_block_group(file_rel, [fblocks[i] for i in _g],
                              f"{file_rel}[group {_gi}/{len(plan.groups)}]",
                              withheld)

    def _fill_block_group(file_rel: str, fblocks: list[ImplBlock],
                          glabel: str,
                          withheld: frozenset[str] = frozenset()) -> None:
        """One joint fill cycle over ``fblocks`` — the whole file, or one
        test-welded group of its blocks: prompt → splice → compile → U2 over
        the group's attributed tests (with comparative banking of strict
        self-failure improvement) → global regression gate."""
        nonlocal fns_filled_total
        file_path = target_src_dir / file_rel
        stub_names = [m.fn_name for b in fblocks for m in b.stub_members]
        refill_names = [m.fn_name for b in fblocks for m in b.refill_members]
        wanted = [*stub_names, *refill_names]
        if not wanted:
            return
        _pull_file.file = file_rel
        attempts.note_dispatched(FnLocator(file_rel, n) for n in wanted)
        # One read serves both: the text the prompts quote and the byte
        # pre-image every whole-file revert below restores through the
        # transaction primitive.
        _pre = capture(file_path, None)
        snapshot = _pre.text("replace")
        # Locators in the crash-evidence dict that are not origin-blame:
        # callees of the faulting frame. `crash_blamed` convicts; this set
        # is dispatch. Without it `collect_refill_evidence` grades them
        # NAMED and `_plan_line` lists them as "leave it unchanged".
        _aimed_locs = frozenset(
            loc for loc in (crash_evidence or {})
            if loc not in (crash_blamed or frozenset()))
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
                                        measured_owner=measured_owner_keys,
                                        crashed=crash_blamed or frozenset(),
                                        aimed=_aimed_locs,
                                        crash_sites=crash_sites,
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
        # TYPES_IN_SCOPE on this route is selected over the WHOLE file, so
        # the observables that may keep a documented type's own statement of
        # an expectation are the file's union — the same scope `_union_pins`
        # already takes, for the same reason.
        _file_observables = frozenset().union(*(
            asserted_output_literals_from_tests(
                attributed_test_leaves(evs, cap=_ATTRIBUTED_TEST_LEAF_CAP),
                _test_texts)
            for evs in _file_refill_evs.values())) if _file_refill_evs \
            else frozenset()
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

        def _unit_for(name: str, container: str = "") -> IdentityMustUnit:
            """Stub-time (name, container, sig) for a writable name of this
            file. Installable/bypass records carry no signature; the
            extractor row does, and aiming reads the return type."""
            hit: dict[str, Any] | None = None
            for r in _all_fn_records:
                if r.get("file") != file_rel or r.get("fn_name") != name:
                    continue
                rec_c = str(r.get("container") or "")
                if not container or rec_c == container:
                    hit = r
                    break
                if hit is None:
                    hit = r
            if hit is None:
                return IdentityMustUnit(name=name, container=container,
                                        file=file_rel)
            return IdentityMustUnit(
                name=name,
                container=str(hit.get("container") or container),
                sig=str(hit.get("sig_text") or ""), file=file_rel)
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
            aimed=tuple(i.fn_name for i in _installable
                        if FnLocator(file_rel, i.fn_name) in _aimed_locs),
            bypass=_free_bypass, pins=_union_pins,
            behaviour_must=_behaviour_must(
                *(_unit_for(i.fn_name) for i in _installable),
                *(_unit_for(x.fn_name, x.container) for x in _free_bypass)),
            coverage_must=_coverage_must(
                file_rel,
                *(i.fn_name for i in _installable),
                *(x.fn_name for x in _free_bypass)))]
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
                   "declaration)" if i.unwired else
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
        def _order_stubs(file_rel: str, names: tuple[str, ...]) -> tuple[str, ...]:
            if not dispatch_ranks:
                return names
            return tuple(sorted(names, key=lambda n: (
                -dispatch_ranks.get(FnLocator(file_rel, n), 0), n)))

        _block_plans = [BlockFillPlan(
            b.container,
            _order_stubs(b.file, tuple(m.fn_name for m in b.stub_members)),
            tuple(dict.fromkeys((*(m.fn_name for m in b.refill_members),
                                 *(x.fn_name
                                   for x in _bypass_for(b.container))))),
            inferred=tuple(sorted(inferred_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            escalated=tuple(sorted(escalated_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            measured_owner=tuple(sorted(measured_owner_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            crashed=tuple(sorted(crashed_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            memory_crashed=tuple(sorted(
                n for n in crashed_refill_names(
                    _file_refill_evs[_impl_block_id(b.file, b.container)])
                if FnLocator(b.file, n) in (crash_memory_blamed
                                            or frozenset()))),
            aimed=tuple(sorted(aimed_refill_names(
                _file_refill_evs[_impl_block_id(b.file, b.container)]))),
            bypass=_bypass_for(b.container),
            pins=_block_pins[_impl_block_id(b.file, b.container)],
            wraps_pinned_type=_block_wraps[_impl_block_id(b.file, b.container)],
            behaviour_must=_behaviour_must(
                *b.stub_members, *b.refill_members,
                *(_unit_for(x.fn_name, x.container)
                  for x in _bypass_for(b.container))),
            coverage_must=_coverage_must(
                b.file,
                *(m.fn_name for m in (*b.stub_members, *b.refill_members)),
                *(x.fn_name for x in _bypass_for(b.container))),
            sigs=tuple(
                (m.fn_name, m.sig_text or "")
                for m in (*b.stub_members, *b.refill_members)
                if m.sig_text))
            for b in fblocks]
        _covered = {" ".join(b.container.split("{", 1)[0].split())
                    for b in fblocks} | {""}
        _extra_plans = [BlockFillPlan(
            _c, (), tuple(x.fn_name for x in _entries),
            bypass=tuple(_entries), pins=_pins_for_bypass(_entries),
            behaviour_must=_behaviour_must(
                *(_unit_for(x.fn_name, x.container) for x in _entries)),
            coverage_must=_coverage_must(
                file_rel, *(x.fn_name for x in _entries)))
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
                    for n in (*p.stubs, *p.refills)),
                hidden_symbols=hidden_symbols_from_types(
                    _crate_type_records, file_rel))
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
                cluster=cluster_name, cfg_dead=_cfg_dead_fns(_rf),
                unit=screening_unit(_rf, _rfn),
                pins=_block_pins.get(_impl_block_id(_rf, _rcont), "") or _union_pins,
                observables=_file_observables)
            if (_hline := honoured_guidance_line(_fk, _frec, _fverdict.text)):
                _log(f"  {_hline}")
            if (_rline := retired_guidance_line(_fk, _frec, _fverdict)):
                _log(f"  {_rline}")
            _text = _fverdict.text.strip()
            if _text and _text not in _seen_retro:
                _seen_retro.add(_text)
                _file_retro_parts.append(_fverdict.text)
            # A declined unit's sentence rides the same section: the
            # instruction is generic and the record is the unit's own.
            _dtext = declined_guidance(_rfn, _frec)
            if _dtext and _dtext not in _seen_retro:
                _seen_retro.add(_dtext)
                _file_retro_parts.append(_dtext)
        _file_retro = "\n\n".join(_file_retro_parts)
        prior_error = ""
        _file_kind: LastErrorKind | None = None
        _retry_names: frozenset[str] | None = None
        # What the kept remainder of a peeled reply was, for the retry turn.
        _retry_note = ""
        # The group's one conversation: attempt 1 opens it with the file
        # prompt; a revert's retry and a peel's retry are turns on it.
        _conv = _open_conversation(glabel, "file")
        _prev_feedback: tuple[object, ...] | None = None
        # The class a reverted attempt earned, noted only when no further
        # attempt follows it (the last attempt, or the unit's end below).
        _revert_cls: FillOutcomeClass | None = None
        for _attempt in range(1, _FILE_FIRST_MAX_ATTEMPTS + 1):
            # Every later call of this group is asked here, once, whichever
            # path came back to the top: an empty reply, nothing spliced, a
            # peel retry after a judged keep, unspliced names, or the
            # compile / test / regression revert that falls through the
            # bottom of the loop. The first attempt is the claim the route
            # already made. Run `3040-51eab48c` (2026-10-02, fill 5): two
            # files each opened attempt 2 for a peeled name after a keep of
            # the same wave had turned the last red test green.
            # Asked on the tests of the names THIS attempt will ask — the
            # peeled or unspliced names of a retry, the whole group on a
            # revert — not on the claim's scope: the gate stands a claim on
            # whatever part of its ground has not moved, so a peel whose own
            # tests had all moved was still bought while another test of the
            # file stayed red.
            _ask_names = (_retry_names if _retry_names is not None
                          else frozenset((*stub_names, *refill_names)))
            _unit_end = unit_clock.ended(frozenset(
                t for n in _ask_names
                for t in _frontier_tests.tests_for(FnLocator(file_rel, n)))
            ) if _attempt > 1 else ""
            if _unit_end:
                if unit_clock.note_once():
                    _note_unit_end(_unit_end, "file", glabel, _attempt - 1)
                if _revert_cls is not None:
                    _cls(_revert_cls, glabel, "file")
                return
            # TYPES_IN_SCOPE selected over the WHOLE file, which is this
            # route's unit — the per-impl path searches one block's member
            # signatures because that is all it may write. A retry adds only
            # the types its result names that were not shown.
            _file_types = "" if _conv.opened else _types_section_for_fix(
                "", snapshot, prior_error, "", "", _crate_type_records,
                file_rel=file_rel, observables=_file_observables)
            _plans: list[BlockFillPlan] = [
                *_free_plan, *_block_plans, *_extra_plans]
            if _retry_names is not None:
                _plans = restrict_fill_plan(_plans, _retry_names)
                _covered = {
                    n for p in _plans for n in (*p.stubs, *p.refills)}
                _missing = tuple(sorted(_retry_names - _covered))
                if _missing:
                    _plans.append(BlockFillPlan(header="", refills=_missing))
            _targets = tuple(
                n for p in _plans for n in (*p.stubs, *p.refills))
            prompt = "" if _conv.opened else build_file_fill_prompt(
                file_rel=file_rel, file_text=snapshot,
                fill_plan=_plans,
                tests_block="\n\n".join(f"// {p}\n{t}" for p, t in test_blocks),
                refill_evidence="\n".join(
                    s for s in (_file_refill_evidence,
                                observed_stub_excerpts(
                                    (FnLocator(b.file, m.fn_name)
                                     for b in fblocks for m in b.stub_members),
                                    _frontier_tests, read_source=_read_source),
                                unit_notes_text(
                                    (FnLocator(b.file, m.fn_name)
                                     for b in fblocks
                                     for m in (*b.stub_members, *b.refill_members)),
                                    _frontier_tests),
                                _install_evidence, _bypass_evidence) if s),
                blueprint=_bp_slice, notes=notes_section,
                # E0616/E0599 feedback carries the blamed type's crate-visible
                # fn inventory (`with_blamed_type_api`) — deterministic per
                # excerpt, so the byte-identical-resample check below still
                # compares what the model would actually receive.
                prior_error=with_blamed_type_api(
                    prior_error, _crate_type_records),
                kind=_file_kind,
                crash_evidence=_file_crash,
                retrospective_guidance=_file_retro,
                module_name=module_name, cluster_name=cluster_name,
                gotchas=language_driver().gotchas_summary(), types_section=_file_types,
                invariants_section=invariants_section,
                prior_regression=_prior_regression_for(
                    (_retro_key(file_rel, m.fn_name, b.container)
                     for b in fblocks
                     for m in (*b.stub_members, *b.refill_members)),
                    _union_pins))
            # A retry whose measured feedback is identical to the previous
            # retry's is a temperature resample, not a retry: the result, its
            # kind and the targets came out the same, so no new information
            # reaches the model. This route retries on that feedback and has
            # no repeated-signature exit, so an identical revert reason looped
            # it verbatim: `src/ensure.rs::render` sent the SAME prompt sha on
            # attempts 2 and 3 (2026-08-19 19:45:30 and 19:53:41 UTC),
            # reverted all three times on the identical 11-test newly-red
            # list, then fell back to per-block anyway.
            _feedback_key = (prior_error, _file_kind, _targets, _retry_note)
            if _conv.opened and _feedback_key == _prev_feedback:
                _log(f"  [phase-c/file] {glabel}: attempt {_attempt} would "
                     "send the same result as the previous retry — the revert "
                     "feedback did not change, so another call buys a "
                     "temperature resample of the same question; falling back "
                     "to per-block fills.")
                _emit("phase_c.file.fill.resample_skipped", data={
                    "cluster": cluster_name, "file": file_rel, "group": glabel,
                    "attempt": _attempt})
                return
            if _conv.opened:
                _prev_feedback = _feedback_key
            _log(f"  [phase-c/file] {glabel}: file-first fill "
                 f"({len(fblocks)} block(s), {len(stub_names)} stub(s), "
                 f"{len(refill_names)} re-fill(s), "
                 f"attempt {_attempt}/{_FILE_FIRST_MAX_ATTEMPTS}).")
            _emit("phase_c.file.fill.start", data={
                "cluster": cluster_name, "file": file_rel, "group": glabel,
                "blocks": len(fblocks), "stubs": len(stub_names),
                "refills": len(refill_names)})
            attempts.note_call()
            try:
                # The stream-meta rows of this call carry the units it
                # serves, so the FLAT grading can tell a measured refusal
                # from a reasoning-capped truncation per unit.
                with serving_units(
                        f"{file_rel}::{n}"
                        for n in (_retry_names if _retry_names is not None
                                  else wanted)):
                    _think = escalated_followup_think(_grade_parts)
                    if not _conv.opened:
                        _file_reply = _conv.open(
                            prompt, has_deliverable=_has_fn_bodies,
                            followup_think=_think)
                    else:
                        _file_reply = _conv.retry(AttemptFeedback(
                            kind=_file_kind,
                            excerpt=with_blamed_type_api(
                                prior_error, _crate_type_records),
                            targets=_targets,
                            output=fn_output(
                                [(n, p.header) for p in _plans
                                 for n in (*p.stubs, *p.refills)]),
                            note=_retry_note,
                            sections=(EvidenceSection(
                                "TYPES_IN_SCOPE", types_new_to(
                                    _conv.text(), prior_error,
                                    _crate_type_records, viewer_file=file_rel),
                                "Types this result names that were not "
                                "shown above:\n"),),
                            moved=_conv.bodies_moved(
                                _read_source, frozenset(wanted),
                                normalize=_cluster_view)),
                            has_deliverable=_has_fn_bodies,
                            followup_think=_think)
                parsed = extract_fn_bodies(_file_reply)
            except TranscriptBound:
                # No reply: the conversation is full. Not an answer dropped
                # and not a verdict on the bodies — the per-block route
                # still gets its shot at the stubs.
                _log(f"  [phase-c/file] {glabel}: conversation at its bound "
                     "— falling back to per-block fills.")
                return
            except Exception as e:
                _log(f"  [phase-c/file] {file_rel}: LLM error: {e}")
                _cls(FillOutcomeClass.ANSWER_DROPPED, glabel, "file")
                # Only the names THIS attempt pursued are lost: on a peel
                # retry the plan was restricted to the peeled names, and
                # the kept remainder of the earlier attempt was landed and
                # judged — marking it lost would strip its service and
                # re-rank a just-landed body as never-served.
                attempts.note_lost(
                    FnLocator(file_rel, n)
                    for n in (_retry_names if _retry_names is not None
                              else wanted))
                return
            _audit = _audit_reply(_log, _emit, cluster=cluster_name,
                                  unit=file_rel, route="file",
                                  reply=_file_reply, parsed=parsed)
            if _audit is not None and _audit.empty:
                _name_cont: dict[str, str] = {
                    n: p.header for p in _plans for n in (*p.stubs, *p.refills)}

                def _cont_of(n: str, _nc: dict[str, str] = _name_cont) -> str:
                    return str(_nc.get(n, ""))

                _note_declined_units(
                    file_rel, _audit.empty, _cont_of,
                    response_sha=_audit.response_sha, route="file")
            if not parsed:
                if (_retry_names is not None
                        and _attempt < _FILE_FIRST_MAX_ATTEMPTS):
                    _log(f"  [phase-c/file] {file_rel}: empty/no FN blocks "
                         f"({fn_tag_occurrences(_file_reply)} FN tag(s) in "
                         "reply) on peel-retry — retrying (attempts remain).")
                    _retry_note = ("Your previous reply carried no <FN> "
                                   "block the harness could parse.")
                    _pre = capture(file_path, None)
                    snapshot = _pre.text("replace")
                    continue
                _log(f"  [phase-c/file] {file_rel}: empty/no FN blocks "
                     f"({fn_tag_occurrences(_file_reply)} FN tag(s) in reply) — "
                     "falling back to per-block fills.")
                if _retry_names is None:
                    _cls(FillOutcomeClass.ANSWER_DROPPED, glabel, "file")
                    attempts.note_lost(
                        FnLocator(file_rel, n) for n in wanted)
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
                _baseline_test = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env)
                _hollow_before = hollow_count(snapshot_src_tree(target_src_dir))
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
                         + (f"block header names {len(_db.ambiguous)} impls "
                            "of this file (ambiguous): "
                            + "; ".join(_db.ambiguous) + ". "
                            if _db.ambiguous else
                            "block header does not match any impl in this "
                            "file. ")
                         + f"raw_block={_db.block_attr!r} "
                         f"raw_canon={_db.attr_canon!r}")
                spliced_now = 0
                _landed: list[LandedBody] = []
                # Names whose body the splicer refused for its own TEXT, with
                # what the next turn is told (`reply_refusal_feedback`): a
                # failed attempt on those names, not a dropped answer.
                _text_refused: dict[str, str] = {}
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
                # ONE authorization set: the names the prompt told the model
                # are writable are the names the splice lets overwrite. The
                # installable complement (`_installable`, rendered as
                # "writable in this prompt … rewrite it together with the
                # producer") was in the prompt and not in this set, so every
                # installable body the model returned was refused
                # `target_not_stub` — run `3014175-d98ed09e` (2026-09-08,
                # iter 2, `src/error.rs`): three offered, three returned,
                # three refused. In the coordinated case that instruction
                # exists for, the producer would land and its table would
                # not: a tree holding halves of two answers.
                _overwrite_names = _refill_names | frozenset(
                    i.fn_name for i in _installable)
                if _retry_names is not None:
                    _refill_names = frozenset(
                        n for n in _refill_names if n in _retry_names)
                    _overwrite_names = frozenset(
                        n for n in _overwrite_names if n in _retry_names)
                for rb in routing.routed:
                    wc = rb.fn_data["where_clause"]
                    if wc and (rb.via_trait_default
                               or signature_fixed_by_trait(rb.container)):
                        wc = None
                    _fn = rb.fn_data["fn_name"]
                    if _retry_names is not None and _fn not in _retry_names:
                        continue
                    if _fn in withheld:
                        _log(f"  [phase-c/file] {glabel}: `{_fn}` withheld — "
                             "the schedule deferred it as a re-buy this fill; "
                             "its body is not spliced.")
                        continue
                    _fsp = _splice_fn_with_where(
                        file_path, rb.container, _fn,
                        rb.fn_data["body"], wc,
                        allow_nonstub=_fn in _overwrite_names,
                        sig_text=rb.sig_text)
                    ok, reason, _delta = _fsp
                    if ok:
                        spliced_now += 1
                        _landed.append(LandedBody(
                            _fn, rb.container, rb.sig_text,
                            rb.fn_data["body"], wc,
                            overwrite_authorized=_fn in _overwrite_names))
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
                                f"overwrite {'allowed' if _fn in _overwrite_names else 'not authorized'}")
                             + "]")
                        # A body the reader lost is charged to the MEMBER it
                        # was for — a group with one dropped member grades
                        # `dropped` for that member and by its own reading
                        # for the rest. An unauthorized overwrite is a
                        # refusal by contract, not a lost answer; a
                        # duplicate of a body that already landed is not an
                        # answer at all.
                        _ffb = reply_refusal_feedback(
                            _fsp, unit=_fn, file_rel=file_rel)
                        if _ffb is not None and not _dup:
                            _text_refused[_fn] = _ffb
                            _cls(FillOutcomeClass.COMPILE_FAIL,
                                 f"{file_rel}::{_fn}", "file")
                        elif reason != "target_not_stub" and not _dup:
                            _cls(FillOutcomeClass.ANSWER_DROPPED,
                                 f"{file_rel}::{_fn}", "file")
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
                        _rsp = _splice_fn_with_where(
                            file_path, str(_rec.get("container", "")),
                            _p["fn_name"], _p["body"], _p["where_clause"],
                            sig_text=str(_rec.get("sig_text", "")))
                        _ok, _reason, _d = _rsp
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
                            _rfb = reply_refusal_feedback(
                                _rsp, unit=_p["fn_name"], file_rel=file_rel)
                            if _rfb is not None:
                                _text_refused[_p["fn_name"]] = _rfb
                                _cls(FillOutcomeClass.COMPILE_FAIL,
                                     f"{file_rel}::{_p['fn_name']}", "file")
                if spliced_now == 0 and _text_refused:
                    # Nothing reached the file, and the splicer named why for
                    # the reply's own text: a failed attempt on those names,
                    # whose next turn carries the parser's diagnostic — the
                    # outcome every splice route takes (`splice_feedback`).
                    _refused_names = frozenset(_text_refused)
                    if _retry_names is None:
                        # The rest of a first attempt's names are lost as
                        # before; a refused name was answered and charged.
                        attempts.note_lost(
                            FnLocator(file_rel, n) for n in wanted
                            if n not in _refused_names)
                    prior_error = "\n\n".join(
                        _text_refused[n] for n in sorted(_refused_names))
                    _file_kind = LastErrorKind.COMPILE
                    if _attempt < _FILE_FIRST_MAX_ATTEMPTS:
                        _log(f"  [phase-c/file] {glabel}: nothing spliced — "
                             f"{len(_refused_names)} body(ies) refused for "
                             f"their own text ({', '.join(sorted(_refused_names))}); "
                             "retrying those names with the parser's diagnostic "
                             f"(attempt {_attempt + 1}/{_FILE_FIRST_MAX_ATTEMPTS}).")
                        _revert_cls = None
                        _retry_names = _refused_names
                        _retry_note = (
                            "No body from your last reply reached the file. "
                            "The bodies for "
                            + ", ".join(f"`{n}`" for n in sorted(_refused_names))
                            + " were refused for their own text; the "
                            "diagnostic below says why. Rewrite only those.")
                        _pre = capture(file_path, None)
                        snapshot = _pre.text("replace")
                        continue
                    _log(f"  [phase-c/file] {glabel}: nothing spliced — "
                         f"{len(_refused_names)} body(ies) refused for their "
                         "own text on the last attempt; falling back to "
                         "per-block fills.")
                    return
                if spliced_now == 0:
                    if (_retry_names is not None
                            and _attempt < _FILE_FIRST_MAX_ATTEMPTS):
                        _log(f"  [phase-c/file] {glabel}: nothing spliced "
                             f"on peel-retry of {sorted(_retry_names)} — "
                             "retrying the same names (attempts remain).")
                        prior_error = (
                            (prior_error + "\n" if prior_error else "")
                            + "previous retry spliced nothing for: "
                            + ", ".join(sorted(_retry_names)))
                        _pre = capture(file_path, None)
                        snapshot = _pre.text("replace")
                        continue
                    _log(f"  [phase-c/file] {glabel}: nothing spliced — "
                         "falling back to per-block fills.")
                    if _retry_names is None:
                        _cls(FillOutcomeClass.ANSWER_DROPPED, glabel, "file")
                        # Attempt-scoped, as at the LLM-error return above.
                        # A peel-retry that spliced nothing already reverted
                        # those names on the peel; do not also mark them lost.
                        attempts.note_lost(
                            FnLocator(file_rel, n) for n in wanted)
                    return
                _attempted_names = [b.fn_name for b in _landed]
                _peel_reverted: tuple[str, ...] = ()
                _peel_retry = False
                _peel_err = ""
                rc, cargo_out, _diags = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                if rc != 0:
                    _rep = repair_missing_imports(
                        _run_cargo_check_diag, check_cmd, target_src_dir,
                        cluster_env or {}, file_rel, _diags, _log,
                        "[phase-c/file]", validate_parse=_parse_ok)
                    if _rep is not None:
                        rc, cargo_out, _diags = _rep
                _pre_peel_out = cargo_out
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
                        file_path=file_path, pre_image=_pre,
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
                        _peel_reverted = _peel.reverted
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
                # A peel that left compiling remainder used to FILE FILLED
                # return on attempt 1 of 3, so the blamed names never retried
                # with the diagnostic that named them (run `451330-7e8a7ca9`,
                # src/fmt.rs::debug). The retry is DECIDED here but TAKEN only
                # after the gates below judge the kept remainder: retrying
                # before the gates left attempt-1's kept splices on the tree
                # unjudged whenever the retry attempt exited early (LLM error,
                # empty reply, nothing spliced) — compile-checked bodies that
                # no U2 / crash / regression gate ever saw.
                if _peel_reverted:
                    _peel_err = _focus_cargo_errors(_pre_peel_out, file_rel)
                    _codes = sorted(ecodes_in_text(_peel_err))
                    _first_diag = next(
                        (ln.strip() for ln in _peel_err.splitlines()
                         if ln.lstrip().startswith("error[")), "")
                    for _n in sorted(_peel_reverted):
                        _verdict_row(
                            f"{file_rel}::{_n}", Disposition.REVERT,
                            "compile failed",
                            outcome=GateOutcome.NEGATIVE,
                            attempt=_attempt,
                            validity=ValidityVector(binary_completed=False),
                            model_calls=_attempt, route="file",
                            extra={"error_codes": _codes,
                                   "primary": _first_diag})
                    _peel_retry = _attempt < _FILE_FIRST_MAX_ATTEMPTS and rc == 0
                    _emit("phase_c.file.fill.revert", data={
                        "cluster": cluster_name, "file": file_rel,
                        "group": glabel, "attempt": _attempt,
                        "max_attempts": _FILE_FIRST_MAX_ATTEMPTS,
                        "reason": "compile failed",
                        "reverted": sorted(_peel_reverted),
                        "error_codes": _codes,
                        "retrying": _peel_retry,
                        "spliced": spliced_now,
                        "planned": len(stub_names) + len(refill_names),
                        "banked": False})
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
                        if _key in escalated_keys or _key in measured_owner_keys:
                            _file_tests |= _frontier_tests.tests_for(_key)
                        else:
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
                    _joint = judge_refill(_file_tests, _baseline_test, _u2_file)
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
                # ONE non-regression keep, crash evidence or not — the bar
                # the per-impl and pair routes apply too. A crash-evidenced
                # refill used to skip the panic-site check below; it does
                # not any more: an earlier panic is fewer assertions passed
                # whatever named the unit.
                _crash_keep_file = False
                # The non-regression keep: a REJECT whose every
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
                                      and _joint is not None
                                      and _joint.non_regression)
                if _nonregr_keep_file and SameCrashJudge(
                        frozenset(crash_blamed or ()), culprits_of).refuses(
                        output=(_u2_file.capture if _u2_file is not None else ""),
                        aimed=_file_tests,
                        written=[FnLocator(file_rel, b.fn_name) for b in _landed]):
                    _log(f"  [phase-c/file] GUARD {glabel}: re-fill REVERTED — "
                         "every aimed test is still a native crash and the "
                         "crash's culprit(s) it wrote still are.")
                    _nonregr_keep_file = False
                if _nonregr_keep_file:
                    _log(f"  [phase-c/file] GUARD {glabel}: re-fill KEPT on "
                         "non-regression — no self-failure is newly red "
                         "(all were failing at baseline) and no attributed "
                         "test panics earlier than it did"
                         + (" (crash-evidenced)" if _file_crash else "")
                         + "; judged at the next sweep in the joint tree.")
                elif (rc == 0 and _u2_reject
                        and _joint is not None and _joint.refused_on_position):
                    _log(f"  [phase-c/file] GUARD {glabel}: re-fill REVERTED "
                         "— nothing newly red, but "
                         f"{_joint.site_movement}. Same verdict vector, fewer "
                         "assertions reached: the old bodies were closer.")
                # Peel that put names back as stubs without shrinking this
                # file's hollow set: the survivors are rewrites of already-
                # filled bodies. The unmeasurable keep below would land them
                # (baseline binaries already crashed, so the regression gate
                # cannot see the new cost) and the rest of the iteration
                # would run at the dropped score. Run `1160135-7b10cdfa`:
                # two constructors peeled, ten accessors kept, pass 83 → 23.
                _peel_refuse_keep = bool(_peel_reverted) and not peel_keep_allowed(
                    snapshot,
                    file_path.read_text(encoding="utf-8", errors="replace"),
                    _peel_reverted)
                if _peel_refuse_keep:
                    if _crash_keep_file or _nonregr_keep_file:
                        _log(f"  [phase-c/file] {glabel}: peel restored "
                             f"{list(_peel_reverted)}; hollow did not shrink "
                             "— unmeasurable keep refused, reverting to the "
                             "pre-attempt file.")
                    _crash_keep_file = False
                    _nonregr_keep_file = False
                # A body that ignores a declared input is kept only when a
                # test attributed to that body turned green in this keep;
                # without that it is a decline (`domain/ignored_inputs`). Run
                # `486447-5207101c` kept `render`'s `let _ = (lhs, rhs);` here
                # on "compiles, no attributed test observed passing" and did
                # not aim the unit again for four fills. A sibling's test
                # turning green in the same reply vouches only the sibling.
                # The decliners go back to their pre-image, the rest are
                # re-spliced and judged by the regression gate below, and the
                # decliners take the text-refusal retry with the reason as
                # their error.
                if (rc == 0 and not _peel_refuse_keep
                        and (not _u2_reject or _bank is not None
                             or _crash_keep_file or _nonregr_keep_file)):
                    _vouched_f = vouched_for_keep(
                        {(b.fn_name, b.container): _frontier_tests.tests_for_body(
                            file_rel, b.fn_name, b.container) for b in _landed},
                        turned_green(_baseline_test, _u2_file))
                    _ignoring_f = declined_by_ignored_inputs(
                        ((b.fn_name, b.sig_text, b.body, b.container)
                         for b in _landed),
                        vouched=_vouched_f)
                    if _ignoring_f:
                        _keep_f = [b for b in _landed
                                   if not is_declined(
                                       b.fn_name, b.container, _ignoring_f)]
                        restore(_pre)
                        for b in _keep_f:
                            if not _splice_fn_with_where(
                                    file_path, b.container, b.fn_name, b.body,
                                    b.where_clause,
                                    allow_nonstub=b.overwrite_authorized,
                                    sig_text=b.sig_text).ok:
                                restore(_pre)
                                _keep_f = []
                                break
                        _landed = _keep_f
                        spliced_now = len(_landed)
                        _note_reverted(file_rel, tuple(
                            identity_name(n) for n in sorted(_ignoring_f)))
                        for _n, _ign in sorted(_ignoring_f.items()):
                            _shown = identity_name(_n)
                            _text_refused[_n] = ignored_inputs_feedback(
                                _shown, _ign)
                            _cls(FillOutcomeClass.DECLINED,
                                 f"{file_rel}::{_shown}", "file")
                            _verdict_row(
                                f"{file_rel}::{_shown}", Disposition.REVERT,
                                "declined — ignores its input(s) "
                                + ", ".join(_ign)
                                + " and no attributed test was observed passing",
                                outcome=GateOutcome.NEGATIVE,
                                attempt=_attempt,
                                validity=ValidityVector(binary_completed=True),
                                model_calls=_attempt, route="file")
                        _log(f"  [phase-c/declined] file {glabel}: "
                             f"{len(_ignoring_f)} body(ies) ignore declared "
                             "input(s) with no attributed test observed "
                             "passing — not kept: "
                             + ", ".join(
                                 f"{identity_name(n)} ({', '.join(i)})"
                                 for n, i in sorted(_ignoring_f.items()))
                             + (f"; {spliced_now} other body(ies) re-spliced"
                                if spliced_now else ""))
                        _emit("phase_c.fill.declined", data={
                            "cluster": cluster_name, "unit": glabel,
                            "route": "file", "reason": "ignored_inputs",
                            "names": sorted(
                                identity_name(n) for n in _ignoring_f),
                            "ignored": {identity_name(n): list(i) for n, i
                                        in sorted(_ignoring_f.items())}})
                        if not _landed:
                            # Nothing of the reply stands: the same outcome
                            # as a reply refused for its text before it was
                            # spliced, and the same retry.
                            prior_error = "\n\n".join(
                                _text_refused[n] for n in sorted(_ignoring_f))
                            _file_kind = LastErrorKind.COMPILE
                            if _attempt < _FILE_FIRST_MAX_ATTEMPTS:
                                _revert_cls = None
                                _retry_names = frozenset(
                                    identity_name(n) for n in _ignoring_f)
                                _retry_note = (
                                    "No body from your last reply was kept. "
                                    "The bodies for "
                                    + ", ".join(f"`{n}`"
                                                for n in sorted(_ignoring_f))
                                    + " ignore inputs their signature "
                                    "declares; the error below says which. "
                                    "Rewrite only those.")
                                _pre = capture(file_path, None)
                                snapshot = _pre.text("replace")
                                continue
                            _log(f"  [phase-c/file] {glabel}: every body "
                                 "declined on the last attempt; falling back "
                                 "to per-block fills.")
                            return
                _regr_file = (_test_regression_gate(
                    tdd_test_cmd, target_src_dir, cluster_env, _baseline_test,
                    stub_free=stub_free,
                    introduced_stubs=bool(_peel_reverted),
                    hollow_before=_hollow_before)
                    if rc == 0 else None)
                if (rc == 0 and not _peel_refuse_keep
                        and (not _u2_reject or _bank is not None
                             or _crash_keep_file or _nonregr_keep_file)
                        and _regr_file is None):
                    fns_filled_total += spliced_now
                    # This call DESIGNED the file: the same-call refill-only
                    # drop below the file pass keys on this set, so a file
                    # whose call landed nothing keeps the per-block fallback
                    # its early-return log line promises.
                    ff_produced_files.add(file_rel)
                    # The peel survivors, not `_attempted_names`: a peeled
                    # name was restored and stays reverted.
                    _note_kept(file_rel, [b.fn_name for b in _landed])
                    # The reading the route row is graded on; the class
                    # reads the same one below.
                    _cmp_f = (last_comparison() if _regr_file is None
                              and tdd_test_cmd else None)
                    note_kept_comparison(_cmp_f)
                    _record_regression_evidence(
                        _retro_key(file_rel, m.fn_name, b.container)
                        for b in fblocks
                        for m in (*b.stub_members, *b.refill_members))
                    _log(f"  [phase-c/file] {glabel}: FILE FILLED "
                         f"{spliced_now} fn(s) in one pass"
                         + (f" — banked improvement: {_bank.summary}."
                            if _bank is not None else "."))
                    # Trust-but-verify: a splice can report ok while the fn
                    # stays a stub on disk (observed: "FILE FILLED 5" with one
                    # fn still `unimplemented!()`). A silent loss here reads
                    # as "filled" forever — re-scan and name the leftovers.
                    # Peeled names are excluded: they are loudly reverted
                    # (their own verdict rows and the fill.revert event),
                    # not a silent splice loss — and with the retry below
                    # they are about to be re-asked.
                    _left = sorted({
                        str(r.get("fn_name", "")) for r in
                        (_scan_unimplemented_records_for_cluster(
                            target_src_dir, cluster) or [])
                        if _norm_src_rel(r.get("file", ""))
                        == _norm_src_rel(file_rel)
                        and str(r.get("fn_name", "")) in set(stub_names)
                        and str(r.get("fn_name", "")) not in set(_peel_reverted)})
                    if _left:
                        # The still-stubbed members were charged where the
                        # reader lost them (the splice refusal above); the
                        # group is graded by its own reading like any
                        # other keep. Charging the whole group WRONG for
                        # one member printed `cashed=0 wrong=1` beside a
                        # `pass 26 -> 32` reading (run `3014175-d98ed09e`,
                        # 2026-09-08, iter 2).
                        _log(f"  [phase-c/file] {glabel}: POST-FILL MISMATCH "
                             f"— still stubbed after accept: {_left}.")
                        _emit("phase_c.file.fill.mismatch", data={
                            "cluster": cluster_name, "file": file_rel,
                            "group": glabel, "still_stubbed": _left})
                    _passed_f = (frozenset(_bank.resolved) if _bank is not None
                                 else (_u2_file.self_passed
                                       if _u2_file is not None else frozenset()))
                    _attr_f = (
                        (_u2_file.self_passed | _u2_file.self_failures
                         | _u2_file.aborted)
                        if _u2_file is not None else frozenset())
                    # A non-regression keep's tests RAN (its GUARD line says
                    # so): it is judged here like any keep, and deferred
                    # only in the verdict row below.
                    _class_f = class_for_kept(
                        reading=keep_reading(
                            net_delta=(_cmp_f.net_pass_delta
                                       if _cmp_f is not None else 0),
                            newly_green=(_cmp_f.newly_green
                                         if _cmp_f is not None else ()),
                            attributed=_attr_f),
                        passed=_passed_f, unreachable=tests_could_not_run(
                            crash_keep=_crash_keep_file,
                            external_only=(
                                _u2_file is not None
                                and _u2_file.decision
                                is U2Decision.ACCEPT_EXTERNAL_ONLY)),
                        attributed=_attr_f,
                        measured=(_u2_file is not None
                                  and not _u2_file.aborted))
                    _cls(_class_f, glabel, "file")
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
                    _deferred_f = _crash_keep_file or _nonregr_keep_file
                    # POSITIVE is this keep's own payment, the same fact the
                    # class reads: tests that were green before the keep are
                    # observed passing after it and prove nothing about it.
                    _paid_f = (_class_f is FillOutcomeClass.CASHED
                               and bool(_passed_f))
                    _verdict_row(
                        glabel,
                        (Disposition.KEEP_DEFERRED if _deferred_f
                         else Disposition.ACCEPT),
                        ("kept on non-regression — no newly-red self-failure"
                         if _deferred_f
                         else f"accepted — {len(_passed_f)} attributed test(s) "
                         "observed passing" if _paid_f
                         else f"kept — {len(_passed_f)} attributed test(s) "
                         "pass and none moved with this keep" if _passed_f
                         else "kept — compiles, but no attributed test was "
                         "observed passing (none were failing, or the scoped "
                         "run was inconclusive)"),
                        outcome=(GateOutcome.POSITIVE
                                 if _paid_f and not _deferred_f
                                 else GateOutcome.UNMEASURABLE),
                        evidence=tuple(sorted(_passed_f)),
                        attempt=_attempt,
                        validity=ValidityVector(binary_completed=True),
                        model_calls=_attempt, route="file")
                    # Names whose body was refused for its own text are owed
                    # the same retry as a peeled name: the kept remainder is
                    # judged, and the next turn carries the parser's
                    # diagnostic for exactly those names.
                    _text_retry = frozenset(
                        n for n in _text_refused
                        if n not in {b.fn_name for b in _landed})
                    if _text_retry and _attempt >= _FILE_FIRST_MAX_ATTEMPTS:
                        _text_retry = frozenset()
                    if _peel_retry or _text_retry:
                        # The kept remainder is judged and banked; NOW retry
                        # the peeled names with the diagnostic that blamed
                        # them. The re-read snapshot carries the accepted
                        # bodies, so a later whole-file revert restores this
                        # judged state, never the pre-fill one.
                        _peeled = (frozenset(_peel_reverted) if _peel_retry
                                   else frozenset())
                        _log(f"  [phase-c/file] {glabel}: retrying "
                             + (f"the {len(_peeled)} peeled name(s) with the "
                                "pre-peel diagnostic" if _peeled else "")
                             + (" and " if _peeled and _text_retry else "")
                             + (f"{len(_text_retry)} name(s) refused for "
                                "their own text with the parser's diagnostic"
                                if _text_retry else "")
                             + " (kept remainder judged and kept).")
                        prior_error = "\n\n".join(
                            x for x in (
                                _peel_err if _peeled else "",
                                *(_text_refused[n] for n in sorted(_text_retry)))
                            if x)
                        _revert_cls = None
                        _retry_names = _peeled | _text_retry
                        _kept_names = sorted(
                            {b.fn_name for b in _landed} - _retry_names)
                        _retry_note = (
                            "Your bodies for "
                            + ", ".join(f"`{n}`" for n in _kept_names)
                            + " compiled and were KEPT."
                            + (" The bodies for "
                               + ", ".join(f"`{n}`" for n in sorted(_peeled))
                               + " were peeled back: the diagnostic below "
                               "blamed them." if _peeled else "")
                            + (" The bodies for "
                               + ", ".join(f"`{n}`" for n in sorted(_text_retry))
                               + " were refused for their own text before "
                               "reaching the file: the diagnostic below says "
                               "why." if _text_retry else "")
                            + " Rewrite only those."
                            if _kept_names else "")
                        _pre = capture(file_path, None)
                        snapshot = _pre.text("replace")
                        continue
                    if _retry_names is not None:
                        _landed_names = {b.fn_name for b in _landed}
                        _unspliced = frozenset(
                            n for n in _retry_names if n not in _landed_names)
                        if (_unspliced
                                and _attempt < _FILE_FIRST_MAX_ATTEMPTS):
                            _log(f"  [phase-c/file] {glabel}: peel-retry "
                                 f"left {len(_unspliced)} name(s) unspliced "
                                 f"({', '.join(sorted(_unspliced))}) — "
                                 "retrying those names; judged remainder stays.")
                            _revert_cls = None
                            _retry_names = _unspliced
                            _retry_note = (
                                "These names were not spliced from your last "
                                "reply (no body for them reached the file): "
                                + ", ".join(f"`{n}`" for n in sorted(_unspliced))
                                + ". The rest of that reply is kept.")
                            _pre = capture(file_path, None)
                            snapshot = _pre.text("replace")
                            continue
                    return
                if _regr_file is not None:
                    # The bodies that turned a test green, kept off the tree
                    # the restore puts back, for the joint ask.
                    attempts.remember_bodies({
                        f"{file_rel}::{b.fn_name}": b.body
                        for b in _landed if b.body})
                restore(_pre)
                # The whole reply is restored: the peel did not narrow the
                # failure to its bodies, so no one of them is convicted by it.
                _note_reverted(file_rel, _attempted_names, collateral=True)
                _retry_note = ""
                if _regr_file is not None:
                    _revert_reason = f"regressed sibling test(s): {_regr_file[0].summary}"
                    prior_error = _regr_file[1]
                    _file_kind = regression_detail_kind(_regr_file[0])
                elif _peel_refuse_keep:
                    _revert_reason = (
                        "peel restored stub(s) and this file's hollow set "
                        "did not shrink")
                    prior_error = ""
                    _file_kind = None
                elif rc == 0:
                    _revert_reason = "U2 test failure (no strict improvement)"
                    prior_error = _u2_file.detail if _u2_file is not None else ""
                    _file_kind = LastErrorKind.REDDENED
                else:
                    _revert_reason = "compile failed"
                    prior_error = _focus_cargo_errors(cargo_out, file_rel)
                    _file_kind = LastErrorKind.COMPILE
                if _text_refused:
                    # The reverted reply also had bodies the splicer refused
                    # for their own text; the whole-group retry is told why.
                    prior_error = "\n\n".join(
                        x for x in (prior_error, *(
                            _text_refused[n] for n in sorted(_text_refused)))
                        if x)
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
                _revert_cls = (FillOutcomeClass.COMPILE_FAIL if rc != 0
                               else FillOutcomeClass.WRONG_BODY)
                if not _more:
                    _cls(_revert_cls, glabel, "file")

    def _fill_one_block(block: ImplBlock,
                        withheld: frozenset[str] = frozenset()) -> None:
        """One impl block's fill: context assembly, the `_attempt_impl_fill`
        unit on `drive_fix_loop` (replaces the hand-rolled
        while loop), and exhausted-block bookkeeping (retro + fail emit)."""
        nonlocal fns_filled_total, impls_filled, impls_failed
        block_id = _impl_block_id(block.file, block.container)
        _pull_file.file = block.file
        # Per-file converge keying: only the converge module keeps full budget.
        block_max_stalls = _effective_stalls(_impl_full_stalls, _module_of(block.file))
        file_path = target_src_dir / block.file
        if not file_path.exists():
            return
        # Crash evidence keyed to this block's members (faulting/origin
        # stacks from a localized binary crash) — rendered into the prompt
        # and an override for the exhaustion skip: a localized crash is
        # evidence no prior attempt ever saw.
        # This block's members, at this block's container. A same-named
        # member of another impl shares the locator and does not share
        # the text a frame resolved to this container.
        _at_crash = crash_sites or CrashSites()
        _crash_txts = tuple(dict.fromkeys(
            t for name in dict.fromkeys(
                m.fn_name for m in (*block.stub_members, *block.refill_members))
            if (t := _at_crash.evidence_for(
                crash_evidence or {}, block.file, name, block.container))))
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
        # The record was earned from a crash/abort the tree's still-stubbed
        # fns decided (`crash_earned_conviction`) — judged before the
        # premise rules, which flip while the body is a stub and back the
        # moment the gate keeps the correct body.
        elif crash_earned_conviction(_retro_rec):
            _poisoned_retro = True
            _log(f"  [phase-c/impl] {block_id}: retro guidance was earned "
                 "from a crash/abort the tree's still-stubbed fn(s) decided "
                 "— guidance withheld, exhaustion not honoured "
                 "(crash_earned).")
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
        elif tree_premise_absent(_retro_rec):
            _poisoned_retro = True
            _log(f"  [phase-c/impl] {block_id}: retro guidance recorded no "
                 "tree premise — guidance withheld, exhaustion not honoured "
                 "(no_tree_premise).")
        # The premise's complement: the stamped convicting tests now
        # measurably enter different bodies, so the lesson (and the
        # attempts its exhaustion counted) judged a superseded execution.
        elif convictor_entry_moved(_retro_rec):
            _poisoned_retro = True
            _log(f"  [phase-c/impl] {block_id}: retro guidance was earned "
                 "when its convicting tests entered a different set of "
                 "bodies — guidance withheld, exhaustion not honoured "
                 "(convictor_entry_moved).")
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
        # Refill-only: no stub to write, and no test uniquely convicts THIS
        # container. The four `from` impls share `src/error.rs::from` on the
        # frontier key; Debug asserts and a panic inside
        # `construct_from_boxed` are not this coercion's to fix (run
        # `463713-b7c666c8`, 2026-08-28/29). Skip before the GPU call.
        if not block.stub_members and block.refill_members:
            _owned = tuple(
                m for m in block.refill_members
                if convicting_tests_for_member(
                    _frontier_tests, block.file, m.fn_name, block.container,
                    _homonym_map.get((block.file, m.fn_name),
                                     (block.container,))))
            if not _owned:
                _log(f"  [phase-c/impl] {block_id}: skipped (refill-only; "
                     "no test uniquely convicts this container — "
                     "mis-targeted re-buy).")
                _emit("phase_c.impl.refill_skipped", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "members": [m.fn_name for m in block.refill_members]})
                _cls(FillOutcomeClass.MIS_TARGETED, block_id, "impl")
                return
            if len(_owned) < len(block.refill_members):
                block = replace(block, refill_members=_owned)
            # The same refusal on MEASURED evidence — the gate the per-fn
            # route already has twice (`fill_one_fn`, the pre-loop skip and
            # the retro arm) and this route lacked: on run `358833-aa15d93c`
            # (2026-09-01) the `src/fmt.rs::ErrorImpl` block ground four
            # `E0616`/`E0599` rounds plus a maximal last-resort while the
            # sweep's own owner map proved neither convicting test ever
            # enters `debug` — the `{:?}` arm of the dispatcher never
            # delegates here. When EVERY refill member is positively
            # unentered by every convicting test, no body written in this
            # block can move those verdicts: defer to the outer loop before
            # the first model call. `unreached_by` declines on an unbound
            # reading, an unmeasured test or a crashed binary, so ignorance
            # changes nothing.
            _bc_tests = _convicting_for_block(block)
            if _bc_tests and all(
                    coverage_unreached_by(
                        FnLocator(block.file, m.fn_name), _bc_tests)
                    for m in block.refill_members):
                _names = ", ".join(sorted(_bc_tests))
                _log(f"  [phase-c/impl] {block_id}: skipped (refill-only; "
                     "no convicting test executes any member of this block "
                     f"— {_names} enter(s) the crate elsewhere; deferred to "
                     "outer loop).")
                _emit("phase_c.impl.coverage_unreached", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "members": [m.fn_name for m in block.refill_members],
                    "convicting": sorted(_bc_tests)})
                _cls(FillOutcomeClass.MIS_TARGETED, block_id, "impl")
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
        # A real per-block attempt supersedes a lost file-first answer for
        # the same unit — the released fallback running is exactly what
        # makes the unit attempted after all (the dispatch stamp clears it).
        attempts.note_dispatched(
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
        # the SAME FILE should splice there — an already-filled target whose
        # body is unchanged is a no-op splice via the freshness gate — rather
        # than being dropped, even when that container isn't a formal
        # trait/family match. Live evidence: `impl Error`/`impl ErrorImpl` in
        # anyhow's error.rs are tightly coupled (one wraps the other) but
        # share no trait, so neither `related_containers` nor the
        # dispatch-family heuristic ever pairs them — the model still answers
        # both together (it can see the whole file), and every such body was
        # silently dropped. Kept SEPARATE from `related_blocks` (the curated
        # PROMPT context) so widening the ROUTING match set doesn't bloat
        # every per-block prompt with the rest of the file's unrelated impls.
        _route_candidates = list(related_blocks)
        _route_seen = {_impl_block_id(rb.file, rb.container) for rb in _route_candidates}
        _route_seen.add(_impl_block_id(block.file, block.container))
        for b2 in blocks:
            _bid2 = _impl_block_id(b2.file, b2.container)
            if b2.file == block.file and _bid2 not in _route_seen:
                _route_candidates.append(b2)
                _route_seen.add(_bid2)
        # The block's byte pre-image: the text view feeds the prompts, the
        # bytes are what every revert of this block restores.
        _block_pre = capture(file_path, None)
        snapshot = _block_pre.text()
        # LAST_ERROR window is recomputed from the tree cargo just compiled
        # (inside each attempt), not snapshotted here: a splice that grows a
        # body shifts siblings past a frozen `line_hi`, and a header /
        # non-target member of this block is already outside a fill-target
        # byte window on attempt 1 (run `463713-b7c666c8`).
        # Per-block context.
        stub_fn_names = [m.fn_name for m in block.stub_members]
        # Refill members need their OWN failing test(s) and
        # blueprint/notes context surfaced too, not just the stubs'.
        refill_fn_names = [m.fn_name for m in block.refill_members]
        _fill_target_names = stub_fn_names + refill_fn_names
        # What the record supports for each re-fill member — DIRECT vs
        # INFERRED, the attributed tests, same-named candidates in the file,
        # the tests' own failure text, their SOURCE (attributed-first, then
        # name-match, capped by distinct file) and the pins — the ONE
        # assembly every block-serving route calls (`block_evidence`), so a
        # route cannot be added with a subset of the evidence (the pair
        # route shipped with one of these seven calls and its prompt was
        # blind — run `537093-ffcfd139`, 2026-09-01).
        _block_ev = assemble_block_evidence(
            (block,), blocks, _frontier_tests,
            find_tests=lambda leaf, mx: _find_tests_referencing_fn(
                tests_dir_files, leaf, max_blocks=mx),
            escalated=escalated_keys,
            measured_owner=measured_owner_keys,
            crashed=crash_blamed or frozenset(),
            aimed=frozenset(
                loc for loc in (crash_evidence or {})
                if loc not in (crash_blamed or frozenset())),
            crash_sites=crash_sites,
            read_source=_read_source,
            ledger_entries=ledger_entries, oracle_fp=oracle_fp,
            tuple_structs=_tuple_structs,
            prior_regressed=tuple(
                str(n) for n in (_retro_rec.get("regressed") or [])),
            prior_regressed_output={
                str(k): str(v) for k, v in dict(
                    _retro_rec.get(REGRESSED_OUTPUT) or {}).items()},
            prior_regressed_attempt=regressed_attempt_of(_retro_rec),
            leaf_cap=_ATTRIBUTED_TEST_LEAF_CAP,
            file_cap=_ATTRIBUTED_TEST_FILE_CAP)
        _inferred_refill = _block_ev.inferred_names
        _crashed_refill = _block_ev.crashed_names
        _aimed_refill = _block_ev.aimed_names
        test_blocks = list(_block_ev.test_blocks)
        _pins = _block_ev.pins
        _refill_evidence_text = _block_ev.refill_evidence
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
        # This block's fix-loop token (`FIX_LOOP_SCOPE`): stamped on every
        # retro record the block's loop writes and passed to every screen it
        # runs, so the loop's own fresh lesson is judged with the premise it
        # has here and no later loop inherits it.
        _block_loop_scope = (f"{block.file}::*impl*::{block.container}:"
                             f"{uuid.uuid4().hex[:12]}")
        _prior_key = _retro_key(block.file, "*impl*", block.container)
        _prior_rec = retros_cache.get(_prior_key, {})
        _prior_verdict = screen_guidance(
            _prior_rec, snapshot, evidence=_block_answers,
            anchor_types=anchor_types_for_container(block.container),
            cluster=cluster_name, cfg_dead=_cfg_dead_fns(block.file),
            unit=screening_unit(block.file, "*impl*"),
            pins=_pins, observables=_block_ev.observables,
            loop_scope=_block_loop_scope)
        prior_retro = _prior_verdict.text
        _declined_parts = [
            _dg for _m in (*block.stub_members, *block.refill_members)
            if (_dg := declined_guidance(
                _m.fn_name,
                retros_cache.get(_retro_key(block.file, _m.fn_name,
                                            block.container), {})))]
        if _declined_parts:
            prior_retro = "\n".join(
                p for p in (prior_retro, *_declined_parts) if p)
        # The line carries the STORE key so the grep lands on the record.
        if (_hline := honoured_guidance_line(
                _prior_key, _prior_rec, _prior_verdict.text)):
            _log(f"  {_hline}")
        if (_rline := retired_guidance_line(
                _prior_key, _prior_rec, _prior_verdict)):
            _log(f"  {_rline}")
        notes_section = ""
        # Computed OUTSIDE the notes branch: the escalated set also decides
        # the completion policy of the ask below (`followup_think`), which
        # must not depend on whether an FR file exists.
        _esc_targets = _block_ev.escalated_names
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
                hidden_symbols=hidden_symbols_from_types(
                    _crate_type_records, block.file),
            )
        last_error_excerpt = ""
        # What produced that excerpt, decided where the failure is MEASURED
        # and carried to the prompt as data. The builder used to recover it
        # by grepping the rendered prose, and the substring it looked for
        # was not the one the renderer writes, so a body that compiled and
        # aborted reached the model as "produced these errors — do not
        # resubmit the same approach" (run `681204-b695ab3f`, 2026-09-14).
        last_error_kind = LastErrorKind.UNKNOWN
        # The last failed attempt's REAL two-tier metric. Re-deriving it from
        # `last_error_excerpt` scores a compiled-but-red block as 0 errors —
        # test output carries no rustc E-codes — and 0 never beats a prior 0,
        # so a block was parked the round after it started compiling.
        last_metric: FixProgress | None = None
        # True while the LAST judged attempt's splice was reverted by the
        # regression gate — a path that banks no salvage, so its metric
        # describes a file image the tree permanently discarded. The retro
        # comparison below must not move the floor on it: run
        # `358833-aa15d93c` (2026-09-01) had the maximal last-resort compile
        # (0 errors), get REVERTED for reddening 5 passing Debug tests, and
        # still grade `improved` against a prior floor of 1 — `new_best`
        # moved to a state the tree does not hold and the loop re-entered
        # to buy `E0616` again. A compile-fail or U2 revert stays OUT of
        # this flag: the first is the classic attempt-trajectory floor, the
        # second banks its compiling image as salvage, so both describe
        # something the loop can retain.
        _last_regr_reverted: list[bool] = [False]
        # When the LAST judged attempt died — a binary that completed at the
        # baseline crashed under it, or the scoped run aborted — the
        # stub-panic sites the gate located in the crashed binaries' own
        # output (`None` while the last failure was not a crash; empty when
        # it was and no stub panic was located, i.e. a segfault). The retro
        # writer stamps the record with them (`crash_stub_sites`) and never
        # exhausts the block on an abort the tree's stubs decided: the live
        # `src/ptr.rs::*impl*::Own<T>` record was earned from exactly that
        # crash (run `2585062-494e630e`, 2026-09-08).
        _last_crash_sites: list[frozenset[tuple[str, int]] | None] = [None]
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
        # Keep-while-red: baseline = the crate's error signature with THIS block's
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

        # The block's one conversation: attempt 1 opens it with the per-impl
        # prompt; every later attempt — a retry after a gate, a retro round's
        # re-entry, the maximal last resort — is a turn on it that carries
        # what was measured, with the model's own replies above.
        _conv = _open_conversation(str(block_id), "impl")
        _impl_output = fn_output(
            [(n, block.container) for n in _block_target_names])

        def _attempt_impl_fill(fix_attempt: int) -> FixOutcome:
            nonlocal last_response
            attempts.note_call()
            # Default this attempt's loss to the model's. Only the two
            # positively-identified pipeline-side losses below overwrite it, so
            # the escalation stays available exactly where it was before.
            _last_loss[:] = [AttemptLoss.MODEL_FAILURE]
            # The opening turn's TYPES_IN_SCOPE: the member sigs + impl header
            # + where clause, and the types the file names that another file
            # declares. A retry adds only the types its result names that the
            # conversation has not shown (`types_new_to`); E0277/E0599/E0046/
            # E0053 routinely name a trait the signatures don't mention.
            # The whole file as the cluster compiles it, read when the
            # conversation opens: the prompt's own-file view. Bodies other
            # fills rewrite later reach the retry turn as `moved`.
            _own_text = ""
            _types_section_eff = ""
            if not _conv.opened:
                _own_text = _cluster_view(file_path.read_text(
                    encoding="utf-8", errors="replace"))
                _types_section_eff = _types_section_for_fix(
                    _member_sigs_text, "", last_error_excerpt or "",
                    _block_container, _block_where, _crate_type_records,
                    file_rel=block.file, observables=_block_ev.observables,
                    file_text=_own_text)
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
            prompt = "" if _conv.opened else _build_per_impl_prompt(
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
                    # An E0616/E0599 additionally carries the blamed type's
                    # crate-visible fn inventory — the surface the error
                    # implies the model is missing (`with_blamed_type_api`).
                    last_error=with_blamed_type_api(
                        last_error_excerpt or "", _crate_type_records),
                    last_error_kind=last_error_kind,
                    bound_methods=_bound_methods_section_for(
                        _block_where, _crate_type_records, file_rel=block.file),
                    refill_evidence=_refill_evidence_text,
                    inferred_refill_names=_inferred_refill,
                    crashed_refill_names=_crashed_refill,
                    aimed_refill_names=_aimed_refill,
                    sibling_rejected=_steer.shared,
                    behaviour_must=_behaviour_must(
                        *block.stub_members, *block.refill_members),
                    own_file=_own_text),
                fr_path=fr_path,
            )
            _think = escalated_followup_think(_esc_targets)
            try:
                with serving_units(f"{block.file}::{n}"
                                   for n in _block_target_names):
                    if not _conv.opened:
                        response = _conv.open(
                            prompt, has_deliverable=_has_fn_bodies,
                            followup_think=_think)
                    else:
                        response = _conv.retry(AttemptFeedback(
                            kind=last_error_kind,
                            excerpt=with_blamed_type_api(
                                last_error_excerpt or "", _crate_type_records),
                            targets=tuple(_block_target_names),
                            output=_impl_output,
                            note=(sibling_rejected_note(_steer.shared)
                                  if _steer.contradicts else ""),
                            sections=(
                                EvidenceSection(
                                    "TYPES_IN_SCOPE", types_new_to(
                                        _conv.text(), last_error_excerpt or "",
                                        _crate_type_records,
                                        viewer_file=block.file),
                                    "Types this result names that were not "
                                    "shown above:\n"),
                                EvidenceSection(
                                    "RETROSPECTIVE_GUIDANCE", prior_retro,
                                    "A diagnosis of the attempts so far:\n")),
                            moved=_conv.bodies_moved(
                                _read_source, frozenset(_block_target_names),
                                normalize=_cluster_view)),
                            has_deliverable=_has_fn_bodies,
                            followup_think=_think)
            except TranscriptBound:
                _last_loss[:] = [AttemptLoss.TRANSCRIPT_BOUND]
                return FixOutcome("stop")
            except Exception as e:
                _log(f"  [phase-c/impl] {block_id}: LLM error: {e}")
                return FixOutcome("stop")
            last_response = response
            parsed = bind_headerless_names(
                extract_fn_bodies(response), _block_target_names)
            _audit = _audit_reply(_log, _emit, cluster=cluster_name,
                                  unit=str(block_id), route="impl",
                                  reply=response, parsed=parsed)
            if _audit is not None and _audit.empty:
                _note_declined_units(
                    block.file, _audit.empty, lambda _n: block.container,
                    response_sha=_audit.response_sha, route="impl")
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
                members_filled_this_block, block_already_complete, \
                last_metric, last_error_kind
            _last_regr_reverted[0] = False
            _last_crash_sites[0] = None
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
                _baseline_test = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env)
                _hollow_before = hollow_count(snapshot_src_tree(target_src_dir))
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
                # (name, signature, body, container) of every body this
                # attempt landed, for the keep's ignored-input decline.
                _impl_landed: list[tuple[str, str, str, str]] = []
                refusals: list[str] = []
                # What each refusal that judges the reply's TEXT tells the
                # next turn (`splice_feedback.reply_refusal_feedback`).
                _reply_feedback: list[str] = []
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
                    if fn_data["fn_name"] in withheld:
                        # A carved run (`narrow_blocks_to`): the schedule
                        # deferred this member as a re-buy this fill.
                        _log(f"  [phase-c/impl] {block_id}: "
                             f"`{fn_data['fn_name']}` withheld — the schedule "
                             "deferred it as a re-buy this fill; its body is "
                             "not spliced.")
                        continue
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
                    _sp = _splice_fn_with_where(
                        target_file, container, fn_data["fn_name"],
                        fn_data["body"], wc,
                        allow_nonstub=fn_data["fn_name"] in _refill_names,
                        sig_text=_rb.sig_text,
                    )
                    ok, reason, delta = _sp
                    if ok:
                        spliced_now += 1
                        _impl_spliced.append(fn_data["fn_name"])
                        _impl_landed.append((fn_data["fn_name"], _rb.sig_text,
                                             str(fn_data["body"]), container))
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
                        _fb = reply_refusal_feedback(
                            _sp, unit=fn_data["fn_name"], file_rel=block.file)
                        if _fb is not None:
                            _reply_feedback.append(_fb)
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
                    if not (_reply_feedback
                            and len(_reply_feedback) == len(refusals)
                            and not routing.dropped):
                        return FixOutcome("stop")
                    # Every body was refused for its own text: a failed
                    # attempt the next turn can repair, not a lost answer.
                    # Nothing was written, so the refusals' diagnostics are
                    # this attempt's compile verdict.
                    _last_loss[:] = [AttemptLoss.MODEL_FAILURE]
                    _log(f"  [phase-c/impl] {block_id}: every body was refused "
                         "for its own text — charged as a failed attempt; the "
                         "next turn carries the diagnostics.")
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
                if spliced_now == 0:
                    _no_diags: list[Any] = []
                    rc, cargo_out, diagnostics = (
                        101, "\n\n".join(_reply_feedback), _no_diags)
                else:
                    rc, cargo_out, diagnostics = _run_cargo_check_diag(
                        check_cmd, target_src_dir, cluster_env)
                if rc != 0 and spliced_now:
                    # Deterministic repair: a body cannot add `use` items, so
                    # apply rustc's own single-candidate import suggestions to
                    # the block's file and re-check before charging the model.
                    _rep = repair_missing_imports(
                        _run_cargo_check_diag, check_cmd, target_src_dir,
                        cluster_env or {}, block.file, diagnostics, _log,
                        "[phase-c/impl]", validate_parse=_parse_ok)
                    if _rep is not None:
                        rc, cargo_out, diagnostics = _rep
                _last_full_out[0] = cargo_out  # `full_error` pull source
                _u2_impl: U2Verdict | None = None
                if rc == 0:
                    # Shared gate: accept the block only if its
                    # members' own tests pass too — not just compile.
                    # Conviction-scoped (see the file-first gate above): only
                    # tests with direct evidence at a member may revert the block.
                    _block_tests: set[str] = set()
                    for _bm in (*block.stub_members, *block.refill_members):
                        _lk = FnLocator(block.file, _bm.fn_name)
                        if _lk in escalated_keys or _lk in measured_owner_keys:
                            _block_tests |= _frontier_tests.tests_for(_lk)
                        else:
                            _block_tests |= _frontier_tests.convicting_for(_lk)
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
                _joint_impl: JointImprovement | None = None
                if (_u2_impl_reject and _u2_impl is not None
                        and _baseline_test is not None):
                    _jb = judge_refill(_block_tests, _baseline_test, _u2_impl)
                    _joint_impl = _jb
                    _bank_impl = _jb if _jb.banks else None
                    if _jb.site_movement:
                        _log(f"  [phase-c/impl] {block_id}: panic sites moved "
                             f"— {_jb.site_movement}")
                # Global monotone ratchet: a compiled fill is rejected if it
                # reddened ANY previously-passing cluster test — even when the
                # block's own U2 tests pass (or it would be kept while red). This is
                # the guard the pass-count oscillation was slipping through:
                # both accept paths below verified compile/own-tests only. When
                # rc==0 the crate compiles clean, so the keep-while-red compile-signature is
                # empty and one of the two paths WILL accept — hence the check
                # runs exactly once per green attempt (never wasted). Red
                # attempts skip it (tests can't run; deferred to the TDD loop).
                _regr = (_test_regression_gate(
                    tdd_test_cmd, target_src_dir, cluster_env, _baseline_test,
                    stub_free=stub_free, hollow_before=_hollow_before)
                    if rc == 0 else None)
                if _regr is not None:
                    _cmp, _regr_detail = _regr
                    restore(_block_pre)
                    _last_regr_reverted[0] = True
                    _note_reverted(block.file, _impl_spliced, collateral=True)
                    _log(f"  [phase-c/impl] {block_id}: REVERTED {spliced_now} "
                         f"fn(s) — regressed sibling test(s): "
                         f"{_cmp.summary} (attempt {fix_attempt}).")
                    last_error_excerpt = _regr_detail
                    # The gate runs the tests only after a clean compile,
                    # so this branch is always a body rustc ACCEPTED.
                    last_error_kind = regression_detail_kind(_cmp)
                    if _cmp.newly_crashed:
                        _last_crash_sites[0] = _cmp.crash_stub_sites
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
                # The same bar as the file route's non-regression keep: a
                # compiling landed set whose attributed tests still fail is
                # KEPT (deferred to the sweep) when none of them was passing
                # at baseline — crash evidence or not. It used to require
                # crash evidence here, so the same body was kept by the file
                # route and reverted by this one.
                # `non_regression` is the file route's bar exactly, panic
                # sites included (`judge_refill`): an attributed test that
                # now fails at an earlier line reverts here too.
                _crash_keep = (
                    rc == 0 and _u2_impl_reject
                    and _joint_impl is not None
                    and _joint_impl.non_regression)
                if (rc == 0 and _u2_impl_reject and _joint_impl is not None
                        and _joint_impl.refused_on_position):
                    _log(f"  [phase-c/impl] GUARD {block_id}: re-fill REVERTED "
                         "— nothing newly red, but "
                         f"{_joint_impl.site_movement}. Same verdict vector, "
                         "fewer assertions reached: the old bodies were closer.")
                if _crash_keep and SameCrashJudge(
                        frozenset(crash_blamed or ()), culprits_of).refuses(
                        output=(_u2_impl.capture if _u2_impl is not None else ""),
                        aimed=_block_tests,
                        written=[FnLocator(block.file, m.fn_name)
                                 for m in (*block.stub_members,
                                           *block.refill_members)]):
                    _log(f"  [phase-c/impl] GUARD {block_id}: re-fill REVERTED "
                         "— every aimed test is still a native crash and the "
                         "crash's culprit(s) it wrote still are.")
                    _crash_keep = False
                if _crash_keep and _u2_impl is not None:
                    _log(f"  [phase-c/impl] GUARD {block_id}: re-fill KEPT on "
                         "non-regression "
                         f"({len(_u2_impl.self_failures)} self-failure(s), "
                         "none passing at baseline"
                         + (", crash-evidenced" if _block_crash_evidence else "")
                         + ") — judged for real at the next sweep, in the "
                         "joint tree.")
                # A kept body that ignores a declared input needs a test
                # attributed to that body turned green by this keep
                # (`domain/ignored_inputs`); without one the attempt is a
                # decline and the next one is told which inputs it dropped.
                _ignoring_i = (declined_by_ignored_inputs(
                    _impl_landed, vouched=vouched_for_keep(
                        {(_n, _c): _frontier_tests.tests_for_body(
                            block.file, _n, _c)
                         for _n, _sig, _body, _c in _impl_landed},
                        turned_green(_baseline_test, _u2_impl)))
                    if rc == 0 and (not _u2_impl_reject
                                    or _bank_impl is not None or _crash_keep)
                    else {})
                if _ignoring_i:
                    restore(_block_pre)
                    _note_reverted(block.file, _impl_spliced, collateral=True)
                    for _n in sorted(_ignoring_i):
                        _cls(FillOutcomeClass.DECLINED,
                             f"{block.file}::{identity_name(_n)}", "impl")
                    last_error_excerpt = "\n\n".join(
                        ignored_inputs_feedback(identity_name(_n), _ign)
                        for _n, _ign in sorted(_ignoring_i.items()))
                    last_error_kind = LastErrorKind.COMPILE
                    _log(f"  [phase-c/declined] impl {block_id}: "
                         f"{len(_ignoring_i)} body(ies) ignore declared "
                         "input(s) with no attributed test observed passing "
                         "— attempt reverted: "
                         + ", ".join(
                             f"{identity_name(n)} ({', '.join(i)})" for n, i
                             in sorted(_ignoring_i.items())))
                    _emit("phase_c.fill.declined", data={
                        "cluster": cluster_name, "unit": block_id,
                        "route": "impl", "reason": "ignored_inputs",
                        "names": sorted(identity_name(n) for n in _ignoring_i),
                        "ignored": {identity_name(n): list(i) for n, i
                                    in sorted(_ignoring_i.items())}})
                    _verdict_row(
                        block_id, Disposition.REVERT,
                        "declined — ignores declared input(s) and no "
                        "attributed test was observed passing",
                        outcome=GateOutcome.NEGATIVE,
                        attempt=fix_attempt,
                        validity=ValidityVector(binary_completed=True),
                        model_calls=fix_attempt)
                    _isig = frozenset(
                        f"ignored_inputs:{n}:{','.join(i)}"
                        for n, i in _ignoring_i.items())
                    return FixOutcome("failed", err_sig=_isig,
                                      metric=FixProgress(0, len(_isig)))
                if rc == 0 and (not _u2_impl_reject or _bank_impl is not None
                                or _crash_keep):
                    _note_kept(block.file, _impl_spliced)
                    _cmp_i = last_comparison() if tdd_test_cmd else None
                    note_kept_comparison(_cmp_i)
                    _record_regression_evidence(
                        [_retro_key(block.file, "*impl*", block.container),
                         *(_retro_key(block.file, m.fn_name, block.container)
                           for m in (*block.stub_members, *block.refill_members))])
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
                    _attr_i = (
                        (_u2_impl.self_passed | _u2_impl.self_failures
                         | _u2_impl.aborted)
                        if _u2_impl is not None else _block_answers.tests)
                    _cls(class_for_kept(reading=keep_reading(
                                            net_delta=(
                                                _cmp_i.net_pass_delta
                                                if _cmp_i is not None else 0),
                                            newly_green=(
                                                _cmp_i.newly_green
                                                if _cmp_i is not None else ()),
                                            attributed=_attr_i),
                                        passed=_passed_i,
                                        unreachable=tests_could_not_run(
                                            crash_keep=_crash_keep,
                                            external_only=_u2_external_only),
                                        attributed=_attr_i,
                                        measured=(_u2_impl is not None
                                                  and not _u2_impl.aborted)),
                         block_id, "impl")
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
                # Keep-while-red: keep a NON-REGRESSING fill even when the crate
                # doesn't fully compile — it adds no error beyond baseline and
                # none on the filled fns, so it's a correct prerequisite of the
                # interdependent web, not a failure to discard. This dissolves
                # the convergence deadlock where every correct piece is reverted
                # for not single-handedly making rc==0.
                # Keep-while-red keeps ONLY while the crate is RED (rc!=0),
                # its actual purpose — accumulating prerequisites of a web that
                # can't yet compile. When rc==0 the crate compiles and tests CAN
                # run, so reaching here means U2 FAILED (own tests red): keeping a
                # green-but-behaviorally-wrong body just masks it as "filled".
                # Fall through to revert (→ stub) so the per-fn behavioral re-fill
                # retries it cleanly with the failing test.
                # (`spliced_now`: an attempt whose every body was refused for
                # its text wrote nothing — there is nothing to keep, and its
                # "red crate" is the refusal's diagnostic, not rustc's.)
                if _b3_keep and rc != 0 and spliced_now:
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
                        # Keep-while-red keeps land while the crate is RED, so no test ran
                        # and no site can be blamed. Recorded as landed with no
                        # blocker, which makes the payoff verdict conservative:
                        # one such block is enough to withhold "payoff zero".
                        block_outcomes.append(UnitOutcome(landed=True))
                        _log(f"  [phase-c/impl] {block_id}: KEPT {spliced_now} "
                             f"fn(s) (B3 non-regressing; {len(_cur_sig)} crate "
                             f"err(s) remain elsewhere) (attempt {fix_attempt}).")
                        _cls(class_for_kept(
                            reading=keep_reading(), passed=frozenset(),
                            unreachable=tests_could_not_run(crate_red=True)),
                             block_id, "impl")
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
                    if _u2_impl.aborted:
                        _last_crash_sites[0] = _u2_impl.external_stub_sites
                # Compile failed (or U2 self-test failure). Structured diagnostics
                # give the error signature; a U2 test failure uses the
                # failing-test set (test output has no rustc E-codes).
                # rc != 0 establishes a minimum count of 1.
                cur_err_sig = (_u2_impl.self_failures
                               if _u2_impl_reject and _u2_impl is not None
                               # No rustc ran on a refused reply; the refusal
                               # reasons are the signature the stall rule
                               # compares, so the same refusal twice stalls.
                               else frozenset(f"splice:{r}" for r in refusals)
                               if not spliced_now
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
                # Window from the file cargo compiled this attempt — header
                # through close of THIS container, not a pre-splice snapshot
                # of fill-target offsets.
                try:
                    _cur_err = file_path.read_text(encoding="utf-8")
                except OSError:
                    _cur_err = ""
                _err_lo, _err_hi = container_line_window(
                    _cur_err, block.container)
                last_error_excerpt = (
                    focus_diagnostics(
                        diagnostics, block.file,
                        line_lo=_err_lo, line_hi=_err_hi,
                        locate=_site_locator(),
                        target_container=block.container)
                    or _focus_cargo_errors(cargo_out, block.file))
                # rustc rejected it: it never ran, so there is no faulting
                # call to order the next body away from.
                last_error_kind = LastErrorKind.COMPILE
                _record_error_codes_seen(cargo_out)
                if deps.retire_contradicted_notes is not None and fr_path:
                    deps.retire_contradicted_notes(fr_path, cargo_out)
                # The fill regressed (a new error, or an error on the filled
                # fns) — keep-while-red already accepted non-regressing fills
                # above. Revert; the driver decides whether to retry.
                # A compiling attempt is banked first: the regression gate above
                # already cleared this file image against the pre-splice
                # baseline, so it is a strictly better end state than the stubs
                # if the loop later exhausts (see `_salvage` restore below).
                # An earlier panic is not a better image than the bodies it
                # replaced. Salvage would write it back at block exit, which
                # undoes the revert the keep just took.
                if cur_metric.compiles and not (
                        _joint_impl is not None and _joint_impl.refused_on_position):
                    salvage.offer(
                        file_path.read_text(encoding="utf-8", errors="replace"),
                        cur_metric)
                restore(_block_pre)
                _note_reverted(block.file, _impl_spliced, collateral=True)
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
            # The same fix-loop trajectory the PER-FN route has published since
            # it was written (`fill_one_fn.py::_on_fn_failure`), from the route
            # that needs it more: on run `1383168-eb151d09` (2026-08-29) the
            # per-fn route emitted eleven `loop.iter` records across its four
            # units while `impl Error` spent 21:41:11 -> 22:29:46 UTC — 48
            # minutes, five attempts and a maximal last-resort call — and
            # published start, `maximal` and fail, so nothing between them said
            # whether the block was converging. The floor and the stall count
            # are what answer that (floor falling = the model is closing on it;
            # stall climbing = it is re-buying the same wrong answer), and both
            # are already in hand here. Same event name and same field set as
            # the per-fn emit, so every consumer reads one shape per route.
            _emit("loop.iter", data={
                "name": "phase_c.impl",
                "cluster": cluster_name,
                "file": block.file, "container": block.container,
                "iter": attempt_n,
                "metric_new": outcome.metric.compile_errors,
                "metric_new_failing_tests": outcome.metric.failing_tests,
                "metric_best": str(gauge.best),
                "improved": gauge.stalls == 0,
                "stalled": gauge.stalls,
            })

        def _on_impl_stall(attempt_n: int, gauge: FloorStall) -> None:
            _codes = sorted(ecodes_in_text(last_error_excerpt))
            _log(f"  [phase-c/impl] {block_id}: floor stalled "
                 f"at {gauge.best} for {gauge.stalls} iter(s) "
                 f"codes={_codes or '—'} — stopping this block.")

        # A repeated error set is the block's outcome for this fill. The
        # retrospective may still be stored. The loop is not re-entered,
        # the maximal turn is not bought, and salvage does not write a
        # compiling image — the pre-fill bodies stay. The same rule as the
        # per-fn path (`_on_fn_repeated`).
        _repeated_stop: list[int] = []

        def _on_impl_repeated(attempt_n: int, gauge: FloorStall) -> None:
            _repeated_stop.append(attempt_n)
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
                # The block's own tests: an impl run claims several blocks
                # of a file on one scope.
                unit_tests=lambda: _answers_for_block(block),
            )
            if members_filled_this_block > 0 or block_already_complete:
                break
            _unit_end = unit_clock.ended(_answers_for_block(block))
            if _unit_end:
                # The unit ran past its bound (`FILL_UNIT_SHARE_MULTIPLE` ×
                # the run's share), or the ground its claim was planned on
                # moved: no last-resort call and no retrospective — neither
                # is evidence about these bodies. The block exits as a failed
                # block (salvage, stubs to the per-fn route); run
                # `484518-91681c1a` spent 199 minutes of a 15-minute share
                # here.
                if unit_clock.note_once():
                    _note_unit_end(_unit_end, "impl", block_id, fix_attempt,
                                   retro_round=_retro_round)
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
                           and not _repeated_stop
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
                # A turn on the block's conversation, not a second document:
                # the whole file is already above (the opening turn renders
                # it whole, and bodies rewritten since reach this turn as
                # `moved`), so what the budgeted attempts lacked is every
                # test that references the targets, verbatim.
                _max_tests: list[tuple[str, str]] = []
                _max_target_names = _block_target_names
                for _fn in _max_target_names:
                    for _pth, _txt in _find_tests_referencing_fn(
                            tests_dir_files, _fn, max_blocks=4):
                        if not any(p == _pth for p, _ in _max_tests):
                            _max_tests.append((_pth, _txt))
                _tests_text = "\n\n".join(
                    f"// {p}\n{t}" for p, t in _max_tests)
                _log(f"  [phase-c/impl] {block_id}: maximal last-resort "
                     f"turn (every referencing test, {len(_tests_text)} "
                     "chars).")
                _emit("phase_c.impl.fill.maximal", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "prompt_chars": len(_tests_text)})
                try:
                    attempts.note_call()
                    with serving_units(f"{block.file}::{n}"
                                       for n in _max_target_names):
                        _max_reply = _conv.retry(AttemptFeedback(
                            kind=last_error_kind,
                            excerpt=with_blamed_type_api(
                                last_error_excerpt, _crate_type_records),
                            targets=tuple(_max_target_names),
                            output=_impl_output,
                            note=("The budgeted attempts are spent. Below is "
                                  "every test that references these targets, "
                                  "whole; the file is above."),
                            sections=(EvidenceSection(
                                "TESTS_VERBATIM", _tests_text),),
                            moved=_conv.bodies_moved(
                                _read_source, frozenset(_max_target_names),
                                normalize=_cluster_view)),
                            has_deliverable=_has_fn_bodies)
                    _max_parsed = bind_headerless_names(
                        extract_fn_bodies(_max_reply), _max_target_names)
                    _max_audit = _audit_reply(
                        _log, _emit, cluster=cluster_name,
                        unit=str(block_id), route="impl-maximal",
                        reply=_max_reply, parsed=_max_parsed)
                    if _max_audit is not None and _max_audit.empty:
                        _note_declined_units(
                            block.file, _max_audit.empty,
                            lambda _n: block.container,
                            response_sha=_max_audit.response_sha,
                            route="impl-maximal")
                    if _max_parsed:
                        _splice_and_verify(_max_parsed, fix_attempt + 1)
                    else:
                        _log(f"  [phase-c/impl] {block_id}: maximal turn "
                             "returned no FN blocks "
                             f"({fn_tag_occurrences(_max_reply)} FN tag(s) "
                             "in reply).")
                except TranscriptBound:
                    _log(f"  [phase-c/impl] {block_id}: maximal turn not "
                         "sent — the conversation reached its bound.")
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
            _floor_note = ""
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
                        cfg_dead=_cfg_dead_fns(block.file),
                        unit=screening_unit(block.file, "*impl*"),
                        pins=_pins, observables=_block_ev.observables,
                        loop_scope=_block_loop_scope),
                    # The pins of the tests this block answers for, plus the
                    # asserted literals a one-argument `contains` puts only
                    # in observables: the diagnosis may not prescribe away
                    # an observable they name.
                    pinned_observables=pins_for_contradiction_guard(
                        _pins, _block_ev.observables),
                    compiled=bool(last_metric and last_metric.compiles),
                )
                _signal = retro.get("signal", "")
                if _signal in ("llm_error", "empty_response"):
                    raise RuntimeError(f"transient retro signal: {_signal}")
                # Errors all in OTHER files → the block is blocked by a break
                # elsewhere; its error count reflects the blocker, not these
                # bodies. Record the diagnosis but never exhaust, never move
                # the baseline, and don't retry now (mirrors the per-fn rule).
                _downstream = _signal == "downstream_only"
                # A regression-reverted attempt's metric describes a file
                # image the gate discarded and no salvage retains — the tree
                # holds `snapshot`, whose floor is `prior_best`. Letting it
                # move `new_best` graded the discard as progress: run
                # `358833-aa15d93c`, the maximal last-resort compiled, was
                # reverted on 5 newly-red Debug tests, `improved` came back
                # true against the prior floor of 1, and the loop re-entered
                # at `E0616`. The diagnosis still lands (the record below is
                # stamped, `regressed=` included); the comparison operates
                # on what the tree retains.
                _regr_reverted = _last_regr_reverted[0]
                if _downstream:
                    new_best = prior_best
                    improved = False
                    full_exhaust = False
                elif _regr_reverted:
                    new_best = prior_best
                    improved = False
                    # Not-improved on the kept tree, so the same rule as the
                    # comparison arm below: no re-entry bought on a metric
                    # the tree does not hold. The retro screens
                    # (`regression_scoped`, coverage-unreached above) decide
                    # what the stored record may steer later.
                    full_exhaust = True
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
                # Same rule, measured — the per-fn retro arm's coverage
                # gate, ported: a verdict drawn from tests that provably
                # never enter any member of this block is a verdict about
                # somewhere else. Convicting on it parks the block AND
                # stores a lesson every later prompt obeys. Refuses only on
                # positive evidence (`convictors_never_enter`); the pre-loop
                # gate normally prevents this round happening at all, this
                # is the arm for a conviction reached some other way.
                _bc_now = _block_evidence.tests
                _cov_unreached_block = False
                if (full_exhaust and _bc_now and all(
                        coverage_unreached_by(
                            FnLocator(block.file, _m.fn_name), _bc_now)
                        for _m in (*block.stub_members,
                                   *block.refill_members))):
                    full_exhaust = False
                    _cov_unreached_block = True
                    retro = {**retro, "guidance": ""}
                    _log(f"  [phase-c/impl/retro] {block_id}: NOT exhausting "
                         "and storing no guidance — no convicting test "
                         "executes any member of this block "
                         f"({', '.join(sorted(_bc_now))}); the attempt "
                         "bought no evidence about this block.")
                    _emit("phase_c.impl.retro.coverage_unreached", data={
                        "cluster": cluster_name, "file": block.file,
                        "container": block.container,
                        "convicted_by": sorted(_bc_now)})
                # A verdict earned from a crash whose only panics are
                # still-stubbed siblings' is the tree's verdict, not the
                # block's — the per-fn retro arm's rule, ported. The record
                # is still written (stamped `crash_stub_sites`, so the lesson
                # is withheld as hollow-tree guidance and the class is
                # countable); it just convicts nothing.
                if full_exhaust and _last_crash_sites[0]:
                    full_exhaust = False
                    _log(f"  [phase-c/impl/retro] {block_id}: NOT exhausting "
                         "— the last failure was a crash whose only panics "
                         "are still-stubbed sibling(s) at "
                         + ", ".join(f"{f}:{ln}" for f, ln
                                     in sorted(_last_crash_sites[0]))
                         + "; the verdict is the tree's, not the block's.")
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
                            last_error_excerpt),
                        pins=pins_for_contradiction_guard(
                            _pins, _block_ev.observables),
                        crash_stub_sites=_last_crash_sites[0],
                        loop_scope=_block_loop_scope)
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
                # A downstream break, a regression-reverted attempt (the
                # metric describes a discarded splice) and a measured
                # coverage-unreached block all withhold the retry: none of
                # them is evidence a re-prompt of THIS block buys anything,
                # and the last two are attempt-invariant. A round that did
                # not improve does not either, even when the splice, coverage
                # or hollow-sibling arm above vetoed its conviction: those
                # arms decide what is stored, not whether the floor moved
                # (the per-fn arm re-entered six unimproved rounds on run
                # `2657150-3139f958` that way).
                _retry_informed = (improved and not full_exhaust
                                   and not _downstream
                                   and not _regr_reverted
                                   and not _cov_unreached_block)
                _floor_note = (f"best {prior_best} -> {new_best} on the "
                               "kept tree")
            except Exception as _re:
                _log(f"  [phase-c/impl/retro] {block_id}: {_re}")
            # Fresh guidance for the next round's prompt (the closure reads
            # this enclosing binding), screened BEFORE the re-entry is
            # decided: the re-entry is bought by that guidance, so a round
            # whose record the screen retires does not re-enter.
            _prior_key = _retro_key(block.file, "*impl*", block.container)
            _prior_rec = retros_cache.get(_prior_key, {})
            _prior_verdict = screen_guidance(
                _prior_rec, snapshot, evidence=_block_answers,
                anchor_types=anchor_types_for_container(block.container),
                cluster=cluster_name, cfg_dead=_cfg_dead_fns(block.file),
                unit=screening_unit(block.file, "*impl*"),
                pins=_pins, observables=_block_ev.observables,
                loop_scope=_block_loop_scope)
            if _retry_informed and not informed_reentry(_prior_verdict):
                _retry_informed = False
                _log(f"  [phase-c/impl] {block_id}: retro round {_retro_round} "
                     "improved the error floor but its guidance would not "
                     "reach the next prompt ("
                     f"{_prior_verdict.retired_by or 'no guidance'}) — not "
                     "re-entering an uninformed fix loop.")
                _emit("phase_c.impl.retro_retry_withheld", data={
                    "cluster": cluster_name, "file": block.file,
                    "container": block.container,
                    "retro_round": _retro_round,
                    "retired_by": _prior_verdict.retired_by,
                })
            if _repeated_stop:
                if _retry_informed:
                    _log(f"  [phase-c/impl] {block_id}: retro round "
                         f"{_retro_round} stored; not re-entering — the fix "
                         "loop stopped on a repeated error set, which ends "
                         "the block for this fill.")
                break
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
            # Logged like the pass-start read: this is the record that steers
            # the retry the line below announces.
            prior_retro = _prior_verdict.text
            if (_hline := honoured_guidance_line(
                    _prior_key, _prior_rec, prior_retro)):
                _log(f"  {_hline}")
            if (_rline := retired_guidance_line(
                    _prior_key, _prior_rec, _prior_verdict)):
                _log(f"  {_rline}")
            # The floor named here is the KEPT tree's — a comparison that a
            # reverted attempt can no longer move (the round would not
            # re-enter), so the line states what it measured instead of
            # asserting an improvement over a state the tree did not hold.
            _log(f"  [phase-c/impl] {block_id}: retro round {_retro_round} "
                 f"kept the error floor improving ({_floor_note}) — "
                 "re-entering the fix loop with the new guidance.")
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
            # A repeated stop keeps nothing: the pre-fill bodies are the
            # block's state for this fill, including when a compiling
            # attempt is sitting in salvage.
            _salvaged = None if _repeated_stop else salvage.best
            if _salvaged is not None:
                with _splice_check_lock:
                    _sv_base = _test_regression_baseline(
                        tdd_test_cmd, target_src_dir, cluster_env)
                    # The salvage is a transaction: the compiling image
                    # goes in, is checked, and is rolled back to the
                    # stubbed pre-image byte for byte when it does not hold.
                    _sv_txn = TreeTxn(target_src_dir, validate_parse=_parse_ok)
                    _sv_txn.write(block.file, _salvaged.body)
                    _sv_rc, _, _ = _run_cargo_check_diag(
                        check_cmd, target_src_dir, cluster_env)
                    _sv_regr = (_test_regression_gate(
                        tdd_test_cmd, target_src_dir, cluster_env, _sv_base,
                        stub_free=stub_free)
                        if _sv_rc == 0 else None)
                    if _sv_rc != 0 or _sv_regr is not None:
                        _sv_txn.rollback()
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
                        _sv_txn.commit()
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
                _loss = _last_loss[0] if _last_loss else None
                errors.append(f"{block.file}::{block.container}: "
                              f"impl-fill failed after {fix_attempt} attempt(s)")
            # Classify outside the lock: ``_cls`` takes the same lock
            # (a nested acquire deadlocks ``threading.Lock``). An
            # attempt-1 LLM error leaves ``last_metric is None``; that
            # is a lost answer, not a compile failure (run-priced at
            # ``1064759-0f23fb68``'s fabricated-win neighbour).
            if (_loss in (AttemptLoss.ROUTING_DROP, AttemptLoss.SPLICE_REFUSAL)
                    or last_metric is None):
                _cls(FillOutcomeClass.ANSWER_DROPPED, block_id, "impl")
            elif last_metric.compiles:
                _cls(FillOutcomeClass.WRONG_BODY, block_id, "impl")
            else:
                _cls(FillOutcomeClass.COMPILE_FAIL, block_id, "impl")
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

    # PAIR FILL — a cross-file dispatcher/implementer pair the attribution
    # hop resolved dispatches as ONE transaction, before either file route
    # can open its halves independently. The class (run `358833-aa15d93c`,
    # 2026-09-01): the paying write is co-dependent across two files —
    # delegation alone reddens the no-backtrace Debug tests, the printer
    # alone is red-dead — so each half is correctly rejected by the revert
    # gate and no sequence of independently-judged keeps can land the
    # joint state. One prompt carries both blocks, both replies splice,
    # one compile + test reading keeps or reverts the two splices
    # together. Selection is `pair_dispatches` (domain): the hop's own
    # records and the collected blocks, no live names.
    def _fill_pair(pd: PairDispatch) -> None:
        nonlocal fns_filled_total, impls_filled, impls_failed
        _check_graceful_stop("pair-fill dispatch")
        pair = pd.pair
        halves = (pd.dispatcher_block, pd.implementer_block)
        pair_id = " + ".join(
            f"{b.file}::" + ",".join(
                dict.fromkeys(
                    m.fn_name for m in (*b.stub_members, *b.refill_members,
                                        *b.members)))
            for b in halves)
        _pair_locs = pd.member_locators()
        _pair_extra = tuple(pd.extra_blocks)
        _pair_tests: set[str] = set()
        for _loc in _pair_locs:
            _pair_tests |= _frontier_tests.convicting_for(_loc)
        # The pair's durable memory: one retro-store record keyed on BOTH
        # endpoints. Its `regressed` stamp is the previous joint write's
        # newly-red set (a measurement the next prompt renders as evidence),
        # its guidance the screened diagnosis of that write, and its
        # `last_prompt_sha` what makes a byte-identical rebuild a printed
        # refusal instead of a silent temperature resample. A compile
        # failure is not a regression: cargo already answered, so the
        # diagnostic goes into `last_error` (the builder slot per-impl
        # already has) and is retried in this call; `last_compile_error`
        # on the record carries it to the next pass and `last_prompt_sha`
        # is cleared so the identical-prompt refusal cannot lock the pair
        # out (run `719098-e021d93b`, 2026-09-01: E0599 `OsStr::from_bytes`
        # reverted the joint write, stamped the no-error prompt's sha, and
        # the next pass would have rebuilt that prompt verbatim).
        _pair_key = _retro_key(pair.dispatcher.file, "*pair*",
                               pair_record_container(pair))
        _pair_rec = retros_cache.get(_pair_key, {}) or {}
        _extra_locs = frozenset(
            FnLocator(b.file, m.fn_name)
            for b in _pair_extra
            for m in (*b.stub_members, *b.refill_members, *b.members))
        if _extra_locs:
            # Remember the slots before the reply can arrive: a
            # compile-fail or a later pair-only fill must not drop them.
            with _counters_lock:
                _crec = dict(retros_cache.get(_pair_key, {}) or {})
                _crec[PAIR_CRASH_CALLEES] = union_pair_callees(
                    _crec, _extra_locs)
                retros_cache[_pair_key] = _crec
                _save_retrospectives(project_dir, retros_cache)
            _pair_rec = retros_cache.get(_pair_key, {}) or {}
        _prior_regressed = tuple(
            str(t) for t in (_pair_rec.get("regressed") or []))
        _prior_regressed_output = {
            str(k): str(v)
            for k, v in dict(_pair_rec.get(REGRESSED_OUTPUT) or {}).items()}
        # The FULL evidence assembly, over both halves' members — the same
        # call the per-impl route makes (`block_evidence`): attributed test
        # SOURCE first, the failing tests' own words, the pins, then the
        # name-match fallback. The route this replaces had only the name
        # match: run `537093-ffcfd139`'s pair prompt carried three test
        # files, none holding its two convicting tests, and no evidence
        # block — the transaction was correct and the prompt was blind.
        _pair_ev = assemble_block_evidence(
            (pd.dispatcher_block, pd.implementer_block, *_pair_extra),
            all_blocks,
            _frontier_tests,
            find_tests=lambda leaf, mx: _find_tests_referencing_fn(
                tests_dir_files, leaf, max_blocks=mx),
            escalated=escalated_keys,
            measured_owner=measured_owner_keys,
            crashed=crash_blamed or frozenset(),
            aimed=frozenset(
                loc for loc in (crash_evidence or {})
                if loc not in (crash_blamed or frozenset())) | frozenset(
                FnLocator(b.file, m.fn_name)
                for b in _pair_extra
                for m in extra_write_members(b)),
            crash_sites=crash_sites,
            read_source=_read_source,
            ledger_entries=ledger_entries, oracle_fp=oracle_fp,
            tuple_structs=_tuple_structs,
            prior_regressed=_prior_regressed,
            prior_regressed_output=_prior_regressed_output,
            # The pair's keep clears `last_failed_body` and its revert sets
            # it, so a record from before the stamp still says whether a
            # keep followed the revert.
            prior_regressed_attempt=regressed_attempt_of(
                _pair_rec, keep_clears_failed_body=True),
            joint_rewrite=True,
            joint_endpoint_names=frozenset(
                loc.fn_name for loc in pd.pair.member_locators()),
            leaf_cap=_ATTRIBUTED_TEST_LEAF_CAP,
            file_cap=_ATTRIBUTED_TEST_FILE_CAP)
        _half_ctx: list[tuple[ImplBlock, str, str]] = []
        for b in halves:
            try:
                _htxt = (target_src_dir / b.file).read_text(encoding="utf-8")
            except OSError as _he:
                _log(f"  [phase-c/pair] {pair_id}: cannot read {b.file}: "
                     f"{_he} — pair not dispatched.")
                return
            _half_ctx.append(
                (b, _htxt, _extract_impl_where_clause(_htxt, b.container)))
        _types_join = "\n\n".join(dict.fromkeys(s for s in (
            _types_section_for_fix(
                " ".join(m.sig_text for m in (*b.stub_members,
                                              *b.refill_members)),
                _htxt, "", b.container, _hwc, _crate_type_records,
                file_rel=b.file, observables=_pair_ev.observables)
            for b, _htxt, _hwc in _half_ctx) if s))
        _bound_join = "\n\n".join(dict.fromkeys(s for s in (
            _bound_methods_section_for(
                _hwc, _crate_type_records, file_rel=b.file)
            for b, _htxt, _hwc in _half_ctx) if s))
        # The pair record's guidance, under the ONE screening every stored
        # lesson goes through — judged against BOTH halves' text (a layout or
        # impossibility claim about either file retires it) and under the
        # pair's own scope: `joint_members` exempts a record earned by this
        # same transaction from `regression_scoped` (its acceptance gate
        # re-judges the reddened tests), and `joint_body_sha` expires it the
        # moment any route lands a different body in either member.
        _halves_text = "\n".join(_htxt for _b, _htxt, _hwc in _half_ctx)
        _pair_anchors = (
            anchor_types_for_container(pd.dispatcher_block.container)
            | anchor_types_for_container(pd.implementer_block.container))
        # A member's self type may be declared in a file the pair does not
        # write (Error in lib.rs, the Debug impl in error.rs). Without that
        # declaration the sieve treats a field of the missing type as a lie
        # about the in-file implementer.
        _decl = src_text_declaring(target_src_dir, _pair_anchors)
        if _decl:
            _halves_text = _halves_text + "\n" + _decl
        _pair_answers = ConvictionEvidence(frozenset(
            t for loc in _pair_locs for t in _frontier_tests.tests_for(loc)))
        _pair_verdict = screen_guidance(
            _pair_rec, _halves_text, evidence=_pair_answers,
            anchor_types=_pair_anchors,
            cluster=cluster_name,
            cfg_dead=(_cfg_dead_fns(pd.dispatcher_block.file)
                      | _cfg_dead_fns(pd.implementer_block.file)),
            unit=None,
            joint_members=pd.member_scope(),
            joint_body_sha=pd.member_body_sha(),
            pins=_pair_ev.pins, observables=_pair_ev.observables)
        _pair_retro = _pair_verdict.text
        if (_hline := honoured_guidance_line(
                str(_pair_key), _pair_rec, _pair_retro)):
            _log(f"  {_hline}")
        if (_rline := retired_guidance_line(
                str(_pair_key), _pair_rec, _pair_verdict)):
            _log(f"  {_rline}")

        def _make_pair_prompt(_err: str, _kind: LastErrorKind) -> str:
            return _build_per_impl_prompt(
                block=pd.dispatcher_block,
                related_blocks=[],
                test_blocks=list(_pair_ev.test_blocks),
                module_name=module_name,
                cluster_name=cluster_name,
                sections=PerImplSections(
                    types=_types_join, invariants=invariants_section,
                    notes="", crate_idioms="", dispatch_family="",
                    macros=macros_section, siblings="",
                    retrospective_guidance=_pair_retro,
                    bound_methods=_bound_join,
                    refill_evidence=_pair_ev.refill_evidence,
                    inferred_refill_names=_pair_ev.inferred_names,
                    crashed_refill_names=_pair_ev.crashed_names,
                    aimed_refill_names=_pair_ev.aimed_names,
                    behaviour_must=_behaviour_must(
                        *(m for b in (*halves, *_pair_extra)
                          for m in (*b.stub_members, *b.refill_members))),
                    last_error=_err,
                    last_error_kind=_kind),
                fr_path=fr_path,
                partner_block=pd.implementer_block,
                extra_blocks=_pair_extra)

        # Compile diagnostic first (this call already answered); else the
        # stored verdict of the last joint revert. A crash summary names
        # the binaries; without the previous bodies LAST_ERROR's "do not
        # resubmit the same approach" had no referent, so run
        # `186665-40892dbd` fill 2 resubmitted fill 1's `by_ref` dispatcher
        # after an honoured diagnosis that named a different expression.
        _pair_err = pair_last_error(
            str(_pair_rec.get("last_compile_error") or ""),
            str(_pair_rec.get("last_error_preview") or ""),
            str(_pair_rec.get("last_failed_body") or ""),
            kind=kind_from_record(_pair_rec))
        _raw_err = _pair_err.text
        _last_error = with_blamed_type_api(
            _raw_err, _crate_type_records) if _raw_err else ""
        # The pair's one conversation: attempt 1 opens it with the joint
        # prompt (LAST_ERROR there is the stored verdict of the previous
        # fill); an in-call compile retry is a turn on it.
        _conv = _open_conversation(pair_id, "pair")
        _pair_targets = [
            (m.fn_name, b.container)
            for b in (pd.dispatcher_block, pd.implementer_block, *_pair_extra)
            for m in (*b.stub_members, *b.refill_members)]
        for _attempt in range(1, _FILE_FIRST_MAX_ATTEMPTS + 1):
            # The pair's later call is asked like every other unit's: the
            # compile retry falls through to here, and a pair whose leftover
            # tests another keep already moved has nothing left to judge it.
            _unit_end = (unit_clock.ended(frozenset(_pair_tests))
                         if _attempt > 1 else "")
            if _unit_end:
                if unit_clock.note_once():
                    _note_unit_end(_unit_end, "pair", pair_id, _attempt - 1)
                # As at the last failed attempt below: the sha of the prompt
                # that was sent must not refuse the next pass's question.
                with _counters_lock:
                    _crec = dict(retros_cache.get(_pair_key, {}) or {})
                    _crec["last_prompt_sha"] = ""
                    retros_cache[_pair_key] = _crec
                    _save_retrospectives(project_dir, retros_cache)
                return
            if not _conv.opened:
                prompt = _make_pair_prompt(_last_error, _pair_err.kind)
                # Compare against the sha of the last prompt SENT (the cache,
                # which this loop updates), not the entry snapshot: an in-call
                # compile retry must see attempt 1's stamp. A compile retry
                # that rebuilds those same bytes (the diagnostic did not
                # change) still clears the sha — otherwise the next pass
                # seeds LAST_ERROR, rebuilds the same prompt, and the
                # refusal locks the pair out of the error-informed question
                # (run `719098-e021d93b`, 2026-09-01).
                _prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                _prev_sha = str(
                    (retros_cache.get(_pair_key, {}) or {}).get("last_prompt_sha")
                    or "")
                if _prompt_sha == _prev_sha:
                    _log(f"  [phase-c/pair] {pair_id}: rebuilt a byte-identical "
                         "prompt — the revert feedback did not change, so another "
                         "call buys a temperature resample of the same question; "
                         "pair not dispatched this pass.")
                    _emit("phase_c.pair.fill.resample_skipped", data={
                        "cluster": cluster_name,
                        "dispatcher": site_payload(pair.dispatcher),
                        "implementer": site_payload(pair.implementer)})
                    if _last_error:
                        with _counters_lock:
                            _crec = dict(retros_cache.get(_pair_key, {}) or {})
                            _crec["last_prompt_sha"] = ""
                            retros_cache[_pair_key] = _crec
                            _save_retrospectives(project_dir, retros_cache)
                    return
                _log(f"  [phase-c/pair] {pair_id}: pair fill — dispatcher and "
                     "implementer written under ONE judgment across "
                     f"{len(halves)} files ({len(_pair_tests)} convicting test(s)"
                     + (f", {sum(len({m.fn_name for m in (*b.stub_members, *b.refill_members, *b.members)}) for b in _pair_extra)} table slot(s)"
                        if _pair_extra else "")
                     + ").")
                _emit("phase_c.pair.fill.start", data={
                    "cluster": cluster_name,
                    "dispatcher": site_payload(pair.dispatcher),
                    "implementer": site_payload(pair.implementer),
                    "convicting": sorted(_pair_tests)})
                attempts.note_dispatched(_pair_locs)
                attempts.note_call()
                with _counters_lock:
                    # The sha of what is being SENT, persisted before the reply can
                    # arrive — the refusal above compares against the last prompt
                    # sent, not the last one that produced a revert.
                    retros_cache[_pair_key] = {
                        **(retros_cache.get(_pair_key, {}) or {}),
                        "last_prompt_sha": _prompt_sha}
                    _save_retrospectives(project_dir, retros_cache)
                try:
                    with serving_units(f"{k.file}::{k.fn_name}"
                                       for k in sorted(_pair_locs)):
                        _pair_reply = _conv.open(
                            prompt, has_deliverable=_has_fn_bodies)
                except TranscriptBound:
                    return
                except Exception as _pe:
                    _log(f"  [phase-c/pair] {pair_id}: LLM error: {_pe}")
                    attempts.note_lost(_pair_locs)
                    return
            else:
                attempts.note_call()
                try:
                    with serving_units(f"{k.file}::{k.fn_name}"
                                       for k in sorted(_pair_locs)):
                        _pair_reply = _conv.retry(AttemptFeedback(
                            kind=LastErrorKind.COMPILE, excerpt=_last_error,
                            targets=tuple(n for n, _ in _pair_targets),
                            output=fn_output(_pair_targets),
                            sections=(EvidenceSection(
                                "TYPES_IN_SCOPE", types_new_to(
                                    _conv.text(), _last_error,
                                    _crate_type_records,
                                    viewer_file=pd.dispatcher_block.file),
                                "Types this result names that were not "
                                "shown above:\n"),),
                            moved=_conv.bodies_moved(
                                _read_source,
                                frozenset(n for n, _ in _pair_targets),
                                normalize=_cluster_view)),
                            has_deliverable=_has_fn_bodies)
                except TranscriptBound:
                    return
                except Exception as _pe:
                    _log(f"  [phase-c/pair] {pair_id}: LLM error: {_pe}")
                    attempts.note_lost(_pair_locs)
                    return
            _pair_parsed = extract_fn_bodies(_pair_reply)
            if not _pair_parsed:
                _log(f"  [phase-c/pair] {pair_id}: empty/no FN blocks "
                     f"({fn_tag_occurrences(_pair_reply)} FN tag(s) in reply).")
                attempts.note_lost(_pair_locs)
                return

            def _record_pair_regression(_rcmp: RegressionComparison,
                                        _rdetail: str,
                                        *,
                                        _reply: str = _pair_reply) -> None:
                """The pair's durable memory of its own joint revert: the
                diagnosis (screened like every stored lesson on the next read),
                the newly-red set as the `regressed` stamp (what the next
                prompt renders as measured evidence), and the joint premise —
                the member set and the kept bodies' sha, which is what lets the
                same transaction read the lesson back (`joint_members` exempts
                it from `regression_scoped`; `joint_body_sha` expires it when
                either member's body moves). Transient LLM failures update no
                state, exactly as the per-impl retro rule."""
                try:
                    retro = _phase_c_retrospective(
                        fn_name="*pair*",
                        sig_text=pair_record_container(pair),
                        container=pair_record_container(pair),
                        file_rel=pair.dispatcher.file,
                        cluster_name=cluster_name,
                        last_body=_reply[:_FAILED_BODY_CAP],
                        error_excerpt=_rdetail,
                        fix_attempts=1,
                        prior_retrospective=_pair_retro,
                        pinned_observables=pins_for_contradiction_guard(
                            _pair_ev.pins, _pair_ev.observables),
                        compiled=True)
                    _signal = retro.get("signal", "")
                    if _signal in ("llm_error", "empty_response"):
                        raise RuntimeError(f"transient retro signal: {_signal}")
                    _prov = RetroProvenance.current(cluster_name, _halves_text)
                    _blockers = BlockedOnStubs.at_conviction(
                        _reply[:_FAILED_BODY_CAP], _halves_text)
                    with _counters_lock:
                        _cached = dict(retros_cache.get(_pair_key, {}) or {})
                        _kept_sha = str(_cached.get("last_prompt_sha") or "")
                        _rec = stamp_conviction({
                            "category": retro.get("category", ""),
                            "guidance": retro.get("guidance", ""),
                            "signal": _signal,
                            "retrospective_count": int(
                                _pair_rec.get("retrospective_count", 0)) + 1,
                            "error_count": 0,
                            "failing_test_count": len(_rcmp.newly_red),
                            # The pair re-dispatches once per traversal pass by
                            # design; exhaustion semantics belong to the routes
                            # that own retry budgets.
                            "fully_exhausted": False,
                            "last_error_preview": _rdetail[:240],
                            # The verdict, beside the excerpt of it. The
                            # preview's budget cuts the TAIL, which on a
                            # wide crash is the whole instruction, and the
                            # next prompt used to re-derive the class from
                            # whatever prose survived.
                            LAST_ERROR_KIND: regression_detail_kind(
                                _rcmp).value,
                            "last_failed_body": _reply[:_FAILED_BODY_CAP],
                            "last_compile_error": str(
                                _cached.get("last_compile_error") or ""),
                        }, _prov, _pair_answers, _blockers,
                            regressed=tuple(sorted(_rcmp.newly_red)),
                            pins=pins_for_contradiction_guard(
                                _pair_ev.pins, _pair_ev.observables),
                            crash_stub_sites=(_rcmp.crash_stub_sites
                                              if _rcmp.newly_crashed else None))
                        _rec[JOINT_MEMBERS] = sorted(pd.member_scope())
                        _rec[JOINT_BODY_SHA] = pd.member_body_sha()
                        _rec[PAIR_CRASH_CALLEES] = union_pair_callees(
                            _cached, _extra_locs)
                        # What the reddened tests printed, beside their
                        # names: the next prompt renders both.
                        _rec[REGRESSED_OUTPUT] = dict(_rcmp.newly_red_output)
                        _rec["last_prompt_sha"] = _kept_sha
                        retros_cache[_pair_key] = _rec
                        _save_retrospectives(project_dir, retros_cache)
                    _log(f"  [phase-c/pair/retro] {pair_id}: joint revert "
                         f"recorded ({len(_rcmp.newly_red)} newly-red test(s), "
                         "premise = both member bodies on the kept tree) — the "
                         "next pair prompt carries the diagnosis and what those "
                         "tests assert.")
                except Exception as _re:
                    _log(f"  [phase-c/pair/retro] {pair_id}: {_re}")

            with _splice_check_lock:
                _snaps = {b.file: capture(target_src_dir / b.file, None)
                          for b in (*halves, *_pair_extra)}
                _pair_baseline = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env)
                _pair_hollow_before = hollow_count(snapshot_src_tree(target_src_dir))
                _spliced_by_file: dict[str, list[str]] = {}
                # Bodies refused for their own TEXT (`reply_refusal_feedback`):
                # a failed attempt the next turn repairs, not a lost answer.
                _pair_text_refused: list[str] = []
                # (unit, signature, body, container) of every body the pair
                # landed, for the keep's ignored-input decline.
                _pair_landed: list[tuple[str, str, str, str]] = []
                for b, _htxt, _hwc in _half_ctx:
                    _routing = route_fn_bodies(
                        _pair_parsed, target=b.container, related=[],
                        trait_defaults=trait_default_hosts(all_blocks, b.file),
                        members=members_in_file(all_blocks, b.file))
                    _half_names = {m.fn_name for m in (*b.stub_members,
                                                       *b.refill_members)}
                    _half_refills = {m.fn_name for m in b.refill_members}
                    for _rb in _routing.routed:
                        _fd = _rb.fn_data
                        if _fd["fn_name"] not in _half_names:
                            continue
                        _wc2 = _fd["where_clause"]
                        if _wc2 and (_rb.via_trait_default
                                     or signature_fixed_by_trait(_rb.container)):
                            _wc2 = None
                        _psp = _splice_fn_with_where(
                            target_src_dir / b.file, _rb.container,
                            _fd["fn_name"], _fd["body"], _wc2,
                            allow_nonstub=_fd["fn_name"] in _half_refills,
                            sig_text=_rb.sig_text)
                        _ok, _reason, _delta = _psp
                        if _ok:
                            _spliced_by_file.setdefault(b.file, []).append(
                                _fd["fn_name"])
                            _pair_landed.append((
                                f"{b.file}::{_fd['fn_name']}", _rb.sig_text,
                                str(_fd["body"]), _rb.container))
                        else:
                            _log(f"  [phase-c/pair] {pair_id}: splice "
                                 f"`{_fd['fn_name']}` failed: {_reason}")
                            _pfb = reply_refusal_feedback(
                                _psp, unit=_fd["fn_name"], file_rel=b.file)
                            if _pfb is not None:
                                _pair_text_refused.append(_pfb)
                # Same-file table slots: splice by name, not by the model's
                # block attr. A free fn has no header the pair prompt can
                # name, and an invented one used to drop the body. The
                # locator is already the write set.
                _parsed_by_name = {p.get("fn_name"): p for p in _pair_parsed}
                _extra_wanted: list[str] = []
                for _eb in _pair_extra:
                    # Members-only extras compile and their tests pass —
                    # quote them, do not require a rewrite. Run
                    # `171582-cb964fb9` spent 23 pair fills rewriting
                    # those slots to None / a wild deref.
                    for _em in (*_eb.stub_members, *_eb.refill_members):
                        _extra_wanted.append(_em.fn_name)
                        _efd = _parsed_by_name.get(_em.fn_name)
                        if not _efd:
                            _log(f"  [phase-c/pair] {pair_id}: table slot "
                                 f"`{_em.fn_name}` missing from reply.")
                            continue
                        _esp = _splice_fn_with_where(
                            target_src_dir / _eb.file, _eb.container,
                            _em.fn_name, _efd["body"],
                            _efd.get("where_clause"),
                            allow_nonstub=True, sig_text=_em.sig_text)
                        _ok, _reason, _delta = _esp
                        if _ok:
                            _spliced_by_file.setdefault(_eb.file, []).append(
                                _em.fn_name)
                            _pair_landed.append((
                                f"{_eb.file}::{_em.fn_name}", _em.sig_text,
                                str(_efd["body"]), _eb.container))
                        else:
                            _log(f"  [phase-c/pair] {pair_id}: splice "
                                 f"`{_em.fn_name}` failed: {_reason}")
                            _efb = reply_refusal_feedback(
                                _esp, unit=_em.fn_name, file_rel=_eb.file)
                            if _efb is not None:
                                _pair_text_refused.append(_efb)

                def _revert_pair(_kept: dict[str, PreImage] = _snaps,
                                 _names: dict[str, list[str]] = _spliced_by_file
                                 ) -> None:
                    for _pre_img in _kept.values():
                        restore(_pre_img)
                    for _pf, _pnames in _names.items():
                        _note_reverted(_pf, _pnames)

                _n_spliced = sum(len(v) for v in _spliced_by_file.values())
                _spliced_names = {
                    n for ns in _spliced_by_file.values() for n in ns}
                _missing_extra = [n for n in _extra_wanted
                                  if n not in _spliced_names]
                _need_files = pair_required_files(pd)
                _missing_files = _need_files - set(_spliced_by_file)
                # A same-file slot counts its file as landed as soon as it
                # splices, so a half member refused for its own text is
                # neither a missing file nor a missing extra. The pair is
                # still half-written: revert and re-ask, and do not
                # cargo-check that partial tree. A quoted half is not
                # required: omitting its FN keeps the current body.
                if _pair_text_refused or _missing_files or _missing_extra:
                    if _n_spliced:
                        _revert_pair()
                    if _pair_text_refused:
                        # A body the splicer refused for its own text is a
                        # failed attempt, as on every other splice route
                        # (`splice_feedback`): the next turn carries the
                        # parser's diagnostic, and the answer is not dropped.
                        _cls(FillOutcomeClass.COMPILE_FAIL, pair_id, "pair")
                        _last_error = "\n\n".join(_pair_text_refused)
                        _emit("phase_c.pair.fill.reverted", data={
                            "cluster": cluster_name, "why": "reply_refused",
                            "errors": len(_pair_text_refused),
                            "attempt": _attempt,
                            "retrying": _attempt < _FILE_FIRST_MAX_ATTEMPTS})
                        if _attempt < _FILE_FIRST_MAX_ATTEMPTS:
                            _log(f"  [phase-c/pair] {pair_id}: "
                                 f"{len(_pair_text_refused)} body(ies) refused "
                                 "for their own text — nothing kept; retrying "
                                 "with the parser's diagnostic (attempt "
                                 f"{_attempt + 1}/{_FILE_FIRST_MAX_ATTEMPTS}).")
                            continue
                        _log(f"  [phase-c/pair] {pair_id}: "
                             f"{len(_pair_text_refused)} body(ies) refused for "
                             "their own text on the last attempt — nothing kept.")
                        with _counters_lock:
                            _crec = dict(retros_cache.get(_pair_key, {}) or {})
                            _crec["last_prompt_sha"] = ""
                            retros_cache[_pair_key] = _crec
                            _save_retrospectives(project_dir, retros_cache)
                        return
                    if _missing_extra:
                        _log(f"  [phase-c/pair] {pair_id}: reply omitted "
                             f"table slot(s) {', '.join(_missing_extra)} — "
                             "nothing kept (the transaction needs the slots "
                             "the last crash-revert called).")
                    else:
                        _log(f"  [phase-c/pair] {pair_id}: reply covered "
                             f"{len(_spliced_by_file)} of {len(_need_files)} "
                             "required file(s) — nothing kept (the "
                             "transaction needs every rewritten half).")
                    _cls(FillOutcomeClass.ANSWER_DROPPED, pair_id, "pair")
                    _emit("phase_c.pair.fill.partial", data={
                        "cluster": cluster_name,
                        "spliced": {f: sorted(ns)
                                    for f, ns in _spliced_by_file.items()}})
                    return
                rc, _pair_out, _pair_diags = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                if rc != 0:
                    _pair_sig = _cargo_err_sig_from_diagnostics(_pair_diags)
                    _revert_pair()
                    _log(f"  [phase-c/pair] {pair_id}: REVERTED both halves — "
                         f"compile failed ({len(_pair_sig)} err(s)).")
                    _verdict_row(
                        pair_id, Disposition.REVERT,
                        "pair fill compile failed — both splices reverted as "
                        "one transaction",
                        outcome=GateOutcome.NEGATIVE, attempt=1,
                        evidence=tuple(sorted(_pair_sig)), model_calls=1)
                    _cls(FillOutcomeClass.COMPILE_FAIL, pair_id, "pair")
                    with _counters_lock:
                        impls_failed += 1
                    _emit("phase_c.pair.fill.reverted", data={
                        "cluster": cluster_name, "why": "compile",
                        "errors": len(_pair_sig), "attempt": _attempt,
                        "retrying": _attempt < _FILE_FIRST_MAX_ATTEMPTS})
                    _excerpt_parts = [
                        _focus_cargo_errors(_pair_out, b.file) for b in halves]
                    _excerpt = "\n".join(x for x in _excerpt_parts if x).strip()
                    if not _excerpt:
                        _excerpt = "\n".join(_pair_sig)
                    _last_error = with_blamed_type_api(
                        _excerpt, _crate_type_records)
                    with _counters_lock:
                        _crec = dict(retros_cache.get(_pair_key, {}) or {})
                        _crec["last_compile_error"] = _last_error[:4000]
                        retros_cache[_pair_key] = _crec
                        _save_retrospectives(project_dir, retros_cache)
                    if _attempt < _FILE_FIRST_MAX_ATTEMPTS:
                        _log(f"  [phase-c/pair] {pair_id}: compile failed — "
                             f"retrying with LAST_ERROR "
                             f"(attempt {_attempt + 1}/"
                             f"{_FILE_FIRST_MAX_ATTEMPTS}).")
                        continue
                    with _counters_lock:
                        _crec = dict(retros_cache.get(_pair_key, {}) or {})
                        _crec["last_prompt_sha"] = ""
                        retros_cache[_pair_key] = _crec
                        _save_retrospectives(project_dir, retros_cache)
                    return
                # Do not clear last_compile_error here: compiler_rejected
                # reads it on the record so a later fill cannot honour a
                # call rustc already rejected. The pair LAST_ERROR prompt
                # does not concatenate it with a compiling crash — that
                # mix is how a by_ref crash was diagnosed as as_ptr.
                _pair_sites = frozenset(
                    FnSite(b.file, m.fn_name, b.container)
                    for b in (*halves, *_pair_extra)
                    for m in (*b.stub_members, *b.refill_members))
                _pair_u2 = _u2_test_gate(_pair_tests, _pair_sites, tdd_test_cmd,
                                         target_src_dir, cluster_env)
                _pair_regr = _test_regression_gate(
                    tdd_test_cmd, target_src_dir, cluster_env, _pair_baseline,
                    stub_free=stub_free, hollow_before=_pair_hollow_before)
                if _pair_regr is not None:
                    _rcmp, _rdetail = _pair_regr
                    attempts.remember_bodies({
                        str(key): str(body)
                        for key, _sig, body, _cont in _pair_landed if body})
                    _revert_pair()
                    _log(f"  [phase-c/pair] {pair_id}: REVERTED both halves — "
                         f"regressed sibling test(s): {_rcmp.summary}.")
                    _verdict_row(
                        pair_id, Disposition.REVERT,
                        f"pair regressed sibling test(s): {_rcmp.summary} — "
                        "both splices reverted as one transaction",
                        outcome=GateOutcome.NEGATIVE, attempt=1,
                        evidence=tuple(sorted(_rcmp.signature)), model_calls=1)
                    _cls(FillOutcomeClass.WRONG_BODY, pair_id, "pair")
                    with _counters_lock:
                        impls_failed += 1
                    _emit("phase_c.pair.fill.reverted", data={
                        "cluster": cluster_name, "why": "regression",
                        "detail": _rcmp.summary})
                    # The transaction remembers its own revert: the diagnosis is
                    # a model call made while still holding the splice lock,
                    # which is uncontended here — pair jobs run serially on this
                    # thread BEFORE any worker pool starts (`_pair_jobs` loop
                    # precedes the file-first dispatch).
                    _record_pair_regression(_rcmp, _rdetail)
                    return
                # The file route's bar exactly, panic sites included
                # (`judge_refill`). Run `24951-a72c4642` (2026-09-29) kept a
                # pair write here on names alone: `test_altdebug` went from
                # failing at `tests/test_fmt.rs:91` to `:90`, and the earlier
                # panic cut the implementer the test really reaches out of
                # the coverage owner map for the rest of the run.
                _pair_joint = (
                    judge_refill(_pair_tests, _pair_baseline, _pair_u2)
                    if (_pair_u2 is not None
                        and _pair_u2.decision is U2Decision.REJECT
                        and _pair_baseline is not None) else None)
                if _pair_joint is not None and _pair_joint.site_movement:
                    _log(f"  [phase-c/pair] {pair_id}: panic sites moved — "
                         f"{_pair_joint.site_movement}")
                _pair_nonregr = (_pair_joint is not None
                                 and _pair_joint.non_regression)
                if _pair_nonregr and SameCrashJudge(
                        frozenset(crash_blamed or ()), culprits_of).refuses(
                        output=(_pair_u2.capture if _pair_u2 is not None else ""),
                        aimed=_pair_tests,
                        written=[FnLocator(b.file, m.fn_name)
                                 for b in (*halves, *_pair_extra)
                                 for m in (*b.stub_members, *b.refill_members)]):
                    _log(f"  [phase-c/pair] GUARD {pair_id}: joint re-fill "
                         "REVERTED — every aimed test is still a native crash "
                         "and the crash's culprit(s) it wrote still are.")
                    _pair_nonregr = False
                if _pair_nonregr and _pair_u2 is not None:
                    # The same bar as the file and impl routes: convicting
                    # tests still failing, none newly red — KEPT, deferred
                    # to the sweep (the pair used to be the one route that
                    # reverted here).
                    _log(f"  [phase-c/pair] GUARD {pair_id}: joint re-fill "
                         f"KEPT on non-regression ({len(_pair_u2.self_failures)} "
                         "convicting test(s) still failing, none passing at "
                         "baseline, none panicking earlier) — judged at the "
                         "next sweep.")
                if (_pair_u2 is not None
                        and _pair_u2.decision is U2Decision.REJECT
                        and not _pair_nonregr):
                    _revert_pair()
                    _log(f"  [phase-c/pair] {pair_id}: REVERTED both halves — "
                         f"{len(_pair_u2.self_failures)} convicting test(s) "
                         "still failing after the joint write.")
                    _verdict_row(
                        pair_id, Disposition.REVERT,
                        "pair convicting tests still failing — both splices "
                        "reverted as one transaction",
                        outcome=GateOutcome.NEGATIVE, attempt=1,
                        evidence=tuple(sorted(_pair_u2.self_failures)),
                        model_calls=1)
                    _cls(FillOutcomeClass.WRONG_BODY, pair_id, "pair")
                    with _counters_lock:
                        impls_failed += 1
                    _emit("phase_c.pair.fill.reverted", data={
                        "cluster": cluster_name, "why": "u2",
                        "failing": sorted(_pair_u2.self_failures)})
                    return
                # The keep's ignored-input decline (`domain/ignored_inputs`):
                # a joint write that drops a declared input of one of its
                # bodies, with no test attributed to that body turned green
                # by the write, is not kept; the pair record carries the
                # reason to the next buy.
                _pair_attr: dict[tuple[str, str], set[str]] = {}
                for _u, _sig, _body, _c in _pair_landed:
                    # The unit is `file::fn`. One split from the right: a
                    # path has no `::`.
                    _parts = _u.rsplit("::", 1)
                    _pair_attr[(_u, _c)] = _frontier_tests.tests_for_body(
                        _parts[0], _parts[-1], _c)
                _ignoring_p = declined_by_ignored_inputs(
                    _pair_landed, vouched=vouched_for_keep(
                        _pair_attr, turned_green(_pair_baseline, _pair_u2)))
                if _ignoring_p:
                    _revert_pair()
                    _pfeed = "\n\n".join(
                        ignored_inputs_feedback(
                            identity_name(u).rsplit("::", 1)[-1], i)
                        for u, i in sorted(_ignoring_p.items()))
                    with _counters_lock:
                        impls_failed += 1
                        _drec = dict(retros_cache.get(_pair_key, {}) or {})
                        _drec["last_error_preview"] = _pfeed[:240]
                        _drec[LAST_ERROR_KIND] = LastErrorKind.COMPILE.value
                        _drec["last_failed_body"] = _pair_reply[:_FAILED_BODY_CAP]
                        _drec["last_compile_error"] = _pfeed
                        retros_cache[_pair_key] = _drec
                        _save_retrospectives(project_dir, retros_cache)
                    _log(f"  [phase-c/declined] pair {pair_id}: "
                         f"{len(_ignoring_p)} body(ies) ignore declared "
                         "input(s) with no convicting test observed passing "
                         "— both halves reverted: "
                         + ", ".join(f"{u} ({', '.join(i)})" for u, i
                                     in sorted(_ignoring_p.items())))
                    _emit("phase_c.fill.declined", data={
                        "cluster": cluster_name, "unit": pair_id,
                        "route": "pair", "reason": "ignored_inputs",
                        "names": sorted(_ignoring_p),
                        "ignored": {u: list(i) for u, i
                                    in sorted(_ignoring_p.items())}})
                    _verdict_row(
                        pair_id, Disposition.REVERT,
                        "declined — a body ignores declared input(s) and no "
                        "convicting test was observed passing",
                        outcome=GateOutcome.NEGATIVE, attempt=1,
                        model_calls=1)
                    _cls(FillOutcomeClass.DECLINED, pair_id, "pair")
                    return
                with _counters_lock:
                    fns_filled_total += _n_spliced
                    impls_filled += 1
                    _krec = dict(retros_cache.get(_pair_key, {}) or {})
                    # A kept joint write IS the last attempt, so no earlier
                    # failure is its LAST_ERROR. Clearing only the compile
                    # excerpt and the body left the revert's preview and
                    # kind behind, and `pair_last_error` rendered them:
                    # run `11358-e5de094e` (2026-09-15) sent fills 4 and 5
                    # "Your previous attempt compiled and then panicked"
                    # over fill 2's crash of tests/test_fmt.rs, after fill
                    # 3 cashed +2 and fill 4 was kept. The diagnosis
                    # (guidance, regressed output) is the retrospective's
                    # and stays.
                    if (_krec.get("last_compile_error")
                            or _krec.get("last_failed_body")
                            or _krec.get("last_error_preview")
                            or _krec.get(LAST_ERROR_KIND)):
                        _krec["last_compile_error"] = ""
                        _krec["last_failed_body"] = ""
                        _krec["last_error_preview"] = ""
                        _krec.pop(LAST_ERROR_KIND, None)
                        retros_cache[_pair_key] = _krec
                        _save_retrospectives(project_dir, retros_cache)
                for _pf, _pnames in _spliced_by_file.items():
                    _note_kept(_pf, _pnames)
                _cmp_p = last_comparison() if tdd_test_cmd else None
                note_kept_comparison(_cmp_p)
                _record_regression_evidence([_pair_key])
                block_outcomes.append(UnitOutcome(landed=True))
                _pair_passed = (_pair_u2.self_passed
                                if _pair_u2 is not None else frozenset())
                # Two facts: whether the keep is deferred to the sweep (the
                # verdict row — a non-regression keep is), and whether its
                # tests could run (the class — a non-regression keep's did).
                _pair_external_only = (
                    _pair_u2 is not None
                    and _pair_u2.decision is U2Decision.ACCEPT_EXTERNAL_ONLY)
                _pair_deferred = _pair_external_only or (
                    _pair_u2 is not None and _pair_nonregr)
                _attr_p = (
                    (_pair_u2.self_passed | _pair_u2.self_failures
                     | _pair_u2.aborted)
                    if _pair_u2 is not None else frozenset())
                _cls(class_for_kept(reading=keep_reading(
                                        net_delta=(_cmp_p.net_pass_delta
                                                   if _cmp_p is not None else 0),
                                        newly_green=(_cmp_p.newly_green
                                                     if _cmp_p is not None
                                                     else ()),
                                        attributed=_attr_p),
                                    passed=_pair_passed,
                                    unreachable=tests_could_not_run(
                                        external_only=_pair_external_only),
                                    attributed=_attr_p,
                                    measured=(_pair_u2 is not None
                                              and not _pair_u2.aborted)),
                     pair_id, "pair")
                _verdict_row(
                    pair_id,
                    (Disposition.KEEP_DEFERRED if _pair_deferred
                     else Disposition.ACCEPT),
                    (f"pair accepted — {len(_pair_passed)} convicting test(s) "
                     "observed passing" if _pair_passed
                     else "pair kept — compiles, no regression; judged at the "
                     "next sweep"),
                    outcome=(GateOutcome.POSITIVE
                             if _pair_passed and not _pair_deferred
                             else GateOutcome.UNMEASURABLE),
                    evidence=tuple(sorted(_pair_passed)), attempt=1,
                    validity=ValidityVector(binary_completed=True),
                    model_calls=1)
                _log(f"  [phase-c/pair] {pair_id}: FILLED {_n_spliced} fn(s) "
                     "across both halves"
                     + (f" — {len(_pair_passed)} convicting test(s) passing."
                        if _pair_passed else "."))
                _emit("phase_c.pair.fill.done", data={
                    "cluster": cluster_name,
                    "members_filled": _n_spliced,
                    "passed": sorted(_pair_passed)})
                return

    # RED-SLICE SESSION — one transcript over every crate fn a failing test
    # executes, before any per-unit route opens those units alone. The
    # stateless routes rebuilt an 88K-char document per attempt and kept
    # nothing between attempts: run `3003-93e9a9bf` (2026-09-02) tried the
    # one pair that could move its two red tests six times — four times on
    # the same private-field diagnostic — and dispatched the tests' 20-unit
    # coverage slice as per-file prompts it had itself labelled re-buys.
    # The session (`application/reconstruct/slice_session`) gives the model
    # the slice as its editable set, the compiler's and the suite's answers
    # as turns, and the probe's observed values beside every failing
    # assertion; acceptance is one transaction over the whole suite.
    def _make_session_route() -> SessionRoute | None:
        """Plan the packed sessions and return the route that runs them one
        at a time under the fill schedule; None when nothing is planned."""
        served: set[FnLocator] = set()
        if not session_slices or os.getenv("SLICE_SESSION", "1") == "0":
            return None
        _ask_turns = deps.ask_turns
        _max_turns = int(os.getenv("SLICE_SESSION_MAX_TURNS", "8"))
        _max_minutes = float(os.getenv("SLICE_SESSION_MAX_MINUTES", "90"))
        _max_units = int(os.getenv("SLICE_SESSION_MAX_UNITS", "32"))
        _max_pulls_session = int(os.getenv("SLICE_SESSION_MAX_PULLS", "6"))
        _probe_on = deps.run_cargo is not None and os.getenv("ASSERT_PROBE", "1") != "0"
        _coverage_fn = deps.measure_coverage_slice
        _coverage_on = (_coverage_fn is not None
                        and os.getenv("SLICE_SESSION_COVERAGE", "1") != "0")
        _held_now = HeldStubs.of_map(held)

        def _coverage(names: Sequence[str], hung: Sequence[str] = ()
                      ) -> Mapping[str, frozenset[FnSite]]:
            if not _coverage_on or _coverage_fn is None:
                return {}
            hung_map = {}
            for item in hung:
                if "::" in item:
                    lab, test = item.split("::", 1)
                    hung_map[lab] = test
            cov = _coverage_fn(project_dir, cluster_name,
                               failing_tests=frozenset(names),
                               cluster_env=cluster_env, hung=hung_map)
            hung_found = getattr(cov, "hung_tests", None) or ()
            if hung_found:
                _emit("phase_c.coverage.hung", data={
                    "cluster": cluster_name,
                    "hung_tests": sorted(hung_found),
                })
            owners = getattr(cov, "owners", None) or {}
            return {str(t).rsplit("::", 1)[-1]: frozenset(ks)
                    for t, ks in dict(owners).items()}

        _executed_all: frozenset[FnSite] | None = None
        if session_owners is not None:
            _executed_all = (frozenset().union(*session_owners.values())
                             if session_owners else frozenset())

        _cfg_set = cluster_cfg_set(cluster)
        _collisions_seen: set[FnSite] = set()

        def _tree_sites() -> Mapping[FnSite, FnRecord]:
            """The tree's fn records keyed by site, one per container. A
            cfg twin resolves to the arm the cluster compiles; a residual
            collision keeps the first declaration and is logged once."""
            recs = [FnRecord.from_dict(_raw) if isinstance(_raw, dict) else _raw
                    for _raw in _enumerate_fns_via_extractor(target_src_dir) or []]
            found = records_by_site(
                (r for r in recs if not r.is_test and r.fn_name),
                compiled=lambda r: not any(
                    _cfg_set.excludes_for_cargo(p) for p in r.cfg_predicates))
            for _site, _arms in found.collisions:
                if _site in _collisions_seen:
                    continue
                _collisions_seen.add(_site)
                _log(f"  [phase-c/session] {site_label(_site)} is declared "
                     f"{len(_arms)} times on the tree and the cluster cfg cannot "
                     f"choose ({'; '.join(_arms)}) — the first declaration keys "
                     "the site")
            return found.by_site

        def _resolve(units: Iterable[FnSite],
                     recs: Mapping[FnSite, FnRecord]) -> dict[FnSite, FnRecord]:
            """Slice units to records: the exact site first, then the same
            site under the other rooting (`error.rs` vs `src/error.rs`)."""
            def _norm(u: FnSite) -> FnSite:
                return canon_site(FnSite(_norm_src_rel(u.file), u.fn_name, u.container))
            alias = {_norm(site): rec for site, rec in recs.items()}
            out: dict[FnSite, FnRecord] = {}
            for u in units:
                hit = recs.get(u) or alias.get(_norm(u))
                if hit is not None:
                    out[u] = hit
            return out

        def _session_costs(
                slices: Mapping[str, Collection[FnSite]]) -> SliceCosts | None:
            """The turn-0 prompt's size as a function of the packing: what
            each file RENDERS to at each rendering threshold, each test's
            quoted block, and the sections every session carries. Sized
            against `SLICE_SESSION_TURN0_CHARS`, default 45% of the
            single-call limit, so a session leaves its result turns room —
            the 05:23 session of run `334902-2774708c` had 8%.

            The file term calls `render_slice_file`, the same function the
            turn-0 builder calls, once per threshold. It used to charge
            `min(len(text), SESSION_FILE_CHARS)`, which is the threshold at
            which the renderer CONDENSES a file, not a size the renderer
            ever emits: on run `32959-4a2ff1ab` (2026-09-16) `src/ensure.rs`
            was 56,026 raw, rendered to 1,805, and was charged 36,000, so a
            20-unit slice was charged 103,320 for a file term that renders
            to 70,117 — 33,203 over, a quarter of the capacity — and both of
            the cluster's remaining measurable red tests were refused a
            session on every fill of that run's last six hours. Measuring
            the renderer also gives the planner its second lever: the same
            slice rendered at a narrower threshold, which costs it nothing
            it may write.

            The editable set per file is the UNION over every test in
            `slices`; a packed session's own editable set is a subset, so
            the charge is an upper bound on what that session renders —
            conservative in the direction that cannot refuse a session
            that would have fit."""
            limit = int(deps.single_call_char_limit or 0)
            raw_cap = os.getenv("SLICE_SESSION_TURN0_CHARS", "")
            try:
                max_chars = int(raw_cap) if raw_cap else int(limit * 0.45)
            except ValueError:
                max_chars = int(limit * 0.45)
            if max_chars <= 0:
                return None
            file_chars: dict[int, dict[str, int]] = {
                t: {} for t in _SESSION_FILE_THRESHOLDS}
            types_chars: dict[str, int] = {}
            by_file: dict[str, list[FnSite]] = {}
            for us in slices.values():
                for u in us:
                    by_file.setdefault(u.file, []).append(u)
            _tree_recs = list(_all_recs.values())
            for f, sites in by_file.items():
                try:
                    text = (target_src_dir / f).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                # The TYPES_IN_SCOPE section this file's units pull in — the
                # same call the session makes per file when it opens.
                recs = [_all_recs[u] for u in dict.fromkeys(sites) if u in _all_recs]
                for _t in _SESSION_FILE_THRESHOLDS:
                    file_chars[_t][f] = len(render_slice_file(
                        f, text, recs, _tree_recs, file_chars=_t,
                        executed=_executed_all or (),
                        coverage_known=_executed_all is not None))
                if recs and text:
                    types_chars[f] = len(_types_section_for_fix(
                        " ".join(r.sig_text for r in recs), text, "",
                        " ".join(dict.fromkeys(r.container for r in recs)), "",
                        _crate_type_records, file_rel=f))
            test_chars: dict[str, int] = {}
            evidence_chars: dict[str, int] = {}
            for t in slices:
                leaf = t.rsplit("::", 1)[-1]
                blocks = attributed_test_blocks(
                    (leaf,),
                    lambda lf: _find_tests_referencing_fn(
                        tests_dir_files, lf, max_blocks=1),
                    file_cap=_ATTRIBUTED_TEST_FILE_CAP, into=[])
                test_chars[t] = sum(len(txt) for _, txt in blocks)
                _ev = _frontier_tests.evidence_for(t)
                evidence_chars[t] = len(quote_panic_site(_ev, _read_source)) if _ev else 0
            fixed = len(build_slice_session_prompt(
                module_name=module_name, cluster_name=cluster_name,
                slice_=SessionSlice(tests=(), units=()), records={},
                all_records=[], file_texts={}, target_evidence={},
                test_blocks=[], types_section="",
                invariants=invariants_section, macros=macros_section,
                fr_path=fr_path, max_turns=_max_turns,
                # Every distinct crash block, reserved for every slice: the
                # blocks are keyed by unit, not by test, so the fit cannot
                # know which slice carries which; over-reserving costs a
                # little packing, under-reserving a turn 0 over the cap.
                crash_evidence=crash_evidence_for_units(
                    crash_evidence or {}, None),
                # The behaviour instruction's bound, for the same reason:
                # which rules a packed slice carries depends on its units.
                behaviour_must=" " * BEHAVIOUR_MUST_MAX_CHARS))
            return SliceCosts(file_chars=file_chars, test_chars=test_chars,
                              fixed_chars=fixed, max_chars=max_chars,
                              types_chars=types_chars,
                              evidence_chars=evidence_chars)

        _all_recs = _tree_sites()
        _slices = expand_locator_sites(session_slices, _all_recs)
        for _file, _fn, _n in locator_only_expansions(session_slices, _all_recs):
            _log(f"  [phase-c/session] locator-only {_file}::{_fn} "
                 f"expanded to {_n} tree site(s)")
        _costs = _session_costs(_slices)
        _plans = list(plan_slices(_slices, held_tests=_held_now.by_test.keys(),
                                  held_units=_held_now.units,
                                  max_units=_max_units, costs=_costs))
        # The plan is a record on every fill, refusal included: the union
        # the engine actually handed `plan_slices` (owners plus hops plus
        # locator expansion) was in no event before, so the over-union
        # refusals of run `2965-453dd7a4` could only be reconstructed from
        # the owner-map event. `sessions` on the event is what packing
        # produced; whether and when each opens is the fill schedule's call
        # (`phase_c.fill.schedule`), never a withhold on the hollow count —
        # run `334902-2774708c`'s 05:23 session was the only run of the night
        # that moved a verdict, and a count-gated withhold would have skipped
        # it while leaving the routes that burned the night untouched.
        _gaps = slice_plan_gaps(_slices, held_tests=_held_now.by_test.keys(),
                                max_units=_max_units, costs=_costs)
        if session_slices:
            _emit("phase_c.session.plan", data={
                "cluster": cluster_name,
                **slice_plan_payload(_slices, _plans, _gaps, _costs),
                "opened": len(_plans)})
        if _costs is not None and _plans:
            def _size_of(p: SessionSlice, i: int) -> str:
                n = _costs.chars(p.tests, p.units,
                                 file_chars=p.file_chars or None)
                at = ("" if not p.file_chars or p.file_chars == _costs.widest()
                      else f" rendered at {p.file_chars:,}")
                return f"{p.label(seq=i)} {n:,} chars{at}"
            _log("  [phase-c/session] turn-0 size per session: "
                 + ", ".join(_size_of(p, i) for i, p in enumerate(_plans))
                 + f" (capacity {_costs.max_chars:,})")
        if _gaps.fitted_clause():
            _log(f"  [phase-c/session] {_gaps.fitted_clause()}")
        if _gaps.condensed_clause():
            _log(f"  [phase-c/session] {_gaps.condensed_clause()}")
        if not _plans:
            if session_slices:
                _log(f"  [phase-c/session] no slice planned "
                     f"({len(session_slices)} failing test(s) — "
                     f"{_gaps.log_clause()})")
            return None
        _run_cargo = deps.run_cargo

        def _probe(names: Sequence[str]) -> Mapping[str, str]:
            if not _probe_on or _run_cargo is None:
                return {}
            by_file: dict[Path, list[str]] = {}
            for _n in names:
                _leaf = _n.rsplit("::", 1)[-1]
                for _p, _txt in _find_tests_referencing_fn(
                        tests_dir_files, _leaf, max_blocks=1):
                    if f"fn {_leaf}(" not in _txt:
                        continue
                    _fp = Path(_p)
                    if not _fp.is_absolute():
                        _fp = project_dir / _fp
                    by_file.setdefault(_fp, []).append(_leaf)
                    break
            if not by_file:
                return {}
            return run_assert_probes(
                project_dir=project_dir, cluster_root=target_src_dir,
                cluster_name=cluster_name, cluster_env=cluster_env,
                tests_by_file=by_file, run_cargo=_run_cargo, log=_log).rendered

        def _run_one_session(_sl: SessionSlice, clock: RunClock) -> RouteRunReport:
            nonlocal fns_filled_total, impls_filled, impls_failed
            nonlocal _all_recs, served
            _check_graceful_stop("slice-session dispatch")
            if served or attempts.dispatched:
                # An earlier run of this fill (a session, or any route the
                # schedule put before it) may have landed bodies; a packed
                # sibling sharing a hub unit must open on the tree as it
                # now stands, not on the records enumerated at plan time.
                _all_recs = _tree_sites()
            _records = _resolve(_sl.units, _all_recs)
            _missing = [u for u in _sl.units if u not in _records]
            if _missing:
                _log(f"  [phase-c/session] {_sl.label()}: {len(_missing)} unit(s) "
                     "have no fn record on the tree and are left out: "
                     + ", ".join(site_label(u) for u in _missing[:6]))
            if not _records:
                return RouteRunReport(empty=True)
            # The write set's table complement: free fns of a slice file whose
            # signature fits a field of a table an editable unit builds, and
            # that nothing installs yet or that the editable builder's own
            # literal installs. Coverage cannot put either in the slice, and
            # without them an editable builder cannot be written together
            # with the slots it fills.

            def _read_slice_file(f: str) -> str | None:
                try:
                    return (target_src_dir / f).read_text(
                        encoding="utf-8", errors="replace")
                except OSError:
                    return None
            _ws = session_write_set(
                _sl, _records, list(_all_recs.values()),
                _crate_type_records, _read_slice_file)
            _installable_sites = _ws.installable
            if _installable_sites:
                _log(f"  [phase-c/session] {_sl.label()}: "
                     f"{len(_installable_sites)} installable fn(s) admitted to "
                     "the editable set — signature fits a table an editable unit "
                     "builds, and nothing installs it yet or that unit's own "
                     "literal does: " + ", ".join(
                         f"{site_label(u)}->{t}"
                         for u, t in _installable_sites.items()))
                _emit("phase_c.session.installable_admitted", data={
                    "cluster": cluster_name, "tests": list(_sl.tests),
                    "admitted": [{**site_payload(u), "slot": t}
                                 for u, t in _installable_sites.items()]})
            if _ws.coupled:
                _log(f"  [phase-c/session] {_sl.label()}: {len(_ws.coupled)} "
                     "coupled body(ies) admitted to the editable set — they "
                     "sit on one cycle with an editable unit: " + ", ".join(
                         f"{site_label(u)} ({c})"
                         for u, c in _ws.coupled.items()))
                _emit("phase_c.session.coupled_admitted", data={
                    "cluster": cluster_name, "tests": list(_sl.tests),
                    "admitted": [{**site_payload(u), "coupling": c}
                                 for u, c in _ws.coupled.items()]})
            _sl, _records = _ws.slice_, _ws.records
            _units = _sl.units
            _leaves = tuple(dict.fromkeys(t.rsplit("::", 1)[-1] for t in _sl.tests))
            _tblocks = attributed_test_blocks(
                _leaves,
                lambda leaf: _find_tests_referencing_fn(tests_dir_files, leaf, max_blocks=1),
                file_cap=_ATTRIBUTED_TEST_FILE_CAP, into=[])
            _tsources = tuple(t for _, t in _tblocks)
            _spins = pinned_observables_from_tests(_leaves, _tsources)
            _sobs = asserted_output_literals_from_tests(_leaves, _tsources)
            _file_texts = {
                f: (target_src_dir / f).read_text(encoding="utf-8", errors="replace")
                for f in _sl.files}
            _types_parts: list[str] = []
            _bound_parts: list[str] = []
            for _f in _sl.files:
                _frecs = [_records[u] for u in _sl.units if u.file == _f]
                _tsec = _types_section_for_fix(
                    " ".join(r.sig_text for r in _frecs), _file_texts[_f], "",
                    " ".join(dict.fromkeys(r.container for r in _frecs)), "",
                    _crate_type_records, file_rel=_f, observables=_sobs)
                if _tsec and _tsec not in _types_parts:
                    _types_parts.append(_tsec)
                for _cont in dict.fromkeys(r.container for r in _frecs if r.container):
                    _wc = _extract_impl_where_clause(_file_texts[_f], _cont)
                    _bsec = _bound_methods_section_for(
                        _wc, _crate_type_records, file_rel=_f) if _wc else ""
                    if _bsec and _bsec not in _bound_parts:
                        _bound_parts.append(_bsec)
            _initial_obs = _probe(_sl.tests)
            _evidence: dict[str, str] = {}
            for _t in _sl.tests:
                _ev = _frontier_tests.evidence_for(_t)
                _said = quote_panic_site(_ev, _read_source) if _ev else ""
                _obs = _initial_obs.get(_t) or _initial_obs.get(_t.rsplit("::", 1)[-1], "")
                _evidence[_t] = "\n".join(x for x in (_said, _obs) if x)
            _skey = _retro_key(_sl.files[0], "*session*",
                               "+".join(sorted(_sl.member_scope())))
            _srec = retros_cache.get(_skey, {}) or {}
            _prior_notes = ""
            _stored_scope = _srec.get(JOINT_MEMBERS)
            if (isinstance(_stored_scope, list)
                    and frozenset(str(x) for x in _stored_scope) == _sl.member_scope()):
                _prior_notes = str(_srec.get("guidance") or "")
                _guard = pins_for_contradiction_guard(_spins, _sobs)
                if (_prior_notes and _guard
                        and prescription_removes_pinned_observable(
                            _prior_notes, _guard)):
                    _log(f"  [phase-c/session] {_sl.label()}: previous notes "
                         "prescribed removing a pinned observable — withheld")
                    _prior_notes = ""
            _prior_regr = render_prior_regression(
                tuple(str(t) for t in (_srec.get("regressed") or [])),
                _spins,
                {str(k): str(v) for k, v in dict(
                    _srec.get(REGRESSED_OUTPUT) or {}).items()},
                regressed_attempt_of(_srec))
            _bp = ""
            if not _blueprint.empty:
                _bp = _blueprint.slice_for(
                    frozenset(u.fn_name for u in _sl.units)
                    | frozenset(tok for r in _records.values()
                                for tok in _container_names(r.container)),
                    cap=6000)
            _turn0 = build_slice_session_prompt(
                module_name=module_name, cluster_name=cluster_name,
                slice_=_sl, records=_records,
                all_records=list(_all_recs.values()),
                file_texts=_file_texts, target_evidence=_evidence,
                test_blocks=_tblocks,
                types_section="\n\n".join(_types_parts),
                bound_methods="\n\n".join(_bound_parts),
                invariants=invariants_section, macros=macros_section,
                notes=_prior_notes, prior_regression=_prior_regr,
                kind=kind_from_record(_srec),
                blueprint=_bp, fr_path=fr_path,
                max_turns=_max_turns, executed=_executed_all,
                file_chars=_sl.file_chars or SESSION_FILE_CHARS,
                installable=_installable_sites,
                crash_evidence=crash_evidence_for_units(
                    crash_evidence or {}, _sl.units, crash_sites),
                behaviour_must=_behaviour_must(*_records.values()),
                unit_notes=unit_notes_text(
                    (locator_of(u) for u in _sl.units), _frontier_tests))
            _pull_file.file = _sl.files[0]

            def _check_now() -> tuple[int, str, list[Any]]:
                rc, out, diags = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                _last_full_out[0] = out
                return rc, out, list(diags)

            _watch = list(_units)

            def _bodies_now() -> Mapping[FnSite, str]:
                return {u: r.body_text
                        for u, r in _resolve(tuple(_watch), _tree_sites()).items()}

            def _extend_watch(sites: Sequence[FnSite]) -> None:
                for s in sites:
                    if s not in _watch:
                        _watch.append(s)

            # What the transport recorded about each call — reasoning-
            # capped, follow-up fn blocks — read off the rows the call
            # itself records, so the session can tell a capped reply that
            # decided nothing from the model declining.
            _last_call: list[CallCapReading | None] = [None]

            def _ask(msgs: list[dict[str, Any]]) -> str:
                with observe_call() as _rows:
                    _reply = str(_ask_turns(
                        msgs, model=_TASK2_MODEL,
                        followup_think=followup_think_env_on()))
                _last_call[0] = read_call(_rows)
                return _reply

            _ports = SessionPorts(
                ask_turns=_ask,
                splice=_splice_fn_with_where,
                check=_check_now,
                focus_errors=lambda out, f: str(_focus_cargo_errors(out, f)),
                blamed_type_api=lambda text: with_blamed_type_api(
                    text, _crate_type_records),
                same_crash=SameCrashJudge(
                    frozenset(crash_blamed or ()), culprits_of),
                measure_suite=lambda raw: _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env, raw),
                read_source=_read_source,
                enumerate_bodies=_bodies_now,
                extend_body_watch=_extend_watch,
                answer_pulls=make_request_answerer(
                    _pull_sources, log=_log, emit=_emit,
                    max_pulls=_max_pulls_session),
                log=_log, emit=_emit,
                check_graceful_stop=_check_graceful_stop,
                probe=_probe if _probe_on else None,
                implicated=lambda diags: _implicated_keys_from_diags(
                    list(diags), target_src_dir),
                coverage=_coverage if _coverage_on else None,
                may_claim=clock.may_claim, note_delta=clock.note_delta,
                call_reading=lambda: _last_call[0])
            _sid = "session:" + "+".join(_sl.tests)
            with _splice_check_lock, serving_units(
                    f"{u.file}::{u.fn_name}" for u in _sl.units):
                _out = run_slice_session(
                    _ports, target_src_dir=target_src_dir,
                    cluster_name=cluster_name, slice_=_sl, records=_records,
                    turn0=_turn0, max_turns=_max_turns, max_minutes=_max_minutes,
                    max_transcript_chars=int(deps.single_call_char_limit or 0),
                    all_records=list(_all_recs.values()), executed=_slices)
            attempts.note_dispatched(
                locator_of(u) for u in (_out.dispatched | _out.kept))
            attempts.note_reverted(locator_of(u) for u in _out.reverted)
            attempts.note_kept(locator_of(u) for u in _out.kept)
            attempts.note_call(_out.model_calls)
            with _counters_lock:
                if _out.outcome == "accepted":
                    impls_filled += 1
                    fns_filled_total += len(_out.kept)
                elif _out.outcome == "kept":
                    fns_filled_total += len(_out.kept)
                elif _out.outcome == "reverted":
                    impls_failed += 1
            # Only units whose bodies stayed. A no_change, compile-fail, or
            # full-revert session spent GPU and left the tree as it was —
            # the pair and per-fn routes still have a shot this call.
            served |= {locator_of(u) for u in _out.kept}
            if _out.unmeasured:
                # Every landing (if any) came from a reasoning-capped reply:
                # an answer the cap dropped, not a measured refusal. The
                # class and the route row say so; the next schedule ranks
                # the slice as never served, not as a flat re-buy.
                _log(f"  [phase-c/session] {_sl.label()}: {_out.capped_turns} "
                     f"reasoning-capped reply(ies) and no uncapped landing — "
                     "graded UNMEASURED, not a re-buy.")
                _cls(FillOutcomeClass.ANSWER_DROPPED, _sid, "session")
            if _out.outcome == "accepted":
                # The accepted outcome's own delta: every target passing is
                # not a payment when they passed before the session opened.
                _cls(class_for_kept(reading=keep_reading(
                                        net_delta=(_out.final.pass_delta
                                                   if _out.final is not None
                                                   else 0),
                                        newly_green=(_out.final.newly_green
                                                     if _out.final is not None
                                                     else ()),
                                        attributed=frozenset(_sl.tests)),
                                    passed=frozenset(_sl.tests),
                                    unreachable=tests_could_not_run(),
                                    attributed=frozenset(_sl.tests),
                                    measured=True),
                     _sid, "session")
                _verdict_row(
                    _sid, Disposition.ACCEPT,
                    f"session accepted on turn {_out.turns} — every target test "
                    "passes, no regression",
                    outcome=GateOutcome.POSITIVE, attempt=_out.turns,
                    evidence=tuple(_sl.tests), model_calls=_out.model_calls,
                    validity=ValidityVector(binary_completed=True),
                    route="session")
                block_outcomes.append(UnitOutcome(landed=True))
            elif _out.outcome == "kept":
                if not _out.unmeasured:
                    # Same reading note_kept records below: a keep-best
                    # tree at net +5 with a target still red is CASHED,
                    # not WRONG (run 3014175-d98ed09e iter 3 session #1).
                    _cls(class_for_kept(
                        reading=keep_reading(
                            net_delta=(_out.final.pass_delta
                                       if _out.final is not None else 0),
                            newly_green=(_out.final.newly_green
                                         if _out.final is not None else ()),
                            attributed=frozenset(_sl.tests)),
                        passed=(_out.final.green_targets
                                if _out.final is not None else frozenset()),
                        unreachable=tests_could_not_run(),
                        attributed=frozenset(_sl.tests),
                        measured=True),
                         _sid, "session")
                _verdict_row(
                    _sid, Disposition.KEEP_DEFERRED,
                    f"session kept {len(_out.kept)} body(ies) after {_out.turns} "
                    "turn(s) — compiles, no regression, targets still red",
                    outcome=GateOutcome.UNMEASURABLE, attempt=_out.turns,
                    evidence=tuple(_out.newly_red), model_calls=_out.model_calls,
                    route="session")
                block_outcomes.append(UnitOutcome(landed=bool(_out.kept)))
            elif _out.outcome == "reverted":
                if not _out.unmeasured:
                    _cls(FillOutcomeClass.WRONG_BODY, _sid, "session")
                _verdict_row(
                    _sid, Disposition.REVERT,
                    f"session reverted after {_out.turns} turn(s) — the last "
                    f"change set regressed {len(_out.newly_red)} test(s) and no "
                    "subset held",
                    outcome=GateOutcome.NEGATIVE, attempt=_out.turns,
                    evidence=tuple(_out.newly_red), model_calls=_out.model_calls,
                    route="session")
            else:
                if not _out.unmeasured:
                    _cls(FillOutcomeClass.COMPILE_FAIL
                         if _out.compile_failures and _out.compile_failures >= _out.turns
                         else FillOutcomeClass.WRONG_BODY, _sid, "session")
                _verdict_row(
                    _sid, Disposition.REJECT,
                    f"session ended without a kept change after {_out.turns} "
                    f"turn(s) ({_out.compile_failures} compile failure(s))",
                    outcome=GateOutcome.NEGATIVE, attempt=_out.turns,
                    model_calls=_out.model_calls, route="session")
            # The session's own memory: its notes (written by the context that
            # saw both the targets and the regressions), the last reddened set
            # with what it printed, and the member scope the next session on
            # this slice reads it under. Replaces the cold retrospective call
            # for this route.
            with _counters_lock:
                _new_rec = dict(_srec)
                _new_rec.update({
                    "category": "SESSION_NOTES",
                    "guidance": _out.notes or str(_srec.get("guidance") or ""),
                    "signal": "ok",
                    "retrospective_count": int(_srec.get("retrospective_count", 0) or 0) + 1,
                    "error_count": 0,
                    "failing_test_count": sum(
                        1 for t in (_out.final.targets if _out.final else ())
                        if not t.passing),
                    "fully_exhausted": False,
                    "last_error_preview": "",
                    "convicted_by": list(_sl.tests),
                    "regressed": sorted(_out.newly_red),
                    REGRESSED_OUTPUT: dict(_out.newly_red_output),
                    REGRESSED_ATTEMPT: (RegressedAttempt.KEPT.value
                                        if _out.kept else
                                        RegressedAttempt.REVERTED.value),
                    JOINT_MEMBERS: sorted(_sl.member_scope()),
                    "session_outcome": _out.outcome,
                    "session_turns": _out.turns,
                })
                if _out.newly_red and _out.final is not None:
                    _new_rec[LAST_ERROR_KIND] = kind_of_compiled_run(
                        aborted=bool(_out.final.crashed),
                        hung=bool(_out.final.hung)).value
                retros_cache[_skey] = _new_rec
                _save_retrospectives(project_dir, retros_cache)
            if _out.kept and _out.newly_red:
                # The forgiven losses on the UNIT records too: the next
                # file-first / per-impl prompt for a kept unit reads its own
                # key, not the slice's. Outside the lock — the writer takes it.
                _record_regression_evidence(
                    [_retro_key(u.file, u.fn_name, u.container) for u in _out.kept],
                    newly_red=_out.newly_red,
                    newly_red_output=_out.newly_red_output)
            if _out.kept and _out.final is not None:
                note_kept(_out.final.pass_delta,
                          newly_red=tuple(sorted(_out.newly_red)),
                          newly_crashed=tuple(_out.final.crashed),
                          newly_green=_out.final.newly_green)
            return RouteRunReport(
                model_calls=_out.model_calls,
                kept=frozenset(locator_of(u) for u in _out.kept),
                reverted=frozenset(locator_of(u) for u in _out.reverted),
                dispatched=frozenset(locator_of(u) for u in _out.dispatched),
                stopped=_out.outcome == "stopped",
                unmeasured=_out.unmeasured, cap_owned=True,
                first_turn_minutes=_out.first_turn_minutes,
                # Every slice unit with a record was editable and in view;
                # a unit with none (`_missing`) was left out inside the run.
                opened=frozenset(locator_of(u) for u in _records),
                candidate_bodies=dict(_out.candidate_bodies))

        return SessionRoute(_plans, _run_one_session, served, log=_log)

    _session_route = _make_session_route()
    _pair_served: set[FnLocator] = set()
    _pair_jobs = []
    for _pd in pair_dispatches(
            _frontier_tests.pairs, all_blocks,
            extra_locators=_pair_extra_locs,
            crash_named=_crash_named):
        _impl_loc = FnLocator(
            _pd.pair.implementer.file, _pd.pair.implementer.fn_name)
        _pair_test_names: set[str] = set()
        for _loc in _pd.member_locators():
            _pair_test_names |= set(_frontier_tests.tests_for(_loc))
            _pair_test_names |= set(_frontier_tests.convicting_for(_loc))
        _entered = {t for t in _pair_test_names if entered_by_test(_impl_loc, t)}
        _quoted = quote_dispatcher_when_implementer_entered(
            _pd, pair_tests=_pair_test_names, entered=_entered)
        if extra_write_members(_pd.dispatcher_block) and (
                not extra_write_members(_quoted.dispatcher_block)):
            _log(f"  [phase-c/pair] {_impl_loc.file}::{_impl_loc.fn_name} "
                 "every leftover of the pair enters the implementer — "
                 "dispatcher quoted, not rewritten.")
        _pair_jobs.append(_quoted)
    _stubs_all: frozenset[FnLocator] = frozenset(
        _scan_unimplemented_for_cluster(target_src_dir, cluster).units)
    _hollow = HeldStubs.of_map(held).hollow()
    _pair_trim_logged = [False]
    _ff_trim_logged = [False]

    def _tests_for(locs: Iterable[FnLocator]) -> frozenset[str]:
        out: set[str] = set()
        for loc in locs:
            for t in _frontier_tests.tests_for(loc):
                out.add(str(t).rsplit("::", 1)[-1])
        return frozenset(out)

    def _snapshot() -> RouteSnap:
        with _counters_lock:
            return RouteSnap(units=attempts.snapshot(), classes=_classes_of_run())

    def _report_since(snap: RouteSnap) -> RouteRunReport:
        """This run's report: the units, calls and classes stamped with
        the run label bound on this thread (the executor binds it around
        `route.run`, on the worker that runs it), read off the attempt
        ledger since `snap`. A wave of concurrent file or impl runs shares
        one tick stream, so an unlabelled window would carry every
        sibling's landings onto each row; the label is what keeps a row
        to its own run."""
        now = _snapshot()
        delta = FillAttempts.since(snap.units, now.units, current_run_label())
        before = snap.classes.as_dict()
        after = now.classes.as_dict()
        classes = {k: after[k] - before.get(k, 0) for k in after
                   if after[k] - before.get(k, 0)}
        report = RouteRunReport.from_delta(delta, classes)
        bodies = attempts.remembered_bodies(delta.reverted)
        if bodies:
            report = replace(report, candidate_bodies=bodies)
        return report

    def _blocks_after_pair() -> list[ImplBlock]:
        if not _pair_served:
            return list(blocks)
        trimmed, dropped = blocks_after_pair_fill(blocks, frozenset(_pair_served))
        if dropped and not _pair_trim_logged[0]:
            _pair_trim_logged[0] = True
            _log(f"  [phase-c/impl] dropped {len(dropped)} pair-served "
                 "refill target(s) from the remaining routes this call "
                 "(next sweep judges the joint write): " + ", ".join(dropped))
            _emit("phase_c.impl.refill_deferred_after_pair", data={
                "cluster": cluster_name, "dropped": sorted(dropped)})
        return trimmed

    def _viable_now() -> list[ImplBlock]:
        """The per-impl route's blocks as the tree stands: the viable set
        minus pair-served refills minus refill-only blocks of files the
        file route already designed this call."""
        out = list(viable_blocks)
        if _pair_served:
            out, _d = blocks_after_pair_fill(out, frozenset(_pair_served))
        served_files = set(ff_produced_files)
        if served_files:
            out, dropped_ff = blocks_after_file_first(out, served_files)
            if dropped_ff and not _ff_trim_logged[0]:
                _ff_trim_logged[0] = True
                ids = [f"{b.file}::{b.container}" for b in dropped_ff]
                _log(f"  [phase-c/impl] dropped {len(dropped_ff)} "
                     "refill-only block(s) after file-first served their "
                     "file this call (next sweep judges them): " + ", ".join(ids))
                _emit("phase_c.impl.refill_deferred_after_file_first", data={
                    "cluster": cluster_name, "dropped": ids, "kept": len(out)})
        return out

    def _file_units_now() -> list[tuple[str, list[ImplBlock]]]:
        if os.getenv("PHASE_C_FILE_FIRST", "1") == "0":
            return []
        return file_route_units(
            _blocks_after_pair(), max_stubs=_PER_IMPL_MAX_STUBS,
            file_primary=_file_primary)

    def _file_plan_logged(units: list[tuple[str, list[ImplBlock]]]) -> None:
        if _file_primary and units:
            _src = "env" if (_route_env or "").strip() else (
                f"model default for {_TASK2_MODEL}")
            _log(f"  [phase-c/file] route order file_primary ({_src}): "
                 f"{len(units)} file unit(s) "
                 f"({sum(len(bs) for _, bs in units)} block(s), "
                 "free fns and single-block files included).")
        _emit("phase_c.fill.route_plan", data={
            "cluster": cluster_name, "route": "file",
            "units": len(units),
            "blocks": sum(len(bs) for _, bs in units),
            "order": str(_route_order)})

    _pair_route = PairRoute(
        _pair_jobs, fill_pair=_fill_pair, pair_served=_pair_served,
        session_served=lambda: (_session_route.served
                                if _session_route is not None else set()),
        snapshot=_snapshot, report_since=_report_since,
        tests_for=_tests_for, stubs=_stubs_all, log=_log)
    _file_route = FileRoute(
        units_now=_file_units_now, fill_file=_fill_file_first,
        admits=lambda f, bs: _file_route_admits(
            f, bs, target_src_dir=target_src_dir,
            frontier_tests=_frontier_tests,
            per_impl_max_stubs=_PER_IMPL_MAX_STUBS, log=_log),
        snapshot=_snapshot, report_since=_report_since,
        tests_for=_tests_for, stubs=_stubs_all,
        on_plan=_file_plan_logged, log=_log)
    _impl_route = ImplRoute(
        blocks_now=_viable_now, fill_block=_fill_one_block,
        snapshot=_snapshot, report_since=_report_since,
        tests_for=_tests_for, stubs=_stubs_all, log=_log)

    def _bodies_sha(targets: frozenset[FnLocator]) -> str:
        return bodies_sha_on_tree(targets, (
            FnRecord.from_dict(_raw) if isinstance(_raw, dict) else _raw
            for _raw in _enumerate_fns_via_extractor(target_src_dir) or []))

    def _finish() -> ClusterFillResult:
        if _retro_gate.total:
            _log(f"  [phase-c/impl] cluster {cluster_name}: {_retro_gate.render()}")
        _payoff = assess_pass_payoff(block_outcomes)
        if _payoff.unjudgeable_rate == 1.0:
            _pay_data = {"cluster": cluster_name, "route": "impl",
                         **_payoff.event_data()}
            if _payoff.is_zero:
                _log(f"  [phase-c/impl] cluster {cluster_name}: PAYOFF ZERO — "
                     f"{_payoff.summary}.")
                _emit("phase_c.pass.payoff_zero", data=_pay_data)
            else:
                _log(f"  [phase-c/impl] cluster {cluster_name}: UNJUDGEABLE — "
                     f"{_payoff.rate_summary}.")
                _emit("phase_c.pass.unjudgeable", data=_pay_data)
        bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
        remaining_records = _scan_unimplemented_records_for_cluster(target_src_dir, cluster)
        remaining_n = (len({(r["file"], r["fn_name"]) for r in remaining_records})
                       if remaining_records is not None
                       else _scan_unimplemented_for_cluster(
                           target_src_dir, cluster).unit_count)
        return ClusterFillResult(
            fns_filled=fns_filled_total,
            bytes_filled=max(0, bytes_after - bytes_before),
            attempts=attempts.calls,
            errors=tuple(errors),
            remaining=remaining_n,
            impls_filled=impls_filled,
            impls_failed=impls_failed,
            dispatched_fns=tuple(sorted(attempts.dispatched)),
            reverted_fns=tuple(sorted(attempts.reverted)),
            answer_lost_fns=tuple(sorted(attempts.lost)),
            payoff=_payoff,
            fill_classes=_fill_classes.histogram(),
        )

    routes: list[FillRoute] = []
    if _session_route is not None:
        routes.append(_session_route)
    routes.extend((_pair_route, _file_route, _impl_route))
    return PreparedFill(routes=tuple(routes), hollow=_hollow, finish=_finish,
                        bodies_sha=_bodies_sha, landed=lambda: fns_filled_total,
                        crash_callees=_other_callees)


def fill_impl_blocks(
    deps: ImplFillDeps,
    *args: Any,
    fill_budget_minutes: float = 0.0,
    fill_iteration: int = 0,
    run_id: str = "",
    route_ledger: RouteLedger | None = None,
    **kw: Any,
) -> ClusterFillResult:
    """Prepare the four impl-path routes, execute them under the fill
    schedule, and return the result — the legacy single-call shape. The
    engine (`phase_c_engine.PhaseCFillEngine.run`) calls `prepare_impl_fill`
    itself so the per-fn route is scheduled beside these four."""
    prepared = prepare_impl_fill(deps, *args, **kw)
    if not prepared.routes:
        return prepared.finish()
    project_dir = kw.get("project_dir") or (args[0] if args else None)
    execute_fill(
        prepared.routes, hollow=prepared.hollow, bodies_sha=prepared.bodies_sha,
        deps=FillExecutionDeps(
            log=deps.log, emit=deps.emit, check_stop=deps.check_graceful_stop,
            ledger=route_ledger,
            project=(Path(project_dir).name if project_dir is not None else ""),
            cluster=str(kw.get("cluster_name", "")), run_id=run_id,
            iteration=fill_iteration, oracle_fp=str(kw.get("oracle_fp", "")),
            budget_minutes=fill_budget_minutes,
            scheduled=os.getenv("PHASE_C_FILL_SCHEDULE", "1") != "0",
            parallelism=int(kw.get("parallelism", 1) or 1),
            unmeasurable_tests=frozenset(kw.get("unmeasurable_tests") or ()),
            crash_blamed=(kw.get("crash_sites") or CrashSites()).blame_map(
                kw.get("crash_blamed") or ()),
            attributed=frozenset(kw.get("attributed") or ()),
            unit_evidence=kw.get("unit_evidence"),
            on_wave_kept=kw.get("on_wave_kept"),
            premise=kw.get("fill_premise"),
            ground_now=kw.get("fill_ground_now")))
    return prepared.finish()


class _FileFillPlan(NamedTuple):
    """What the file-first route would ask for one file, under the fn bound.

    `groups` is None when the whole file fits one prompt, an empty list when
    the route can open nothing on it (over the bound and no test-welded
    multi-block group under it), else the welded groups as block indexes.
    `wanted` is the fn count the bound was read against, `sizes` the fn
    count per group, `solo` the uncoupled blocks left to the per-block
    loop, `stranded` the rendered blocks no route covers."""

    wanted: int
    max_fns: int
    groups: list[list[int]] | None
    sizes: tuple[int, ...]
    solo: int
    stranded: tuple[str, ...]


def _file_fill_plan(
    file_rel: str, fblocks: list[ImplBlock], *,
    frontier_tests: FailureFrontier, per_impl_max_stubs: int,
) -> _FileFillPlan:
    """What the file-first route would ask for `file_rel` — the ONE
    reading of the fn bound, taken at plan time by `_file_route_admits`
    and again by `_fill_file_first` when the run opens."""
    wanted_all = [m.fn_name for b in fblocks
                  for m in (*b.stub_members, *b.refill_members)]
    # Bounds how many fns one prompt asks for; counts re-fills too — they
    # are bodies the model must write. The stubs-era env var is still
    # honoured so an existing .env keeps working.
    max_fns = int(os.getenv("PHASE_C_FILE_MAX_FNS")
                  or os.getenv("PHASE_C_FILE_MAX_STUBS") or "20")
    if len(wanted_all) <= max_fns:
        return _FileFillPlan(wanted=len(wanted_all), max_fns=max_fns,
                             groups=None, sizes=(), solo=0, stranded=())
    # Welding is keyed on CONVICTING tests: the coupling claim is "one
    # validation run judges both blocks", which only a test that can
    # actually reject them supports. An inferred attribution welds
    # unrelated blocks into one oversized joint prompt.
    _binputs = [BlockGroupInput(
        idx=_i,
        fn_count=len(b.stub_members) + len(b.refill_members),
        tests=frozenset(
            t for m in (*b.stub_members, *b.refill_members)
            for t in (frontier_tests.convicting_for(
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
                if not impl_route_covers(b, max_stubs=per_impl_max_stubs)}
    # A singleton group runs only when it is one of those. Otherwise it is
    # exactly the per-block route's unit and running it in both routes
    # doubles its attempts. A single block larger than the bound keeps the
    # old skip semantics either way.
    _joint = [g for g in _packed
              if (len(g) >= 2 or _orphans.intersection(g))
              and sum(_binputs[i].fn_count for i in g) <= max_fns]
    _stranded = sorted(_orphans - {i for g in _joint for i in g})
    return _FileFillPlan(
        wanted=len(wanted_all), max_fns=max_fns, groups=_joint,
        sizes=tuple(sum(_binputs[i].fn_count for i in g) for g in _joint),
        solo=len(_packed) - len(_joint),
        stranded=tuple(
            f"{fblocks[i].container.splitlines()[0] if fblocks[i].container else 'free fns'}"
            f" ({_binputs[i].fn_count} fn(s))" for i in _stranded))


def _file_route_admits(
    file_rel: str, fblocks: list[ImplBlock], *,
    target_src_dir: Path, frontier_tests: FailureFrontier,
    per_impl_max_stubs: int, log: Callable[[str], None],
) -> bool:
    """Whether the file-first route can OPEN a run on `file_rel` now.

    The fn bound used to be read only inside `_fill_file_first`, after
    the schedule had already ranked the run: on the nightly tree of run
    `2131920-86818224` (2026-09-07) `src/error.rs` held 21 fns against
    `PHASE_C_FILE_MAX_FNS=20` with no test-welded group under it, so
    every fill's schedule line opened with `1. file src/error.rs
    (never_served; 14 stub(s))`, the route returned EMPTY at 0.0 min,
    and `_rank_of` — which skips EMPTY rows by contract — ranked the
    same identity never-served again on the next fill. A run the route
    cannot open is not a candidate; the bound is a plan-time fact and
    is read here, once per plan, so the schedule never sees it."""
    if not (target_src_dir / file_rel).exists():
        return False
    plan = _file_fill_plan(
        file_rel, fblocks, frontier_tests=frontier_tests,
        per_impl_max_stubs=per_impl_max_stubs)
    if not plan.wanted:
        return False
    if plan.stranded:
        # Nothing downstream will attempt these; say so where it is read,
        # rather than let the count above imply the per-block loop has them.
        log(f"  [phase-c/file] {file_rel}: {len(plan.stranded)} block(s) "
            "that NO route covers exceed the bound alone and are left "
            "unfilled: " + ", ".join(plan.stranded)
            + f" — raise PHASE_C_FILE_MAX_FNS (now {plan.max_fns}) to "
            "reach them.")
    if plan.groups is not None and not plan.groups:
        log(f"  [phase-c/file] {file_rel}: skipped — {plan.wanted} "
            f"fn(s) exceeds PHASE_C_FILE_MAX_FNS={plan.max_fns} and no "
            "test-welded multi-block group fits the bound; not "
            "scheduled — per-block fills will handle the blocks that "
            "route covers.")
        return False
    return True
