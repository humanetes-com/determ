"""FillOneFn — the per-fn fill use-case (P2 / R3.3).

The engine unit that reconstructs ONE `unimplemented!()` stub via the per-fn
fix loop: locate → prompt → splice → cargo check → U2 test gate → accept or
revert, with self-note extraction and a progress-gated retrospective on
exhaustion. Ported verbatim from `pipeline.py:_fill_one_stub`; every
`pipeline.py` helper it used is now a narrow role port (`fill_engine`).

Pure-by-shape: reads `rec` (a `Stub`) + `ctx` (`PerFnPassCtx`), mutates only the
target tree (under `ctx.splice_check_lock`) and reports the outcome as a
`FillResult` for the dispatcher to aggregate.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.cargo_errors import (
    ecodes_in_text,
)
from pipeline_core.application.reconstruct.cargo_errors import (
    err_sig as cargo_err_sig,
)
from pipeline_core.application.reconstruct.context import (
    PerFnPassCtx,
    crash_block_mark,
    crash_block_stands,
    fill_skip,
)
from pipeline_core.application.reconstruct.fill_rules import (
    is_unimplemented_body,
    make_failed_attempt_body,
    module_of,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    make_request_answerer,
    workspace_file_reader,
)
from pipeline_core.domain.behaviour_select import select_usage_examples
from pipeline_core.domain.cfg import filter_cfg_inactive_branches
from pipeline_core.domain.crash_site import blame_outside_unit
from pipeline_core.domain.dispatch import sibling_blocked_u2_failures
from pipeline_core.domain.models import FillResult, FnLocator, FnSite, Stub
from pipeline_core.domain.reconstruction import SpliceOutcome
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
    RetroGateVerdict,
    RetroProvenance,
    admissible_guidance,
    anchor_types_for_container,
    conviction_standing,
    honoured_guidance_line,
    judge_guidance,
    stamp_conviction,
)
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.test_report import U2Decision
from pipeline_core.domain.type_refs import types_referenced_in_text
from pipeline_core.domain.verdict import (
    Disposition,
    GateOutcome,
    ValidityVector,
    VerdictRecord,
)
from pipeline_core.ports.fill_engine import (
    Cargo,
    EngineTelemetry,
    Mutator,
    PromptContext,
    Retros,
    StallPolicy,
    TestLinker,
)
from pipeline_core.prompts.context import PerFnFixSections, PerFnSections
from pipeline_core.prompts.per_fn import (
    build_per_fn_fix_prompt,
    build_per_fn_prompt,
)
from pipeline_core.prompts.pull_menu import pull_menu_section
from pipeline_core.prompts.sections import format_type_for_prompt


@dataclass(frozen=True, slots=True)
class FillCaps:
    """Per-fn fix-loop caps (resolved from SETTINGS.fix_caps at the root)."""
    per_fn_fix_hard_cap: int
    per_fn_fix_max_stalls: int
    fix_err_cap: int


@dataclass(frozen=True, slots=True)
class FillOneFn:
    """Per-fn fill unit over narrow role ports (ISP)."""
    tel: EngineTelemetry
    mut: Mutator
    cargo: Cargo
    prompt: PromptContext
    retros: Retros
    tests: TestLinker
    stall: StallPolicy
    caps: FillCaps

    def _salvage_compiling_body(
        self, ctx: PerFnPassCtx, salvage: CompilingSalvage, file_path: Path,
        rel_file: str, fn_name: str, container: str, sig_text: str, rec: Stub,
        revert: Callable[[], bool],
    ) -> bool:
        """Restore the fix loop's best COMPILING body instead of leaving the
        stub. Call with the stub already reverted in, holding the splice lock.

        The stub panics, which aborts the test binary and un-judges every
        sibling test in it, so a compiling body is what the outer TDD loop wants
        to iterate on even when this fn's own tests still fail. Kept only if it
        still compiles here AND is not a net loss against the stub — the
        baseline is captured now, against the stub, because that is the state
        the keep is being compared against."""
        best = salvage.best
        if best is None:
            return False
        _base = self.cargo.test_regression_baseline(
            ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env)
        _sp = self.mut.splice_body_scoped(
            file_path, fn_name, container, sig_text, best.body,
            cfg_predicates=rec.cfg_predicates, allow_nonstub=True)
        if not _sp.ok:
            self.tel.log(f"  [phase-c/per-fn] salvage splice refused for "
                         f"{rel_file}::{fn_name}: {_sp.reason} — stub kept.")
            return False
        _rc, _ = self.cargo.run(ctx.check_cmd, ctx.target_src_dir, ctx.cluster_env)
        _regr = (self.cargo.test_regression_gate(
            ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env, _base)
            if _rc == 0 else None)
        if _rc != 0 or _regr is not None:
            _why = ("no longer compiles here" if _regr is None
                    else f"would regress {_regr[0].summary}")
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name}: best "
                         f"compiling attempt NOT salvaged ({_why}) — "
                         "reverting to stub.")
            revert()
            return False
        _ctx = f" [{container}]" if container else ""
        self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name}{_ctx}: fix loop "
                     f"exhausted — KEEPING best compiling body ({best.metric}); "
                     "a stub would abort the test binary and un-judge its "
                     "siblings. Deferred to the outer loop.")
        self.tel.emit("phase_c.fn.fill.salvaged", data={
            "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
            "file": rel_file, "fn": fn_name, "container": container,
            "failing_tests": best.metric.failing_tests,
        })
        return True

    def run(self, idx: int, rec: Stub, *, ctx: PerFnPassCtx) -> FillResult:
        rel_file = rec.file
        fn_name = rec.fn_name
        container = rec.container
        sig_text = rec.sig_text
        attempts = 0   # LLM attempts this fn consumed — returned in FillResult
        # Still-stubbed sites that held this fn's attributed tests on the
        # accepted attempt — the external-only evidence `pass_payoff` reads.
        # A list so the nested attempt closure can record without `nonlocal`,
        # matching the other cross-attempt holders in this method.
        _blocked_by: list[FnSite] = []
        # Non-empty iff the latest revert was a crash-only regression blamed
        # wholly outside this unit — holds the crash-block mark to persist.
        _crash_blocked_mark: list[str] = []
        # A crash-implicated fn is never exhaustion-skipped: the localized
        # faulting stack is evidence no prior attempt ever saw.
        _crash_evidence = (ctx.crash_evidence or {}).get(
            FnLocator(rel_file, fn_name), "")
        _unit_key = self.retros.retro_key(rel_file, fn_name, container)
        # Crash-blocked: a previous attempt was reverted for a crash-only
        # regression whose localized blame lies wholly OUTSIDE this unit.
        # While the blamed file's text is unchanged, re-attempting buys the
        # identical crash — measured 2026-08-10, `src/context.rs::StdErrorforE`
        # was reverted for `binary crashed: ['tests/test_context.rs'] (net -6)`
        # five times, ~4-6 model calls each, with ASAN blaming
        # `src/error.rs::object_drop` throughout.
        if ctx.crash_blocked is not None:
            _mark = ctx.crash_blocked.get(_unit_key)
            if _mark is not None:
                if crash_block_stands(ctx.target_src_dir, _mark):
                    self.tel.log(
                        f"  [phase-c/per-fn] {idx}/{ctx.total} skipped "
                        f"{rel_file}::{fn_name} [{container}] — crash-blocked "
                        f"by a sibling's fault ({_mark}); re-admitted when the "
                        "blamed file changes. No attempt charged.")
                    self.tel.emit("phase_c.fn.fill.skip", data={
                        "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                        "file": rel_file, "fn": fn_name,
                        "reason": "crash_blocked",
                    })
                    return fill_skip(rec, attempts)
                ctx.crash_blocked.pop(_unit_key, None)
                self.tel.log(
                    f"  [phase-c/per-fn] {rel_file}::{fn_name} [{container}]: "
                    f"blamed file moved since the crash-block ({_mark}) — "
                    "re-admitted.")
        file_path = ctx.target_src_dir / rel_file
        try:
            snapshot = file_path.read_text(encoding="utf-8")
        except Exception:
            return fill_skip(rec, attempts)
        # A conviction reached while the prompt taught a field the current
        # layout does not have is not evidence the fn is hard — the attempts
        # it counted were never fair samples. Don't honour the flag while its
        # own guidance contradicts the layout being compiled. The impl's self
        # type anchors claims the prose leaves unattached ("change the body to
        # `self._object.provide(..)`" names no struct at all).
        _anchor_types = anchor_types_for_container(container)
        _retro_rec = (ctx.retros_cache.get(
            self.retros.retro_key(rel_file, fn_name, container), {}) or {})
        _poisoned_retro = ctx.retro_gate.record(judge_guidance(
            _retro_rec.get("guidance", ""), snapshot,
            anchor_types=_anchor_types)) is RetroGateVerdict.RETIRED
        if _poisoned_retro:
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: retro guidance contradicts the "
                         "current layout — guidance withheld, exhaustion not "
                         "honoured (layout-contradicted).")
        # …and a conviction earned in ANOTHER context (other cluster's layout,
        # the absolute acceptance gate, the pre-group route set), or drawn from
        # tests that no longer convict this fn, is proof of nothing here —
        # whatever its guidance says.
        _provenance = RetroProvenance.current(ctx.cluster_name, snapshot)
        _evidence = ConvictionEvidence(
            frozenset(ctx.tdd_frontier_tests.convicting_for(
                FnLocator(rel_file, fn_name)))
            if ctx.tdd_frontier_tests is not None else frozenset())
        _standing = conviction_standing(_retro_rec, _provenance, _evidence)
        _stale_conviction = (bool(_retro_rec.get("fully_exhausted"))
                             and _standing is not ConvictionStanding.CURRENT)
        if _stale_conviction:
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: exhaustion verdict was earned in a "
                         f"different context — not honoured ({_standing.value}).")
        if (not _crash_evidence and not _poisoned_retro and not _stale_conviction
                and self.retros.is_fn_fully_exhausted(
                    ctx.retros_cache, rel_file, fn_name, container)):
            self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} skipped "
                         f"{rel_file}::{fn_name} [{container}] — fully exhausted "
                         "across iters (retrospective marked GENUINELY_HARD/"
                         "FROZEN_BOUND_MISMATCH, or a retro round brought no "
                         "error-count improvement).")
            self.tel.emit("phase_c.fn.fill.skip", data={
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name,
                "reason": "fully_exhausted",
            })
            self.tel.emit("phase_c.verdict", data={
                **VerdictRecord(
                    unit=str(_unit_key), route="per-fn", attempt=0,
                    disposition=Disposition.CAP_REFUSAL,
                    reason="skipped — prior conviction stands "
                           "(fully exhausted)").to_json_dict(),
                "cluster": ctx.cluster_name,
            })
            return fill_skip(rec, attempts)
        self.tel.check_stop(f"per-fn pass {ctx.pass_n} fn {fn_name}")
        # Resolve current offsets — file may have been modified by prior
        # fills in this pass, shifting byte positions. Re-enumerate to
        # find this specific record's current location.
        loc_at = self.mut.locate_fn_by_record(file_path, fn_name, container, sig_text,
                                               rec.cfg_predicates)
        _cur_body = ""   # current (wrong) body, for a behavioural re-fill
        if loc_at is None:
            # Fall back to regex locator (for tests-fixture path where
            # the extractor binary isn't built).
            loc = self.mut.locate_fn(snapshot, fn_name)
            if loc is None:
                return fill_skip(rec, attempts)
            sig_text = sig_text or loc[0]
        else:
            sig_text = loc_at.sig_text
            if rec.refill:
                # From the locator's own read, never sliced out of `snapshot`:
                # that image predates this unit queueing for the splice lock.
                _cur_body = loc_at.body_text
        # `snapshot` is the WHOLE FILE as it stood before this unit queued for
        # the splice lock. Under parallelism several units in the same file
        # snapshot the same starting state, so by the time a losing unit
        # reverts, its image predates every sibling fill that landed meanwhile
        # — writing it back erases fns already reported as filled (observed:
        # three `filled` constructors back to `unimplemented!()`). Revert only
        # THIS fn, at offsets resolved against the file as it stands now.
        _own_pre_image = _cur_body or "unimplemented!()"

        def _revert_own_body() -> bool:
            # A revert that silently no-ops leaves the failed body in the
            # tree: every sibling's compile then fails on THIS fn's error,
            # the next attempt's splice refuses `target_not_stub`, and the
            # race guard misreads that as another worker's fill. Surface
            # the failure loudly — never assume a revert landed.
            out = self.mut.revert_body_scoped(
                file_path, fn_name, container, sig_text, _own_pre_image,
                cfg_predicates=rec.cfg_predicates)
            if not out.ok:
                self.tel.log(f"  [phase-c/per-fn] REVERT FAILED for "
                             f"{rel_file}::{fn_name}: {out.reason} — broken "
                             "body left in tree; siblings will see its errors.")
                self.tel.emit("phase_c.fn.revert.fail", data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name, "reason": out.reason,
                })
            return out.ok

        test_blocks = self.tests.find_tests_for_fn(
            ctx.tests_dir_files, fn_name,
            file_rel=rel_file, src_dir=ctx.src_for_macros,
            container=container,
        )
        # Crate idioms ranked by THIS fn's signature tokens.
        crate_idioms_section = self.prompt.extract_crate_idioms(
            ctx.idiom_blocks, rel_file, target_sig_text=sig_text,
            max_idioms=5,
        )
        _fn_t0 = time.monotonic()
        self.tel.emit("phase_c.fn.fill.start", data={
            "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
            "file": rel_file, "fn": fn_name,
            "tests_attached": len(test_blocks),
            "sig_chars": len(sig_text),
            "refill": rec.refill,   # behavioral re-fill of a wrong body
        })
        # Monotone-ratchet baseline: cluster runtime test state BEFORE this fn's
        # OWN splice, so a fill whose own tests pass but which reddens a sibling
        # is reverted, not accepted. Captured freshly per ATTEMPT, inside the
        # splice lock right before this attempt's own splice (see
        # `_attempt_fn_fill`) — NOT once here at fn-dispatch time. Under
        # PIPELINE_PARALLELISM>1, several fns dispatch together and queue for
        # the same shared lock; a baseline captured before that queueing goes
        # stale by the time this fn's own turn arrives, since OTHER queued fns'
        # accepted changes land first. A stale baseline misattributes THEIR
        # regressions to whichever fn happens to run its own check right after
        # — live-observed on anyhow: `test_as_ref`/`test_downcast` (real
        # `Error::downcast`/`AsRef` tests, unrelated to `Chain`) were reported
        # as "regressed" by logically-unrelated `chain.rs` fns (`new`,
        # `default`, `next_back`, `len`, `size_hint`) and `error.rs::root_cause`
        # — one real regression elsewhere, misattributed to everyone queued
        # behind it. None ⇒ crate not test-runnable now (or guard off) → gate
        # self-disables for that attempt. Tests failing only because this fn is
        # still an `unimplemented!()` stub are in the baseline, so turning them
        # green is never seen as a regression.

        # Notes loaded inside the fix-loop on every iteration so that
        # (a) notes added during this fn's own fix loop become visible
        # to later iterations, and (b) the relevance filter (P3.4) can
        # use the CURRENT cargo error excerpt to prioritise notes whose
        # error codes / keywords match.

        # === Inner fix loop (slice 8a f2: unit fn + drive_fix_loop —
        # the driver owns attempt counting, the FloorStall gauge, and the
        # stall/repeated terminations; this unit owns prompt/splice/lock/
        # verify/accept/revert) ===
        last_response = ""
        last_body = ""
        error_excerpt = ""
        # The last failed attempt's REAL two-tier metric. Kept, not
        # re-derived from the error excerpt: a test-failure excerpt carries no
        # rustc E-codes, so re-parsing it scores every compiled-but-red
        # attempt as 0 errors — and 0 can never beat a prior 0, which parked
        # fns the moment they stopped failing to compile.
        last_metric: FixProgress | None = None
        # Best COMPILING attempt seen across the loop. `unimplemented!()` is not
        # a neutral fallback — it panics and aborts the whole test binary, so
        # reverting a compiling body to it un-judges every sibling test in that
        # binary. On exhaustion we restore this instead (regression-gated).
        salvage = CompilingSalvage()
        # a behavioral re-fill enters the loop already "in fix mode" —
        # seed the current wrong body + a behavioral instruction so attempt 1 is
        # a corrective fix (build_per_fn_fix_prompt) with the failing test shown,
        # not a naive from-scratch stub fill.
        if rec.refill:
            last_body = _cur_body
            error_excerpt = (
                "This body COMPILES but its attributed test(s) below still FAIL. "
                "Rewrite the BEHAVIOR so those test(s) pass; keep the signature "
                "and do not reintroduce `unimplemented!()`.")
        attempts_log: list[str] = []
        first_failed_body = ""
        first_error_excerpt = ""
        # Corrected R2/R3: carry the bound-provided method inventory
        # from the last gather so the exhaustion retro can redirect to a frozen
        # bound's trait method instead of mis-routing to GENUINELY_HARD.
        retro_bound_methods = ""
        # P2 pulls for the per-fn ladder: bounded typed requests; a bodyless
        # reply with a <REQUEST .../> re-asks the SAME rung with the answer.
        _pull_menu = ""
        _answerer = None
        if os.getenv("CONTEXT_PULLS", "1") != "0":
            _max_pulls = int(os.getenv("CONTEXT_PULLS_MAX", "2"))
            _pull_sources = PullSources(
                read_file=workspace_file_reader(ctx.target_src_dir),
                find_tests=lambda name: "\n\n".join(
                    f"// {p}\n{t}" for p, t in self.tests.find_tests_for_fn(
                        ctx.tests_dir_files, name)),
                type_def=lambda name: ("\n\n".join(
                    format_type_for_prompt(r) for r in
                    types_referenced_in_text(name, ctx.crate_type_records)[:3])
                    # Crate-local records win; well-known unstable std APIs
                    # (std::error::Request et al.) answer as a last fallback so
                    # the pull never starves on a std type the manifest can't have.
                    or std_api_def(name)),
                full_error=lambda: error_excerpt,   # this fn's latest error
            )
            _pull_menu = pull_menu_section(
                _pull_sources.available_kinds(), _max_pulls)
            _answerer = make_request_answerer(
                _pull_sources, log=self.tel.log, emit=self.tel.emit,
                max_pulls=_max_pulls)
        # P2: history of (body, error_sig, outcome) for THIS fn's fix loop.
        # Surfaced to the model in subsequent fix prompts so it sees the
        # search space it has already explored.
        per_fn_history: list[dict[str, Any]] = []

        def _attempt_fn_fill(fix_attempt: int) -> FixOutcome:
            nonlocal attempts, sig_text, last_response, \
                last_body, error_excerpt, first_failed_body, \
                first_error_excerpt, retro_bound_methods, last_metric
            attempts += 1
            # Only the ACCEPTED attempt's blame may leave this unit. An
            # external-only attempt that the regression gate then reverts goes
            # back through the fix loop, and a later attempt may well be judged
            # on its own merits — carrying the earlier blockers out would
            # report a judgeable fill as unjudgeable.
            _blocked_by.clear()
            cur_text = file_path.read_text(encoding="utf-8")
            # E/R3: strip inactive #[cfg] branches so the model sees only the
            # active variant (e.g. one StdError definition, not three).
            if ctx.cluster_cfg_set is not None:
                cur_text = filter_cfg_inactive_branches(cur_text, ctx.cluster_cfg_set)
            # Slice 8a: one gather per attempt — notes re-filtered with the
            # LATEST cargo error (P3.4), TYPES_IN_SCOPE re-selected from
            # sig + container + where + the PREVIOUS BODY (types the last
            # attempt actually manipulated — new) + the current error
            # (Plan 9 F4: E0277/E0599/E0046/E0053 routinely name a
            # trait/type the signature doesn't mention).
            _fn_ctx = self.prompt.gather(
                file_text=cur_text, file_rel=rel_file, fn_name=fn_name,
                sig_text=sig_text, body_text=last_body,
                error_excerpt=error_excerpt, container=container,
                type_records=ctx.crate_type_records, fr_path=ctx.fr_path,
            )
            notes_section = _fn_ctx.notes_section
            _types_section_eff = _fn_ctx.types_section
            retro_bound_methods = _fn_ctx.bound_methods_section
            # a re-fill target is corrective from attempt 1 — take the
            # fix branch (prev_body + failing test) rather than the stub-fill one.
            if fix_attempt == 1 and not rec.refill:
                prompt = build_per_fn_prompt(
                    file_text=cur_text, fn_name=fn_name, sig_text=sig_text,
                    test_blocks=test_blocks, module_name=ctx.module_name,
                    cluster_name=ctx.cluster_name, file_rel=rel_file,
                    sections=PerFnSections(
                        macros=ctx.macros_section, notes=notes_section,
                        container=container, types=_types_section_eff,
                        invariants=ctx.invariants_section,
                        siblings=self.prompt.collect_sibling_filled_bodies(
                            cur_text, fn_name, container),
                        crate_idioms=crate_idioms_section,
                        bound_methods=_fn_ctx.bound_methods_section,
                        blueprint=_fn_ctx.blueprint_section,
                        crash_evidence=_crash_evidence,
                        usage_examples=select_usage_examples(
                            ctx.behaviour_specs, fn_name=fn_name,
                            container=container, char_budget=1200)),
                    fr_path=ctx.fr_path,
                )
            else:
                # prior retrospective guidance — start from "what to try
                # instead" rather than naive retry-with-errors. Judged against
                # THIS attempt's file text: the store is shared by clusters
                # with different private layouts, so guidance that names a
                # field this layout lacks is withheld, not shown. Every
                # honoured record is LOGGED: the store steers prompts silently,
                # and a poisoned record's only other trace is its downstream
                # test failures (the ContextError-source `None` guidance,
                # 2026-08-13/14, was visible in no log for two days).
                _retro_key = self.retros.retro_key(rel_file, fn_name, container)
                _retro_rec = ctx.retros_cache.get(_retro_key, {})
                _retro_guidance = admissible_guidance(
                    _retro_rec, cur_text, evidence=_evidence,
                    anchor_types=_anchor_types)
                # The line carries the STORE key (file::fn::container), not a
                # display name: `fn new` exists under three containers, and
                # the whole point is that the grep lands on the one record
                # that steered this prompt.
                if (_hline := honoured_guidance_line(
                        _retro_key, _retro_rec, _retro_guidance)):
                    self.tel.log(f"  {_hline}")
                prompt = build_per_fn_fix_prompt(
                    file_text=cur_text, fn_name=fn_name, sig_text=sig_text,
                    test_blocks=test_blocks, module_name=ctx.module_name,
                    cluster_name=ctx.cluster_name, file_rel=rel_file,
                    prev_body=last_body, error_excerpt=error_excerpt,
                    sections=PerFnFixSections(
                        macros=ctx.macros_section, notes=notes_section,
                        container=container, types=_types_section_eff,
                        invariants=ctx.invariants_section,
                        retrospective_guidance=_retro_guidance,
                        # hoist already-filled sibling fn impls (same
                        # name, different container) — best target-body source.
                        siblings=self.prompt.collect_sibling_filled_bodies(
                            cur_text, fn_name, container),
                        crate_idioms=crate_idioms_section,
                        bound_methods=_fn_ctx.bound_methods_section,
                        blueprint=_fn_ctx.blueprint_section,
                        crash_evidence=_crash_evidence),
                    # P3.1: open the sig-change escape hatch after multiple
                    # compile failures. The model can return an additional
                    # <SIGNATURE> block; we splice it only when fn is non-pub.
                    allow_sig_change=(fix_attempt >= 5),
                    fr_path=ctx.fr_path,
                    attempt_history=per_fn_history,  # P2
                )

            body, last_response = self.prompt.ask_per_fn_body(
                prompt + _pull_menu, fn_name, attempts_log,
                sig_text=sig_text, answer_request=_answerer)
            if body is None:
                # Model gave no usable response — stop retrying this fn.
                self.tel.emit("phase_c.fn.fill.skip", data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name,
                    "fix_attempt": fix_attempt,
                    "reason": "empty_response",
                })
                return FixOutcome("stop")

            # P3.1: if the model proposed a sig change, try it FIRST so
            # the body splice operates on the updated sig. Rolls back if
            # the resulting cargo check still fails (revert via snapshot).
            if fix_attempt >= 5:
                proposed_sig = self.mut.extract_signature_block(last_response)
                if proposed_sig and self.mut.replace_fn_signature(
                        file_path, fn_name, proposed_sig):
                    # Refresh sig_text so subsequent prompts use the new one.
                    new_loc = self.mut.locate_fn(
                        file_path.read_text(encoding="utf-8"), fn_name)
                    if new_loc is not None:
                        sig_text = new_loc[0]
                    self.tel.log(f"  [phase-c/per-fn] {fn_name}: sig swapped (attempt "
                                 f"{fix_attempt}): {sig_text}")

            # Splice by exact byte offset. Re-enumerate just before
            # splicing because prior fills (or our sig swap above) may
            # have shifted offsets. When a fn's name is duplicated in a
            # file (Display::fmt vs Debug::fmt), we disambiguate by
            # container + sig_text in locate_fn_by_record.
            # Hold splice_check_lock from splice through cargo check through
            # the revert (if applicable). Other workers wait so we don't
            # corrupt target/ with concurrent cargo invocations.
            ctx.splice_check_lock.acquire()
            _lock_released = False
            # Fresh regression baseline, captured NOW (inside the lock, right
            # before this attempt's own splice) rather than once at fn-dispatch
            # time — see the comment on the old dispatch-time capture this
            # replaced, above `_attempt_fn_fill`'s definition, for why a stale
            # baseline under parallelism misattributes other workers' queued-
            # ahead regressions to this fn.
            _attempt_baseline_test = self.cargo.test_regression_baseline(
                ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env)
            def _locate_and_splice() -> SpliceOutcome:
                _loc = self.mut.locate_fn_by_record(
                    file_path, fn_name, container, sig_text, rec.cfg_predicates)
                # A behavioral re-fill target replaces an already-filled
                # (compiling-but-wrong) body → the splice must accept a non-stub.
                if _loc is not None:
                    return self.mut.splice_fn_body_at(
                        file_path, _loc.body_byte_start, _loc.body_byte_end,
                        body, allow_nonstub=rec.refill, fn_name=fn_name)
                # Fallback: bare-name locator. Less accurate for
                # same-name collisions but acceptable when the syn
                # binary isn't available.
                return self.mut.splice_fn_body(
                    file_path, fn_name, body, allow_nonstub=rec.refill)

            splice_out = _locate_and_splice()
            # Diagnose a `target_not_stub` WHILE STILL HOLDING the lock: the
            # non-stub body is either another worker's fill (genuine race) or
            # THIS fn's own earlier failed attempt that a refused revert left
            # behind. The two need opposite handling — a race discards our
            # output, a revert-leak must be repaired and the splice retried,
            # or the broken body keeps failing every sibling's compile.
            _race_detected = False
            if not splice_out.ok and splice_out.reason == "target_not_stub":
                try:
                    _race_loc = self.mut.locate_fn_by_record(
                        file_path, fn_name, container, sig_text, rec.cfg_predicates)
                    if _race_loc is not None:
                        _race_cur_body = _race_loc.body_text
                        if not is_unimplemented_body(_race_cur_body):
                            def _norm(s: str) -> str:
                                return " ".join(s.split())
                            _own = {_norm(h.get("body", ""))
                                    for h in per_fn_history if h.get("body")}
                            if _norm(_race_cur_body) in _own:
                                self.tel.log(
                                    f"  [phase-c/per-fn] REVERT LEAK — "
                                    f"{rel_file}::{fn_name}: own failed body "
                                    "found in tree; restoring pre-image and "
                                    "retrying the splice.")
                                self.tel.emit("phase_c.fn.revert.leak", data={
                                    "cluster": ctx.cluster_name,
                                    "pass_n": ctx.pass_n,
                                    "file": rel_file, "fn": fn_name,
                                })
                                _rest = self.mut.splice_fn_body_at(
                                    file_path, _race_loc.body_byte_start,
                                    _race_loc.body_byte_end,
                                    _own_pre_image, allow_nonstub=True,
                                    allow_stub_body=True)
                                if _rest.ok:
                                    splice_out = _locate_and_splice()
                            else:
                                _race_detected = True
                except Exception:
                    pass
            ok, delta = splice_out.ok, splice_out.delta
            if ok and splice_out.reason == "ok_xml_unescaped":
                self.tel.log(f"  [phase-c/per-fn] {fn_name}: landed only after "
                             "resolving XML entities in the body — the reply "
                             "used the escaped rendering.")
            if ok and splice_out.reason == "ok_own_brace_dropped":
                self.tel.log(f"  [phase-c/per-fn] {fn_name}: landed only after "
                             "dropping the fn's own closing brace — the reply "
                             "closed the body itself.")
            if not ok:
                ctx.splice_check_lock.release()
                _lock_released = True
                _ctx = f" [{container}]" if container else ""
                if _race_detected:
                    self.tel.log(f"  [phase-c/per-fn] GUARD POSSIBLE RACE — "
                                 f"{rel_file}::{fn_name}{_ctx} already filled by "
                                 "another worker; this worker's LLM output "
                                 "discarded.")
                else:
                    self.tel.log(f"  [phase-c/per-fn] splice refused for "
                                 f"{rel_file}::{fn_name}{_ctx}: "
                                 f"{splice_out.reason}.")
                self.tel.emit("phase_c.fn.fill.skip", data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name,
                    "container": container,
                    "fix_attempt": fix_attempt, "body_chars": len(body),
                    "reason": "race_lost" if _race_detected else splice_out.reason,
                })
                return FixOutcome("stop")

            rc, cargo_out = self.cargo.run(
                ctx.check_cmd, ctx.target_src_dir, ctx.cluster_env)
            if rc != 0:
                # Deterministic repair: a body cannot add `use` items, so apply
                # rustc's own single-candidate import suggestions to this fn's
                # file and re-check before charging the model an attempt. The
                # structured diagnostics are fetched only on failure — the
                # plain `run` above stays the compile gate.
                _, _, _diags = self.cargo.check_diag(
                    ctx.check_cmd, ctx.target_src_dir, ctx.cluster_env)
                _rep = repair_missing_imports(
                    self.cargo.check_diag, ctx.check_cmd, ctx.target_src_dir,
                    ctx.cluster_env or {}, rel_file, _diags, self.tel.log,
                    "[phase-c/per-fn]")
                if _rep is not None:
                    rc, cargo_out, _diags = _rep
            # Plan 10 U2: compile is only a pre-gate — accept the fn only if
            # its OWN tests pass. The SHARED u2_test_gate decides; on a test
            # failure it routes through the same fix loop as a compile error
            # (failing-test set as the stall signature).
            _u2_fail_sig: frozenset[str] | None = None
            if rc == 0:
                # Container-qualified own-site: a test panicking in a same-named
                # sibling (e.g. this fn is a `Bool` impl's `not`, the panic is in
                # the free `not(cond)` stub) is external, not this fn's failure.
                # Same set the skip gate judged the stored conviction against —
                # one definition of what this fn answers for.
                _v = self.cargo.u2_test_gate(
                    set(_evidence.tests),
                    frozenset({FnSite(rel_file, fn_name, container)}),
                    ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env,
                    _attempt_baseline_test)
                if _v.decision is U2Decision.REJECT:
                    # A real self-failure (assertion/behavior). Route through the
                    # normal fix path: revert + re-prompt with the failing tests
                    # as the stall signature.
                    _u2_fail_sig, cargo_out = _v.self_failures, _v.detail
                    # EXCEPT when this attempt was dispatched on localized
                    # crash evidence: a correct fix of a LATENT fault moves no
                    # test at baseline by definition — the tests it saves only
                    # crash when the activating sibling fill is in the tree,
                    # and that fill is being crash-reverted by the same fault
                    # (measured 2026-08-09/10: eight crash-evidence re-fills
                    # of the block holding `object_drop` were all reverted for
                    # no strict improvement while `ext_context` crash-reverted
                    # beside them; each half's payoff exists only jointly).
                    # Accept on NON-REGRESSION — no self-failure that was
                    # passing in a completed binary at baseline — and let the
                    # next sweep judge the joint tree. The regression gate
                    # below still runs, and the batch ratchet's snapshot
                    # predates this fill, so a breach claws it back then.
                    if (_crash_evidence and _attempt_baseline_test is not None
                            and not (_v.self_failures
                                     & _attempt_baseline_test.passed_names)):
                        self.tel.log(
                            f"  [phase-c/per-fn] GUARD {rel_file}::{fn_name}: "
                            "crash-evidence re-fill KEPT on non-regression "
                            f"({len(_v.self_failures)} self-failure(s), none "
                            "passing at baseline) — judged for real at the "
                            "next sweep, in the joint tree.")
                        _u2_fail_sig = None
                    # Sibling-blocked pre-check, BEFORE the fix loop spends a
                    # call: when every failing attributed test also needs a
                    # still-stubbed sibling, no rewrite of THIS fn alone can
                    # pass it — a correct body panics inside the sibling's
                    # stub, an avoiding body fails the assertion. The loop
                    # already knows this before (`defer_blocked_consumers`)
                    # and after (`_salvage_compiling_body`) the burn; this is
                    # the during. Keep the compiling body and defer — the
                    # regression gate below still runs, so a body that
                    # reddens siblings still reverts.
                    if (_u2_fail_sig is not None
                            and ctx.tdd_frontier_tests is not None):
                        _held = sibling_blocked_u2_failures(
                            _v.self_failures, FnLocator(rel_file, fn_name),
                            ctx.tdd_frontier_tests.dispatch,
                            ctx.still_stubbed)
                        if _held:
                            _blocked_by[:] = sorted(
                                FnSite(k.file, k.fn_name, "")
                                for k in _held)
                            _names = ", ".join(
                                f"{k.file}::{k.fn_name}"
                                for k in sorted(_held))
                            self.tel.log(
                                f"  [phase-c/per-fn] {idx}/{ctx.total} kept "
                                f"{rel_file}::{fn_name} (compiles; every "
                                f"failing attributed test needs still-stubbed "
                                f"sibling(s): {_names} — no rewrite of this "
                                "fn alone can pass them; fix loop skipped, "
                                "deferred to outer loop).")
                            self.tel.emit(
                                "phase_c.fn.u2_sibling_blocked", data={
                                    "cluster": ctx.cluster_name,
                                    "pass_n": ctx.pass_n,
                                    "file": rel_file, "fn": fn_name,
                                    "failing_tests": sorted(_v.self_failures),
                                    "blocked_on": sorted(
                                        f"{k.file}::{k.fn_name}"
                                        for k in _held),
                                })
                            _u2_fail_sig = None
                elif _v.decision is U2Decision.ACCEPT_EXTERNAL_ONLY:
                    # Compiles, but every attributed failure panics inside a
                    # DIFFERENT still-stubbed fn — not this fn's fault. Keep the
                    # compiling body and defer to the outer TDD loop (re-runs
                    # once the blocking producers fill). Reverting would be
                    # strictly worse.
                    #
                    # Report WHICH sibling held it: a body that landed and
                    # cannot be judged is the unit of evidence `pass_payoff`
                    # needs. When one site blocks every landed body in a pass,
                    # the pass provably could not move a verdict.
                    _blocked_by[:] = sorted(_v.blocking_fns)
                    _ctx2 = f" [{container}]" if container else ""
                    _why = []
                    if _v.external:
                        _why.append(f"{len(_v.external)} attributed test(s) "
                                    "blocked by still-stubbed sibling(s)")
                    if _v.no_verdict:
                        _why.append(f"{len(_v.no_verdict)} unjudged (aborted "
                                    "test process)")
                    self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} kept "
                                 f"{rel_file}::{fn_name}{_ctx2} (compiles; "
                                 f"{'; '.join(_why)} — deferred to outer loop).")
            # Global monotone ratchet: this fn compiles and its own tests pass,
            # but reject it if it reddened any previously-passing cluster test
            # (or dropped the pass count). Routed through the SAME failure path
            # as a U2 test failure — revert + re-prompt with the reddened tests
            # as the stall signature.
            if rc == 0 and _u2_fail_sig is None:
                _regr = self.cargo.test_regression_gate(
                    ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env,
                    _attempt_baseline_test)
                if _regr is not None:
                    _cmp, _regr_detail = _regr
                    _u2_fail_sig = _cmp.signature
                    cargo_out = _regr_detail
                    self.tel.log(
                        f"  [phase-c/per-fn] {fn_name}: reverted — "
                        f"regressed sibling test(s): {_cmp.summary}.")
                    # Crash-only (no newly-red verdict in a completed binary)
                    # with the localized blame wholly outside this unit: the
                    # fill routes execution into a sibling's fault and the
                    # binary dies. The revert STAYS — a memory fault does not
                    # prove this fill innocent the way a stub panic does — but
                    # retrying, diagnosing and convicting the unit charges it
                    # for the sibling's defect (and writes a retrospective
                    # whose premise is false).
                    if (_cmp.newly_crashed and not _cmp.newly_red
                            and blame_outside_unit(
                                ctx.crash_blamed or frozenset(),
                                rel_file, frozenset({fn_name}))):
                        _crash_blocked_mark[:] = [crash_block_mark(
                            ctx.target_src_dir,
                            [b.file for b in (ctx.crash_blamed or frozenset())])]
            if rc == 0 and _u2_fail_sig is None:
                ctx.splice_check_lock.release()
                _lock_released = True
                _ctx = f" [{container}]" if container else ""
                self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} filled "
                             f"{rel_file}::{fn_name}{_ctx} "
                             f"({delta:+d} bytes, body={len(body)}"
                             + (f", fix-attempts={fix_attempt}" if fix_attempt > 1 else "")
                             + ")")
                self.tel.emit("phase_c.fn.fill.done", duration_ms=int(
                        (time.monotonic() - _fn_t0) * 1000),
                      data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name,
                    "body_chars": len(body), "bytes_delta": delta,
                    "fix_attempts": fix_attempt,
                    "recovered": fix_attempt > 1,
                    "success": True,
                })
                self.tel.emit("phase_c.verdict", data={
                    **VerdictRecord(
                        unit=str(_unit_key), route="per-fn",
                        attempt=fix_attempt,
                        disposition=(Disposition.KEEP_DEFERRED if _blocked_by
                                     else Disposition.ACCEPT),
                        reason=("kept — every attributed test blocked by a "
                                "still-stubbed sibling" if _blocked_by
                                else "accepted on own tests"),
                        outcome=(GateOutcome.UNMEASURABLE if _blocked_by
                                 else GateOutcome.POSITIVE),
                        validity=ValidityVector(binary_completed=True),
                        model_calls=fix_attempt).to_json_dict(),
                    "cluster": ctx.cluster_name,
                })
                # Recovered fix-loop → extract a self-note (model writes
                # for itself). High-signal moment: we know the lesson
                # actually worked.
                if fix_attempt > 1 and ctx.fr_path is not None and first_failed_body:
                    note = self.retros.extract_self_note(
                        failed_body=first_failed_body,
                        error_excerpt=first_error_excerpt,
                        fixed_body=body,
                    )
                    # P6.4: tag the note with the error codes from the
                    # original failure so it can be surfaced next time
                    # any of those codes recurs.
                    _ecodes = ecodes_in_text(first_error_excerpt or "")
                    if note and self.retros.append_note(ctx.fr_path, note,
                                                        error_codes=_ecodes):
                        self.tel.log(f"  [model-notes] +1 note (recovered): {note[:80]}...")
                        self.tel.emit("phase_c.note.added", data={
                            "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                            "file": rel_file, "fn": fn_name,
                            "kind": "recovered", "preview": note[:200],
                        })
                return FixOutcome("accepted")

            def _excerpt(max_chars: int | None = None) -> str:
                # `focus_errors` parses cargo output into `error[E…]` blocks
                # keyed on their `-->` location. A U2 detail is not cargo
                # output — it is a rendered explanation (failing test names,
                # test bodies, the abort notice), so nothing parses as a block
                # and it falls through to the head-truncating `output[:cap]`
                # branch, silently cutting whatever the render appended last.
                # Pass it through intact instead of filtering it for
                # diagnostics it was never going to contain.
                if _u2_fail_sig is not None:
                    cap = max_chars or self.caps.fix_err_cap
                    return cargo_out[:cap]
                return self.cargo.focus_errors(cargo_out, rel_file,
                                               max_chars=max_chars)

            # Capture first-failure context for note extraction later.
            if fix_attempt == 1:
                first_failed_body = body
                first_error_excerpt = _excerpt()

            # P5.5: track error codes seen so we can promote recurring
            # ones to every subsequent prompt.
            self.retros.record_error_codes(cargo_out)
            # Plan 10 U2: when the failure is a TEST failure (not compile),
            # the stall signature is the failing-test set so floor-stall
            # works (a test output has no E-codes → cargo_err_sig would be
            # empty and never stall).
            cur_err_sig = (_u2_fail_sig if _u2_fail_sig is not None
                           else cargo_err_sig(cargo_out))
            # An attempt that reached the test gate COMPILED — its compile tier
            # is 0, so it strictly beats every red attempt no matter how many
            # tests are still failing. Scoring both on one count made the first
            # compiling attempt look like a regression and stalled the loop out.
            _compiled = _u2_fail_sig is not None
            cur_metric = FixProgress(
                compile_errors=0 if _compiled else max(1, len(cur_err_sig)),
                failing_tests=len(cur_err_sig) if _compiled else 0)
            # P2: record this failed attempt for the prior-attempts block.
            # We store the focused excerpt (already tier-sliced) so the
            # next prompt sees what really failed without re-paying budget.
            _focused = _excerpt(self.caps.fix_err_cap)
            per_fn_history.append({
                "attempt": fix_attempt,
                "body": body,
                "error_sig": _focused,
                # A compiling body whose tests failed is NOT a compile failure;
                # labelling it one tells the next prompt (PRIOR_ATTEMPTS) to
                # re-fight an error the attempt had already solved.
                "outcome": "test_fail" if _compiled else "compile_fail",
            })
            last_body = body
            last_metric = cur_metric
            salvage.offer(body, cur_metric)
            error_excerpt = _excerpt()
            # Revert to snapshot before returning; the model will see
            # the file as unimplemented again but with last_body + errors
            # in the prompt. (Reverting unconditionally — the exhaustion
            # block below restores + annotates anyway, so the end state
            # on a stall/repeated exit is identical to the old in-loop
            # ordering.)
            _revert_own_body()
            if not _lock_released:
                ctx.splice_check_lock.release()
                _lock_released = True
            if _crash_blocked_mark:
                self.tel.log(
                    f"  [phase-c/per-fn] GUARD {rel_file}::{fn_name}: kept "
                    "reverted and STOPPING — the crash is a sibling's "
                    "localized fault; re-prompting this unit buys the same "
                    "crash until the blamed file changes.")
                return FixOutcome("stop")
            _codes = sorted(ecodes_in_text(cargo_out))
            _kind = "U2 test" if _u2_fail_sig is not None else "compile"
            self.tel.log(f"  [phase-c/per-fn] {fn_name}: {_kind} failed "
                         f"(attempt {fix_attempt}); "
                         f"codes={_codes or '—'}; re-prompting with errors.")
            return FixOutcome("failed", err_sig=cur_err_sig,
                              metric=cur_metric)

        def _on_fn_failure(attempt_n: int, outcome: FixOutcome,
                           gauge: FloorStall) -> None:
            self.tel.emit("loop.iter", data={
                "name": "phase_c.per_fn",
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name,
                "iter": attempt_n,
                "metric_new": outcome.metric.compile_errors,
                "metric_new_failing_tests": outcome.metric.failing_tests,
                "metric_best": str(gauge.best),
                "improved": gauge.stalls == 0,
                "stalled": gauge.stalls,
            })

        def _on_fn_stall(attempt_n: int, gauge: FloorStall) -> None:
            self.tel.log(f"  [phase-c/per-fn] {fn_name}: floor stalled at "
                         f"{gauge.best} for {gauge.stalls} iter(s) — "
                         f"stopping fix loop (attempt {attempt_n}).")

        def _on_fn_repeated(attempt_n: int, gauge: FloorStall) -> None:
            self.tel.log(f"  [phase-c/per-fn] {fn_name}: same error set after "
                         f"attempt {attempt_n} — stopping fix loop.")

        # === Retro-informed retry loop: a failed fix loop gets a
        # retrospective diagnosis and, while each round STRICTLY improves
        # this fn's best error count, re-enters the fix loop immediately
        # with that guidance in the prompt — instead of parking the fn
        # behind an annotated comment until a later pass happens to
        # re-admit it. Strictly decreasing error counts terminate on their
        # own; the round cap is a runaway backstop, not the working bound.
        _retro_rounds_max = int(os.getenv("PER_FN_RETRO_ROUNDS_MAX", "25"))
        _retro_round = 0
        success = False
        fix_attempt = 0
        while True:
            _retro_round += 1
            success, fix_attempt = drive_fix_loop(
                _attempt_fn_fill,
                hard_cap=self.caps.per_fn_fix_hard_cap,
                max_stalls=self.stall.effective_stalls(
                    self.caps.per_fn_fix_max_stalls, module_of(rel_file)),
                stop_context=f"per-fn fix for {fn_name}",
                on_failure=_on_fn_failure,
                on_stall=_on_fn_stall,
                on_repeated=_on_fn_repeated,
                check_stop=self.tel.check_stop,
            )
            if success:
                break
            if _crash_blocked_mark:
                # No retrospective and no conviction: the failure was never
                # about this body, and a diagnosis of it would write guidance
                # whose premise is false into the store the loop later
                # believes. The unit exits crash-blocked below instead.
                break

            # retrospective: actively diagnose why the fix loop
            # exhausted and prescribe a different strategy for the next
            # attempt. Persisted across module iterations. Runs on EVERY
            # failed round so the improvement gate always has a fresh
            # measurement.
            #
            # Exhaustion is PROGRESS-GATED — no hard cap on the number
            # of retros. A retro fires; if the next-best error count
            # is strictly lower than the prior best (real improvement),
            # the fn stays eligible AND re-enters the fix loop below.
            # If it isn't, the fn is marked fully exhausted on the spot.
            _retry_informed = False
            try:
                _retros = self.retros.load(ctx.project_dir)
                _retro_k = self.retros.retro_key(rel_file, fn_name, container)
                _prior = _retros.get(_retro_k, {})
                _prior_count = int(_prior.get("retrospective_count", 0))
                _prior_best = best_progress_of(_prior)
                # The diagnosis is asked to build ON the prior lesson — a lesson
                # this layout disproves would just be restated in the new one.
                _prior_guidance = admissible_guidance(
                    _prior, snapshot, evidence=_evidence,
                    anchor_types=_anchor_types)
                _retro = self.retros.diagnose(
                    fn_name=fn_name, sig_text=sig_text,
                    container=container, file_rel=rel_file,
                    cluster_name=ctx.cluster_name,
                    last_body=last_body or first_failed_body or "",
                    error_excerpt=error_excerpt or first_error_excerpt or "",
                    fix_attempts=fix_attempt,
                    prior_retrospective=_prior_guidance,
                    bound_methods_section=retro_bound_methods,
                )
                # Transient empties (llm_error / empty_response) don't
                # update state — we never saw a real fix attempt's
                # outcome, so they cannot count toward exhaustion (and
                # don't justify an immediate retry either).
                _signal = _retro.get("signal", "")
                _transient = _signal in ("llm_error", "empty_response")
                # Every error is in ANOTHER file: the crate is broken
                # elsewhere and this fn is probably fine. Its error count
                # reflects the blocker, not this body — folding it into
                # best_error_count would exhaust an innocent fn the moment
                # the blocker persists across two rounds (observed: a
                # corrupted error.rs permanently parked unrelated fns in
                # backtrace.rs and nightly.rs). Record the diagnosis but
                # never exhaust, never move the baseline, and don't retry
                # now (no body change can fix a downstream break).
                _downstream = _signal == "downstream_only"
                if not _transient:
                    # The attempt's own metric, not a re-parse of its text.
                    _cur_progress = last_metric or FixProgress(
                        len(cargo_err_sig(
                            error_excerpt or first_error_excerpt or "")), 0)
                    # First retro for this fn establishes a baseline —
                    # no prior to compare, so we never exhaust on
                    # round 1. Subsequent retros exhaust unless
                    # current count is strictly lower than prior best.
                    if _downstream:
                        _new_best = _prior_best
                        _improved = False
                        _full_exhaust = False
                    elif _prior_best is None:
                        _new_best = _cur_progress
                        _improved = True
                        _full_exhaust = (
                            _retro.get("category") in ("GENUINELY_HARD",
                                                       "FROZEN_BOUND_MISMATCH")
                        )
                    else:
                        _new_best = min(_prior_best, _cur_progress)
                        _improved = _cur_progress < _prior_best
                        _full_exhaust = (
                            # FROZEN_BOUND_MISMATCH: structurally proven
                            # unsatisfiable — park immediately like
                            # GENUINELY_HARD.
                            _retro.get("category") in ("GENUINELY_HARD",
                                                       "FROZEN_BOUND_MISMATCH")
                            or not _improved
                        )
                    _new_count = _prior_count + 1
                    # Stamped with the context it was earned in (same cluster,
                    # layout, gate and route set), the convicting tests it was
                    # drawn from, AND the hollow siblings the attempt ran
                    # against: the next reader honours the verdict only where
                    # all three still hold. The third is what a verdict like
                    # `src/error.rs::new`'s needs — "the vtable functions
                    # referenced in the body are not implemented in the crate"
                    # is an argument that expires the moment they are.
                    _blockers = BlockedOnStubs.at_conviction(
                        last_body or first_failed_body or "", snapshot,
                        exclude=fn_name)
                    _retros[_retro_k] = stamp_conviction({
                        "category": _retro.get("category", ""),
                        "guidance": _retro.get("guidance", ""),
                        "signal": _signal,
                        "retrospective_count": _new_count,
                        "error_count": _cur_progress.compile_errors,
                        "failing_test_count": _cur_progress.failing_tests,
                        **(best_progress_fields(_new_best)
                           if _new_best is not None else {}),
                        "fully_exhausted": _full_exhaust,
                        "last_error_preview": (error_excerpt or "")[:240],
                    }, _provenance, _evidence, _blockers)
                    self.retros.save(ctx.project_dir, _retros)
                    # Make the fresh guidance visible to this fn's OWN next
                    # round (the fix prompt reads ctx.retros_cache) and to
                    # every later dispatch in this pass.
                    ctx.retros_cache[_retro_k] = _retros[_retro_k]
                    _retry_informed = not _full_exhaust and not _downstream
                    self.tel.log(f"  [phase-c/retro] {fn_name} "
                                 f"[{_retro.get('category','?')}] "
                                 f"signal={_signal} "
                                 f"errs={_cur_progress} "
                                 f"(prior_best={_prior_best}, "
                                 f"improved={_improved}, "
                                 f"exhausted={_full_exhaust}): "
                                 f"{_retro.get('guidance','')[:120]}...")
                    self.tel.emit("phase_c.retrospective", data={
                        "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                        "file": rel_file, "fn": fn_name,
                        "category": _retro.get("category", ""),
                        "signal": _signal,
                        "retrospective_count": _new_count,
                        "error_count": _cur_progress.compile_errors,
                        "failing_test_count": _cur_progress.failing_tests,
                        "best_error_count": str(_new_best),
                        "improved": _improved,
                        "fully_exhausted": _full_exhaust,
                        "preview": _retro.get("guidance", "")[:200],
                    })
                    self.tel.emit("phase_c.verdict", data={
                        **VerdictRecord(
                            unit=str(_unit_key), route="per-fn",
                            attempt=fix_attempt,
                            disposition=(Disposition.CONVICTION
                                         if _full_exhaust
                                         else Disposition.REJECT),
                            reason=f"retro round {_retro_round}: "
                                   f"category="
                                   f"{_retro.get('category', '')!r} "
                                   f"signal={_signal!r} "
                                   f"improved={_improved}",
                            outcome=(GateOutcome.UNMEASURABLE
                                     if _downstream
                                     else GateOutcome.NEGATIVE),
                            model_calls=fix_attempt).to_json_dict(),
                        "cluster": ctx.cluster_name,
                    })
            except Exception as _re:
                self.tel.log(f"  [phase-c/retro] failed for {fn_name}: {_re}")
            if not (_retry_informed and _retro_round < _retro_rounds_max):
                break
            self.tel.log(f"  [phase-c/per-fn] {fn_name}: retro round "
                         f"{_retro_round} kept the error floor improving — "
                         "re-entering the fix loop with the new guidance.")
            self.tel.emit("phase_c.fn.retro_retry", data={
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name,
                "retro_round": _retro_round,
            })

        if not success and _crash_blocked_mark:
            # Plain revert only: no salvage (the best compiling body IS the
            # crash-activating one), no annotated stub (the body was never
            # shown wrong), no conviction, no exhaustion. Attempts stay in the
            # returned telemetry — they were real model calls — but nothing
            # persistent moves against this unit.
            with ctx.splice_check_lock:
                _revert_own_body()
            if ctx.crash_blocked is not None:
                ctx.crash_blocked[_unit_key] = _crash_blocked_mark[0]
            self.tel.log(
                f"  [phase-c/per-fn] GUARD {rel_file}::{fn_name} "
                f"[{container}]: crash-blocked ({_crash_blocked_mark[0]}) — "
                "no attempt charged, no retrospective written; re-admitted "
                "when the blamed file changes.")
            self.tel.emit("phase_c.fn.crash_blocked", data={
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name, "container": container,
                "mark": _crash_blocked_mark[0],
            })
            self.tel.emit("phase_c.verdict", data={
                **VerdictRecord(
                    unit=str(_unit_key), route="per-fn", attempt=attempts,
                    disposition=Disposition.KEEP_DEFERRED,
                    reason="crash-blocked on a sibling's fault — no charge, "
                           "re-admitted when the blamed file changes",
                    outcome=GateOutcome.UNMEASURABLE,
                    validity=ValidityVector(binary_completed=False,
                                            blame_outside_unit=True),
                    evidence_keys=(_crash_blocked_mark[0],),
                    model_calls=attempts).to_json_dict(),
                "cluster": ctx.cluster_name,
            })
            return FillResult(file=rec.file, fn_name=fn_name,
                              filled=False, attempts=attempts)
        if not success:
            # Restore the snapshot first (clears whatever broken body the
            # loop left in place), then splice an annotated stub that
            # preserves the last attempt + cargo error as comments. The
            # fn stays callable (unimplemented!() at the end) and a later
            # pass will retry — by then more siblings in the file will be
            # implemented, giving richer context. Lock the file-write
            # phase against concurrent workers.
            with ctx.splice_check_lock:
                _revert_own_body()
                _salvaged = self._salvage_compiling_body(
                    ctx, salvage, file_path, rel_file, fn_name, container,
                    sig_text, rec, _revert_own_body)
                if not _salvaged:
                    annotated_body = make_failed_attempt_body(
                        attempted_body=last_body or first_failed_body or "",
                        error_excerpt=error_excerpt or first_error_excerpt or "",
                        fix_attempts=fix_attempt,
                    )
                    # Container-scoped: annotate THIS fn's body, never a
                    # same-named sibling in another impl (the `from`/`fmt`/`not`
                    # collision). The annotated body deliberately ENDS in
                    # `unimplemented!()` so the crate still compiles and the stub
                    # scan re-finds it — hence allow_stub_body, without which the
                    # write is refused `body_is_stub` every single time and the
                    # failure history never reaches the next pass.
                    _ann = self.mut.splice_body_scoped(
                        file_path, fn_name, container, sig_text, annotated_body,
                        cfg_predicates=rec.cfg_predicates, allow_stub_body=True)
                    if not _ann.ok:
                        self.tel.log(f"  [phase-c/per-fn] ANNOTATE FAILED for "
                                     f"{rel_file}::{fn_name}: {_ann.reason} — "
                                     "exhausted fn not re-stubbed.")
            exhaust_error = (f"{rel_file}::{fn_name}: fix loop exhausted "
                             f"after {fix_attempt} attempt(s)")
            self.tel.emit("phase_c.fn.fill.fail", duration_ms=int(
                    (time.monotonic() - _fn_t0) * 1000),
                  data={
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name,
                "fix_attempts": fix_attempt,
                "retro_rounds": _retro_round,
                "salvaged_body_kept": bool(_salvaged),
                "last_error_chars": len(error_excerpt),
                "last_error_preview": error_excerpt[:200],
                "error_codes": sorted(ecodes_in_text(
                    (error_excerpt or "") + " " + (first_error_excerpt or ""))),
            })
            # Exhausted fix-loop — extract a "what didn't work" note so the
            # model doesn't repeat the same dead-end approach on the next pass.
            if ctx.fr_path is not None and (first_failed_body or last_body):
                note = self.retros.extract_self_note(
                    failed_body=last_body or first_failed_body,
                    error_excerpt=error_excerpt or first_error_excerpt,
                    fixed_body="",  # no fixed body — fix loop exhausted
                )
                # P6.4: tag exhausted-fail notes with the codes seen
                # across the run so they re-surface for the same code.
                _ecodes2 = ecodes_in_text((error_excerpt or "")
                                          + " " + (first_error_excerpt or ""))
                if note and self.retros.append_note(ctx.fr_path, note,
                                                    error_codes=_ecodes2):
                    self.tel.log(f"  [model-notes] +1 note (exhausted): {note[:80]}...")
                    self.tel.emit("phase_c.note.added", data={
                        "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                        "file": rel_file, "fn": fn_name,
                        "kind": "exhausted", "preview": note[:200],
                    })
            try:
                ts = int(time.time())
                fpath = ctx.failures_dir / f"{fn_name}-{ts}.txt"
                fpath.write_text(
                    f"# fn: {rel_file}::{fn_name}\n"
                    f"# fix_attempts: {fix_attempt}\n"
                    f"# last_response_chars: {len(last_response)}\n"
                    f"# last_error_excerpt: {error_excerpt[:500]}\n"
                    f"{'=' * 60}\n## ATTEMPTS LOG\n{'=' * 60}\n"
                    + "\n".join(attempts_log) + "\n"
                    f"{'=' * 60}\n## LAST RESPONSE\n{'=' * 60}\n{last_response}\n"
                    f"{'=' * 60}\n## LAST ERROR EXCERPT\n{'=' * 60}\n{error_excerpt}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            # The disposition summary must agree with the salvage decision it
            # follows: on the anyhow run `298540-f792975a` (2026-08-12),
            # `src/chain.rs::new` logged `KEEPING best compiling body`
            # immediately followed by `reverted after 2 attempt(s)` while the
            # on-disk file held the compiling body — and the summary is what
            # exhaustion charging and retro readers quote.
            if _salvaged:
                self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} "
                             f"{rel_file}::{fn_name}: fix loop exhausted after "
                             f"{fix_attempt} attempt(s) ({_retro_round} retro "
                             "round(s)) — best compiling body KEPT, deferred "
                             "to the outer loop.")
            else:
                self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} "
                             f"{rel_file}::{fn_name}: "
                             f"reverted after {fix_attempt} attempt(s) "
                             f"({_retro_round} retro round(s)).")
            return FillResult(file=rel_file, fn_name=fn_name, filled=False,
                              attempts=attempts, error=exhaust_error)

        return FillResult(file=rel_file, fn_name=fn_name, filled=True,
                          attempts=attempts, blocked_by=tuple(_blocked_by))
