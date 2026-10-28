"""An owed function gets an opened writer, a test that died in another stub
does not grade a body, and a feature's rules reach the functions only its
examples run.

Run `14141-9a904822` (2026-10-08, cluster `stable`) is the instance for all
three: `src/ensure.rs::render` was graded FLAT while 12 of its 15
convicting tests died inside the stubbed Display `src/error.rs::fmt`, then
sat in no candidate (fills 3-4), reported-not-routed (5-6) and deferred
`pair_wait` (7-8), and the prompt that wrote it carried no rule of
`ensure!` because a macro feature names no function."""
from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    Feature,
    ObservableOutcome,
    Rule,
)
from pipeline_core.domain.behaviour_book import AimedUnit, render_rules, rules_for_units
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    SweepView,
    body_sha,
    classify,
    sig_sha_of,
)
from pipeline_core.domain.fill_schedule import (
    RouteKind,
    RouteLedgerRow,
    RouteRun,
    RunOutcome,
    schedule,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.schedule_invariants import decided_from_schedule, violations

_RENDER = FnLocator("src/ensure.rs", "render")
_FMT = FnLocator("src/error.rs", "fmt")
_DEBUG = FnLocator("src/fmt.rs", "debug")


def _pair() -> RouteRun:
    return RouteRun(kind=RouteKind.PAIR, label="pair src/error.rs::fmt + src/fmt.rs::debug",
                    targets=frozenset({_FMT, _DEBUG}),
                    target_tests=frozenset({"test_atom", "test_altdebug"}),
                    files=("src/error.rs", "src/fmt.rs"), bodies_sha="pair")


def _file() -> RouteRun:
    return RouteRun(kind=RouteKind.FILE, label="file src/ensure.rs",
                    targets=frozenset({_RENDER}), target_tests=frozenset({"test_atom"}),
                    files=("src/ensure.rs",), bodies_sha="f")


def _rows() -> list[RouteLedgerRow]:
    p = _pair()
    prior = RouteRun(kind=p.kind, label=p.label, targets=p.targets,
                     target_tests=p.target_tests, files=p.files, bodies_sha="old")
    return [RouteLedgerRow(kind=prior.kind, label=prior.label,
                           targets=tuple(sorted(prior.targets)), bodies_sha="old",
                           oracle_fp="o", outcome=RunOutcome.REVERTED,
                           target_tests=tuple(sorted(prior.target_tests)))]


def test_a_pair_wait_deferred_writer_of_an_owed_function_opens() -> None:
    """Fills 7-8: `file src/ensure.rs` deferred `pair_wait` behind the pair.
    Owed, it opens as `starved` and the pair still opens. Control: with
    nothing owed the same inputs keep today's deferral."""
    runs = [_file(), _pair()]
    today = schedule(runs, _rows(), oracle_fp="o", hollow={})
    assert {s.run.label: s.reason for s in today.deferred} == {
        "file src/ensure.rs": "pair_wait"}
    owed = schedule(runs, _rows(), oracle_fp="o", hollow={}, due={_RENDER})
    opened = {s.run.label: s for s in owed.ordered}
    assert set(opened) == {"file src/ensure.rs", _pair().label}
    assert opened["file src/ensure.rs"].starved_writer == ("src/ensure.rs::render",)
    assert "starved: opened for owed src/ensure.rs::render" in owed.render()


def test_an_owed_function_with_no_opened_writer_is_slice_unit_starved() -> None:
    runs = [_file(), _pair()]
    today = schedule(runs, _rows(), oracle_fp="o", hollow={})
    found = violations(decided_from_schedule(today), hollow={},
                       due={_RENDER})
    assert [v.kind for v in found] == ["slice_unit_starved"]
    assert "file src/ensure.rs (pair_wait)" in found[0].detail
    owed = schedule(runs, _rows(), oracle_fp="o", hollow={}, due={_RENDER})
    assert not violations(decided_from_schedule(owed), hollow={},
                          due={_RENDER})
    nobody = schedule([_pair()], _rows(), oracle_fp="o", hollow={})
    lone = violations(decided_from_schedule(nobody), hollow={},
                      due={_RENDER})
    assert lone and "no candidate run writes it" in lone[0].detail


_KEY = LedgerKey(project="anyhow", cluster="stable", file="src/ensure.rs",
                 fn_name="render")


def _landed() -> LedgerEntry:
    return LedgerEntry(key=_KEY, body_sha=body_sha(("left: right:",)),
                       bodies=("left: right:",), sig_sha=sig_sha_of(("fn render",)),
                       layout_fp="fp", state=BodyState.LANDED, oracle_fp="o")


def _views(held: dict[str, frozenset[FnLocator]]) -> tuple[SweepView, SweepView]:
    tests = frozenset({"test_atom", "test_as", "test_unary"})
    before = SweepView(frozenset(), tests, True, 0)
    after = SweepView(frozenset(), tests, True, 0, held=held)
    return before, after


def test_tests_that_died_in_another_stub_do_not_grade_the_body() -> None:
    """Iter 3: `render`'s convicting tests died in the stubbed Display.
    All held elsewhere → UNMEASURED. Control: the same sweep without the
    held reading grades FLAT (today's row)."""
    tests = {"test_atom", "test_as", "test_unary"}
    held_all = {t: frozenset({_FMT}) for t in tests}
    before, after = _views(held_all)
    assert classify(_landed(), attributed=tests, before=before, after=after,
                    body_now_sha="")[0] is BodyState.UNMEASURED
    before, after = _views({})
    assert classify(_landed(), attributed=tests, before=before, after=after,
                    body_now_sha="")[0] is BodyState.FLAT


def test_one_observing_test_still_grades_flat_and_a_hold_at_the_unit_itself_judges() -> None:
    tests = {"test_atom", "test_as", "test_unary"}
    mixed = {"test_atom": frozenset({_FMT}), "test_as": frozenset({_FMT})}
    before, after = _views(mixed)
    assert classify(_landed(), attributed=tests, before=before, after=after,
                    body_now_sha="")[0] is BodyState.FLAT
    own = {t: frozenset({_RENDER}) for t in tests}
    before, after = _views(own)
    assert classify(_landed(), attributed=tests, before=before, after=after,
                    body_now_sha="")[0] is BodyState.FLAT


def test_a_green_test_that_now_dies_in_another_stub_refutes_the_landing() -> None:
    """Only a test that was already failing is excused by a hold elsewhere.
    A test green before the landing that dies in a different stub after it
    reached that stub through the new body: the landing lost it, REFUTED.
    Red before the fix: it graded FLAT and the wrong body stayed."""
    before = SweepView(frozenset({"test_green"}), frozenset({"test_atom"}), True, 0)
    after = SweepView(frozenset(), frozenset({"test_green", "test_atom"}), True, 0,
                      held={"test_green": frozenset({_FMT})})
    state, delta = classify(_landed(), attributed={"test_green", "test_atom"},
                            before=before, after=after, body_now_sha="")
    assert state is BodyState.REFUTED and ("test_green", "lost") in delta


def _ensure_suite() -> BehaviourSuite:
    vs = BehaviourSpec("t1", "", "ensure!(a == b)",
                       ObservableOutcome.display("Condition failed: `a == b` (1 vs 2)"),
                       subkind=BehaviourSubKind.OUTCOME,
                       exercises=frozenset({"src/ensure.rs::render", "src/error.rs::fmt"}))
    long_ = BehaviourSpec("t2", "", "ensure!(long == long)",
                          ObservableOutcome.display("Condition failed: `long == long`"),
                          subkind=BehaviourSubKind.OUTCOME, klass=BehaviourClass.BOUNDED_RESOURCE,
                          exercises=frozenset({"src/ensure.rs::render", "src/error.rs::fmt"}))
    msg = BehaviourSpec("m1", "", "Error::msg(\"a\").to_string()",
                        ObservableOutcome.display("a"), subkind=BehaviourSubKind.OUTCOME,
                        exercises=frozenset({"src/error.rs::fmt", "src/error.rs::msg"}))
    rule = Rule("rule::ensure", "A false comparison appends (Debug(left) vs Debug(right)).",
                ("t1", "t2"), "ensure!", ())
    return BehaviourSuite("stable", (vs, long_, msg), features=(
        Feature("ensure!", "macro", ("t1", "t2"), (rule,)),
        Feature("Error::msg", "fn", ("m1",), ())))


def test_a_macro_features_rule_reaches_the_function_only_its_examples_run() -> None:
    """Control: Display `fmt` runs under both features and gets no
    `ensure!` rule; a unit with no file gets no exclusive-set aim (the
    declaration aims alone, today's behaviour)."""
    specs = _ensure_suite().specs
    assert [r.feature for r in rules_for_units(
        specs, [AimedUnit("render", "", "src/ensure.rs")])] == ["ensure!"]
    assert not rules_for_units(specs, [AimedUnit("fmt", "impl Display for Error",
                                                 "src/error.rs")])
    assert not rules_for_units(specs, [AimedUnit("render", "")])


def test_a_rule_shows_its_bounded_resource_example() -> None:
    suite = _ensure_suite()
    by_id = {s.behaviour_id: s for s in suite.specs}
    text = render_rules(rules_for_units(suite.specs, [AimedUnit("render", "", "src/ensure.rs")]),
                        by_id)
    assert "Condition failed: `long == long`" in text
    assert "(1 vs 2)" not in text


def _session() -> RouteRun:
    return RouteRun(kind=RouteKind.SESSION, label="session[1 test(s), 3 unit(s)] #0",
                    targets=frozenset({_RENDER, _FMT}),
                    target_tests=frozenset({"test_atom"}),
                    files=("src/ensure.rs", "src/error.rs"), bodies_sha="s")


def test_a_function_owed_on_the_session_rung_opens_a_session_writer() -> None:
    """The ladder: once a unit route has answered for `render` under the
    test's evidence, the next ask is the slice session. An opened unit route
    writing `render` does not cover it; the deferred session opens.
    Control: owed on the unit rung, the opened file run covers it."""
    from dataclasses import replace

    from pipeline_core.domain.fill_schedule import _cover_due
    file_run = schedule([_file()], [], oracle_fp="o", hollow={}).ordered[0]
    session = replace(
        schedule([_session()], [], oracle_fp="o", hollow={}).ordered[0],
        deferred=True, reason="kind_cap")
    opened, still = _cover_due([file_run], [session], frozenset({_RENDER}),
                               {_RENDER: "session"})
    assert {s.run.label for s in opened} == {_file().label, _session().label}
    assert not still
    opened_u, still_u = _cover_due([file_run], [session], frozenset({_RENDER}),
                                   {_RENDER: "unit"})
    assert [s.run.label for s in opened_u] == [_file().label]
    assert [s.run.label for s in still_u] == [_session().label]


def test_a_held_session_owed_a_function_opens_after_its_holder_with_its_keeps_frozen(
        tmp_path) -> None:
    """A pair holds the only session that writes an owed function. The cover
    opens the session at its own position, after the pair, and it opens
    with the bodies the pair kept frozen. Controls: without the owed set the
    session waits `pair_wait` and the function is `slice_unit_starved`;
    with the pre-fix open rule (a session returned unchanged) the session
    opens free to rewrite the pair's kept body."""
    import pytest

    from pipeline_core.adapters.route_ledger_store import RouteLedgerStore
    from pipeline_core.application.reconstruct import fill_reading
    from pipeline_core.application.reconstruct import fill_scheduler as fsx
    from pipeline_core.application.reconstruct.fill_routes import RouteRunReport

    runs = [_session(), _pair()]
    held = schedule(runs, _rows(), oracle_fp="o", hollow={})
    assert {s.run.label: s.reason for s in held.deferred} == {
        _session().label: "pair_wait"}
    assert [v.kind for v in violations(decided_from_schedule(held), hollow={},
                                       due={_RENDER})] == ["slice_unit_starved"]

    owed = schedule(runs, _rows(), oracle_fp="o", hollow={}, due={_RENDER},
                    due_rungs={_RENDER: "session"})
    assert [s.run.label for s in owed.ordered] == [_pair().label, _session().label]
    assert not violations(decided_from_schedule(owed), hollow={}, due={_RENDER})

    seen: list[tuple[str, frozenset[FnLocator]]] = []

    class _Route:
        parallel = False

        def __init__(self, run: RouteRun) -> None:
            self.kind = run.kind
            self._run = run

        def plan(self) -> list[RouteRun]:
            return [self._run]

        def run(self, run: RouteRun, clock) -> RouteRunReport:
            seen.append((run.label, frozenset(run.frozen)))
            clock.may_claim()
            return RouteRunReport(model_calls=1, kept=frozenset(run.targets))

    def _fill(root) -> None:
        store = RouteLedgerStore(root)
        store.append("p", _rows())
        fill_reading.reset_last_comparison()
        seen.clear()
        n = iter(range(1_000_000))
        fsx.execute_fill(
            [_Route(_session()), _Route(_pair())], hollow={},
            bodies_sha=lambda _t: f"b{next(n)}",
            deps=fsx.FillExecutionDeps(
                log=lambda _m: None, emit=lambda *_a, **_k: None,
                check_stop=lambda _c: None, ledger=store,
                project="p", cluster="c", run_id="r", iteration=8,
                oracle_fp="o", scheduled=True, due=frozenset({_RENDER}),
                due_rungs={_RENDER: "session"}))

    fixed = tmp_path / "fixed"
    fixed.mkdir()
    _fill(fixed)
    assert [label for label, _f in seen] == [_pair().label, _session().label]
    assert seen[1][1] >= {_FMT, _DEBUG}

    real = fsx.withhold_landed
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fsx, "withhold_landed", lambda run, landed: (
            (run, frozenset()) if run.kind is RouteKind.SESSION
            else real(run, landed)))
        control = tmp_path / "control"
        control.mkdir()
        _fill(control)
    assert seen[1] == (_session().label, frozenset())