"""L2 cargo-memoization tests — CODE_ARCHITECTURE.md R10.4. Offline: a counting
fake runner stands in for cargo; no real compile."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.cargo_cache import CargoCache, tree_hash


def _crate(td: str, body: str) -> Path:
    p = Path(td)
    (p / "src").mkdir()
    (p / "src" / "lib.rs").write_text(body, encoding="utf-8")
    return p


def test_tree_hash_changes_with_content() -> None:
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        p1 = _crate(a, "fn x() {}\n")
        p2 = _crate(b, "fn x() {}\n")
        assert tree_hash(p1) == tree_hash(p2)  # same content → same key
        (p2 / "src" / "lib.rs").write_text("fn y() {}\n", encoding="utf-8")
        assert tree_hash(p1) != tree_hash(p2)  # a fill changed the tree → new key


def test_record_then_hit_same_tree() -> None:
    calls = {"n": 0}

    def fake(cmd, project_dir, *a, **k):
        calls["n"] += 1
        return (0, "test result: ok. 1 passed; 0 failed")

    cache = CargoCache(record=True)
    runner = cache.wrap(fake)
    with tempfile.TemporaryDirectory() as td:
        proj = _crate(td, "fn x() {}\n")
        r1 = runner("cargo test", proj)
        r2 = runner("cargo test", proj)  # identical tree+cmd → cached
    assert r1 == r2
    assert calls["n"] == 1  # real runner invoked once
    assert (cache.hits, cache.misses) == (1, 1)


def test_tree_change_misses() -> None:
    calls = {"n": 0}

    def fake(cmd, project_dir, *a, **k):
        calls["n"] += 1
        return (calls["n"], f"run {calls['n']}")

    cache = CargoCache(record=True)
    runner = cache.wrap(fake)
    with tempfile.TemporaryDirectory() as td:
        proj = _crate(td, "fn x() {}\n")
        runner("cargo test", proj)
        (proj / "src" / "lib.rs").write_text("fn x() { 1; }\n", encoding="utf-8")
        runner("cargo test", proj)  # tree changed → real runner again
    assert calls["n"] == 2


def test_replay_mode_raises_on_miss() -> None:
    cache = CargoCache(record=False)  # no recordings
    runner = cache.wrap(lambda *a, **k: (0, "should not run"))
    with tempfile.TemporaryDirectory() as td:
        proj = _crate(td, "fn x() {}\n")
        with pytest.raises(KeyError):
            runner("cargo test", proj)
