"""A session sends the turn 0 it was charged for, and a requested context
never ends it.

Run `3366333-dfeac80c` (2026-10-29), fill 1: the packer charged the session
131,982 chars against a capacity of 132,000 on the slice it packed; the
session opened on that slice grown by five installable bodies and carrying
sections the charge does not price, and sent turn 0 at 166,299. After its
first result turn the model asked for two whole files; the next turn would
have been 257,194 chars against a 244,224 bound and the session ended there,
2 judged turns of 8. Over `logs/events.jsonl` on that day, 160 of 196
sessions sent a turn 0 above 132,000.

Claims (invented shape):
- the turn 0 is measured as built and rebuilt at each narrower rendering
  threshold until it fits; when none fits there is no prompt to send;
- a requested answer that would leave the transcript no room for the reply
  and result after it is not sent, the model is told so, and the session
  continues; a request that fits is answered as before.
Negative controls: the turn 0 built once at the planned threshold is over
capacity, and so is the narrowest rendering; the over-bound answer, sent,
is the transcript stop.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct import slice_session as ss
from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice, fit_turn0
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_THRESHOLDS = (36_000, 18_000, 9_000, 4_500)


def _build(t: int) -> str:
    # A fixed part plus one file rendered at threshold t.
    return "p" * 50_000 + "f" * (t * 3)


def test_turn0_is_rebuilt_narrower_until_it_fits() -> None:
    text, used = fit_turn0(_build, 36_000, _THRESHOLDS, 110_000)
    assert used == 18_000 and len(text) <= 110_000
    # Negative control: built once at the planned threshold.
    assert len(_build(36_000)) > 110_000


def test_nothing_is_sent_when_none_fits_and_no_capacity_builds_once() -> None:
    calls: list[int] = []

    def counted(t: int) -> str:
        calls.append(t)
        return _build(t)

    assert fit_turn0(counted, 36_000, _THRESHOLDS, 10_000) is None
    assert calls == [36_000, 18_000, 9_000, 4_500]
    # Negative control: the narrowest rendering was built and is over.
    assert len(_build(4_500)) > 10_000
    calls.clear()
    assert fit_turn0(counted, 36_000, _THRESHOLDS, 0) == (_build(36_000), 36_000)
    assert calls == [36_000]


_U = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RECS = {_U: FnRecord(file="src/lib.rs", fn_name="fmt",
                      container="impl fmt::Display for Report",
                      sig_text="fn fmt(&self) -> fmt::Result", body_text="BASE")}


def _reading() -> TestStateReading:
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=frozenset({"t_keep"}),
        failed=frozenset({"t_fmt"}), pass_count=1, fail_count=1),))


def _run(root: Path, *, answer: str, bound: int):
    (root / "src").mkdir(parents=True)
    (root / "src" / "lib.rs").write_text("BASE")
    seen: list[str] = []
    events: list[tuple[str, dict]] = []

    def ask(messages: list[dict]) -> str:
        seen.append(str(messages[-1].get("content") or ""))
        if len(seen) == 1:
            return '<REQUEST kind="file" arg="src/lib.rs"/>'
        return "no change"

    ports = SessionPorts(
        ask_turns=ask,
        splice=lambda *a, **k: (True, "", 0),
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=lambda raw, _role=None: _reading(),
        read_source=lambda p: None,
        enumerate_bodies=lambda: {_U: "BASE"},
        answer_pulls=lambda r, conversation="": answer if "<REQUEST" in r else None,
        log=lambda m: None,
        emit=lambda ev, data=None, **k: events.append((ev, data or {})),
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_fmt",), units=(_U,)),
        records=_RECS, turn0="turn0", max_turns=4,
        max_transcript_chars=bound)
    return seen, events


def test_an_answer_that_would_cross_the_bound_is_refused_and_the_session_goes_on(
        tmp_path: Path) -> None:
    bound = 100_000
    big = "x" * (bound - ss._RESULT_ROOM)  # leaves less than the room
    seen, events = _run(tmp_path, answer=big, bound=bound)
    assert len(seen) == 2, "the session asked again after the refusal"
    assert "was not sent" in seen[1] and big not in seen[1]
    assert not [e for e, _ in events if e == "phase_c.session.transcript_stop"]
    assert [d["over_bound"] for e, d in events
            if e == "phase_c.session.turn" and d.get("kind") == "request"] == [True]
    # Negative control: sent, the answer would have needed the room the
    # bound reserves for the reply and its result.
    assert len("turn0") + len(big) + ss._RESULT_ROOM > bound


def test_an_answer_that_fits_is_sent(tmp_path: Path) -> None:
    seen, events = _run(tmp_path, answer="the file", bound=100_000)
    assert len(seen) == 2 and "the file" in seen[1]
    assert [d["over_bound"] for e, d in events
            if e == "phase_c.session.turn" and d.get("kind") == "request"] == [False]
