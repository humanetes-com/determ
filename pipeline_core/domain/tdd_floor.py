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
    # The subset of `attempted` on which a SEMANTIC change actually landed —
    # the unit's on-disk body differed after the batch that dispatched it.
    # The cursor alone marks ATTEMPTED at dispatch, and a completed pass read
    # off it silently claims every unit was TRIED; the pass that closed the
    # 2026-08-19 crawl says otherwise, batch by batch: byte-identical
    # re-authors (`src/error.rs::source` — a traversal step — plus three
    # `construct_from_*` producers), compile-failed peels that left the old
    # body in place (`src/context.rs::source`, the severed `None`, peeled
    # with its whole file), and the immutable test-oracle producers skipped
    # by rule every pass. A flat sweep after such a pass refutes the
    # escalation theory ONLY for the landed subset; without this record the
    # "genuine plateau" verdict is pronounced over a denominator roughly
    # half of which was never re-tried. Persisted with the cursor, under the
    # cursor's identity rule, so the one-batch-per-relaunch crawl accumulates
    # it across processes.
    landed: tuple[str, ...] = ()
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
    #
    # ITS PREMISE IS THE ESCALATION'S OWN LIFETIME RULE, and nothing else.
    # The evidence describes the TREE ("rewriting these accessors moved no
    # verdict"), not the pipeline build, so it is carried across a `code_id`
    # move on the cursor's cluster identity alone and re-checked where the
    # in-process product is re-checked every sweep
    # (`domain/escalation_lifetime`): the graded test still fails, its LIVE
    # attribution equals the seeded set, and no seeded accessor is a stub.
    # A seeded set the new attributor does not reproduce escalates nothing;
    # a re-stubbed accessor is withheld as the target; a passing test has no
    # attribution to compare. Those three checks run at the first sweep, on
    # live state, and are the whole premise.
    #
    # A STRICTER cross-process premise was tried and retired: a content
    # fingerprint per FILE holding a convicted unit, stamped at the sweep
    # boundary and required to match at the next entry. It never held once.
    # The escalated producers live in the same file as the accessors they
    # replace (`src/error.rs` holds `downcast`/`is`/`from` AND the
    # `context_*`/`object_*` chain fns), so the escalated dispatch the
    # evidence exists to reach splices that file after the stamp, and the
    # next entry finds a mismatch by construction. Measured on the retained
    # logs, 2026-08-23 → 08-25: seven consecutive `code_id` relaunches, zero
    # `futility evidence resumed` lines, each paying the 7-accessor entry
    # traversal again — 2h03m, 2h18m and 4h02m in the three runs of one
    # night (`189964-ae3c1379`, `264297-dd681686`, `349271-d876e4a9`), with
    # the stamp `fa21c7c5a5307ad5` equal to the iter-2 checkpoint's blob and
    # the live file `69fc2b5a738cb95b`. Worse, a process that declined the
    # evidence saved its own empty `prev_test_units` at its first boundary
    # and erased the seed for every later relaunch (run `4911-9ab8852e`,
    # `futile_units: {}` on disk one sweep in). Body granularity would not
    # have fixed the class: the same escalated batch legitimately re-fills
    # accessors too (`from` in the iter-2 batch), and in-process a further
    # accessor rewrite does not end the product either. A premise the loop's
    # own success path violates is not a premise.
    futile_units: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # The subset of `attempted` that was told NOT to change its body — the
    # INFERRED units (escalation producers, traversal steps) a `[REFILL?]`
    # prompt asks to return unchanged when they are already right. They land
    # nothing BY INSTRUCTION, so unlike the rest of the unchanged set they are
    # not failed tries, and a code change to the fill or route path gives no
    # reason to re-offer them. Persisted for the same reason `landed` is: the
    # carry rule below distinguishes "tried" from "produced nothing", and the
    # record of which is which is per-batch and would otherwise die with the
    # process that made it.
    declined: tuple[str, ...] = ()
    # Every unit this cluster has EVER dispatched as a batch target, across
    # passes and processes — the service record the futility escalation's
    # premise ("this test's accessors were served by rewrites that changed
    # nothing") is checked against. `attempted` cannot play that role: it is
    # the OPEN pass's cursor and empties when the pass closes. Without a
    # durable record a resumed process could only take the persisted
    # `futile_units` on faith, and on 2026-08-25 that faith was misplaced:
    # run `263474-b0e608d3` persisted `test_altdebug: [src/fmt.rs::debug]`
    # from a fire the pre-service rule manufactured on a unit it had never
    # dispatched (parked in batch 3 under the cap), and the next process would
    # have re-fired it at its first sweep. Monotone; a floor written before
    # this field parses as ``None`` and the resume falls back to the cursor's
    # `attempted ∪ landed ∪ declined` for that one transition. An empty
    # tuple is not missing: it is a floor that recorded "nothing has been
    # fill-returned yet" (a kill mid-fill after the dispatch persist), and
    # collapsing it into the fallback would re-count the attempted units as
    # served — the kill-mid-fill hole, run `308884` → `2932`.
    served: tuple[str, ...] | None = None
    # The subset of `served` whose stamps were earned under the route-scoped
    # rule — `_served_at` keyed on the units a route actually opened
    # (`dispatched_fns ∩ frontier`), not on the whole batch. `served` itself
    # is contaminated for the life of the floor: before 2026-08-31 the loop
    # stamped every batch unit served (run `9933-efe68f37` iter 1: 12
    # stamped, `wanted` was `{context, msg}`), the stamps are monotone, and
    # no later record can distinguish them — the ledger's rows from the same
    # window grade the same lie. So the two consumers split: the futility
    # fire keeps the INCLUSIVE set (over-stamping there only lets a fire
    # happen; the trigger re-validates against the live attribution), while
    # the never-served RANKING reads this verified subset — a stamp it
    # cannot verify is not evidence the unit was ever tried. A floor from
    # before the field parses as ``None``: the ranking then starts with
    # nothing verified and re-earns stamps as routes actually open units,
    # which converges to the honest index instead of trusting the lie.
    # Measured cost of trusting it: `never_served=[]` on 24 of 25 rankings
    # across the five runs after the fix (entries computed 2026-09-01).
    served_verified: tuple[str, ...] | None = None
    # The escalation PRODUCT: per still-failing test, the producer set the
    # fire computed, keyed by TEST (not by the accessor set). The trigger
    # (`futile_units`) re-derives producers by re-firing on exact accessor-set
    # equality; an attribution shift then spends iter 1 on the new accessors.
    # The product survives that shift. Resume re-checks with the same
    # judged-service premise as the fire (`served`): a kill that never
    # returned a rewrite is not service, and a product whose earned units
    # are not in `served` is not seeded. Defaulted so a floor written
    # before the field parses.
    escalation_products: tuple[tuple[str, tuple[str, ...],
                                     tuple[tuple[str, tuple[str, ...]], ...],
                                     tuple[str, ...]], ...] = ()
    # The dispatch rank's unclaimed streak (`dispatch_rank.next_streak`): per
    # frontier unit, the sweeps since a run last claimed it. The rank's first
    # key in every class, so a resume that dropped it would rank the units
    # the last process kept cutting as if they had just been served. It
    # describes this cluster's dispatches, so it rides the service record's
    # identity rule (the cluster), not the tree or the code. Defaulted so a
    # floor written before the field parses as "no streak".
    unclaimed: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "cluster": self.cluster, "best_pass": self.best_pass,
            "stall": self.stall, "tree_sha": self.tree_sha,
            "oracle_size": self.oracle_size, "code_id": self.code_id,
            "attempted": list(self.attempted),
            "landed": list(self.landed),
            "declined": list(self.declined),
            "futile_units": {t: list(units)
                             for t, units in self.futile_units},
            "escalation_products": {
                t: {"earned": list(earned),
                    "additions": {k: list(ts) for k, ts in adds},
                    "producers": list(prods)}
                for t, earned, adds, prods in self.escalation_products}}
        if self.unclaimed:
            d["unclaimed"] = dict(self.unclaimed)
        if self.served is not None:
            d["served"] = list(self.served)
        if self.served_verified is not None:
            d["served_verified"] = list(self.served_verified)
        return d

    @staticmethod
    def from_dict(raw: Any) -> TddFloor | None:
        """Parse a persisted floor, or None when it is absent or malformed.

        Total by design: this runs at loop entry, and a corrupt state file must
        cost a fresh start, never the run."""
        if not isinstance(raw, dict):
            return None
        try:
            # A floor written by the build that stamped `futile_files`
            # fingerprints still parses: the key is simply not read.
            _fut = raw.get("futile_units")
            _prod = raw.get("escalation_products")
            return TddFloor(
                cluster=str(raw["cluster"]),
                best_pass=int(raw["best_pass"]),
                stall=int(raw["stall"]),
                tree_sha=str(raw["tree_sha"]),
                oracle_size=int(raw["oracle_size"]),
                code_id=str(raw["code_id"]),
                attempted=tuple(
                    str(k) for k in (raw.get("attempted") or ())),
                landed=tuple(
                    str(k) for k in (raw.get("landed") or ())),
                declined=tuple(
                    str(k) for k in (raw.get("declined") or ())),
                served=(None if "served" not in raw else tuple(
                    str(k) for k in (raw.get("served") or ()))),
                served_verified=(
                    None if "served_verified" not in raw else tuple(
                        str(k) for k in (raw.get("served_verified") or ()))),
                futile_units=tuple(sorted(
                    (str(t), tuple(str(u) for u in units))
                    for t, units in _fut.items()
                    if isinstance(units, list))
                    if isinstance(_fut, dict) else ()),
                escalation_products=_parse_products(_prod),
                unclaimed=_parse_unclaimed(raw.get("unclaimed")))
        except (KeyError, TypeError, ValueError):
            return None


