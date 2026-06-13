"""P6 data-vs-render split: the monitor's log/state/backend readers (relocated to
pipeline_core.monitor.run_data) parse the pipeline log + pipeline_state.md + .env
+ coverage JSON into plain data."""
from __future__ import annotations

import json

from pipeline_core.monitor import run_data as rd


def test_parse_task_pipeline_progression():
    lines = [
        "PIPELINE STARTED — x",
        "TASK 0a: context",
        "TASK 0b discovery",
        "TASK 0c [planner]",
        "pipeline: task0c/stable — [git] committed",
        "pipeline: stripped — [git] committed",
        "MODULE anyhow — ITERATION 3",
        "TASK 1: requirements",
        "TASK 1 output validated OK",
        "TASK 2: generate",
        "TASK 3: run",
        "Tests FAILED",
        "TASK 4: analyze",
    ]
    tp = rd.parse_task_pipeline(lines)
    assert tp["module"] == "anyhow" and tp["iteration"] == 3
    assert tp["states"]["task0c"] == "done" and tp["states"]["strip"] == "done"
    assert tp["states"]["task1"] == "done"
    assert tp["task3_pass"] is False
    assert tp["current"] == "task4"


def test_parse_task_pipeline_reset_on_restart():
    tp = rd.parse_task_pipeline(["TASK 0a:", "PIPELINE STARTED — fresh"])
    assert all(v == "pending" for v in tp["states"].values())
    assert tp["current"] is None


def test_parse_pipeline_state(tmp_path, monkeypatch):
    pd = tmp_path / "ws"
    pd.mkdir()
    (pd / "pipeline_state.md").write_text(
        "2 clusters: stable, nightly\n"
        "[task0c/stable] done\n"
        "[task0c/nightly] in_progress\n"
        "current: task0c/nightly / augment\n"
        "[task0c/stable] 83.40% coverage\n"
    )
    r = rd.parse_pipeline_state(str(pd))
    assert r["clusters"] == ["stable", "nightly"]
    assert r["done"] == {"stable"} and r["current"] == "nightly"
    assert r["coverage"] == "83.40%"
    assert rd.parse_pipeline_state(None)["clusters"] == []


def test_parse_pipeline_log_tail(tmp_path, monkeypatch):
    log = tmp_path / "p.log"
    log.write_text(
        "PIPELINE STARTED — x\n"
        "Attempt 2/5\n"
        "Starting compiler cluster: stable\n"
        "File 1/3: src/error.rs\n"
        "Compile error: boom\n"
        "Coverage: 91.2%\n"
    )
    monkeypatch.setattr(rd, "PIPELINE_LOG", str(log))
    p = rd.parse_pipeline_log()
    assert p["attempt"] == "2" and p["max_attempt"] == "5"
    assert p["current_cluster"] == "stable"
    assert p["errors"] == 1 and p["coverage"] == "91.2%"
    assert "src/error.rs" in p["cluster_files"]


def test_parse_pipeline_log_rich(tmp_path, monkeypatch):
    log = tmp_path / "p.log"
    log.write_text(
        "PIPELINE STARTED — x\n"
        "Attempt 1/3\n"
        "[task0c planner] src/lib.rs: planning\n"
        "quick-retry 2/4 — retrying\n"
        "Appended gen_tests_3 to src/lib.rs (12 uncovered lines)\n"
        "src/lib.rs quick-cov: 50/60 (83.3%)\n"
        "src/lib.rs quick-cov: 55/60 (91.6%)\n"
        "Queue: 2 file(s) — error.rs(4)  lib.rs(2)\n"
    )
    monkeypatch.setattr(rd, "PIPELINE_LOG", str(log))
    p = rd.parse_pipeline_log()
    assert "src/lib.rs" in p["all_files"] and p["current_file"] == "src/lib.rs"
    assert p["inner_retry"] == "2" and p["inner_retry_max"] == "4"
    assert p["file_history"]["src/lib.rs"] == [12]
    assert p["quick_cov_history"]["src/lib.rs"] == [83.3, 91.6]
    assert p["queue_history"]["error.rs"] == [4]
    assert p["last_line"].startswith("Queue:")


