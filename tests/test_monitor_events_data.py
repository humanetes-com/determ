"""Data-vs-render split: the monitor's events.jsonl readers (relocated to
pipeline_core.monitor.events_data) roll up the live run correctly and stay
behind the run-start filter."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.reasoning_cap import CutRate, CutStopKnobs, mark_of
from pipeline_core.monitor import events_data as ed

_CUT_RATE_FIXTURES = Path(__file__).parent / "fixtures" / "cut_rate"


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


def test_read_recent_events_scopes_by_run_id_not_by_a_clock(tmp_path, monkeypatch):
    """A restart inside the 1 MB window must show the NEW run only, and must
    not depend on a start time that can be wrong.

    Live on 2026-08-04: `pipeline_start_time` read the mtime of the
    supervisor's `/proc/{pid}` — which drifts during the process's life — and
    returned 19:15:33 UTC for a run begun at 16:56:21. Every event of the live
    run then sorted as older than its own start and was dropped, so the whole
    Phase C panel rendered `?/?`, `0/0` and no pass count while the run was
    healthy and filling. Scoping on the id the emitter already writes cannot
    fail that way."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-08-04T16:00:00+00:00", "event": "x.keep",
         "run_id": "old-run", "data": {"n": "previous"}},
        {"ts": "2026-08-04T16:56:23+00:00", "event": "x.keep",
         "run_id": "525803-9a51be5a", "data": {"n": "live"}},
        {"ts": "2026-08-04T17:10:00+00:00", "event": "x.keep",
         "run_id": "525803-9a51be5a", "data": {"n": "live2"}},
    )
    # A start time hours AFTER every record — the exact failure observed. The
    # id filter must ignore it entirely.
    _patch(monkeypatch, ev, run_start=4_102_444_800.0)
    got = ed.read_recent_events(lambda n: n == "x.keep")
    assert [g["data"]["n"] for g in got] == ["live", "live2"]


