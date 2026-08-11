"""`read_frozen_test_sources` — siblings PLUS the crate's `tests/` directory.

The gap this closes made a fix look broken. The derive requirement that cost
five of eight layout authorings lives in `tests/baseline.rs` and
`tests/integration.rs`, which are write-protected integration tests, not
`gen_tests` siblings. A scan wired to `read_oracle_sibling_sources` therefore
saw nothing and the requirement never reached the prompt — indistinguishable,
from the outside, from the scan itself not working.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.gen_tests_storage import (
    read_frozen_test_sources,
    read_oracle_sibling_sources,
)


def _crate(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "counter.rs").write_text("pub struct Counter;")
    (root / "src" / "counter.gen_tests_1.rs").write_text("fn a() { Counter::new(1); }")
    (root / "tests" / "baseline.rs").write_text("fn b() { Counter::default(); }")


def test_integration_tests_are_included_siblings_are_not_lost(tmp_path: Path) -> None:
    _crate(tmp_path)
    sib = read_oracle_sibling_sources(tmp_path)
    frozen = read_frozen_test_sources(tmp_path)
    assert set(sib) == {"src/counter.gen_tests_1.rs"}
    assert set(frozen) == {"src/counter.gen_tests_1.rs", "tests/baseline.rs"}


def test_negative_control_sibling_reader_misses_the_requirement(tmp_path: Path) -> None:
    """The pre-fix behaviour, pinned: the sibling reader cannot see the call
    that broke the runs. If this ever starts passing, the two readers have been
    conflated and the field-path gate has silently widened with them."""
    _crate(tmp_path)
    sib_text = "\n".join(read_oracle_sibling_sources(tmp_path).values())
    frozen_text = "\n".join(read_frozen_test_sources(tmp_path).values())
    assert "Counter::default()" not in sib_text
    assert "Counter::default()" in frozen_text


def test_dot_directories_are_skipped(tmp_path: Path) -> None:
    """`.skeleton/`, `.skeleton-filled/` and `.best_tree/` each hold a copy of
    these files; reading them multiplies every path by the number of working
    trees on disk while adding no constraint."""
    _crate(tmp_path)
    shadow = tmp_path / ".skeleton" / "stable" / "tests"
    shadow.mkdir(parents=True)
    (shadow / "baseline.rs").write_text("fn dupe() { Counter::default(); }")
    assert set(read_frozen_test_sources(tmp_path)) == {
        "src/counter.gen_tests_1.rs", "tests/baseline.rs"}


def test_missing_tests_dir_degrades_to_siblings(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.gen_tests_1.rs").write_text("fn a() {}")
    assert set(read_frozen_test_sources(tmp_path)) == {"src/a.gen_tests_1.rs"}


def test_absent_project_dir_is_empty(tmp_path: Path) -> None:
    assert read_frozen_test_sources(tmp_path / "nope") == {}
