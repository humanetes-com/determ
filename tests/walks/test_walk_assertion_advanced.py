"""Walk: a sweep whose failing test got past an assertion is not "bought nothing".

Run `11358-e5de094e` (2026-09-15, cluster `stable`) iter 5 printed "1
body(ies) landed since the last sweep and NO test changed verdict ... on this
evidence they bought nothing" over a fill whose kept body had taken the
aimed test from failing at its first assertion to failing at its second; the
ledger graded the landing `flat`.

Replayed on the flat readings of `358833-aa15d93c` (135 passing, 2 failing at
every sweep), with the failing tests' synthetic panic moving from line 10 to
line 12 of their test file between the first two sweeps. Asserted: the loop
reports the advance, emits `phase_c.tdd.assertion_advanced`, and does not
print the "bought nothing" line for that sweep. Negative control: the same
replay with the panic line unmoved prints "bought nothing" and emits no
advance.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.walks.harness import load_walk, replay

pytestmark = pytest.mark.walk


def _replay(monkeypatch, tmp_path: Path, lines: list[int]):
    events: list[tuple[str, dict]] = []
    # The harness's default fill lands bodies every iteration, which is the
    # "landed since the last sweep" half of both lines.
    logs = replay(load_walk("358833-aa15d93c"), monkeypatch, tmp_path,
                  events=events, max_stalls="9", panic_lines=lines)
    return logs, events


def test_a_panic_that_moved_later_is_reported_not_bought_nothing(
        monkeypatch, tmp_path: Path) -> None:
    logs, events = _replay(monkeypatch, tmp_path, [10, 12, 12])
    advanced = [d for e, d in events if e == "phase_c.tdd.assertion_advanced"]
    assert advanced, "the second sweep's later panic must be reported"
    assert advanced[0]["iter"] == 2
    assert {a["before"] for a in advanced[0]["advanced"]} == {10}
    assert {a["after"] for a in advanced[0]["advanced"]} == {12}
    iter2 = [ln for ln in logs if "iter 2:" in ln]
    assert any("LATER assertion" in ln for ln in iter2)
    assert not any("bought nothing" in ln for ln in iter2)


def test_negative_control_an_unmoved_panic_still_bought_nothing(
        monkeypatch, tmp_path: Path) -> None:
    logs, events = _replay(monkeypatch, tmp_path, [10, 10, 10])
    assert not [e for e, _ in events if e == "phase_c.tdd.assertion_advanced"]
    assert any("iter 2:" in ln and "bought nothing" in ln for ln in logs)


def test_negative_control_the_pre_change_sweep_calls_the_moved_panic_nothing(
        monkeypatch, tmp_path: Path) -> None:
    """The pre-change reading injected into the same moving-panic replay:
    with no panic comparison the loop prints "bought nothing" exactly as
    run `11358-e5de094e` did."""
    import pipeline_core.application.reconstruct.phase_c_tdd as _mod
    monkeypatch.setattr(_mod, "compare_panic_sites",
                        lambda _b, _a: ((), ()))
    logs, events = _replay(monkeypatch, tmp_path, [10, 12, 12])
    assert not [e for e, _ in events if e == "phase_c.tdd.assertion_advanced"]
    assert any("iter 2:" in ln and "bought nothing" in ln for ln in logs)
