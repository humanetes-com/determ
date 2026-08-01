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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class Comparable(Protocol):
    """Anything a floor gauge can order (a plain count, a :class:`FixProgress`).
    Deliberately NOT a union of the two: mixing units in one gauge is the bug
    this module exists to prevent, and `int < FixProgress` raising TypeError is
    the loud failure that catches it."""

    def __lt__(self, other: Any, /) -> bool: ...


@dataclass(frozen=True, order=True, slots=True)
class FixProgress:
    """Lower-is-better fix-loop metric that keeps the COMPILE tier and the TEST
    tier SEPARATE, ordered lexicographically: fewer compile errors always wins,
    and only at an equal compile-error count does the failing-test count decide.

    A fix attempt improves along two axes that share no unit. Collapsing them
    into one integer inverts the ordering at the exact moment an attempt breaks
    through:

        WRONG:  attempt 3 → 2 rustc errors        → metric 2
                attempt 4 → compiles, 12 tests red → metric 12  (reads WORSE →
                stall → loop stops on the first attempt that ever compiled)

        RIGHT:  attempt 3 → FixProgress(2, 0)
                attempt 4 → FixProgress(0, 12)     → strictly better (0 < 2)

    `compile_errors` carries the rc-honest minimum convention of its callers: a
    non-zero rc with no parseable diagnostic is still at least one error, never
    zero, so a red attempt can never tie a green one.
    """

    compile_errors: int
    failing_tests: int = 0

    @property
    def compiles(self) -> bool:
        return self.compile_errors == 0

    def __str__(self) -> str:
        if self.compile_errors:
            return f"{self.compile_errors} compile err(s)"
        return f"compiles, {self.failing_tests} failing test(s)"


@dataclass(frozen=True, slots=True)
class SalvagedFill:
    """The best COMPILING body a fix loop produced, and the metric it scored."""

    body: str
    metric: FixProgress


class CompilingSalvage:
    """Remembers the best compiling attempt of a fix loop so exhaustion can
    restore it instead of writing the `unimplemented!()` stub back.

    A stub is NOT a neutral zero. It panics at runtime, which aborts the whole
    test binary, so every sibling test in that binary reports no verdict at all:

        WRONG:  fn compiles, 2 of its own tests red  → exhausted → stub
                → binary aborts → 68 tests unjudged, score frozen
        RIGHT:  fn compiles, 2 of its own tests red  → exhausted → body kept
                → 2 tests red, the other 66 get judged

    A body that compiles is therefore never worse than the stub on this fn's own
    tests (the stub cannot pass them either) and strictly better on its
    siblings' — provided it reddens nothing that was green, which the caller
    still verifies with the regression gate before keeping it.
    """

    __slots__ = ("_best",)

    def __init__(self) -> None:
        self._best: SalvagedFill | None = None

    def offer(self, body: str, metric: FixProgress) -> None:
        """Record an attempt. Red attempts and empty bodies are ignored — only a
        body that COMPILED is a candidate to keep over the stub."""
        if not body.strip() or not metric.compiles:
            return
        if self._best is None or metric < self._best.metric:
            self._best = SalvagedFill(body, metric)

    @property
    def best(self) -> SalvagedFill | None:
        return self._best


BEST_COMPILE_KEY = "best_compile_errors"
BEST_TESTS_KEY = "best_failing_tests"


def best_progress_of(record: Mapping[str, Any]) -> FixProgress | None:
    """The floor a retrospective record has already reached, or None when it has
    none yet — including records written before the metric carried tiers. None
    means "treat this round as the baseline", the lenient direction: a fn is
    never parked on a floor that was never measured in comparable units."""
    lo, hi = record.get(BEST_COMPILE_KEY), record.get(BEST_TESTS_KEY)
    if lo is None or hi is None:
        return None
    return FixProgress(int(lo), int(hi))


