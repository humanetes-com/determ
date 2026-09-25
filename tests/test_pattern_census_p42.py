"""P42 wide-run premise refusal, on the recorded fill.

Run `116341-24c76a97` (2026-09-25) fill 6: the premise event deferred both
formatter pairs after the per-fn keeps moved `test_downcast` and
`test_downcast_mut`; each pair was planned on 54 tests. The detector reads
the schedule event's planned tests and the premise event's moved list, and
names each deferred run with planned tests left unmoved. Control: a run
whose every planned test moved is not a hit, and a premise event whose
moved list was cut short is skipped.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p42", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()
_FIX = json.loads((Path(__file__).parent / "fixtures" / "premise"
                   / "116341-24c76a97_fill6.json").read_text())
_PAIR = "pair src/error.rs::fmt + src/fmt.rs::debug"
_NARROW = "impl src/ptr.rs"


def _events(moved: list[str], moved_count: int | None = None):
    sched = [{"iter": 6, "runs": [
        {"label": label, "target_tests": r["target_tests"]}
        for label, r in _FIX["runs"].items()]}]
    prem = [{"iter": 6, "moved": moved,
             "moved_count": moved_count if moved_count is not None else len(moved),
             "deferred": [_PAIR, _NARROW]}]
    return sched, prem


def test_the_recorded_pair_deferral_is_a_hit_and_the_narrow_run_is_not() -> None:
    hits = census.p42_wide_refusals(*_events(["test_downcast", "test_downcast_mut"]))
    # test_downcast_mut is not among the pair's planned tests; test_too_long
    # moved later in the fill. At the deferral one of 54 had moved.
    assert hits == [f"iter 6 {_PAIR}: 53 of 54 unmoved"]


def test_a_moved_list_cut_short_is_skipped() -> None:
    assert census.p42_wide_refusals(
        *_events(["test_downcast"], moved_count=60)) == []
