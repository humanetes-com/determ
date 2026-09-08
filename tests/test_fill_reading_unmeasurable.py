"""The fill's own keeps name the tests no later run in the fill can move.

`unmeasurable_tests_now` reads the last KEPT comparison — not the last
measured one (a hang the gate reverted leaves the tree at baseline) —
and names the crashed / hung binaries' tests from the last-complete
catalog `measure_test_state` refreshes, so tests that never printed a
verdict before the abort are still named. `begin_fill` drops the
previous fill's keep: the sweep between fills re-measured that tree.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct import fill_reading
from pipeline_core.domain.test_results import (
    RegressionComparison,
    TestStateReading,
    parse_binary_results,
)

_SUITE = """
     Running tests/bin_x.rs (target/debug/deps/bin_x-0123456789abcdef)

running 2 tests
test t_x1 ... FAILED
test t_x2 ... FAILED

test result: FAILED. 0 passed; 2 failed; 0 ignored

     Running tests/bin_ok.rs (target/debug/deps/bin_ok-0123456789abcdef)

running 1 test
test t_ok ... ok

test result: ok. 1 passed; 0 failed; 0 ignored
"""


def _cmp(**kw) -> RegressionComparison:
    base = {"newly_red": frozenset(), "newly_crashed": (), "excluded_crashed": (),
            "not_run": (), "measured": (), "base_pass": 2, "cur_pass": 2,
            "base_executed": 3, "cur_executed": 3}
    return RegressionComparison(**{**base, **kw})


def _catalogued() -> str:
    fill_reading.reset_last_comparison()
    reading = TestStateReading(parse_binary_results(_SUITE))
    fill_reading.note_last_reading(reading)
    labels = {b.label for b in reading.binaries}
    return next(lb for lb in labels if "bin_x" in lb)


def test_a_kept_crash_names_the_binary_s_tests_from_the_catalog() -> None:
    label = _catalogued()
    assert fill_reading.unmeasurable_tests_now() == frozenset()
    fill_reading.note_kept_comparison(_cmp(crashed_all_red=(label,)))
    assert fill_reading.unmeasurable_tests_now() == frozenset({"t_x1", "t_x2"})
    # A hang is the same reading with the in-flight test named.
    fill_reading.note_kept_comparison(_cmp(
        newly_hung=(label,), hung_tests=(f"{label}::t_x1",),
        newly_crashed=(label,)))
    assert fill_reading.unmeasurable_tests_now() == frozenset({"t_x1", "t_x2"})


def test_a_reverted_measurement_is_not_a_keep_and_a_new_fill_forgets() -> None:
    label = _catalogued()
    # The gate measured a crash and reverted it: the tree is the baseline
    # again, so nothing is unmeasurable.
    fill_reading.note_last_comparison(_cmp(crashed_all_red=(label,)))
    assert fill_reading.unmeasurable_tests_now() == frozenset()
    fill_reading.note_kept_comparison(_cmp(crashed_all_red=(label,)))
    assert fill_reading.unmeasurable_tests_now()
    fill_reading.begin_fill()
    assert fill_reading.unmeasurable_tests_now() == frozenset()
    # ...while the catalog survives the fill boundary.
    fill_reading.note_kept_comparison(_cmp(crashed_all_red=(label,)))
    assert fill_reading.unmeasurable_tests_now() == frozenset({"t_x1", "t_x2"})
