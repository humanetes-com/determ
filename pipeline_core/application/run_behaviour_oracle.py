"""Behaviour-oracle use-case (F10 Use-A / M4) — run the harvested positive
behaviours against the reconstruction and tally the verdicts.

Cost gate (plan_2 M4): the oracle is a SECOND gate on an already-unit-passing
tree — running N behaviour programs multiplies an hours-long t3, so it fires
ONLY when the caller confirms unit tests are green for the cluster. On a
unit-failing tree it is a no-op (the unit failures are the actionable signal).

Execution (compile+run one spec, decide HOLD/MISMATCH) is INJECTED — this module
is pure orchestration over VOs, so it is testable without cargo. An
assertion-behaviour holds iff it runs panic-free; an outcome-behaviour holds iff
the captured observable equals the frozen golden Then.
"""
from __future__ import annotations

from collections.abc import Callable

from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourSuite, BehaviourVerdict
from pipeline_core.domain.behaviour_oracle import (
    BehaviourOracleReport,
    BehaviourOutcome,
    summarize,
)

# (verdict, detail) for one spec run against the reconstruction.
RunSpec = Callable[[BehaviourSpec], tuple[BehaviourVerdict, str]]


def run_behaviour_oracle(
    suite: BehaviourSuite,
    *,
    unit_tests_green: bool,
    run_spec: RunSpec,
    log: Callable[[str], None] = lambda _m: None,
) -> BehaviourOracleReport:
    """Run every POSITIVE oracle spec in `suite` against the reconstruction.
    No-op (empty report) when unit tests are not green — the cost gate."""
    if not unit_tests_green:
        return BehaviourOracleReport(cfg_cluster=suite.cfg_cluster)
    specs = suite.positive_oracle_specs
    if not specs:
        return BehaviourOracleReport(cfg_cluster=suite.cfg_cluster)
    outcomes: list[BehaviourOutcome] = []
    for spec in specs:
        verdict, detail = run_spec(spec)
        outcomes.append(summarize(spec, verdict, detail))
    report = BehaviourOracleReport(cfg_cluster=suite.cfg_cluster,
                                   outcomes=tuple(outcomes))
    if report.mismatches:
        log(f"  [behaviour-oracle] {suite.cfg_cluster}: "
            f"{len(report.mismatches)} MISMATCH / {len(specs)} — compiles-but-"
            f"wrong; suspect units: {', '.join(sorted(report.suspect_units)) or '(none)'}")
    else:
        log(f"  [behaviour-oracle] {suite.cfg_cluster}: all {len(specs)} behaviours hold.")
    return report