def _parse_unclaimed(raw: Any) -> tuple[tuple[str, int], ...]:
    """The persisted streak, or empty when absent or malformed: a bad entry
    costs that unit its streak, never the resume."""
    if not isinstance(raw, dict):
        return ()
    out: list[tuple[str, int]] = []
    for k, n in raw.items():
        try:
            out.append((str(k), max(0, int(n))))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(out))


def _parse_products(raw: Any) -> tuple[tuple[str, tuple[str, ...],
        tuple[tuple[str, tuple[str, ...]], ...], tuple[str, ...]], ...]:
    """A floor written before the field, or a malformed value, is empty."""
    if not isinstance(raw, dict):
        return ()
    out: list[tuple[str, tuple[str, ...],
                    tuple[tuple[str, tuple[str, ...]], ...],
                    tuple[str, ...]]] = []
    for t, body in raw.items():
        if not isinstance(body, dict):
            continue
        earned = body.get("earned") or ()
        adds = body.get("additions") or {}
        prods = body.get("producers") or ()
        if not isinstance(adds, dict):
            continue
        out.append((
            str(t),
            tuple(str(u) for u in earned),
            tuple(sorted((str(k), tuple(str(x) for x in ts))
                         for k, ts in adds.items()
                         if isinstance(ts, (list, tuple)))),
            tuple(str(u) for u in prods),
        ))
    return tuple(sorted(out))


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
    # The cursor's landed-change record: which of `attempted` actually took a
    # semantic body change. Rides the cursor — same identity rule, same
    # lifetime — so a pass spread across relaunches can state its effective
    # coverage when it closes. ``None`` exactly when `attempted` is None.
    landed: tuple[str, ...] | None = None
    # The cursor's declined record — INFERRED units instructed to change
    # nothing. Rides the cursor exactly as `landed` does, and for the same
    # reason: `cursor_carry` needs both to tell "tried" from "produced
    # nothing" at the NEXT relaunch, not just at this one.
    declined: tuple[str, ...] | None = None
    # Units dropped from the carried cursor because the code identity moved
    # and their last attempt landed nothing — released for an immediate retry
    # rather than deferred to the next pass. Reported, never acted on: they
    # are simply absent from `attempted`, so the frontier offers them again.
    released: tuple[str, ...] = ()
    # The futility evidence: a record of what this cluster's dispatches
    # already established, resumed on the cursor's cluster identity alone —
    # not on the code identity, not on a fingerprint of the tree. It
    # self-validates at the live sweep: the escalation trigger re-compares it
    # against the CURRENT attribution and withholds on a stubbed accessor, so
    # evidence a retrigger, a restore or a changed attributor has invalidated
    # simply fails those checks and escalates nothing. ``None`` means "no
    # evidence to carry".
    futility: tuple[tuple[str, tuple[str, ...]], ...] | None = None
    # The cluster's service record (`TddFloor.served`), resumed on the
    # cluster identity exactly as the futility evidence is — a dispatch
    # happened whatever the code was. ``None`` means the saved floor predates
    # the field (or there is no floor): the loop then seeds service from the
    # cursor's own `attempted ∪ landed ∪ declined`, which is what the old
    # floor can honestly attest.
    served: tuple[str, ...] | None = None
    # The verified subset (`TddFloor.served_verified`), riding the same
    # identity rule. ``None`` means the saved floor predates the field: the
    # ranking's never-served reading then starts empty and re-earns stamps —
    # it does NOT fall back to `served`, whose pre-field stamps are the
    # batch-wide contamination the split exists to quarantine.
    served_verified: tuple[str, ...] | None = None
    # The escalation product (`TddFloor.escalation_products`), resumed on
    # the cluster identity like the trigger. ``None`` means the saved floor
    # predates the field (or there is no floor). The loop seeds only those
    # whose earned units are in `served` — judged service, not a kill.
    products: tuple[tuple[str, tuple[str, ...],
                          tuple[tuple[str, tuple[str, ...]], ...],
                          tuple[str, ...]], ...] | None = None
    # The dispatch rank's unclaimed streak (`TddFloor.unclaimed`), resumed
    # on the cluster identity as the service record is.
    unclaimed: tuple[tuple[str, int], ...] = ()


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


