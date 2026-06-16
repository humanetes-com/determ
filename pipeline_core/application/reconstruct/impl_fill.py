"""Per-impl-block Phase-C fill engine (P4) — relocated from pipeline.py.

Concrete behind `_PhaseCFillAdapter.fill_impl_blocks`. ~39 collaborators injected via
`ImplFillDeps` (composition root late-binds the pipeline.py helpers for seams); body
relocated byte-for-byte behind a rebind-prelude. FOLLOW-UP: partition `ImplFillDeps` into
the existing `ports/fill_engine.py` role ports (overlaps `_PhaseCFillAdapter`).
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.domain.llm_output import extract_fn_bodies
from pipeline_core.domain.models import BuildCluster, DispatchFamily, Frontier, ImplBlock
from pipeline_core.domain.refine import FloorStall
from pipeline_core.prompts.context import PerImplSections


@dataclass(frozen=True, slots=True)
class ImplFillDeps:
    """Collaborators for the per-impl fill engine, injected by the composition root."""

    build_dispatch_family_section: Callable[..., Any]
    build_per_impl_prompt: Callable[..., Any]
    cargo_env: Callable[..., Any]
    cargo_err_sig: Callable[..., Any]
    cargo_err_sig_from_diagnostics: Callable[..., Any]
    check_graceful_stop: Callable[..., Any]
    cluster_filter_type_methods: Callable[..., Any]
    collect_crate_macros: Callable[..., Any]
    collect_impl_blocks_for_cluster: Callable[..., Any]
    collect_sibling_filled_bodies: Callable[..., Any]
    compute_cluster_macro_invariants: Callable[..., Any]
    detect_crate_no_std_invariant: Callable[..., Any]
    dispatch_families_via_extractor: Callable[..., Any]
    effective_stalls: Callable[..., Any]
    emit: Callable[..., Any]
    enumerate_types_via_extractor: Callable[..., Any]
    extract_crate_idioms: Callable[..., Any]
    extract_impl_where_clause: Callable[..., Any]
    filter_model_notes_for_prompt: Callable[..., Any]
    find_tests_referencing_fn: Callable[..., Any]
    focus_cargo_errors: Callable[..., Any]
    frontier_from_diags: Callable[..., Any]
    impl_block_id: Callable[..., Any]
    is_fn_fully_exhausted: Callable[..., Any]
    load_retrospectives: Callable[..., Any]
    log: Callable[..., Any]
    module_of: Callable[..., Any]
    phase_c_retrospective: Callable[..., Any]
    record_error_codes_seen: Callable[..., Any]
    retro_key: Callable[..., Any]
    run_cargo_check_diag: Callable[..., Any]
    save_retrospectives: Callable[..., Any]
    scan_unimplemented_for_cluster: Callable[..., Any]
    scan_unimplemented_records_for_cluster: Callable[..., Any]
    splice_fn_with_where: Callable[..., Any]
    splice_lock_for: Callable[..., Any]
    types_section_for_fix: Callable[..., Any]
    u2_test_gate: Callable[..., Any]
    ask: Callable[..., Any]
    failed_body_cap: Any
    per_fn_fix_hard_cap: Any
    per_impl_max_stubs: Any
    task2_model: Any


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
    tdd_frontier_keys: set[tuple[str, str]] | None,
    tdd_frontier_tests: Frontier | None = None,  # Plan 10 U2
    tdd_test_cmd: str | None = None,                                       # Plan 10 U2
) -> dict[str, Any]:
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
    _u2_test_gate = deps.u2_test_gate
    ask = deps.ask
    _FAILED_BODY_CAP = deps.failed_body_cap
    _PER_FN_FIX_HARD_CAP = deps.per_fn_fix_hard_cap
    _PER_IMPL_MAX_STUBS = deps.per_impl_max_stubs
    _TASK2_MODEL = deps.task2_model
    if cluster_env is None:
        cluster_env = _cargo_env()
    target_src_dir.mkdir(parents=True, exist_ok=True)
    # This tree's splice+cargo lock (per-tree: parallel clusters don't share).
    _splice_check_lock = _splice_lock_for(target_src_dir)
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    impls_filled = 0
    impls_failed = 0
    fns_filled_total = 0
    attempts_total = 0
    errors: list[str] = []

    blocks = _collect_impl_blocks_for_cluster(target_src_dir, cluster) or []
    if not blocks:
        return {
            "fns_filled": 0, "bytes_filled": 0, "attempts": 0,
            "errors": [], "remaining": 0,
            "impls_filled": 0, "impls_failed": 0,
        }

    # If a TDD frontier is provided, narrow to blocks whose stub_members
    # intersect the frontier. Otherwise process all blocks with stubs.
    if tdd_frontier_keys is not None:
        narrowed: list[ImplBlock] = []
        for b in blocks:
            stub_keys = {(b.file, m.fn_name) for m in b.stub_members}
            if stub_keys & tdd_frontier_keys:
                narrowed.append(b)
        blocks = narrowed

    # Drop empty / free-fn / oversized blocks (handled by per-fn fallback).
    def _viable(b: ImplBlock) -> bool:
        if not b.container:
            return False
        if not b.stub_members:
            return False
        return not len(b.stub_members) > _PER_IMPL_MAX_STUBS
    viable_blocks = [b for b in blocks if _viable(b)]
    _log(f"  [phase-c/impl] cluster {cluster_name}: "
         f"{len(viable_blocks)} impl block(s) viable "
         f"({len(blocks) - len(viable_blocks)} skipped — free fns / "
         "oversized).")

    # Crate-level context (shared by every block we visit).
    _src_for_macros = target_src_dir / "src" if (target_src_dir / "src").exists() else target_src_dir
    _macro_invariants = _compute_cluster_macro_invariants(_src_for_macros, cluster)
    _no_std_invariant = _detect_crate_no_std_invariant(target_src_dir)
    invariants_section = "\n\n".join(
        s for s in (_no_std_invariant, _macro_invariants) if s)
    _crate_type_records = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(target_src_dir) or [], cluster)
    macros_section = _collect_crate_macros(_src_for_macros)

    # Dispatch families (FR-027): autoref-specialization trait sets dispatched
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
        # Honor cross-iter exhaustion — if a prior retrospective marked
        # this block GENUINELY_HARD it's skipped.
        if _is_fn_fully_exhausted(retros_cache, block.file, "*impl*", block.container):
            _log(f"  [phase-c/impl] {block_id}: skipped (prior retro "
                 "marked exhausted).")
            return
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
        snapshot = file_path.read_text(encoding="utf-8")
        # Per-block context.
        stub_fn_names = [m.fn_name for m in block.stub_members]
        test_blocks: list[tuple[str, str]] = []
        # Reuse the per-fn test finder by unioning tests touching any stub.
        for fn_name in stub_fn_names[:4]:
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
            (m.sig_text) for m in block.stub_members)
        crate_idioms_section = _extract_crate_idioms(
            blocks, block.file,
            target_sig_text=_target_sig_concat,
            max_idioms=5,
        )
        # FR-024 sibling reference: union over stub member names so e.g.
        # a stub `fmt` sees an already-filled `fmt` from another impl.
        _sib_parts: list[str] = []
        for fn_name in stub_fn_names[:4]:
            _s = _collect_sibling_filled_bodies(
                snapshot, fn_name, block.container)
            if _s:
                _sib_parts.append(_s)
        siblings_section = "\n\n".join(_sib_parts)
        prior_retro = retros_cache.get(
            _retro_key(block.file, "*impl*", block.container), {}
        ).get("guidance", "")
        notes_section = ""
        if fr_path is not None:
            notes_section = _filter_model_notes_for_prompt(
                fr_path, file_rel=block.file, fn_name=stub_fn_names[0] if stub_fn_names else "",
                file_text=snapshot, current_error="",
            )
        last_error_excerpt = ""
        last_response = ""
        members_filled_this_block = 0
        # B3 (FR-029): baseline = the crate's error signature with THIS block's
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
        })

        def _attempt_impl_fill(fix_attempt: int) -> FixOutcome:
            nonlocal attempts_total, fns_filled_total, impls_filled, \
                last_error_excerpt, last_response, members_filled_this_block
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
                    dispatch_family=dispatch_family_section, siblings=siblings_section),
                fr_path=fr_path,
            )
            try:
                response = ask(prompt, model=_TASK2_MODEL)
            except Exception as e:
                _log(f"  [phase-c/impl] {block_id}: LLM error: {e}")
                return FixOutcome("stop")
            last_response = response
            parsed = extract_fn_bodies(response)
            if not parsed:
                _log(f"  [phase-c/impl] {block_id}: empty/no FN blocks "
                     f"(attempt {fix_attempt}).")
                return FixOutcome("stop")
            # Hold the global splice+cargo lock while we mutate and check.
            with _splice_check_lock:
                # Splice each parsed body into the impl indicated by its
                # `block` attribute. Match against TARGET container OR any
                # related container.
                routed: list[tuple[dict[str, Any], str]] = []  # (fn_data, container)
                for fn_data in parsed:
                    container = fn_data["block"]
                    if container == block.container:
                        routed.append((fn_data, block.container))
                        continue
                    matched = False
                    for rb in related_blocks:
                        if container == rb.container:
                            routed.append((fn_data, rb.container))
                            matched = True
                            break
                    if not matched:
                        # Loose match: header prefix only.
                        for cand in [block, *related_blocks]:
                            if container.split("{", 1)[0].strip() == cand.container.split("{", 1)[0].strip():
                                routed.append((fn_data, cand.container))
                                matched = True
                                break
                    if not matched:
                        _log(f"  [phase-c/impl] {block_id}: dropped "
                             f"`{fn_data['fn_name']}` — block header "
                             "does not match any TARGET or RELATED.")
                spliced_now = 0
                # Apply each splice. Operating on the same file in
                # sequence keeps byte offsets consistent because we
                # re-locate after each splice via the syn enumerator.
                for fn_data, container in routed:
                    target_file = target_src_dir / block.file
                    ok, reason, delta = _splice_fn_with_where(
                        target_file, container, fn_data["fn_name"],
                        fn_data["body"], fn_data["where_clause"],
                    )
                    if ok:
                        spliced_now += 1
                    else:
                        _log(f"  [phase-c/impl] {block_id}: splice "
                             f"`{fn_data['fn_name']}` failed: {reason}")
                if spliced_now == 0:
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
                if rc == 0:
                    # Plan 10 U2 (shared gate): accept the block only if its
                    # members' own tests pass too — not just compile.
                    _block_tests: set[str] = set()
                    for _bm in block.stub_members:
                        _block_tests |= (tdd_frontier_tests or {}).get(
                            (block.file, _bm.fn_name), set())
                    _u2_impl = _u2_test_gate(_block_tests, tdd_test_cmd,
                                             target_src_dir, cluster_env)
                else:
                    _u2_impl = None
                if rc == 0 and _u2_impl is None:
                    members_filled_this_block = spliced_now
                    fns_filled_total += spliced_now
                    impls_filled += 1
                    _log(f"  [phase-c/impl] {block_id}: "
                         f"FILLED {spliced_now} fn(s) "
                         f"(attempt {fix_attempt}).")
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
                # B3 (FR-029): keep a NON-REGRESSING fill even when the crate
                # doesn't fully compile — it adds no error beyond baseline and
                # none on the filled fns, so it's a correct prerequisite of the
                # interdependent web, not a failure to discard. This dissolves
                # the convergence deadlock where every correct piece is reverted
                # for not single-handedly making rc==0.
                if _b3_keep:
                    _cur_sig = _cargo_err_sig_from_diagnostics(diagnostics)
                    _new_errs = _cur_sig - _baseline_sig
                    _frontier = _frontier_from_diags(diagnostics, target_src_dir)
                    _filled = {fd["fn_name"] for fd, _ in routed}
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
                if _u2_impl is not None:
                    cargo_out = _u2_impl[1]   # route test-failure detail as the error
                # Compile failed (or U2 test failure). Structured diagnostics
                # give the error signature; a U2 test failure uses the
                # failing-test set (test output has no rustc E-codes).
                # rc != 0 establishes a minimum count of 1.
                cur_err_sig = (_u2_impl[0] if _u2_impl is not None
                               else _cargo_err_sig_from_diagnostics(diagnostics))
                cur_err_count = max(1, len(cur_err_sig))  # rc-honest minimum
                last_error_excerpt = _focus_cargo_errors(cargo_out, block.file)
                _record_error_codes_seen(cargo_out)
                # The fill regressed (a new error, or an error on the filled
                # fns) — B3 (FR-029) already accepted non-regressing fills
                # above. Revert; the driver decides whether to retry.
                file_path.write_text(snapshot, encoding="utf-8")
                return FixOutcome("failed", err_sig=cur_err_sig,
                                   err_count=cur_err_count)

        def _on_impl_failure(attempt_n: int, outcome: FixOutcome,
                             gauge: FloorStall) -> None:
            _log(f"  [phase-c/impl] {block_id}: compile failed "
                 f"(attempt {attempt_n}); errs={outcome.err_count} "
                 f"floor={gauge.best} stall={gauge.stalls}.")

        def _on_impl_stall(attempt_n: int, gauge: FloorStall) -> None:
            _log(f"  [phase-c/impl] {block_id}: floor stalled "
                 f"at {gauge.best} for {gauge.stalls} iter(s) "
                 "— stopping this block.")

        def _on_impl_repeated(attempt_n: int, gauge: FloorStall) -> None:
            _log(f"  [phase-c/impl] {block_id}: identical errors "
                 f"after attempt {attempt_n} — stopping.")

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
        if members_filled_this_block == 0:
            impls_failed += 1
            errors.append(f"{block.file}::{block.container}: "
                          f"impl-fill failed after {fix_attempt} attempt(s)")
            _emit("phase_c.impl.fill.fail", duration_ms=int(
                (time.monotonic() - _t0) * 1000),
                data={
                    "cluster": cluster_name,
                    "file": block.file,
                    "container": block.container,
                    "members_failed": len(block.stub_members),
                    "fix_attempts": fix_attempt,
                    "last_error_preview": last_error_excerpt[:200],
                })
            # Per-impl retrospective — surface a strategy hint for the
            # next iter to consume. Uses the existing per-fn retro helper
            # with the impl id as the fn key so the on-disk store layout
            # stays uniform.
            try:
                cur_errs = len(_cargo_err_sig(last_error_excerpt))
                prior = retros_cache.get(
                    _retro_key(block.file, "*impl*", block.container), {})
                prior_best = prior.get("best_error_count")
                retro = _phase_c_retrospective(
                    fn_name="*impl*",
                    sig_text=block.container,
                    container=block.container,
                    file_rel=block.file,
                    cluster_name=cluster_name,
                    last_body=last_response[:_FAILED_BODY_CAP],
                    error_excerpt=last_error_excerpt,
                    fix_attempts=fix_attempt,
                    prior_retrospective=prior.get("guidance", ""),
                )
                if prior_best is None:
                    new_best = cur_errs
                    improved = True
                else:
                    new_best = min(int(prior_best), cur_errs)
                    improved = cur_errs < int(prior_best)
                full_exhaust = (
                    retro.get("category") == "GENUINELY_HARD"
                    or (prior_best is not None and not improved)
                )
                retros_cache[_retro_key(block.file, "*impl*", block.container)] = {
                    "category": retro.get("category", ""),
                    "guidance": retro.get("guidance", ""),
                    "signal": retro.get("signal", ""),
                    "retrospective_count": int(prior.get("retrospective_count", 0)) + 1,
                    "error_count": cur_errs,
                    "best_error_count": new_best,
                    "fully_exhausted": full_exhaust,
                    "last_error_preview": last_error_excerpt[:240],
                }
                _save_retrospectives(project_dir, retros_cache)
            except Exception as _re:
                _log(f"  [phase-c/impl/retro] {block_id}: {_re}")

    for block in viable_blocks:
        _fill_one_block(block)
    bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    remaining_records = _scan_unimplemented_records_for_cluster(target_src_dir, cluster)
    # None = extractor absent → text-scan fallback; 0 here would make the
    # caller skip the per-fn cleanup pass entirely.
    remaining_n = (len(remaining_records) if remaining_records is not None
                   else len(_scan_unimplemented_for_cluster(target_src_dir, cluster)))
    return {
        "fns_filled": fns_filled_total,
        "bytes_filled": max(0, bytes_after - bytes_before),
        "attempts": attempts_total,
        "errors": errors,
        "remaining": remaining_n,
        "impls_filled": impls_filled,
        "impls_failed": impls_failed,
    }
