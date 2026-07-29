"""Per-impl-block Phase-C fill engine (P4) — relocated from pipeline.py.

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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.cargo_errors import ecodes_in_text, focus_diagnostics
from pipeline_core.application.reconstruct.cluster_membership import (
    collect_impl_refill_targets,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    ask_with_pulls,
    workspace_file_reader,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.blueprint import Blueprint, blueprint_path, parse_blueprint
from pipeline_core.domain.dispatch import impl_route_covers
from pipeline_core.domain.fill_groups import BlockGroupInput, coupled_fill_groups
from pipeline_core.domain.identity import ContainerId, RetroKey
from pipeline_core.domain.impl_routing import (
    containers_in_file,
    refill_authorized_names,
    route_fn_bodies,
    trait_default_hosts,
)
from pipeline_core.domain.impl_routing import (
    norm_src_rel as _norm_src_rel,
)
from pipeline_core.domain.llm_output import extract_fn_bodies
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
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refine import (
    CompilingSalvage,
    FixProgress,
    FloorStall,
    best_progress_fields,
    best_progress_of,
)
from pipeline_core.domain.retro_gate import (
    ConvictionEvidence,
    ConvictionStanding,
    RetroGateTally,
    RetroGateVerdict,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
    judge_guidance,
    stamp_conviction,
)
from pipeline_core.domain.rust_idents import canon_container, signature_fixed_by_trait
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.test_report import (
    JointImprovement,
    U2Decision,
    U2TestGate,
    U2Verdict,
    assess_joint_improvement,
)
from pipeline_core.domain.test_results import (
    RegressionComparison,
    TestStateReading,
    strip_ansi,
)
from pipeline_core.prompts.context import PerImplSections
from pipeline_core.prompts.fix_prompts import (
    BlockFillPlan,
    build_file_fill_prompt,
    build_maximal_impl_prompt,
)
from pipeline_core.prompts.pull_menu import pull_menu_section


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
    enumerate_types_via_extractor: Callable[..., list[TypeRecord] | None]
    extract_crate_idioms: Callable[..., str]
    extract_impl_where_clause: Callable[..., str]
    filter_model_notes_for_prompt: Callable[..., str]
    find_tests_referencing_fn: Callable[..., list[tuple[str, str]]]
    focus_cargo_errors: Callable[..., str]
    frontier_from_diags: Callable[..., Frontier]
    impl_block_id: Callable[..., ContainerId]
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
    tdd_frontier_tests: FailureFrontier | None = None,  # Plan 10 U2
    tdd_test_cmd: str | None = None,                                       # Plan 10 U2
    parallelism: int = 1,
    crash_evidence: dict[FnLocator, str] | None = None,
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
    # P4 rebind-prelude — collaborators localised so the 8 nested closures stay byte-identical.
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
    _enumerate_types_via_extractor = deps.enumerate_types_via_extractor
    _extract_crate_idioms = deps.extract_crate_idioms
    _extract_impl_where_clause = deps.extract_impl_where_clause
    _filter_model_notes_for_prompt = deps.filter_model_notes_for_prompt
    _find_tests_referencing_fn = deps.find_tests_referencing_fn
    _focus_cargo_errors = deps.focus_cargo_errors
    _frontier_from_diags = deps.frontier_from_diags
    _impl_block_id = deps.impl_block_id
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

    target_src_dir.mkdir(parents=True, exist_ok=True)
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

    blocks = _collect_impl_blocks_for_cluster(target_src_dir, cluster) or []
    if not blocks:
        return ClusterFillResult()

    # debt #46 point 2 — stamp `refill_members` onto blocks that have an
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

    # Crate-level context (shared by every block we visit).
    _src_for_macros = target_src_dir / "src" if (target_src_dir / "src").exists() else target_src_dir
    _macro_invariants = _compute_cluster_macro_invariants(_src_for_macros, cluster)
    _no_std_invariant = _detect_crate_no_std_invariant(target_src_dir)
    invariants_section = "\n\n".join(
        s for s in (_no_std_invariant, _macro_invariants) if s)
    _crate_type_records = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(target_src_dir) or [], cluster)
    macros_section = _collect_crate_macros(_src_for_macros)

    # P3: the crate blueprint — the model's own design doc, sliced per block
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

    # P2 pull protocol: the model may request ONE typed artifact instead of
    # guessing (bounded round-trips; menu appended to every fill prompt).
    # All sources are post-strip-legal: the reconstruction tree, the tests,
    # the enumerated type records, the last cargo output.
    _pulls_on = os.getenv("CONTEXT_PULLS", "1") != "0"
    _max_pulls = int(os.getenv("CONTEXT_PULLS_MAX", "2"))
    _last_full_out: list[str] = [""]  # set by _splice_and_verify per check
    _pull_sources = PullSources(
        read_file=workspace_file_reader(target_src_dir),
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

    def _ask_fill(prompt: str) -> str:
        """The shared ask seam: pull round-trips when enabled, plain ask
        otherwise. Deliverable-first — FN bodies short-circuit."""
        if not _pulls_on:
            return str(ask(prompt, model=_TASK2_MODEL))
        return ask_with_pulls(
            lambda p: str(ask(p, model=_TASK2_MODEL)), prompt + _pull_menu,
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
        """P5 file-first pass: ONE prompt fills every stub of a multi-block
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
        # Only multi-block groups run here: a lone uncoupled block is exactly
        # the per-block route's unit, and running it in both routes doubles
        # its attempts. A single block larger than the bound keeps the old
        # skip semantics.
        _joint = [g for g in _packed
                  if len(g) >= 2
                  and sum(_binputs[i].fn_count for i in g) <= max_fns]
        _solo = len(_packed) - len(_joint)
        if not _joint:
            _log(f"  [phase-c/file] {file_rel}: skipped — {len(wanted_all)} "
                 f"fn(s) exceeds PHASE_C_FILE_MAX_FNS={max_fns} and no "
                 "test-welded multi-block group fits the bound; per-block "
                 "fills will handle it.")
            return
        _sizes = ", ".join(
            str(sum(_binputs[i].fn_count for i in g)) for g in _joint)
        _log(f"  [phase-c/file] {file_rel}: {len(wanted_all)} fn(s) exceeds "
             f"PHASE_C_FILE_MAX_FNS={max_fns} — split into {len(_joint)} "
             f"test-welded group(s) of {_sizes} fn(s) "
             f"(+{_solo} uncoupled block(s) → per-block loop).")
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
        snapshot = file_path.read_text(encoding="utf-8", errors="replace")
        test_blocks: list[tuple[str, str]] = []
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
        notes_section = ""
        if fr_path is not None:
            notes_section = _filter_model_notes_for_prompt(
                fr_path, file_rel=file_rel, fn_name=wanted[0],
                file_text=snapshot, current_error="")
        # Any localized crash inside THIS file applies: the prompt renders the
        # whole file, so a crashing body is visible as an idiom to imitate.
        _file_crash = "\n\n".join(dict.fromkeys(
            txt for loc, txt in (crash_evidence or {}).items()
            if loc.file == file_rel))
        prior_error = ""
        for _attempt in range(1, _FILE_FIRST_MAX_ATTEMPTS + 1):
            prompt = build_file_fill_prompt(
                file_rel=file_rel, file_text=snapshot,
                fill_plan=[BlockFillPlan(
                    b.container,
                    tuple(m.fn_name for m in b.stub_members),
                    tuple(m.fn_name for m in b.refill_members))
                    for b in fblocks],
                tests_block="\n\n".join(f"// {p}\n{t}" for p, t in test_blocks),
                blueprint=_bp_slice, notes=notes_section, prior_error=prior_error,
                crash_evidence=_file_crash)
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
                parsed = extract_fn_bodies(_ask_fill(prompt))
            except Exception as e:
                _log(f"  [phase-c/file] {file_rel}: LLM error: {e}")
                return
            if not parsed:
                _log(f"  [phase-c/file] {file_rel}: empty/no FN blocks — "
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
                _baseline_test = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env)
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
                routing = route_fn_bodies(
                    parsed, target=containers[0],
                    related=containers[1:] + _extra_containers,
                    trait_defaults=trait_default_hosts(all_blocks, file_rel))
                for _db in routing.dropped:
                    _log(f"  [phase-c/file] {file_rel}: dropped `{_db.fn_name}` — "
                         f"block header does not match any impl in this file. "
                         f"raw_block={_db.block_attr!r} raw_canon={_db.attr_canon!r}")
                spliced_now = 0
                _landed: list[tuple[str, str]] = []
                # Refill targets are already-filled-but-WRONG bodies; the splice
                # must be told to overwrite a non-stub for exactly those names,
                # or the model's correction is refused `target_not_stub` and
                # silently dropped. Authorization spans the whole file because
                # this route routes bodies file-wide.
                _refill_names = refill_authorized_names(all_blocks, file_rel)
                for rb in routing.routed:
                    wc = rb.fn_data["where_clause"]
                    if wc and (rb.via_trait_default
                               or signature_fixed_by_trait(rb.container)):
                        wc = None
                    _fn = rb.fn_data["fn_name"]
                    ok, reason, _delta = _splice_fn_with_where(
                        file_path, rb.container, _fn,
                        rb.fn_data["body"], wc,
                        allow_nonstub=_fn in _refill_names)
                    if ok:
                        spliced_now += 1
                        _landed.append((_fn, rb.container))
                        if rb.via_trait_default:
                            _log(f"  [phase-c/file] {file_rel}: `{_fn}` was "
                                 f"tagged {rb.fn_data['block']!r} (an empty "
                                 "override) — routed to its default body in "
                                 f"`{rb.container}`.")
                        if reason == "ok_xml_unescaped":
                            _log(f"  [phase-c/file] {file_rel}: `{_fn}` landed "
                                 "only after resolving XML entities in the body "
                                 "— the reply used the escaped rendering.")
                    else:
                        # `target_not_stub` is unfalsifiable from the log alone —
                        # the body is reverted before anyone reads it. Name the
                        # container it routed to and whether an EARLIER body in
                        # this same reply already filled the fn, which is the
                        # difference between a duplicate answer and a stale
                        # stub record.
                        _dup = [c for n, c in _landed if n == _fn]
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
                # while the other is a stub. `route_fn_bodies` only matches impl
                # containers, so free-fn bodies land in `routing.dropped`. Route
                # each dropped body whose name matches a still-stubbed FREE fn
                # in THIS file, disambiguating by the stub's verbatim signature
                # (a free fn has no impl container to narrow on).
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
                            _log(f"  [phase-c/file] {file_rel}: routed free fn "
                                 f"`{_p['fn_name']}` to its module-level stub.")
                        else:
                            _log(f"  [phase-c/file] {file_rel}: free-fn splice "
                                 f"`{_p['fn_name']}` failed: {_reason}")
                if spliced_now == 0:
                    _log(f"  [phase-c/file] {glabel}: nothing spliced — "
                         "falling back to per-block fills.")
                    return
                rc, cargo_out, _diags = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                if rc != 0:
                    _rep = repair_missing_imports(
                        _run_cargo_check_diag, check_cmd, target_src_dir,
                        cluster_env or {}, file_rel, _diags, _log,
                        "[phase-c/file]")
                    if _rep is not None:
                        rc, cargo_out, _diags = _rep
                _u2_file: U2Verdict | None = None
                if rc == 0:
                    # Stubs AND re-fills. A behavioral re-fill block has no stub
                    # left, so keying this gate on stubs alone hands an all-refill
                    # file an EMPTY test set — and an empty set is an
                    # unconditional ACCEPT, meaning the failing tests the re-fill
                    # exists to fix are never run and the bodies are kept on
                    # compile alone.
                    _unit_fns = frozenset(
                        FnLocator(file_rel, m.fn_name)
                        for b in fblocks
                        for m in (*b.stub_members, *b.refill_members))
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
                        for m in (*b.stub_members, *b.refill_members))
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
                if _u2_reject and _u2_file is not None and _baseline_test is not None:
                    _jb = assess_joint_improvement(
                        frozenset(_file_tests),
                        _baseline_test.passed_names,
                        _baseline_test.ignored_names,
                        _u2_file)
                    _bank = _jb if _jb.banks else None
                _regr_file = (_test_regression_gate(
                    tdd_test_cmd, target_src_dir, cluster_env, _baseline_test)
                    if rc == 0 else None)
                if rc == 0 and (not _u2_reject or _bank is not None) and _regr_file is None:
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
                    return
                file_path.write_text(snapshot, encoding="utf-8")
                if _regr_file is not None:
                    _revert_reason = f"regressed sibling test(s): {_regr_file[0].summary}"
                    prior_error = _regr_file[1]
                elif rc == 0:
                    _revert_reason = "U2 test failure (no strict improvement)"
                    prior_error = _u2_file.detail if _u2_file is not None else ""
                else:
                    _revert_reason = "compile failed"
                    prior_error = _focus_cargo_errors(cargo_out, file_rel)
                _more = _attempt < _FILE_FIRST_MAX_ATTEMPTS
                _log(f"  [phase-c/file] {glabel}: reverted "
                     f"({_revert_reason}) — "
                     + ("retrying file-first once more."
                        if _more else "falling back to per-block fills."))

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
        _poisoned_retro = _retro_gate.record(judge_guidance(
            _retro_rec.get("guidance", ""), _cur_text,
            anchor_types=_block_anchors)) is RetroGateVerdict.RETIRED
        if _poisoned_retro:
            _log(f"  [phase-c/impl] {block_id}: retro guidance contradicts the "
                 "current layout — guidance withheld, exhaustion not honoured "
                 "(layout-contradicted).")
        # A verdict earned in another context (other cluster's layout, the
        # absolute acceptance gate, the pre-group route set) sampled a
        # different problem — it cannot convict this block. Neither can one
        # drawn from tests that no longer convict these members.
        _block_evidence = ConvictionEvidence(_convicting_for_block(block))
        _standing = conviction_standing(
            _retro_rec, RetroProvenance.current(cluster_name, _cur_text),
            _block_evidence)
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
        # debt #46 point 2: refill members need their OWN failing test(s) and
        # blueprint/notes context surfaced too, not just the stubs'.
        refill_fn_names = [m.fn_name for m in block.refill_members]
        _fill_target_names = stub_fn_names + refill_fn_names
        test_blocks: list[tuple[str, str]] = []
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
        # P3: this block's blueprint slice — ranked by overlap with the
        # stub/refill names, the container tokens, and the file stem.
        _blueprint_slice = "" if _blueprint.empty else _blueprint.slice_for(
            frozenset([*_fill_target_names, *_container_names(block.container),
                       Path(block.file).stem]),
            cap=int(os.getenv("BLUEPRINT_SLICE_CAP", "2500")))
        # Judged against THIS block's file text — the retro store is shared by
        # clusters with different private layouts, so guidance naming a field
        # this layout lacks is withheld rather than shown (it would steer every
        # attempt into the same E0609/E0560 on the field that does not exist).
        prior_retro = admissible_guidance(
            retros_cache.get(_retro_key(block.file, "*impl*", block.container), {}),
            snapshot, evidence=_block_evidence,
            anchor_types=anchor_types_for_container(block.container))
        notes_section = ""
        if fr_path is not None:
            notes_section = _filter_model_notes_for_prompt(
                fr_path, file_rel=block.file,
                fn_name=_fill_target_names[0] if _fill_target_names else "",
                file_text=snapshot, current_error="",
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
            # Slice 8a: TYPES_IN_SCOPE re-selected each attempt from the
            # member sigs + impl header + where clause + the current error
            # (Plan 9 F4: E0277/E0599/E0046/E0053 routinely name a trait
            # the impl block's signatures don't mention).
            _types_section_eff = _types_section_for_fix(
                _member_sigs_text, "", last_error_excerpt or "",
                _block_container, _block_where, _crate_type_records)
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
                    bound_methods=_bound_methods_section_for(
                        _block_where, _crate_type_records)),
                fr_path=fr_path,
            )
            try:
                response = _ask_fill(prompt)
            except Exception as e:
                _log(f"  [phase-c/impl] {block_id}: LLM error: {e}")
                return FixOutcome("stop")
            last_response = response
            parsed = extract_fn_bodies(response)
            if not parsed:
                _log(f"  [phase-c/impl] {block_id}: empty/no FN blocks "
                     f"(attempt {fix_attempt}).")
                return FixOutcome("stop")
            return _splice_and_verify(parsed, fix_attempt)

        def _splice_and_verify(parsed: list[dict[str, Any]],
                               fix_attempt: int) -> FixOutcome:
            """Route → splice → cargo-verify one parsed reply. Shared by the
            budgeted attempts and the P6 maximal last-resort attempt."""
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
                _baseline_test = _test_regression_baseline(
                    tdd_test_cmd, target_src_dir, cluster_env)
                # Splice each parsed body into the impl indicated by its
                # `block` attribute. Match against TARGET container OR any
                # related container. Route by canonical impl-header identity
                # (exact first) — a multi-line where-clause container must
                # match the model's single-line `block` attribute.
                routing = route_fn_bodies(
                    parsed, target=block.container,
                    related=[rb.container for rb in _route_candidates],
                    trait_defaults=trait_default_hosts(blocks, block.file))
                routed: list[tuple[dict[str, Any], str, bool]] = [
                    (rb.fn_data, rb.container, rb.via_trait_default)
                    for rb in routing.routed]
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
                for fn_data, container, _via_default in routed:
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
                    )
                    if ok:
                        spliced_now += 1
                        if reason == "ok_xml_unescaped":
                            _log(f"  [phase-c/impl] {block_id}: "
                                 f"`{fn_data['fn_name']}` landed only after "
                                 "resolving XML entities in the body — the "
                                 "reply used the escaped rendering.")
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
                    _log(f"  [phase-c/impl] {block_id}: nothing spliced "
                         f"(attempt {fix_attempt}).")
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
                    # Plan 10 U2 (shared gate): accept the block only if its
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
                        _u2_impl)
                    _bank_impl = _jb if _jb.banks else None
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
                    tdd_test_cmd, target_src_dir, cluster_env, _baseline_test)
                    if rc == 0 else None)
                if _regr is not None:
                    _cmp, _regr_detail = _regr
                    file_path.write_text(snapshot, encoding="utf-8")
                    _log(f"  [phase-c/impl] {block_id}: REVERTED {spliced_now} "
                         f"fn(s) — regressed sibling test(s): "
                         f"{_cmp.summary} (attempt {fix_attempt}).")
                    last_error_excerpt = _regr_detail
                    _rsig = _cmp.signature
                    last_metric = FixProgress(0, max(1, len(_rsig)))
                    # rc==0 got us here, so the block COMPILES — its regression
                    # is a test-tier loss, never a compile-tier one.
                    return FixOutcome("failed", err_sig=_rsig,
                                      metric=FixProgress(
                                          0, max(1, len(_rsig))))
                if rc == 0 and (not _u2_impl_reject or _bank_impl is not None):
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
                    _log(f"  [phase-c/impl] {block_id}: "
                         f"FILLED {spliced_now} fn(s) "
                         f"(attempt {fix_attempt})"
                         + (f" — banked improvement: {_bank_impl.summary}."
                            if _bank_impl is not None else "."))
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
                # debt #46 point 4: B3 keeps ONLY while the crate is RED (rc!=0),
                # its actual purpose — accumulating prerequisites of a web that
                # can't yet compile. When rc==0 the crate compiles and tests CAN
                # run, so reaching here means U2 FAILED (own tests red): keeping a
                # green-but-behaviorally-wrong body just masks it as "filled".
                # Fall through to revert (→ stub) so the per-fn behavioral re-fill
                # (debt #46 point 1) retries it cleanly with the failing test.
                if _b3_keep and rc != 0:
                    _cur_sig = _cargo_err_sig_from_diagnostics(diagnostics)
                    _new_errs = _cur_sig - _baseline_sig
                    _frontier = _frontier_from_diags(diagnostics, target_src_dir)
                    _filled = {fd["fn_name"] for fd, _, _ in routed}
                    _on_fill = any(f == block.file and fn in _filled
                                   for (f, fn) in _frontier)
                    if not _new_errs and not _on_fill:
                        members_filled_this_block = spliced_now
                        fns_filled_total += spliced_now
                        impls_filled += 1
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
                # P7: whole rustc diagnostics (count-capped) when the JSON
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
                return FixOutcome("failed", err_sig=cur_err_sig,
                                   metric=cur_metric)

        def _on_impl_failure(attempt_n: int, outcome: FixOutcome,
                             gauge: FloorStall) -> None:
            _codes = sorted(ecodes_in_text(last_error_excerpt))
            _kind = "U2 test" if not _codes and last_error_excerpt else "compile"
            _log(f"  [phase-c/impl] {block_id}: {_kind} failed "
                 f"(attempt {attempt_n}); errs={outcome.metric} "
                 f"codes={_codes or '—'} "
                 f"floor={gauge.best} stall={gauge.stalls}.")

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
            # P6: maximal last-resort — ONE attempt with budgets OFF (round 1
            # only; later rounds change guidance, not budgets): the WHOLE
            # current file, every referencing test verbatim, and the full
            # focused error. A defect outside the budgeted window (sibling
            # member, type elsewhere in the file) is invisible to the normal
            # prompt; cost is one LLM call on an already-failed block.
            if (_retro_round == 1
                    and os.getenv("PHASE_C_MAXIMAL_LAST_RESORT", "1") != "0"):
                _max_cap = int(os.getenv("PHASE_C_MAXIMAL_CHAR_CAP", "120000"))
                _cur_text = file_path.read_text(encoding="utf-8", errors="replace")
                _max_tests: list[tuple[str, str]] = []
                _max_target_names = [m.fn_name for m in
                                      (*block.stub_members, *block.refill_members)]
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
                        _max_parsed = extract_fn_bodies(_ask_fill(_max_prompt))
                        if _max_parsed:
                            _splice_and_verify(_max_parsed, fix_attempt + 1)
                        else:
                            _log(f"  [phase-c/impl] {block_id}: maximal attempt "
                                 "returned no FN blocks.")
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
                        prior, snapshot, evidence=_block_evidence,
                        anchor_types=anchor_types_for_container(block.container)),
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
                # The dict write + on-disk save must be atomic together —
                # two blocks finishing concurrently each write their OWN key
                # (safe unlocked, per-block-unique), but an unlocked
                # save-after-write race could have one thread's
                # `_save_retrospectives` persist a snapshot taken before the
                # other thread's key landed, silently losing that entry.
                # Stamped with the context it was earned in AND the convicting
                # tests it was drawn from: a later reader honours the verdict
                # only where both still hold.
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
                    }, _prov, _block_evidence)
                    _save_retrospectives(project_dir, retros_cache)
                _retry_informed = not full_exhaust and not _downstream
            except Exception as _re:
                _log(f"  [phase-c/impl/retro] {block_id}: {_re}")
            if not (_retry_informed and _retro_round < _retro_rounds_max):
                break
            # Fresh guidance for the next round's prompt (the closure reads
            # this enclosing binding).
            prior_retro = admissible_guidance(
                retros_cache.get(_retro_key(block.file, "*impl*", block.container), {}),
                snapshot, evidence=_block_evidence,
                anchor_types=anchor_types_for_container(block.container))
            _log(f"  [phase-c/impl] {block_id}: retro round {_retro_round} "
                 "kept the error floor improving — re-entering the fix loop "
                 "with the new guidance.")
            _emit("phase_c.impl.retro_retry", data={
                "cluster": cluster_name, "file": block.file,
                "container": block.container, "retro_round": _retro_round,
            })

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
                        tdd_test_cmd, target_src_dir, cluster_env, _sv_base)
                        if _sv_rc == 0 else None)
                    if _sv_rc != 0 or _sv_regr is not None:
                        file_path.write_text(_stubbed, encoding="utf-8")
                        _log(f"  [phase-c/impl] {block_id}: best compiling "
                             "attempt NOT salvaged ("
                             + ("no longer compiles here" if _sv_regr is None
                                else f"would regress {_sv_regr[0].summary}")
                             + ") — stubs kept.")
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

    # P5: file-first pass for multi-block files — one prompt designs the
    # file's impls together; the per-block loop below handles the remainder
    # (the #42 freshness gate skips blocks the file pass completed).
    if os.getenv("PHASE_C_FILE_FIRST", "1") != "0":
        _by_file: dict[str, list[ImplBlock]] = {}
        for b in viable_blocks:
            _by_file.setdefault(b.file, []).append(b)
        for _file_rel, _fblocks in _by_file.items():
            if len(_fblocks) >= 2:
                _fill_file_first(_file_rel, _fblocks)
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
    )