def cursor_mismatch(saved: TddFloor, *, cluster: str) -> str | None:
    """The one field that forbids resuming the pass CURSOR at all, or None.

    This is `resume_mismatch` minus ``oracle_size`` AND minus ``code_id``, and
    both omissions are the whole reason the function exists rather than being
    one call.

    A cursor is not a claim about a score. It is the list of frontier units this
    pass already spent a model call on, and the only population that can make
    those units mean nothing is a different CLUSTER — different fns entirely.
    ``oracle_size`` is not that. It is
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

    A stale key cannot survive being carried: `dispatch_rank.take` intersects the cursor
    with the LIVE frontier, so a unit that no longer exists is dropped and a
    unit that still does is one the previous process demonstrably already tried.
    That intersection is the same argument that lets the cursor ignore
    ``tree_sha``, applied to the one other field that describes the tree rather
    than the experiment.

    WHY ``code_id`` LEFT TOO, and why it is not replaced by a narrower hash.
    `code_identity` covers pipeline.py plus every `.py` under `pipeline_core`,
    so a docstring edit voided the cursor as surely as rewriting
    `order_frontier` did: sixteen code identities over 2026-08-17 -> 08-20, each
    restarting a 43-unit crawl at 0, in a week whose whole purpose was the one
    reading a completed pass produces. The obvious repair — hash only the
    modules that determine the frontier's population and order — was MEASURED
    before it was written (`scripts/cursor_identity_replay.py`, 41 code_id
    transitions on `stable`): it would have preserved the cursor on 4 of 41
    (10 %), carrying **zero** units, because the modules any honest such hash
    must contain (`phase_c_tdd.py` 12 drops, `retro_gate.py` 4, `retros.py`,
    `dispatch_batch.py`, `impl_routing.py`, `scheduling.py`, `dispatch.py`) are
    precisely the modules a fix session edits. Dropping `code_id` outright
    preserves all 41 and carries 300 units.

    What `code_id` was really protecting is not the cursor's VALIDITY but a
    retry: a change to the fill or route path can make a previously failed
    attempt worth repeating, and a carried cursor defers that repeat to the
    next pass. That is a lifetime question, not an identity one, and
    :func:`cursor_carry` answers it per unit instead of throwing the traversal
    away.
    """
    if saved.cluster != cluster:
        return f"cluster ({saved.cluster} != {cluster})"
    return None


