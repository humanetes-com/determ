"""Pins for the layout-outcome store (F11 M3 precondition — persist which
component t1.5 authored/gated/froze plus each authored item's locate-key, so a
re-trigger can find it later and re-author against the LIVE skeleton)."""
from __future__ import annotations

import json

from pipeline_core.adapters.layout_outcome_store import (
    load_layout_records,
    save_layout_outcomes,
    update_layout_outcome,
)
from pipeline_core.application.author_layout import (
    AuthorLayoutResult,
    ComponentResult,
    LayoutOutcome,
)


def _result(cluster="stable"):
    return AuthorLayoutResult(cfg_cluster=cluster, components=(
        ComponentResult(item_names=("Quoted",), authored=True,
                        authored_defs=(("Quoted", "struct Quoted(String);"),)),
        ComponentResult(item_names=("Sealed",), authored=False, gated=True),
        ComponentResult(
            item_names=("ErrorImpl", "ErrorVTable", "Own", "Ref"),
            authored=False, reason="did not compile"),
    ))


def _by_names(records):
    return {r.names: r for r in records}


def test_round_trip_preserves_outcomes_and_defs(tmp_path):
    save_layout_outcomes(tmp_path, _result())
    by_names = _by_names(load_layout_records(tmp_path, "stable"))
    assert by_names[frozenset({"Quoted"})].outcome is LayoutOutcome.AUTHORED
    assert by_names[frozenset({"Quoted"})].defs == (
        ("Quoted", "struct Quoted(String);"),)
    assert by_names[frozenset({"Sealed"})].outcome is LayoutOutcome.GATED
    assert by_names[frozenset(
        {"ErrorImpl", "ErrorVTable", "Own", "Ref"})].outcome is LayoutOutcome.FROZEN


def test_missing_file_returns_empty_list(tmp_path):
    assert load_layout_records(tmp_path, "nightly") == []


def test_per_cluster_isolation(tmp_path):
    save_layout_outcomes(tmp_path, _result("stable"))
    save_layout_outcomes(tmp_path, AuthorLayoutResult(cfg_cluster="nightly", components=()))
    assert load_layout_records(tmp_path, "stable") != []
    assert load_layout_records(tmp_path, "nightly") == []


def test_rows_without_defs_load_with_empty_defs(tmp_path):
    # A store written before locate-keys existed must still load (the
    # consumer skips AUTHORED rows without defs instead of crashing).
    p = tmp_path / ".skeleton"
    p.mkdir()
    (p / "_layout_outcomes_stable.json").write_text(json.dumps(
        [{"item_names": ["Own"], "outcome": "AUTHORED"}]), encoding="utf-8")
    rec = load_layout_records(tmp_path, "stable")[0]
    assert rec.outcome is LayoutOutcome.AUTHORED
    assert rec.defs == ()


def test_update_flips_frozen_to_authored_with_defs(tmp_path):
    save_layout_outcomes(tmp_path, _result())
    update_layout_outcome(tmp_path, "stable", ComponentResult(
        item_names=("ErrorImpl", "ErrorVTable", "Own", "Ref"), authored=True,
        authored_defs=(("Own", "struct Own<T> { ptr: *mut T }"),)))
    by_names = _by_names(load_layout_records(tmp_path, "stable"))
    rec = by_names[frozenset({"ErrorImpl", "ErrorVTable", "Own", "Ref"})]
    assert rec.outcome is LayoutOutcome.AUTHORED
    assert rec.defs == (("Own", "struct Own<T> { ptr: *mut T }"),)
    # untouched rows survive the upsert
    assert by_names[frozenset({"Quoted"})].outcome is LayoutOutcome.AUTHORED


def test_update_on_missing_store_is_a_noop(tmp_path):
    assert update_layout_outcome(tmp_path, "stable", ComponentResult(
        item_names=("Own",), authored=True)) is None
