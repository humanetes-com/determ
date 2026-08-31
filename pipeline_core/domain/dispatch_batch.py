"""One iteration's slice of the TDD frontier, and the pass that slice belongs to.

WHY THIS EXISTS. The TDD sweep is the loop's only measurement of the tree, and
it is nearly free: the cargo runs on the iteration lines cost `rc=101 (0.4s)`,
`(1.0s)`, `(1.1s)`. What separates two sweeps is the fill batch between them,
and that batch was the WHOLE frontier. Run `669347-cf33eae3` (2026-08-15)
dispatched 55 impl blocks plus 9 file units in one batch and logged exactly one
`phase_c.tdd.iter` in 4h17m. Measured from `phase_c.tdd.iter` timestamps in
`logs/events.jsonl`, one iteration costs 198-253 minutes:

    3368-753757e6    4 iters   gaps 0, 198, 253 min   (life 458m05s)
    263217-d41a5bb5  2 iters   gap 243 min            (life 308m29s)
    500301-6a66bf86  1 iter    -                      (life 214m10s)

against run lifetimes of 214-458 minutes. So a run yields one or two data
points, and every gate that acts at a sweep boundary — the batch ratchet,
re-admission, the crash retrigger, the plateau exit — gets one or two chances
to fire in a whole run. Slicing the frontier makes the loop LOOK more often at
the same model cost. It does not change WHAT gets filled or in what order:
`order_frontier` still ranks the entire frontier and the batch takes the next
not-yet-attempted units off that ranking.

THE PASS IS THE UNIT THE STALL BUDGET IS DENOMINATED IN. `TDD_MAX_STALLS` asks
"have the last N iterations of fills failed to move the score". With a capped
batch an iteration attempts only part of the frontier, so charging the stall on
every iteration would make the plateau exit fire (frontier/cap) times sooner in
wall-clock than it does today — trading a sampling defect for a premature-exit
one. A PASS is one traversal of the frontier; only the sweep that follows a
COMPLETED pass may charge the stall, which restores the pre-cap meaning exactly.

THE CURSOR IS A SET, NOT AN INDEX. The frontier is rebuilt from the failing
tests every iteration and shrinks as fills land, so a positional cursor over a
changing list would keep re-dispatching the head and starve the tail — the same
shape as the "iteration spent on the blocked instead of the blockers" pathology
the loop already carries scar tissue for. Tracking which units this pass has
already attempted is stable under that churn: units that leave the frontier
simply never come back for their turn, and units that arrive mid-pass are
picked up before the pass closes.

THE PASS CLOSES OVER A COHORT, NOT OVER A MOVING TARGET. "Picked up before the
pass closes" was the sentence that had to change. A pass whose closure condition
is "every unit CURRENTLY in the frontier has been attempted" cannot close while
the frontier gains more units per sweep than the cap retires — and the frontier
legitimately grows, because futility escalation adds a crashed test's producers
(36, 39 and 25 fns on one iteration of run `1692702-94e6163a`, 2026-08-16) and
crash evidence adds its subjects. That run's frontier went 35 -> 57 -> 63 against
a cursor at 12 -> 21 -> 36 and closed nothing in 3h36m.

Two terminators are denominated in completed traversals — the flat-score GPU
guard (`domain/flat_score.py`, which needs `first_traversal < traversal`) and
the plateau budget (`TDD_MAX_STALLS`, which charges only after a completed
pass) — so a traversal index stuck at 0 disables BOTH, and that run had no
self-terminating path at all: `stall` read 0 of 3 at every iteration while the
same 79/101 came back three times. A gate whose unlock condition is progress the
situation prevents is the deadlock shape `domain/pass_highwater.py` documents;
this was another instance of it, arriving through a precondition rather than
through a rule.

So a pass is opened over the frontier AS IT STOOD when the pass opened — its
COHORT — and closes when every cohort member has been attempted or has left the
frontier. Units arriving mid-pass are still dispatched in rank order (that is
what keeps freshly-blamed crash subjects immediate), they simply do not extend
the traversal they arrived in; they belong to the next cohort. Closure is then
bounded: every iteration retires `cap` distinct units and no unit is retired
twice, so the pass ends after at most (cohort + arrivals ranked above it) / cap
iterations instead of never.

`cohort=None` keeps the pre-cohort behaviour exactly (the cohort is the current
frontier), which is what every caller that does not track a pass does.
"""
from __future__ import annotations

