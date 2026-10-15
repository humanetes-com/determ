"""Assertion positions, the decided outcome rule, and the localized revert.

Run `273278-22529e44` (2026-09-29, cluster `stable`): the earlier-panic
judgment ordered panic sites by line number in any file. On a tree with stubs
every site is in `src/`, so a fill that let tests reach a still-stubbed fn
declared earlier in the same file (`src/context.rs` 43|50 -> 23) was graded a
regression; the sweep reverted ten landings by attribution and the score fell
from 37 to 19. The recorded pair below is that sweep's before/after capture
(the iter-1 checkpoint, and the same tree with the ten refuted rows' bodies
spliced), and the rows are the ledger's ten refuted rows.

The invented shapes pin the class the position model closes: a helper above
its callers, a closure defined before its call, one name in two binaries.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Sequence
from pathlib import Path

from pipeline_core.application.reconstruct import site_revert
from pipeline_core.application.reconstruct.site_revert import (
    SiteRevertPorts,
    SiteSweep,
    revert_site_regressions,
)
from pipeline_core.domain import body_ledger, slice_session, test_report
from pipeline_core.domain.assertion_position import (
    KeyedVerdicts,
    TestKey,
    TestOutcomes,
    assertion_positions,
    is_oracle_test_path,
    outcomes_of,
    own_assertion_lines,
)
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    SweepView,
    classify,
)
from pipeline_core.domain.cause_bisection import localize
from pipeline_core.domain.oracle_ledger import bare_test_name
from pipeline_core.domain.test_report import parse_test_output
from pipeline_core.domain.test_results import parse_failed_tests

_FIX = Path(__file__).resolve().parent / "fixtures" / "site_regression"


def _any_source(_rel: str) -> str:
    """An adversarial reader: every path reads as a file whose every line is
    inside some test's body. A position may still only exist where the
    panic is in the binary's own oracle file."""
    return "\n".join(["fn t() {"] + ["    assert!(x);"] * 2000 + ["}"])


def _view(text: str, positions: dict[TestKey, int]) -> SweepView:
    """The view of one capture. A failing name is keyed under the binary its
    injected position names, so the raw line-order control compares its
    positions over the same keys its verdicts sit under."""
    rep = parse_test_output(text)
    passed = frozenset(bare_test_name(t) for t in rep.passed)
    failed = frozenset(bare_test_name(t) for t in parse_failed_tests(text))
    where = {k.test: k.binary for k in positions}
    return SweepView(
        passed=passed, failed=failed, positions=positions,
        verdicts=KeyedVerdicts(
            passed=frozenset(TestKey("", t) for t in passed),
            failed=frozenset(TestKey(where.get(t, ""), t) for t in failed),
            positions=positions))


def _raw_line_order_positions(text: str) -> dict[TestKey, int]:
    """The pre-fix comparison's input: every failure's printed site, keyed by
    FILE and bare name, first section winning — line order over any file."""
    out: dict[TestKey, int] = {}
    for f in parse_test_output(text).failures:
        if f.panic_file and f.panic_line is not None:
            out.setdefault(TestKey(f.panic_file, bare_test_name(f.test_name)),
                           f.panic_line)
    return out


def _rows() -> list[LedgerEntry]:
    out = []
    for d in json.loads((_FIX / "stable_273278_refuted_rows.json").read_text()):
        out.append(LedgerEntry(
            key=LedgerKey("anyhow", "stable", d["file"], d["fn"]),
            body_sha="", bodies=(), sig_sha="", layout_fp="",
            state=BodyState.LANDED,
            verdict_delta=tuple(tuple(p) for p in d["verdict_delta"]),
            convicting=tuple(d.get("convicting") or ())))
    return out


def _grade(row: LedgerEntry, before: SweepView, after: SweepView) -> BodyState:
    attributed = set(row.convicting) | {t for t, _k in row.verdict_delta}
    state, _delta = classify(row, attributed=attributed, before=before,
                             after=after, body_now_sha="")
    return state


