"""Test-oracle immutability (FR-018 / Hard Rule 11 / debt #17b).

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
    out = pipeline._filter_test_targets(frontier, Path("/proj"))
    assert ("src/lib.rs", "cap_limit") in out
    assert ("src/lib.rs", "extra_cap_limit") not in out


def test_filter_test_targets_noop_without_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path
    install_fake_extractor(
        monkeypatch, fns=[{"file": "src/lib.rs", "fn_name": "f", "cfg_predicates": []}])
    frontier = {("src/lib.rs", "f"): {"t"}}
    assert pipeline._filter_test_targets(frontier, Path("/proj")) == frontier
    assert pipeline._filter_test_targets({}, Path("/proj")) == {}