from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass

#: Frontier units dispatched per iteration when the cap is left at its default.
#: 12 against the live frontier of 55-69 puts a sweep roughly every 45 minutes
#: at the ~4h/iteration rate measured above, i.e. five to six measurements where
#: the loop previously took one.
DEFAULT_MAX_DISPATCH = 12


@dataclass(frozen=True, slots=True)
class DispatchBatch[K: Hashable]:
    """The units one iteration will dispatch, and whether taking them closes a
    pass over the frontier."""

    keys: tuple[K, ...]
    #: True when this batch exhausts what the current pass had left, so the
    #: NEXT sweep measures a completed traversal and may charge the stall.
    completes_pass: bool
    #: How many of the frontier's units this pass had already attempted before
    #: this batch. 0 on the first batch of a pass.
    attempted_before: int
    #: The whole frontier this batch was drawn from.
    frontier_size: int

    @property
    def capped(self) -> bool:
        """True when the cap actually withheld work — i.e. the batch is a
        strict subset of what the pass had pending. False when the batch took
        everything left, which is the uncapped loop's behaviour."""
        return not self.completes_pass

    @property
    def rendered(self) -> str:
        """`taken/frontier` for the iteration line, with a pass marker so a
        reader can tell a mid-pass sweep from one that closed a traversal."""
        done = self.attempted_before + len(self.keys)
        return (f"{len(self.keys)} of {self.frontier_size} "
                f"({done}/{self.frontier_size} this pass"
                f"{'' if self.completes_pass else ', mid-pass'})")


def take_batch[K: Hashable](ordered: Sequence[K], attempted: Iterable[K],
                            cap: int,
                            cohort: Iterable[K] | None = None,
                            exempt: Iterable[K] | None = None,
                            ) -> DispatchBatch[K]:
    """Next slice of `ordered` this pass has not attempted yet.

    `ordered` is the frontier in dispatch rank (leaf-first), `attempted` is what
    the current pass has already handed to the engine. A `cap` of 0 or less
    disables batching and returns the whole pending set, which is the pre-cap
    behaviour — keep that available, because a small crate whose frontier is
    already under the cap should not pay for the machinery.

    `exempt` is the units whose motivating evidence is LIVE ON THIS SWEEP, and
    it is subtracted from `attempted` before anything else runs. Without it the
    cursor silently voids every ranking applied above it: `promote_crash_evidence`
    moves a fault's blamed frames to the front of `ordered`, and the very next
    statement drops them again because an earlier batch of the same pass had
    already offered them once. Priority applied to a list the filter has already
    emptied is not priority.

    Measured, run `1519852-5d607329` (2026-08-30, cluster stable): five binaries
    dead of `AddressSanitizer: heap-use-after-free`, 13 tests reporting no
    verdict, the localizer blaming `src/error.rs::drop` with `::object_drop` and
    `src/ptr.rs::as_ptr` aimed as evidence — and all three sitting in the
    persisted cursor's `attempted`. The batch line read `0 of them are in this
    batch`, five processes in a row (log lines 1556, 1914, 2182, 2419, 2676),
    while the loop's own fallback for a capped-out retrigger is "the blamed fn
    stays in the fill frontier with its crash report instead". That fallback was
    unreachable by construction.

    WHY THIS CANNOT SPIN, and why the bound is not another cap. The exemption is
    recomputed from evidence the sweep re-derives, so it ends by itself the
    moment the fault does — a right body produces no crash, no blame, no
    exemption. What bounds a unit the model cannot fix is the machinery that
    already owns that question: conviction and exhaustion withhold it from
    `ordered` upstream, and an exempt unit that is not in `ordered` cannot be
    taken. The cursor is a per-pass FAIRNESS device — it exists so the head does
    not starve the tail — and fairness is the wrong instrument for a unit that
    is blocking 13 tests. A second cap here would rebuild the retrigger's own
    failure: a budget that expires while the fault it was spent on is still live.

    `cohort` is the frontier the CURRENT pass opened over. The pass closes when
    every one of those units has been attempted or has left the frontier —
    never when the frontier as it stands now is exhausted, which a growing
    frontier makes unreachable (see the module docstring). `None` means "the
    frontier as it stands is the cohort", which is the pre-cohort behaviour and
    what a caller that does not track passes wants.

    When the pass has nothing pending (every frontier unit already attempted, or
    the frontier shrank below what was attempted) the pass has closed: a fresh
    one starts here, over the frontier as it stands NOW rather than as it stood
    when the last pass opened.
    """
    served = set(attempted)
    seen = served - set(exempt or ())
    pending = [k for k in ordered if k not in seen]
    live = set(ordered)
    # TRAVERSAL ACCOUNTING IGNORES THE EXEMPTION, deliberately. A unit re-offered
    # on live evidence has already had its turn in this pass, so counting it as
    # outstanding would hold the traversal open — and a fault blaming more frames
    # than the cap could then never be fully taken in one batch, leaving
    # `completes_pass` permanently False. Both terminators denominated in
    # completed passes (the flat-score guard and `TDD_MAX_STALLS`) would go
    # quietly dead, which is the deadlock shape `domain/pass_highwater.py`
    # documents: a gate whose unlock condition is progress the situation
    # prevents. The exemption buys a re-dispatch; it does not buy a second
    # traversal credit.
    outstanding = (live if cohort is None else live & set(cohort)) - served
    if not pending:
        # Pass closed. Reopen on the current frontier; `attempted_before` is 0
        # because none of THIS pass's traversal has happened yet.
        pending = list(ordered)
        seen = set()
        outstanding = live
    attempted_before = len(ordered) - len(pending)
    keys = tuple(pending) if cap <= 0 or len(pending) <= cap \
        else tuple(pending[:cap])
    return DispatchBatch(keys=keys,
                         completes_pass=not (outstanding - set(keys)),
                         attempted_before=attempted_before,
                         frontier_size=len(ordered))