def test_the_recorded_stub_tree_has_no_position_and_refutes_nothing() -> None:
    """Plan sentence: on the recorded pair the new model yields no position
    for any site (all 160/142 are in `src/`) and grades all ten landings not
    refuted — the eight with `gained` proven, `anyhow_kind` and `format_err`
    (regressed only) flat. Negative controls: line order over raw sites finds
    the eleven `src/context.rs` moves, and the gain rule alone over those
    raw positions still refutes the two regressed-only landings."""
    before_t = (_FIX / "stable_273278_before.txt").read_text()
    after_t = (_FIX / "stable_273278_after.txt").read_text()
    assert assertion_positions(before_t, _any_source) == {}
    assert assertion_positions(after_t, _any_source) == {}
    before, after = _view(before_t, {}), _view(after_t, {})
    rows = _rows()
    assert len(rows) == 10
    graded = {(r.key.file, r.key.fn_name): _grade(r, before, after) for r in rows}
    assert BodyState.REFUTED not in graded.values()
    regressed_only = {(r.key.file, r.key.fn_name) for r in rows
                      if {k for _t, k in r.verdict_delta} == {"regressed"}}
    assert len(regressed_only) == 2
    assert all(graded[n] is BodyState.FLAT for n in regressed_only)
    assert sum(s is BodyState.PROVEN for s in graded.values()) == 8

    raw_b = _view(before_t, _raw_line_order_positions(before_t))
    raw_a = _view(after_t, _raw_line_order_positions(after_t))
    moved = [k for k, v in raw_b.positions.items()
             if k in raw_a.positions and raw_a.positions[k] < v
             and k.test in raw_b.failed and k.test in raw_a.failed]
    context_moves = [k for k in moved if k.binary == "src/context.rs"]
    assert len(context_moves) == 11
    raw_graded = {(r.key.file, r.key.fn_name): _grade(r, raw_b, raw_a) for r in rows}
    assert all(raw_graded[n] is BodyState.REFUTED for n in regressed_only)


# --- invented shapes ----------------------------------------------------------

_HELPER_ABOVE = """\
fn make_chain() -> u32 {
    assert!(ready());
    1
}

#[test]
fn test_a() {
    let c = make_chain();
    assert_eq!(c, 1);
    assert_eq!(c, 2);
}
"""


def test_a_helper_above_its_callers_has_no_position() -> None:
    lines = own_assertion_lines(_HELPER_ABOVE, "test_a")
    assert lines is not None
    assert 2 not in lines                       # the helper's assert
    assert {8, 9, 10} <= lines                  # the test's own lines


_CLOSURE = """\
#[test]
fn test_c() {
    let test = || Ok(check(1 | 2));
    assert_err(test, "a | b { }");
    let other = |x: u32| {
        assert!(x > 0);
    };
    other(3);
    fn inner() {
        assert!(false);
    }
    inner();
}
"""


def test_a_closure_or_nested_fn_inside_the_test_has_no_position() -> None:
    """The closure is written before its call; its line is not an earlier
    assertion of the test. Braces and pipes inside the string literal are
    not code."""
    lines = own_assertion_lines(_CLOSURE, "test_c")
    assert lines is not None
    assert {3, 5, 6, 7, 9, 10, 11}.isdisjoint(lines)
    assert {4, 8, 12} <= lines


def test_a_name_defined_twice_in_one_file_has_no_position() -> None:
    src = "mod a {\nfn t() {\n assert!(x);\n}\n}\nmod b {\nfn t() {\n assert!(y);\n}\n}\n"
    assert own_assertion_lines(src, "t") is None


def _capture(binary: str, test: str, line: int) -> str:
    return (f"     Running {binary} (target/debug/deps/x-0)\n\n"
            f"running 1 test\ntest {test} ... FAILED\n\nfailures:\n\n"
            f"---- {test} stdout ----\n\nthread '{test}' panicked at "
            f"{binary}:{line}:5:\nassertion failed\n\nfailures:\n    {test}\n\n"
            "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")


def test_one_name_in_two_binaries_is_keyed_apart() -> None:
    """Each binary's failure keeps its own key; a first-section-wins bare
    name would have given one of them the other's position."""
    text = (_capture("tests/test_a.rs", "t", 3)
            + _capture("tests/test_b.rs", "t", 4))
    pos = assertion_positions(text, _any_source)
    assert pos == {TestKey("tests/test_a.rs", "t"): 3,
                   TestKey("tests/test_b.rs", "t"): 4}


def test_a_panic_in_another_file_than_the_binary_has_no_position() -> None:
    text = _capture("tests/test_a.rs", "t", 3).replace(
        "panicked at tests/test_a.rs", "panicked at tests/common/mod.rs")
    assert assertion_positions(text, _any_source) == {}
    assert not is_oracle_test_path("src/context.rs")
    assert not is_oracle_test_path("tests/../src/lib.rs")
    assert is_oracle_test_path("tests/test_a.rs")


# --- the decided rule ----------------------------------------------------------

def _kv(binary: str = "tests/t.rs", **kw: object) -> KeyedVerdicts:
    return KeyedVerdicts.of_names(binary=binary, **kw)  # type: ignore[arg-type]


