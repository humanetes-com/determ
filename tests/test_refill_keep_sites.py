"""Every refill keep reads one non-regression rule, panic sites included.

Run `24951-a72c4642` (2026-09-29, cluster `stable`): a pair write made a test
that was red before it fail one assertion EARLIER (`:91` -> `:90` of the same
test file). The pair's keep judged "non-regression" from names alone — the
test was red before and after — and kept it; the sweep graded the landing
`flat`, the flat row excluded the unit, and the earlier panic hid the unit
the test really reaches from coverage. Three of the four keep sites (impl,
pair, per-fn) built the judgment from their own flags; only the file route
compared sites. The fixtures below are an invented one-test crate shape.

Run `273278-22529e44` (same day) set the rule these tests now pin: a verdict
outranks a position. Any lost test refuses; an earlier position refuses only
when nothing attributed gained; positions exist only inside the failing
test's own body (`assertion_position`).
"""
from __future__ import annotations

import re
from pathlib import Path

from pipeline_core.adapters.fn_reseed import (
    current_bodies,
    current_signatures,
    splice_bodies,
)
from pipeline_core.application.reconstruct.body_reseed import (
    TreeWriter,
    restore_prior,
)
from pipeline_core.domain.assertion_position import PositionMove, TestKey
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    body_sha,
)
from pipeline_core.domain.slice_session import (
    TargetStatus,
    TurnReading,
    judge_against_best,
)
from pipeline_core.domain.test_report import (
    U2Verdict,
    judge_refill,
    reading_of_capture,
)
from pipeline_core.domain.test_results import TestStateReading

_REPO = Path(__file__).resolve().parents[1]
_BIN = "tests/test_x.rs"


def _test_file(items: dict[str, tuple[int, int]]) -> str:
    """An invented test file: each named test fn opens at the first line and
    closes at the second, its body plain assertions."""
    lines = ["// filler"] * 200
    for name, (a, b) in items.items():
        lines[a - 1] = f"fn {name}() {{"
        for k in range(a, b - 1):
            lines[k] = "    assert!(check());"
        lines[b - 1] = "}"
    return "\n".join(lines)


_X_SRC = _test_file({"test_ok": (10, 14), "test_gain": (36, 44),
                     "test_new": (86, 97), "test_early": (115, 125)})


def _sources(rel: str) -> str | None:
    return _X_SRC if rel == _BIN else None


def _read(capture: str) -> TestStateReading:
    return reading_of_capture(capture, sources=_sources)


_LIBTEST_RED = """\
     Running tests/test_x.rs (target/debug/deps/test_x-0123)

running 2 tests
test test_new ... FAILED
test test_ok ... ok

failures:

---- test_new stdout ----

thread 'test_new' panicked at tests/test_x.rs:{line}:5:
assertion `left == right` failed

failures:
    test_new

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""


def _attempt_red_at(line: int, test: str = "test_new",
                    passed: frozenset[str] = frozenset()) -> U2Verdict:
    return U2Verdict.reject(
        frozenset({test}), "detail", self_passed=passed,
        positions={TestKey(_BIN, test): line})


def test_the_baseline_reading_carries_the_positions_of_its_capture() -> None:
    """The keep compares against the reading, so the reading must hold the
    positions the capture printed — read off the same capture as the names."""
    base = _read(_LIBTEST_RED.format(line=91))
    assert base.positions == {TestKey(_BIN, "test_new"): 91}
    assert "test_ok" in base.passed_names


def test_a_panic_outside_the_tests_own_body_has_no_position() -> None:
    """Line 20 is filler between items, the shape of a helper above its
    callers; a panic in `src/` is never read at all."""
    assert _read(_LIBTEST_RED.format(line=20)).positions == {}
    in_src = _LIBTEST_RED.format(line=91).replace(
        "panicked at tests/test_x.rs:91", "panicked at src/lib.rs:91")
    assert _read(in_src).positions == {}


def test_a_refill_that_fails_an_attributed_test_earlier_is_not_non_regression() -> None:
    """The 24951 shape: a keep whose attributed test was red at baseline and
    now stops at an earlier line of its own body, nothing gained, is not a
    non-regression keep. Negative control: the pre-fix pair/impl/per-fn
    predicate (no self-failure passing at baseline) keeps the same attempt."""
    base = _read(_LIBTEST_RED.format(line=91))
    attempt = _attempt_red_at(90)
    judged = judge_refill({"test_new"}, base, attempt)
    assert not judged.non_regression
    assert judged.regressed_sites == (
        PositionMove(TestKey(_BIN, "test_new"), 91, 90),)
    pre_fix_keep = not (attempt.self_failures & base.passed_names)
    assert pre_fix_keep


_TWO_RED = """\
     Running tests/test_x.rs (target/debug/deps/test_x-0123)

