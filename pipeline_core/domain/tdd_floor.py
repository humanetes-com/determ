"""The TDD loop's floor, made durable across process restarts (pure domain).

The loop exits — and only then does its cluster get checkpointed as done —
after ``max_stalls`` consecutive non-improving sweeps. Every part of that
streak lived in process memory: ``LoopState()``, ``stall = 0``,
``best_pass = -1`` were reconstructed at each start. An iteration on `anyhow`
costs 25-224 min and the process died roughly every 5-7 h, so three
non-improving sweeps had to fit inside one lifetime and never did.

Measured consequence: `nightly` scored exactly ``pass=93 fail=30 oracle=123``
in three separate processes across five sweeps (07-30 20:09, 20:35, 21:02,
07-31 02:31, 06:15) and its stall streak never exceeded ``1/3``, because a
death sat in the gaps. `task2a/anyhow/nightly` could not be marked done and the
run could not reach `task2b` — no matter how good the fills were.

Two further defects fall out of the same three lines and are fixed here:

* ``best_pass = -1`` made iter 1 of EVERY process report ``improved=True,
  stall=0`` even when the tree was byte-identical to the one the previous
  process had just measured. Both 21:02 and 02:31 did exactly that, and
  `run_health`, `ab_efficacy` and the C3/C4 flow audits all read it.
* A kill landing after a REGRESSING fill let the next process adopt the
  regressed count as its floor, leaving the batch ratchet nothing to restore
  the peak from.

RESUMING IS CONDITIONAL, and on all three of:

* ``tree_sha`` — a changed tree is a different measurement.
* ``oracle_size`` — a `--clean` restart re-runs task0c augmentation and the
  oracle changes IDENTITY (111 -> 115 -> 124 -> 122 on this crate, ~1/3 of the
  test names differing). A streak carried across one would be meaningless.
* ``code_id`` — restarts here exist mainly to pick up a code fix. Resuming
  ``stall=2`` across a restart that shipped a fill improvement would exit the
  loop after one sweep and checkpoint the cluster at the very plateau the fix
  was meant to break: the precise opposite of the intent.

Any mismatch starts fresh and says which field differed. A floor that resumes
when it should not is worse than one that never resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UNSET_PASS = -1
"""No sweep has scored this cluster in this process yet.

It is a sentinel a real value always beats, which is what makes the comparison
`pass_count > best_pass` correct on the first sweep without a special case. It
is also, in exactly the same breath, a lie to anyone READING it: `-1` renders
as a pass count, and this codebase has three separate defects on record that
began with a sentinel of this shape being read as a measurement — the floor
that made iter 1 of every process report `improved=True`, the batch ratchet's
high-water, and a cfg gate whose "could not read" and "nothing there" were the
same empty string.

Keep the sentinel for the arithmetic; never let it reach a human unlabelled.
:func:`render_pass_floor` is how it is spelled in a log line.
"""


def render_pass_floor(best_pass: int) -> str:
    """A pass floor as a reader should see it: a count, or ``unset``."""
    return "unset" if best_pass <= UNSET_PASS else str(best_pass)


@dataclass(frozen=True, slots=True)
class TddFloor:
    """A cluster's loop floor as persisted between processes."""

    cluster: str
    best_pass: int
    stall: int
    tree_sha: str
    oracle_size: int
    code_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"cluster": self.cluster, "best_pass": self.best_pass,
                "stall": self.stall, "tree_sha": self.tree_sha,
                "oracle_size": self.oracle_size, "code_id": self.code_id}

    @staticmethod
    def from_dict(raw: Any) -> TddFloor | None:
        """Parse a persisted floor, or None when it is absent or malformed.

        Total by design: this runs at loop entry, and a corrupt state file must
        cost a fresh start, never the run."""
        if not isinstance(raw, dict):
            return None
        try:
            return TddFloor(
                cluster=str(raw["cluster"]),
                best_pass=int(raw["best_pass"]),
                stall=int(raw["stall"]),
                tree_sha=str(raw["tree_sha"]),
                oracle_size=int(raw["oracle_size"]),
                code_id=str(raw["code_id"]))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class FloorResume:
    """How much of a persisted floor this process may adopt.

    The two halves of a floor are NOT one decision, and gating them together
    is what made Stage A inert on its first live day. See :func:`resume_floor`.
    ``None`` in either field means "start that half fresh"."""

    stall: int | None
    best_pass: int | None
    note: str


