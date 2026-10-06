"""The pure half of per-phase cost: what a spawned process was, what one event
adds, and the fold from the event stream to closed-phase samples."""
from __future__ import annotations

import pytest

from pipeline_core.domain.phase_cost import (
    CARGO_SPAWN,
    CargoKind,
    PhaseCostFold,
    PhaseKey,
    PhaseWork,
    RunConfig,
    classify_cargo,
    work_delta,
)

C, T, OTH = CargoKind.COMPILE, CargoKind.TEST, CargoKind.OTHER


@pytest.mark.parametrize(("argv", "kinds"), [
    (["cargo", "build"], [C]),
    (["/home/u/.cargo/bin/cargo", "test", "--lib"], [T]),
    (["cargo", "+nightly", "test"], [T]),
    (["cargo", "-Z", "unstable-options", "check"], [C]),
    (["cargo", "test", "--no-run"], [C]),
    (["cargo", "llvm-cov", "--json"], [T]),
    (["cargo", "llvm-cov", "report", "--json"], [OTH]),
    (["cargo", "metadata", "--format-version", "1"], [OTH]),
    (["timeout", "600", "cargo", "nextest", "run"], [T]),
    (["rustc", "--edition", "2021", "x.rs"], [C]),
    (["sh", "-c", "cd w && cargo build && cargo test -q"], [C, T]),
    (["/bin/sh", "-c", "RUSTFLAGS=x cargo tarpaulin --out Json"], [T]),
    (["git", "status"], []),
    (["cargo"], [OTH]),
    ([], []),
])
def test_classify_cargo(argv, kinds) -> None:
    assert classify_cargo(argv) == kinds


def test_work_delta_reads_the_four_work_events() -> None:
    assert work_delta({"event": "llm.request"}) == PhaseWork(llm_calls=1)
    assert work_delta({"event": "llm.response", "duration_ms": 1500,
                       "data": {"response": {"char_count": 42}}}) == PhaseWork(
        llm_ms=1500, response_chars=42)
    assert work_delta({"event": "llm.stream_meta",
                       "data": {"prompt_tokens": 900, "completion_tokens": 100}}
                      ) == PhaseWork(prompt_tokens=900, completion_tokens=100)
    assert work_delta({"event": "llm.followup_meta",
                       "data": {"prompt_tokens": None}}) == PhaseWork()
    assert work_delta({"event": CARGO_SPAWN, "data": {"kinds": ["compile", "test", "test"]}}
                      ) == PhaseWork(compiles=1, test_runs=2)
    assert work_delta({"event": "phase_c.verdict"}) is None


def _enter(sid, phase, parent=None, **kw):
    return {"event": "phase.enter", "scope": sid, "parent_scope": parent,
            "phase": phase, "ts": "2026-10-04T10:00:00+00:00", **kw}


def _exit(sid, phase, ms=1000, outcome="ok"):
    return {"event": "phase.exit", "scope": sid, "phase": phase,
            "duration_ms": ms, "outcome": outcome}


def test_fold_charges_a_step_and_its_phase_inclusively() -> None:
    fold = PhaseCostFold()
    assert fold.feed(_enter("1", "t0c", cluster="stable")) is None
    fold.feed(_enter("2", "t0c.augment", parent="1", cluster="stable"))
    fold.feed({"event": "llm.request", "in_scope": "2"})
    fold.feed({"event": CARGO_SPAWN, "in_scope": "2", "data": {"kinds": ["test"]}})
    fold.feed({"event": "llm.request", "in_scope": "1"})
    step = fold.feed(_exit("2", "t0c.augment", 700))
    phase = fold.feed(_exit("1", "t0c", 900))
    assert step is not None and phase is not None
    assert step.key == PhaseKey("t0c.augment", "", "stable")
    assert step.parent_scope == "1" and not step.top_level
    assert (step.work.llm_calls, step.work.test_runs, step.wall_ms) == (1, 1, 700)
    assert phase.top_level
    assert (phase.work.llm_calls, phase.work.test_runs) == (2, 1)
    assert fold.run_work.llm_calls == 2 and fold.open_scopes() == 0


def test_unscoped_work_counts_for_the_run_only() -> None:
    """A context-free daemon thread's call (no `in_scope`) is run work, never
    some phase's."""
    fold = PhaseCostFold()
    fold.feed(_enter("1", "t0a"))
    fold.feed({"event": "llm.request"})
    sample = fold.feed(_exit("1", "t0a"))
    assert sample is not None and sample.work.llm_calls == 0
    assert fold.run_work.llm_calls == 1


def test_an_exit_without_its_enter_and_a_scopeless_exit_are_not_samples() -> None:
    fold = PhaseCostFold()
    assert fold.feed(_exit("9", "t0a")) is None
    # `phase_skipped` announces an exit with no scope at all.
    assert fold.feed({"event": "phase.exit", "phase": "t0b", "outcome": "skipped",
                      "duration_ms": 0}) is None


def test_a_skipped_scope_is_still_a_sample_with_its_outcome() -> None:
    fold = PhaseCostFold()
    fold.feed(_enter("1", "setup"))
    s = fold.feed(_exit("1", "setup", 3, outcome="skipped"))
    assert s is not None and s.outcome == "skipped"


def test_run_config_fingerprint_ignores_where_the_route_order_came_from() -> None:
    """An env var naming the model default runs identically; the fingerprint
    is of the resolved values only, `as_dict` keeps the provenance."""
    model = "RedHatAI/Muse-Glimmer-30B-FP8-block"
    implicit = RunConfig.from_env({"MODEL": model})
    route = dict(implicit.values)["PHASE_C_ROUTE_ORDER"]
    explicit = RunConfig.from_env({"MODEL": model, "PHASE_C_ROUTE_ORDER": route})
    assert implicit.fingerprint == explicit.fingerprint
    assert implicit.as_dict()["route_order_source"] == "model-default"
    assert explicit.as_dict()["route_order_source"] == "env"
    assert RunConfig.from_env({"MODEL": model, "PIPELINE_PARALLELISM": "4"}
                              ).fingerprint != implicit.fingerprint


def test_run_config_keeps_the_ledger_env_block_shape() -> None:
    """`tools/perf_ledger.py` rows written before the resolver moved carry
    these keys; `compare` diffs them by name."""
    keys = set(RunConfig.from_env({}).as_dict())
    assert keys == {"MODEL", "PHASE_C_ROUTE_ORDER", "route_order_source",
                    "PHASE_C_DRIVER", "VLLM_REASONING_STRENGTH", "VLLM_MAX_MODEL_LEN",
                    "VLLM_MAX_THINK_CHARS", "VLLM_REASONING_BUDGET",
                    "VLLM_THINKING_BUDGET", "PIPELINE_PARALLELISM",
                    # beside the hash, like route_order_source
                    "model_family", "knob_sources"}
    # The model knobs resolve through the transport's resolver, which refuses
    # a value the transport could not run under instead of recording None.
    with pytest.raises(ValueError, match="VLLM_MAX_MODEL_LEN"):
        RunConfig.from_env({"VLLM_MAX_MODEL_LEN": "garbage"})
