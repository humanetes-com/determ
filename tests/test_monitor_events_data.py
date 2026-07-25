"""P6 data-vs-render split: the monitor's events.jsonl readers (relocated to
pipeline_core.monitor.events_data) roll up the live run correctly and stay
behind the run-start filter."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.monitor import events_data as ed


def _write(p: Path, *recs: dict) -> None:
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _patch(monkeypatch, path: Path, run_start: float = 0.0) -> None:
    monkeypatch.setattr(ed, "EVENTS_LOG", str(path))
    monkeypatch.setattr(ed, "pipeline_start_time", lambda: run_start)


def test_read_recent_events_filters_name_and_run_start(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "x.keep", "data": {"n": 1}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "y.drop", "data": {}},
        {"bad": "json-but-no-event"},
        {"ts": "1999-01-01T00:00:00+00:00", "event": "x.keep", "data": {"old": True}},
    )
    # run_start in 2026 → the 1999 record is dropped, the y.drop is name-filtered.
    _patch(monkeypatch, ev, run_start=1_750_000_000.0)
    got = ed.read_recent_events(lambda n: n == "x.keep")
    assert len(got) == 1 and got[0]["data"]["n"] == 1


def test_task_progression_from_events(tmp_path, monkeypatch):
    # debt#7: stage progression derived from structured events (no log scrape).
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"event": "task.start", "task": "task0a"},
        {"event": "task.start", "task": "task0b"},
        {"event": "cluster.done", "cluster": "stable"},
        {"event": "cluster.done", "cluster": "extra_cap"},
        {"event": "module.iter", "module": "microcrate", "iteration": 0},
        {"event": "task.start", "task": "task1"},
        {"event": "task.start", "task": "task2"},
        {"event": "task.start", "task": "task3"},
        {"event": "task.done", "task": "task3", "data": {"passed": True}},
        {"event": "task.start", "task": "task4"},
    )
    _patch(monkeypatch, ev, run_start=0.0)
    tp = ed.task_progression()
    assert tp is not None
    assert tp["module"] == "microcrate" and tp["iteration"] == 0
    assert tp["task0c_clusters_done"] == 2
    assert tp["task3_pass"] is True
    assert tp["current"] == "task4"
    s = tp["states"]
    assert all(s[t] == "done" for t in
               ("task0a", "task0b", "task0c", "strip", "task1", "task2", "task3"))
    assert s["task4"] == "in_progress"


def test_task_progression_resets_per_iteration(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"event": "task.start", "task": "task2"},
        {"event": "task.done", "task": "task3", "data": {"passed": False}},
        {"event": "module.iter", "module": "m", "iteration": 1},  # new iter resets t2/3/4
    )
    _patch(monkeypatch, ev, run_start=0.0)
    tp = ed.task_progression()
    assert tp is not None and tp["iteration"] == 1
    assert tp["states"]["task2"] == "pending"  # reset by module.iter
    assert tp["states"]["task3"] == "pending"


def test_task_progression_baseline_without_events(tmp_path, monkeypatch):
    # debt#7: events are now the sole source — no events → all-pending baseline.
    ev = tmp_path / "events.jsonl"
    _write(ev, {"event": "llm.request", "model": "m"})  # unrelated event only
    _patch(monkeypatch, ev, run_start=0.0)
    tp = ed.task_progression()
    assert tp["current"] is None and tp["iteration"] is None
    assert all(v == "pending" for v in tp["states"].values())


def test_pipeline_log_facts_replay(tmp_path, monkeypatch):
    # debt#7: task0c progress facts derived from structured events (no log scrape).
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"event": "compiler.cluster.start", "cluster": "stable"},
        {"event": "task0c.attempt", "data": {"attempt": 2, "max_attempt": 5}},
        {"event": "task0c.planner", "file": "src/lib.rs"},
        {"event": "task0c.file", "file": "src/error.rs", "data": {"idx": 1, "total": 3}},
        {"event": "task0c.compile_error", "file": "src/error.rs"},
        {"event": "task0c.quick_retry", "file": "src/lib.rs", "data": {"retry": 2, "max": 4}},
        {"event": "task0c.gen_tests_appended", "file": "src/lib.rs",
         "data": {"pass_n": 3, "uncovered": 12}},
        {"event": "task0c.quick_cov", "file": "src/lib.rs",
         "data": {"covered": 50, "coverable": 60, "pct": 83.3}},
        {"event": "task0c.quick_cov", "file": "src/lib.rs",
         "data": {"covered": 55, "coverable": 60, "pct": 91.6}},
        {"event": "task0c.queue", "data": {"files": [["error.rs", 4], ["lib.rs", 2]]}},
    )
    _patch(monkeypatch, ev, run_start=0.0)
    p = ed.pipeline_log_facts()
    assert p["attempt"] == "2" and p["max_attempt"] == "5"
    assert p["current_cluster"] == "stable"
    assert p["errors"] == 1
    assert "src/lib.rs" in p["all_files"]
    assert "src/error.rs" in p["cluster_files"] and "src/lib.rs" in p["cluster_files"]
    assert p["current_file"] == "src/error.rs"      # last task0c.file wins
    assert p["inner_retry"] == "2" and p["inner_retry_max"] == "4"
    assert p["file_history"]["src/lib.rs"] == [12]
    assert p["quick_cov_history"]["src/lib.rs"] == [83.3, 91.6]
    assert p["queue_history"]["error.rs"] == [4]


def test_pipeline_log_facts_resets(tmp_path, monkeypatch):
    # task0c.attempt clears files_this_pass + inner_retry; task0c.file clears retry;
    # quick_retry_done clears the retry counter; new cluster clears cluster_files.
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"event": "compiler.cluster.start", "cluster": "stable"},
        {"event": "task0c.planner", "file": "src/a.rs"},
        {"event": "compiler.cluster.start", "cluster": "nightly"},  # resets cluster_files
        {"event": "task0c.quick_retry", "file": "src/b.rs", "data": {"retry": 1, "max": 2}},
        {"event": "task0c.quick_retry_done", "file": "src/b.rs"},   # clears counter
        {"event": "task0c.attempt", "data": {"attempt": 1, "max_attempt": 3}},
    )
    _patch(monkeypatch, ev, run_start=0.0)
    p = ed.pipeline_log_facts()
    assert p["current_cluster"] == "nightly"
    assert p["cluster_files"] == []          # cleared on the nightly cluster.start
    assert p["files_this_pass"] == []        # cleared on task0c.attempt
    assert p["inner_retry"] == "?" and p["inner_retry_max"] == "?"


def test_vllm_runtime_replay(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"event": "vllm.budget", "data": {"request_max": 8000}},
        {"event": "vllm.input_retry", "data": {"request_max": 4096, "cpt": 3.1}},
        {"event": "vllm.input_retry", "data": {"request_max": 4096, "cpt": 3.2}},
        {"event": "vllm.cpt_update", "data": {"cpt": 3.4, "request_max": 4096}},
    )
    _patch(monkeypatch, ev, run_start=0.0)
    vr = ed.vllm_runtime()
    assert vr["max_tokens"] == 4096      # latest request_max
    assert vr["cpt"] == 3.4              # latest cpt
    assert vr["retries"] == 2           # two input_retry events


def test_vllm_runtime_empty(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(ev, {"event": "other", "data": {}})
    _patch(monkeypatch, ev, run_start=0.0)
    assert ed.vllm_runtime() == {"max_tokens": None, "cpt": None, "retries": 0}


def test_coverage_from_events_latest_wins(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "coverage.measured",
         "data": {"pct": 50.0}},
        {"ts": "2026-06-10T00:01:00+00:00", "event": "coverage.measured",
         "data": {"pct": 83.871}},
    )
    _patch(monkeypatch, ev)
    assert ed.coverage_from_events() == "83.9%"


def test_phase_c_state_rollup(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.pass.start",
         "data": {"cluster": "stable", "pass_n": 1, "max_passes": 3, "stubs": 4}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "a", "fix_attempts": 1, "body_chars": 10}},
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "b", "fix_attempts": 3, "recovered": True, "body_chars": 20}},
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.fn.fill.fail",
         "data": {"fn": "c", "fix_attempts": 2, "error_codes": ["E0277"],
                  "last_error_preview": "error[E0277]: x"}},
        {"ts": "2026-06-10T00:00:04+00:00", "event": "phase_c.note.added", "data": {}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["active"] and s["cluster"] == "stable" and s["pass_n"] == 1
    assert s["fills_done"] == 2 and s["fills_first"] == 1 and s["fills_recovered"] == 1
    assert s["fills_failed"] == 1 and s["notes"] == 1
    assert s["last_error_code"] == "E0277" and s["err_codes"]["E0277"] == 1
    assert s["hot_fn"] == ("b", 3)  # most cumulative attempts


def test_phase_c_state_tdd_skip_passdone_test_clusterdone(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.fn.fill.start",
         "data": {"fn": "inflight", "cluster": "nightly"}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.fn.fill.skip", "data": {}},
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.pass.done",
         "data": {"remaining": 7}},
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.pass.test",
         "data": {"rc": 1, "failed_tests": ["t1", "t2"]}},
        {"ts": "2026-06-10T00:00:04+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "nightly", "iter": 2, "pass_count": 9,
                  "fail_count": 3, "frontier_size": 5, "stall": 1, "kind": "outer"}},
        {"ts": "2026-06-10T00:00:05+00:00", "event": "cluster.done", "cluster": "nightly"},
        # A trailing fill.start with no matching done → stays in-flight.
        {"ts": "2026-06-10T00:00:06+00:00", "event": "phase_c.fn.fill.start",
         "data": {"fn": "inflight", "cluster": "nightly"}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["fills_skipped"] == 1 and s["stubs_remaining"] == 7
    assert s["last_test_rc"] == 1 and s["last_test_failed"] == 2
    assert s["tdd_iter"] == 2 and s["tdd_pass"] == 9 and s["tdd_fail"] == 3
    assert s["tdd_by_cluster"]["nightly"]["pass"] == 9
    assert "nightly" in s["clusters_done"]
    assert s["current_fn"] == "inflight" and s["current_attempt"] == 0


def test_phase_c_impl_fills_and_parallel_in_flight(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        # Iter 1 opens the frontier; an impl block + a per-fn re-fill run in
        # parallel; the impl finishes (2 members), the fn stays in-flight.
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 1, "pass_count": 0,
                  "fail_count": 21, "frontier_size": 10, "stall": 0}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.impl.fill.start",
         "data": {"cluster": "stable", "file": "src/fmt.rs",
                  "container": "impl ErrorImpl", "stub_count": 2}},
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.fn.fill.start",
         "data": {"cluster": "stable", "file": "src/lib.rs", "fn": "render",
                  "refill": True}},
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.impl.fill.done",
         "data": {"container": "impl ErrorImpl", "members_filled": 2,
                  "fix_attempts": 1}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    # impl members count toward the frontier segment; fn.render is still in flight.
    assert s["pass_fills_done"] == 2 and s["tdd_frontier"] == 10
    assert [u["label"] for u in s["in_flight"]] == ["render"]
    assert s["in_flight"][0]["kind"] == "fn" and s["in_flight"][0]["refill"] is True
    assert s["current_fn"] == "render"


def test_phase_c_file_fill_counts_and_fallback_drops_file_unit(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 1, "frontier_size": 10}},
        # A whole-file fill succeeds → 4 fns counted toward the frontier.
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.file.fill.start",
         "data": {"cluster": "stable", "file": "src/fmt.rs", "blocks": 2, "stubs": 4}},
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.file.fill.done",
         "data": {"file": "src/fmt.rs", "fns_filled": 4}},
        # A second file-fill falls back to per-block → its file unit is superseded.
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.file.fill.start",
         "data": {"cluster": "stable", "file": "src/ctx.rs", "blocks": 1, "stubs": 2}},
        {"ts": "2026-06-10T00:00:04+00:00", "event": "phase_c.impl.fill.start",
         "data": {"cluster": "stable", "file": "src/ctx.rs", "container": "impl Ctx",
                  "stub_count": 2}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["pass_fills_done"] == 4
    assert [(u["label"], u["kind"]) for u in s["in_flight"]] == [("impl Ctx", "impl")]


def test_phase_c_tdd_iter_clears_stale_in_flight(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 1, "frontier_size": 3}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "a", "fix_attempts": 1}},
        # A fill.start with no matching done, then a fresh iter must drop that
        # dangling in-flight unit (cumulative fills_done still keeps climbing).
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.fn.fill.start",
         "data": {"fn": "stale"}},
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 2, "frontier_size": 2}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["tdd_frontier"] == 2
    assert s["fills_done"] == 1 and s["in_flight"] == []


def test_phase_c_state_empty(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(ev, {"ts": "2026-06-10T00:00:00+00:00", "event": "other", "data": {}})
    _patch(monkeypatch, ev)
    assert ed.phase_c_state()["active"] is False


def test_read_recent_events_no_file(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path / "does-not-exist.jsonl")
    assert ed.read_recent_events(lambda n: True) == []
    assert ed.coverage_from_events() == ""
    assert ed.phase_c_state()["active"] is False


def test_latest_compile_file_stats(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "compile.file_stats",
         "data": {"src/a.rs": [3, 1]}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "compile.file_stats",
         "data": {"src/a.rs": [4, 0]}},  # latest wins
    )
    _patch(monkeypatch, ev)
    assert ed.latest_compile_file_stats() == {"src/a.rs": [4, 0]}


def test_latest_test_and_flow_audit(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "tests.file_stats",
         "data": {"src/a.rs": [1, 0]}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "flow_audit.report",
         "data": {"failed": 0, "verdicts": []}},
    )
    _patch(monkeypatch, ev)
    assert ed.latest_test_file_stats() == {"src/a.rs": [1, 0]}
    assert ed.latest_flow_audit() == {"failed": 0, "verdicts": []}
    assert ed.latest_compile_file_stats() is None  # none emitted


def test_configure_sets_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "EVENTS_LOG", "sentinel")
    target = str(tmp_path / "x.jsonl")
    ed.configure(events_log=target)
    assert target == ed.EVENTS_LOG


def test_phase_c_state_driver_tdd_without_any_pass_start(tmp_path, monkeypatch):
    """The frontier holding only behavioural re-fills (zero stubs) short-circuits
    the per-fn engine before `phase_c.pass.start` — the run is healthy but has no
    pass counter at all. The tdd iter must still identify the live loop."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 4, "pass_count": 93,
                  "fail_count": 18, "stall": 2, "max_stalls": 3}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.file.fill.start",
         "data": {"cluster": "stable", "file": "src/error.rs", "blocks": 7}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["driver"] == "tdd" and s["tdd_max_stalls"] == 3
    assert s["pass_n"] is None and s["tdd_iter"] == 4


def test_phase_c_state_driver_per_fn_when_no_tdd_iter(tmp_path, monkeypatch):
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.pass.start",
         "data": {"cluster": "stable", "pass_n": 2, "max_passes": 3, "stubs": 5}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["driver"] == "per-fn" and s["pass_n"] == 2
