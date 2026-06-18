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
from pipeline_core.domain.models import ImplBlock
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


def _container_index(recs: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    return {(r.get("file", ""), r.get("fn_name", "")): r.get("container", "")
            for r in recs}


def _err_preview(output: str, cap: int) -> str:
    return output[-cap:] if len(output) > cap else output


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
            for target in plan.free_fns:
                changed += make_fix_unit(self.session, d.unit, target).run()
            if plan.deferred:
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
            for impl_target in plan.impl_groups:
                changed += make_fix_unit(self.session, d.unit, impl_target).run()
            for fn_target in plan.free_fns:
                changed += make_fix_unit(self.session, d.unit, fn_target).run()
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
    )
    return ok, last_output
