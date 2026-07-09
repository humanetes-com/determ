"""The shared compile/test fix loop, as typed strategies over a driver (P3.C).

`generate_fix_loop` was a 700-line closure nest: an outer measure loop whose one
attempt branched — by `(FixStrategy, FillUnit)` settings — into a whole-file
rewrite, a per-fn dispatch, or a per-impl dispatch, each re-deriving its frontier
and grouping inline. This module makes the branch a TYPE:

* :class:`CompileFixStrategy` — the `attempt` protocol the driver runs.
* :class:`WholeFileFix` / :class:`PerFnFix` / :class:`PerImplFix` — the three
  strategies; each builds its frontier via the :class:`FrontierBuilder` port,
  partitions it with the pure :mod:`pipeline_core.domain.fix_plan` planner, and
  runs the resulting :class:`~pipeline_core.application.reconstruct.fix_unit`
  units. The whole-file rewrite + the per-fn one-shot/behavior fill are distinct
  collaborators, injected — not re-inlined.
* :func:`select_strategy` — the settings→strategy mapping (was an if/else over
  enums); :func:`run_generate_fix` — the thin driver over
  :func:`run_callback_loop`.

Splice-unit escalation (s117 #3): a unit whose inner loop floor-stalls WITHOUT
ever improving on its opening error count (`UnitRunOutcome.escalate`) is retried
at the next-larger scope — fn → enclosing impl block → whole-file rewrite — so
a defect that lives outside the unit's window (a sibling member, a type in the
same file) stops burning the whole outer stall budget at the wrong scope.

`pipeline.py` is the composition root: it binds the ports + the resident
whole-file/overview callables and calls :func:`run_generate_fix`.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pipeline_core.application.reconstruct.fix_unit import (
    FixUnitDeps,
    make_fix_unit,
    one_shot_fill,
)
from pipeline_core.application.refine_loop import run_callback_loop
from pipeline_core.domain.enums import FillUnit, FixStrategy
from pipeline_core.domain.fix_plan import plan_per_fn, plan_per_impl
from pipeline_core.domain.fix_report import FixReport
from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.fix_target import FnTarget, ImplTarget
from pipeline_core.domain.models import FnLocator, ImplBlock
from pipeline_core.ports.fix_engine import FrontierBuilder

# (output, history, retro) -> FixReport — the legacy whole-file rewrite path.
WholeFileFn = Callable[[str, list[dict[str, Any]], str], FixReport]


@dataclass(frozen=True, slots=True)
class GenerateFixDeps:
    """The strategy-level collaborators. `unit` carries the per-unit role ports
    (checker/model/splicer/prompts/log/...); the rest are the strategy's own
    seams: frontier attribution, the static workspace reads, the compile-overview
    log, and the whole-file fallback."""

    unit: FixUnitDeps
    frontiers: FrontierBuilder
    enumerate_fns: Callable[[], list[dict[str, Any]]]
    collect_impl_blocks: Callable[[], list[ImplBlock]]
    log_overview: Callable[[str, int], None]
    whole_file_fix: WholeFileFn
    behavior_max_fns: int
    src_snip_cap: int


class CompileFixStrategy(Protocol):
    """One outer-loop attempt: produce a fix (or abort), return its report."""

    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = ...) -> FixReport: ...


def _container_index(recs: list[dict[str, Any]]) -> dict[FnLocator, str]:
    return {FnLocator(r.get("file", ""), r.get("fn_name", "")): r.get("container", "")
            for r in recs}


def _err_preview(output: str, cap: int) -> str:
    return output[-cap:] if len(output) > cap else output


def _escalate_to_whole_file(
    session: FixSession, deps: GenerateFixDeps, stalled_files: list[str],
    history: list[dict[str, Any]], retro: str,
) -> None:
    """Last escalation rung: units in `stalled_files` spent their stall budget
    without ever improving on their opening error count — the defect spans a
    scope no fn/impl unit can see, so the model gets the whole file(s) once."""
    probe = deps.unit.checker.probe(
        session.check_only_cmd, session.project_dir,
        dict(session.env), session.timeout)
    if probe.rc == 0:
        return
    files = ", ".join(sorted(set(stalled_files)))
    deps.unit.log("  [fix loop/escalate] identical-error stall at unit scope "
                  f"({files}) → whole-file rewrite")
    deps.whole_file_fix(probe.rendered, history, retro)


class WholeFileFix:
    """Legacy strategy: the model returns each broken file in full."""

    def __init__(self, deps: GenerateFixDeps) -> None:
        self._deps = deps

    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = "") -> FixReport:
        return self._deps.whole_file_fix(output, history, retro)


class PerFnFix:
    """unit=fn: one inner cargo-check loop per UNIQUE broken fn; ambiguous names
    and any non-compile failure fall back to the whole-file rewrite."""

    def __init__(self, session: FixSession, deps: GenerateFixDeps) -> None:
        self.session, self.deps = session, deps

    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = "") -> FixReport:
        d = self.deps
        if output.startswith("TIMEOUT after "):
            return FixReport.aborted(reason="check_timeout", summary="check timed out")
        try:
            d.log_overview(output, len(history))
            frontier = d.frontiers.compile_frontier(output)
            if not frontier:
                return d.whole_file_fix(output, history, retro)
            recs = d.enumerate_fns()
            name_counts = Counter(
                (r.get("file", ""), r.get("fn_name", "")) for r in recs)
            plan = plan_per_fn(
                frontier, name_counts=name_counts,
                container_index=_container_index(recs),
                writable=self.session.is_writable)
            changed: list[str] = []
            stalled_fns: list[FnTarget] = []
            for target in plan.free_fns:
                res = make_fix_unit(self.session, d.unit, target).run()
                changed += res.changed
                if res.escalate:
                    stalled_fns.append(target)
            # Escalation rung 2: a stalled fn retries at its ENCLOSING impl
            # scope (the model sees every sibling member — the usual home of
            # the real defect); each block at most once per attempt.
            stalled_files: list[str] = []
            if stalled_fns:
                fn_to_block = {(b.file, m.fn_name): b
                               for b in d.collect_impl_blocks()
                               for m in b.members}
                seen: set[tuple[str, str]] = set()
                for t in stalled_fns:
                    block = fn_to_block.get((t.file_rel, t.fn_name))
                    if block is None or not block.container:
                        stalled_files.append(t.file_rel)
                        continue
                    key = (block.file, block.container)
                    if key in seen:
                        continue
                    seen.add(key)
                    d.unit.log(f"  [fix loop/escalate] {t.file_rel}::{t.fn_name} "
                               "stalled on identical errors → impl "
                               f"{block.container[:50]}")
                    impl_target = ImplTarget(block.file, block.container, block)
                    ires = make_fix_unit(self.session, d.unit, impl_target).run()
                    changed += ires.changed
                    if ires.escalate:
                        stalled_files.append(block.file)
            if stalled_files:
                _escalate_to_whole_file(self.session, d, stalled_files,
                                        history, retro)
            elif plan.deferred:
                probe = d.unit.checker.probe(
                    self.session.check_only_cmd, self.session.project_dir,
                    dict(self.session.env), self.session.timeout)
                if probe.rc != 0:
                    d.unit.log("  [fix loop/fn] file-level errors remain → "
                               "whole-file fallback")
                    d.whole_file_fix(probe.rendered, history, retro)
            err = _err_preview(output, d.unit.err_cap)
            return FixReport.changed(
                summary=f"per_fn changed={len(changed)}",
                files_changed_str=", ".join(changed[:4]) if changed
                else "no files changed",
                error_preview=err, error_sig=err)
        except Exception as e:
            d.unit.log(f"  [fix loop/fn] unexpected error ({e}) → "
                       "legacy whole-file fallback")
            return d.whole_file_fix(output, history, retro)


class PerImplFix:
    """unit=impl (default): group compile errors by impl block (the model sees
    impl-level constraints + sibling impls). A clean compile with failing tests
    routes to the one-shot stub/behaviour fill; everything else falls back."""

    def __init__(self, session: FixSession, deps: GenerateFixDeps) -> None:
        self.session, self.deps = session, deps
        self._per_fn = PerFnFix(session, deps)

    def _fill_when_compiling(
        self, output: str, history: list[dict[str, Any]], retro: str,
    ) -> FixReport:
        """No compile-error frontier: tests fail on code that compiles. Attribute
        to fns (panic stubs first, then behavioural) and one-shot fill them."""
        d = self.deps
        tdd = d.frontiers.tdd_frontier(output)
        kind = "tdd-stub"
        behavior_srcs: dict[str, tuple[str, str]] = {}
        if not tdd:
            tdd, behavior_srcs = d.frontiers.behavior_frontier(output)
            kind = "behavior"
            if not tdd:
                return d.whole_file_fix(output, history, retro)
            if len(tdd) > d.behavior_max_fns:
                # Most-failing-tests first; name tiebreak for determinism.
                tdd = dict(sorted(tdd.items(),
                                  key=lambda kv: (-len(kv[1]), kv[0]))[:d.behavior_max_fns])
        changed = one_shot_fill(
            tdd, kind=kind, behavior_srcs=behavior_srcs, session=self.session,
            model=d.unit.model, splicer=d.unit.splicer, log=d.unit.log,
            thinking_directive=d.unit.thinking_directive, err_cap=d.unit.err_cap,
            src_snip_cap=d.src_snip_cap, output=output)
        err = _err_preview(output, d.unit.err_cap)
        kind_tag = "tdd_stub" if kind == "tdd-stub" else "behavior"
        return FixReport.changed(
            summary=f"{kind_tag} changed={len(changed)}",
            files_changed_str=", ".join(changed[:4]) if changed
            else "no files changed",
            error_preview=err, error_sig=err)

    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = "") -> FixReport:
        d = self.deps
        if output.startswith("TIMEOUT after "):
            return FixReport.aborted(reason="check_timeout", summary="check timed out")
        try:
            d.log_overview(output, len(history))
            frontier = d.frontiers.compile_frontier(output)
            if not frontier:
                return self._fill_when_compiling(output, history, retro)
            blocks = d.collect_impl_blocks()
            if not blocks:
                return self._per_fn.attempt(output, history, retro=retro)
            fn_to_block = {(b.file, m.fn_name): b
                           for b in blocks for m in b.members}
            plan = plan_per_impl(
                frontier, fn_to_block=fn_to_block,
                container_index=_container_index(d.enumerate_fns()),
                writable=self.session.is_writable)
            changed: list[str] = []
            stalled_files: list[str] = []
            for impl_target in plan.impl_groups:
                res = make_fix_unit(self.session, d.unit, impl_target).run()
                changed += res.changed
                if res.escalate:
                    stalled_files.append(impl_target.file_rel)
            for fn_target in plan.free_fns:
                res = make_fix_unit(self.session, d.unit, fn_target).run()
                changed += res.changed
                # A free fn here has no enclosing impl by construction
                # (plan_per_impl grouped every impl member) → file rung.
                if res.escalate:
                    stalled_files.append(fn_target.file_rel)
            if stalled_files:
                _escalate_to_whole_file(self.session, d, stalled_files,
                                        history, retro)
            err = _err_preview(output, d.unit.err_cap)
            return FixReport.changed(
                summary=f"per_impl changed={len(changed)}",
                files_changed_str=", ".join(changed[:4]) if changed
                else "no files changed",
                error_preview=err, error_sig=err)
        except Exception as e:
            d.unit.log(f"  [fix loop/impl] unexpected error ({e}) → "
                       "legacy whole-file fallback")
            return d.whole_file_fix(output, history, retro)


def select_strategy(strategy: FixStrategy, unit: FillUnit,
                    session: FixSession, deps: GenerateFixDeps) -> CompileFixStrategy:
    """Map the (strategy, unit) settings to the strategy object."""
    if strategy is FixStrategy.PER_FN:
        return PerImplFix(session, deps) if unit is FillUnit.IMPL \
            else PerFnFix(session, deps)
    return WholeFileFix(deps)


def run_generate_fix(
    *, session: FixSession, deps: GenerateFixDeps,
    strategy: FixStrategy, unit: FillUnit,
    run_check: Callable[[], tuple[bool, str, int]],
    do_retro: Callable[[str, list[dict[str, Any]]], str],
    max_stalls: int, log: Callable[[str], None],
    on_iter: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[bool, str]:
    """Drive the chosen strategy via the shared callback loop until the check
    passes, the strategy aborts, or the floor-stall budget is spent."""
    chosen = select_strategy(strategy, unit, session, deps)
    log(f"  [fix loop] strategy={strategy} unit={unit}")
    ok, last_output, _hist = run_callback_loop(
        name="generate_fix_loop",
        run_check=run_check,
        do_fix=lambda o, h, *, retro="": chosen.attempt(o, h, retro=retro).as_record(),
        do_retro=do_retro,
        max_stalls=max_stalls,
        on_iter=on_iter,
    )
    return ok, last_output
