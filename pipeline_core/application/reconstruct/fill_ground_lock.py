"""The splice lock of a tree is the transaction a fill's ground is held by.

Every route of a fill writes, measures and keeps or reverts while it holds
the tree's splice+cargo lock (`splice.splice_lock_for`): the file, impl,
pair, session and salvage routes in `with` blocks, the per-fn route by
explicit `acquire`/`release` around each attempt. So while a fill's ground
is bound (`fill_ground`), the lock is where the ground is kept honest, and
no keep site has to remember to say so:

- TAKING the lock is the premise. Any file whose bytes are not the accepted
  ones was written outside every hold, by no verdict. It is put back before
  the holder reads anything (`HEALTH ‼ UNJUDGED_WRITE`,
  `phase_c.fill.unjudged_write`), so neither the holder's baseline nor the
  next sweep's batch ratchet reads it.
- RELEASING the lock is the verdict. Whatever the holder left changed is
  what its gates decided: a keep, a salvage of the best compiling body, an
  annotated stub, a crash settlement's stub. Those bytes become the
  accepted ones, per file. A revert that put the old bytes back changes
  nothing. A revert that did NOT put them back is not a verdict: the one
  revert writer (`MutatorAdapter.revert_body_scoped`) reports the failure
  (`note_failed_revert`), and the release restores that file from the
  accepted bytes instead of storing what the revert left. A hold that ends
  by a crash accepts nothing: the next premise removes whatever the
  interrupted write left. A `with` hold that ends by a stop accepts,
  because the session's stop path restores its best judged state first;
  the per-function attempt's explicit hold is abandoned on any exit that
  escapes it (`AttemptHold`), so it accepts nothing.

The design this replaces kept a list of keep sites that had to call
`accept`; the list on the 2026-10-22 plan omitted the per-fn salvage, the
annotated stub and the impl salvage, and any site added later would have
been one more. Tying the accept to the release makes a staying write that
skips it impossible to produce inside a hold, and one made outside a hold
is a premise break by construction.

Outside a fill nothing is bound and the lock is a plain lock.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from pipeline_core.application.reconstruct.tree_txn import (
    PreImage,
    restore,
    tree_scan_root,
)
from pipeline_core.domain.fill_context import current_route
from pipeline_core.domain.fill_ground import (
    FillGround,
    GroundBreak,
    GroundRoleError,
    MeasureRole,
    UnjudgedWrite,
)

__all__ = [
    "AttemptHold",
    "GroundSession",
    "GroundedLock",
    "check_measurement",
    "current_ground",
    "fill_ground",
    "ground_lock_for",
    "note_failed_revert",
    "note_premise_reading",
]

UNJUDGED_WRITE_EVENT = "phase_c.fill.unjudged_write"
PREMISE_AFTER_WRITE_EVENT = "phase_c.fill.premise_after_write"


def read_tree(root: Path) -> dict[str, bytes | None]:
    """Every readable `.rs` under the crate's `src/` (`scan_tree`'s images)."""
    return scan_tree(root)[0]


def scan_tree(root: Path) -> tuple[dict[str, bytes | None], frozenset[str]]:
    """Every `.rs` under the crate's `src/` (or under `root` when it has no
    `src/`), keyed the way the routes and cargo spell it from `root`, and the
    files that exist and could not be read. An unreadable file is not a
    missing one: read as missing, the diff would restore the accepted bytes
    over it on a transient read failure, so the ground neither restores nor
    accepts it."""
    scan, prefix = tree_scan_root(root)
    out: dict[str, bytes | None] = {}
    unreadable: set[str] = set()
    if not scan.is_dir():
        return out, frozenset()
    for p in sorted(scan.rglob("*.rs")):
        rel = prefix + str(p.relative_to(scan))
        try:
            out[rel] = p.read_bytes()
        except OSError:
            unreadable.add(rel)
    return out, frozenset(unreadable)


class GroundSession:
    """One fill's ground over one tree, with the counts the fill-return line
    and the flow audit report."""

    def __init__(self, root: Path, *, log: Callable[[str], None],
                 emit: Callable[..., None], cluster: str = "",
                 iteration: int = 0) -> None:
        self.root = Path(root)
        self._log = log
        self._emit = emit
        self.cluster = cluster
        self.iteration = iteration
        self.ground = FillGround.open(read_tree(self.root))
        self.opened = self.ground.identity
        self.premises = 0
        self.judgments = 0
        self.breaks = 0
        #: Reverts that did not put their pre-image back (`note_failed_revert`):
        #: each is restored from the accepted bytes at its hold's release and
        #: counted in `breaks` as well.
        self.failed_reverts = 0
        self.broken_files: list[str] = []
        #: run label -> pass count of the first premise that run read; the
        #: route row's `base_pass`, so a run's baseline can be compared with
        #: the sweep's reading without a `fill.reading` (a session emits
        #: none).
        self.base_pass: dict[str, int] = {}

    def restore_unjudged(self, when: str,
                         only: frozenset[str] | None = None) -> GroundBreak | None:
        """Put back every file whose bytes are not the accepted ones (only the
        files in ``only`` when given). Raises `UnjudgedWrite` when the restore
        cannot be made."""
        found, unreadable = scan_tree(self.root)
        brk = self.ground.diff(found, ignore=unreadable)
        if brk is not None and only is not None:
            brk = brk.restricted(only)
        if brk is None:
            return None
        run = current_route()[1] or when
        self.breaks += 1
        self.broken_files.extend(f for f in brk.files if f not in self.broken_files)
        self._log(f"  [phase-c/ground] HEALTH ‼ UNJUDGED_WRITE {run}: the tree "
                  f"holds bytes no verdict left in {', '.join(brk.files)} "
                  f"({when}) — restored to the accepted bytes before anything "
                  "reads it.")
        self._emit(UNJUDGED_WRITE_EVENT, data={
            "cluster": self.cluster, "iter": self.iteration, "run": run,
            "when": when, **brk.payload()})
        try:
            # The accepted bytes are a pre-image: restored byte for byte by
            # the transaction module, a file the ground never held removed.
            for rel in brk.files:
                restore(PreImage(self.root / rel, self.ground.stored(rel), None))
        except OSError as exc:
            raise UnjudgedWrite(brk.files, f"restore failed: {exc}") from exc
        after, unreadable = scan_tree(self.root)
        left = self.ground.diff(after, ignore=unreadable)
        if left is not None:
            left = left.restricted(frozenset(brk.files))
        if left is not None:
            raise UnjudgedWrite(left.files, "restore left other bytes")
        return brk

    def accept_hold(self, refused: frozenset[str] = frozenset()) -> None:
        """Accept what the hold left changed, except ``refused``: files a
        revert inside the hold failed to put back, which no verdict left and
        which the caller restores from the accepted bytes."""
        found, unreadable = scan_tree(self.root)
        brk = self.ground.diff(found, ignore=unreadable | refused)
        if brk is not None:
            self.ground.accept({rel: found.get(rel) for rel in brk.files})

    def premise_check(self) -> None:
        """A premise read inside a hold whose holder already wrote: the
        baseline then includes the holder's own write. Logged loudly and
        measured as asked; the restore is the lock's, at acquire."""
        self.premises += 1
        found, unreadable = scan_tree(self.root)
        brk = self.ground.diff(found, ignore=unreadable)
        if brk is None:
            return
        run = current_route()[1] or "?"
        self._log(f"  [phase-c/ground] HEALTH ‼ PREMISE_AFTER_WRITE {run}: a "
                  f"baseline was read after this hold wrote "
                  f"{', '.join(brk.files)} — it is not the accepted tree.")
        self._emit(PREMISE_AFTER_WRITE_EVENT, data={
            "cluster": self.cluster, "iter": self.iteration, "run": run,
            **brk.payload()})


