"""Reading `logs/deaths.jsonl` back: which pids ended on a deliberate stop.

The one consumer is the layout retrigger's interrupted-attempt settlement, which
until 2026-08-15 charged a mechanical failure for any process that did not
survive an authoring call — including the operator's own SIGTERM. Two of those
on `anyhow` that day (runs `462635-c8369188` and `496645-9b9de7e1`) filled the
cap of 2 on the 34-name construct/vtable component and locked it out of every
later run.

The rows below are the real ones, trimmed to the fields the reader uses.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.death_record import deliberate_stop_pids

_ROWS = [
    # `continue.sh --y`: the graceful sentinel was seen, so the recorder says so.
    {"pid": 462635, "exit_status": 143, "signal": "TERM",
     "deliberate_stop": True, "uptime_s": 3416},
    # A bare `kill`/`--force`: no sentinel, still an external decision.
    {"pid": 496645, "exit_status": 143, "signal": "TERM",
     "deliberate_stop": False, "uptime_s": 24},
    # SIGKILL is NOT read as a stop — the OOM killer sends it too, and a death
    # whose cause cannot be told apart from a pipeline fault keeps the charge.
    {"pid": 3368, "exit_status": 137, "signal": "KILL",
     "deliberate_stop": False, "uptime_s": 27485},
    # The vanished-with-its-supervisor class: synthesized, no signal at all.
    {"pid": 1082156, "signal": "UNKNOWN_KILLED_WITH_SUPERVISOR"},
]


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    (tmp_path / "deaths.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def test_only_the_externally_stopped_pids_come_back(tmp_path: Path) -> None:
    assert deliberate_stop_pids(_write(tmp_path, _ROWS)) == frozenset(
        {462635, 496645})


def test_a_missing_record_waives_nothing(tmp_path: Path) -> None:
    # The question asked is "may the charge be waived", so no readable evidence
    # must fall back to charging — never to waiving on a file nobody could read.
    assert deliberate_stop_pids(tmp_path) == frozenset()


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path: Path) -> None:
    p = tmp_path / "deaths.jsonl"
    p.write_text(
        json.dumps(_ROWS[0]) + "\n"
        + "{not json at all\n"
        + "\n"
        + json.dumps([1, 2, 3]) + "\n"          # valid JSON, wrong shape
        + json.dumps({"signal": "TERM"}) + "\n"  # no pid
        + json.dumps(_ROWS[1]) + "\n",
        encoding="utf-8")
    assert deliberate_stop_pids(tmp_path) == frozenset({462635, 496645})
