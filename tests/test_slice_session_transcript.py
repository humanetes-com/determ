"""A session's transcript is a budget the session itself keeps.

Run `366282-7c585f74` (2026-09-15, cluster `nightly`, fill 3) is the
instance. The second packed session's turn 2 came back `finish_reason
length` with 135,396 chars of content and no change set; the loop appended
it like any other turn, answered the request it carried, and sent turn 3 at
296,189 chars — past `SINGLE_CALL_CHAR_LIMIT` (293,376). The client that
sends the turns does not fit them (`ask_client.ask_turns` warns past 90 %
and sends the list, because the whole conversation is what the model must
see, and its docstring says the session route owns its own turn budget);
nothing in the session owned it. Four turns, 45 minutes, no bodies.

The plan's sentences, and what this module asserts:

- the bound is checked BEFORE a call, so no turn is ever SENT over it;
- the session ends on its best judged state, the way the wall clock ends
  it — a reply already in the transcript is not trimmed or dropped, because
  the user turn that answers a request refers to the turn that made it;
- `max_transcript_chars=0` is the behaviour before the bound, and is the
  negative control: the same session sends the over-bound turn.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_FMT = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RECS = {_FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                        container="impl fmt::Display for Report",
                        sig_text="fn fmt(&self) -> fmt::Result",
                        body_text="BASE")}
#: A reply the server cut at its token ceiling: long, and carrying a
#: request rather than a change set, which is the turn the loop continues
#: on.
_RUNAWAY = "<REQUEST kind=\"full_error\" arg=\"\"/>" + ("x" * 4_000)


def _reading() -> TestStateReading:
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=frozenset({"t_keep"}),
        failed=frozenset({"t_fmt"}), pass_count=1, fail_count=1),))


def _run(root: Path, *, bound: int, max_turns: int = 8):
    (root / "src").mkdir(parents=True)
    (root / "src" / "lib.rs").write_text("BASE")
    sent: list[int] = []
    events: list[tuple[str, dict]] = []

    def ask(messages: list[dict]) -> str:
        sent.append(sum(len(str(m.get("content") or "")) for m in messages))
        return _RUNAWAY

    ports = SessionPorts(
        ask_turns=ask,
        splice=lambda *a, **k: (True, "", 0),
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=lambda raw: _reading(),
        read_source=lambda p: None,
        enumerate_bodies=lambda: {_FMT: "BASE"},
        answer_pulls=lambda r, conversation="": "here is the error",
        log=lambda m: None,
        emit=lambda ev, data=None, **k: events.append((ev, data or {})),
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_fmt",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=max_turns,
        max_transcript_chars=bound)
    return out, sent, events


def test_no_turn_is_sent_over_the_bound(tmp_path: Path) -> None:
    out, sent, events = _run(tmp_path, bound=1_000)
    assert sent, "the first turn is sent — the bound is not a refusal to open"
    assert max(sent) <= 1_000
    assert len(sent) == 1, "the runaway reply closed the budget in one turn"
    stops = [d for ev, d in events if ev == "phase_c.session.transcript_stop"]
    assert len(stops) == 1
    assert stops[0]["bound"] == 1_000 and stops[0]["chars"] > 1_000
    assert out.turns == 1


def test_without_the_bound_the_over_limit_turn_goes_out(tmp_path: Path) -> None:
    """The negative control, which is also what the live session did."""
    out, sent, events = _run(tmp_path, bound=0, max_turns=3)
    assert len(sent) == 3
    assert max(sent) > 1_000, "a later turn carried the runaway reply"
    assert not [ev for ev, _ in events if ev == "phase_c.session.transcript_stop"]


def test_the_bound_does_not_cut_a_session_that_stays_under_it(tmp_path: Path) -> None:
    """A budget nothing reaches changes nothing: the same session spends
    its turns exactly as it does with the bound off."""
    out, sent, _ = _run(tmp_path, bound=10_000_000, max_turns=3)
    control, control_sent, _ = _run(tmp_path / "control", bound=0, max_turns=3)
    assert len(sent) == len(control_sent) == 3
    assert out.turns == control.turns == 3
