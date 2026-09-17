"""Integration: augmentation's stop must see a function no test executes.

Real `cargo llvm-cov`, the pipeline's `LlvmCovProvider` (its command line and
its lcov reader), the real syn extractor and the criterion's reader
(`fn_criterion.read_function_execution`). No model.

The class: test augmentation stopped on a crate-wide LINE percentage, and a
function no test enters was indistinguishable from one missed branch. On
anyhow's `nightly` cluster (task0c, run `330242-0be2c3e1`, 2026-09-03) the stop
fired at 95.2 % with `src/wrapper.rs`'s `provide` executed by no test; that body
then stayed `unimplemented!()` through every fill gate, and run
`301146-2d8a5f06` (2026-09-17) passed every test and exited 1 on it.

The crate is invented and generated per run: one line-heavy function the test
calls, so line coverage clears 90 %; a shared function no test calls; a
function gated by the cluster's `--cfg` that no test calls; and an
uninstantiated generic. Asserted: all three uncalled functions read UNEXECUTED
(the gated one cluster-gated) while the line target is met, and the record
built from that reading holds the stop open. Negative control: the pre-fix stop
(`coverage >= target`) fires on the same measurement.

`test_premise_llvm_cov_emits_zero_count_function_records` fails loudly if the
lcov stops carrying `FN`/`FNDA:0` for a never-called fn or an uninstantiated
generic: the criterion reads an absent record as UNMEASURED, so if rustc stopped
emitting them every such function would silently leave the target set.

Gated on `INTEGRATION=1` and on `cargo-llvm-cov` being installed;
`scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.cargo import base_cargo_env
from pipeline_core.adapters.coverage import CoverageDeps, LlvmCovProvider
from pipeline_core.adapters.coverage_owner import _body_counts
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.tasks.fn_criterion import (
    FnCriterionInputs,
    fold_reading,
    read_function_execution,
)
from pipeline_core.domain.coverage import CoverageMeasurement
from pipeline_core.domain.fn_execution import FunctionExecution
from pipeline_core.domain.models import BuildCluster

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo-llvm-cov; set INTEGRATION=1"),
    pytest.mark.skipif(shutil.which("cargo-llvm-cov") is None,
                       reason="cargo-llvm-cov is not installed"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "fn-execution"
_MODULE = "fnexec_widgets"
_CFG = "gizmo"

_CARGO_TOML = f"""[package]
name = "{_MODULE}"
version = "0.1.0"
edition = "2021"

[lints.rust]
unexpected_cfgs = {{ level = "allow" }}
"""

_BUSY = "\n".join(f"    total += {i};" for i in range(1, 81))
_LIB = f"""pub fn busy() -> u32 {{
    let mut total = 0;
{_BUSY}
    total
}}

pub fn lonely_shared(x: u32) -> u32 {{
    x + 1
}}

#[cfg({_CFG})]
pub fn lonely_gated(x: u32) -> u32 {{
    x + 2
}}