def best_progress_fields(best: FixProgress) -> dict[str, int]:
    """The persisted form of a floor — both tiers, never one collapsed count."""
    return {BEST_COMPILE_KEY: best.compile_errors,
            BEST_TESTS_KEY: best.failing_tests}


def readmission_warranted(
    *, fills_since_reset: int, best_metric: int, metric_at_last_reset: int,
) -> bool:
    """Whether re-admitting previously-exhausted units is bought evidence.

    An exhausted unit is one the loop already convicted of "nothing left to
    try". Re-admitting the set costs a full extra frontier of model calls, so
    the price is NEW CONTEXT — and new context is what a retry needs to be
    anything other than the same attempt again.

    Both conditions are required and neither substitutes for the other:

    * ``fills_since_reset`` — work actually landed in the tree. Necessary
      (nothing changed otherwise) but NOT sufficient.
    * ``best_metric > metric_at_last_reset`` — that work moved the score.

    Gating on fills alone counted a fill that changed no verdict exactly like
    one that fixed a test, so the loop financed its own growth out of work that
    produced no evidence: live on `anyhow/nightly`, 51 fills moved pass=93 by
    +0, re-admitted the fns that had already failed, and took the frontier
    41 -> 73 with iteration cost following to 224 min. Reading the FLOOR rather
    than the last measurement is what stops one real gain from paying for
    re-admission twice.

    ``metric_at_last_reset`` is a PRECONDITION on the caller: any score beats
    a negative sentinel, so an unseeded baseline grants one re-admission
    unconditionally. That grant is deliberate — it is the only way a unit that
    is ITSELF the blocker ever gets re-admitted, since the flags persist on
    disk and releasing them otherwise needs the score move the unit is
    blocking. What the caller must decide is how often to spend it: run 589477
    iter 2 spent it on 7 units off a fill phase measured at `by pair: +0 -0`,
    frontier 40 -> 73. See `run_phase_c_tdd`, which seeds the baseline unless
    the CODE changed since the process that set the flags.
    """
    return fills_since_reset > 0 and best_metric > metric_at_last_reset


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

    @property
    def repeated(self) -> bool:
        """Metric loops have no repeated-signature exit (that is the outcome
        view's churn guard). Present so `LoopState` satisfies the
        `StallGauge` protocol the unified loop engine reads."""
        return False

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


@dataclass
class FloorStall:
    """Floor-stall gauge for lower-is-better metrics (a :class:`FixProgress`, a
    remaining-stub count, a last-pass failure count). Every observation on ONE
    gauge must be the same metric type — a gauge fed rustc error counts on some
    attempts and failing-test counts on others measures nothing.
    One `observe(count, signature=...)` per iteration:
    a strict decrease of the floor resets the stall counter, anything else
    increments it. `signature` is an equality-comparable snapshot of the errors
    (e.g. a frozenset of "file:code" strings); `repeated` is True when two
    consecutive observations carried the same non-None signature — the
    "identical errors two attempts running" early exit the fix loops share.

    Replaces the hand-rolled best/stall/prev_sig triples in the per-fn inner
    loop, the per-impl block loop, and the pass-level gauge (slice 8a).
    `LoopState` above is the full meta-awareness history for loops driven by
    `run_refine_loop`; FloorStall is the minimal gauge for inner loops that
    keep their own history/prompt machinery.
    """
    best: Comparable | None = None
    stalls: int = 0
    observations: int = 0
    repeated: bool = False
    _prev_sig: object = None

    def observe(self, count: Comparable, signature: object = None) -> bool:
        """Record one iteration's count; returns True when the floor improved."""
        self.observations += 1
        improved = self.best is None or count < self.best
        if improved:
            self.best = count
            self.stalls = 0
        else:
            self.stalls += 1
        self.repeated = (
            self.observations > 1
            and signature is not None
            and signature == self._prev_sig
        )
        self._prev_sig = signature
        return improved

    def stalled(self, max_stalls: int) -> bool:
        return self.stalls >= max_stalls


