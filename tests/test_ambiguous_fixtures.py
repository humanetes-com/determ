"""Fixture-ambiguity gate: unique-name trees fail; a homonym crate passes."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tools.check_ambiguous_fixtures import (
    CRATES,
    EXTRACTOR,
    container_homonym_of,
    evaluate,
    would_fail,
)


def test_cfg_toggle_arms_are_not_container_homonyms() -> None:
    """hardcrate ``cap_limit`` / microcrate ``capacity_hint``: two arms,
    one empty container — not the class FnSite fixes. Negative control
    of the (file, name) metric that printed 1.4% / 3.7% and could never
    fail."""
    triples = [("src/lib.rs", "cap_limit", ""),
               ("src/lib.rs", "cap_limit", "")]
    st = container_homonym_of(triples)
    assert st["keys"] == 1 and st["homonym_keys"] == 0
    assert st["homonym_pct"] == 0.0
    assert would_fail([st])


def test_argh_shaped_homonyms_keep_the_gate_green() -> None:
    """Four set_flag containers on one key → homonym% > 0."""
    triples = [
        ("src/lib.rs", "set_flag", f"impl {i}") for i in range(4)
    ] + [("src/lib.rs", "unique", "")]
    st = container_homonym_of(triples)
    assert st["homonym_keys"] == 1
    assert st["worst"] == 4
    unique = container_homonym_of([("src/a.rs", "a", ""), ("src/a.rs", "b", "")])
    assert not would_fail([unique, st])
    assert would_fail([unique])
    assert would_fail([])


def test_missing_required_crate_fails(tmp_path: Path) -> None:
    """A skip of argh used to leave the gate green. Absence is a fail."""
    missing = tmp_path / "nope" / "src"
    rc = evaluate(
        [("ghost", missing)],
        enumerate_fn=lambda _src: [("src/a.rs", "a", "impl A")],
    )
    assert rc == 1


def test_empty_enumerate_of_present_crate_fails(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    rc = evaluate(
        [("ghost", src)],
        enumerate_fn=lambda _src: [],
    )
    assert rc == 1


def test_old_file_fn_metric_cannot_fail_on_cfg_toggle_arms() -> None:
    """Shipped gate counted two empty-container ``#[cfg]`` arms as
    homonyms (hardcrate 1.4%). That metric stays > 0 on unique-name
    crates, so ``max(ambiguous%) == 0`` could never fire. Distinct
    containers on the same pairs is 0% — the gate can fail."""
    pairs = [("src/lib.rs", "cap_limit")] * 2 + [
        ("src/lib.rs", f"f{i}") for i in range(71)
    ]
    counts = Counter(pairs)
    old_pct = 100.0 * sum(1 for c in counts.values() if c > 1) / len(counts)
    assert old_pct > 0
    triples = [(f, n, "") for f, n in pairs]
    assert would_fail([container_homonym_of(triples)])


@pytest.mark.skipif(
    not EXTRACTOR.exists() or not all(p.is_dir() for _n, p in CRATES),
    reason="fixture trees or skeleton-extractor missing",
)
def test_live_crates_match_plan_container_distinct_counts() -> None:
    """Plan sentence: hardcrate/microcrate 0.0% container-distinct;
    argh 5 keys 21.7% worst 4. Old (file, name) metric on hardcrate is
    the negative control that could not fail the shipped gate."""
    from tools.check_ambiguous_fixtures import _enumerate

    by_name = {}
    for name, src in CRATES:
        by_name[name] = container_homonym_of(_enumerate(src))
    assert by_name["hardcrate"]["homonym_pct"] == 0.0
    assert by_name["microcrate"]["homonym_pct"] == 0.0
    assert by_name["argh"]["homonym_keys"] == 5
    assert by_name["argh"]["worst"] == 4
    hard_triples = _enumerate(CRATES[0][1])
    old = Counter((f, n) for f, n, _c in hard_triples)
    old_pct = 100.0 * sum(1 for c in old.values() if c > 1) / len(old)
    assert old_pct > 0.0
    assert evaluate() == 0
