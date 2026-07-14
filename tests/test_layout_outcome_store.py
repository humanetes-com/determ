"""Pins for the layout-outcome store (F11 M3 precondition — persist which
component t1.5 authored/gated/froze, so a re-trigger can find it later)."""
from __future__ import annotations

from pipeline_core.adapters.layout_outcome_store import (
    load_layout_outcomes,
    save_layout_outcomes,
)
from pipeline_core.application.author_layout import (
    AuthorLayoutResult,
    ComponentResult,
    LayoutOutcome,
)


def _result(cluster="stable"):
    return AuthorLayoutResult(cfg_cluster=cluster, components=(
        ComponentResult(item_names=("Quoted",), authored=True),
        ComponentResult(item_names=("Sealed",), authored=False, gated=True),
        ComponentResult(
            item_names=("ErrorImpl", "ErrorVTable", "Own", "Ref"),
            authored=False, reason="did not compile"),
    ))


def test_round_trip_preserves_outcomes(tmp_path):
    save_layout_outcomes(tmp_path, _result())
    got = load_layout_outcomes(tmp_path, "stable")
    by_names = dict(got)
    assert by_names[frozenset({"Quoted"})] is LayoutOutcome.AUTHORED
    assert by_names[frozenset({"Sealed"})] is LayoutOutcome.GATED
    assert by_names[frozenset({"ErrorImpl", "ErrorVTable", "Own", "Ref"})] is LayoutOutcome.FROZEN


def test_missing_file_returns_empty_list(tmp_path):
    assert load_layout_outcomes(tmp_path, "nightly") == []


def test_per_cluster_isolation(tmp_path):
    save_layout_outcomes(tmp_path, _result("stable"))
    save_layout_outcomes(tmp_path, AuthorLayoutResult(cfg_cluster="nightly", components=()))
    assert load_layout_outcomes(tmp_path, "stable") != []
    assert load_layout_outcomes(tmp_path, "nightly") == []