def promote_crash_evidence[K: Hashable, V](
    ordered: Sequence[tuple[K, V]], grades: Mapping[K, int],
) -> list[tuple[K, V]]:
    """`ordered` with crash-evidence units moved to the front, best grade first,
    each grade keeping the ranking it already had.

    WHY THIS IS NOT PART OF `order_frontier`. That ranking answers "which unit
    is most likely to unblock others" from static shape — panic depth, how many
    failing tests attribute it, how many callers reference it. Crash evidence
    answers a different question: THIS fn, on THIS sweep, is named by a fault
    the tree just produced. The two were being compared on the first ranking's
    scale, so the loop's strongest signal was ordered as if it were its weakest.

    The measurement, run `1692702-94e6163a` (2026-08-16). The chain-derived
    blame named `src/error.rs::context_chain_drop_rest`, `::context_drop_rest`,
    `::drop`, `::object_drop` and `::object_drop_front` on iterations 2 and 3
    off a double drop of `Arc<Atomic<bool>>` (freed `tests/test_downcast.rs:83`,
    touched `:85`). The evidence pack gave all five a prompt, a frontier entry,
    an exhaustion void and dispatch ELIGIBILITY — and nothing gave them
    priority, so iteration 2's batch of 12 out of 57 logged `0 skipped — free
    fns` while four of the five are free fns. `object_drop_front`, byte-
    identical to `object_drop` since the 79-passing checkpoint and the only one
    of the five that is wrong, sorted last of them on the `(file, fn)` tiebreak
    and has not been dispatched in any run since `2219473-e43ea61c`.

    Grades are `0` best (an observed faulting frame), then chain-derived
    inference, then a synthesized subject — an inference must not outrank an
    observation. Units absent from `grades` keep their order behind every graded
    one. Stable within a grade, so the family of one fault dispatches together
    in the batch that found it rather than one iteration apart in name order.
    """
    if not grades:
        return list(ordered)
    graded: dict[int, list[tuple[K, V]]] = {}
    rest: list[tuple[K, V]] = []
    for item in ordered:
        grade = grades.get(item[0])
        if grade is None:
            rest.append(item)
        else:
            graded.setdefault(grade, []).append(item)
    out: list[tuple[K, V]] = []
    for grade in sorted(graded):
        out.extend(graded[grade])
    out.extend(rest)
    return out


