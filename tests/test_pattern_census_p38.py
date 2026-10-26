"""P38, unfounded flat re-buy: a `rebuy_flat` deferral the iteration's
standing-FLAT set does not found.

Until 2026-10-17 the schedule's unmeasurable demotion (every aimed test's
binary did not complete, and the run writes no crash-blamed unit) carried the
label `rebuy_flat`. Every post-fix P38 hit on the logs since 2026-10-01 was
that label: 28 deferrals on runs `450922-661c5e9f`, `3163270-ca5a787c`,
`2157238-210637e6`, `35210-6d56e8c9` and `14235-bf24f931`, computed the same
day with and without the recognition below. The demotion now has its own
reason; on older events it is recognised from the recorded inputs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
_spec = importlib.util.spec_from_file_location("pattern_census_p38", _PATH)
assert _spec is not None and _spec.loader is not None
pc = importlib.util.module_from_spec(_spec)
sys.modules["pattern_census_p38"] = pc
_spec.loader.exec_module(pc)


def _event(reason: str, aimed: list[str], unmeasurable: list[str]) -> dict:
    return {"iter": 4, "cluster": "stable",
            "inputs": {"standing_flat": {}, "unmeasurable_tests": unmeasurable},
            "deferred": [{"kind": "file", "label": "file src/a.rs",
                          "reason": reason, "targets": ["src/a.rs::f"],
                          "target_tests": aimed}]}


def test_an_old_unmeasurable_deferral_is_not_a_flat_re_buy() -> None:
    sc = _event("rebuy_flat", ["t1", "t2"], ["t1", "t2", "t3"])
    assert pc.p38_unfounded_flat(sc, set()) == []


def test_a_rebuy_flat_with_a_measurable_aim_and_no_flat_body_still_hits() -> None:
    sc = _event("rebuy_flat", ["t1", "t9"], ["t1"])
    assert pc.p38_unfounded_flat(sc, set()) == [("file src/a.rs", ["src/a.rs::f"])]


def test_the_new_label_is_not_read_as_a_re_buy() -> None:
    sc = _event("unmeasurable", ["t1"], ["t1"])
    assert pc.p38_unfounded_flat(sc, set()) == []


def _session_event(reason: str) -> dict:
    return {"iter": 3, "cluster": "stable", "inputs": {"standing_flat": {}},
            "runs": [{"kind": "session", "label": "session[1 test(s)] #0",
                      "reason": reason, "target_tests": ["t_a"],
                      "targets": ["src/a.rs::f", "src/a.rs::g"]}],
            "deferred": []}


def _route(kind: str, outcome: str, targets: list[str], it: int,
           tests: list[str]) -> dict:
    return {"kind": kind, "outcome": outcome, "targets": targets,
            "iteration": it, "cluster": "stable", "target_tests": tests,
            "oracle_fp": "o",
            "label": f"{kind} {it}", "net_pass_delta": 2 if outcome == "cashed" else 0}


def test_a_session_rebuy_after_a_cash_on_its_slice_is_a_hit() -> None:
    routes = [_route("session", "flat", ["src/a.rs::f"], 1, ["t_a"]),
              _route("file", "cashed", ["src/a.rs::g"], 2, ["t_b"])]
    assert pc.p38_session_rebuy_after_cash(
        _session_event("rebuy_flat"), routes) == [
            ("session[1 test(s)] #0", "file 2")]


def test_control_a_flat_row_or_a_cash_off_the_slice_is_no_hit() -> None:
    flat = [_route("session", "flat", ["src/a.rs::f"], 1, ["t_a"]),
            _route("file", "flat", ["src/a.rs::g"], 2, ["t_b"])]
    off = [_route("session", "flat", ["src/a.rs::f"], 1, ["t_a"]),
           _route("file", "cashed", ["src/z.rs::h"], 2, ["t_b"])]
    ev = _session_event("rebuy_flat")
    assert pc.p38_session_rebuy_after_cash(ev, flat) == []
    assert pc.p38_session_rebuy_after_cash(ev, off) == []
    # A session ranked served is not a re-buy.
    hit = [_route("session", "flat", ["src/a.rs::f"], 1, ["t_a"]),
           _route("file", "cashed", ["src/a.rs::g"], 2, ["t_b"])]
    assert pc.p38_session_rebuy_after_cash(_session_event("served"), hit) == []
