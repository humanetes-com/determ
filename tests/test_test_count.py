"""The expected-test inventory counts what cargo runs — including doc tests.

task3 announced 149 tests against `expected=124` for `stable` on the merged
anyhow tree and 129 against `expected=104` for `nightly`: an undershoot of
exactly 25 in both, printed on every task3 entry as the nonsense aggregate
`273/228 passed`, and 25 is what `cargo test --doc` reports on that tree. With
that much slack the `reported < expected` guard could not fire in the direction
it was written for.
"""
from __future__ import annotations

from pathlib import Path

# Aliased: an imported name starting with `Test` is collected by pytest as a
# test class and warns about its constructor.
from pipeline_core.application.test_count import TestCountDeps as _CountDeps
from pipeline_core.application.test_count import active_test_inventory_for_cluster


def _deps() -> _CountDeps:
    return _CountDeps(
        enumerate_fns=lambda _p: [],
        mod_records=lambda _p: [],
        file_cfg_predicate=lambda _p, _r: None,
        is_gen_tests_file=lambda _n: False,
    )


def test_doc_tests_are_counted_in_the_expected_total(tmp_path: Path) -> None:
    """The 25-per-cluster undershoot in miniature: a src file with a doc test
    and no `#[test]` fn contributes to what cargo runs, so it must contribute to
    what task3 expects."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "/// Example\n"
        "/// ```\n"
        "/// let x = mycrate::f();\n"
        "/// ```\n"
        "pub fn f() {}\n")
    total, names = active_test_inventory_for_cluster(tmp_path, None, _deps())
    assert total == 1
    # Counted, never NAMED: rustdoc identifies it as `src/lib.rs - f (line 1)`,
    # which is not a `#[test]` name and reconciles against nothing.
    assert names == frozenset()


def test_a_file_with_neither_kind_of_test_counts_zero(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "/// Not an example, just prose.\npub fn f() {}\n")
    assert active_test_inventory_for_cluster(tmp_path, None, _deps())[0] == 0
