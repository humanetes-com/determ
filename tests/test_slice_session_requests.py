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
- an unasked session stays `never_served` when another route has opened
  its units, and three such tries stay `never_served`. A row that
  recorded a body, or a row written before the opened and green fields,
  stays the re-buy it was.

Negative control: `request_allowance=0` (every request a turn, the
behaviour before) never asks the body.
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
        measure_suite=lambda raw, _role=None: (green if (root / "src" / "lib.rs").read_text() != "BASE"
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
    assert out.outcome == "accepted" and out.carried_body is True
    assert "(8 turn(s) of budget remain.)" in sent[-1]
    # Negative control: every request a turn. The eight requests spend the
    # budget and the body is never asked for.
    ctrl, ctrl_sent = _run(tmp_path / "ctrl", [_REQUEST] * 8 + [_BODY], allowance=0)
    assert ctrl.turns == 8 and ctrl.model_calls == 8
    assert ctrl.judged_turns == 0 and ctrl.outcome == "no_change"
    assert ctrl.carried_body is False


def test_a_request_past_the_allowance_is_a_turn_so_the_loop_ends(tmp_path: Path):
    out, _sent = _run(tmp_path, [], allowance=3, max_turns=2)
    assert out.requests == 5 and out.model_calls == 5 and out.turns == 2


def test_a_session_that_wrote_no_body_is_unanswered_and_unmeasured(tmp_path: Path):
    """The recorded shape: seven requests, then notes and no body."""
    out, _sent = _run(tmp_path, [_REQUEST] * 7 + [_NOTES], allowance=7)
    assert out.outcome == "no_change" and out.judged_turns == 0
    assert out.capped_turns == 0
    assert out.unanswered and out.unmeasured and out.carried_body is False
    assert _outcome(RouteRunReport(model_calls=out.model_calls,
                                   unmeasured=out.unmeasured), 0) is RunOutcome.UNMEASURED
    # Control: bodies that do not compile are an answer, graded as before.
    bad, _s = _run(tmp_path / "bad", [_BODY, _NOTES], allowance=7, compiles=False)
    assert bad.compile_failures == 1 and bad.answered_turns == 1
    assert bad.carried_body is True
    assert not bad.unanswered and not bad.unmeasured
    assert _outcome(RouteRunReport(model_calls=bad.model_calls), 0) is RunOutcome.FLAT


_LOC = FnLocator("src/lib.rs", "fmt")


def _session_run(bodies: str = "b9") -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label="session[1 test(s), 1 unit(s)]",
                    targets=frozenset({_LOC}), target_tests=frozenset({"t_fmt"}),
                    bodies_sha=bodies)


def _row(outcome: RunOutcome, started: str, *, kind: RouteKind = RouteKind.SESSION,
         bodies: str, delta: int = 0,
         kept: tuple[tuple[str, str], ...] = (),
         opened: tuple[tuple[str, str], ...] | None = None,
         newly_green: tuple[str, ...] | None = None,
         carried_body: bool | None = None) -> RouteLedgerRow:
    return RouteLedgerRow(
        kind=kind, label="session[1 test(s), 1 unit(s)]",
        targets=(("src/lib.rs", "fmt"),), bodies_sha=bodies, oracle_fp="o",
        outcome=outcome, run_id="r", iteration=1, cluster="c", started=started,
        minutes=5.0, model_calls=8, target_tests=("t_fmt",),
        net_pass_delta=delta, kept=kept, opened=opened,
        newly_green=newly_green, carried_body=carried_body)


def _reason(rows: list[RouteLedgerRow]) -> str:
    hit = schedule([_session_run()], rows, oracle_fp="o", hollow={})
    return [*hit.ordered, *hit.deferred][0].reason


def test_an_unasked_session_stays_never_served_when_another_route_opened_the_unit():
    """Plan sentence: a session that carried no body is never_served, weight
    2, even when another route opened the unit, and three such tries stay
    never_served. Under a budget it opens before the per-fn re-fill.

    The other route's row is what makes the unit served. A ledger that
    holds only the session row ranks never_served because the unit itself
    was never opened, which is a different fact."""
    opened = _row(RunOutcome.FLAT, "2026-10-14T18:00", kind=RouteKind.FILE,
                  bodies="bfile")
    one = [opened, _row(RunOutcome.UNMEASURED, "2026-10-14T19:15", bodies="b1")]
    assert _reason(one) == "never_served"
    three = [opened, *[
        _row(RunOutcome.UNMEASURED, f"2026-10-14T19:{h}:00", bodies=f"b{h}")
        for h in (20, 30, 40)]]
    assert _reason(three) == "never_served"
    # A body that was kept is still a miss. So is a compile failure, which
    # records that a reply carried a body and kept nothing.
    assert _reason([opened, _row(
        RunOutcome.FLAT, "2026-10-14T19:15", bodies="bk",
        kept=(("src/lib.rs", "fmt"),))]) == "rebuy_flat"
    assert _reason([opened, _row(
        RunOutcome.FLAT, "2026-10-14T19:15", bodies="bc", carried_body=True,
        opened=(("src/lib.rs", "fmt"),), newly_green=())]) == "rebuy_flat"
    # The shape a row has when it recorded what it opened and what it
    # greened and both are empty, and it has no bit: not a try.
    recorded = [opened, _row(
        RunOutcome.FLAT, "2026-10-14T19:15", bodies="br",
        opened=(("src/lib.rs", "fmt"),), newly_green=())]
    assert _reason(recorded) == "never_served"
    # The same identity (the bodies sha matches) is the same rank. The
    # outcome alone would call it a flat re-buy.
    same = RouteRun(
        kind=RouteKind.SESSION, label="session[1 test(s), 1 unit(s)]",
        targets=frozenset({_LOC}), target_tests=frozenset({"t_fmt"}),
        bodies_sha="br")
    assert schedule([same], recorded, oracle_fp="o", hollow={},
                    budget_minutes=200).ordered[0].reason == "never_served"
    # A row written before those fields keeps the outcome it recorded.
    assert _reason([opened, _row(
        RunOutcome.FLAT, "2026-10-14T19:15", bodies="bl")]) == "rebuy_flat"
    # A later row that paid the test does not force never_served.
    paid = [*one, _row(RunOutcome.CASHED, "2026-10-14T20:00",
                       kind=RouteKind.PER_FN, bodies="bp", delta=1,
                       newly_green=("t_fmt",))]
    assert _reason(paid) == "served"
    # A graded body between unanswered tries is still the miss.
    graded = _row(RunOutcome.FLAT, "2026-10-14T19:25", bodies="bg",
                  kept=(("src/lib.rs", "fmt"),))
    assert _reason([opened, three[1], graded, three[2], three[3]]) == "rebuy_flat"
    per = RouteRun(kind=RouteKind.PER_FN, label="per-fn re-fill",
                   targets=frozenset({_LOC}),
                   target_tests=frozenset({"t_fmt"}), bodies_sha="p")
    sched = schedule([per, _session_run()], recorded, oracle_fp="o",
                     hollow={}, budget_minutes=200)
    assert [s.run.kind for s in sched.ordered] == [RouteKind.SESSION, RouteKind.PER_FN]
    assert sched.ordered[0].reason == "never_served" and sched.ordered[0].weight == 2
    assert sched.ordered[1].reason == "served" and sched.ordered[1].weight == 1
    assert not sched.deferred