def test_the_decided_rule_on_mixed_outcomes() -> None:
    """Any lost refuses; earlier refuses only when nothing gained."""
    k = TestKey("tests/t.rs", "e")
    red = ("g", "e", "l")

    def _o(after_positions: dict[TestKey, int] | None = None,
           after_passed: tuple[str, ...] = (),
           after_failed: tuple[str, ...] = red) -> TestOutcomes:
        return outcomes_of(
            ("g", "e"), before=_kv(failed=red, positions={k: 10}),
            after=_kv(passed=after_passed, failed=after_failed,
                      positions=after_positions or {k: 10}))

    assert not _o().refuses
    assert _o(after_positions={k: 9}).refuses                       # earlier alone
    assert not _o(after_positions={k: 9}, after_passed=("g",),
                  after_failed=("e",)).refuses                      # gained + earlier
    lost = outcomes_of(("g",), before=_kv(passed=("g",)),
                       after=_kv(failed=("g",)))
    assert lost.refuses and lost.lost == frozenset({"g"})


def test_a_silent_death_refuses_whatever_was_gained() -> None:
    """A test that ran to its own verdict before and whose own process died
    with no panic after refuses, even beside a gain. A death already present
    before, or one that printed a panic (not in `aborted_silent`), is not an
    abort of this attempt. Negative control: the model without the abort
    clause (`aborted` empty) does not refuse the same outcome."""
    o = outcomes_of(("g", "d"), before=_kv(failed=("g", "d")),
                    after=_kv(passed=("g",), failed=("d",),
                              aborted_silent=("d",)))
    assert o.aborted == frozenset({"d"}) and o.gained == frozenset({"g"})
    assert o.refuses
    assert not TestOutcomes(gained=o.gained, lost=o.lost).refuses
    again = outcomes_of(("d",), before=_kv(failed=("d",), aborted=("d",)),
                        after=_kv(failed=("d",), aborted_silent=("d",)))
    assert not again.aborted and not again.refuses


def test_one_name_in_two_binaries_is_two_tests() -> None:
    """Run `14495-a26ad4ea` (2026-10-15): `tests/test_repr.rs::test_drop`
    passes, `tests/test_downcast.rs::test_drop` is red before and after, and
    another attributed test turns green. Nothing is lost and the gain
    stands. A copy that WAS green and now fails is still lost, whatever the
    other copy does. Negative control: the same readings collapsed onto bare
    names, as the name-set rule read them, put `test_drop` in passed-before
    and failed-after and refuse."""
    repr_, down = "tests/test_repr.rs", "tests/test_downcast.rs"
    before = KeyedVerdicts(
        passed=frozenset({TestKey(repr_, "test_drop")}),
        failed=frozenset({TestKey(down, "test_drop"), TestKey(down, "gain")}))
    after = KeyedVerdicts(
        passed=frozenset({TestKey(repr_, "test_drop"), TestKey(down, "gain")}),
        failed=frozenset({TestKey(down, "test_drop")}))
    o = outcomes_of(("test_drop", "gain"), before=before, after=after)
    assert o.lost == frozenset() and o.gained == frozenset({"gain"})
    assert not o.refuses
    broke = KeyedVerdicts(
        passed=frozenset({TestKey(down, "gain")}),
        failed=frozenset({TestKey(repr_, "test_drop"),
                          TestKey(down, "test_drop")}))
    assert outcomes_of(("test_drop", "gain"), before=before,
                       after=broke).lost == frozenset({"test_drop"})
    names_before_passed = {k.test for k in before.passed}
    names_after_failed = {k.test for k in after.failed}
    assert "test_drop" in names_before_passed & names_after_failed


# --- localization ------------------------------------------------------------

def test_bisection_finds_the_one_cause_among_four() -> None:
    probes: list[tuple[str, ...]] = []

    def moves_back(sub: Sequence[str]) -> bool:
        probes.append(tuple(sub))
        return "c" in sub

    assert localize(["a", "b", "c", "d"], moves_back) == ("c",)
    assert len(probes) == 4


def test_bisection_walks_both_halves_and_keeps_a_conjunction() -> None:
    assert localize(["a", "b", "c", "d"],
                    lambda s: "a" in s or "d" in s) == ("a", "d")
    # Neither half alone moves it back: the pair is the cause.
    assert localize(["a", "b"], lambda s: set(s) == {"a", "b"}) == ("a", "b")


class _Tree:
    """An invented tree of four landed units; unit `c` alone moves `t` from
    line 20 to line 10 of its own body."""

    def __init__(self) -> None:
        self.landed = {"a", "b", "c", "d"}
        self.sweeps = 0

    def view(self) -> SiteSweep:
        self.sweeps += 1
        k = TestKey("tests/t.rs", "t")
        line = 10 if "c" in self.landed else 20
        passed = frozenset({"p"}) | (frozenset({"q"}) if self.landed else frozenset())
        v = SweepView(passed=passed, failed=frozenset({"t"}), positions={k: line})
        return SiteSweep(101, "", len(passed), v)