def promote_product_ahead[K: Hashable, V](
    ordered: Sequence[tuple[K, V]], product: frozenset[K],
) -> list[tuple[K, V]]:
    """`ordered` with the product units moved to the front, stable on both
    sides.

    WHY. `promote_producers` reorders only the escalated units' own slots,
    so a newly attributed accessor — the unit `order_frontier` ranks first
    — keeps the head of the batch. Persist-the-product then keeps the
    constructors in the cohort and still spends the first dispatch on the
    accessors: runs `4911`/`2932` (2026-08-25/26), and the cap=1 replay of
    that shift (new accessor at slot 0, producer parked). The product is
    already the measurement that those accessors cannot pay; leaving them
    at the front is the re-buy persist exists to close. Crash promotion
    still runs after this, so an observed fault keeps the very front.
    """
    if not product:
        return list(ordered)
    head = [item for item in ordered if item[0] in product]
    if not head:
        return list(ordered)
    tail = [item for item in ordered if item[0] not in product]
    return head + tail


def promote_by_blocker_counts[K: Hashable, V](
    ordered: Sequence[tuple[K, V]], counts: Mapping[K, int],
) -> list[tuple[K, V]]:
    """`ordered` re-sorted by descending blocker count, original index for ties.

    Unlike `promote_producers`, this reorders the WHOLE list: `take_batch`
    takes a prefix, so a producer that is in the frontier but ranked last
    never makes a cap=12 slice. The live instance is run `155810-cbb3b9fc`
    iter 2: `error.rs::construct` held 27 of 30 deferred consumers and sat
    outside the first batch because `order_frontier` ranked it on leaf-name
    frequency. An empty `counts` is a no-op. Crash-evidence promotion that
    runs after this still wins."""
    if not counts:
        return list(ordered)
    return [
        item for _, item in sorted(
            enumerate(ordered),
            key=lambda idx_item: (
                -counts.get(idx_item[1][0], 0), idx_item[0]),
        )
    ]


def overlay_crash_on_ranks[K: Hashable](
    ranks: Mapping[K, int], grades: Mapping[K, int],
) -> dict[K, int]:
    """Crash-evidence units outrank every histogram count; grade 0 is highest.

    `dispatch_ranks` is what the engine re-sorts on after `take_batch`. Passing
    the histogram alone undoes `promote_crash_evidence` the same way leaf-first
    undid constructor ranking (run `155810-cbb3b9fc` iter 1). Offset is
    `max(histogram)+1+(max(grade)-grade)` so the best observed grade sits
    above weaker inferences above any blocker count. Empty `grades` returns a
    copy of `ranks`. Units absent from `grades` keep their histogram value."""
    out = dict(ranks)
    if not grades:
        return out
    ceiling = max(out.values(), default=0)
    span = max(grades.values())
    for k, g in grades.items():
        out[k] = ceiling + 1 + (span - g)
    return out


def promote_producers[K: Hashable, V](
    ordered: Sequence[tuple[K, V]], escalated: frozenset[K],
    builders: frozenset[K], callees: frozenset[K],
) -> list[tuple[K, V]]:
    """`ordered` with the ESCALATED units reordered so the table-builders lead
    and a callee precedes its caller — leaving every non-escalated unit exactly
    where it was.

    WHY, and the measurement. A futility escalation dispatches the crate's
    constructors as one cohort, and `order_frontier`'s final tiebreak is
    `-leaf_name_counts[fn]` — the fn's own name as a word across the tree. The
    crate's most frequent words are its accessors and delegators (`new`, `from`,
    `msg`, `context`, `deref`), while the table-BUILDERS the escalation actually
    needs rewritten (`construct_from_adhoc`, …) occur a handful of times, so
    they sort to the BACK of the cohort and arrive one dispatch (one ~3h sweep)
    later than the delegators that merely call them. Three producer passes ran
    the identical first batch — `context`/`with_context`/`__dispatch_ensure`/
    `error.rs::{context,deref,downcast,from,from_boxed,msg,new}`/`kind.rs::new`/
    `lib.rs::must_use`, zero table-builders — and each was stopped or re-bought
    on exactly that batch-1/batch-2 boundary (`40938-f4e8e94b`,
    `4911-9ab8852e`, `95878-aaf5bf1d`, `349271-d876e4a9`). The delegators cannot
    pay the test until the body they delegate to is right, so the first batch of
    every producer pass was spending its GPU on the half of the defect that
    cannot move first.

    The reorder is confined to the escalated units' own positions: the
    subsequence of `ordered` whose keys are in `escalated` is stably re-sorted
    by `(0 if builder else 1, 0 if callee else 1, original index)` and dropped
    back into those same slots, so a directly-convicting non-escalated unit
    keeps its rank and the crash-evidence promotion that runs after this is
    undisturbed. `builders` are the escalated units that CONSTRUCT a crate
    nominal type — the table literal, the funnel's payload literal, a wrapper's
    tuple constructor — the constructor ROLE read off the body's shape, so the
    delegating wrong constructor ranks with the table-builders it should be one
    of; `callees` are the escalated units another escalated unit calls (a
    resolved call, not a name token), so a delegator follows what it delegates
    to. Both are computed by the caller from the fn/type records
    (`installable_fns.producer_dispatch_ranks`) — this function is pure
    ordering."""
    if not escalated:
        return list(ordered)
    esc_positions = [i for i, (k, _) in enumerate(ordered) if k in escalated]
    if not esc_positions:
        return list(ordered)
    esc_items = [ordered[i] for i in esc_positions]

    def _rank(idx_item: tuple[int, tuple[K, V]]) -> tuple[int, int, int]:
        idx, (k, _) = idx_item
        return (0 if k in builders else 1, 0 if k in callees else 1, idx)

    reordered = [it for _, it in sorted(enumerate(esc_items), key=_rank)]
    out = list(ordered)
    for slot, item in zip(esc_positions, reordered, strict=True):
        out[slot] = item
    return out


