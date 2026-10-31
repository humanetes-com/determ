"""The held-out agreement reading: run a cluster's held-out captures against
a reconstructed tree and count how many print what the original printed.

This is the caller `run_behaviour_oracle` never had. That use case was
written to run behaviours against the reconstruction and was left unwired
(the comparison of 2026-08-25 already noted a harvest "not wired as a
gate"). It is called here with `unit_tests_green=True` whatever the tree's
state: that flag is a cost gate for running the WHOLE suite as a second
gate, and this is a reading of a handful of programs that must also be
taken on a tree with failures left — the recorded wrong tree was 98 pass /
7 fail, and a loop that closes on remaining failures is the common case.

Counts only leave this module. The report's `suspect_units` (the escalation
frontier a mismatch contributes) is not read: a held-out mismatch that aimed
a fill would be a steer, and it would turn the held-out set into oracle.
The per-capture rows go to `record_rows`, a file for the person reading the
run.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.run_behaviour_oracle import run_behaviour_oracle
from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSuite, BehaviourVerdict, then_value_text
from pipeline_core.domain.behaviour_probe import ProbeResult, capture_moved, printed_value, program_for
from pipeline_core.domain.held_out import Agreement, AgreementReading, AgreementRow, reading_of
from pipeline_core.ports.behaviour_probe import RunProbe

_OUTCOME = {BehaviourVerdict.HOLD: Agreement.AGREES, BehaviourVerdict.MISMATCH: Agreement.DIFFERS,
            BehaviourVerdict.NOT_RUN: Agreement.NOT_RUN}


@dataclass(frozen=True)
class AgreementDeps:
    load_held_out: Callable[[Path, str], BehaviourSuite]
    scratch_tree: Callable[[Path], AbstractContextManager[Path]]
    # (crate dir, cluster, program, name, extra_env=…) -> ProbeResult
    run_probe: RunProbe
    record_rows: Callable[[str, str, str, Sequence[AgreementRow]], object]
    log: Callable[[str], None]
    emit: Callable[..., object]


def judge_capture(spec: BehaviourSpec, result: ProbeResult) -> tuple[BehaviourVerdict, str]:
    """One held-out program's run against the reconstruction, as a verdict
    and what it printed (or why it printed nothing)."""
    if not (result.ok and result.stdout.strip()):
        why = "no stdout" if result.ok else (result.stderr[-300:].strip() or "failed, no stderr")
        return BehaviourVerdict.NOT_RUN, why
    if capture_moved(spec, result.stdout):
        return BehaviourVerdict.MISMATCH, printed_value(result.stdout)
    return BehaviourVerdict.HOLD, printed_value(result.stdout)


def measure_agreement(
    deps: AgreementDeps,
    *,
    project_dir: Path,
    tree_dir: Path,
    module: str,
    cluster: Any,
    where: str,
    oracle_pass: int | None = None,
    oracle_total: int | None = None,
    stubs: int | None = None,
) -> AgreementReading | None:
    """Take the reading for `cluster` on the crate at `tree_dir` and log it.
    None when the cluster has no held-out capture (nothing to read; said
    once, so an empty split is not mistaken for agreement). Never raises:
    the reading is additive and a failure to take it is logged."""
    name = getattr(cluster, "name", str(cluster))
    try:
        suite = deps.load_held_out(project_dir, name)
        if not suite.specs:
            deps.log(f"  [behaviour-agreement] {name} ({where}): no held-out capture — "
                     f"no reading (the split is applied when t0d takes captures)")
            return None
        by_id = {s.behaviour_id: s for s in suite.specs}
        with deps.scratch_tree(tree_dir) as crate:
            def run_spec(spec: BehaviourSpec) -> tuple[BehaviourVerdict, str]:
                n = list(by_id).index(spec.behaviour_id)
                return judge_capture(spec, deps.run_probe(
                    crate, cluster, program_for(spec, crate=module), f"held_{name}_{n}",
                    extra_env=dict(spec.required_env)))
            report = run_behaviour_oracle(suite, unit_tests_green=True, run_spec=run_spec)
        rows = [AgreementRow(o.behaviour_id, _OUTCOME[o.verdict],
                             expected=then_value_text(by_id[o.behaviour_id].then),  # type: ignore[arg-type]
                             observed=o.detail)
                for o in report.outcomes]
        reading = reading_of(name, where, rows, oracle_pass=oracle_pass,
                             oracle_total=oracle_total, stubs=stubs)
        deps.record_rows(module, name, where, rows)
        deps.log(f"  [behaviour-agreement] {name} ({where}): {reading.line()}")
        deps.emit("behaviour.agreement", **reading.payload())
        return reading
    except Exception as e:
        deps.log(f"  [behaviour-agreement] {name} ({where}): reading not taken: "
                 f"{type(e).__name__}: {e}")
        return None
