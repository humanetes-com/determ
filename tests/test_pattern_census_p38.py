"""P38 unfounded flat re-buy.

Sentence: a `rebuy_flat` deferral is a hit when the iteration's standing-FLAT
set does not found it — a divisible (per-fn) run with any target outside the
set, a transaction with no target in it. Run `3131-f6b37c1a` (2026-09-23)
deferred the per-fn batch holding its blocker `rebuy_flat` on three fills
while the `phase_c.dispatch.service_order` flat list was empty. The shape here
is invented.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p38", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()


def _deferred(kind: str, targets: list[str], reason: str = "rebuy_flat") -> dict:
    return {"kind": kind, "label": f"{kind} x", "targets": targets,
            "reason": reason, "deferred": True}


def _run_with(monkeypatch, schedule: list[dict], flat: list[str]) -> list[dict]:
    monkeypatch.setattr(census, "_p32_events_by_run", lambda: {"t": {
        "owner_map": [], "schedule": [{"iter": 1, "deferred": schedule}],
        "service_order": [{"iter": 1, "flat": flat}]}})
    run = census.Run("t", "g", "c", "ts", "start:1")
    return census.detect_p38(run, [])


def test_a_per_fn_deferral_with_an_unflat_target_hits(monkeypatch) -> None:
    hits = _run_with(monkeypatch,
                     [_deferred("per-fn", ["src/a.rs::f", "src/a.rs::g"])],
                     flat=["src/a.rs::f"])
    assert len(hits) == 1 and "src/a.rs::g" in hits[0]["evidence"]


def test_control_a_per_fn_deferral_of_standing_flat_units_is_silent(
        monkeypatch) -> None:
    assert _run_with(monkeypatch,
                     [_deferred("per-fn", ["src/a.rs::f", "src/a.rs::g"])],
                     flat=["src/a.rs::f", "src/a.rs::g"]) == []


def test_a_transaction_is_founded_by_one_flat_member(monkeypatch) -> None:
    assert _run_with(monkeypatch,
                     [_deferred("file", ["src/a.rs::f", "src/a.rs::g"])],
                     flat=["src/a.rs::g"]) == []
    hits = _run_with(monkeypatch,
                     [_deferred("file", ["src/a.rs::f", "src/a.rs::g"])],
                     flat=[])
    assert len(hits) == 1


def test_a_carved_file_piece_is_judged_unit_by_unit(monkeypatch) -> None:
    piece = dict(_deferred("file", ["src/a.rs::f", "src/a.rs::g"]),
                 parent="file:0::src/a.rs:file src/a.rs")
    hits = _run_with(monkeypatch, [piece], flat=["src/a.rs::g"])
    assert len(hits) == 1 and "src/a.rs::f" in hits[0]["evidence"]


def test_reverts_and_sessions_are_not_judged(monkeypatch) -> None:
    assert _run_with(monkeypatch, [
        _deferred("file", ["src/a.rs::f"], reason="rebuy_reverted"),
        _deferred("session", ["src/a.rs::f"])], flat=[]) == []


def test_a_recorded_standing_set_is_judged_without_a_service_order_event(
        monkeypatch) -> None:
    """The service-order event is emitted only when dispatch order moved.
    A fill that sat still still records the set it ranked on, and an empty
    map founds nothing."""
    deferred = [_deferred("per-fn", ["src/a.rs::f"])]
    monkeypatch.setattr(census, "_p32_events_by_run", lambda: {"t": {
        "owner_map": [], "service_order": [],
        "schedule": [{"iter": 1, "deferred": deferred,
                      "inputs": {"standing_flat": {}}}]}})
    hits = census.detect_p38(census.Run("t", "g", "c", "ts", "s:1"), [])
    assert len(hits) == 1 and "src/a.rs::f" in hits[0]["evidence"]

    monkeypatch.setattr(census, "_p32_events_by_run", lambda: {"t": {
        "owner_map": [], "service_order": [],
        "schedule": [{"iter": 1, "deferred": deferred,
                      "inputs": {"standing_flat": {"src/a.rs::f": "sha"}}}]}})
    assert census.detect_p38(census.Run("t", "g", "c", "ts", "s:1"), []) == []


def test_a_different_clusters_flat_list_does_not_judge_this_fill(
        monkeypatch) -> None:
    """The other cluster recorded an empty flat list. Joining this fill to
    that list would call the deferral unfounded. The fill is unjudgeable
    until its own cluster's event is there, and that event founds it."""
    deferred = [_deferred("per-fn", ["src/a.rs::f"])]
    monkeypatch.setattr(census, "_p32_events_by_run", lambda: {"t": {
        "owner_map": [],
        "service_order": [{"iter": 1, "cluster": "other", "flat": []}],
        "schedule": [{"iter": 1, "cluster": "stable", "deferred": deferred}]}})
    assert census.detect_p38(census.Run("t", "g", "c", "ts", "s:1"), []) == []

    monkeypatch.setattr(census, "_p32_events_by_run", lambda: {"t": {
        "owner_map": [],
        "service_order": [
            {"iter": 1, "cluster": "other", "flat": []},
            {"iter": 1, "cluster": "stable", "flat": ["src/a.rs::f"]}],
        "schedule": [{"iter": 1, "cluster": "stable",
                      "deferred": deferred}]}})
    assert census.detect_p38(census.Run("t", "g", "c", "ts", "s:1"), []) == []


def test_a_run_without_service_order_events_cannot_hit(monkeypatch) -> None:
    monkeypatch.setattr(census, "_p32_events_by_run", lambda: {"t": {
        "owner_map": [], "service_order": [],
        "schedule": [{"iter": 1, "deferred": [
            _deferred("per-fn", ["src/a.rs::f"])]}]}})
    assert census.detect_p38(census.Run("t", "g", "c", "ts", "s:1"), []) == []
