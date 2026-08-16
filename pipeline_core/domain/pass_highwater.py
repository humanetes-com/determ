"""The best pass count a cluster's CURRENT tree lineage has scored.

A plain monotone maximum over sweeps is the wrong shape for this loop, because
not every sweep measures the same tree. The layout/crash retrigger re-authors a
component and reverts every fn that references it to `unimplemented!()` — on
`anyhow` cluster `stable`, run `259637-36db9aad` (2026-08-07), that was **56 of
178 filled bodies**, and the next sweep read 21 where the one before it read 81.
The 81 is not a floor the tree fell below. It is a fact about a tree the
pipeline itself deleted.

Left standing, that stale high-water is read by four consumers and is wrong for
all of them:

* ``improved`` — false on every sweep of the re-climb, so the loop reads as
  plateaued for as long as the re-climb takes;
* the persisted ``TddFloor`` — a process that dies mid-window resumes believing
  the tree owes it 60 tests;
* the plateau exit's report — `best=81` on a tree that peaked at 26;
* ``readmission_warranted`` — and this one is not a mis-report but a DEADLOCK.
  It releases the disk-persisted `fully_exhausted` convictions only on
  ``best_metric > metric_at_last_reset``. With the baseline stuck at 81 the loop
  printed `109 fill(s) since the last check moved pass no further than 81 — NOT
  re-admitting exhausted fn(s)` on every sweep from iteration 4 to its death,
  while the two units the ~60 missing tests run through
  (`src/error.rs::*impl*::Error`, `src/error.rs::new::Error`) sat convicted.
  **Passing 81 needed those two; releasing those two needed passing 81.**

The batch ratchet withholds a breach inside the re-fill window
(`tdd_ratchet.breach_withheld`), the stall budget stands down on
`RefillGrace.bodies_in_flight`, and the stub-progress check exempts a sweep that
shrank the stub set. Those three are the consumers somebody thought of. This
type exists so the fourth cannot be forgotten again: the void is recorded on the
high-water ITSELF, so every reader of it is corrected at once instead of each
gate growing its own exemption.

**Two neighbouring figures are deliberately NOT touched, and both are supposed
to disagree with this one during a re-climb.** The batch ratchet's own best is
tied to the src-tree snapshot it would restore, so re-basing it would silently
retarget the restore; leaving it means an expiring window still reverts to
exactly the tree a one-iter window would have, which is the whole safety
argument for sizing the window generously. And the durable best TREE
(`.best_tree/<cluster>` and its `BestTreeMark`) holds source text a heal copies
back, so the 81-passing copy stays the best copy however far the live tree has
fallen. This type answers "how good is the tree the loop is working on"; those
two answer "what would we put back".

Termination is unaffected. A re-base follows one of exactly two events, and both
are bounded: a retrigger fire, which is capped per component over a finite
component set, and the TDD driver's durable best-tree restore, which is spent
once per process. So the number of re-bases in a run is bounded; and once the
re-climb genuinely flattens, ``improved`` goes false against the RE-BASED figure
and the plateau exit fires on schedule — against a number that describes the
tree it is exiting on.
"""
from __future__ import annotations

from dataclasses import dataclass

# The same sentinel the persisted floor uses, imported rather than restated: the
# two numbers are compared against each other on every resume, and a second
# definition is a second thing to keep in step.
from .tdd_floor import UNSET_PASS


@dataclass(frozen=True, slots=True)
class PassHighWater:
    """`best` and, when a deliberate re-stub has voided it, the iteration that
    did so. `voided_at` is the ITERATION rather than a bare flag because the
    grant and the re-base happen in different sweeps of the same loop: a
    retrigger fires below the iteration's read sites, so the first measurement
    of the tree it made is the NEXT iteration's, and re-basing on the granting
    iteration's own (pre-re-stub) score would restore exactly the stale number
    this type exists to retire."""

    best: int = UNSET_PASS
    voided_at: int | None = None

    def observed(self, pass_count: int) -> PassHighWater:
        """The high-water after a sweep scored `pass_count` — the plain
        monotone step, used when nothing voided the lineage."""
        return self if pass_count <= self.best else PassHighWater(
            best=pass_count, voided_at=self.voided_at)

    def lineage_replaced(self, iteration: int) -> PassHighWater:
        """Mark `best` as describing a tree the pipeline has just replaced.

        TWO producers, and the name says neither of them specifically on
        purpose. The first is the retrigger's re-stub, and it is called only
        where bodies actually went back to `unimplemented!()`: a fired-but-
        failed attempt re-stubbed nothing and voids nothing, so its sweep
        measures the same tree as the last one and a re-base there would hand
        the loop a free reset of a floor it has not lost. The second is the
        durable best-tree restore in the TDD driver, which overwrites every body
        and every layout file from `.best_tree/<cluster>`; it moves the score UP
        rather than down, and it is still a different tree, so `best` still
        stops describing what the loop is working on. The method was called
        `voided_by_restub` while only the first existed, and the restore shipped
        without calling it at all — on the argument that a higher score reads as
        an improvement anyway, which holds against the sweep it replaces and not
        against a monotone `best_pass`."""
        return PassHighWater(best=self.best, voided_at=iteration)

    def rebase_due(self, iteration: int) -> bool:
        """Whether `iteration`'s measurement is the first one taken after the
        re-stub, and therefore the new lineage's baseline."""
        return self.voided_at is not None and iteration > self.voided_at

    def rebased(self, pass_count: int) -> PassHighWater:
        """The high-water restated as `pass_count`, void cleared.

        Unconditionally DOWN when the re-stub cost tests, which is the point.
        The caller must not apply this to a sweep that measured nothing — a
        tree that fails to compile reports 0 because nothing ran, and adopting
        that as the lineage's baseline would make the next broken build read as
        a 0-to-0 plateau on a cluster that is merely un-buildable."""
        return PassHighWater(best=pass_count, voided_at=None)
