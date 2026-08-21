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
about to be launched against has already been seen `repeats` times, the loop has
returned to a score it already had and the model calls about to be spent are the
ones that did not move it last time. STOP. That is a window rather than a
consecutive streak on purpose: the recorded pathology is not a flat line but a
fixed point — 79 → re-stub → 32 → 39 → … → 79 — so `83 → 79 → 83` has to trip
exactly as `83 → 83` does, and a consecutive-only rule reads the re-climb as
movement every time.

Two readings of the same number are the whole rule. Nothing else has to be true
— not a completed traversal, not a stalled stub count, not a closed pass. Every
additional conjunct this guard has carried has been a way for it to stay silent
through a burn (see the traversal clause below and the exemption list further
down), and each was withdrawn only after a run paid for it in GPU-hours.

THE TRAVERSAL CLAUSE WAS THE RULE'S SECOND HALF AND IS NOW OFF BY DEFAULT. The
dispatch cap (`domain/dispatch_batch.py`) makes a sweep measure a SLICE of the
frontier — 12 units of 59 on the live anyhow tree — so "two sweeps in a row read
79/101" says twelve units bought nothing, not that the frontier did. The clause
therefore required a COMPLETED traversal to separate two matching readings
before the stop could charge, so the rest of the frontier got its turn.

It was withdrawn as a precondition on 2026-08-17 because it waits on a quantity
the loop is not obliged to produce, and three runs measured what that costs —
every one of them ended on an operator's stop rather than the loop's own
decision:

    1064759-0f23fb68  7h48m  79/101 at six of seven sweeps; stall 1 of 3
    1692702-94e6163a  3h36m  frontier 35 -> 57 -> 63 outgrew the cursor; no
                             pass closed; `first < traversal` read `0 < 0`
    1920662-40f711b2  9h48m  repeat clause satisfied at iter 3 (00:25:43Z,
                             `83/106` twice); withheld on `traversal 0`, which
                             the run never left; 191 model calls, net -46

The cohort fix in `domain/dispatch_batch.py` was supposed to make traversals
tick again, and on the third run it did not: the cohort opened at iter 2 over a
frontier of 43, iter 3 attempted 15 of them against a frontier that had shrunk
to 16, and iter 4 left through a crash retrigger's `continue`, which commits no
batch by design. From iter 5 a grace window excused every sweep.

WHAT REPLACES IT. The concern was real — a stop on the second repeat has
attempted 12 units of 59 — but the remedy did not have to be a precondition.
`repeats` is 2 and every process starts with an EMPTY window, so a relaunch
records one reading, dispatches ONE batch, and can only trip on the sweep that
follows it. That batch is a slice the previous process never reached, because
`TddFloor.attempted` carried the cursor across the boundary. Progress through
the frontier is bounded below by one fresh slice per relaunch rather than by a
traversal that may never complete, and the stopped run's fills are all still on
disk, which moves the frontier on its own. The loss is bounded and
self-unlocking; the clause's failure mode was neither.
`TDD_FLAT_SCORE_REQUIRE_TRAVERSAL=1` restores it.

ONE batch per process, not two, and the arithmetic matters to whoever is
relaunching. The reading is recorded at the sweep and the decision is taken
BEFORE the dispatch, so the second sweep's recording is already the second
occurrence and the second batch never goes out — pinned by
`test_the_gpu_guard_stops_the_capped_loop_on_the_second_repeat`, which asserts
exactly one. Covering a frontier of F at cap C therefore costs ceil(F/C)
relaunches, not half that: at the live cap of 12 and the 33-unit frontier of
run `1920662-40f711b2`'s last iteration, three.

The plateau budget still charges on a completed traversal (`TDD_MAX_STALLS`),
so the two terminators are no longer denominated in the same unit. That is
deliberate and is the point: a guard that can only charge when another
mechanism advances is not a second clock, it is the first one wearing a hat.

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

from collections.abc import Sequence
from dataclasses import dataclass, replace

from pipeline_core.domain.loop_facts import (
    Fact,
    PassClosed,
    SweepMeasured,
)


