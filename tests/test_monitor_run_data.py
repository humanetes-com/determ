"""Data-vs-render split: the monitor's log/state/backend readers (relocated to
pipeline_core.monitor.run_data) parse the pipeline log + pipeline_state.md + .env
+ coverage JSON into plain data."""
from __future__ import annotations

import json

from pipeline_core.monitor import run_data as rd


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


def test_log_tail_meta(tmp_path, monkeypatch):
    log = tmp_path / "p.log"
    log.write_text("first line\n\n  Queue: 2 file(s) — error.rs(4)\n\n")
    monkeypatch.setattr(rd, "PIPELINE_LOG", str(log))
    m = rd.log_tail_meta()
    assert m["last_line"] == "Queue: 2 file(s) — error.rs(4)"  # last non-empty, stripped
    assert m["age"] >= 0.0


def test_log_tail_meta_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "PIPELINE_LOG", str(tmp_path / "nope.log"))
    assert rd.log_tail_meta() == {"last_line": "", "age": 0.0}


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
    rd.configure(script_dir="/s", pipeline_log="/p")
    assert (rd.SCRIPT_DIR, rd.PIPELINE_LOG) == ("/s", "/p")


def test_pipeline_running_reads_registry(tmp_path, monkeypatch):
    import os
    reg = tmp_path / "reg.json"
    monkeypatch.setenv("PIPELINE_RUN_REGISTRY", str(reg))
    # No registry file: no run ever registered.
    assert rd.pipeline_running() is False
    # Malformed / non-list contents parse to "not running", never raise.
    reg.write_text("{not json")
    assert rd.pipeline_running() is False
    reg.write_text(json.dumps({"pid": 1}))
    assert rd.pipeline_running() is False
    # A registered pid that is dead (a run killed before atexit unregistered
    # it): /proc is the arbiter, the stale entry is not "running". pid 0 is
    # never a /proc entry, so it models the reaped run deterministically.
    reg.write_text(json.dumps([{"pid": 0}]))
    assert rd.pipeline_running() is False
    # A registered pid that is alive — even alongside a stale sibling.
    reg.write_text(json.dumps([{"pid": 0}, {"pid": os.getpid()}]))
    assert rd.pipeline_running() is True


def test_pipeline_running_ignores_cmdline_lookalikes(tmp_path, monkeypatch):
    """Negative control for the 2026-08-18 false positive: a live process
    whose ARGV contains 'pipeline.py' (the watcher shell quoting the name in
    its own pgrep loop) must not read as a running pipeline. The old
    `pgrep -f pipeline.py` substring check returned True on exactly this."""
    import os
    import subprocess
    import sys
    reg = tmp_path / "reg.json"
    reg.write_text("[]")
    monkeypatch.setenv("PIPELINE_RUN_REGISTRY", str(reg))
    decoy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         "pipeline.py --project anyhow"])
    try:
        # The decoy is alive and its command line contains the magic string —
        # prove the precondition the old code tripped on, then the assertion.
        assert os.path.exists(f"/proc/{decoy.pid}")
        assert rd.pipeline_running() is False
    finally:
        decoy.kill()
        decoy.wait()
