"""The recorder end to end: real phase scopes, a real emitter, the recorder as
its observer and the SQLite store — the path a live run takes. Plus the
identity probes and the cargo boundary."""
from __future__ import annotations

import subprocess
import sys

import pytest

from pipeline_core import log as log_mod
from pipeline_core import phases
from pipeline_core.adapters import run_stats_recorder as rsr
from pipeline_core.adapters.run_stats_store import SQLiteRunStatsStore, run_stats_path
from pipeline_core.adapters.telemetry import JsonlEventEmitter
from pipeline_core.domain.phase_cost import CodeIdentity, PhaseKey, RunConfig
from pipeline_core.threads import ContextPool


@pytest.fixture
def run(tmp_path):
    events = tmp_path / "logs" / "events.jsonl"
    events.parent.mkdir()
    emitter = JsonlEventEmitter(lambda: str(events), {"run_id": "r1"})
    store = SQLiteRunStatsStore(run_stats_path(events))
    probes: list[str] = []
    origins = iter(["tree-at-setup", "tree-at-t0c", "unused"])

    def probe() -> str:
        probes.append("p")
        return next(origins)

    rec = rsr.RunStatsRecorder(
        store, run_id="r1", crate="anyhow", identity=CodeIdentity("code1", "stamp", "head"),
        config=RunConfig.from_env({"MODEL": "m"}), origin_probe=probe,
        log_fn=lambda _m: None)
    rec.start()
    emitter.observe(rec.observe)
    orig = log_mod._log_sink, log_mod._emit_sink
    log_mod.set_sinks(lambda _m: None, emitter.emit)
    yield store, rec, probes
    log_mod.set_sinks(*orig)
    rsr.arm_cargo_counter(None)
    store.close()


def _call(_i: object = None) -> None:
    log_mod.emit("llm.request")
    log_mod.emit("llm.stream_meta", data={"prompt_tokens": 1000, "completion_tokens": 50})


def test_a_run_records_each_phase_with_its_work(run) -> None:
    store, rec, probes = run
    with phases.phase_scope("setup") as ph:
        ph.skip("checkpoint says done")
    with phases.phase_scope("t0c", cluster="stable") as ph:
        ph.step("t0c.augment", cluster="stable")
        _call()
        with ContextPool(max_workers=2) as pool:      # a pooled lane
            list(pool.map(_call, range(2)))
        log_mod.emit("cargo.spawn", data={"kinds": ["test"]})
    rec.finish()

    samples = {s.key.phase: s for s in store.samples(["r1"])}
    assert set(samples) == {"t0c", "t0c.augment"}       # skipped is not a sample
    for pid in ("t0c", "t0c.augment"):
        w = samples[pid].work
        assert (w.llm_calls, w.prompt_tokens, w.completion_tokens, w.test_runs) == (
            3, 3000, 150, 1)
    assert samples["t0c"].key == PhaseKey("t0c", "", "stable")
    # The origin is the tree at the first top-level phase that actually ran.
    assert len(probes) == 2
    stored = store.run("r1")
    assert stored is not None
    assert stored.record.origin == "tree-at-t0c"
    assert (stored.record.code_key, stored.tree_stamp, stored.commit) == (
        "code1", "stamp", "head")
    assert stored.outcome == "completed" and stored.ended is not None
    assert stored.work.llm_calls == 3
    assert stored.config["MODEL"] == "m"


def test_an_error_exit_marks_the_run(run) -> None:
    store, rec, _ = run
    with pytest.raises(RuntimeError), phases.phase_scope("t0a"):
        raise RuntimeError("boom")
    rec.finish()
    stored = store.run("r1")
    assert stored is not None and stored.outcome == "error"
    [s] = store.samples(["r1"])
    assert s.outcome == "error"


def test_a_signalled_run_stays_unfinished(run) -> None:
    store, _rec, _ = run
    with phases.phase_scope("t0a"):
        pass
    stored = store.run("r1")       # finish() never ran
    assert stored is not None and stored.ended is None and stored.outcome is None


def test_the_popen_audit_hook_charges_cargo_to_the_spawning_phase(run) -> None:
    store, rec, _ = run
    rsr.arm_cargo_counter(rsr.emit_cargo_spawn)
    with phases.phase_scope("t0d"):
        # Not real cargo: the classifier reads argv, so any process whose argv
        # names `cargo test` exercises the boundary without a toolchain.
        subprocess.run([sys.executable, "-c", "pass", "cargo", "test"], check=True)
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    rec.finish()
    [s] = store.samples(["r1"])
    assert (s.work.test_runs, s.work.compiles) == (1, 0)


def test_a_store_failure_stops_recording_without_raising(run) -> None:
    store, rec, _ = run
    store.close()                        # every later write raises
    with phases.phase_scope("t0a"):
        _call()
    rec.finish()                         # no exception reaches the pipeline


def test_workspace_tree_id_ignores_target_and_follows_content(tmp_path) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text("fn a() {}\n")
    (ws / ".gitignore").write_text("target/\n")
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    before = rsr.workspace_tree_id(ws)
    (ws / "target").mkdir()
    (ws / "target" / "junk.rs").write_text("x")
    assert rsr.workspace_tree_id(ws) == before
    objects = sorted(p.name for p in (ws / ".git" / "objects").rglob("*"))
    (ws / "src" / "lib.rs").write_text("fn a() { 1; }\n")
    assert rsr.workspace_tree_id(ws) != before
    # Read-only: nothing was hashed into the workspace's object store.
    assert sorted(p.name for p in (ws / ".git" / "objects").rglob("*")) == objects
    assert rsr.workspace_tree_id(tmp_path / "missing") == "absent"


def test_code_identity_moves_with_the_extractor_not_the_monitor(tmp_path) -> None:
    (tmp_path / "pipeline.py").write_text("x = 1\n")
    (tmp_path / "pipeline_core" / "monitor").mkdir(parents=True)
    (tmp_path / "pipeline_core" / "a.py").write_text("y = 1\n")
    ext = tmp_path / "tools" / "skeleton_extractor" / "src"
    ext.mkdir(parents=True)
    (ext / "lib.rs").write_text("fn x() {}\n")
    (tmp_path / ".pre_run_stamp").write_text("abcdef0123456789 extra\n")
    base = rsr.resolve_code_identity(tmp_path, commit="77f7ca7")
    assert (base.tree_stamp, base.commit) == ("abcdef012345", "77f7ca7")
    (tmp_path / "pipeline_core" / "monitor" / "v.py").write_text("z = 1\n")
    (ext.parent / "target").mkdir()
    (ext.parent / "target" / "gen.rs").write_text("junk")
    assert rsr.resolve_code_identity(tmp_path).key == base.key
    (ext / "lib.rs").write_text("fn x() { 1 }\n")
    assert rsr.resolve_code_identity(tmp_path).key != base.key
