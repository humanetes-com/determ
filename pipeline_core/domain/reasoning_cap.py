"""Fold of `llm.stream_meta` / `llm.followup_meta` over one fill window.

`LLMController.run` already emits `finish_reason` / `stop` / `followup` /
`reasoning_chars` per reply. Nothing folded them, so a constructor call that
hit `finish_reason=length` at 67 376 reasoning chars (run `2932-cf516410`,
2026-08-26, 5 of 9 fill replies) was only visible by grepping the event
log after the fact. The count belongs beside the landed-change audit —
replies do not exist yet on the pre-fill `iter N: cargo test` line.

The followup rows are a second kind in the SAME window. A `stream_meta` row
with `followup=True` says the main stream was cut and a forced completion
ran; it says nothing about whether THAT completion finished. Measured
2026-08-26 on run `82157-476ddc16`: all 8 file-first replies were the
followup's, each written under `request_max` = the content budget (8192)
with no thinking reserve, and the row on record carried the main stream's
`finish_reason`. `summarize_followup_cut` is the count that was missing —
followups that were themselves cut at `length`.

A larger thinking budget is a later, GPU-owned decision. This module only
COUNTS. The live caveat from that constructor trace: diagnosis arrived in
the first screen and extra tokens drifted, so a zero length-hit count is
not the reading this exists to take.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.fill_context import current_route

_active: list[dict[str, Any]] | None = None

# Per-call observers. A caller that needs the rows of ITS OWN call — the
# slice session reading whether the reply it just got was reasoning-capped
# — binds one around the call (`observe_call`); every row recorded inside
# appends to it as well as to the fill window. A ContextVar for the same
# reason as `_units_var`: fill routes run on worker threads, and each
# thread's stack scopes its own calls.
_observers_var: ContextVar[tuple[list[dict[str, Any]], ...]] = ContextVar(
    "reasoning_cap_observers", default=())

# The units (`file::fn`) the call currently in flight is serving, bound by
# the fill route around its ask so the controller's row lands already
# joined. A ContextVar because fill routes run on worker threads: each
# thread's binding scopes its own calls while every row still appends into
# the one window the iteration opened. Empty = an unjoined call (a
# retrospective, a classifier), whose rows no per-unit fold reads.
_units_var: ContextVar[tuple[str, ...]] = ContextVar(
    "reasoning_cap_units", default=())

# Marker key on rows recorded by `record_followup_meta`. Stream rows carry no
# `kind` so their dict stays byte-identical to the `llm.stream_meta` payload.
_KIND = "kind"
_FOLLOWUP = "followup"

# Calls that serve the pipeline's own records rather than a fill or a
# diagnosis — the self-note extractor is the one on record. Its ladder asks
# twice on an empty first reply by design (`extract_self_note`), so its
# empties are the ladder working, not the budget failing, and admitting them
# would let the run-level cut rate (`CutRate`) read the notes store's
# declines as a starved model. Bound by the caller around its ask; the
# health fold skips rows recorded inside.
_bookkeeping_var: ContextVar[bool] = ContextVar(
    "reasoning_cap_bookkeeping", default=False)


def is_reasoning_capped(meta: Mapping[str, Any]) -> bool:
    """A reply that ended because thinking ran out, not because the model
    stopped. `followup` is the recovery that fires on `length` /
    `think_cap`; counting it too matches the 2932 bar (5 of 9)."""
    return (str(meta.get("finish_reason") or "") == "length"
            or str(meta.get("stop") or "") == "think_cap"
            or bool(meta.get("followup")))


def _is_followup_row(row: Mapping[str, Any]) -> bool:
    return str(row.get(_KIND) or "") == _FOLLOWUP


def is_cut(meta: Mapping[str, Any]) -> bool:
    """A main-stream call that returned no decided answer: reasoning ran out
    (:func:`is_reasoning_capped`, which includes the forced followup that
    fires on an empty main reply) or the reply on record is empty.

    `stop == "closed"` with no `finish_reason` is the StopDetector ending a
    FINISHED structure early and is not a cut — 83 such rows on record, all
    with content (events.jsonl through 2026-09-21)."""
    return is_reasoning_capped(meta) or int(meta.get("content_chars") or 0) == 0


@contextmanager
def bookkeeping_call() -> Iterator[None]:
    """Mark the enclosed model call(s) as the pipeline's own bookkeeping, so
    the run-level cut rate does not count them (see `_bookkeeping_var`)."""
    token = _bookkeeping_var.set(True)
    try:
        yield
    finally:
        _bookkeeping_var.reset(token)


def is_bookkeeping_call() -> bool:
    """Whether the call in flight right now is bound inside
    :func:`bookkeeping_call`. `record_stream_meta` reads this to decide
    whether to fold the row into the in-process run-level fold; the
    controller also stamps it onto the `llm.stream_meta` event it writes to
    disk, so a reader replaying events.jsonl (the monitor, a report) can
    exclude the same rows by the same fact instead of reconstructing it from
    a caller string."""
    return _bookkeeping_var.get()


@dataclass(frozen=True, slots=True)
class CallMark:
    """One main-stream call as the cut rate sees it."""

    route: str            # fill route kind bound around the call, "" outside a route
    cut: bool             # :func:`is_cut`
    empty: bool           # the reply on record is empty
    prompt_tokens: int    # the server's prompt count (0 when not reported)


@dataclass(frozen=True, slots=True)
class CutRate:
    """The share of the last `window` main-stream calls that returned no
    decided answer, and whether it has reached `share_pct`.

    Why a run-level fold exists at all: the per-call grade (a capped reply
    is UNMEASURED, not FLAT) is correct and is exactly what hides the rate —
    every capped call is quietly re-bought. Computed on events.jsonl
    2026-08-10 → 2026-09-21: 12,002 calls, 1,810 cut (15.1%); 132/506 on
    08-13 under a half-sized thinking budget, 3/117 on 08-27 after it was
    doubled, back to 18/115 on 09-21 as prompts grew. Each time the finding
    took sessions. A reading of a run whose calls are that cut measures the
    budget, not reconstruction.

    `share_pct <= 0` never fires. No verdict before `min_calls` calls; after
    that the share is over the last `min(calls, window)`. Pure: `add`
    returns a new value, so the fold replays over recorded rows unchanged."""

    window: int
    share_pct: float
    min_calls: int
    marks: tuple[CallMark, ...] = ()
    seen: int = 0

    def add(self, mark: CallMark) -> CutRate:
        keep = max(1, self.window)
        return CutRate(self.window, self.share_pct, self.min_calls,
                       (*self.marks, mark)[-keep:], self.seen + 1)

    @property
    def cut(self) -> int:
        return sum(1 for m in self.marks if m.cut)

    @property
    def share(self) -> float:
        return 100.0 * self.cut / len(self.marks) if self.marks else 0.0

    @property
    def fired(self) -> bool:
        return (self.share_pct > 0 and self.seen >= self.min_calls
                and bool(self.marks) and self.share >= self.share_pct)

    def by_route(self) -> dict[str, tuple[int, int]]:
        """route -> (cut, calls) over the window; "" is reported as "other"."""
        out: dict[str, tuple[int, int]] = {}
        for m in self.marks:
            key = m.route or "other"
            c, n = out.get(key, (0, 0))
            out[key] = (c + int(m.cut), n + 1)
        return out

    def describe(self) -> str:
        """The breakdown every health line and the stop print."""
        routes = ", ".join(f"{r} {c}/{n}" for r, (c, n)
                           in sorted(self.by_route().items()))
        empty = sum(1 for m in self.marks if m.empty)
        largest = max((m.prompt_tokens for m in self.marks), default=0)
        return (f"calls={len(self.marks)} (of {self.seen} seen) "
                f"cut={self.cut} empty={empty} share={self.share:.1f}% "
                f"[{routes or 'none'}] largest-prompt={largest} tok")


def mark_of(meta: Mapping[str, Any], route: str) -> CallMark:
    return CallMark(route=route, cut=is_cut(meta),
                    empty=int(meta.get("content_chars") or 0) == 0,
                    prompt_tokens=int(meta.get("prompt_tokens") or 0))


def summarize_capped(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """`(capped, total)` over a fill window's `llm.stream_meta` rows.

    Followup rows share the window and are EXCLUDED from both counts. The
    audit line reads `N of M fill replies reasoning-capped`, where M is one
    row per call; a followup is a second completion of the same call and it
    runs on exactly the calls that were capped, so admitting it would raise
    the denominator by one per capped call and read 5 of 9 as 5 of 14."""
    stream = [r for r in rows if not _is_followup_row(r)]
    return sum(1 for r in stream if is_reasoning_capped(r)), len(stream)


def is_truncated_followup(row: Mapping[str, Any]) -> bool:
    """A followup row whose reply is INCOMPLETE, by either mechanism.

    Two of them, and they are not the same event: the server ran out of
    tokens (`finish_reason == "length"`) or this client stopped reading
    mid-reasoning at the char cap (`stop == "think_cap"`, `_consume` breaks
    the stream). :func:`summarize_followup_cut` counts the first alone and
    keeps that name — a server budget is what sizing `request_max` answers,
    and a client break is not. Salvage has no such distinction:
    `LLMController._followup` salvages on BOTH, because both leave a reply
    whose last element is unfinished, so the fold that reports salvage must
    cover both or it undercounts every think-capped followup. Invisible while
    `PHASE_C_FOLLOWUP_THINK` is off (nothing arms the cap on the followup),
    wrong the moment it is switched on — which is when the reading matters.
    `stop == "closed"` is the StopDetector ending a FINISHED structure and is
    neither."""
    return (_is_followup_row(row)
            and (str(row.get("finish_reason") or "") == "length"
                 or str(row.get("stop") or "") == "think_cap"))


def summarize_followup_cut(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many forced followups in the window ran out of SERVER tokens
    (`finish_reason == "length"`) — the completion that wrote the reply on
    record was itself budget-bound, so the reply is a truncation of a
    truncation. A client-side `think_cap` break is a different event and is
    not counted here (see :func:`is_truncated_followup`). Stream rows are
    ignored: their `finish_reason` is the MAIN stream's, which
    `summarize_capped` already counts."""
    return sum(1 for r in rows
               if _is_followup_row(r) and str(r.get("finish_reason") or "") == "length")


