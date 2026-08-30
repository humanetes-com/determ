"""Publish the score the regression gate already measures, mid-fill.

WHY THIS EXISTS. An iteration is: sweep, then a fill phase, then the next
sweep. The sweep costs 0.7 s; the fill costs two to four hours
(`1383168-eb151d09` iter 1: file 8m42s, impl 48m35s, per-fn 45m17s). The
score is only ever recorded at the top of an iteration, so for the whole
fill phase the operator can see which route is walking and how many units,
and cannot see whether anything is working.

That blindness is not necessary, and this module is the wire that was never
run. The regression gate calls `cargo_runner.measure_test_state`, which runs
the FULL cluster `test_cmd` and returns a per-binary `TestStateReading` with
`pass_count`, `fail_count`, `passed_names` and `failed_names`. It does this
as a pre-fill baseline AND a post-fill re-measure, on every splice — nine
times inside run `1519852-5d607329`'s single iteration, and that run was
killed before its fill phase finished. `compare_readings` then produces a
`RegressionComparison` carrying `base_pass`, `cur_pass` and
`net_pass_delta`. The gate reduced all of it to a regression yes/no and
dropped the numbers: `regression_guard.py` held no emit call of any kind,
and every event in the store carrying a `pass_count` was a sweep-boundary
event (36 in total from 2026-08-28 to 08-30, 17 of them iteration lines).

WHAT IT COST, measured over cluster `stable` since 2026-08-14. Same-oracle
deltas WITHIN a run: n=35, total +10, 2 positive, 33 zero, **0 negative**.
ACROSS a kill: n=56, total +30, 6 positive, 50 zero, **0 negative**. Nothing
was reverting good fills — the operator's standing impression that "progress
only happens when I kill the pipeline" was a measurement-placement artefact.
51 of 67 runs lived one or two sweeps, i.e. they died inside a fill phase, so
that phase's payoff was measured by the NEXT run's entry sweep because that
was the next sweep that ever happened. Run `1519852-5d607329`'s bodies were
spliced and KEPT ("judged for real at the next sweep, in the joint tree");
its own iter 2 would have read the same 101/126 that `1618155-b60a8c4c`'s
entry sweep read (+18 pairs, -0). Killing relocated the credit; it never
created it. Restarting to see a number the process already knew was the only
lever available, and it is an expensive one: it moves `code_id`, releases
cursor units for retry, and re-pays an entry sweep.

WHAT THIS DELIBERATELY DOES NOT CLAIM. The observer fires at the MEASUREMENT
— immediately after `compare_readings` — and not at the gate's exit. So
`regressed` here is the comparison's own reading and not the final keep/
revert: the crash-only branch below it can still keep a fill the comparison
called regressed (a still-hollow sibling's panic is not the fill's fault),
and the file-first peel path can revert one it called clean. Firing at the
measurement is the point rather than a shortcut — the gate has six exits
across 170 lines and threading an outcome through all of them would put
control-flow risk into a change whose whole value is that it cannot alter
control flow. The keep/revert is already logged by the `guard(...)` lines
that sit immediately after this one. A consumer that needs the OUTCOME
rather than the reading is the re-ranking work, which has to join these
events to the splice results anyway.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pipeline_core.domain.test_results import RegressionComparison

#: Log prefix, chosen to be greppable next to the `[phase-c/tdd] iter N:`
#: lines it interleaves with. A reader tailing the log during a fill phase
#: sees these and nothing else move.
TAG = "[phase-c/fill-reading]"


def render_fill_reading(cmp: RegressionComparison) -> str:
    """One line: what the suite scored, and what moved since the baseline.

    Reads as a sweep line on purpose — the numbers have the same meaning as
    `pass=` on the iteration line, they are simply taken mid-fill."""
    delta = cmp.net_pass_delta
    parts = [f"pass {cmp.base_pass} -> {cmp.cur_pass} (net {delta:+d})",
             f"executed {cmp.base_executed} -> {cmp.cur_executed}",
             f"{len(cmp.measured)} binar{'y' if len(cmp.measured) == 1 else 'ies'} measured"]
    if cmp.newly_red:
        parts.append("newly red: " + ", ".join(sorted(cmp.newly_red)[:6]))
    if cmp.newly_crashed:
        parts.append(f"newly crashed: {list(cmp.newly_crashed)} "
                     f"(was passing {cmp.crash_lost_pass})")
    if cmp.excluded_crashed:
        parts.append(f"{len(cmp.excluded_crashed)} excluded (crashed at baseline)")
    parts.append("comparison REGRESSED — the gate decides the keep below"
                 if cmp.regressed else "no regression in the measured scope")
    return f"  {TAG} " + "; ".join(parts)


def fill_reading_fields(cmp: RegressionComparison) -> dict[str, Any]:
    """The event payload. Named to match `phase_c.tdd.iter` where the meaning
    is the same (`pass_count`), so a reader can put a mid-fill reading and a
    sweep on one axis without a translation table."""
    return {
        "pass_count": cmp.cur_pass,
        "base_pass": cmp.base_pass,
        "net_pass_delta": cmp.net_pass_delta,
        "base_executed": cmp.base_executed,
        "cur_executed": cmp.cur_executed,
        "measured_binaries": len(cmp.measured),
        "newly_red": sorted(cmp.newly_red),
        "newly_crashed": list(cmp.newly_crashed),
        "crash_lost_pass": cmp.crash_lost_pass,
        "excluded_crashed": list(cmp.excluded_crashed),
        "regressed": cmp.regressed,
    }


def publish_fill_reading(
    cmp: RegressionComparison,
    *,
    emit: Callable[..., None] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Emit `phase_c.fill.reading` and log one line for a mid-fill measurement.

    Both sinks are optional so a harness can bind one, neither, or both. Never
    raises into the gate: this is telemetry hung off a decision path, and a
    formatting bug here must not be able to revert a body."""
    try:
        if log is not None:
            log(render_fill_reading(cmp))
        if emit is not None:
            emit("phase_c.fill.reading", **fill_reading_fields(cmp))
    except Exception:  # noqa: BLE001 — telemetry may never break the gate
        pass
