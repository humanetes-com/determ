"""The phase board: what the run's phase events say about every registered phase.

Pure fold. `run_data.read_phase_records` and `run_data.state_phase_statuses` do
the I/O; this module turns their output into one status per (phase, cluster)
and the aggregate the strip draws. The registry (`domain.phase_registry`) is the
vocabulary, `phase.enter` / `phase.exit` events are what THIS process ran, and
the `pipeline_state.md` statuses are the authority for what an earlier process
finished.

Status of a slot:

    running    entered, not exited, and the pipeline process is alive
    aborted    entered, not exited, and the process is gone — the phase did not
               finish (the monitor used to show whatever the stale `current:`
               line said, which is how a dead process read as "augmenting")
    done       exited ok
    skipped    the phase's own checkpoint said it was already done
    error      exited with an error
    pending    nothing in the events and the state file does not say done

A phase with no events this run whose `done_keys` are satisfied in the state
file is `done` (finished by an earlier process). A phase with events always
takes the events over the state file.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pipeline_core.domain.phase_registry import (
    PHASE_ENTER,
    PHASE_EXIT,
    TOP_LEVEL,
    Phase,
    children_of,
    phase,
    phase_satisfied_by_state,
)

__all__ = ["Slot", "Board", "fold_board", "phase_status", "PhaseStatus",
           "running_top", "focus_top"]

_OUTCOME_STATUS = {"ok": "done", "skipped": "skipped", "error": "error",
                   "aborted": "aborted"}
# What the aggregate over several clusters reports, strongest first.
_PRECEDENCE = ("running", "error", "aborted")


@dataclass(frozen=True, slots=True)
class Slot:
    status: str
    since: float | None = None
    duration_s: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PhaseStatus:
    """The aggregate over a phase's clusters."""

    status: str
    since: float | None
    duration_s: float | None
    clusters: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Board:
    # (phase id, cluster) -> slot; cluster "" = announced without one.
    slots: Mapping[tuple[str, str], Slot]
    # Phase ids in the order their latest event arrived.
    recency: tuple[str, ...]
    state: Mapping[str, str]
    alive: bool
    clusters: tuple[str, ...]


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fold_board(
    records: Iterable[Mapping[str, Any]],
    state: Mapping[str, str],
    *,
    alive: bool,
    clusters: Sequence[str] = (),
) -> Board:
    """Replay `records` (this run's `phase.enter` / `phase.exit`, in order)."""
    slots: dict[tuple[str, str], Slot] = {}
    recency: list[str] = []
    for rec in records:
        name = rec.get("event")
        pid = rec.get("phase")
        if name not in (PHASE_ENTER, PHASE_EXIT) or not isinstance(pid, str):
            continue
        cl = rec.get("cluster") or ""
        key = (pid, cl)
        data = rec.get("data") or {}
        detail = str(data.get("detail", "")) if isinstance(data, Mapping) else ""
        ts = _epoch(rec.get("ts"))
        if pid in recency:
            recency.remove(pid)
        recency.append(pid)
        if name == PHASE_ENTER:
            # A parent entering again (t1 on the next iteration, t2a on the
            # next cluster) discards the steps its previous entry left behind.
            for child in children_of(pid):
                for k in [k for k in slots if k[0] == child.id and k[1] in (cl, "")]:
                    del slots[k]
            slots[key] = Slot("running", since=ts, detail=detail)
            continue
        prior = slots.get(key)
        dur = rec.get("duration_ms")
        slots[key] = Slot(
            _OUTCOME_STATUS.get(str(rec.get("outcome")), "done"),
            since=prior.since if prior else ts,
            duration_s=dur / 1000.0 if isinstance(dur, (int, float)) else None,
            detail=detail or (prior.detail if prior else ""))
    if not alive:
        slots = {k: (Slot("aborted", v.since, v.duration_s, v.detail)
                     if v.status == "running" else v) for k, v in slots.items()}
    return Board(slots, tuple(recency), dict(state), alive, tuple(clusters))


def _slots_of(board: Board, pid: str) -> dict[str, Slot]:
    return {cl: s for (p, cl), s in board.slots.items() if p == pid}


def _aggregate(statuses: Sequence[str]) -> str:
    for s in _PRECEDENCE:
        if s in statuses:
            return s
    if all(s == "skipped" for s in statuses):
        return "skipped"
    return "done"


def phase_status(board: Board, p: Phase) -> PhaseStatus:
    """One phase's status over its clusters, events first, state file second."""
    own = _slots_of(board, p.id)
    if not own:
        # A step announced without its parent scope (the blueprint runs before
        # t2a's first cluster scope opens) still says the phase is under way.
        kid = [s.status for c in children_of(p.id) for s in _slots_of(board, c.id).values()]
        live = [k for k in kid if k in ("running", "aborted", "error")]
        if live:
            return PhaseStatus(_aggregate(live), None, None, {})
        done = phase_satisfied_by_state(p, dict(board.state))
        return PhaseStatus("done" if done else "pending", None, None, {})
    whole = own.get("")
    per = {cl: s for cl, s in own.items() if cl}
    if p.per_cluster and per:
        names = board.clusters or tuple(per)
        per_status = {cl: (per[cl].status if cl in per
                           else (whole.status if whole else "pending"))
                      for cl in names}
        vals = list(per_status.values())
        if "pending" in vals and any(v != "pending" for v in vals):
            # Between clusters: some finished, the next has not entered yet.
            agg = "running" if board.alive else "aborted"
            if "error" in vals:
                agg = "error"
        else:
            agg = _aggregate(vals) if "pending" not in vals else "pending"
        stamps = [t for t in (per[c].since for c in per) if t is not None]
        since = min(stamps) if stamps else None
        dur = sum(s.duration_s or 0.0 for s in per.values()) or None
        return PhaseStatus(agg, since, dur, per_status)
    s = whole or next(iter(own.values()))
    return PhaseStatus(s.status, s.since, s.duration_s, {})


def running_top(board: Board) -> Phase | None:
    """The top-level phase currently running: the latest in run order among
    those whose aggregate is `running`."""
    hit: Phase | None = None
    for p in TOP_LEVEL:
        if phase_status(board, p).status == "running":
            hit = p
    return hit


def focus_top(board: Board) -> Phase | None:
    """The phase the sub-step line describes: the running one, else the one
    whose event arrived last (what just ended, or where the run stopped), else
    the first the state file does not mark done."""
    cur = running_top(board)
    if cur is not None:
        return cur
    for pid in reversed(board.recency):
        p = phase(pid)
        if p is None:
            continue
        top = phase(p.parent) if p.parent else p
        if top is not None:
            return top
    # No event this run (the process was just launched, or died before its
    # first phase): the state file alone says where the run stands — the first
    # phase it does not mark done.
    return next((p for p in TOP_LEVEL if phase_status(board, p).status != "done"), None)
