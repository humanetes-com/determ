"""Test-oracle immutability (Hard Rule 11).

The fill/fix frontier must never target a `#[cfg(test)]`/`#[test]` fn — splicing a
test body rewrites the oracle (the hardcrate `extra_cap_limit` 255→512 corruption).
`_filter_test_targets` strips such keys from every attribution path.
"""
from __future__ import annotations

import pytest

import pipeline
from pipeline_core.application.reconstruct.test_targets import is_test_fn_record
from tests.harness import install_fake_extractor


def _recs() -> list[dict]:
    # production fn + an inline #[cfg(test)] test fn (carries the 'test' cfg the
    # extractor propagates from the enclosing `#[cfg(test)] mod`).
    return [
        {"file": "src/lib.rs", "fn_name": "cap_limit", "cfg_predicates": ["not (any (extra_cap))"]},
        {"file": "src/lib.rs", "fn_name": "extra_cap_limit",
         "cfg_predicates": ["test", "any (extra_cap)"], "container": "mod tests"},
    ]


def test_is_test_fn_record() -> None:
    assert is_test_fn_record({"cfg_predicates": ["test", "x"]}) is True
    assert is_test_fn_record({"cfg_predicates": ["any (x)"]}) is False
    assert is_test_fn_record({}) is False


def test_filter_test_targets_drops_test_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path
    install_fake_extractor(monkeypatch, fns=lambda _p: _recs())
    frontier = {
        ("src/lib.rs", "cap_limit"): {"default_cap_limit"},       # production → kept
        ("src/lib.rs", "extra_cap_limit"): {"extra_cap_limit"},   # test oracle → dropped
    }
    out = pipeline._APP.phase_c_tdd_deps.filter_test_targets(frontier, Path("/proj"), cluster=None)
    assert ("src/lib.rs", "cap_limit") in out
    assert ("src/lib.rs", "extra_cap_limit") not in out


def test_filter_test_targets_noop_without_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path
    install_fake_extractor(
        monkeypatch, fns=[{"file": "src/lib.rs", "fn_name": "f", "cfg_predicates": []}])
    frontier = {("src/lib.rs", "f"): {"t"}}
    assert pipeline._APP.phase_c_tdd_deps.filter_test_targets(frontier, Path("/proj"), cluster=None) == frontier
    assert pipeline._APP.phase_c_tdd_deps.filter_test_targets({}, Path("/proj"), cluster=None) == {}


def test_filter_test_targets_drops_a_site_the_cluster_never_compiles(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The same composed filter drops a site the cluster's cfg-set decidably
    excludes, stub or filled — run `473471-33301154` (2026-09-29) admitted a
    cfg-dead locator to `stable`'s frontier because the filter had no cluster.
    Negative control: the no-cluster reading keeps it."""
    from pathlib import Path
    from types import SimpleNamespace
    install_fake_extractor(monkeypatch, fns=[
        {"file": "src/lib.rs", "fn_name": "live", "cfg_predicates": []},
        {"file": "src/lib.rs", "fn_name": "gated", "cfg_predicates": ["feat_x"]}])
    frontier = {("src/lib.rs", "live"): {"t"}, ("src/lib.rs", "gated"): {"t"}}
    filt = pipeline._APP.phase_c_tdd_deps.filter_test_targets
    cluster = SimpleNamespace(name="c", cfg_true=[], cfg_false=["feat_x"])
    assert filt(frontier, Path("/proj"), cluster=cluster) == {
        ("src/lib.rs", "live"): {"t"}}
    assert filt(frontier, Path("/proj"), cluster=None) == frontier
