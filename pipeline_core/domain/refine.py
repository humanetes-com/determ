"""Refine-loop domain types — the shared shape of every "generate → measure → (retro on
stall) → retry until improved-or-budget" loop in the pipeline (task0c augmentation, per-fn
fill, the TDD driver, post-merge fill, the compile-fix loop).

`LoopState` is the accumulated history; `LoopState.context()` renders the **meta-awareness** the
next attempt should see — which loop iteration it is, how many consecutive stalls, a compact
trajectory of what each prior attempt did, and the latest retrospective. Previously each loop
re-tried blind (no awareness it was repeating); centralising it here means every loop that runs
through `application.refine_loop.run_refine_loop` gets that awareness uniformly.

Pure domain: stdlib only, no I/O. The strategy (which actually calls the model / cargo) is the
:class:`pipeline_core.ports.RefineStrategy` port, injected by each task.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Attempt:
    """One iteration's outcome. `iteration`/`improved` are set by the loop driver, so a strategy
    may construct an Attempt with just `metric` (+ optional error/note)."""
    metric: float             # the value being improved (coverage %, pass count, -error count, …)
    iteration: int = 0        # 1-based; assigned by the driver
    improved: bool = False    # did `metric` beat the best-so-far? recomputed by the driver
    error: str = ""           # compile/test output excerpt for this attempt ("" if none)
    retro: str = ""           # retrospective guidance in force for this attempt
    note: str = ""            # free-form, e.g. "model flagged unreachable"


@dataclass
class LoopState:
    """Accumulated loop history + the meta-awareness renderer. Mutated by the loop driver."""
    attempts: list[Attempt] = field(default_factory=list)
    stalls: int = 0                       # consecutive non-improving attempts
    best_metric: float = float("-inf")
    pending_retro: str = ""               # retro for the attempt about to run (driver-set)

    @property
    def iteration(self) -> int:
        """1-based index of the attempt about to run."""
        return len(self.attempts) + 1

    def record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        if attempt.improved:
            self.best_metric = max(self.best_metric, attempt.metric)
            self.stalls = 0
        else:
            self.stalls += 1
        self.pending_retro = ""

    def latest_retro(self) -> str:
        if self.pending_retro:
            return self.pending_retro
        for a in reversed(self.attempts):
            if a.retro:
                return a.retro
        return ""

    def context(self, *, max_trajectory: int = 6) -> str:
        """Render the meta-awareness for the NEXT attempt's prompt. Empty until there is history.

        Shows: which iteration, consecutive stalls, a compact per-attempt trajectory (metric +
        delta + any note), and the most recent retrospective. The caller decides where to splice
        this into its prompt; the loop driver passes it to `strategy.attempt(state)`.
        """
        if not self.attempts:
            return ""
        lines = [
            f"RETRY CONTEXT — attempt {self.iteration} on this unit; "
            f"{self.stalls} consecutive attempt(s) with no improvement (best metric {self.best_metric:g})."
        ]
        recent = self.attempts[-max_trajectory:]
        lines.append("Prior attempts (most recent last) — do NOT repeat an approach that already failed:")
        prev: float | None = None
        for a in recent:
            delta = "" if prev is None else f" ({a.metric - prev:+g})"
            tag = f" — {a.note}" if a.note else ("" if a.improved else " — no gain")
            lines.append(f"  #{a.iteration}: metric={a.metric:g}{delta}{tag}")
            prev = a.metric
        retro = self.latest_retro()
        if retro:
            lines.append(f"Latest retrospective: {retro}")
        return "\n".join(lines) + "\n"