def _row(name: str) -> LedgerEntry:
    return LedgerEntry(key=LedgerKey("p", "c", "src/a.rs", name), body_sha="",
                       bodies=(), sig_sha="", layout_fp="",
                       state=BodyState.REFUTED,
                       verdict_delta=(("t", "regressed"),),
                       prior_bodies=("old",))


def _ports(tree: _Tree) -> SiteRevertPorts:
    def restore(rows: Sequence[LedgerEntry]) -> tuple[list[LedgerEntry], str]:
        for r in rows:
            tree.landed.discard(r.key.fn_name)
        return list(rows), ""

    return SiteRevertPorts(
        restore=restore, snapshot=lambda: set(tree.landed),
        rollback=lambda snap: setattr(tree, "landed", set(snap)),  # type: ignore[arg-type]
        sweep=tree.view, log=lambda _m: None)


def test_only_the_landing_that_caused_the_move_is_reverted() -> None:
    """Plan sentence: of four landed rows, one causes the move; only that
    one is reverted. Negative control: without localization (the block this
    replaced) all four are reverted."""
    tree = _Tree()
    before = SweepView(failed=frozenset({"t"}),
                       positions={TestKey("tests/t.rs", "t"): 20})
    pre = tree.view()
    rows = [_row(n) for n in "abcd"]
    res = revert_site_regressions(_ports(tree), rows=rows, before=before, pre=pre)
    assert [r.key.fn_name for r in res.reverted] == ["c"]
    assert tree.landed == {"a", "b", "d"}

    control = _Tree()
    restored, _why = _ports(control).restore(rows)
    assert len(restored) == 4


def test_a_revert_that_loses_a_passing_test_is_rolled_back(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`q` passes only while some landing is on the tree; a revert of all
    four loses it and is rolled back byte for byte."""
    tree = _Tree()
    before = SweepView(failed=frozenset({"t"}),
                       positions={TestKey("tests/t.rs", "t"): 20})
    pre = tree.view()
    rows = [_row(n) for n in "abcd"]
    monkeypatch.setattr(site_revert, "localize", lambda cands, _mb: tuple(cands))
    res = revert_site_regressions(_ports(tree), rows=rows, before=before, pre=pre)
    assert res.rolled_back and res.reverted == () and res.final is None
    assert tree.landed == {"a", "b", "c", "d"}


# --- the source scan -----------------------------------------------------------

def test_no_judge_orders_raw_panic_lines() -> None:
    """Every judge compares typed positions through the one rule; a judge
    that reads a raw panic line (or the deleted line-order comparison) fails
    here without this test being edited. Negative control: the pre-fix
    `classify` body named `compare_panic_sites` over `panic_sites`."""
    judges = (body_ledger.classify, test_report.judge_refill,
              slice_session.judge_against_best)
    for fn in judges:
        src = inspect.getsource(fn)
        assert "panic_line" not in src and "panic_file" not in src, fn.__name__
        assert "compare_panic_sites" not in src and "panic_sites" not in src
    assert not hasattr(test_report, "compare_panic_sites")
    pre_fix = "regressed, advanced = compare_panic_sites(\n {t: site for t, site in before.panic_sites.items()"
    assert "compare_panic_sites" in pre_fix and "panic_sites" in pre_fix


_LITERALS = '''\
/* a block comment { with /* a nested */ brace } */
fn helper<'a>(x: &'a str) -> Vec<(u8, u8)> {
    vec![(1, 2)]
}

#[test]
fn test_l() {
    let a = r#"raw { | } "# ;
    let b = br"bytes { ";
    let c = b"more { \\" |";
    let d = b'{';
    let e = '{';
    let f = '\\'';
    let g = "esc \\" { |";
    // a line comment { |
    let h = move |x: u8| -> u8 { x };
    let k = (|y: u8| y + 1, 3);
    assert_eq!(h(1), k.0(0));
}
'''


def test_literals_and_comments_are_not_code() -> None:
    """Braces, pipes and quotes inside comments, raw, byte and plain string
    literals, and char literals neither end the test's item nor open a
    closure; a lifetime is not a char literal. The two real closures (one
    with a return type, one ending at a comma) are excluded."""
    lines = own_assertion_lines(_LITERALS, "test_l")
    assert lines is not None
    assert {8, 9, 10, 11, 12, 13, 14, 15, 18} <= lines
    assert {16, 17}.isdisjoint(lines)
    assert own_assertion_lines(_LITERALS, "helper") == frozenset({2, 3, 4})
    assert own_assertion_lines(_LITERALS, "absent") is None
    assert own_assertion_lines("", "t") is None
    # a trait method declaration has no body and is not an item here
    assert own_assertion_lines("trait T { fn t(&self); }", "t") is None
