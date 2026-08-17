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
    # The OPEN pass's cursor: `"<file>::<fn>"` for every frontier unit this
    # pass has already dispatched. Empty when no pass is open.
    #
    # WHY IT IS PERSISTED, and it is the flat-score guard's safety net. That
    # guard stops the run on the second identical reading, which under a
    # dispatch cap of 12 can mean stopping having attempted 12 units of 59.
    # With a process-local cursor that stop is a LOCK rather than a pause:
    # `order_frontier` is deterministic, so the relaunch starts with an empty
    # attempted-set, sends the SAME first 12, reads the same score and stops
    # again — units 13-59 unreachable without a human editing the env, which
    # is the deadlock shape `domain/pass_highwater.py` documents. Carrying the
    # cursor across the boundary is what makes the guard's eagerness safe: each
    # relaunch takes the NEXT slice, and the frontier is covered across as many
    # short runs as it takes instead of burning one long one.
    #
    # Defaulted so a floor written by an older build parses (`from_dict` is
    # total by design and an absent cursor simply means "start the pass").
    attempted: tuple[str, ...] = ()
    # The futility evidence: per failing test, the frontier unit-set
    # (`"<file>::<fn>"`) the LAST sweep attributed to it. The escalation's
    # trigger is "this test's attributed units are UNCHANGED across two judged
    # sweeps while it kept failing" — held process-locally, that takes two
    # sweeps of THIS process, and the flat-score guard stops a resumed flat
    # run at exactly its second sweep, before the widened frontier's first
    # dispatch. Run `2773540-e47c3754` (2026-08-17): the escalation added
    # `construct`/`erase`/`chain::new`/`context.rs::source` at iter 2, the
    # guard stopped the run at the same sweep, and the additions died with
    # the process — so no relaunch could ever dispatch them, while the tree
    # held the severed `ContextError<C, Error>::source -> None` the
    # escalation existed to reach. Persisting the evidence lets a resume
    # detect the unchanged set at its FIRST sweep, so the escalated slice is
    # dispatched inside the one batch the guard's design grants each
    # relaunch.
    futile_units: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"cluster": self.cluster, "best_pass": self.best_pass,
                "stall": self.stall, "tree_sha": self.tree_sha,
                "oracle_size": self.oracle_size, "code_id": self.code_id,
                "attempted": list(self.attempted),
                "futile_units": {t: list(units)
                                 for t, units in self.futile_units}}

    @staticmethod
    def from_dict(raw: Any) -> TddFloor | None:
        """Parse a persisted floor, or None when it is absent or malformed.

        Total by design: this runs at loop entry, and a corrupt state file must
        cost a fresh start, never the run."""
        if not isinstance(raw, dict):
            return None
        try:
            _fut = raw.get("futile_units")
            return TddFloor(
                cluster=str(raw["cluster"]),
                best_pass=int(raw["best_pass"]),
                stall=int(raw["stall"]),
                tree_sha=str(raw["tree_sha"]),
                oracle_size=int(raw["oracle_size"]),
                code_id=str(raw["code_id"]),
                attempted=tuple(
                    str(k) for k in (raw.get("attempted") or ())),
                futile_units=tuple(sorted(
                    (str(t), tuple(str(u) for u in units))
                    for t, units in _fut.items()
                    if isinstance(units, list))
                    if isinstance(_fut, dict) else ()))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class FloorResume:
    """How much of a persisted floor this process may adopt.

    The halves of a floor are NOT one decision, and gating them together
    is what made Stage A inert on its first live day. See :func:`resume_floor`.
    ``None`` in a field means "start that half fresh"."""

    stall: int | None
    best_pass: int | None
    note: str
    # The open pass's cursor. ``None`` means "open a fresh pass"; an empty
    # tuple is a resumed pass that happened to have dispatched nothing.
    attempted: tuple[str, ...] | None = None
    # The futility evidence, resumed on the CURSOR's identity rule (cluster +
    # code_id, never the oracle or the tree): like the cursor it is a record
    # of what this cluster's dispatches already established, not a claim
    # about a score, and it self-validates at the live sweep — the escalation
    # trigger re-compares it against the CURRENT attribution, so evidence a
    # retrigger or restore has invalidated simply fails the equality and
    # escalates nothing. ``None`` means "no evidence to carry".
    futility: tuple[tuple[str, tuple[str, ...]], ...] | None = None


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


