"""The GPU protector: stop the RUN when the score keeps returning to a number
it has already read.

WHY. Run `1064759-0f23fb68` (2026-08-15 20:08 UTC → 2026-08-16 04:56 UTC, 7h48m)
took seven scored sweeps of cluster `stable` and read `pass=79 oracle=101` at six
of them; the seventh was the sweep after a 1-body re-stub. The per-binary vectors
of those six are byte-identical (`unittests src/lib.rs 37/0 … source 4/1`), so
across roughly 70 landed fills not one test changed verdict. The loop never
terminated on its own: the plateau budget charges only on a COMPLETED frontier
traversal, and with the dispatch cap a traversal is ~5 iterations, so `stall`
stood at 1 of 3 after 7h48m and the run ended on an operator's SIGTERM
(`deaths.jsonl`, exit=143). Every GPU-hour after the second identical reading
bought nothing that any measurement in the pipeline can see.

WHAT IT MEASURES. A reading is the pair (pass count, oracle size) — never the
pass count alone. The last twelve sweeps before this one were recorded at
oracles 79, 87, 101, 103 and 105, so `39` names five different measurements and
two of them are not comparable at all; pairing is what makes "the same number"
mean the same tree-level fact.

THE RULE. Over the last `window` JUDGED readings, if the reading a dispatch is
about to be launched against has already been seen `repeats` times AND at least
one COMPLETED frontier traversal separates two of those readings, the loop has
returned to a score it already had and the model calls about to be spent are the
ones that did not move it last time. That is a window rather than a consecutive
streak on purpose: the recorded pathology is not a flat line but a fixed point —
79 → re-stub → 32 → 39 → … → 79 — and a consecutive-only rule reads the re-climb
as movement every time.

THE TRAVERSAL CLAUSE IS NOT DECORATION, AND IT COST A REVIEW TO FIND. The
dispatch cap (`domain/dispatch_batch.py`) makes a sweep measure a SLICE of the
frontier — 12 units of 59 on the live anyhow tree — so "two sweeps in a row read
79/101" says twelve units bought nothing, not that the frontier did. Without
this clause the guard stops the run after two slices, i.e. having attempted 24
of 59 units, and it stops there again on every relaunch: `order_frontier` is
deterministic and a new process starts with an empty window and an empty
attempted-set, so it re-dispatches the SAME first 12 and reads the same score.
Units 25-59 then become unreachable without a human editing
`TDD_FLAT_SCORE_REPEATS` — a gate whose unlock condition is the progress the
gate itself prevents, which is the deadlock shape `domain/pass_highwater.py`
documents at length. Requiring a completed traversal bounds the loss to ONE
traversal per process (five iterations at the measured cap and frontier) while
keeping every later repeat immediate, because the window still holds the
mid-traversal readings and only the DECISION waits.

This is the same correction the plateau budget already carries: `TDD_MAX_STALLS`
charges only on a sweep that followed a completed traversal, so the two
terminators are now denominated in the same unit of time. A guard measured in
sweeps beside a budget measured in traversals is two clocks, and the faster one
decides everything.

WHAT IS NOT JUDGED. Three readings cannot judge a dispatch, and each is the same
exemption the plateau budget already applies (`phase_c_tdd.py`):

- a sweep inside a re-fill grace window measures the retrigger's DELIBERATE
  re-stub, not what the last dispatch bought;
- a sweep that strictly shrank the stub set is a cluster paying in stubs and
  being paid back in tests several iterations later (cluster `stable` of run
  `7445-00ee8a3c`, 2026-08-07: stubs 132 → 118 → 82 → 75 while pass went
  11 → 13 → 13);
- a sweep whose tree did not compile is not a measurement at all — its 0 is the
  absence of one, and the red-build budget owns that case.

Those sweeps are SKIPPED, not reset: they leave the window as they found it, so a
destruction whose whole re-climb lands back on the number it started from still
trips. The count of skipped sweeps travels with the guard so the stop line can
say how far apart the repeated readings really were.

WHAT A STOP COSTS, STATED HONESTLY. This gate governs a STOP, not a retry:
nothing is convicted, no exhaustion flag is written, no tree is restored, and
`pipeline_state.md` still reads `current: task2a`, so `bash continue.sh`
resumes on the same tree with every fill preserved. What a false stop does NOT
cost is one relaunch — that was the original claim here and it is wrong. The
guard is deterministic in its inputs and the loop is deterministic in its
dispatch order, so a relaunch that changes nothing reproduces the stop at the
same place; that is stated as intended behaviour in the entry that shipped this
module. A false stop is therefore a HALT until a human changes something, which
is why the traversal clause above matters and why the exemption list below is
the conservative part of this design rather than the lax part.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class ScoreReading:
    """One sweep's measurement: how many tests passed, out of how many ran.

    The denominator is part of the identity. `39/87` and `39/79` are different
    facts about the crate — the second oracle is eight tests smaller because a
    binary stopped reporting — and treating them as the same number is how a
    plateau hides inside a shrinking suite."""

    pass_count: int
    oracle_size: int

    def __str__(self) -> str:
        return f"{self.pass_count}/{self.oracle_size}"


@dataclass(frozen=True, slots=True)
class ScoreObservation:
    """A judged reading and the frontier traversal it was taken during.

    The traversal index is what makes "the same number twice" mean the loop has
    been round its own plan, rather than that two adjacent slices of one plan
    both bought nothing. The caller supplies it; the guard never derives it,
    because only the driver knows when a batch closed a pass."""

    reading: ScoreReading
    traversal: int


@dataclass(frozen=True, slots=True)
class FlatScoreGuard:
    """The window of judged readings, and whether the newest one repeats.

    Immutable: every transition returns a new guard, so a caller cannot record
    a reading by accident on a path that only meant to inspect one."""

    window: int = 5
    repeats: int = 2
    readings: tuple[ScoreObservation, ...] = ()
    # Excused sweeps since the newest recorded reading (`pending`), and the
    # number that sat immediately before it (`gap`). Two counters because the
    # recording and the decision happen in the same call: a single one is always
    # 0 by the time a stop line reads it, which is how far apart the two
    # identical readings were — the one thing an operator needs to judge the
    # stop by hand.
    pending: int = 0
    gap: int = 0

    @property
    def enabled(self) -> bool:
        """`repeats < 2` disables the guard (a repeat count of 1 would fire on
        the first sweep, before any dispatch has been judged), and a window
        smaller than the repeat count can never hold one."""
        return self.repeats >= 2 and self.window >= self.repeats

    def observed(self, reading: ScoreReading, traversal: int) -> FlatScoreGuard:
        """Record a judged sweep's reading, keeping the last `window` of them.

        `traversal` is how many complete passes over the frontier the loop has
        finished when this sweep was taken — see `domain/dispatch_batch.py`."""
        if not self.enabled:
            return self
        return replace(
            self,
            readings=(*self.readings,
                      ScoreObservation(reading, traversal))[-self.window:],
            pending=0, gap=self.pending)

    def excused(self) -> FlatScoreGuard:
        """A sweep that cannot judge the last dispatch. The window is left
        exactly as it was — the reading is not evidence in either direction —
        and the skip is counted so the stop line can report the gap."""
        if not self.enabled:
            return self
        return replace(self, pending=self.pending + 1)

    def repeated(self, reading: ScoreReading) -> int:
        """How many times `reading` appears in the window (0 when disabled)."""
        if not self.enabled:
            return 0
        return sum(1 for o in self.readings if o.reading == reading)

    def first_traversal_of(self, reading: ScoreReading) -> int | None:
        """The earliest traversal the window saw `reading` in, or None."""
        seen = [o.traversal for o in self.readings if o.reading == reading]
        return min(seen) if seen else None

    def trips_on(self, reading: ScoreReading, traversal: int) -> bool:
        """Would a dispatch launched against `reading` be spending on a score
        the window has already seen `repeats` times, across a completed pass
        over the frontier?

        The NEWEST reading has to be the repeated one. A window holding
        `79, 66, 79, 80` contains a repeat, but the sweep about to dispatch read
        80 — the loop moved, and stopping it there would be reading a stale
        entry as the present.

        And `traversal` has to have MOVED since the earliest matching reading.
        Two readings inside one traversal are two slices of a single plan; only
        a repeat that survives the loop finishing that plan is evidence about
        the frontier rather than about twelve of its units. With no cap (or a
        frontier under it) every batch closes a pass, the index advances every
        iteration, and this clause costs nothing — which is why it is invisible
        in every walk, all of which drive a one-fn frontier.

        `enabled` is checked HERE and not left to the arithmetic: with the guard
        turned off by `repeats=0`, an empty window's count of 0 satisfies
        `>= 0` and the disable switch would stop the run at its first
        dispatch."""
        if not self.enabled or self.repeated(reading) < self.repeats:
            return False
        first = self.first_traversal_of(reading)
        return first is not None and first < traversal

    def render(self, reading: ScoreReading) -> str:
        """The window as a log line: oldest → newest, each reading stamped with
        the traversal it was taken in, plus the repeat count."""
        return (f"{' '.join(f'{o.reading}@t{o.traversal}' for o in self.readings)} "
                f"(reading {reading} {self.repeated(reading)}x in the last "
                f"{len(self.readings)} judged sweep(s))")
