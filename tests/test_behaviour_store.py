"""`load_behaviour_suite` must not read a CORRUPT suite file as an ABSENT one.

Both return an empty suite (the loader fails open on purpose — behaviours are an
additive oracle), so the only thing that distinguishes them for a human reading a
run is the log line and the event. Before the split, a corrupt
`.skeleton/_behaviours_<cluster>.json` was silent: the channel looked like it had
simply never been harvested. These tests pin the difference in both directions —
missing is quiet, unusable is loud — plus the happy path as the negative control.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline_core.adapters.behaviour_store as bs
from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSuite,
)


@pytest.fixture
def sink(monkeypatch):
    logs: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(bs, "log", logs.append)
    monkeypatch.setattr(bs, "emit", lambda event, **f: events.append((event, f)))
    return logs, events


def _write(project_dir: Path, cluster: str, text: str) -> Path:
    p = bs._suite_path(project_dir, cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_is_an_empty_suite_and_says_nothing(tmp_path, sink):
    logs, events = sink
    suite = bs.load_behaviour_suite(tmp_path, "stable")
    assert suite == BehaviourSuite(cfg_cluster="stable")
    assert logs == [] and events == []


def test_invalid_json_is_empty_but_loud(tmp_path, sink):
    logs, events = sink
    p = _write(tmp_path, "stable", "{not json at all")
    suite = bs.load_behaviour_suite(tmp_path, "stable")
    assert suite == BehaviourSuite(cfg_cluster="stable")
    assert len(logs) == 1 and str(p) in logs[0]
    assert [e for e, _ in events] == ["behaviour.suite.unreadable"]
    data = events[0][1]["data"]
    assert data["path"] == str(p) and data["cfg_cluster"] == "stable"
    assert "JSONDecodeError" in data["error"]


def test_valid_json_of_the_wrong_shape_is_empty_but_loud(tmp_path, sink):
    logs, events = sink
    _write(tmp_path, "nightly", json.dumps({"specs": "notalist"}))
    suite = bs.load_behaviour_suite(tmp_path, "nightly")
    assert suite == BehaviourSuite(cfg_cluster="nightly")
    assert len(logs) == 1
    assert [e for e, _ in events] == ["behaviour.suite.unreadable"]
    assert events[0][1]["data"]["cfg_cluster"] == "nightly"


def test_json_that_is_not_an_object_is_empty_but_loud(tmp_path, sink):
    logs, events = sink
    _write(tmp_path, "stable", json.dumps(["a", "list"]))
    assert bs.load_behaviour_suite(tmp_path, "stable") == BehaviourSuite(cfg_cluster="stable")
    assert len(logs) == 1
    assert [e for e, _ in events] == ["behaviour.suite.unreadable"]


def test_wellformed_suite_round_trips_and_stays_quiet(tmp_path, sink):
    """Negative control: the happy path is untouched by the corrupt-file split."""
    logs, events = sink
    suite = BehaviourSuite(
        cfg_cluster="stable",
        specs=(
            BehaviourSpec(
                behaviour_id="b1",
                setup="let e = anyhow!(\"x\");",
                when="e.to_string()",
                source_ref="tests/test_context.rs:12",
                provenance=BehaviourProvenance.HARVESTED,
            ),
        ),
    )
    path = bs.save_behaviour_suite(tmp_path, suite)
    assert path.exists()
    assert bs.load_behaviour_suite(tmp_path, "stable") == suite
    assert logs == [] and events == []


def test_the_view_directory_holds_only_the_files_of_the_last_write(tmp_path):
    """The view is generated: a file a previous pass wrote and this one does
    not (a feature the book no longer has, the givens index of a book with no
    test-file item) would describe a book that no longer exists."""
    d = bs.write_feature_files(tmp_path, "stable", {"a.feature": "A", "_givens.md": "G"})
    assert sorted(p.name for p in d.iterdir()) == ["_givens.md", "a.feature"]
    bs.write_feature_files(tmp_path, "stable", {"b.feature": "B"})
    assert [p.name for p in d.iterdir()] == ["b.feature"]
