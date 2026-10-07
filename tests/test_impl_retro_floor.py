"""The per-impl retro floor measures the KEPT tree, not the reverted
candidate.

Run `358833-aa15d93c` (2026-09-01): the maximal last-resort on
`src/fmt.rs::ErrorImpl` compiled (0 compile errors) and was REVERTED for
reddening 5 passing Debug tests, yet `improved = cur_progress < prior_best`
came back true against the prior floor of 1 — `new_best` moved to a state
the tree does not hold, `full_exhaust` stayed false, the log printed "kept
the error floor improving" over a revert, and the next round opened at
`E0616` again (~5 model calls per re-entry, bounded only by
`PER_FN_RETRO_ROUNDS_MAX=25`).

Under the fix a regression-reverted attempt contributes its diagnosis (the
record lands, `regressed=` stamped) but moves no floor and buys no
re-entry. The negative control keeps the classic attempt-trajectory floor:
a compile-fail round (also reverted, but its metric IS the model's
diagnostic progress and its compiling images are salvageable) still
baselines and re-enters exactly as before."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pipeline
from pipeline_core.adapters.retros import retrospectives_path
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refine import BEST_COMPILE_KEY
from pipeline_core.domain.test_results import RegressionComparison, TestStateReading
from tests.harness import (
    diag,
    install_cluster_scan,
    install_fake_extractor,
    install_scripted_cargo,
)

FILLED_LIB = """pub struct Meter { v: u32 }

impl Meter {
    pub fn read(&self) -> u32 {
        self.v
    }
}
"""

_UNIT = FnLocator("lib.rs", "read")
REPLY = '<FN name="read" block="impl Meter">self.v + 1</FN>'


def _block() -> ImplBlock:
    m = FnRecord(file="lib.rs", fn_name="read", container="impl Meter",
                 sig_text="pub fn read (& self) -> u32")
    return ImplBlock(file="lib.rs", container="impl Meter",
                     members=(m,), refill_members=(m,))


def _setup(monkeypatch, tmp_path: Path) -> tuple[Path, list[str]]:
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
    monkeypatch.setattr(pipeline, "_ask_raw", lambda *a, **k: REPLY)
    logs: list[str] = []
    monkeypatch.setattr(pipeline, "_log", lambda msg: logs.append(str(msg)))
    return ws, logs


def _regressing_guard(monkeypatch) -> None:
    """A regression gate that fires on every green attempt: baseline is a
    sentinel and the comparison names one newly-red previously-passing
    test."""
    cmp = RegressionComparison(
        newly_red=frozenset({"t_green"}), newly_crashed=(),
        excluded_crashed=(), not_run=(), measured=("t_green", "t_red"),
        base_pass=1, cur_pass=0, base_executed=2, cur_executed=2)
    monkeypatch.setattr(pipeline, "_regression_guard", SimpleNamespace(
        baseline=lambda *a, **k: TestStateReading(binaries=()),
        gate=lambda *a, **k: (
            cmp, "REGRESSED previously-passing test(s): ['t_green']")))


def _run(ws: Path) -> pipeline.ClusterFillResult:
    return pipeline._phase_c_fill_impl_blocks(  # type: ignore[no-any-return]
        project_dir=ws, target_src_dir=ws / "src", tests_dir_files=[],
        module_name="m", cluster_name="t", cluster=None,
        check_cmd="cargo check", cluster_env={}, fr_path=None,
        tdd_frontier_keys={_UNIT},
        tdd_frontier_tests=FailureFrontier.build(
            {_UNIT: {"t_red"}}, {_UNIT: {"t_red"}}),
        tdd_test_cmd="cargo test",
    )


def _retro_record(ws: Path) -> dict[str, Any]:
    data = json.loads(retrospectives_path(ws).read_text(encoding="utf-8"))
    keys = [k for k in data if "*impl*" in k]
    assert keys, data
    return data[keys[0]]


def test_a_regression_reverted_attempt_moves_no_floor_and_buys_no_reentry(
        monkeypatch, tmp_path):
    ws, logs = _setup(monkeypatch, tmp_path)
    install_scripted_cargo(monkeypatch, check_diag=(0, "", []))
    _regressing_guard(monkeypatch)
    res = _run(ws)
    assert (ws / "src" / "lib.rs").read_text() == FILLED_LIB
    assert res.impls_filled == 0
    rec = _retro_record(ws)
    # The diagnosis lands — with the regressed stamp — but the floor does
    # not move to the discarded candidate's (0 compile errors) reading.
    assert rec.get("regressed") == ["t_green"]
    assert BEST_COMPILE_KEY not in rec, rec
    assert rec.get("fully_exhausted") is True
    # No round re-entered on the reverted attempt: the false line is gone.
    assert not any("kept the error floor improving" in ln for ln in logs)


def test_negative_control_a_compile_fail_round_still_baselines_and_reenters(
        monkeypatch, tmp_path):
    """The classic trajectory floor is untouched: a compile-failed attempt
    (distinct signatures, so the loop exhausts on floor stall) baselines
    `new_best` from its own metric and the retro re-enters the loop."""
    ws, logs = _setup(monkeypatch, tmp_path)
    codes = iter(["E0308", "E0425", "E0277", "E0599", "E0061", "E0433"])
    install_scripted_cargo(
        monkeypatch,
        check_diag=lambda: (101, "error: x",
                            [diag("src/lib.rs", code=next(codes, "E0999"))]))
    _run(ws)
    rec = _retro_record(ws)
    assert rec.get(BEST_COMPILE_KEY) == 1, rec
    assert any("kept the error floor improving" in ln
               and "on the kept tree" in ln for ln in logs)
