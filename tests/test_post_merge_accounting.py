"""task2c's accounting: the tests a crashed binary takes with it, and the fns
they convict.

`parse_test_counts` sums `test result:` lines and a binary that segfaults prints
none, so its tests leave the numerator AND the denominator together. task2a has
`phase_c.tdd.oracle_vanished` for this and task3 has the reported-vs-expected
check; task2c had neither, and it is the stage that decides when the tree is
done. Measured on `anyhow` 2026-08-05: `pass=139 fail=1` logged while
`tests/test_downcast.rs` cored and 6 of its 7 tests reported nothing.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.generators.task2_generator import (
    StaticSkeletonTask2Generator,
)

_frontier = StaticSkeletonTask2Generator._frontier_from_silent_binaries


def test_a_crashed_binarys_tests_convict_the_fns_they_exercise(tmp_path: Path) -> None:
    # A crashed binary prints no FAILED lines, so its tests convict nothing and
    # no fill is ever aimed at them — and on the merged `anyhow` tree they were
    # the ONLY tests whose repair could have moved the score.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_downcast.rs").write_text(
        "#[test]\nfn test_downcast() { }\n#[test]\nfn test_drop() { }\n",
        encoding="utf-8")
    (tests_dir / "test_ensure.rs").write_text(
        "#[test]\nfn test_ensure() { }\n", encoding="utf-8")
    reverse_map = {
        "test_downcast": {("src/error.rs", "object_downcast")},
        "test_drop": {("src/error.rs", "object_drop_front")},
        "test_ensure": {("src/ensure.rs", "render")},
    }
    out = _frontier(
        ("Running tests/test_downcast.rs",),
        [tests_dir / "test_downcast.rs", tests_dir / "test_ensure.rs"],
        reverse_map,
    )
    assert set(out) == {("src/error.rs", "object_downcast"),
                        ("src/error.rs", "object_drop_front")}
    # Convicted by the whole binary, which is the honest granularity: it went
    # down as a unit and nothing in the output says which test did it.
    assert out[("src/error.rs", "object_downcast")] == {"test_downcast"}
    # A binary that reported normally contributes nothing.
    assert ("src/ensure.rs", "render") not in out


def test_no_silent_binaries_means_no_extra_frontier(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.rs").write_text("#[test]\nfn t() {}\n", encoding="utf-8")
    assert _frontier((), [tests_dir / "test_x.rs"], {"t": {("a.rs", "f")}}) == {}


def test_an_unreadable_test_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    missing = tmp_path / "tests" / "gone.rs"
    assert _frontier(("Running tests/gone.rs",), [missing],
                     {"t": {("a.rs", "f")}}) == {}
