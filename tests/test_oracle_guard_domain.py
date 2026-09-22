"""The oracle drift model: paths that differ from the strip-time seal.

Run `92187-f71f12bf` (2026-09-22): an edit made outside the pipeline to one
test file moved the oracle fingerprint and was visible nowhere else. The
fold here is the fingerprint's (`canon_body`), so a change the fingerprint
sees is drift and a reformat it forgives is not. Invented paths.
"""
from __future__ import annotations

from pipeline_core.domain.oracle_guard import oracle_drift

_SEALED = {"tests/a.rs": "fn a() { x(); }", "tests/b.rs": "fn b() {}",
           "src/m.gen_tests_1.rs": "fn g() {}"}


def test_identical_and_reformatted_are_clean() -> None:
    cur = {**_SEALED, "tests/a.rs": "fn a() {\n    x();\n}"}
    assert oracle_drift(cur, _SEALED, seal="oracle-m").clean


def test_changed_added_removed_are_named() -> None:
    cur = {"tests/a.rs": "fn a() { y(); }", "tests/b.rs": "fn b() {}",
           "tests/c.rs": "fn c() {}"}
    d = oracle_drift(cur, _SEALED, seal="oracle-m")
    assert d.changed == ("tests/a.rs",)
    assert d.added == ("tests/c.rs",)
    assert d.removed == ("src/m.gen_tests_1.rs",)
    assert d.paths() == ("src/m.gen_tests_1.rs", "tests/a.rs", "tests/c.rs")
    assert "changed: tests/a.rs" in d.summary()
