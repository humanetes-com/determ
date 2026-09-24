"""FillOneFn — the per-fn fill use-case.

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
import uuid
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
from pipeline_core.application.reconstruct.fill_reading import (
    last_comparison,
    note_kept_comparison,
)
from pipeline_core.application.reconstruct.fill_rules import (
    REFILL_SEED_EXCERPT,
    AttemptFailure,
    fix_prompt_kind,
    is_unimplemented_body,
    make_failed_attempt_body,
    module_of,
    seed_attempt_failure,
)
from pipeline_core.application.reconstruct.import_repair import (
    repair_missing_imports,
)
from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    reconstruction_file_reader,
)
from pipeline_core.application.reconstruct.tdd_ratchet import (
    hollow_count,
    snapshot_src_tree,
)
from pipeline_core.application.reconstruct.unit_conversation import TranscriptBound
from pipeline_core.domain import unit_clock
from pipeline_core.domain.behaviour_select import IdentityMustUnit, select_usage_examples
from pipeline_core.domain.cfg import filter_cfg_inactive_branches
from pipeline_core.domain.coverage_slice import (
    unreached_by as coverage_unreached_by,
)
from pipeline_core.domain.crash_site import blame_outside_unit
from pipeline_core.domain.declined import declined_guidance
from pipeline_core.domain.dispatch import sibling_blocked_u2_failures
from pipeline_core.domain.fill_class import (
    FillOutcomeClass,
    class_for_kept,
    tests_could_not_run,
)
from pipeline_core.domain.impl_routing import norm_src_rel
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.model_notes import (
    NoteProvenance,
    body_fingerprint,
    rejected_symbols_in_errors,
)
from pipeline_core.domain.models import FillResult, FnLocator, FnSite, Stub
from pipeline_core.domain.reasoning_cap import serving_units
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.refill_evidence import (
    asserted_output_literals_from_tests,
    pinned_observables_from_tests,
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
    GuidanceScreening,
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
    informed_reentry,
    judge_guidance,
    pins_for_contradiction_guard,
    regressed_tests_from_excerpt,
    regression_scoped,
    retired_guidance_line,
    screen_guidance,
    screening_unit,
    stamp_conviction,
    tree_premise_absent,
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
from pipeline_core.prompts.layout_authoring import identity_must_instruction
from pipeline_core.prompts.per_fn import (
    build_per_fn_fix_prompt,
    build_per_fn_prompt,
    sig_change_permission,
)
from pipeline_core.prompts.retry_turn import (
    BODY_OUTPUT,
    AttemptFeedback,
    EvidenceSection,
    types_new_to,
)
from pipeline_core.prompts.sections import render_types_section


def _cfg_dead_fns_of(ctx: PerFnPassCtx, rel_file: str) -> frozenset[str]:
    """The fns of `rel_file` this cluster never compiles (`ctx.cfg_dead`),
    in the retro gate's vocabulary: a conviction blocked on one of them can
    never see it fill, so it must not keep the record alive."""
    rel = norm_src_rel(rel_file)
    return frozenset(fn for f, fn in ctx.cfg_dead if f == rel)

# Attributed leaves whose source is sliced for pins — the impl routes' cap.
_ATTRIBUTED_TEST_LEAF_CAP = 12


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
        _hollow_before = hollow_count(snapshot_src_tree(ctx.target_src_dir))
        _sp = self.mut.splice_body_scoped(
            file_path, fn_name, container, sig_text, best.body,
            cfg_predicates=rec.cfg_predicates, allow_nonstub=True)
        if not _sp.ok:
            self.tel.log(f"  [phase-c/per-fn] salvage splice refused for "
                         f"{rel_file}::{fn_name}: {_sp.reason} — stub kept.")
            return False
        _rc, _ = self.cargo.run(ctx.check_cmd, ctx.target_src_dir, ctx.cluster_env)
        _regr = (self.cargo.test_regression_gate(
            ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env, _base,
            stub_free=ctx.stub_free, hollow_before=_hollow_before)
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
        # Positive evidence the latest accepted U2 saw. A list so the nested
        # attempt closure can write without ``nonlocal``. CASHED at the
        # return site must read this, not the else of ``not _blocked_by``.
        _observed_passed: list[frozenset[str]] = [frozenset()]
        # Non-empty iff the latest revert was a crash-only regression blamed
        # wholly outside this unit — holds the crash-block mark to persist.
        _crash_blocked_mark: list[str] = []
        # Non-empty once a compiling attempt proved, by an attempt-invariant
        # reading, that no rewrite of THIS body can move its failing tests —
        # every one needs a still-stubbed sibling, or none executes the body.
        # Holds the reason. From then on a failed attempt (a sibling
        # regression the gate below reverts, or a compile error) ends the
        # unit instead of re-prompting: the best a retry can reach is a body
        # that regresses nothing and still pays nothing.
        _unpayable_mark: list[str] = []
        # HOW the last judged attempt failed, and — when the test process
        # died under it — what the crashed binaries printed: the stub-panic
        # sites outside this unit (`None` while the last failure was not a
        # crash; empty when it was and no stub panic was located, i.e. a
        # segfault) and the unit's own rendered panics. Read by the retro
        # writer (the record is stamped `crash_stub_sites`, never exhausted
        # on an abort the tree's stubs decided) and by the failed-attempt
        # comment (labelled by failure kind, never "did not compile" over a
        # body that compiled). Lists so the attempt closure can write them
        # without `nonlocal`, like the holders above.
        _last_failure: list[AttemptFailure] = [AttemptFailure.COMPILE]
        _last_crash_sites: list[frozenset[tuple[str, int]] | None] = [None]
        _last_own_panics: list[tuple[str, ...]] = [()]
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
        # Two readings of what this fn answers for: the convicting tests (the
        # acceptance gate's definition, `u2_test_gate` below) and every
        # attributed test, escalated included. A record earned from test
        # outcomes is stamped with and read under the second
        # (`evidence_for_record`); the guidance screen judges every record
        # against it.
        _unit_loc = FnLocator(rel_file, fn_name)
        _evidence = ConvictionEvidence(
            frozenset(ctx.tdd_frontier_tests.convicting_for(_unit_loc))
            if ctx.tdd_frontier_tests is not None else frozenset())
        _answers = ConvictionEvidence(
            frozenset(ctx.tdd_frontier_tests.tests_for(_unit_loc))
            if ctx.tdd_frontier_tests is not None else frozenset())
        _poisoned_retro = ctx.retro_gate.record(judge_guidance(
            _retro_rec.get("guidance", ""), snapshot,
            anchor_types=_anchor_types)) is RetroGateVerdict.RETIRED
        if _poisoned_retro:
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: retro guidance contradicts the "
                         "current layout — guidance withheld, exhaustion not "
                         "honoured (layout-contradicted).")
        # The record was written from a REGRESSION of tests this fn does not
        # answer for: its lesson explains what un-reddens those, which on a
        # re-fill for other tests can be the removal of the observable they
        # pin (`regression_scoped`). The exhaustion it carries counted
        # attempts judged on the wrong tests.
        elif regression_scoped(_retro_rec, _answers):
            _poisoned_retro = True
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: retro guidance was written from a "
                         "regression of test(s) this fn does not answer for "
                         "— retro guidance withheld (regression_scoped), "
                         "exhaustion not honoured (regression-scoped).")
        # The record was earned from a crash/abort the tree's still-stubbed
        # fns decided (`crash_earned_conviction`): the lesson explains an
        # abort the stubs caused, and the exhaustion counted attempts no
        # body could have passed. Judged BEFORE the entry-premise rules:
        # those flip to "moved" while the body is a stub and back to
        # "current" the moment the gate keeps the correct body — which is
        # when the stored lesson would be injected against it.
        elif crash_earned_conviction(_retro_rec):
            _poisoned_retro = True
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: retro guidance was earned from a "
                         "crash/abort the tree's still-stubbed fn(s) decided "
                         "— retro guidance withheld, exhaustion not honoured "
                         "(crash_earned).")
        # A stored lesson with `blocked_on` absent or empty cannot expire
        # on its own evidence and must not convict the unit into a skip
        # either: honouring `fully_exhausted` here would mean the next
        # launch never screens, so the ninth rule would have nothing to
        # retire. Run `9933-efe68f37` left `src/error.rs::msg::Error` and
        # `::context::Error` exhausted with empty `blocked_on`; without
        # this clause the relaunch would skip them and the `force_capture`
        # / `::<C, Error>` sentences would stay out of prompts only because
        # there was no prompt. Same shape as the impl route's
        # impossibility-unfounded skip: guidance withheld, exhaustion not
        # honoured, record stays.
        elif tree_premise_absent(_retro_rec):
            _poisoned_retro = True
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: retro guidance recorded no tree "
                         "premise — retro guidance withheld, exhaustion not "
                         "honoured (no_tree_premise).")
        # The premise's complement: the record carries an entry stamp and
        # the stamped convicting tests now measurably enter different
        # bodies. The lesson (and the attempts its exhaustion counted)
        # judged an execution a later fill has redirected.
        elif convictor_entry_moved(_retro_rec):
            _poisoned_retro = True
            self.tel.log(f"  [phase-c/per-fn] {rel_file}::{fn_name} "
                         f"[{container}]: retro guidance was earned when its "
                         "convicting tests entered a different set of bodies "
                         "— retro guidance withheld, exhaustion not honoured "
                         "(convictor_entry_moved).")
        # …and a conviction earned in ANOTHER context (other cluster's layout,
        # the absolute acceptance gate, the pre-group route set), or drawn from
        # tests that no longer convict this fn, is proof of nothing here —
        # whatever its guidance says.
        _provenance = RetroProvenance.current(ctx.cluster_name, snapshot)
        _standing = conviction_standing(
            _retro_rec, _provenance,
            evidence_for_record(_retro_rec, convicting=_evidence,
                                answers=_answers))
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
        # The pins of the tests this fn answers for, for the exhaustion
        # retrospective: a diagnosis that never sees them can prescribe away
        # the observable they name (`build_phase_c_retro_prompt`). Sliced by
        # leaf out of the test files that mention the leaf, as the impl
        # routes do.
        _fn_leaves = tuple(dict.fromkeys(
            t.rsplit("::", 1)[-1]
            for t in sorted(_answers.tests)))[:_ATTRIBUTED_TEST_LEAF_CAP]
        _fn_sources = tuple(
            t for leaf in _fn_leaves
            for _, t in self.tests.find_tests_for_fn(ctx.tests_dir_files, leaf))
        _fn_pins = pinned_observables_from_tests(_fn_leaves, _fn_sources)
        # The one-argument `assert!(s.contains(..))` literals, which the pins
        # extraction does not read; the contradiction guard needs both.
        _fn_observables = asserted_output_literals_from_tests(_fn_leaves, _fn_sources)
        # The fix loop's own token (`FIX_LOOP_SCOPE`): minted once per unit
        # fill, stamped on every retro record this fill writes, and passed to
        # every screen this fill runs, so a lesson the loop just wrote is
        # judged with the premise it has here and no other loop inherits it.
        _loop_scope = f"{rel_file}::{fn_name}::{container}:{uuid.uuid4().hex[:12]}"

        def _screen_stored_retro(text: str) -> tuple[str, dict[str, Any],
                                                     GuidanceScreening]:
            """The stored record for this unit and what the screen lets a
            prompt built on ``text`` carry. The one reading both the fix
            prompt and the retro re-entry decision use, so the loop never
            re-enters on guidance its prompt would then withhold."""
            key = self.retros.retro_key(rel_file, fn_name, container)
            rec = ctx.retros_cache.get(key, {})
            return key, rec, screen_guidance(
                rec, text, evidence=_answers,
                anchor_types=_anchor_types, cluster=ctx.cluster_name,
                cfg_dead=_cfg_dead_fns_of(ctx, rel_file),
                unit=screening_unit(rel_file, fn_name),
                pins=_fn_pins, observables=_fn_observables,
                loop_scope=_loop_scope)
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
        # to later iterations, and (b) the relevance filter can
        # use the CURRENT cargo error excerpt to prioritise notes whose
        # error codes / keywords match.

        # === Inner fix loop (unit fn + drive_fix_loop —
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
        # The seed is how THAT body failed, written with the excerpt it
        # explains: a refill compiled, so its first ask is never the
        # cargo-check prompt, and the excerpt quotes no run of it yet.
        _last_failure[0] = seed_attempt_failure(
            refill=rec.refill, crash_implicated=bool(_crash_evidence))
        _excerpt_quotes_run: list[bool] = [False]
        if rec.refill:
            last_body = _cur_body
            error_excerpt = REFILL_SEED_EXCERPT
        attempts_log: list[str] = []
        first_failed_body = ""
        first_error_excerpt = ""
        # Carries the bound-provided method inventory
        # from the last gather so the exhaustion retro can redirect to a frozen
        # bound's trait method instead of mis-routing to GENUINELY_HARD.
        retro_bound_methods = ""
        # The unit's one conversation: the first attempt opens it (the stub
        # prompt, or the fix prompt for a behavioural refill), every later
        # attempt — a retry after a gate, a retro round's re-entry — is a
        # turn on it that carries what was measured. Requests are answered
        # on it when CONTEXT_PULLS is on.
        _max_pulls = int(os.getenv("CONTEXT_PULLS_MAX", "2"))
        _pull_sources: PullSources | None = None
        if os.getenv("CONTEXT_PULLS", "1") != "0":
            _pull_sources = PullSources(
                read_file=reconstruction_file_reader(
                    ctx.target_src_dir, ctx.project_dir),
                find_tests=lambda name: "\n\n".join(
                    f"// {p}\n{t}" for p, t in self.tests.find_tests_for_fn(
                        ctx.tests_dir_files, name)),
                type_def=lambda name: (render_types_section(
                    types_referenced_in_text(
                        name, ctx.crate_type_records, visible_from=rel_file)[:3],
                    viewer_file=rel_file)
                    # Crate-local records win; well-known unstable std APIs
                    # (std::error::Request et al.) answer as a last fallback so
                    # the pull never starves on a std type the manifest can't have.
                    or std_api_def(name)),
                full_error=lambda: error_excerpt,   # this fn's latest error
            )
        _conv = self.prompt.open_conversation(
            f"{rel_file}::{fn_name}", route="per-fn", sources=_pull_sources,
            max_pulls=_max_pulls)
        _read_view = reconstruction_file_reader(ctx.target_src_dir, ctx.project_dir)

        def _cluster_view(text: str) -> str:
            if ctx.cluster_cfg_set is None:
                return text
            return filter_cfg_inactive_branches(text, ctx.cluster_cfg_set)
        # History of (body, error_sig, outcome) for THIS fn's fix loop.
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
            _last_crash_sites[0] = None
            _last_own_panics[0] = ()
            cur_text = file_path.read_text(encoding="utf-8")
            # Strip inactive #[cfg] branches so the model sees only the
            # active variant (e.g. one StdError definition, not three).
            if ctx.cluster_cfg_set is not None:
                cur_text = filter_cfg_inactive_branches(cur_text, ctx.cluster_cfg_set)
            # One gather per attempt — notes re-filtered with the
            # LATEST cargo error, TYPES_IN_SCOPE re-selected from
            # sig + container + where + the PREVIOUS BODY (types the last
            # attempt actually manipulated — new) + the current error
            # (E0277/E0599/E0046/E0053 routinely name a
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
            turn: str | AttemptFeedback
            if not _conv.opened and not rec.refill:
                turn = build_per_fn_prompt(
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
                            container=container, char_budget=1200),
                        identity_must=identity_must_instruction(
                            ctx.behaviour_specs,
                            units=(IdentityMustUnit(
                                fn_name, container, sig_text),))),
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
                _retro_key, _retro_rec, _retro_verdict = _screen_stored_retro(
                    cur_text)
                _retro_guidance = _retro_verdict.text
                if (_dg := declined_guidance(fn_name, _retro_rec)):
                    _retro_guidance = "\n".join(
                        p for p in (_retro_guidance, _dg) if p)
                # The line carries the STORE key (file::fn::container), not a
                # display name: `fn new` exists under three containers, and
                # the whole point is that the grep lands on the one record
                # that steered this prompt.
                if (_hline := honoured_guidance_line(
                        _retro_key, _retro_rec, _retro_guidance)):
                    self.tel.log(f"  {_hline}")
                if (_rline := retired_guidance_line(
                        _retro_key, _retro_rec, _retro_verdict)):
                    self.tel.log(f"  {_rline}")
            if _conv.opened:
                # A turn on the conversation: the model's previous body and
                # its reasoning are above; this carries the measured result
                # and only the evidence the conversation does not hold yet.
                turn = AttemptFeedback(
                    kind=fix_prompt_kind(
                        _last_failure[0], quoted_run=_excerpt_quotes_run[0]),
                    excerpt=error_excerpt, targets=(fn_name,),
                    output=BODY_OUTPUT,
                    note=(sig_change_permission(fn_name)
                          if fix_attempt >= 5 else ""),
                    sections=(
                        EvidenceSection(
                            "RETROSPECTIVE_GUIDANCE", _retro_guidance,
                            "A diagnosis of the attempts so far:\n"),
                        EvidenceSection(
                            "TYPES_IN_SCOPE", types_new_to(
                                _conv.text(), error_excerpt,
                                ctx.crate_type_records, viewer_file=rel_file),
                            "Types this result names that were not shown "
                            "above:\n"),
                        EvidenceSection("BOUND_METHODS",
                                        _fn_ctx.bound_methods_section),
                        EvidenceSection("MODEL_NOTES", notes_section),
                        EvidenceSection(
                            "RUST_GOTCHAS_DETAIL",
                            language_driver().gotchas_for_error(error_excerpt))),
                    moved=_conv.bodies_moved(
                        _read_view, frozenset({fn_name}),
                        normalize=_cluster_view))
            elif rec.refill:
                turn = build_per_fn_fix_prompt(
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
                        crash_evidence=_crash_evidence,
                        identity_must=identity_must_instruction(
                            ctx.behaviour_specs,
                            units=(IdentityMustUnit(
                                fn_name, container, sig_text),))),
                    # Open the sig-change escape hatch after multiple
                    # compile failures. The model can return an additional
                    # <SIGNATURE> block; we splice it only when fn is non-pub.
                    allow_sig_change=False,
                    fr_path=ctx.fr_path,
                    kind=fix_prompt_kind(
                        _last_failure[0], quoted_run=_excerpt_quotes_run[0]),
                )

            # The stream-meta rows of this call carry the unit they serve,
            # so the FLAT grading can tell a measured refusal from a
            # reasoning-capped truncation per unit.
            try:
                with serving_units((f"{rel_file}::{fn_name}",)):
                    body, last_response = self.prompt.ask_per_fn_body(
                        turn, fn_name, attempts_log, conversation=_conv,
                        sig_text=sig_text)
            except TranscriptBound:
                self.tel.emit("phase_c.fn.fill.skip", data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name,
                    "fix_attempt": fix_attempt,
                    "reason": "transcript_bound",
                })
                return FixOutcome("stop")
            if body is None:
                # Model gave no usable response — stop retrying this fn.
                self.tel.emit("phase_c.fn.fill.skip", data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name,
                    "fix_attempt": fix_attempt,
                    "reason": "empty_response",
                })
                return FixOutcome("stop")

            # If the model proposed a sig change, try it FIRST so
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
            _attempt_hollow_before = hollow_count(
                snapshot_src_tree(ctx.target_src_dir))
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
                    "[phase-c/per-fn]",
                    validate_parse=self.mut.parse_validator())
                if _rep is not None:
                    rc, cargo_out, _diags = _rep
            # U2: compile is only a pre-gate — accept the fn only if
            # its OWN tests pass. The SHARED u2_test_gate decides; on a test
            # failure it routes through the same fix loop as a compile error
            # (failing-test set as the stall signature).
            _u2_fail_sig: frozenset[str] | None = None
            # The POSITIVE evidence the gate saw, kept beside the decision.
            # ACCEPT means "tests pass / gate off / inconclusive" — three
            # states, one branch — and only the first is a measurement of this
            # fill. Empty here means the accept below is a KEEP, not a win.
            _u2_passed: frozenset[str] = frozenset()
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
                _u2_passed = _v.self_passed
                _observed_passed[0] = _v.self_passed
                if _v.decision is U2Decision.REJECT:
                    # A real self-failure (assertion/behavior). Route through the
                    # normal fix path: revert + re-prompt with the failing tests
                    # as the stall signature.
                    _u2_fail_sig, cargo_out = _v.self_failures, _v.detail
                    if _v.aborted:
                        _last_crash_sites[0] = _v.external_stub_sites
                        _last_own_panics[0] = tuple(
                            f"{p.file}:{p.line}: {p.message}"
                            for p in (_v.crash.crate_panics
                                      if _v.crash is not None else ())[:4])
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
                            _unpayable_mark[:] = [
                                "every failing attributed test needs "
                                f"still-stubbed sibling(s): {_names}"]
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
                    # The same refusal on measured evidence rather than on
                    # stub-ness. The check above asks whether a hollow
                    # sibling holds every failing test; this asks whether
                    # the failing tests enter this body at all. Both are
                    # attempt-invariant, and they are not the same set:
                    # on cluster `stable`, run `2450551-9a6a8683`
                    # (2026-08-30), one stub remained in 209 fillable
                    # units so nothing was sibling-blocked, and the fix
                    # loop still spent 4,550 s over five attempts and two
                    # retrospective rounds on `src/lib.rs::Ok` for
                    # `test_trailer` — which enters 24 bodies, `format_err`
                    # and `must_use` in `lib.rs`, never `Ok`. It was alone
                    # in the first dependency level at pool width 1, so
                    # the pass's other three units waited behind it, and
                    # the body it finally kept was the one it started
                    # with. Refuses only on positive evidence; see
                    # `convictors_never_enter`.
                    if _u2_fail_sig is not None:
                        _unreached = coverage_unreached_by(
                            FnLocator(rel_file, fn_name), _v.self_failures)
                        if _unreached:
                            _names = ", ".join(sorted(_unreached))
                            self.tel.log(
                                f"  [phase-c/per-fn] {idx}/{ctx.total} kept "
                                f"{rel_file}::{fn_name} (compiles; no failing "
                                f"attributed test executes this body — "
                                f"{_names} enter(s) the crate elsewhere; fix "
                                "loop skipped, deferred to outer loop).")
                            _unpayable_mark[:] = [
                                "no failing attributed test executes this "
                                f"body ({_names} enter(s) the crate "
                                "elsewhere)"]
                            self.tel.emit(
                                "phase_c.fn.u2_coverage_unreached", data={
                                    "cluster": ctx.cluster_name,
                                    "pass_n": ctx.pass_n,
                                    "file": rel_file, "fn": fn_name,
                                    "failing_tests": sorted(_unreached),
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
                    _blk_names = ", ".join(
                        f"{s.file}::{s.fn_name}" for s in _blocked_by)
                    self.tel.log(f"  [phase-c/per-fn] {idx}/{ctx.total} kept "
                                 f"{rel_file}::{fn_name}{_ctx2} (compiles; "
                                 f"{'; '.join(_why)}"
                                 + (f": {_blk_names}" if _blk_names else "")
                                 + " — deferred to outer loop).")
                    self.tel.emit("phase_c.u2.external_only", data={
                        "cluster": ctx.cluster_name,
                        "file": rel_file, "fn": fn_name,
                        "blocking_fns": [f"{s.file}::{s.fn_name}"
                                         for s in _blocked_by],
                        "unjudged": sorted(_v.no_verdict),
                    })
            # Global monotone ratchet: this fn compiles and its own tests pass,
            # but reject it if it reddened any previously-passing cluster test
            # (or dropped the pass count). Routed through the SAME failure path
            # as a U2 test failure — revert + re-prompt with the reddened tests
            # as the stall signature.
            if rc == 0 and _u2_fail_sig is None:
                _regr = self.cargo.test_regression_gate(
                    ctx.tdd_test_cmd, ctx.target_src_dir, ctx.cluster_env,
                    _attempt_baseline_test, stub_free=ctx.stub_free,
                    hollow_before=_attempt_hollow_before)
                if _regr is not None:
                    _cmp, _regr_detail = _regr
                    _u2_fail_sig = _cmp.signature
                    cargo_out = _regr_detail
                    if _cmp.newly_crashed:
                        _last_crash_sites[0] = _cmp.crash_stub_sites
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
                # POSITIVE only with a test observed PASSING. Without one the
                # fill is KEPT on a gate that convicted nobody — the tests were
                # never failing, or ran inconclusively, or the gate was off —
                # and recording that as a win is what let a 7h48m run report 50
                # successes against a byte-identical verdict vector (run
                # `1064759-0f23fb68`, 2026-08-15/16). The joint routes already
                # bank on positive evidence only (`assess_joint_improvement`:
                # "counting absence as progress fabricated improvement"); this
                # is that rule at the per-fn ledger.
                self.tel.emit("phase_c.verdict", data={
                    **VerdictRecord(
                        unit=str(_unit_key), route="per-fn",
                        attempt=fix_attempt,
                        disposition=(Disposition.KEEP_DEFERRED if _blocked_by
                                     else Disposition.ACCEPT),
                        reason=("kept — every attributed test blocked by a "
                                "still-stubbed sibling" if _blocked_by
                                else f"accepted — {len(_u2_passed)} attributed "
                                "test(s) observed passing" if _u2_passed
                                else "kept — compiles, but no attributed test "
                                "was observed passing (none were failing, or "
                                "the scoped run was inconclusive)"),
                        outcome=(GateOutcome.POSITIVE
                                 if _u2_passed and not _blocked_by
                                 else GateOutcome.UNMEASURABLE),
                        evidence_keys=(
                            tuple(f"{s.file}::{s.fn_name}" for s in _blocked_by)
                            if _blocked_by
                            else tuple(sorted(_u2_passed))),
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
                    # Tag the note with the error codes from the
                    # original failure so it can be surfaced next time
                    # any of those codes recurs. The premise rides
                    # beside it — the prose may generalise, the record may
                    # not, and without the record the lesson is run-scoped.
                    _ecodes = ecodes_in_text(first_error_excerpt or "")
                    _prov = NoteProvenance(
                        unit=f"{rel_file}::{fn_name}",
                        error_codes=tuple(sorted(_ecodes)),
                        rejected_symbols=tuple(sorted(
                            rejected_symbols_in_errors(
                                first_error_excerpt or ""))),
                        failed_body_hash=body_fingerprint(first_failed_body))
                    if note and self.retros.append_note(ctx.fr_path, note,
                                                        error_codes=_ecodes,
                                                        provenance=_prov):
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

            # Track error codes seen so we can promote recurring
            # ones to every subsequent prompt.
            self.retros.record_error_codes(cargo_out)
            # When the failure is a TEST failure (not compile),
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
            _last_failure[0] = (
                AttemptFailure.COMPILE if not _compiled
                else AttemptFailure.ABORT if _last_crash_sites[0] is not None
                else AttemptFailure.TEST)
            # Record this failed attempt for the prior-attempts block.
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
            _excerpt_quotes_run[0] = True
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
            if _unpayable_mark:
                # Both gates above answer an attempt-invariant question and
                # only clear the U2 signature; the regression gate then
                # reverted a body that reddened a sibling, and this path used
                # to re-prompt it as a fixable error. Run `33522-b27db63d`
                # (2026-09-17): `src/fmt.rs::debug`, which the coverage
                # reading proved `test_whitespace` never enters, was rewritten
                # 11 times over 41 minutes, every body reddening `test_debug`,
                # with three retrospective re-entries on top.
                self.tel.log(
                    f"  [phase-c/per-fn] GUARD {rel_file}::{fn_name}: kept "
                    f"reverted and STOPPING — {_unpayable_mark[0]}; this "
                    + ("attempt regressed a sibling" if _u2_fail_sig is not None
                       else "attempt did not compile")
                    + ", and no rewrite of this body can move its failing "
                    "tests, so a re-prompt can only buy back a body that "
                    "changes nothing.")
                self.tel.emit("phase_c.fn.unpayable_stop", data={
                    "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                    "file": rel_file, "fn": fn_name,
                    "reason": _unpayable_mark[0],
                    "attempt": fix_attempt,
                })
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
            if unit_clock.expired():
                # Past the unit's bound (`FILL_UNIT_SHARE_MULTIPLE` × the
                # run's share): no retrospective — running out of time is not
                # evidence about this body. The unit exits as a failed unit.
                if unit_clock.note_once():
                    self.tel.log(
                        f"  [phase-c/per-fn] {rel_file}::{fn_name}: UNIT_BOUND "
                        f"— {unit_clock.elapsed_minutes():.0f} min past its "
                        f"{unit_clock.bound_minutes():.0f}-min bound after "
                        f"{fix_attempt} attempt(s), retro round {_retro_round}; "
                        "no retrospective.")
                    self.tel.emit("phase_c.unit.bound", data={
                        "cluster": ctx.cluster_name, "route": "per-fn",
                        "unit": f"{rel_file}::{fn_name}",
                        "attempts": fix_attempt, "retro_round": _retro_round,
                        "bound_minutes": unit_clock.bound_minutes(),
                        "elapsed_minutes": round(
                            unit_clock.elapsed_minutes(), 1)})
                break
            if _crash_blocked_mark or _unpayable_mark:
                # No retrospective and no conviction: the failure was never
                # about this body, and a diagnosis of it would write guidance
                # whose premise is false into the store the loop later
                # believes. The unit exits crash-blocked or unpayable below.
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
            # Set when the diagnosis is drawn from tests that never enter
            # this body: such a round bought no evidence about the unit, so
            # it neither convicts nor earns another round.
            _no_unit_evidence = False
            try:
                _retros = self.retros.load(ctx.project_dir)
                _retro_k = self.retros.retro_key(rel_file, fn_name, container)
                _prior = _retros.get(_retro_k, {})
                _prior_count = int(_prior.get("retrospective_count", 0))
                _prior_best = best_progress_of(_prior)
                # The diagnosis is asked to build ON the prior lesson — a lesson
                # this layout disproves would just be restated in the new one.
                _prior_guidance = admissible_guidance(
                    _prior, snapshot, evidence=_answers,
                    anchor_types=_anchor_types, cluster=ctx.cluster_name,
                    cfg_dead=_cfg_dead_fns_of(ctx, rel_file),
                    unit=screening_unit(rel_file, fn_name),
                    pins=_fn_pins, observables=_fn_observables,
                    loop_scope=_loop_scope)
                _retro = self.retros.diagnose(
                    fn_name=fn_name, sig_text=sig_text,
                    container=container, file_rel=rel_file,
                    cluster_name=ctx.cluster_name,
                    last_body=last_body or first_failed_body or "",
                    error_excerpt=error_excerpt or first_error_excerpt or "",
                    fix_attempts=fix_attempt,
                    prior_retrospective=_prior_guidance,
                    bound_methods_section=retro_bound_methods,
                    pinned_observables=pins_for_contradiction_guard(
                        _fn_pins, _fn_observables),
                    compiled=_last_failure[0] is not AttemptFailure.COMPILE,
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
                    # A conviction is a verdict on the model's bodies. A last
                    # error that is a splice/nesting diagnostic — the reply's
                    # own `fn …(&self)` line spliced INSIDE the body, a
                    # `parse_broken` — is a verdict on how the pipeline
                    # placed the text, and bought no evidence about the unit.
                    _splice_failure = exhaustion_from_splice_diagnostic(
                        error_excerpt or first_error_excerpt or "")
                    if _full_exhaust and _splice_failure:
                        _full_exhaust = False
                        self.tel.log(
                            f"  [phase-c/retro] {rel_file}::{fn_name}: NOT "
                            "exhausting — the last error is a splice/nesting "
                            "diagnostic, not a verdict on the body.")
                    # Same rule, measured: a verdict drawn from tests that
                    # never enter this body is a verdict about somewhere
                    # else. Convicting on it parks the unit AND stores a
                    # lesson the next prompt obeys — on run
                    # `2450551-9a6a8683` the record written here told every
                    # later attempt to make `Ok<T>(v)` yield `Err(v)`,
                    # which cannot typecheck (E0308 twice, then E0512
                    # "cannot transmute between types of different sizes"),
                    # and it persisted to disk as `fully_exhausted` where
                    # nothing could retire it: the retirement rule wants
                    # compile evidence, and a body that never compiles is
                    # never adopted. The pre-loop gate above normally
                    # prevents this round happening at all; this is the
                    # arm for a conviction reached some other way. Drop
                    # the guidance too — parking is reversible, a stored
                    # lesson steers every future prompt for the unit.
                    _cov_unreached = coverage_unreached_by(
                        _unit_loc, _evidence.tests)
                    if _cov_unreached:
                        _full_exhaust = False
                        _no_unit_evidence = True
                        _retro = {**_retro, "guidance": ""}
                        self.tel.log(
                            f"  [phase-c/retro] {rel_file}::{fn_name}: NOT "
                            "exhausting and storing no guidance — no "
                            "convicting test executes this body "
                            f"({', '.join(sorted(_cov_unreached))}); the "
                            "attempt bought no evidence about this unit.")
                        self.tel.emit(
                            "phase_c.retro.coverage_unreached", data={
                                "cluster": ctx.cluster_name,
                                "pass_n": ctx.pass_n,
                                "file": rel_file, "fn": fn_name,
                                "convicted_by": sorted(_cov_unreached),
                            })
                    # A verdict earned from an abort whose only panics are
                    # still-stubbed siblings' is the tree's verdict, not the
                    # body's: no rewrite of this fn can pass a test that is
                    # dropped through a hollow `vtable()`. Convicting on it
                    # parks the unit behind the readmission deadlock —
                    # `src/ptr.rs::new::Own<T>` was `fully_exhausted` on that
                    # signature for the whole of run `2585062-494e630e`
                    # while the textbook body sat in its own comment. The
                    # record is still written (stamped `crash_stub_sites`,
                    # so the lesson is withheld as hollow-tree guidance and
                    # the class is countable), it just convicts nothing.
                    if _full_exhaust and _last_crash_sites[0]:
                        _full_exhaust = False
                        self.tel.log(
                            f"  [phase-c/retro] {rel_file}::{fn_name}: NOT "
                            "exhausting — the last failure was an abort whose "
                            "only panics are still-stubbed sibling(s) at "
                            + ", ".join(f"{f}:{ln}" for f, ln
                                        in sorted(_last_crash_sites[0]))
                            + "; the verdict is the tree's, not the body's.")
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
                    }, _provenance,
                        # A compiled attempt learned from test outcomes and is
                        # answerable to every test the fn answers for; a
                        # compile failure is stamped with the convicting
                        # tests of the moment.
                        (_answers if _cur_progress.compile_errors == 0
                         else _evidence),
                        _blockers,
                        # From the FULL excerpt: the preview above cuts a
                        # regression's test list mid-name.
                        regressed=regressed_tests_from_excerpt(
                            error_excerpt or first_error_excerpt or ""),
                        pins=pins_for_contradiction_guard(_fn_pins, _fn_observables),
                        crash_stub_sites=_last_crash_sites[0],
                        loop_scope=_loop_scope)
                    self.retros.save(ctx.project_dir, _retros)
                    # Make the fresh guidance visible to this fn's OWN next
                    # round (the fix prompt reads ctx.retros_cache) and to
                    # every later dispatch in this pass.
                    ctx.retros_cache[_retro_k] = _retros[_retro_k]
                    # Not exhausting is not the same as having a reason to go
                    # again: a round refused as coverage-unreached kept
                    # `_full_exhaust` False and used to re-enter here with its
                    # guidance emptied, up to PER_FN_RETRO_ROUNDS_MAX times
                    # (reproduced on the engine harness: 50 asks, 25
                    # retrospectives for one unit).
                    #
                    # And not convicting is not the same as improving: the
                    # splice, coverage and hollow-sibling arms above veto the
                    # CONVICTION of a round that did not improve, and used to
                    # hand it another round as a side effect. On run
                    # `2657150-3139f958` (2026-09-18) `src/error.rs::construct`
                    # read `improved=False` at the same floor (compiles, 141
                    # failing) on rounds 2 to 7, each exempted by the
                    # still-stubbed-sibling arm, and re-entered every time —
                    # 1h40m of one worker on one unit while the other 13 units
                    # of the run had finished, the run's 25-minute share long
                    # spent. The re-entry is bought only by a strictly lower
                    # floor, as this loop's header states.
                    _retry_informed = (_improved and not _full_exhaust
                                       and not _downstream
                                       and not _no_unit_evidence)
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
            if _retry_informed:
                # The re-entry is bought by the guidance the next prompt will
                # carry, so it reads the screen that prompt runs, on the
                # tree it will be built on.
                _now_text = file_path.read_text(encoding="utf-8")
                if ctx.cluster_cfg_set is not None:
                    _now_text = filter_cfg_inactive_branches(
                        _now_text, ctx.cluster_cfg_set)
                _nk, _nrec, _nverdict = _screen_stored_retro(_now_text)
                if not informed_reentry(_nverdict):
                    _retry_informed = False
                    self.tel.log(
                        f"  [phase-c/per-fn] {fn_name}: retro round "
                        f"{_retro_round} improved the error floor but its "
                        "guidance would not reach the next prompt ("
                        f"{_nverdict.retired_by or 'no guidance'}) — not "
                        "re-entering an uninformed fix loop.")
                    self.tel.emit("phase_c.fn.retro_retry_withheld", data={
                        "cluster": ctx.cluster_name, "pass_n": ctx.pass_n,
                        "file": rel_file, "fn": fn_name,
                        "retro_round": _retro_round,
                        "retired_by": _nverdict.retired_by,
                    })
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
        if not success and _unpayable_mark:
            # Plain revert, like the crash-blocked exit: no salvage (every
            # compiling attempt regressed a sibling or paid nothing), no
            # annotated stub, no self-note, no conviction. The attempts were
            # real calls and stay in the result; nothing persistent moves
            # against the unit, and the next fill's reading decides again.
            with ctx.splice_check_lock:
                _revert_own_body()
            self.tel.log(
                f"  [phase-c/per-fn] {idx}/{ctx.total} {rel_file}::{fn_name}: "
                f"reverted after {fix_attempt} attempt(s) — "
                f"{_unpayable_mark[0]}; no retrospective, no conviction.")
            self.tel.emit("phase_c.verdict", data={
                **VerdictRecord(
                    unit=str(_unit_key), route="per-fn", attempt=fix_attempt,
                    disposition=Disposition.REVERT,
                    reason=f"unpayable: {_unpayable_mark[0]}",
                    outcome=GateOutcome.UNMEASURABLE,
                    validity=ValidityVector(binary_completed=True,
                                            tests_blocked=True),
                    model_calls=attempts).to_json_dict(),
                "cluster": ctx.cluster_name,
            })
            self.tel.emit("phase_c.fill.class", data={
                "cluster": ctx.cluster_name,
                "unit": f"{rel_file}::{fn_name}",
                "route": "per-fn",
                "class": FillOutcomeClass.MIS_TARGETED.value,
            })
            return FillResult(file=rel_file, fn_name=fn_name, filled=False,
                              attempts=attempts,
                              fill_class=FillOutcomeClass.MIS_TARGETED.value)
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
                        failure=_last_failure[0],
                        stub_sites=_last_crash_sites[0] or (),
                        own_panics=_last_own_panics[0],
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
                # Tag exhausted-fail notes with the codes seen
                # across the run so they re-surface for the same code.
                # Same premise record as the recovered path — an
                # exhausted lesson is the MORE dangerous one to leave
                # premise-less, since nothing ever validated its advice.
                _ecodes2 = ecodes_in_text((error_excerpt or "")
                                          + " " + (first_error_excerpt or ""))
                _prov2 = NoteProvenance(
                    unit=f"{rel_file}::{fn_name}",
                    error_codes=tuple(sorted(_ecodes2)),
                    rejected_symbols=tuple(sorted(
                        rejected_symbols_in_errors(
                            (error_excerpt or "")
                            + "\n" + (first_error_excerpt or "")))),
                    failed_body_hash=body_fingerprint(
                        last_body or first_failed_body or ""))
                if note and self.retros.append_note(ctx.fr_path, note,
                                                    error_codes=_ecodes2,
                                                    provenance=_prov2):
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
            if last_metric is None or (
                    last_metric.compiles is False and not _salvaged):
                _ex_cls = (FillOutcomeClass.ANSWER_DROPPED
                           if last_metric is None
                           else FillOutcomeClass.COMPILE_FAIL)
            else:
                _ex_cls = FillOutcomeClass.WRONG_BODY
            self.tel.emit("phase_c.fill.class", data={
                "cluster": ctx.cluster_name,
                "unit": f"{rel_file}::{fn_name}",
                "route": "per-fn",
                "class": _ex_cls.value,
            })
            return FillResult(file=rel_file, fn_name=fn_name, filled=False,
                              attempts=attempts, error=exhaust_error,
                              fill_class=_ex_cls.value)

        # The reading the route row is graded on is the reading the class
        # reads: a keep whose suite reading moved verdicts is CASHED whatever
        # its attributed set says (run `3014175-d98ed09e`, 2026-09-08: the
        # `object_ref` keep read `pass 32 -> 87` under an empty attributed
        # set and graded UNREACHABLE).
        _cmp_u = last_comparison() if ctx.tdd_test_cmd else None
        note_kept_comparison(_cmp_u)
        _ok = class_for_kept(
            passed=_observed_passed[0],
            unreachable=tests_could_not_run(blocked_sibling=bool(_blocked_by)),
            net_delta=_cmp_u.net_pass_delta if _cmp_u is not None else 0,
            attributed=_answers.tests,
            measured=not _blocked_by)
        self.tel.emit("phase_c.fill.class", data={
            "cluster": ctx.cluster_name,
            "unit": f"{rel_file}::{fn_name}",
            "route": "per-fn",
            "class": _ok.value,
        })
        return FillResult(file=rel_file, fn_name=fn_name, filled=True,
                          attempts=attempts, blocked_by=tuple(_blocked_by),
                          fill_class=_ok.value)
