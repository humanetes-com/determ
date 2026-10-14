"""A session's turn budget counts attempts, and a session that wrote
nothing is no verdict on its slice.

Run `1430425-193a8f28` (2026-10-14, cluster `stable`, fill 6): a one-test
session opened with the failing test's units editable. Replies 1-7 were
request tags (four of them for a file already in its prompt), each charged
as one of eight turns; reply 8, told one turn was left, was deliberation
and notes with no function block. The session ended `no_change` with zero
judged turns, was graded FLAT because no reply had been reasoning-capped,
and the next fill deferred the one-test slice on that test as
`rebuy_flat`.

The plan's sentences, and what this module asserts:

- a request reply within the allowance spends no turn of the budget, so a
  body sent after the requests is still asked and judged; past the
  allowance a request is a turn, so the loop ends whatever the answerer
  does;
- a session whose replies carried no function block is unanswered and
  graded UNMEASURED; one whose bodies failed to compile answered;
- the schedule reads an unanswered try as no verdict, but a run of
  `unpaid_grace` of them as a test's latest session tries is one unpaid
  try, so the slice cannot re-open at full weight every fill.

Negative controls: `request_allowance=0` (every request a turn, the
behaviour before) never asks the body; `unpaid_grace=0` re-offers the
slice after any number of unanswered tries.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.fill_routes import RouteRunReport
from pipeline_core.application.reconstruct.fill_scheduler import _outcome
from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    run_slice_session,
)
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    schedule,
)
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_FMT = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RECS = {_FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                        container="impl fmt::Display for Report",
                        sig_text="fn fmt(&self) -> fmt::Result",
                        body_text="BASE")}
_BODY = '<FN name="fmt" block="impl fmt::Display for Report">write!(f, "x")</FN>'
_REQUEST = '<REQUEST kind="file" arg="src/lib.rs"/>'
_NOTES = "<NOTES>the wrapper hides the payload's type</NOTES>"


def _reading(passed: set[str], failed: set[str]) -> TestStateReading:
    p, f = frozenset(passed), frozenset(failed)
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=p, failed=f,
        pass_count=len(p), fail_count=len(f)),))


def _run(root: Path, replies: list[str], *, allowance: int, max_turns: int = 8,
         compiles: bool = True):
    (root / "src").mkdir(parents=True)
    (root / "src" / "lib.rs").write_text("BASE")
    seq = list(replies)
    sent: list[str] = []
    base = _reading({"t_keep"}, {"t_fmt"})
    green = _reading({"t_keep", "t_fmt"}, set())

    def ask(messages: list[dict]) -> str:
        sent.append(str(messages[-1].get("content") or ""))
        return seq.pop(0) if seq else _REQUEST

    def splice(path, container, name, body, where, **k):
        path.write_text(body)
        return True, "", 0

    ports = SessionPorts(
        ask_turns=ask, splice=splice,
        check=lambda: (0, "", []) if compiles else (1, "error[E0308]", ["E0308"]),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=lambda raw: (green if (root / "src" / "lib.rs").read_text() != "BASE"
                                   else base),
        read_source=lambda p: None,
        enumerate_bodies=lambda: {_FMT: (root / "src" / "lib.rs").read_text()},
        # An answerer that never stops answering: termination must not
        # depend on it.
        answer_pulls=lambda r, conversation="": "the file follows" if "<REQUEST" in r else None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_fmt",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=max_turns,
        request_allowance=allowance)
    return out, sent


def test_requests_within_the_allowance_leave_the_budget_for_the_body(tmp_path: Path):
    out, sent = _run(tmp_path, [_REQUEST] * 8 + [_BODY], allowance=8)
    assert out.requests == 8 and out.model_calls == 9
    assert out.turns == 1 and out.judged_turns == 1
    assert out.outcome == "accepted"
    assert "(8 turn(s) of budget remain.)" in sent[-1]
    # Negative control: every request a turn. The eight requests spend the
    # budget and the body is never asked for.
    ctrl, ctrl_sent = _run(tmp_path / "ctrl", [_REQUEST] * 8 + [_BODY], allowance=0)
    assert ctrl.turns == 8 and ctrl.model_calls == 8
    assert ctrl.judged_turns == 0 and ctrl.outcome == "no_change"


def test_a_request_past_the_allowance_is_a_turn_so_the_loop_ends(tmp_path: Path):
    out, _sent = _run(tmp_path, [], allowance=3, max_turns=2)
    assert out.requests == 5 and out.model_calls == 5 and out.turns == 2


def test_a_session_that_wrote_no_body_is_unanswered_and_unmeasured(tmp_path: Path):
    """The recorded shape: seven requests, then notes and no body."""
    out, _sent = _run(tmp_path, [_REQUEST] * 7 + [_NOTES], allowance=7)
    assert out.outcome == "no_change" and out.judged_turns == 0
    assert out.capped_turns == 0
    assert out.unanswered and out.unmeasured
    assert _outcome(RouteRunReport(model_calls=out.model_calls,
                                   unmeasured=out.unmeasured), 0) is RunOutcome.UNMEASURED
    # Control: bodies that do not compile are an answer, graded as before.
    bad, _s = _run(tmp_path / "bad", [_BODY, _NOTES], allowance=7, compiles=False)
    assert bad.compile_failures == 1 and bad.answered_turns == 1
    assert not bad.unanswered and not bad.unmeasured
    assert _outcome(RouteRunReport(model_calls=bad.model_calls), 0) is RunOutcome.FLAT


_LOC = FnLocator("src/lib.rs", "fmt")


def _session_run(bodies: str = "b9") -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label="session[1 test(s), 1 unit(s)]",
                    targets=frozenset({_LOC}), target_tests=frozenset({"t_fmt"}),
                    bodies_sha=bodies)


def _row(outcome: RunOutcome, started: str, *, kind: RouteKind = RouteKind.SESSION,
         bodies: str, delta: int = 0) -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=kind, label="session[1 test(s), 1 unit(s)]",
        targets=(("src/lib.rs", "fmt"),), bodies_sha=bodies, oracle_fp="o",
        outcome=outcome, run_id="r", iteration=1, cluster="c", started=started,
        minutes=5.0, model_calls=8, target_tests=("t_fmt",),
        net_pass_delta=delta)


def _reason(rows: list[RouteLedgerRow], grace: int = 3) -> str:
    return schedule([_session_run()], rows, oracle_fp="o", hollow={},
                    unpaid_grace=grace).ordered[0].reason


def test_unanswered_tries_are_no_verdict_until_the_grace_then_one_unpaid_try():
    one = [_row(RunOutcome.UNMEASURED, "2026-10-14T19:15", bodies="b1")]
    assert _reason(one) == "never_served"
    three = [_row(RunOutcome.UNMEASURED, f"2026-10-14T1{h}:00", bodies=f"b{h}")
             for h in (5, 6, 7)]
    assert _reason(three) == "rebuy_flat"
    # Negative control: no bound, the slice re-opens at full weight forever.
    assert _reason(three, grace=0) == "never_served"
    # A graded try between them breaks the run of unanswered tries.
    mixed = [three[0], _row(RunOutcome.FLAT, "2026-10-14T15:30", bodies="bf"),
             three[1], three[2]]
    assert _reason(mixed) == "rebuy_flat"  # the FLAT row is the latest graded
    paid = [*three, _row(RunOutcome.CASHED, "2026-10-14T18:00",
                         kind=RouteKind.PER_FN, bodies="bp", delta=1)]
    assert _reason(paid) != "rebuy_flat"


def test_the_recorded_flat_grade_deferred_the_slice_and_unmeasured_does_not():
    """The fill-6 row on the recorded test, as it was graded (FLAT) and as
    an unanswered session is now graded (UNMEASURED)."""
    flat = [_row(RunOutcome.FLAT, "2026-10-14T19:15", bodies="b1")]
    assert _reason(flat) == "rebuy_flat"
    unmeasured = [_row(RunOutcome.UNMEASURED, "2026-10-14T19:15", bodies="b1")]
    assert _reason(unmeasured) == "never_served"
