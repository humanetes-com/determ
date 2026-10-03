"""The coverage readings a run emits are its per-file record.

The monitor's files overview and `(→ N%)` projection are built from this run's
`coverage.measured` / `coverage.quick_measured` events (`events_data.
coverage_reading`), so each whole reading has to carry its per-file map. They
used to be read from `/tmp/grcov_filecache_*.json`, which the test suite wrote
and deleted under a live run. Driven through the composed App with the backend
stubbed: the seam under test is the composition's, not a backend's.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline
from pipeline_core.domain.coverage import CoverageMeasurement, PerFileCoverage


def _measurement(ws: Path) -> CoverageMeasurement:
    return CoverageMeasurement(50.0, [
        PerFileCoverage.from_dict({"abs_path": str(ws / "src" / "a.rs"), "covered": 3, "coverable": 6}),
        PerFileCoverage.from_dict({"abs_path": str(ws / "src" / "b.rs"), "covered": 1, "coverable": 2}),
    ], 4, 8)


def _events(path: Path, name: str) -> list[dict]:
    if not path.exists():
        return []
    recs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in recs if r.get("event") == name]


def test_a_quick_check_emits_the_whole_reading_and_answers_its_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = tmp_path / "events.jsonl"
    monkeypatch.setattr(pipeline, "_EVENTS_LOG", str(ev))
    ws = tmp_path / "ws"
    monkeypatch.setattr(pipeline._APP.coverage_provider, "measure_quick",
                        lambda pd, mod: _measurement(pd))
    quick = pipeline._APP.task0c_deps.quick_file_coverage
    assert quick(ws, "mod", "src/a.rs") == (3, 6)
    [rec] = _events(ev, "coverage.quick_measured")
    assert rec["file"] == "src/a.rs" and rec["module"] == "mod"
    assert rec["data"]["files"] == {"src/a.rs": [3, 6], "src/b.rs": [1, 2]}
    assert rec["data"]["file_count"] == 2
    # A backend with no quick path answers nothing and records nothing.
    monkeypatch.setattr(pipeline._APP.coverage_provider, "measure_quick", lambda pd, mod: None)
    assert quick(ws, "mod", "src/a.rs") is None
    assert len(_events(ev, "coverage.quick_measured")) == 1


def test_a_full_measurement_event_carries_its_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = tmp_path / "events.jsonl"
    monkeypatch.setattr(pipeline, "_EVENTS_LOG", str(ev))
    ws = tmp_path / "ws"
    monkeypatch.setattr(pipeline._APP.coverage_provider, "measure",
                        lambda pd, mod: _measurement(pd))
    augmentor = pipeline._APP.task0c_deps.test_augmentor_factory()
    assert augmentor.measure_coverage(ws, "mod", ws).covered == 4
    [rec] = _events(ev, "coverage.measured")
    assert rec["data"]["files"] == {"src/a.rs": [3, 6], "src/b.rs": [1, 2]}
    assert rec["data"]["file_count"] == 2