def resume_mismatch(
    saved: TddFloor, *, cluster: str, oracle_size: int, code_id: str,
) -> str | None:
    """The FIRST field that forbids resuming ``saved`` AT ALL, or None.

    These three are the floor's IDENTITY: they decide whether the saved
    numbers describe the same experiment this process is running. `tree_sha`
    is deliberately not among them — it bounds only the `best_pass` half, for
    a reason that is about restorability rather than identity
    (:func:`resume_floor`).

    Returning the field name rather than a bool is what makes the log line
    say why a streak was dropped — "start fresh" and "start fresh because the
    oracle changed size" send a reader to different places.
    """
    if saved.cluster != cluster:
        return f"cluster ({saved.cluster} != {cluster})"
    if saved.oracle_size != oracle_size:
        return f"oracle_size ({saved.oracle_size} != {oracle_size})"
    if saved.code_id != code_id:
        return f"code_id ({saved.code_id} != {code_id})"
    return None


def resume_floor(
    saved: TddFloor | None, *, cluster: str, tree_sha: str, oracle_size: int,
    code_id: str,
) -> FloorResume:
    """Which halves of ``saved`` this process may adopt, and why not the rest.

    Gating BOTH halves on ``tree_sha`` made the mechanism inert on its first
    live day, because a floor is written straight after a sweep and the loop
    then spends 25-224 min filling — and the process dies in the fill phase,
    which is where essentially all the wall clock is. Live: run `589477`
    refused on ``tree_sha (68f939adfae51660 != 6665a82428c800c3)`` after six
    minutes of filling. The gate therefore permitted a resume only when the
    previous fill phase produced NO net tree change — the case where the
    streak matters least — and refused it whenever fills landed and the score
    did not move, which is the genuine stall it exists to count.

    The halves are split because their safety conditions differ:

    * ``stall`` — the streak is a property of the CLUSTER's score history, not
      of one tree. Carrying it across a changed tree is safe in both
      directions: a tree that scores better resets the streak to 0 at the very
      next sweep, and one that scores no better IS a stall. This is the half
      that fixes the measured symptom — `nightly` scored exactly
      ``pass=93 fail=30 oracle=123`` in three processes across five sweeps and
      never exceeded ``1/3``, so task2a could not be marked done.
    * ``best_pass`` — a high-water mark is only safe to resume if the peak is
      still REACHABLE. The batch ratchet's snapshot (`_bratchet_snap`) is
      in-memory and dies with its process, and the durable `checkpoint_fill_
      tree` is gated on a full oracle, which `nightly` never has. Adopting a
      peak measured on a tree this process does not have would pin the loop
      under a ceiling nothing can restore: three stalls, then the cluster
      checkpointed as done at a regressed tree while claiming a floor it never
      re-achieved. So this half keeps the ``tree_sha`` condition.

    Restoring the peak across a death — rather than only declining to claim it
    — needs a durable ratchet snapshot, which is a separate piece of work.
    """
    if saved is None:
        return FloorResume(stall=None, best_pass=None,
                           note="no persisted floor")
    why = resume_mismatch(saved, cluster=cluster, oracle_size=oracle_size,
                          code_id=code_id)
    if why is not None:
        return FloorResume(stall=None, best_pass=None, note=why + " changed")
    if saved.tree_sha != tree_sha:
        return FloorResume(
            stall=saved.stall, best_pass=None,
            note=(f"stall={saved.stall} resumed; best_pass={saved.best_pass} "
                  f"NOT resumed — tree_sha ({saved.tree_sha} != {tree_sha}) "
                  "changed, so nothing here can restore that peak"))
    return FloorResume(
        stall=saved.stall, best_pass=saved.best_pass,
        note=f"best_pass={saved.best_pass} stall={saved.stall}")