class GroundedLock:
    """A `threading.Lock` that, while a `GroundSession` is bound, restores
    unjudged bytes when taken and accepts the holder's bytes when released."""

    def __init__(self, tree: Path) -> None:
        self._lock = threading.Lock()
        self.tree = Path(tree)
        self._owner: int | None = None
        self._session: GroundSession | None = None
        #: Files a revert inside the current hold did not put back
        #: (`note_failed_revert`); cleared at every release.
        self._refused: set[str] = set()

    @property
    def session(self) -> GroundSession | None:
        return self._session

    def bind(self, session: GroundSession | None) -> None:
        self._session = session

    def held_by_me(self) -> bool:
        return self._owner == threading.get_ident()

    def locked(self) -> bool:
        return self._lock.locked()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not self._lock.acquire(blocking, timeout):
            return False
        self._owner = threading.get_ident()
        session = self._session
        if session is not None:
            try:
                session.restore_unjudged("lock taken")
            except BaseException:
                self._owner = None
                self._lock.release()
                raise
        return True

    def refuse(self, rel: str) -> None:
        """Mark ``rel`` as holding bytes a revert in this hold failed to
        remove. Called by the holder's thread only (`note_failed_revert`)."""
        self._refused.add(rel)

    def release(self, *, accept: bool = True) -> None:
        """Release the hold. ``accept`` stores what the holder left changed as
        the accepted bytes, minus every file a revert in this hold failed to
        put back: those are restored from the accepted bytes now, before the
        lock is let go, so no reader outside the hold sees them and the next
        premise does not inherit them as ground. Releasing without
        ``accept`` stores nothing and the next taker restores the rest."""
        session = self._session
        refused = frozenset(self._refused)
        self._refused.clear()
        try:
            if session is not None and accept:
                session.accept_hold(refused)
            if session is not None and refused:
                session.failed_reverts += 1
                session.restore_unjudged("a revert in this hold did not land",
                                         only=refused)
        finally:
            self._owner = None
            self._lock.release()

    def abandon(self) -> None:
        """Release a hold that ended without a verdict (an exception or a stop
        inside an explicit acquire/release hold): nothing it left is
        accepted, and the next taker restores the accepted bytes."""
        self.release(accept=False)

    def __enter__(self) -> GroundedLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        # A crash is not a verdict: what the interrupted hold left is not
        # accepted, and the next premise removes it. A stop (`SystemExit`
        # from the graceful sentinel, an interrupt) is accepted for the rest
        # of THIS fill: the session's stop path puts the tree back to its
        # best judged state before the stop propagates ("the tree must not
        # hold an unjudged change set"), and only the fill-return premise
        # reads the ground after it. It is not what the next process sees:
        # the driver's stop path (`_restore_abandoned_bet` in
        # `run_phase_c_tdd`) runs after the ground is unbound and decides
        # that tree. The same stop/crash line the driver draws for the fill
        # (`not isinstance(exc, Exception)` re-raises a stop).
        self.release(accept=exc_type is None
                     or not issubclass(exc_type, Exception))