running 2 tests
test test_gain ... FAILED
test test_early ... FAILED

failures:

---- test_gain stdout ----

thread 'test_gain' panicked at tests/test_x.rs:40:5:
assertion failed

---- test_early stdout ----

thread 'test_early' panicked at tests/test_x.rs:121:5:
assertion failed

failures:
    test_gain
    test_early

test result: FAILED. 0 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_a_verdict_outranks_a_position_on_mixed_outcomes() -> None:
    """The decided rule: gained + earlier stands, earlier alone refuses, and
    a lost test refuses whatever gained. Negative control: the rule before
    it ("an earlier panic refuses whatever the others did") refuses the
    gained + earlier attempt."""
    base = _read(_TWO_RED)
    mixed = _attempt_red_at(120, "test_early", frozenset({"test_gain"}))
    judged = judge_refill({"test_gain", "test_early"}, base, mixed)
    assert judged.resolved == frozenset({"test_gain"})
    assert judged.regressed_sites
    assert judged.non_regression and judged.banks
    old_rule_keeps = not judged.new_failures and not judged.regressed_sites
    assert not old_rule_keeps
    alone = judge_refill({"test_gain", "test_early"}, base,
                         _attempt_red_at(120, "test_early"))
    assert not alone.non_regression
    lost_base = _read(_LIBTEST_RED.format(line=91))
    lost = U2Verdict.reject(frozenset({"test_ok"}), "d",
                            self_passed=frozenset({"test_new"}))
    assert not judge_refill({"test_new", "test_ok"}, lost_base,
                            lost).non_regression


def test_the_same_or_a_later_line_is_still_non_regression() -> None:
    base = _read(_LIBTEST_RED.format(line=91))
    assert judge_refill({"test_new"}, base, _attempt_red_at(91)).non_regression
    assert judge_refill({"test_new"}, base, _attempt_red_at(95)).non_regression


# A keep site that builds "non-regression" from a name set alone: the three
# pre-fix predicates were `not (<verdict>.self_failures & <baseline>.passed_names)`.
_NAME_ONLY_KEEP = re.compile(r"self_failures\s*&\s*[\w.]*passed_names")


