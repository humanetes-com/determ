"""A reasoning-capped session reply is an answer dropped, not a decision.

Run `3014175-d98ed09e` (2026-09-08, cluster `stable`, iter-3 fill): session
#3's turn 0 was cut at 120,003 reasoning chars (`llm.stream_meta` stop
`think_cap`, follow-up `fn_blocks 0`, 43 chars of empty tags); the harness
read the empty change set as the model declining, ended the session
`no_change` after one of eight turns, and the ledger graded the slice FLAT
— a served re-buy for the next fill's ordering. The plan's sentences:

- an empty follow-up after a capped turn does not end the session while
  turns remain: the turn is re-asked with the answer-from-your-
  deliberation instruction and the budget line;
- a session whose only judged landing came from a capped reply, or whose
  every reply was capped, is graded UNMEASURED, not FLAT, and the next
  schedule ranks it as never served.

Negative control: a transport that records nothing (`call_reading=None`)
still ends the session on the empty reply, as before.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import _outcome
from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    schedule,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.reasoning_cap import (
    CallCapReading,
    observe_call,
    read_call,
    record_followup_meta,
    record_stream_meta,
)
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_FMT = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RECS = {_FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                        container="impl fmt::Display for Report",
                        sig_text="fn fmt(&self) -> fmt::Result",
                        body_text="BASE")}
_BODY = '<FN name="fmt" block="impl fmt::Display for Report">write!(f, "x")</FN>'


def _reading(passed: set[str], failed: set[str]) -> TestStateReading:
    p, f = frozenset(passed), frozenset(failed)
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=p, failed=f,
        pass_count=len(p), fail_count=len(f)),))


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("BASE")
    return tmp_path


def _run(root: Path, replies: list[str], readings: list[CallCapReading | None] | None,
         *, measure, max_turns: int = 8):
    calls: list[list[dict]] = []
    seq = list(replies)
    caps = list(readings or [])

    def ask(messages: list[dict]) -> str:
        calls.append(list(messages))
        return seq.pop(0) if seq else ""

    def splice(path, container, name, body, where, **k):
        path.write_text(body)
        return True, "", 0

    def bodies():
        return {_FMT: (root / "src" / "lib.rs").read_text()}

    ports = SessionPorts(
        ask_turns=ask, splice=splice, check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=measure, read_source=lambda p: None,
        enumerate_bodies=bodies, answer_pulls=lambda r, conversation="": None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        call_reading=(lambda: caps[len(calls) - 1] if readings is not None else None)
        if readings is not None else None)
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_fmt",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=max_turns)
    return out, calls


_CAPPED_EMPTY = CallCapReading(capped=True, followup=True, followup_fn_blocks=0)
_CAPPED_BODY = CallCapReading(capped=True, followup=True, followup_fn_blocks=1)
_CLEAN = CallCapReading(capped=False, followup=False, followup_fn_blocks=None)


def test_an_empty_capped_reply_is_reasked_and_the_session_goes_on(tmp_path: Path):
    root = _tree(tmp_path)
    base = _reading({"t_keep"}, {"t_fmt"})
    green = _reading({"t_keep", "t_fmt"}, set())
    out, calls = _run(root, ["", _BODY], [_CAPPED_EMPTY, _CLEAN],
                      measure=lambda raw: green if (root / "src" / "lib.rs").read_text() != "BASE" else base)
    assert out.outcome == "accepted"
    assert out.turns == 2 and out.model_calls == 2
    assert out.capped_turns == 1 and out.judged_turns == 1
    assert out.judged_capped_turns == 0 and not out.unmeasured
    reask = calls[1][-1]["content"]
    assert "ran out of reasoning budget" in reask
    assert "Do not restart the analysis" in reask
    assert "7 turn(s) of budget remain" in reask


def test_without_a_cap_reading_an_empty_reply_still_ends_the_session(tmp_path: Path):
    """Negative control: the transport records nothing, so the harness
    cannot tell a capped reply from a decline and ends as before."""
    root = _tree(tmp_path)
    base = _reading({"t_keep"}, {"t_fmt"})
    out, calls = _run(root, ["", _BODY], None, measure=lambda raw: base)
    assert out.outcome == "no_change" and out.turns == 1 and len(calls) == 1
    assert out.capped_turns == 0 and not out.unmeasured


def test_every_reply_capped_and_empty_ends_unmeasured_after_the_budget(tmp_path: Path):
    root = _tree(tmp_path)
    base = _reading({"t_keep"}, {"t_fmt"})
    out, calls = _run(root, ["", "", ""], [_CAPPED_EMPTY] * 3,
                      measure=lambda raw: base, max_turns=3)
    assert out.outcome == "no_change" and out.turns == 3 and len(calls) == 3
    assert out.capped_turns == 3 and out.judged_turns == 0
    assert out.unmeasured
    assert _outcome(RouteRunReport(model_calls=3, unmeasured=True), 0) is RunOutcome.UNMEASURED


def test_a_session_whose_only_landing_is_a_capped_guess_is_unmeasured_unless_it_cashed(
        tmp_path: Path):
    """Session #7 of the same fill: a capped turn 0 whose forced follow-up
    carried an `is` body that special-cased one TypeId — judged net -1,
    reverted. That landing is not a measured refusal; a landing from a
    completed deliberation is."""
    root = _tree(tmp_path)
    base = _reading({"t_keep"}, {"t_fmt"})
    worse = _reading(set(), {"t_fmt", "t_keep"})
    out, _calls = _run(root, [_BODY, ""], [_CAPPED_BODY, _CLEAN],
                       measure=lambda raw: worse if (root / "src" / "lib.rs").read_text() != "BASE" else base)
    assert out.judged_turns == 1 and out.judged_capped_turns == 1
    assert out.unmeasured
    assert _outcome(RouteRunReport(model_calls=2, reverted=frozenset({FnLocator("src/lib.rs", "fmt")}),
                                   unmeasured=True), 0) is RunOutcome.UNMEASURED
    # A capped landing that CASHED is measured positive evidence.
    assert _outcome(RouteRunReport(model_calls=2, unmeasured=True), 3) is RunOutcome.CASHED
    # Control: the same landing from an uncapped reply is a measured refusal.
    (root / "src" / "lib.rs").write_text("BASE")
    out2, _c = _run(root, [_BODY, ""], [_CLEAN, _CLEAN],
                    measure=lambda raw: worse if (root / "src" / "lib.rs").read_text() != "BASE" else base)
    assert out2.judged_turns == 1 and out2.judged_capped_turns == 0
    assert not out2.unmeasured
    assert _outcome(RouteRunReport(model_calls=2, reverted=frozenset({FnLocator("src/lib.rs", "fmt")})),
                    0) is RunOutcome.REVERTED


def test_an_unmeasured_row_ranks_the_slice_never_served_not_rebuy() -> None:
    loc = FnLocator("src/lib.rs", "fmt")
    run = RouteRun(kind=RouteKind.SESSION, label="session[1 test(s)]",
                   targets=frozenset({loc}), target_tests=frozenset({"t_fmt"}),
                   bodies_sha="b")

    def row(outcome: RunOutcome) -> RouteLedgerRow:
        return RouteLedgerRow(
            kind=run.kind, label=run.label, targets=(("src/lib.rs", "fmt"),),
            bodies_sha="b", oracle_fp="o", outcome=outcome, run_id="r",
            iteration=1, cluster="c", started="", minutes=1.0, model_calls=1,
            target_tests=("t_fmt",))

    unmeasured = schedule([run], [row(RunOutcome.UNMEASURED)], oracle_fp="o", hollow={})
    assert unmeasured.ordered[0].reason == "never_served"
    flat = schedule([run], [row(RunOutcome.FLAT)], oracle_fp="o", hollow={})
    assert flat.ordered[0].reason == "rebuy_flat"
    # Nor "served under another identity": a moved body after an
    # unmeasured row is a new try, not a served one.
    moved = RouteRun(kind=run.kind, label=run.label, targets=run.targets,
                     target_tests=run.target_tests, bodies_sha="b2")
    assert schedule([moved], [row(RunOutcome.UNMEASURED)], oracle_fp="o",
                    hollow={}).ordered[0].reason == "never_served"
    # A moved body after a FLAT row on the same route and tests is the
    # unit's re-buy (service is per unit; the route's own keep moved it).
    assert schedule([moved], [row(RunOutcome.FLAT)], oracle_fp="o",
                    hollow={}).ordered[0].reason == "rebuy_flat"
    assert RouteLedgerRow.from_dict(row(RunOutcome.UNMEASURED).to_dict()) is not None


def test_observe_call_reads_the_rows_of_its_own_call() -> None:
    with observe_call() as rows:
        record_stream_meta({"prompt_sha": "a", "finish_reason": "stop",
                            "stop": "think_cap", "followup": True,
                            "content_chars": 43, "reasoning_chars": 120003})
        record_followup_meta({"prompt_sha": "a", "finish_reason": "stop",
                              "stop": None, "fn_blocks": 0, "content_chars": 43})
    reading = read_call(rows)
    assert reading == CallCapReading(capped=True, followup=True, followup_fn_blocks=0)
    assert reading.empty_after_cap
    with observe_call() as clean:
        record_stream_meta({"prompt_sha": "b", "finish_reason": "stop",
                            "stop": "closed", "followup": False})
    assert read_call(clean) == CallCapReading(capped=False, followup=False,
                                              followup_fn_blocks=None)
    with observe_call() as guess:
        record_stream_meta({"prompt_sha": "c", "stop": "think_cap", "followup": True})
        record_followup_meta({"prompt_sha": "c", "fn_blocks": 1})
    assert read_call(guess).capped and not read_call(guess).empty_after_cap
    # Nothing recorded: no reading, never "uncapped".
    with observe_call() as silent:
        pass
    assert read_call(silent) is None
    # Rows recorded outside an observer do not leak into a later one.
    record_stream_meta({"prompt_sha": "d", "stop": "think_cap"})
    with observe_call() as later:
        pass
    assert later == []