@dataclass(frozen=True, slots=True)
class RetroCandidate:
    """One retro-round attempt's own-test/compile state — the two inputs
    `is_retro_improvement` compares against the PRIOR attempt's state."""
    own_tests_passing: int
    compile_error_count: int


def is_retro_improvement(old: RetroCandidate, new: RetroCandidate) -> bool:
    """Whether a retro-round retry counts as forward motion, worth one more
    round. Composes the two improvement signals the pipeline already tracks
    SEPARATELY elsewhere (`FloorStall`'s lower-is-better error count,
    `LoopState`'s higher-is-better metric) into the ONE comparator any
    "should we spend another round on this?" decision can share, instead of
    each loop inventing its own slightly different heuristic:

      1. More of the fn's OWN attributed tests passing than before — the
         primary signal, since that's the thing actually being optimised for.
      2. If the passing-test count is UNCHANGED, a lower compile-error count
         still counts as progress (closer to compiling, even if not yet
         test-reachable) — this tier is what lets a still-broken attempt
         that's nonetheless getting closer earn one more round instead of
         being judged solely on the binary pass/fail of its own tests.

    Same-or-fewer tests passing AND same-or-higher error count → not an
    improvement; the caller should stop spending rounds on this unit.
    """
    if new.own_tests_passing != old.own_tests_passing:
        return new.own_tests_passing > old.own_tests_passing
    return new.compile_error_count < old.compile_error_count


def parse_retrospective_function(line: str) -> tuple[str, str] | None:
    """Parse a retrospective `FUNCTION <name>: [(x) ]<KEYWORD> …` line into
    `(fn_name, keyword)` (regex-free, R7; mirrors
    `FUNCTION\\s+([^:]+(?:::[^:]+)*):\\s*(?:\\([a-d]\\)\\s*)?(\\w+)`). The name may
    contain `::` path separators but ends at the first single `:`. Returns None
    when the line is not a parseable FUNCTION line."""
    if not line.startswith("FUNCTION"):
        return None
    n = len(line)
    i = len("FUNCTION")
    j = i
    while j < n and line[j].isspace():        # \s+
        j += 1
    if j == i or j >= n or line[j] == ":":    # need \s+ then [^:]+ (≥1 non-colon)
        return None
    name_start = j
    while j < n:                              # [^:]+(?:::[^:]+)*
        if line[j] != ":":
            j += 1
        elif line[j:j + 2] == "::" and j + 2 < n and line[j + 2] != ":":
            j += 2                            # `::` path separator → keep going
        else:
            break                             # single colon → name ends
    if j >= n or line[j] != ":":
        return None
    name = line[name_start:j]
    j += 1
    while j < n and line[j].isspace():        # \s*
        j += 1
    if j + 2 < n and line[j] == "(" and line[j + 1] in "abcd" and line[j + 2] == ")":
        j += 3                                # optional (a|b|c|d) tag
        while j < n and line[j].isspace():    # \s*
            j += 1
    k = j
    while k < n and (line[k].isalnum() or line[k] == "_"):  # (\w+)
        k += 1
    if k == j:
        return None
    return name.rstrip(":"), line[j:k]


def parse_retrospective_categories(retrospective: str) -> dict[str, list[str]]:
    """Parse retrospective FUNCTION lines into a category → [fn_names] map.

    Handles both the abbreviated form '(c) UNREACHABLE' and the bare keyword form;
    each function is filed under the first known category its keyword contains."""
    cats: dict[str, list[str]] = {
        "UNREACHABLE": [], "CFG_GATED": [], "TESTABLE": [], "NEEDS_INTERNALS": [],
    }
    for line in retrospective.splitlines():
        parsed = parse_retrospective_function(line.strip())
        if parsed is None:
            continue
        fn_name, keyword = parsed[0], parsed[1].upper()
        for known in cats:
            if known in keyword:
                cats[known].append(fn_name)
                break
    return cats