def summarize_followup_salvage(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """`(cut, salvaged)` over the followup rows in the window.

    `cut` is every TRUNCATED followup (:func:`is_truncated_followup` — server
    budget or client think-cap break), which is a wider population than
    :func:`summarize_followup_cut`'s server-budget count, because the
    controller salvages both. `salvaged` is how many of those
    still yielded a structurally closed prefix that was spliced — a cut whose
    damage was the LAST element only. The pair is the reading the discard
    rule needs: `cut` alone cannot tell "the completion budget is too small
    and we are losing whole replies" from "it is too small and we are losing
    one body of twenty-five". A row without `salvaged_chars` (written before
    the field) counts as not salvaged, which is what it was."""
    truncated = sum(1 for r in rows if is_truncated_followup(r))
    salvaged = sum(1 for r in rows
                   if is_truncated_followup(r) and int(r.get("salvaged_chars") or 0) > 0)
    return truncated, salvaged


def current_units() -> tuple[str, ...]:
    """The `file::fn` labels bound by the innermost `serving_units`; empty
    outside one. Read by the fill reading so a mid-fill measurement names
    the unit(s) whose gate took it."""
    return tuple(_units_var.get())


@contextmanager
def serving_units(units: Iterable[str]) -> Iterator[None]:
    """Bind the `file::fn` unit labels the enclosed model call(s) serve.

    Rows recorded inside carry them under `units`, which is the join the
    FLAT grading needs: run `358833-aa15d93c` iter 3 (2026-09-01) wrote 8
    FLAT ledger rows in a batch whose audit line said `3 of 4 fill replies
    reasoning-capped` — the count and the grades lived in the same window
    with no way to say WHICH units' tries were the truncated ones."""
    token = _units_var.set(tuple(dict.fromkeys(units)))
    try:
        yield
    finally:
        _units_var.reset(token)


def _record(data: Mapping[str, Any]) -> None:
    active = _active
    observers = _observers_var.get()
    if active is None and not observers:
        return
    row = dict(data)
    units = _units_var.get()
    if units:
        row["units"] = units
    if active is not None:
        active.append(row)
    for rows in observers:
        rows.append(row)


@contextmanager
def observe_call() -> Iterator[list[dict[str, Any]]]:
    """Collect the rows the calls made inside record, for the caller that
    made them. Independent of the fill window: a session outside any
    window (a test, a route run on its own) still reads its own call."""
    rows: list[dict[str, Any]] = []
    token = _observers_var.set((*_observers_var.get(), rows))
    try:
        yield rows
    finally:
        _observers_var.reset(token)


@dataclass(frozen=True, slots=True)
class CallCapReading:
    """What the transport recorded about ONE call.

    `capped`: the main stream ended because reasoning ran out
    (:func:`is_reasoning_capped`). `followup`: a forced completion ran.
    `followup_fn_blocks`: how many `<FN` blocks that completion produced
    (None when no follow-up row was recorded). The slice session reads it
    after a reply that carried no change set and no request: on run
    `3014175-d98ed09e` (2026-09-08) session #3's turn 0 was cut at
    120,003 reasoning chars, its follow-up carried 43 chars and no body,
    and the session ended `no_change` after one of eight turns — an
    answer dropped by the cap, indistinguishable from the model declining
    without this reading."""

    capped: bool
    followup: bool
    followup_fn_blocks: int | None

    @property
    def empty_after_cap(self) -> bool:
        """A capped call whose completion carried no fn block: the
        deliberation was cut before a decision and nothing was salvaged."""
        return self.capped and not (self.followup_fn_blocks or 0)


def read_call(rows: Sequence[Mapping[str, Any]]) -> CallCapReading | None:
    """Fold the rows of one observed call. None when the call recorded no
    main-stream row (a transport that records nothing — a fake, a
    non-streaming backend), so a caller cannot mistake silence for an
    uncapped reply."""
    stream = [r for r in rows if not _is_followup_row(r)]
    if not stream:
        return None
    followups = [r for r in rows if _is_followup_row(r)]
    blocks: int | None = None
    if followups:
        blocks = sum(int(r.get("fn_blocks") or 0) for r in followups)
    return CallCapReading(
        capped=any(is_reasoning_capped(r) for r in stream),
        followup=bool(followups) or any(bool(r.get("followup")) for r in stream),
        followup_fn_blocks=blocks)


def record_stream_meta(data: Mapping[str, Any]) -> None:
    """Append to the fill window and observers (no-op outside them) and
    fold into the process's run-level cut rate (`llm_health`), which is
    idle until the TDD driver arms it. Every main-stream row the controller
    writes passes here, so no route can reach the model without being
    counted. Append-only; CPython `list.append` is atomic, so a
    worker-thread fill records into the same window the iteration opened."""
    _record(data)
    if not _bookkeeping_var.get():
        _observe_health(mark_of(data, current_route()[0]))


def record_followup_meta(data: Mapping[str, Any]) -> None:
    """Same window, same atomic append, tagged `kind: followup` so the two
    folds above can tell the forced completion's row from the main
    stream's. The tag is added here rather than by the caller so a row can
    never land untagged and inflate `summarize_capped`'s denominator."""
    _record({**data, _KIND: _FOLLOWUP})


def run_all_replies_capped(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Every JOINED main-stream reply recorded in this window was
    reasoning-capped — at least one reply, all of them capped.

    The run-level twin of :func:`units_all_replies_capped`, for the caller
    that grades a whole route run rather than a unit. Unjoined rows are
    skipped for the reason the module docstring gives: a retrospective or a
    classifier call is not the run's answer, and a route whose fill call was
    truncated would otherwise read as measured because the diagnosis call
    that followed it finished. Followup rows are the second completion of a
    call the main row already reports.

    False on no joined rows at all, so a transport that records nothing and
    a route whose calls this window did not see both keep the grade they
    have. Run `366282-7c585f74` iter 2 (2026-09-15, cluster `nightly`) is
    what this answers: three pair runs, two of them a single fill call cut
    at 120,000 reasoning chars whose forced completion carried 517 and 599
    chars, one re-emitting the bodies already on the tree — and all three
    rowed as graded tries the next schedule ranked on.
    """
    joined = [r for r in rows
              if not _is_followup_row(r) and r.get("units")]
    return bool(joined) and all(is_reasoning_capped(r) for r in joined)


def units_all_replies_capped(
    rows: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """The units (`file::fn`) whose EVERY joined main-stream reply in this
    window was reasoning-capped — at least one reply, all of them capped.

    A truncated call that re-emits the old body is not a measured refusal:
    grading such a unit FLAT stands the re-buy detector on a truncated
    measurement, the same overclaim the FLAT arm's own comment forbids for
    reverts. A unit with any UNCAPPED reply keeps its grade — the model got
    a full turn and returned the same body. Unjoined rows (no `units`) and
    followup rows (a second completion of the same call, whose cap
    `is_reasoning_capped` already folds into the main row) count toward no
    unit."""
    total: dict[str, int] = {}
    capped: dict[str, int] = {}
    for r in rows:
        if _is_followup_row(r):
            continue
        for u in r.get("units") or ():
            total[u] = total.get(u, 0) + 1
            if is_reasoning_capped(r):
                capped[u] = capped.get(u, 0) + 1
    return frozenset(u for u, n in total.items() if capped.get(u, 0) == n)


@contextmanager
def collect_stream_meta() -> Iterator[list[dict[str, Any]]]:
    """Bind a row list for the duration of one fill pass."""
    global _active
    rows: list[dict[str, Any]] = []
    prev = _active
    _active = rows
    try:
        yield rows
    finally:
        _active = prev


# -- the run-level cut stop ------------------------------------------------
#
# One fold per process. Idle (`_health is None`) until the TDD driver arms it
# at cluster entry with knobs read once, like every other stop; every
# main-stream row folds into it from `record_stream_meta`, so no route can
# reach the model uncounted and no route reads it — the only consumers are
# the stop and the `[llm/health]` line. On the first fired fold it logs
# `HEALTH ‼ LLM_CUT_STOP`, emits `llm.cut_rate_stop`, and leaves a pending
# trip that `graceful_stop.check_graceful_stop` polls (the FILL_PHASE_STOP
# path: the next unit boundary exits cleanly, no sentinel file left behind).
# This module does not import `graceful_stop` (it imports this one).

_health_lock = threading.Lock()
_health: CutRate | None = None
_health_log: Callable[[str], None] | None = None
_health_emit: Callable[..., None] | None = None
_health_meta: dict[str, Any] = {}
_health_tripped = False
_health_pending = False


@dataclass(frozen=True, slots=True)
class CutStopKnobs:
    """`LLM_CUT_STOP_SHARE` (percent, 0 disables), `LLM_CUT_STOP_WINDOW`,
    `LLM_CUT_STOP_MIN_CALLS`. Decided 2026-09-21: 4% is the level of the
    fixed-budget regime right after 2026-08-27 (08-27..08-29: 14 of 772,
    1.8%); anything above it is a defect to fix. The minimum exists because a
    run makes ~10 calls an hour (run `450977-5f48b68a`: 27 in 2.5 h), so a
    100-call window alone would rarely fill; at 25 calls one cut stops the
    run, which is the intent."""

    share_pct: float = 4.0
    window: int = 100
    min_calls: int = 25

    @property
    def enabled(self) -> bool:
        return self.share_pct > 0

    def describe(self) -> str:
        if not self.enabled:
            return "cut-stop=off"
        return (f"cut-stop={self.share_pct:g}%/{self.window} "
                f"(min {self.min_calls})")


def cut_stop_knobs_from_env(source: Mapping[str, str] | None = None) -> CutStopKnobs:
    """The three knobs; a garbage value falls back to its default rather
    than disabling the stop, so a typo cannot silently remove it.

    `source` defaults to the process environment (the pipeline's own read,
    at cluster entry). The monitor is a separate process that never
    inherits the pipeline's environment and reads no dotenv, so it passes
    its own parse of `.env` here instead — same parsing, same defaults,
    same clamps, so the two readers cannot drift apart on the same file."""
    d = CutStopKnobs()
    env = source if source is not None else os.environ

    def _num(key: str, default: float) -> float:
        raw = (env.get(key) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return CutStopKnobs(
        share_pct=max(0.0, _num("LLM_CUT_STOP_SHARE", d.share_pct)),
        window=max(1, int(_num("LLM_CUT_STOP_WINDOW", d.window))),
        min_calls=max(1, int(_num("LLM_CUT_STOP_MIN_CALLS", d.min_calls))))


def arm_cut_stop(knobs: CutStopKnobs, *,
                 log: Callable[[str], None] | None = None,
                 emit: Callable[..., None] | None = None,
                 context: Mapping[str, Any] | None = None) -> None:
    """Arm (or re-arm, at the next cluster's entry) the process's cut stop.
    Calls already folded are kept: the window is the process's last calls,
    not the cluster's. A disabled knob still folds, so the health line
    reports the rate with the stop off."""
    global _health, _health_log, _health_emit, _health_meta
    with _health_lock:
        marks = _health.marks if _health is not None else ()
        seen = _health.seen if _health is not None else 0
        _health = CutRate(knobs.window, knobs.share_pct, knobs.min_calls,
                          marks[-max(1, knobs.window):], seen)
        _health_log = log
        _health_emit = emit
        _health_meta = {**(context or {}), "share_pct": knobs.share_pct,
                        "window": knobs.window, "min_calls": knobs.min_calls}


def _observe_health(mark: CallMark) -> None:
    global _health, _health_tripped, _health_pending
    with _health_lock:
        if _health is None:
            return
        _health = _health.add(mark)
        if _health_tripped or not _health.fired:
            return
        _health_tripped = True
        _health_pending = True
        reading = _health
        log, emit, meta = _health_log, _health_emit, dict(_health_meta)
    line = (f"  HEALTH ‼ LLM_CUT_STOP {reading.describe()} reached "
            f"LLM_CUT_STOP_SHARE={reading.share_pct:g}% — the run's model "
            "calls are being cut; stopping at the next unit boundary. Fix the "
            "budget (or the prompt size) and `bash continue.sh` resumes.")
    if log is not None:
        log(line)
    if emit is not None:
        emit("llm.cut_rate_stop", data={
            **meta, "calls": len(reading.marks), "seen": reading.seen,
            "cut": reading.cut, "share": round(reading.share, 2),
            "by_route": {r: {"cut": c, "calls": n}
                         for r, (c, n) in reading.by_route().items()}})


def cut_stop_poll() -> bool:
    """True once, on the first poll after the stop fired; the caller
    (`check_graceful_stop`) then takes the process-wide stop event."""
    global _health_pending
    with _health_lock:
        if not _health_pending:
            return False
        _health_pending = False
        return True


def cut_stop_tripped() -> bool:
    with _health_lock:
        return _health_tripped


def health_reading() -> CutRate | None:
    """The fold as it stands; None while unarmed."""
    with _health_lock:
        return _health


def reset_cut_stop_for_tests() -> None:
    global _health, _health_log, _health_emit, _health_meta
    global _health_tripped, _health_pending
    with _health_lock:
        _health = None
        _health_log = _health_emit = None
        _health_meta = {}
        _health_tripped = _health_pending = False