pub fn generic_never<T: Clone>(t: &T) -> T {{
    t.clone()
}}
"""

_TEST = f"""#[test]
fn busy_adds_up() {{
    assert_eq!({_MODULE}::busy(), 3240);
}}
"""

_CLUSTER = BuildCluster(
    name=_CFG, env_extra={"RUSTFLAGS": f"--cfg {_CFG}"}, tarpaulin_exclude_files=[],
    excluded_cfg=[], active_cfg=[_CFG], prompt_nightly_rule="", is_default=False,
    gated_files=set())


def _crate(root: Path) -> Path:
    crate = root / _MODULE
    (crate / "src").mkdir(parents=True)
    (crate / "tests").mkdir()
    (crate / "Cargo.toml").write_text(_CARGO_TOML, encoding="utf-8")
    (crate / "src" / "lib.rs").write_text(_LIB, encoding="utf-8")
    (crate / "tests" / "busy.rs").write_text(_TEST, encoding="utf-8")
    return crate


def _env() -> dict[str, str]:
    env = base_cargo_env()
    env["RUSTFLAGS"] = env.get("RUSTFLAGS", "") + f" --cfg {_CFG}"
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    return env


def _measure(crate: Path) -> CoverageMeasurement:
    logs: list[str] = []
    provider = LlvmCovProvider(CoverageDeps(
        log=logs.append, cargo_env=_env, cluster=lambda: _CLUSTER,
        tarpaulin_bin=Path("/nonexistent"), grcov_bin=Path("/nonexistent"),
        build_timeout=600, ignore_pragma=""))
    m = provider.measure(crate, _MODULE)
    assert m.coverable > 0, "llvm-cov produced no measurement: " + "\n".join(logs)
    return m


def _inputs() -> FnCriterionInputs:
    if not _EXTRACTOR.exists():
        pytest.skip("skeleton extractor release binary missing (bash pre_run.sh builds it)")
    reader = CliStructureReader(CliExtractor(_EXTRACTOR))
    return FnCriterionInputs(enumerate_fns=reader.enumerate_fns,
                             enumerate_mods=reader.enumerate_mods)


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, CoverageMeasurement]:
    crate = _crate(tmp_path_factory.mktemp("fnexec"))
    return crate, _measure(crate)


def test_the_uncalled_functions_read_unexecuted_while_the_line_target_is_met(
        measured: tuple[Path, CoverageMeasurement]) -> None:
    crate, m = measured
    assert m.pct >= 90.0, f"fixture must clear the line target, got {m.pct:.1f}%"
    reading = read_function_execution(crate, _CLUSTER, m.functions, _inputs())
    assert isinstance(reading, FunctionExecution), reading
    unexecuted = {t.site.fn_name: t.cluster_gated for t in reading.unexecuted}
    assert unexecuted == {"lonely_shared": False, "lonely_gated": True,
                          "generic_never": False}
    assert {s.fn_name for s in reading.executed} == {"busy"}
    assert reading.unmeasured == ()

    record = fold_reading(None, reading, attempt_budget=3)
    assert record.measured and not record.accounted
    # Negative control: the pre-fix stop read the line percentage alone and
    # would have ended augmentation on this very measurement.
    target = 90.0
    assert m.pct >= target


def test_the_lcov_reader_and_the_json_export_reader_agree_on_one_measurement(
        measured: tuple[Path, CoverageMeasurement]) -> None:
    crate, m = measured
    out = crate.parent / "export.json"
    r = subprocess.run(
        ["cargo", "llvm-cov", "report", "--json", "--output-path", str(out),
         "-p", _MODULE, "--ignore-filename-regex", r"tests[/\\]"],
        cwd=crate, env=_env(), capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    export: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    assert _body_counts(export, crate) == m.functions


def test_premise_llvm_cov_emits_zero_count_function_records(
        measured: tuple[Path, CoverageMeasurement]) -> None:
    crate, _m = measured
    lcov = Path("/tmp") / f"llvmcov_{_MODULE}.lcov"
    text = lcov.read_text(encoding="utf-8")
    fn_line: dict[str, int] = {}
    hits: dict[str, int] = {}
    for raw in text.splitlines():
        if raw.startswith("FN:"):
            ln, _, name = raw[3:].partition(",")
            fn_line[name] = int(ln)
        elif raw.startswith("FNDA:"):
            ct, _, name = raw[5:].partition(",")
            hits[name] = max(hits.get(name, 0), int(ct))
    for wanted in ("lonely_shared", "generic_never", "lonely_gated"):
        names = [n for n in fn_line if wanted in n]
        assert names, (
            f"PREMISE BROKEN: cargo llvm-cov's lcov carries no FN record for the "
            f"never-called `{wanted}`. The function-execution criterion reads an "
            "absent record as UNMEASURED, so every such function now leaves the "
            "target set silently — re-derive the join before trusting the stop.")
        assert all(hits.get(n) == 0 for n in names), (
            f"PREMISE BROKEN: `{wanted}` is never called but its FNDA count is not 0")