class AttemptHold:
    """An explicit take/drop hold on a tree's splice lock (the per-function
    attempt, whose hold spans a dozen exits and cannot be one `with` block)
    that knows whether it is held, so the attempt's caller can release a hold
    an exception or a stop left behind. `abandon` releases without a verdict:
    on a `GroundedLock` nothing is accepted; on a plain lock it releases."""

    def __init__(self, lock: Any) -> None:
        self.lock = lock
        self.held = False

    def take(self) -> None:
        self.lock.acquire()
        self.held = True

    def drop(self) -> None:
        self.held = False
        self.lock.release()

    def abandon(self) -> None:
        if not self.held:
            return
        self.held = False
        if isinstance(self.lock, GroundedLock):
            self.lock.abandon()
        else:
            self.lock.release()


# Process-global path-keyed splice+cargo locks. One lock per resolved source
# tree — regression-gate baselines are captured fresh INSIDE this lock, so it
# must be a genuine per-tree singleton across every engine in the process.
_LOCKS: dict[str, GroundedLock] = {}
_LOCKS_GUARD = threading.Lock()


def ground_lock_for(tree: Path) -> GroundedLock:
    """The splice+cargo lock owned by `tree` (one per resolved source tree)."""
    key = str(Path(tree).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = GroundedLock(Path(tree))
        return lock


def note_failed_revert(path: Path) -> None:
    """A revert of ``path`` did not put its pre-image back. Called by the
    one revert writer every route's per-function revert goes through
    (`MutatorAdapter.revert_body_scoped`), so a new caller cannot skip it.

    Releasing a hold accepts what the holder left changed; a revert that
    failed leaves the bytes it was meant to remove, and accepted they became
    the ground the next premise reads (`_revert_own_body` logs `REVERT
    FAILED`, returns false, and the per-function failure path released the
    lock regardless — review of the 2026-10-22 implementation). The hold of
    the calling thread on the tree holding ``path`` refuses that file: its
    release restores the accepted bytes instead of storing these. Outside a
    bound fill, or with no hold on that tree, this does nothing."""
    target = Path(path).resolve()
    with _LOCKS_GUARD:
        locks = list(_LOCKS.values())
    for lock in locks:
        session = lock.session
        if session is None or not lock.held_by_me():
            continue
        root = session.root.resolve()
        try:
            rel = target.relative_to(root)
        except ValueError:
            continue
        lock.refuse(str(rel))
        return


def check_measurement(cwd: Path, role: MeasureRole) -> None:
    """The baseline port's guard, called before every suite measurement that
    names a role. Outside a bound fill it does nothing. Inside one, the
    caller must hold the tree's lock (else another worker's unjudged splice
    is in its reading), and a premise is checked against the accepted
    bytes."""
    if not isinstance(role, MeasureRole):
        raise TypeError(f"measurement role must be a MeasureRole, got {role!r}")
    lock = ground_lock_for(cwd)
    session = lock.session
    if session is None:
        return
    if not lock.held_by_me():
        raise GroundRoleError(
            f"{role.value} measurement of {cwd} outside the tree's splice "
            "lock while a fill's ground is bound")
    if role is MeasureRole.PREMISE:
        session.premise_check()
    else:
        session.judgments += 1


# The bound session of the fill running now. Clusters fill one at a time per
# process, so the last one bound is the one a route row belongs to.
_CURRENT: list[GroundSession | None] = [None]


def current_ground() -> GroundSession | None:
    return _CURRENT[0]


def note_premise_reading(cwd: Path, pass_count: int) -> None:
    """Record the pass count a premise read for the run measuring it."""
    session = ground_lock_for(cwd).session
    if session is None:
        return
    run = current_route()[1]
    if run:
        session.base_pass.setdefault(run, int(pass_count))


@contextmanager
def fill_ground(root: Path, *, log: Callable[[str], None],
                emit: Callable[..., Any], cluster: str = "",
                iteration: int = 0) -> Iterator[GroundSession]:
    """Bind a ground over `root` for one fill. Opened under the lock on the
    tree as it stands (the caller opens it after every boundary write:
    reseed, ratchet restore, re-sweep). On the way out, the fill-return
    premise: anything a write after the last hold left is restored before
    the next sweep reads the tree, and the ground is unbound."""
    lock = ground_lock_for(root)
    with lock:
        session = GroundSession(root, log=log, emit=emit, cluster=cluster,
                                iteration=iteration)
        lock.bind(session)
        _CURRENT[0] = session
    try:
        yield session
    finally:
        try:
            lock.acquire()
            lock.release()
        except UnjudgedWrite as exc:
            # The fill is over; a restore that could not be made at its
            # return has nothing left to defer. It is reported as the run
            # exit's unrestorable write is (`phase_c.fill.unjudged_stop`,
            # which the flow audit fails on) and the sweep reads whatever is
            # on the tree, where the batch ratchet judges it. Raising here
            # turned a finished fill into `cluster_failed` (review of the
            # 2026-10-22 implementation).
            log(f"  [phase-c/ground] HEALTH ‼ UNJUDGED_WRITE after the fill: "
                f"{exc.why} in {', '.join(exc.files) or '?'} — the sweep "
                "reads it as it stands.")
            emit("phase_c.fill.unjudged_stop", data={
                "cluster": cluster, "iter": iteration,
                "files": list(exc.files), "runs": ["after the fill"],
                "deferred": []})
        finally:
            lock.bind(None)
            _CURRENT[0] = None
