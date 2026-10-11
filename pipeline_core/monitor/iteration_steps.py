"""Which step of one TDD iteration the loop is in.

The phase strip at the top of the panel stops at `t2a`, and t2a is where the
hours go: one iteration of `run_phase_c_tdd` is a fixed sequence, and the
panel could say which of its steps was running only by reading the log by
eye. The five steps below are that sequence, in the order the loop executes
them (`application/reconstruct/phase_c_tdd.py`, the `while True:` at the top
of the cluster loop):

    sweep      `deps.run_cargo(test_cmd, ...)` — the cluster's cargo test
    grade      batch ratchet, best-tree/checkpoint, floor, ledger promotion,
               stall + high-water accounting (everything keyed to the reading
               the sweep just took)
    aim        `deps.attribute_failures(...)`, the crash localizer, the
               futility escalation — which units the failing tests implicate
    frontier   eligible set, re-admission, deferral, the flat-score/no-verdict
               stops, ranking, and the batch `take` — ends at the
               `iter N: cargo test rc=… dispatching X of Y` line
    fill       `task2_fill_fns_one_by_one` — file-first, impl, per-fn

WHY THE STEP IS DERIVED HERE AND NOT EMITTED BY THE LOOP. A step event from
`phase_c_tdd.py` would be authoritative, and it would cost more than it is
worth: `domain/measurement.code_identity` hashes every `.py` the pipeline
composes and EXCLUDES `pipeline_core/monitor/`, precisely so an observer-only
edit cannot invalidate a resume. Run `3097970-03bcf403` (2026-08-18) is what
that exclusion is written from — an observer-only fix moved the id, the TDD
floor resumed with its pass cursor and futility evidence discarded, and the
crawl re-spent a ~68-minute relaunch cycle. Teaching the loop to narrate
itself for the panel's benefit would spend exactly that, on every run that
resumes after this change, for a display.

So the step is read from what the loop ALREADY publishes, and only from
markers whose emit site is known:

* the typed events — `phase_c.attribution.*` is emitted inside the aim block
  and nowhere else, `phase_c.impl.fill.start` inside the fill and nowhere
  else, and so on. The map below is by exact name or by an unambiguous family
  prefix, and an event that is NOT in it leaves the step unchanged. That rule
  is what keeps a new event name from silently moving the strip;
* one log line for the one step no event covers. The sweep emits nothing —
  cargo is running — so `iter N: running cargo test...` (phase_c_tdd.py, the
  line immediately above `deps.run_cargo`) is the marker, and the log's own
  staleness is then the time spent in the step.

The strip reports the last step the loop was OBSERVED in. It is deliberately
not a state machine: no step is inferred from the absence of another, and a
backward move (the crash retrigger re-aiming after the frontier was built) is
shown as it happened rather than smoothed into forward progress.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["STEPS", "STEP_IDS", "IterationStep", "step_index",
           "step_of_event", "step_of_log_line", "SWEEP_LOG_MARKER"]


@dataclass(frozen=True, slots=True)
class IterationStep:
    """One step of the TDD iteration: its id (also the strip's label) and what
    the loop is doing while it is current."""

    id: str
    meaning: str


STEPS: tuple[IterationStep, ...] = (
    IterationStep("sweep", "cargo test on the cluster tree — the reading "
                           "every gate below is charged on"),
    IterationStep("grade", "what last iteration's fills bought: batch "
                           "ratchet, best-tree/checkpoint, floor, ledger, "
                           "stall and high-water accounting"),
    IterationStep("aim", "which units the failing tests implicate: "
                         "attribution chain, crash localizer, futility "
                         "escalation"),
    IterationStep("frontier", "build the frontier and choose this "
                              "iteration's batch: re-admission, deferral, "
                              "ranking, dispatch cap"),
    IterationStep("fill", "dispatch the batch — file-first, impl blocks, "
                          "per-fn; the hours of the iteration"),
)

STEP_IDS: tuple[str, ...] = tuple(s.id for s in STEPS)


def step_index(step_id: str) -> int:
    """Position of `step_id` in the iteration, or -1 if it is not a step."""
    return STEP_IDS.index(step_id) if step_id in STEP_IDS else -1


# Exact event names, each keyed to the block its emit site sits in. Only names
# whose site was read are listed; everything else leaves the step alone.
_EVENT_STEP: dict[str, str] = {
    # grade — between the sweep and `attribute_failures`
    "phase_c.tdd.oracle_vanished": "grade",
    "phase_c.tdd.batch_withheld": "grade",
    "phase_c.tdd.batch_revert": "grade",
    "phase_c.ledger.judged": "grade",
    "phase_c.tdd.restore_unmeasured": "grade",
    "phase_c.tdd.no_verdict_moved": "grade",
    "phase_c.tdd.cursor_released": "grade",
    "phase_c.tdd.highwater_rebased": "grade",
    "phase_c.tdd.stall_not_charged_midpass": "grade",
    "phase_c.tdd.stub_progress": "grade",
    "phase_c.checkpoint": "grade",
    # frontier — the eligible build through the rank and `take`
    "phase_c.tdd.compile_blocked_frontier": "frontier",
    "phase_c.dispatch.deferred": "frontier",
    "phase_c.dispatch.refills_deferred": "frontier",
    "phase_c.tdd.no_move_readmission": "frontier",
    "phase_c.tdd.flat_score_withheld": "frontier",
    "phase_c.re_buy": "frontier",
    # fill — the engine's own vocabulary, plus the loop's post-fill audit
    "phase_c.verdict": "fill",
    "phase_c.retrospective": "fill",
    "phase_c.retro.call": "fill",
    "phase_c.test_fix.iter": "fill",
    "phase_c.dispatch.impl_route_deferred": "fill",
    "phase_c.tdd.landed_change": "fill",
    "phase_c.tdd.pass_audit": "fill",
}

# Families whose every member is emitted inside one block. Checked after the
# exact names, longest prefix first, so a single deviant name can be pinned
# above without disowning its family.
_PREFIX_STEP: tuple[tuple[str, str], ...] = (
    ("phase_c.attribution.", "aim"),
    ("phase_c.crash.", "aim"),
    ("phase_c.layout_retrigger.", "aim"),
    ("phase_c.file.fill.", "fill"),
    ("phase_c.fn.fill.", "fill"),
    ("phase_c.impl.", "fill"),
    ("phase_c.fill.", "fill"),
    ("phase_c.pass.", "fill"),
    ("phase_c.refill.", "fill"),
)


def step_of_event(name: str, data: dict[str, Any] | None = None) -> str | None:
    """The step this event proves the loop was in, or None if it proves none.

    `phase_c.tdd.iter` is the one event that needs its payload: it is emitted
    at the END of the frontier step, immediately before the fill call — but
    the loop also emits it on the iterations that dispatch nothing and go
    straight back to the sweep. `dispatched` is what separates them, so the
    strip advances to `fill` only when a batch actually went out.
    """
    if name == "phase_c.tdd.iter":
        dispatched = (data or {}).get("dispatched")
        return "fill" if isinstance(dispatched, int) and dispatched > 0 \
            else "frontier"
    step = _EVENT_STEP.get(name)
    if step is not None:
        return step
    for prefix, sid in sorted(_PREFIX_STEP, key=lambda kv: -len(kv[0])):
        if name.startswith(prefix):
            return sid
    return None


# Written by `phase_c_tdd.py` on the line above `deps.run_cargo`, and the only
# marker for the step that emits no event.
SWEEP_LOG_MARKER = ": running cargo test"


def step_of_log_line(line: str) -> str | None:
    """The step the pipeline log's LAST line proves, or None.

    Only the sweep is read this way, and only from the last line: the marker
    means cargo has been started and nothing has been logged since, so the
    line's own age is the time spent in the step. Every other step publishes
    events, which cannot be reordered by a log rotation or a partial write.
    """
    if SWEEP_LOG_MARKER in line and "[phase-c/tdd]" in line:
        return "sweep"
    return None
