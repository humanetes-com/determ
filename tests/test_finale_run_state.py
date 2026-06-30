"""C-finale: RunState holder + JsonlEventEmitter + commit_and_mark relocations."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.commit_gate import commit_and_mark
from pipeline_core.adapters.telemetry import JsonlEventEmitter
from pipeline_core.domain.run_state import RunState


def test_run_state_defaults() -> None:
    rs = RunState()
    assert rs.git_managed is False and rs.audit_enabled is False
    assert rs.audit_project_dir is None and rs.log_path is None
    assert rs.nolog is False and rs.progress_mode is False and rs.run_started_iso == ""


def test_run_state_is_mutable_holder() -> None:
    rs = RunState()
    rs.git_managed = True
    rs.log_path = Path("/x/run.log")
    assert rs.git_managed and rs.log_path == Path("/x/run.log")


def test_emitter_writes_jsonl_record(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    ctx = {"product": "anyhow", "pipeline_sha": "abc", "run_id": "r1"}
    em = JsonlEventEmitter(lambda: str(log), ctx)
    em.emit("task.start", task="t3", skip=None)  # None fields dropped
    rec = json.loads(log.read_text().strip())
    assert rec["event"] == "task.start" and rec["task"] == "t3"
    assert rec["product"] == "anyhow" and rec["run_id"] == "r1"
    assert "skip" not in rec and "thread" in rec and "tid" in rec


def test_emitter_reads_context_live(tmp_path: Path) -> None:
    ctx = {"product": "unknown", "pipeline_sha": "x", "run_id": ""}
    em = JsonlEventEmitter(lambda: str(tmp_path / "e.jsonl"), ctx)
    ctx["product"] = "later"  # mutated after construction (init_events pattern)
    em.emit("x")
    assert json.loads((tmp_path / "e.jsonl").read_text().strip())["product"] == "later"


def test_emitter_never_raises_on_bad_path() -> None:
    JsonlEventEmitter(lambda: "/nonexistent-dir/zzz/e.jsonl", {}).emit("x")  # no raise


def test_phase_emits_start_and_done(tmp_path: Path) -> None:
    log = tmp_path / "e.jsonl"
    em = JsonlEventEmitter(lambda: str(log), {})
    with em.phase("task3", task="t") as extra:
        extra["data"] = {"passed": True}
    recs = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["event"] for r in recs] == ["task3.start", "task3.done"]
    assert recs[1]["data"] == {"passed": True} and "duration_ms" in recs[1]


def test_phase_done_carries_error_on_exception(tmp_path: Path) -> None:
    log = tmp_path / "e.jsonl"
    em = JsonlEventEmitter(lambda: str(log), {})
    try:
        with em.phase("p"):
            raise ValueError("boom")
    except ValueError:
        pass
    recs = [json.loads(line) for line in log.read_text().splitlines()]
    assert recs[1]["event"] == "p.done" and recs[1]["data"]["error"] == "ValueError"


def test_commit_and_mark_ordering(tmp_path: Path) -> None:
    order: list[str] = []
    commit_and_mark(
        tmp_path, "strip/m", "done", commit_message="msg", tag="tag-x",
        git_commit=lambda p, m: order.append(f"commit:{m}") or True,
        git_tag=lambda p, t: order.append(f"tag:{t}"),
        mark_phase=lambda *a, **k: order.append(f"mark:{a[1]}:{a[2]}"))
    assert order == ["commit:msg", "tag:tag-x", "mark:strip/m:done"]


def test_commit_and_mark_skips_tag_when_none(tmp_path: Path) -> None:
    order: list[str] = []
    commit_and_mark(
        tmp_path, "k", "done", commit_message="m",
        git_commit=lambda p, m: order.append("commit") or True,
        git_tag=lambda p, t: order.append("tag"),
        mark_phase=lambda *a, **k: order.append("mark"))
    assert order == ["commit", "mark"]  # no tag