def test_no_keep_site_judges_non_regression_from_names_alone() -> None:
    """The class, not the instance: a new keep site that re-creates the
    name-only predicate fails here without this test being edited. The
    model is `judge_refill(...).non_regression`; every consumer reads it."""
    offenders = []
    for path in sorted((_REPO / "pipeline_core" / "application").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _NAME_ONLY_KEEP.search(re.sub(r"\s+", " ", text)):
            offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []


def test_the_name_only_pattern_catches_the_pre_fix_predicates() -> None:
    """Negative control for the scan above: the pair site's pre-fix text."""
    pre_fix = """
                _pair_nonregr = (
                    _pair_u2 is not None
                    and _pair_baseline is not None
                    and not (_pair_u2.self_failures
                             & _pair_baseline.passed_names))"""
    assert _NAME_ONLY_KEEP.search(re.sub(r"\s+", " ", pre_fix))


def test_a_session_turn_that_fails_a_target_earlier_regresses_at_no_net_gain() -> None:
    """The session's end-of-session keep reads `TurnReading.regressed`: the
    same clause, with the sweep's forgiveness of a strict net gain."""
    red = (TargetStatus("test_new", False),)
    assert TurnReading(targets=red, earlier=("test_new",)).regressed
    assert not TurnReading(targets=red).regressed
    assert not TurnReading(targets=red, earlier=("test_new",),
                           pass_delta=1).regressed


def test_a_silent_abort_regresses_even_beside_a_gain() -> None:
    """A destroyed verdict is not forgiven by a strict net gain. Negative
    control: the same gain with no abort is not a regression, and a
    position move beside that gain is not either."""
    red = (TargetStatus("test_new", False),)
    assert TurnReading(targets=red, aborted=("test_new",),
                       pass_delta=1).regressed
    assert TurnReading(targets=red, aborted=("test_new",)).regressed
    assert not TurnReading(targets=red, pass_delta=1).regressed
    assert not TurnReading(targets=red, earlier=("test_new",),
                           pass_delta=1).regressed


def test_a_turn_that_fails_a_target_earlier_than_the_best_does_not_hold() -> None:
    """A turn is put back when a target stops earlier than in the session's
    best state and it gained no target over the best. Negative controls:
    the same line holds, and a turn that also turned another target green
    holds (a verdict outranks a position)."""
    red = (TargetStatus("test_new", False), TargetStatus("test_gain", False))
    k = TestKey(_BIN, "test_new")
    best = TurnReading(targets=red, positions={k: 91})
    worse = TurnReading(targets=red, positions={k: 90})
    same = TurnReading(targets=red, positions={k: 91})
    verdict = judge_against_best(worse, best)
    assert not verdict.holds
    assert verdict.earlier_than_best == ("test_new",)
    assert "earlier assertion" in verdict.losses()
    assert judge_against_best(same, best).holds
    gained = TurnReading(
        targets=(TargetStatus("test_new", False),
                 TargetStatus("test_gain", True)), positions={k: 90})
    assert judge_against_best(gained, best).earlier_than_best == ()
    assert judge_against_best(gained, best).holds


def test_a_turn_that_destroys_a_verdict_the_best_did_not_does_not_hold() -> None:
    """A silent abort the best state did not have refuses, including beside
    a target the turn turned green. The abort is not reported as an earlier
    assertion. Negative control: the same gain with no abort holds."""
    red = (TargetStatus("test_new", False), TargetStatus("test_gain", False))
    best = TurnReading(targets=red)
    destroyed = TurnReading(
        targets=(TargetStatus("test_new", False),
                 TargetStatus("test_gain", True)),
        aborted=("test_new",), pass_delta=1)
    verdict = judge_against_best(destroyed, best)
    assert not verdict.holds
    assert verdict.aborted == ("test_new",)
    assert verdict.earlier_than_best == ()
    assert "destroyed the verdict" in verdict.losses()
    same_gain = TurnReading(
        targets=(TargetStatus("test_new", False),
                 TargetStatus("test_gain", True)),
        pass_delta=1)
    assert judge_against_best(same_gain, best).holds
    already = TurnReading(targets=red, aborted=("test_new",))
    again = TurnReading(
        targets=(TargetStatus("test_new", False),
                 TargetStatus("test_gain", True)),
        aborted=("test_new",), pass_delta=1)
    assert judge_against_best(again, already).holds


_WRITER = TreeWriter(splice=splice_bodies,
                     signatures=current_signatures, bodies=current_bodies)

_SRC = """\
pub fn render(f: &mut String) {
    // landed
    f.push_str("worse")
}
"""


def _row(prior: tuple[str, ...]) -> LedgerEntry:
    now = ("\n    // landed\n    f.push_str(\"worse\")\n",)
    return LedgerEntry(
        key=LedgerKey("p", "c", "src/a.rs", "render"), body_sha=body_sha(now),
        bodies=now, sig_sha="", layout_fp="", state=BodyState.REFUTED,
        verdict_delta=(("test_new", "regressed"),), prior_bodies=prior)


def test_a_site_regressed_landing_is_put_back_to_the_bodies_it_replaced(
        tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rs").write_text(_SRC)
    prior = ("\n    // before, kept verbatim\n    f.push_str(\"better\")\n",)
    restored, why = restore_prior(rows=[_row(prior)], src_dir=tmp_path,
                                  writer=_WRITER)
    assert why == "" and len(restored) == 1
    text = (tmp_path / "src" / "a.rs").read_text()
    assert "better" in text and "worse" not in text
    assert "// before, kept verbatim" in text


def test_a_row_without_prior_bodies_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rs").write_text(_SRC)
    restored, why = restore_prior(rows=[_row(())], src_dir=tmp_path,
                                  writer=_WRITER)
    assert restored == [] and why
    assert (tmp_path / "src" / "a.rs").read_text() == _SRC


def test_a_restore_that_does_not_build_writes_nothing(tmp_path: Path) -> None:
    """One transaction: a refused build check leaves the tree as it was."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rs").write_text(_SRC)
    restored, why = restore_prior(
        rows=[_row(("\n    f.push_str(\"better\")\n",))], src_dir=tmp_path,
        writer=_WRITER, check_builds=lambda: (False, "error[E0308]"))
    assert restored == [] and why
    assert (tmp_path / "src" / "a.rs").read_text() == _SRC
