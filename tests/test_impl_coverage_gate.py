"""The per-impl route's coverage-unreached gate — the refusal the per-fn
route has had twice (`fill_one_fn`: the pre-loop skip and the retro arm),
ported to the route that lacked it.

Run `358833-aa15d93c` (2026-09-01): the `src/fmt.rs::ErrorImpl` block ground
four `E0616`/`E0599` rounds plus a maximal last-resort while the sweep's own
owner map proved neither convicting test ever enters `debug`. When every
refill member is positively unentered by every convicting test, no body
written in the block can move those verdicts — defer before the first model
call. `unreached_by` declines on an unbound reading, so ignorance changes
nothing (the negative controls)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pipeline
from pipeline_core.domain import coverage_slice as cov
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from tests.harness import (
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
)

FILLED_LIB = """pub struct Gauge { v: u32 }

impl Gauge {
    pub fn level(&self) -> u32 {
        self.v
    }
    pub fn peak(&self) -> u32 {
        self.v * 2
    }
}
"""

_LEVEL = FnLocator("lib.rs", "level")
_PEAK = FnLocator("lib.rs", "peak")
_ELSEWHERE = FnLocator("other.rs", "run")


def _block() -> ImplBlock:
    members = (
        FnRecord(file="lib.rs", fn_name="level", container="impl Gauge",
                 sig_text="pub fn level (& self) -> u32"),
        FnRecord(file="lib.rs", fn_name="peak", container="impl Gauge",
                 sig_text="pub fn peak (& self) -> u32"),
    )
    return ImplBlock(file="lib.rs", container="impl Gauge",
                     members=members, refill_members=members)


def _setup(monkeypatch, tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "lib.rs").write_text(FILLED_LIB)
    monkeypatch.setenv("PHASE_C_KEEP_NONREGRESSING", "0")
    monkeypatch.setenv("PHASE_C_ROUTE_ORDER", "default")
    monkeypatch.setenv("PHASE_C_FILE_FIRST", "0")
    install_cluster_scan(monkeypatch, impl_blocks=lambda *a, **k: [_block()])
    install_cluster_scan(monkeypatch, stub_records=lambda *a, **k: None)
    install_fake_extractor(
        monkeypatch, fns=lambda sd: pipeline._STRUCTURE.enumerate_fns(sd),
        types=[], dispatch_families=[])
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    return ws


def _install_model(monkeypatch, prompts: list[str]):
    def _ask(prompt: str, *a: Any, **k: Any) -> str:
        prompts.append(prompt)
        return ('<FN name="level" block="impl Gauge">self.v + 1</FN>\n'
                '<FN name="peak" block="impl Gauge">self.v * 3</FN>')

    monkeypatch.setattr(pipeline, "_ask_raw", _ask)


def _run(ws: Path) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_LEVEL, _PEAK},
        tdd_frontier_tests=FailureFrontier.build(
            {_LEVEL: {"t_red"}, _PEAK: {"t_red"}},
            {_LEVEL: {"t_red"}, _PEAK: {"t_red"}}),
    )


def test_a_block_no_convicting_test_enters_is_deferred_before_the_call(
        monkeypatch, tmp_path):
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, prompts)
    cov.bind(CoverageSlice(
        owners={"t_red": frozenset({_ELSEWHERE})},
        failing=frozenset({"t_red"})))
    try:
        res = _run(ws)
    finally:
        cov.bind(None)
    assert prompts == [], "no model call for a measured-unreachable block"
    assert res.impls_filled == 0
    assert (ws / "src" / "lib.rs").read_text() == FILLED_LIB


def test_negative_control_one_entered_member_keeps_the_block(
        monkeypatch, tmp_path):
    """One member the convicting test DOES enter: the block enters the fix
    loop exactly as today."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, prompts)
    cov.bind(CoverageSlice(
        owners={"t_red": frozenset({_ELSEWHERE, _PEAK})},
        failing=frozenset({"t_red"})))
    try:
        _run(ws)
    finally:
        cov.bind(None)
    assert prompts, "an entered member keeps the block dispatchable"


def test_negative_control_unbound_reading_changes_nothing(
        monkeypatch, tmp_path):
    """No reading bound (a fresh process, a crashed binary): the route
    behaves as it did before the reading existed."""
    ws = _setup(monkeypatch, tmp_path)
    prompts: list[str] = []
    _install_model(monkeypatch, prompts)
    cov.bind(None)
    _run(ws)
    assert prompts, "ignorance never refuses"
