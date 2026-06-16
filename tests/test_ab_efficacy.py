"""R13.1 A/B efficacy comparator — pure metric extraction + verdict over synthetic
event slices (no live run, no log scraping)."""
from __future__ import annotations

from typing import Any

from tools.ab_efficacy import (
    compare,
    load_events,
    run_metrics,
    split_runs,
)


def _run(product: str, *, iters: int, t4: int, t2: int, restores: int,
         fills: int, converged: bool, sha: str = "abc") -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = [
        {"event": "pipeline.start", "ts": f"2026-01-01T00:00:0{iters}", "product": product,
         "pipeline_sha": sha}
    ]
    for i in range(1, iters + 1):
        evs.append({"event": "module.iter", "iteration": i})
    evs += [{"event": "task.start", "task": "task4"}] * t4
    evs += [{"event": "task.start", "task": "task2"}] * t2
    evs += [{"event": "ratchet.restore", "data": {"best": 5, "current": 3}}] * restores
    evs += [{"event": "phase_c.impl.fill.done"}] * fills
    if converged:
        evs.append({"event": "task.done", "task": "task3", "data": {"passed": True}})
    return evs


def test_split_runs_segments_on_pipeline_start() -> None:
    a = _run("hardcrate", iters=3, t4=2, t2=2, restores=1, fills=10, converged=True)
    b = _run("hardcrate", iters=2, t4=1, t2=1, restores=0, fills=6, converged=True)
    runs = split_runs(a + b)
    assert len(runs) == 2 and runs[0][0]["event"] == "pipeline.start"


def test_run_metrics_counts_each_channel() -> None:
    m = run_metrics(_run("hardcrate", iters=3, t4=2, t2=4, restores=1, fills=9, converged=True))
    assert (m.iterations, m.task4_runs, m.task2_runs, m.ratchet_restores, m.fills) == (3, 2, 4, 1, 9)
    assert m.converged and m.product == "hardcrate"


def test_unconverged_run_is_stuck() -> None:
    m = run_metrics(_run("hardcrate", iters=0, t4=1, t2=1, restores=0, fills=2, converged=False))
    assert not m.converged and m.iterations == 0


def test_compare_docs_help_when_cheaper_everywhere() -> None:
    a = run_metrics(_run("h", iters=2, t4=1, t2=2, restores=0, fills=6, converged=True))  # enriched
    b = run_metrics(_run("h", iters=4, t4=3, t2=5, restores=2, fills=14, converged=True))  # blank
    res = compare(a, b)
    assert res["verdict"].startswith("docs help")
    assert res["deltas"]["iterations"] == -2 and res["hurt"] == 0


def test_compare_mixed_and_no_effect() -> None:
    a = run_metrics(_run("h", iters=2, t4=4, t2=2, restores=0, fills=6, converged=True))
    b = run_metrics(_run("h", iters=4, t4=1, t2=2, restores=0, fills=6, converged=True))
    assert compare(a, b)["verdict"].startswith("mixed")          # cheaper iters, costlier t4
    same = run_metrics(_run("h", iters=2, t4=1, t2=2, restores=0, fills=6, converged=True))
    assert compare(same, same)["verdict"].startswith("no effect")


def test_compare_inconclusive_when_a_run_is_stuck() -> None:
    a = run_metrics(_run("h", iters=2, t4=1, t2=2, restores=0, fills=6, converged=True))
    stuck = run_metrics(_run("h", iters=0, t4=1, t2=1, restores=0, fills=2, converged=False))
    assert compare(a, stuck)["verdict"].startswith("inconclusive")


def test_load_events_skips_malformed_lines(tmp_path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text('{"event": "pipeline.start"}\nNOT JSON\n\n["not a dict"]\n', encoding="utf-8")
    evs = load_events(p)
    assert evs == [{"event": "pipeline.start"}]
    assert load_events(tmp_path / "missing.jsonl") == []  # never raises