def test_parse_pipeline_log_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "PIPELINE_LOG", str(tmp_path / "nope.log"))
    assert rd.parse_pipeline_log()["attempt"] == "?"
    assert rd.read_log_tail() == []


def test_find_project_dir_and_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "SCRIPT_DIR", str(tmp_path))
    assert rd.find_project_dir() is None  # no tmp/ yet
    ws = tmp_path / "tmp" / "anyhow"
    ws.mkdir(parents=True)
    (ws / "pipeline_state.md").write_text("current: task2c/anyhow / fill\n")
    assert rd.find_project_dir() == str(ws)
    assert rd.current_stage_label() == "task2c/anyhow / fill"


def test_parse_coverage_json_tarpaulin(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(rd.events_data, "pipeline_start_time", lambda: 0.0)
    tj = tmp_path / "tarpaulin-report.json"
    tj.write_text(json.dumps({"files": [
        {"path": ["src", "b.rs"], "covered": 5, "coverable": 8},
    ]}))
    # Deterministic: no grcov files, one tarpaulin report (so the fallback runs).
    monkeypatch.setattr(rd.glob, "glob",
                        lambda pat: [str(tj)] if pat.startswith("/tmp/tarp_") else [])
    cov = rd.parse_coverage_json(None)
    assert cov.get(os.path.join("src", "b.rs")) == (5, 8)


def test_baked_cluster_order(tmp_path, monkeypatch):
    log = tmp_path / "p.log"
    log.write_text("loaded 1 baked cluster(s): old\n"
                   "loaded 2 baked cluster(s): stable, nightly\n")
    monkeypatch.setattr(rd, "PIPELINE_LOG", str(log))
    assert rd.baked_cluster_order() == ["stable", "nightly"]  # last wins


def test_read_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "SCRIPT_DIR", str(tmp_path))
    assert rd.read_backend() == "ollama"  # no .env → default
    (tmp_path / ".env").write_text("FOO=1\nBACKEND = VLLM\n")
    assert rd.read_backend() == "vllm"


def test_parse_max_tokens_and_cpt(tmp_path, monkeypatch):
    g = tmp_path / "g.log"
    g.write_text(
        "guard: max_tokens 8000 → 4096\n"
        "vllm/400_retry triggered\n"
        "vllm/400_retry triggered\n"
        "cpt_ewm=3.1→3.4\n"
    )
    monkeypatch.setattr(rd, "GUARDS_LOG", str(g))
    assert rd.parse_max_tokens() == 4096
    cpt, retries = rd.parse_vllm_cpt()
    assert cpt == 3.4 and retries == 2


def test_parse_coverage_json_grcov(tmp_path, monkeypatch):
    monkeypatch.setattr(rd.events_data, "pipeline_start_time", lambda: 0.0)
    gj = tmp_path / "grcov_unit.json"
    gj.write_text(json.dumps({"source_files": [
        {"name": "src/a.rs", "coverage": [1, 0, None, 2]},
    ]}))
    # Deterministic file set: one main grcov report, no filecache/tarpaulin.
    monkeypatch.setattr(rd.glob, "glob",
                        lambda pat: [str(gj)] if pat == "/tmp/grcov_*.json" else [])
    cov = rd.parse_coverage_json(None)
    assert cov["src/a.rs"] == (2, 3)  # 2 covered (>0), 3 coverable (non-None)


def test_configure_sets_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "SCRIPT_DIR", "x")
    monkeypatch.setattr(rd, "PIPELINE_LOG", "x")
    monkeypatch.setattr(rd, "GUARDS_LOG", "x")
    rd.configure(script_dir="/s", pipeline_log="/p", guards_log="/g")
    assert (rd.SCRIPT_DIR, rd.PIPELINE_LOG, rd.GUARDS_LOG) == ("/s", "/p", "/g")
