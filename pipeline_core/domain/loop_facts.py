"""The inner loop's facts, as a closed vocabulary — stage 1 of the fold.

THE ARGUMENT, RE-MEASURED 2026-08-21. `phase_c_tdd.py` is 3,708 lines (3,053 on
2026-08-17: +655 in four days) and its state is mutable locals — a sweep's
reading, the stall streak, the high-water, the pass cursor, the flat-score
window, the destruction epoch, each read by several gates that cannot see each
other. Every fix has to tell one more reader, so the module grows by the size of
the fix. The proven-body ledger's own reader inventory is the same argument from
the other end: ONE destruction is observed by six exemptions
(`PassHighWater`'s void, the ratchet's withheld breach, the stall stand-down,
the stub-progress exemption, `readmission_warranted`, the ratchet snapshot), and
nothing in the code relates them.

Meanwhile the pipeline already emits 245 `emit()` calls over 140 event types,
302,722 events, 234 MB — a fact log that is written and never read back.

WHAT THIS MODULE IS. The vocabulary those facts have to fit into if the loop's
state is ever to be a FOLD over them rather than a pile of locals: a
discriminated union, not dicts, so a consumer that forgets a case fails at type
check instead of reading a default. `fold` turns a stream of them into the state
vector the loop prints, and `render_iter_line` prints it — which is the
completeness assertion in executable form. If the fold cannot reproduce a real
run's printed vector, the vocabulary is incomplete and the gates must NOT be
converted yet: a fold over an incomplete log is worse than a mutable local,
because it reads a default where the variable would have read the truth.

WHAT IS DELIBERATELY NOT HERE. No gate reads this module yet except
`flat_score`, which is stage 2's single conversion. Converting the
destruction-sensitive gates together is stage 3 and is explicitly out of scope
for 0.3: `domain/destruction_epoch.py` already types the comparison those gates
need, and the ledger changes what they SEE after a destruction, so converting
them in the same release would leave no clean attribution for either change.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class SweepMeasured:
    """One reconciled `cargo test` sweep — the loop's only source of verdicts.

    `judged` is False for a sweep that cannot judge the dispatch before it: a
    grace sweep after a deliberate re-stub, a compile-broken sweep, a tainted
    reading. Such a sweep is not evidence in either direction, which is why the
    flat-score fold counts it as a gap rather than as a reading."""

    iter: int
    cluster: str
    pass_count: int
    fail_count: int
    oracle_size: int
    rc: int = 0
    elapsed_s: float = 0.0
    passed: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    intact: bool = True
    judged: bool = True
    kind: str = "behavior"


@dataclass(frozen=True, slots=True)
class BatchDispatched:
    """The slice of the frontier this iteration handed to the fill engine."""

    iter: int
    cluster: str
    frontier_size: int
    dispatched: int
    attempted_before: int = 0
    cohort_size: int = 0
    skipped_exhausted: int = 0
    completes_pass: bool = False


@dataclass(frozen=True, slots=True)
class PassClosed:
    """A completed traversal of the cohort the pass opened over."""

    iter: int
    cluster: str
    cohort_size: int
    attempted: int
    landed: int = 0


@dataclass(frozen=True, slots=True)
class BodiesLanded:
    """Units whose on-disk body took a semantic change in this batch."""

    iter: int
    cluster: str
    units: tuple[str, ...] = ()
    declined: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BodiesRestubbed:
    """A DELIBERATE destruction: bodies reverted to `unimplemented!()`."""

    iter: int
    cluster: str
    count: int
    cause: str = ""


@dataclass(frozen=True, slots=True)
class BodiesReseeded:
    """The proven-body ledger putting bodies back at a destruction seam.

    New since the original analysis, and the reason the vocabulary had to be
    re-opened: a destruction that is UNDONE inside the iteration changes what
    every destruction-sensitive exemption observes."""

    iter: int
    cluster: str
    seeded: int
    rejected: int = 0
    skipped: int = 0
    site: str = ""


@dataclass(frozen=True, slots=True)
class LayoutReauthored:
    """t1.5 or the retrigger rewriting the layout the bodies answer to."""

    iter: int
    cluster: str
    component: str = ""
    cause: str = ""


@dataclass(frozen=True, slots=True)
class CrashObserved:
    """A sweep whose oracle came back short: a test binary died."""

    iter: int
    cluster: str
    vanished: tuple[str, ...] = ()
    binary: str = ""


Fact = (SweepMeasured | BatchDispatched | PassClosed | BodiesLanded
        | BodiesRestubbed | BodiesReseeded | LayoutReauthored | CrashObserved)


@dataclass(frozen=True, slots=True)
class IterVector:
    """What one iteration printed — the loop's whole state, per sweep.

    This is the object the completeness assertion compares: if a fold over the
    recorded facts cannot rebuild every field of it, the vocabulary is missing
    a fact and stage 2 does not start."""

    iter: int = 0
    cluster: str = ""
    rc: int = 0
    elapsed_s: float = 0.0
    pass_count: int = 0
    fail_count: int = 0
    oracle_size: int = 0
    improved: bool = False
    stall: int = 0
    max_stalls: int = 0
    frontier_size: int = 0
    kind: str = "behavior"
    skipped_exhausted: int = 0
    dispatched: int = 0
    attempted_before: int = 0
    cohort_size: int = 0
    completes_pass: bool = False
    traversals: int = 0
    restubbed: int = 0
    reseeded: int = 0

    def render(self) -> str:
        """The `iter` line as the loop prints it, minus the leading tag.

        Kept beside the fold rather than in the driver because it IS the
        assertion: a renderer that lives with the state it renders cannot
        silently drift from it."""
        return (f"[phase-c/tdd] iter {self.iter}: cargo test rc={self.rc} "
                f"({self.elapsed_s:.1f}s); pass={self.pass_count} "
                f"fail={self.fail_count} oracle={self.oracle_size} "
                f"(improved={self.improved}, stall={self.stall}/"
                f"{self.max_stalls}); frontier={self.frontier_size} fn(s) "
                f"({self.kind}), {self.skipped_exhausted} skipped as "
                f"exhausted; dispatching {self.dispatched} of "
                f"{self.frontier_size}")


@dataclass(frozen=True, slots=True)
class LoopView:
    """The fold's result: the per-iteration vectors, in order, plus the
    running quantities a gate would ask about."""

    vectors: tuple[IterVector, ...] = ()
    traversals: int = 0
    best_pass: int = -1
    stall: int = 0

    @property
    def last(self) -> IterVector | None:
        return self.vectors[-1] if self.vectors else None


@dataclass(slots=True)
class _Accum:
    """Mutable ONLY inside `fold`. The point of the exercise is that no gate
    holds this — it is derived from the facts on every call."""

    vectors: list[IterVector] = field(default_factory=list)
    traversals: int = 0
    best_pass: int = -1
    stall: int = 0
    pending: dict[int, IterVector] = field(default_factory=dict)


def fold(facts: Iterable[Fact], *, max_stalls: int = 0) -> LoopView:
    """Rebuild the loop's per-iteration state vector from its facts alone.

    Order matters only within an iteration: a `BatchDispatched` for iter N
    refines the vector `SweepMeasured` for iter N opened. Facts for an
    iteration that never measured (a retrigger's `continue` commits no batch)
    still produce a vector, because the loop printed one — and a fold that
    silently skipped it would disagree with the log on the ITERATION COUNT,
    which is the first thing an operator reads."""
    acc = _Accum()
    for f in facts:
        match f:
            case SweepMeasured():
                improved = f.judged and f.pass_count > acc.best_pass
                if improved:
                    acc.best_pass = f.pass_count
                    acc.stall = 0
                vec = IterVector(
                    iter=f.iter, cluster=f.cluster, rc=f.rc,
                    elapsed_s=f.elapsed_s, pass_count=f.pass_count,
                    fail_count=f.fail_count, oracle_size=f.oracle_size,
                    improved=improved, stall=acc.stall, max_stalls=max_stalls,
                    kind=f.kind, traversals=acc.traversals)
                acc.pending[f.iter] = vec
                acc.vectors.append(vec)
            case BatchDispatched():
                _refine(acc, f.iter, frontier_size=f.frontier_size,
                        dispatched=f.dispatched,
                        attempted_before=f.attempted_before,
                        cohort_size=f.cohort_size,
                        skipped_exhausted=f.skipped_exhausted,
                        completes_pass=f.completes_pass)
            case PassClosed():
                acc.traversals += 1
                _refine(acc, f.iter, traversals=acc.traversals,
                        completes_pass=True)
            case BodiesRestubbed():
                _refine(acc, f.iter, restubbed=f.count)
            case BodiesReseeded():
                _refine(acc, f.iter, reseeded=f.seeded)
            case _:
                continue
    return LoopView(vectors=tuple(acc.vectors), traversals=acc.traversals,
                    best_pass=acc.best_pass, stall=acc.stall)


def _refine(acc: _Accum, iter_n: int, **fields: object) -> None:
    vec = acc.pending.get(iter_n)
    if vec is None:
        return
    updated = replace(vec, **fields)       # type: ignore[arg-type]
    acc.pending[iter_n] = updated
    for i, v in enumerate(acc.vectors):
        if v.iter == iter_n and v.cluster == vec.cluster:
            acc.vectors[i] = updated
            break


def judged_readings(facts: Sequence[Fact]) -> tuple[tuple[int, int, int], ...]:
    """`(pass_count, oracle_size, traversal)` for every JUDGED sweep, in order.

    The one derivation `flat_score` needs, kept here so the guard does not have
    to know how a traversal is counted — the driver used to supply that number
    from a local, and a gate that both counts and decides is a gate whose
    disagreement with the log cannot be attributed."""
    out: list[tuple[int, int, int]] = []
    traversals = 0
    for f in facts:
        if isinstance(f, PassClosed):
            traversals += 1
        elif isinstance(f, SweepMeasured) and f.judged:
            out.append((f.pass_count, f.oracle_size, traversals))
    return tuple(out)
