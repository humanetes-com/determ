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
from dataclasses import dataclass
from typing import Any

from pipeline_core.application.fix_loop import FixOutcome, drive_fix_loop
from pipeline_core.application.reconstruct.cargo_errors import (
    ecodes_in_text,
)
from pipeline_core.application.reconstruct.cargo_errors import (
    err_sig as cargo_err_sig,
)
from pipeline_core.application.reconstruct.context import PerFnPassCtx, fill_skip
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
from pipeline_core.domain.models import FillResult, FnLocator, FnSite, Stub
from pipeline_core.domain.refine import FloorStall
from pipeline_core.domain.std_api_sigs import std_api_def
from pipeline_core.domain.test_report import U2Decision
from pipeline_core.domain.type_refs import types_referenced_in_text
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

    def run(self, idx: int, rec: Stub, *, ctx: PerFnPassCtx) -> FillResult:
        rel_file = rec.file
        fn_name = rec.fn_name
        container = rec.container
        sig_text = rec.sig_text
        attempts = 0   # LLM attempts this fn consumed — returned in FillResult
        # A crash-implicated fn is never exhaustion-skipped: the localized
        # faulting stack is evidence no prior attempt ever saw.
        _crash_evidence = (ctx.crash_evidence or {}).get(
            FnLocator(rel_file, fn_name), "")
        if not _crash_evidence and self.retros.is_fn_fully_exhausted(
                ctx.retros_cache, rel_file, fn_name, container):
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
            return fill_skip(rec, attempts)
        self.tel.check_stop(f"per-fn pass {ctx.pass_n} fn {fn_name}")
        file_path = ctx.target_src_dir / rel_file
        try:
            snapshot = file_path.read_text(encoding="utf-8")
        except Exception:
            return fill_skip(rec, attempts)
        # Resolve current offsets — file may have been modified by prior
        # fills in this pass, shifting byte positions. Re-enumerate to
        # find this specific record's current location.
        loc_at = self.mut.locate_fn_by_record(file_path, fn_name, container, sig_text,
                                               rec.cfg_predicates)
        _cur_body = ""   # debt #46: current (wrong) body for a behavioral re-fill
        if loc_at is None:
            # Fall back to regex locator (for tests-fixture path where
            # the extractor binary isn't built).
            loc = self.mut.locate_fn(snapshot, fn_name)
            if loc is None:
                return fill_skip(rec, attempts)
            sig_text = sig_text or loc[0]
        else:
            _, _cbs, _cbe, sig_text = loc_at
            if rec.refill:
                _cur_body = snapshot[_cbs:_cbe]
        # `snapshot` is the WHOLE FILE as it stood before this unit queued for
        # the splice lock. Under parallelism several units in the same file
        # snapshot the same starting state, so by the time a losing unit
        # reverts, its image predates every sibling fill that landed meanwhile
        # — writing it back erases fns already reported as filled (observed:
        # three `filled` constructors back to `unimplemented!()`). Revert only
        # THIS fn, at offsets resolved against the file as it stands now.
        _own_pre_image = _cur_body or "unimplemented!()"

        def _revert_own_body() -> None:
            self.mut.revert_body_scoped(
                file_path, fn_name, container, sig_text, _own_pre_image,
                cfg_predicates=rec.cfg_predicates)

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
            "refill": rec.refill,   # debt #46: behavioral re-fill of a wrong body
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
        # debt #46: a behavioral re-fill enters the loop already "in fix mode" —
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
        # Corrected R2/R3 (debt #28): carry the bound-provided method inventory
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
                first_error_excerpt, retro_bound_methods
            attempts += 1
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
            # debt #46: a re-fill target is corrective from attempt 1 — take the
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
                prompt = build_per_fn_fix_prompt(
                    file_text=cur_text, fn_name=fn_name, sig_text=sig_text,
                    test_blocks=test_blocks, module_name=ctx.module_name,
                    cluster_name=ctx.cluster_name, file_rel=rel_file,
                    prev_body=last_body, error_excerpt=error_excerpt,
                    sections=PerFnFixSections(
                        macros=ctx.macros_section, notes=notes_section,
                        container=container, types=_types_section_eff,
                        invariants=ctx.invariants_section,
                        # FR-024: prior retrospective guidance — start from "what to
                        # try instead" rather than naive retry-with-errors.
                        retrospective_guidance=(ctx.retros_cache.get(
                            self.retros.retro_key(rel_file, fn_name, container),
                            {}).get("guidance", "")),
                        # FR-024 fix 3: hoist already-filled sibling fn impls (same
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
            _splice_loc = self.mut.locate_fn_by_record(
                file_path, fn_name, container, sig_text, rec.cfg_predicates)
            # A behavioral re-fill target replaces an already-filled
            # (compiling-but-wrong) body → the splice must accept a non-stub.
            if _splice_loc is not None:
                _, _bs, _be, _ = _splice_loc
                splice_out = self.mut.splice_fn_body_at(
                    file_path, _bs, _be, body, allow_nonstub=rec.refill)
            else:
                # Fallback: regex-based locator. Less accurate for
                # same-name collisions but acceptable when the syn
                # binary isn't available.
                splice_out = self.mut.splice_fn_body(
                    file_path, fn_name, body, allow_nonstub=rec.refill)
            ok, delta = splice_out.ok, splice_out.delta
            if ok and splice_out.reason == "ok_xml_unescaped":
                self.tel.log(f"  [phase-c/per-fn] {fn_name}: landed only after "
                             "resolving XML entities in the body — the reply "
                             "used the escaped rendering.")
            if not ok:
                ctx.splice_check_lock.release()
                _lock_released = True
                # A `target_not_stub` refusal is usually another worker
                # filling this fn between our read and our splice (race).
                # Confirm via a quick re-read; every other reason is its
                # own diagnosis (debt #39) — no guessing needed.
                _race_detected = False
                if splice_out.reason == "target_not_stub":
                    try:
                        _cur_text = file_path.read_text(encoding="utf-8")
                        _race_loc = self.mut.locate_fn_by_record(
                            file_path, fn_name, container, sig_text, rec.cfg_predicates)
                        if _race_loc is not None:
                            _, _race_bs, _race_be, _ = _race_loc
                            _race_cur_body = _cur_text[_race_bs:_race_be]
                            if not is_unimplemented_body(_race_cur_body):
                                _race_detected = True
                    except Exception:
                        pass
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
                _key = FnLocator(rel_file, fn_name)
                # Container-qualified own-site: a test panicking in a same-named
                # sibling (e.g. this fn is a `Bool` impl's `not`, the panic is in
                # the free `not(cond)` stub) is external, not this fn's failure.
                _v = self.cargo.u2_test_gate(
                    (ctx.tdd_frontier_tests or {}).get(_key, set()),
                    frozenset({FnSite(rel_file, fn_name, container)}),
                    ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env)
                if _v.decision is U2Decision.REJECT:
                    # A real self-failure (assertion/behavior). Route through the
                    # normal fix path: revert + re-prompt with the failing tests
                    # as the stall signature.
                    _u2_fail_sig, cargo_out = _v.self_failures, _v.detail
                elif _v.decision is U2Decision.ACCEPT_EXTERNAL_ONLY:
                    # Compiles, but every attributed failure panics inside a
                    # DIFFERENT still-stubbed fn — not this fn's fault. Keep the
                    # compiling body and defer to the outer TDD loop (re-runs
                    # once the blocking producers fill). Reverting would be
                    # strictly worse.
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

            # Capture first-failure context for note extraction later.
            if fix_attempt == 1:
                first_failed_body = body
                first_error_excerpt = self.cargo.focus_errors(cargo_out, rel_file)

            # P5.5: track error codes seen so we can promote recurring
            # ones to every subsequent prompt.
            self.retros.record_error_codes(cargo_out)
            # Plan 10 U2: when the failure is a TEST failure (not compile),
            # the stall signature is the failing-test set so floor-stall
            # works (a test output has no E-codes → cargo_err_sig would be
            # empty and never stall).
            cur_err_sig = (_u2_fail_sig if _u2_fail_sig is not None
                           else cargo_err_sig(cargo_out))
            # P2: record this failed attempt for the prior-attempts block.
            # We store the focused excerpt (already tier-sliced) so the
            # next prompt sees what really failed without re-paying budget.
            _focused = self.cargo.focus_errors(
                cargo_out, rel_file, max_chars=self.caps.fix_err_cap)
            per_fn_history.append({
                "attempt": fix_attempt,
                "body": body,
                "error_sig": _focused,
                "outcome": "compile_fail",
            })
            # P7.2: floor-stall metric — rustc error count (the driver's
            # FloorStall observes it and owns the stall/repeated exits).
            cur_err_count = len(cur_err_sig)
            last_body = body
            error_excerpt = self.cargo.focus_errors(cargo_out, rel_file)
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
            _codes = sorted(ecodes_in_text(cargo_out))
            _kind = "U2 test" if _u2_fail_sig is not None else "compile"
            self.tel.log(f"  [phase-c/per-fn] {fn_name}: {_kind} failed "
                         f"(attempt {fix_attempt}); "
                         f"codes={_codes or '—'}; re-prompting with errors.")
            return FixOutcome("failed", err_sig=cur_err_sig,
                              err_count=cur_err_count)

        def _on_fn_failure(attempt_n: int, outcome: FixOutcome,
                           gauge: FloorStall) -> None:
            self.tel.emit("loop.iter", data={
                "name": "phase_c.per_fn",
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name,
                "iter": attempt_n,
                "metric_new": outcome.err_count,
                "metric_best": gauge.best,
                "improved": gauge.stalls == 0,
                "stalled": gauge.stalls,
            })

        def _on_fn_stall(attempt_n: int, gauge: FloorStall) -> None:
            self.tel.log(f"  [phase-c/per-fn] {fn_name}: floor stalled at "
                         f"{gauge.best} errors for {gauge.stalls} iter(s) — "
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

            # FR-024 retrospective: actively diagnose why the fix loop
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
                _prior_best = _prior.get("best_error_count")
                _prior_guidance = _prior.get("guidance", "")
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
                    _cur_errs = len(cargo_err_sig(
                        error_excerpt or first_error_excerpt or ""
                    ))
                    # First retro for this fn establishes a baseline —
                    # no prior to compare, so we never exhaust on
                    # round 1. Subsequent retros exhaust unless
                    # current count is strictly lower than prior best.
                    if _downstream:
                        _new_best = _prior_best
                        _improved = False
                        _full_exhaust = False
                    elif _prior_best is None:
                        _new_best = _cur_errs
                        _improved = True
                        _full_exhaust = (
                            _retro.get("category") in ("GENUINELY_HARD",
                                                       "FROZEN_BOUND_MISMATCH")
                        )
                    else:
                        _new_best = min(int(_prior_best), _cur_errs)
                        _improved = _cur_errs < int(_prior_best)
                        _full_exhaust = (
                            # FROZEN_BOUND_MISMATCH: structurally proven
                            # unsatisfiable — park immediately like
                            # GENUINELY_HARD.
                            _retro.get("category") in ("GENUINELY_HARD",
                                                       "FROZEN_BOUND_MISMATCH")
                            or not _improved
                        )
                    _new_count = _prior_count + 1
                    _retros[_retro_k] = {
                        "category": _retro.get("category", ""),
                        "guidance": _retro.get("guidance", ""),
                        "signal": _signal,
                        "retrospective_count": _new_count,
                        "error_count": _cur_errs,
                        "best_error_count": _new_best,
                        "fully_exhausted": _full_exhaust,
                        "last_error_preview": (error_excerpt or "")[:240],
                    }
                    self.retros.save(ctx.project_dir, _retros)
                    # Make the fresh guidance visible to this fn's OWN next
                    # round (the fix prompt reads ctx.retros_cache) and to
                    # every later dispatch in this pass.
                    ctx.retros_cache[_retro_k] = _retros[_retro_k]
                    _retry_informed = not _full_exhaust and not _downstream
                    self.tel.log(f"  [phase-c/retro] {fn_name} "
                                 f"[{_retro.get('category','?')}] "
                                 f"signal={_signal} "
                                 f"errs={_cur_errs} "
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
                        "error_count": _cur_errs,
                        "best_error_count": _new_best,
                        "improved": _improved,
                        "fully_exhausted": _full_exhaust,
                        "preview": _retro.get("guidance", "")[:200],
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
                annotated_body = make_failed_attempt_body(
                    attempted_body=last_body or first_failed_body or "",
                    error_excerpt=error_excerpt or first_error_excerpt or "",
                    fix_attempts=fix_attempt,
                )
                # Container-scoped: annotate THIS fn's body, never a same-named
                # sibling in another impl (the `from`/`fmt`/`not` collision).
                self.mut.splice_body_scoped(
                    file_path, fn_name, container, sig_text, annotated_body,
                    cfg_predicates=rec.cfg_predicates)
            exhaust_error = (f"{rel_file}::{fn_name}: fix loop exhausted "
                             f"after {fix_attempt} attempt(s)")
            self.tel.emit("phase_c.fn.fill.fail", duration_ms=int(
                    (time.monotonic() - _fn_t0) * 1000),
                  data={
                "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                "file": rel_file, "fn": fn_name,
                "fix_attempts": fix_attempt,
                "retro_rounds": _retro_round,
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
            self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} {rel_file}::{fn_name}: "
                         f"reverted after {fix_attempt} attempt(s) "
                         f"({_retro_round} retro round(s)).")
            return FillResult(file=rel_file, fn_name=fn_name, filled=False,
                              attempts=attempts, error=exhaust_error)

        return FillResult(file=rel_file, fn_name=fn_name, filled=True,
                          attempts=attempts)