def service_bucket[K: Hashable](
    key: K,
    served: Collection[K],
    flat: Collection[K],
    dead: Collection[K] = (),
    red_dead: Collection[K] = (),
) -> int:
    """0 never-served live, 1 served live, 2 served-and-flat live,
    3 red-dead, 4 dead.

    Dead last regardless of service: a body no test executes cannot move a
    verdict, and never-served-first would otherwise put those dead units at
    the head of every batch. On the 2026-08-30 product the never-served
    dead units that occupy cap slots without this bucket are
    `src/context.rs::write_str` and `src/error.rs::erase`. A locator is in
    `dead` only when every body under that name is unexecuted; a live
    sibling (error.rs::backtrace at line 389 vs ErrorImpl at 918) must
    not be demoted.

    `red_dead` is the same argument one step weaker and one step more
    useful: no FAILING test executes the body, so a rewrite cannot move a
    red verdict this sweep even though passing tests keep it alive and out
    of `dead`. It sits directly above `dead` because dead is the stronger
    statement and implies this one — the order is checked dead-first so a
    unit in both buckets grades as dead. `src/lib.rs::Ok` is the unit that
    named it: generic and `#[inline]`, absent from the profile rather than
    zero-count, so `dead` can never hold it however long nothing calls it.
    """
    if key in dead:
        return 4
    if key in red_dead:
        return 3
    if key not in served:
        return 0
    if key in flat:
        return 2
    return 1


def promote_by_service[K: Hashable, V](
    ordered: Sequence[tuple[K, V]],
    served: Collection[K],
    flat: Collection[K],
    dead: Collection[K] = (),
    red_dead: Collection[K] = (),
) -> list[tuple[K, V]]:
    """`ordered` with never-served live units first, then served, then
    served-and-last-graded-flat, then red-dead, then dead. Stable within a
    bucket.

    WHY THIS IS NOT A KEY IN `order_frontier`. That ranking runs BEFORE
    `promote_producers`, which then puts the table-builders — the units the
    ledger already graded flat — back at the head of the product block.
    Computed 2026-08-30 on run `2220347-7fa9b90f`'s floor: `src/lib.rs::
    format_err` is rank 12 under `order_frontier` (leaf-name frequency
    already broke the 3-test group; the filename tie the first draft named
    was not the live tie) and rank 21 after `promote_producers`, identical
    with or without a service component in `order_frontier`'s key. Adding
    the component before `file_rel` cannot survive constructor-first, so
    the closer sits HERE, after that promotion and before crash evidence
    (which still outranks everything).

    Empty `served`/`flat`/`dead`/`red_dead` is a no-op (every unit buckets 0,
    original index wins) — walks and a first fill of a virgin cluster are
    unchanged. Crash promotion that runs after this still wins, which is why
    a crash-blamed unit needs no exemption from `red_dead`: an observed fault
    re-promotes it above every bucket here.
    """
    if not served and not flat and not dead and not red_dead:
        return list(ordered)
    return [
        item for _, item in sorted(
            enumerate(ordered),
            key=lambda idx_item: (
                service_bucket(idx_item[1][0], served, flat, dead, red_dead),
                idx_item[0],
            ),
        )
    ]
