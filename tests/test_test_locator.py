"""Characterization for the relocated test-locator (P6). Pins the tier ranking
(container > direct > stem > macro) and the generic-fn container filter."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.test_locator import find_tests_for_fn


def _w(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def test_empty_inputs_return_nothing() -> None:
    assert find_tests_for_fn([], "", "") == []


def test_direct_name_beats_stem(tmp_path: Path) -> None:
    direct = _w(tmp_path, "other.rs", "fn t() { divide_round(1, 2); }")
    stem = _w(tmp_path, "round.rs", "fn t() { something_else(); }")
    out = find_tests_for_fn([stem, direct], "divide_round", file_rel="src/round.rs")
    # direct name (tier 1) ranks above the stem match (tier 2).
    assert out[0][0] == str(direct)
    assert {p for p, _ in out} == {str(direct), str(stem)}


def test_generic_fn_filters_by_container_type(tmp_path: Path) -> None:
    hit = _w(tmp_path, "a.rs", "fn t() { let x = ContextError::new(); }")
    miss = _w(tmp_path, "b.rs", "fn t() { let x = OtherType::new(); }")
    out = find_tests_for_fn(
        [miss, hit], "new", file_rel="src/ctx.rs",
        container="impl Display for ContextError<E>",
    )
    assert out and out[0][0] == str(hit)


def test_max_blocks_caps_results(tmp_path: Path) -> None:
    files = [_w(tmp_path, f"t{i}.rs", "fn t(){ foo(); }") for i in range(5)]
    out = find_tests_for_fn(files, "foo", max_blocks=2)
    assert len(out) == 2