def test_read_recent_events_falls_back_to_the_clock_without_a_run_id(
        tmp_path, monkeypatch):
    """Records predating the emitter's `run_id` keep the timestamp filter."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "1999-01-01T00:00:00+00:00", "event": "x.keep", "data": {"n": "old"}},
        {"ts": "2026-06-10T00:00:00+00:00", "event": "x.keep", "data": {"n": "new"}},
    )
    _patch(monkeypatch, ev, run_start=1_750_000_000.0)
    got = ed.read_recent_events(lambda n: n == "x.keep")
    assert [g["data"]["n"] for g in got] == ["new"]


def test_task_progression_from_events(tmp_path, monkeypatch):
    # Stage progression derived from structured events (no log scrape).
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
    # Events are now the sole source — no events → all-pending baseline.
    ev = tmp_path / "events.jsonl"
    _write(ev, {"event": "llm.request", "model": "m"})  # unrelated event only
    _patch(monkeypatch, ev, run_start=0.0)
    tp = ed.task_progression()
    assert tp["current"] is None and tp["iteration"] is None
    assert all(v == "pending" for v in tp["states"].values())


def test_pipeline_log_facts_replay(tmp_path, monkeypatch):
    # task0c progress facts derived from structured events (no log scrape).
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


def test_phase_c_tdd_iter_resets_the_per_sweep_counters(tmp_path, monkeypatch):
    """The per-sweep counters must reset on tdd.iter, not wait for a
    pass.start that a fully-bodied tree never emits.

    On run 500301-6a66bf86 (2026-08-15) the per-fn engine — the only emitter
    of pass.start — short-circuited on a frontier of pure behavioural
    re-fills, so nothing reset these counters at all. They are rendered
    against the per-iteration frontier, so left cumulative they read further
    past their own batch every iteration. The cluster-scope snapshot is
    deliberately kept: it is a slow whole-cluster reading, and a stale one
    beats none.
    """
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        # Iter 1: a pass.start (stubs still exist) + two landed fills.
        {"ts": "2026-06-10T00:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 1, "frontier_size": 9}},
        {"ts": "2026-06-10T00:00:01+00:00", "event": "phase_c.pass.start",
         "data": {"cluster": "stable", "pass_n": 1, "stubs": 4,
                  "total_fns_in_cluster": 20, "filled_count": 16,
                  "all_stubs_remaining": 4}},
        {"ts": "2026-06-10T00:00:02+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "a", "fix_attempts": 1}},
        {"ts": "2026-06-10T00:00:03+00:00", "event": "phase_c.fn.fill.fail",
         "data": {"fn": "b"}},
        # Iter 2: every fn now has a body, so the frontier is all re-fills and
        # NO pass.start follows. One whole-file fill lands 3 fns.
        {"ts": "2026-06-10T00:01:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 2, "frontier_size": 6}},
        {"ts": "2026-06-10T00:01:01+00:00", "event": "phase_c.file.fill.start",
         "data": {"cluster": "stable", "file": "src/fmt.rs", "blocks": 2,
                  "stubs": 0, "refills": 3}},
        {"ts": "2026-06-10T00:01:02+00:00", "event": "phase_c.file.fill.done",
         "data": {"file": "src/fmt.rs", "fns_filled": 3}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    # Iter 2's units only — not 3+1 done / 1 failed carried from iter 1.
    assert s["pass_fills_done"] == 3 and s["pass_fills_failed"] == 0
    # Iter 1's stub batch must not be reused as iter 2's denominator; with it
    # cleared the sweep bar falls back to iter 2's own frontier.
    assert s["pass_stubs_total"] == 0 and s["tdd_frontier"] == 6
    # The cluster-scope snapshot survives the iteration boundary.
    assert s["filled_count_at_pass_start"] == 16
    assert s["all_stubs_remaining_at_pass_start"] == 4
    # Cumulative counters are untouched by the per-sweep reset: iter 1's one
    # per-fn fill plus iter 2's three whole-file fns.
    assert s["fills_done"] == 4 and s["fills_failed"] == 1


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


def test_pipeline_log_facts_reports_whether_the_run_had_a_task0c_phase(tmp_path, monkeypatch):
    """The loop-hierarchy rows are fed ONLY by task0c events. A resumed run
    skips task0c entirely — the clusters are baked and the finished ones are
    checkpointed — so those rows render `attempt ?/? · file ? 0/0` for the whole
    run: honest and useless. The renderer needs to know which case it is in."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-08-05T00:00:00+00:00", "run_id": "r1",
         "event": "compiler.cluster.start", "cluster": "nightly", "data": {}},
        {"ts": "2026-08-05T00:00:01+00:00", "run_id": "r1",
         "event": "phase_c.tdd.iter", "data": {"cluster": "nightly", "iter": 3}},
    )
    _patch(monkeypatch, ev)
    facts = ed.pipeline_log_facts()
    assert facts["has_task0c"] is False
    assert facts["attempt"] == "?" and facts["max_attempt"] == "?"

    ev2 = tmp_path / "events2.jsonl"
    _write(
        ev2,
        {"ts": "2026-08-05T00:00:00+00:00", "run_id": "r2",
         "event": "task0c.attempt", "data": {"attempt": 2, "max_attempt": 10}},
    )
    _patch(monkeypatch, ev2)
    facts2 = ed.pipeline_log_facts()
    assert facts2["has_task0c"] is True
    assert facts2["attempt"] == "2" and facts2["max_attempt"] == "10"


