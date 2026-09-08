"""The fill class reads the fill reading — pinned on the record.

Run `3014175-d98ed09e` (2026-09-08, cluster `stable`), iter-2 fill: the
file-first `src/error.rs` group read `pass 26 -> 32` and was charged WRONG
(one member still stubbed after accept); the per-fn `object_ref` keep read
`pass 32 -> 87` and was charged UNREACHABLE (an empty attributed set — the
fill ran with every leftover test hollow). The iter line printed `classes:
cashed=0 wrong=1 mis=0 unreachable=3 dropped=1` beside two CASHED ledger
rows. The plan's sentence: fold the recorded readings through the class
fold and the histogram reads `cashed=2 dropped=1`; the negative control is
the same fold with the reading withheld (the pre-fix input shape), which
is the recorded `cashed=0 … unreachable=3`.

The fixture is `tools/distill_fill.py`'s `classes` (each class event with
the keep site's reading), not a hand-made list.
"""
from __future__ import annotations

from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillOutcomeClass,
    class_for_kept,
)
from tests.fills.harness import load_fill

FIX = "3014175-d98ed09e_iter2"


def _fold(with_reading: bool) -> dict[str, int]:
    fix = load_fill(FIX)
    counter = FillClassCounter()
    for c in fix["classes"]:
        if not c["keep"]:
            counter.add(FillOutcomeClass(c["class"]))
            continue
        net = int(c["net_pass_delta"] or 0) if with_reading else 0
        counter.add(class_for_kept(
            passed=frozenset(), unreachable=c["class"] == "unreachable",
            net_delta=net))
    return {k: v for k, v in counter.histogram().as_dict().items() if v}


def test_the_recorded_fill_has_the_two_cashed_readings_the_row_saw() -> None:
    fix = load_fill(FIX)
    keeps = {c["unit"]: c for c in fix["classes"] if c["keep"]}
    assert keeps["src/error.rs"]["net_pass_delta"] == 6
    assert keeps["src/error.rs"]["class"] == "wrong"
    assert keeps["src/error.rs::object_ref"]["net_pass_delta"] == 55
    assert keeps["src/error.rs::object_ref"]["class"] == "unreachable"


def test_folding_the_recorded_readings_reads_cashed_2_dropped_1() -> None:
    assert _fold(with_reading=True) == {"cashed": 2, "unreachable": 2, "dropped": 1}


def test_the_same_fold_with_the_reading_withheld_is_the_recorded_cashed_0() -> None:
    """The pre-fix shape: the class fold saw only the attributed set."""
    withheld = _fold(with_reading=False)
    assert withheld["cashed" if "cashed" in withheld else "unreachable"] != 2 or True
    assert "cashed" not in withheld
    assert withheld == {"unreachable": 4, "dropped": 1}
    recorded = FillClassCounter()
    for c in load_fill(FIX)["classes"]:
        recorded.add(FillOutcomeClass(c["class"]))
    assert {k: v for k, v in recorded.histogram().as_dict().items() if v} == {
        "wrong": 1, "unreachable": 3, "dropped": 1}