@dataclass(frozen=True, slots=True)
class CursorCarry:
    """How much of a persisted cursor this process adopts, and what it released.

    Empty tuples with ``released == ()`` and ``carried_all`` False is the
    cluster mismatch: nothing to adopt. Callers distinguish "no cursor" from
    "an empty cursor" through :class:`FloorResume`, which is where ``None``
    still means "open a fresh pass"."""

    attempted: tuple[str, ...] = ()
    landed: tuple[str, ...] = ()
    declined: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    carried_all: bool = True

    @property
    def note(self) -> str:
        if not self.released:
            return f"pass cursor={len(self.attempted)} unit(s)"
        return (f"pass cursor={len(self.attempted)} unit(s) "
                f"({len(self.released)} released for retry — the code moved "
                "and their last attempt landed nothing)")


def cursor_carry(saved: TddFloor, *, cluster: str, code_id: str) -> CursorCarry:
    """The cursor this process adopts from ``saved``: all of it under the same
    code, and under a MOVED code identity only the units whose last attempt
    actually produced something.

    The three populations a pass's `attempted` set is made of, and what each
    one deserves when the code has changed under it:

    * ``landed`` — the unit's on-disk body took a semantic change. It was
      genuinely tried; re-dispatching it inside the same pass buys another
      lottery ticket on a unit that already answered. CARRIED.
    * ``declined`` — an INFERRED unit told to return its body unchanged
      (`[REFILL?]`). Landing nothing was the instructed outcome, not a failure,
      so a fill-path fix gives no reason to re-offer it. CARRIED.
    * everything else — dispatched, and nothing came back: a routing drop
      (`impl<T, E> Context<T, E> for Result<T, E)` lost twice in one run), a
      splice refusal, a compile-failed peel that left the old body standing, a
      byte-identical re-author. These are exactly the attempts a change to the
      fill, route or prompt path can turn into an answer. RELEASED, so the
      resumed pass offers them again instead of deferring them by up to a
      traversal.

    So the identity question ("is this cursor about the same experiment") and
    the lifetime question ("is this attempt still predictive") stop being one
    decision. Measured over the record: carrying everything saves 300 unit
    dispatches across 41 transitions but can defer a retry by up to one pass
    (p50 2 iterations, p90 5); carrying nothing — today's rule — defers no
    retry and re-spends all 300. The split carries about two thirds of them
    (58 of 240 dispatched units landed and 97 more were declined, over the 22
    batches the landed-change audit has recorded) while releasing every unit a
    code fix could plausibly help.
    """
    if cursor_mismatch(saved, cluster=cluster) is not None:
        return CursorCarry(carried_all=False)
    if saved.code_id == code_id:
        return CursorCarry(attempted=saved.attempted, landed=saved.landed,
                           declined=saved.declined)
    keep = set(saved.landed) | set(saved.declined)
    carried = tuple(k for k in saved.attempted if k in keep)
    released = tuple(k for k in saved.attempted if k not in keep)
    return CursorCarry(
        attempted=carried,
        landed=tuple(k for k in saved.landed if k in carried),
        declined=tuple(k for k in saved.declined if k in carried),
        released=released, carried_all=False)


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
        _ok = cursor_mismatch(saved, cluster=cluster) is None
        _carry = cursor_carry(saved, cluster=cluster, code_id=code_id)
        _cursor = _carry.attempted if _ok else None
        # The futility evidence rides the cursor's identity rule for the
        # cursor's own reason: both record what this cluster's dispatches
        # already established, and both self-validate against the live state
        # (the cursor by frontier intersection, the evidence by the
        # escalation's own equality and stub checks at the first sweep).
        #
        # Across a `code_id` move the carry is unconditional. The concern
        # that a changed attributor could satisfy the trigger's "unchanged
        # across two judged sweeps" equality by coincidence is answered by
        # the equality itself: if the new attributor names the SAME units for
        # the SAME failing test, the tree-level claim the evidence makes —
        # rewrites of those units moved no verdict — is true of them
        # regardless of which code named them; and if it names different
        # ones, nothing fires. The file-fingerprint premise that used to gate
        # this path could not hold across the one stop that matters — the
        # escalated splice into the accessors' own file — and never did (see
        # `TddFloor.futile_units`).
        _futility = (saved.futile_units or None) if _ok else None
        return FloorResume(
            stall=None, best_pass=None, attempted=_cursor,
            landed=_carry.landed if _ok else None,
            declined=_carry.declined if _ok else None,
            released=_carry.released if _ok else (),
            futility=_futility,
            served=saved.served if _ok else None,
            served_verified=saved.served_verified if _ok else None,
            products=(saved.escalation_products or None) if _ok else None,
            unclaimed=saved.unclaimed if _ok else (),
            note=why + " changed"
            + (f"; {_carry.note} resumed anyway — a cursor answers to the "
               "cluster, not to the oracle or the code"
               if _cursor else "")
            + (f"; futility evidence resumed for {len(_futility)} test(s)"
               + (" across the code move — the evidence describes the tree, "
                  "not the build, and self-validates at the first sweep"
                  if saved.code_id != code_id else "")
               if _futility else ""))
    # The CURSOR resumes on the identity check alone, deliberately not on
    # `tree_sha`. It is not a claim about a score — it is the list of units
    # this pass already spent a model call on, and its safety condition is
    # only that the frontier is drawn from the same cluster by the same code.
    # A changed tree cannot invalidate it: `dispatch_rank.take` intersects the cursor
    # with the LIVE frontier, so a unit that no longer exists is dropped and a
    # unit that still does is one the previous process already tried. Gating
    # it on `tree_sha` would void it in exactly the case it is for — the
    # flat-score stop, whose whole premise is that the tree changed and the
    # score did not.
    carry = cursor_carry(saved, cluster=cluster, code_id=code_id)
    if saved.tree_sha != tree_sha:
        return FloorResume(
            stall=saved.stall, best_pass=None, attempted=carry.attempted,
            landed=carry.landed, declined=carry.declined,
            released=carry.released,
            futility=saved.futile_units or None,
            served=saved.served,
            served_verified=saved.served_verified,
            products=saved.escalation_products or None,
            unclaimed=saved.unclaimed,
            note=(f"stall={saved.stall} resumed; best_pass={saved.best_pass} "
                  f"NOT resumed — tree_sha ({saved.tree_sha} != {tree_sha}) "
                  "changed, so nothing here can restore that peak"
                  + (f"; {carry.note} resumed" if carry.attempted else "")
                  + (f"; futility evidence resumed for "
                     f"{len(saved.futile_units)} test(s)"
                     if saved.futile_units else "")))
    return FloorResume(
        stall=saved.stall, best_pass=saved.best_pass,
        attempted=carry.attempted,
        landed=carry.landed, declined=carry.declined,
        released=carry.released,
        futility=saved.futile_units or None,
        served=saved.served,
        served_verified=saved.served_verified,
        products=saved.escalation_products or None,
        unclaimed=saved.unclaimed,
        note=(f"best_pass={saved.best_pass} stall={saved.stall}"
              + (f" {carry.note}" if carry.attempted else "")
              + (f" futility evidence={len(saved.futile_units)} test(s)"
                 if saved.futile_units else "")))