def test_phase_c_state_collects_tdd_iter_timestamps(tmp_path, monkeypatch):
    """Consecutive tdd.iter timestamps are whole-iteration durations (one
    summary per iter) — the panel's `~N/iter` median reads them in arrival
    order."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-08-09T10:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 1, "pass_count": 11,
                  "fail_count": 94}},
        {"ts": "2026-08-09T10:30:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 2, "pass_count": 76,
                  "fail_count": 28}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["tdd_iter_ts"] == [
        "2026-08-09T10:00:00+00:00", "2026-08-09T10:30:00+00:00"]


def test_phase_c_state_tracks_newest_fill_timestamp(tmp_path, monkeypatch):
    """The newest fill's age is the finest-grain stall signal — any of the
    three fill shapes (fn / impl / file), done or fail, must refresh it."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-08-09T10:00:00+00:00", "event": "phase_c.fn.fill.done",
         "data": {"fn": "erase", "cluster": "stable"}},
        {"ts": "2026-08-09T10:05:00+00:00", "event": "phase_c.impl.fill.fail",
         "data": {"container": "impl Error", "cluster": "stable"}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["last_fill_ts_iso"] == "2026-08-09T10:05:00+00:00"


def test_phase_c_terminal_events_without_done_still_clear_in_flight(tmp_path, monkeypatch):
    """`file.fill.revert` and `impl.fill.already_complete` end their unit
    without a done/fail event. Unhandled (live, run 1159463-b1e352e5,
    2026-08-09), a reverted src/lib.rs file unit and an already-complete impl
    unit rendered as in-flight for 8.6 h and 5.6 h, crowding the real fills
    out of the 4-slot in-flight display."""
    ev = tmp_path / "events.jsonl"
    _write(
        ev,
        {"ts": "2026-08-09T13:00:00+00:00", "event": "phase_c.tdd.iter",
         "data": {"cluster": "stable", "iter": 3, "frontier_size": 34}},
        {"ts": "2026-08-09T13:26:49+00:00", "event": "phase_c.file.fill.start",
         "data": {"cluster": "stable", "file": "src/lib.rs"}},
        {"ts": "2026-08-09T13:30:47+00:00", "event": "phase_c.file.fill.revert",
         "data": {"file": "src/lib.rs", "attempt": 1}},
        {"ts": "2026-08-09T16:32:09+00:00", "event": "phase_c.impl.fill.start",
         "data": {"cluster": "stable", "file": "src/error.rs",
                  "container": "impl From<Error> for Box<dyn StdError>",
                  "stub_count": 1}},
        {"ts": "2026-08-09T16:47:45+00:00",
         "event": "phase_c.impl.fill.already_complete",
         "data": {"container": "impl From<Error> for Box<dyn StdError>"}},
    )
    _patch(monkeypatch, ev)
    s = ed.phase_c_state()
    assert s["in_flight"] == []
    # Neither shape moves the outcome counters: the fns involved are
    # re-dispatched by a later pass, which is where their outcome is counted.
    assert s["fills_done"] == 0 and s["fills_failed"] == 0


def test_read_recent_events_window_survives_a_long_iteration(tmp_path, monkeypatch):
    """9 h into iter 3 (run 1159463-b1e352e5, 2026-08-09) the 1 MB tail no
    longer reached the previous tdd.iter stamp, so the pace bar — which needs
    two stamps for a median — vanished exactly when the iteration ran long.
    The window must survive >1 MB of newer events."""
    ev = tmp_path / "events.jsonl"
    pad = "x" * 4096
    recs = [{"ts": "2026-08-09T13:06:48+00:00", "event": "phase_c.tdd.iter",
             "data": {"cluster": "stable", "iter": 2, "pass_count": 76}}]
    recs += [{"ts": "2026-08-09T14:00:00+00:00", "event": "llm.response",
              "data": {"pad": pad}} for _ in range(400)]  # ~1.6 MB after the stamp
    _write(ev, *recs)
    _patch(monkeypatch, ev)
    got = ed.read_recent_events(lambda n: n == "phase_c.tdd.iter")
    assert len(got) == 1


def _cut_rate_fixture_events(run_id: str) -> list[dict]:
    """This run's recorded `llm.stream_meta` rows (`test_cut_rate_stop.py`'s
    own fixtures — one process's real cut-rate history), reshaped into the
    on-disk envelope `JsonlEventEmitter` writes."""
    rows = json.loads((_CUT_RATE_FIXTURES / f"{run_id}.json").read_text())["rows"]
    return [{"ts": "2026-09-21T00:00:00+00:00", "event": "llm.stream_meta",
              "run_id": run_id, "data": row} for row in rows]


def test_cut_rate_reading_matches_the_in_process_fold_on_the_recorded_positive(
        tmp_path, monkeypatch):
    """Run `450977-5f48b68a`: 3 of its first 25 calls cut, the recorded
    positive fixture for the run-level stop (`test_cut_rate_stop.py`). The
    monitor's replay-from-disk reading must equal the same fold computed
    in-process over the identical rows — the negative control is that this
    is a real recorded fixture, not a hand-built list the fold is guaranteed
    to fire on."""
    ev = tmp_path / "events.jsonl"
    recs = _cut_rate_fixture_events("450977-5f48b68a")
    _write(ev, *recs)
    _patch(monkeypatch, ev)
    knobs = CutStopKnobs()  # 4%/100 = 4 cut calls, the shipped defaults
    got = ed.cut_rate_reading(knobs)
    want = CutRate(knobs.window, knobs.share_pct)
    for r in recs:
        want = want.add(mark_of(r["data"], ""))
    assert got.share == want.share and got.cut == want.cut
    assert got.fired and want.fired


def test_cut_rate_reading_never_fires_on_the_recorded_negative(tmp_path, monkeypatch):
    """Run `330242-0be2c3e1`: 216 calls whose rate never reaches 4%."""
    ev = tmp_path / "events.jsonl"
    recs = _cut_rate_fixture_events("330242-0be2c3e1")
    _write(ev, *recs)
    _patch(monkeypatch, ev)
    got = ed.cut_rate_reading(CutStopKnobs())
    assert not got.fired


def test_cut_rate_reading_excludes_bookkeeping_rows(tmp_path, monkeypatch):
    """A row the controller stamped `bookkeeping: true` (the self-note
    extractor's empty-then-retry ladder) must not count toward the gauge —
    `record_stream_meta` excludes the identical rows from the in-process
    fold via the same flag."""
    ev = tmp_path / "events.jsonl"
    empty = {"finish_reason": None, "stop": None, "followup": False,
             "content_chars": 0, "prompt_tokens": 100}
    _write(
        ev,
        {"event": "llm.stream_meta", "data": {**empty, "bookkeeping": True}},
        {"event": "llm.stream_meta", "data": {**empty, "bookkeeping": True}},
        {"event": "llm.stream_meta", "data": {**empty, "bookkeeping": True}},
    )
    _patch(monkeypatch, ev)
    got = ed.cut_rate_reading(CutStopKnobs())
    assert got.marks == () and got.seen == 0


def test_fill_reading_sets_the_next_sweep_pass_and_a_sweep_clears_it(tmp_path) -> None:
    """A regressed reading is a reverted keep and is ignored; the sweep's own
    iter event resets the value to the sweep's measurement."""
    import json

    from pipeline_core.monitor import events_data as ed
    log = tmp_path / "events.jsonl"
    evs = [
        {"ts": "2026-09-29T05:00:00+00:00", "run_id": "r", "event": "phase_c.tdd.iter",
         "cluster": "stable", "iter": 1, "pass": 175},
        {"ts": "2026-09-29T05:30:00+00:00", "run_id": "r",
         "event": "phase_c.fill.reading", "pass_count": 182, "regressed": False},
        {"ts": "2026-09-29T05:40:00+00:00", "run_id": "r",
         "event": "phase_c.fill.reading", "pass_count": 181, "regressed": True},
    ]
    log.write_text("\n".join(json.dumps(e) for e in evs) + "\n")
    ed.configure(events_log=str(log))
    assert ed.phase_c_state()["tdd_fill_pass"] == 182
    evs.append({"ts": "2026-09-29T06:30:00+00:00", "run_id": "r",
                "event": "phase_c.tdd.iter", "cluster": "stable", "iter": 2, "pass": 182})
    log.write_text("\n".join(json.dumps(e) for e in evs) + "\n")
    assert ed.phase_c_state()["tdd_fill_pass"] is None