@dataclass(frozen=True, slots=True)
class UnmovedVerdicts:
    """The SECOND clock: consecutive judged sweeps that landed bodies and moved
    no test's verdict.

    WHY A SECOND ONE. The guard below and the plateau budget are both
    denominated in completed frontier traversals, and run `1692702-94e6163a`
    (2026-08-16) showed that unit can stop ticking: its frontier grew 35 -> 57
    -> 63 against a cursor at 12 -> 21 -> 36, so no pass closed, `_traversals`
    stayed 0, `trips_on`'s `first_traversal < traversal` test read `0 < 0`, and
    `stall` read 0 of 3 at every iteration. Three sweeps of `79/101`, 84 model
    calls, 3h36m, and no exit the loop could take by itself. The cohort fix in
    `domain/dispatch_batch.py` makes traversals tick again; this clock is what
    makes the stop independent of whether they do.

    WHAT IT READS, and why that is the right quantity. The per-binary verdict
    vector is the axis `domain/zero_payoff.py` already settled on: unchanged
    exactly when no test anywhere took a different verdict, immune to the
    loop's own bookkeeping about frontiers, caps and passes. The sweep line
    that reports it (`N body(ies) landed since the last sweep and NO test
    changed verdict`) was printing on this run at iterations 2 and 3 — 18 and
    28 bodies — while every terminator sat blind. Counting it is free.

    WHAT IS NOT COUNTED, on the same rule the guard below uses: a sweep inside
    a re-fill grace window (the vector is expected to move DOWN — the loop
    destroyed work on purpose), and a sweep with nothing landed since the last
    one (it judges no dispatch). Those leave the streak exactly as they found
    it rather than resetting it, so a plateau interrupted by an idle sweep is
    still a plateau. Any sweep where the vector MOVED resets it, whichever
    direction it moved in: a changed verdict is the loop demonstrably doing
    something, which is all this clock claims to measure.
    """

    limit: int = 3
    streak: int = 0
    landed: int = 0

    @property
    def enabled(self) -> bool:
        """`limit <= 0` disables the clock (the env knob's off switch)."""
        return self.limit > 0

    def moved(self) -> UnmovedVerdicts:
        """A judged sweep whose vector changed — the streak restarts."""
        return replace(self, streak=0, landed=0) if self.enabled else self

    def unmoved(self, landed: int) -> UnmovedVerdicts:
        """A judged sweep that landed `landed` bodies and moved no verdict."""
        if not self.enabled:
            return self
        return replace(self, streak=self.streak + 1,
                       landed=self.landed + max(0, landed))

    @property
    def trips(self) -> bool:
        return self.enabled and self.streak >= self.limit


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
    # The traversal precondition on `trips_on`, OFF by default — see that
    # method for the three runs that measured it. Kept as a field rather than
    # deleted so the old behaviour is an env var and not a code edit, and so
    # the walks can drive both.
    require_traversal: bool = False
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
        the window has already seen `repeats` times?

        The NEWEST reading has to be the repeated one. A window holding
        `79, 66, 79, 80` contains a repeat, but the sweep about to dispatch read
        80 — the loop moved, and stopping it there would be reading a stale
        entry as the present.

        THE TRAVERSAL CLAUSE IS OFF BY DEFAULT, AND THE THIRD RUN IS WHY. It
        required `traversal` to have moved since the earliest matching reading,
        on the argument that two readings inside one traversal are two slices
        of one plan. The argument is sound and the clause is still unsound as a
        PRECONDITION, because the quantity it waits on is one the loop is not
        obliged to ever produce. Three runs, each ending on an operator's stop
        rather than the loop's own:

          `1064759-0f23fb68`  7h48m   79/101 read at six of seven sweeps
          `1692702-94e6163a`  3h36m   frontier 35 -> 57 -> 63 outgrew the
                                      cursor, no pass closed, `0 < 0`
          `1920662-40f711b2`  9h48m   the repeat clause was satisfied at iter 3
                                      (00:25:43Z, `83/106` twice) and the stop
                                      was withheld on `traversal 0`, which the
                                      run never left

        The cohort fix in `domain/dispatch_batch.py` was supposed to make
        traversals tick again and on that third run it did not: the cohort
        opened at iter 2 over a frontier of 43, iter 3 attempted 15 of them
        against a frontier that had shrunk to 16, and iter 4 left through a
        crash retrigger's `continue` — which by design commits no batch, so
        `_traversals` stayed 0. From iter 5 a re-fill grace window excused
        every sweep. Nine hours, 191 model calls, net -46 passing tests, and
        the guard's own ARMED-and-WITHHELD line printed once and then went
        quiet because the excuses stopped it being evaluated.

        So the rule is now the plain one: a reading the window has already seen
        `repeats` times stops the run. That is a WINDOW and not a consecutive
        streak, so `83 -> 79 -> 83` trips exactly as `83 -> 83` does — the
        recorded pathology is a fixed point, not a flat line.

        WHAT THE CLAUSE WAS PROTECTING, AND WHAT REPLACES IT. With a dispatch
        cap of 12 and a frontier of 59, stopping on the second repeat means the
        run stops having attempted 12 units, not 59. The clause held the stop
        back so the rest got their turn. What actually gives them their turn is
        cheaper and does not depend on a pass closing: `repeats` is 2 and every
        process starts with an EMPTY window, so a relaunch records one reading,
        dispatches ONE batch, and trips on the sweep after it — and that batch
        is a slice the previous process never reached, because
        `TddFloor.attempted` persists the pass cursor across the boundary.
        Progress through the frontier is therefore bounded below by one fresh
        slice per relaunch instead of by a traversal that may never complete,
        and the fills of the stopped run are all still on disk, which moves the
        frontier anyway. The loss is bounded and self-unlocking; the clause's
        failure mode was not.

        It is ONE batch and not two, which is the number an operator divides a
        frontier by. The reading is recorded at the sweep and this decision is
        taken before the dispatch, so the second sweep's recording is already
        the second occurrence — `test_the_gpu_guard_stops_the_capped_loop_on_
        the_second_repeat` asserts a batch count of exactly 1. Covering F units
        at cap C costs ceil(F/C) relaunches.

        `require_traversal=True` restores the old precondition for a caller
        that wants it (`TDD_FLAT_SCORE_REQUIRE_TRAVERSAL=1`). It is off by
        default because the three runs above are what "on" measured.

        `enabled` is checked HERE and not left to the arithmetic: with the guard
        turned off by `repeats=0`, an empty window's count of 0 satisfies
        `>= 0` and the disable switch would stop the run at its first
        dispatch."""
        if not self.enabled or self.repeated(reading) < self.repeats:
            return False
        if not self.require_traversal:
            return True
        first = self.first_traversal_of(reading)
        return first is not None and first < traversal

    def withheld_by_traversal(self, reading: ScoreReading,
                              traversal: int) -> bool:
        """True when the REPEAT clause is already satisfied and only the
        traversal clause is holding the stop back.

        This state is the whole of run `1692702-94e6163a`'s failure to
        terminate and it printed nothing at the time: the guard is silent when
        it does not trip, so "the terminator is armed but cannot charge" and
        "the loop is making progress" looked identical in the log. The driver
        reports it per sweep; the arithmetic lives here beside the rule it is
        the negative of.

        With `require_traversal` off — the default — nothing is ever withheld
        and this is vacuously False, because `trips_on` returns True on the
        same condition that gets us past the guard above. It stays as a method
        rather than being deleted so that turning the precondition back on
        restores its reporting with it: an operator who re-arms the clause gets
        the line that says the clause is what is holding the stop."""
        if not self.enabled or self.repeated(reading) < self.repeats:
            return False
        return not self.trips_on(reading, traversal)

    def render(self, reading: ScoreReading) -> str:
        """The window as a log line: oldest → newest, each reading stamped with
        the traversal it was taken in, plus the repeat count."""
        return (f"{' '.join(f'{o.reading}@t{o.traversal}' for o in self.readings)} "
                f"(reading {reading} {self.repeated(reading)}x in the last "
                f"{len(self.readings)} judged sweep(s))")


# --- stage 2 of the fold: the same rule, as a function of the fact stream ----

@dataclass(frozen=True, slots=True)
class FlatScoreDecision:
    """What the guard decides, and everything its log line needs to say why.

    A value rather than a mutation, which is the whole point of the conversion:
    `FlatScoreGuard.observed`/`excused` had to be called on exactly the right
    paths, in the right order, from a 3,708-line module — and the recorded cost
    of getting that wrong is three runs that ended on an operator's SIGTERM
    instead of the loop's own decision. A fold over the facts cannot be called
    on the wrong path, because there is no path: the readings ARE the judged
    sweeps in the stream."""

    stop: bool
    reading: ScoreReading
    repeats: int
    limit: int
    window: int
    gap: int
    traversals: int
    first_traversal: int | None
    withheld_by_traversal: bool
    observations: tuple[ScoreObservation, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.limit >= 2 and self.window >= self.limit

    def render(self) -> str:
        return (f"{' '.join(f'{o.reading}@t{o.traversal}' for o in self.observations)} "
                f"(reading {self.reading} {self.repeats}x in the last "
                f"{len(self.observations)} judged sweep(s))")


def flat_score(
    facts: Sequence[Fact], *, window: int = 5, limit: int = 2,
    require_traversal: bool = False,
) -> FlatScoreDecision | None:
    """STOP or continue, decided from the fact stream and nothing else.

    Returns None when no judged sweep has been recorded yet — there is no
    reading to be about, which is a different state from "the reading does not
    repeat" and must not be collapsed into it.

    `require_traversal` survives as a PARAMETER rather than as guard state, and
    only because `CLAUDE.md` documents `TDD_FLAT_SCORE_REQUIRE_TRAVERSAL=1` as
    a supported setting. The difference matters: as a field it was part of the
    guard's identity and a caller could observe a reading under one value and
    decide under another; as an argument it is a question the caller asks, and
    the answer is a pure function of the stream.

    THE NEGATIVE CONTROL THIS FUNCTION EXISTS FOR is run `1920662-40f711b2`
    (2026-08-16 → 08-17, 9h48m, 191 model calls, net -46 tests). Its iterations
    1-3 read 79/101, 83/106, 83/106 and no pass closed, so the live guard's
    repeat clause was satisfied at 00:25:43Z and the traversal clause withheld
    the stop: `83/106 has been read 2 time(s) (limit 2) but all of them inside
    traversal 0`. The fold over that same recorded stream, with
    `require_traversal` off, decides STOP at iteration 3."""
    obs: list[ScoreObservation] = []
    traversals = 0
    gap = 0
    pending = 0
    for f in facts:
        if isinstance(f, PassClosed):
            traversals += 1
        elif isinstance(f, SweepMeasured):
            if f.judged:
                obs.append(ScoreObservation(
                    ScoreReading(f.pass_count, f.oracle_size), traversals))
                gap, pending = pending, 0
            else:
                # A sweep that cannot judge the dispatch before it is not
                # evidence in either direction — it is a gap, and the stop line
                # reports it so an operator can tell "two identical readings in
                # a row" from "two identical readings with four excused sweeps
                # between them".
                pending += 1
    if not obs:
        return None
    kept = tuple(obs[-window:]) if window > 0 else tuple(obs)
    reading = kept[-1].reading
    repeats = sum(1 for o in kept if o.reading == reading)
    seen = [o.traversal for o in kept if o.reading == reading]
    first = min(seen) if seen else None
    enabled = limit >= 2 and window >= limit
    tripped = enabled and repeats >= limit
    withheld = bool(tripped and require_traversal
                    and (first is None or first >= traversals))
    return FlatScoreDecision(
        stop=tripped and not withheld, reading=reading, repeats=repeats,
        limit=limit, window=window, gap=gap, traversals=traversals,
        first_traversal=first, withheld_by_traversal=withheld,
        observations=kept)