def cursor_mismatch(saved: TddFloor, *, cluster: str, code_id: str) -> str | None:
    """The first field that forbids resuming the pass CURSOR, or None.

    This is `resume_mismatch` minus ``oracle_size``, and the omission is the
    whole reason the function exists rather than being one call.

    A cursor is not a claim about a score. It is the list of frontier units this
    pass already spent a model call on, and the only thing that can invalidate
    it is the frontier being drawn from a different population: a different
    CLUSTER (different fns entirely) or a different CODE build (a different
    `order_frontier`, a different route set). ``oracle_size`` is neither. It is
    ``pass_count + fail_count`` — a reading of the sweep, which moves whenever a
    test binary's crash state flips, and one landed body is enough to move it:
    on run `1920662-40f711b2` filling `object_drop_front` took
    `tests/test_downcast.rs` from 0/7 to 4/3 and the oracle from 101 to 106 in
    one iteration, then a re-stub took it to 105.

    WHAT GATING THE CURSOR ON IT COST, stated because the cursor was added to
    prevent exactly this. The flat-score guard stops the run on the second
    identical reading, which under a dispatch cap of 12 can mean stopping having
    attempted 12 units of 59; the cursor is what makes that a pause rather than
    a lock, because the relaunch opens on units the stopped run never reached.
    Routed through `resume_mismatch`, the cursor was dropped on any oracle move
    — so the relaunch started empty, `order_frontier` is deterministic, and it
    re-sent the same first 12, read the same score and stopped again. Measured
    over the whole event store (`phase_c.tdd.iter`, 2026-08-17): **26 of 401
    consecutive sweep pairs change `oracle_size`**, so roughly one stop in
    fifteen landed in the lock the cursor exists to remove.

    A stale key cannot survive being carried: `take_batch` intersects the cursor
    with the LIVE frontier, so a unit that no longer exists is dropped and a
    unit that still does is one the previous process demonstrably already tried.
    That intersection is the same argument that lets the cursor ignore
    ``tree_sha``, applied to the one other field that describes the tree rather
    than the experiment.
    """
    if saved.cluster != cluster:
        return f"cluster ({saved.cluster} != {cluster})"
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
        # THE SCORE HALVES DIE HERE AND THE CURSOR DOES NOT, because they are
        # answerable to different identities — see `cursor_mismatch`, which is
        # this check minus `oracle_size`. Both halves used to leave through this
        # return, which voided the cursor on the one transition it was written
        # for: a stop, a restore or a crash flip moves the oracle, and the
        # relaunch then re-dispatched the deterministic prefix it had just been
        # stopped on.
        _carry = cursor_mismatch(saved, cluster=cluster, code_id=code_id) is None
        _cursor = saved.attempted if _carry else None
        # The futility evidence rides the cursor's identity rule for the
        # cursor's own reason: both record what this cluster's dispatches
        # already established, and both self-validate against the live state
        # (the cursor by frontier intersection, the evidence by the
        # escalation's own equality re-check).
        _futility = saved.futile_units if _carry else None
        return FloorResume(
            stall=None, best_pass=None, attempted=_cursor,
            futility=_futility,
            note=why + " changed"
            + (f"; pass cursor resumed at {len(_cursor)} unit(s) anyway — a "
               "cursor answers to cluster+code_id, not to the oracle"
               if _cursor else "")
            + (f"; futility evidence resumed for {len(_futility)} test(s)"
               if _futility else ""))
    # The CURSOR resumes on the identity check alone, deliberately not on
    # `tree_sha`. It is not a claim about a score — it is the list of units
    # this pass already spent a model call on, and its safety condition is
    # only that the frontier is drawn from the same cluster by the same code.
    # A changed tree cannot invalidate it: `take_batch` intersects the cursor
    # with the LIVE frontier, so a unit that no longer exists is dropped and a
    # unit that still does is one the previous process already tried. Gating
    # it on `tree_sha` would void it in exactly the case it is for — the
    # flat-score stop, whose whole premise is that the tree changed and the
    # score did not.
    if saved.tree_sha != tree_sha:
        return FloorResume(
            stall=saved.stall, best_pass=None, attempted=saved.attempted,
            futility=saved.futile_units or None,
            note=(f"stall={saved.stall} resumed; best_pass={saved.best_pass} "
                  f"NOT resumed — tree_sha ({saved.tree_sha} != {tree_sha}) "
                  "changed, so nothing here can restore that peak"
                  + (f"; pass cursor resumed at {len(saved.attempted)} "
                     "unit(s)" if saved.attempted else "")
                  + (f"; futility evidence resumed for "
                     f"{len(saved.futile_units)} test(s)"
                     if saved.futile_units else "")))
    return FloorResume(
        stall=saved.stall, best_pass=saved.best_pass,
        attempted=saved.attempted,
        futility=saved.futile_units or None,
        note=(f"best_pass={saved.best_pass} stall={saved.stall}"
              + (f" pass cursor={len(saved.attempted)} unit(s)"
                 if saved.attempted else "")
              + (f" futility evidence={len(saved.futile_units)} test(s)"
                 if saved.futile_units else "")))
