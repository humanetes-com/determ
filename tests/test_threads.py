"""Pipeline threads continue the context that spawned them.

A worker on an empty context loses every ContextVar bound around the work,
the phase scope among them: a pooled lane's LLM calls were then charged to no
phase, and per-phase cost read zero on exactly the phases that run in
parallel. The behaviour tests drive the shared helpers and the pools built on
them through a real emitter; the AST guard fails on a pipeline module that
builds a bare thread or executor instead."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pipeline_core import log as log_mod
from pipeline_core import phases
from pipeline_core.adapters.coverage_owner import _map_concurrently
from pipeline_core.adapters.telemetry import JsonlEventEmitter
from pipeline_core.application.parallel import generate_in_parallel
from pipeline_core.application.reconstruct.unit_pool import run_units_concurrently
from pipeline_core.threads import ContextPool, context_thread

ROOT = Path(__file__).resolve().parent.parent

# Threads that deliberately run outside any phase: the flow-audit daemon lives
# for the whole run (stamping it with the phase current at spawn would charge
# a run's audit to `setup`), and the monitor's key reader is not the pipeline.
_ALLOWED = {
    "pipeline_core/threads.py",
    "pipeline_core/adapters/flow_audit.py",
    "pipeline_core/monitor/key_input.py",
}


@pytest.fixture
def emitted(tmp_path):
    emitter = JsonlEventEmitter(lambda: str(tmp_path / "events.jsonl"), {"run_id": "t"})
    seen: list[dict] = []
    emitter.observe(seen.append)
    orig = log_mod._log_sink, log_mod._emit_sink
    log_mod.set_sinks(lambda _m: None, emitter.emit)
    yield seen
    log_mod.set_sinks(*orig)


def _phase_of_calls(seen: list[dict]) -> list[str | None]:
    return [r.get("in_phase") for r in seen if r["event"] == "llm.request"]


def _call(*_a, **_k) -> str:
    log_mod.emit("llm.request")
    return "ok"


def test_context_thread_and_pool_carry_the_phase(emitted) -> None:
    with phases.phase_scope("t0c"):
        t = context_thread(_call, name="w")
        t.start()
        t.join()
        with ContextPool(max_workers=2) as pool:
            list(pool.map(_call, range(3)))
    assert _phase_of_calls(emitted) == ["t0c"] * 4


def test_the_pipeline_pools_carry_the_phase(emitted) -> None:
    with phases.phase_scope("t0c"):
        generate_in_parallel(["a", "b"], ask=_call, guard=lambda *_: None)
        run_units_concurrently([1, 2, 3], workers=2, name_prefix="u", run_unit=_call,
                               describe=str, log=lambda _m: None)
        _map_concurrently(_call, [1, 2, 3], workers=2)
    assert _phase_of_calls(emitted) == ["t0c"] * 8


def test_a_plain_thread_is_the_negative_control(emitted) -> None:
    import threading
    with phases.phase_scope("t0c"):
        t = threading.Thread(target=_call)
        t.start()
        t.join()
    assert _phase_of_calls(emitted) == [None]


def _bare_spawns(tree: ast.AST) -> list[int]:
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        owner = f.value.id if (isinstance(f, ast.Attribute)
                               and isinstance(f.value, ast.Name)) else ""
        if name == "ThreadPoolExecutor" or (name == "Thread" and owner in (
                "threading", "_threading", "")):
            hits.append(node.lineno)
    return hits


def test_no_pipeline_module_builds_a_context_free_thread() -> None:
    bad = []
    for p in sorted((ROOT / "pipeline_core").rglob("*.py")):
        rel = str(p.relative_to(ROOT))
        if rel in _ALLOWED or "__pycache__" in p.parts:
            continue
        for line in _bare_spawns(ast.parse(p.read_text(encoding="utf-8"))):
            bad.append(f"{rel}:{line}")
    assert bad == [], ("use pipeline_core.threads.context_thread / ContextPool: "
                       + ", ".join(bad))


def test_the_guard_sees_the_shapes_it_forbids() -> None:
    src = ("import threading as _threading\nimport threading\n"
           "from concurrent.futures import ThreadPoolExecutor\n"
           "threading.Thread(target=f)\n_threading.Thread(target=f)\n"
           "ThreadPoolExecutor(2)\n")
    assert _bare_spawns(ast.parse(src)) == [4, 5, 6]


def test_events_written_to_disk_carry_the_stamp(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    emitter = JsonlEventEmitter(lambda: str(path), {"run_id": "t"})
    orig = log_mod._log_sink, log_mod._emit_sink
    log_mod.set_sinks(lambda _m: None, emitter.emit)
    try:
        with phases.phase_scope("t0a") as ph:
            ph.step("t0a.profile")
            log_mod.emit("llm.request")
    finally:
        log_mod.set_sinks(*orig)
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    call = next(r for r in rows if r["event"] == "llm.request")
    enter = next(r for r in rows if r["event"] == "phase.enter"
                 and r["phase"] == "t0a.profile")
    assert (call["in_phase"], call["in_scope"]) == ("t0a.profile", enter["scope"])
    assert "in_scope" not in enter
