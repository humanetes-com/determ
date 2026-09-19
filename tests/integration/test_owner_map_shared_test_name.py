"""Integration: a test name that fails in two binaries is two failing tests,
and the owner map holds what BOTH executed.

Real cargo, the pipeline's runner (`run_cargo`), the sweep's binary-qualified
verdicts (`parse_binary_results` -> `qualified_verdicts`), the instrumented
coverage reading (`measure_coverage_slice`, llvm tools, the syn extractor).
No model.

The class, run `3893442-cb547388` (2026-09-19): `test_downcast_ref` and
`test_downcast_mut` fail in two test binaries each. The owner map was keyed
by the name and answered it with the first binary that claimed it (or the
crash recovery's run of the later binary), so the red-slice session aimed at
the name got one binary's executed slice while its prompt quoted the other
binary's panic — whose asserted call was a body outside the editable set.
The shape here is invented: `alpha` is executed only by `tests/one.rs::t`,
`beta` only by `tests/two.rs::t`, both tests fail.

Asserted: with the sweep's failing binaries the owner set of `t` holds both
`alpha` and `beta`, and the log names the union. Negative control: the same
reading without them (the pre-fix call) holds exactly one of the two.
Premise: the sweep labels the binaries `tests/one.rs` / `tests/two.rs` and
cargo builds them as targets `one` / `two` — the mapping
`cargo_target_of` relies on.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.domain.measurement import (
    cargo_target_of,
    failing_binaries_by_name,
    qualified_verdicts,
)
from pipeline_core.domain.test_results import parse_binary_results

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + coverage; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "shared-name"
_CLUSTER = "main"
_CMD = "cargo test --offline --no-fail-fast"

_LIB = """pub fn alpha() -> u8 {
    1
}

pub fn beta() -> u8 {
    1
}
"""

_ONE = """#[test]
fn t() {
    assert_eq!(widgets::alpha(), 2);
}
"""

_TWO = """#[test]
fn t() {
    assert_eq!(widgets::beta(), 2);
}
"""


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(
        '[package]\nname = "widgets"\nversion = "0.1.0"\nedition = "2021"\n')
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (project / "tests").mkdir()
    (project / "tests" / "one.rs").write_text(_ONE)
    (project / "tests" / "two.rs").write_text(_TWO)
    (cluster / "tests").symlink_to(project / "tests")
    return project


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    env["RUST_BACKTRACE"] = "0"
    return env


def _sweep(project: Path) -> frozenset[tuple[str, str]]:
    cluster = project / ".skeleton-filled" / _CLUSTER
    _rc, out = run_cargo(CargoCli(), _CMD, cluster, _env(), timeout=600)
    _passed, failed, _incomplete = qualified_verdicts(parse_binary_results(out))
    return failed


def _owners(project: Path, failing_binaries):
    from pipeline_core.adapters.coverage_owner import measure_coverage_slice
    logs: list[str] = []
    reading = measure_coverage_slice(
        project, _CLUSTER, failing_tests={"t"}, cluster_env=_env(),
        enumerate_fns=CliStructureReader(CliExtractor(_EXTRACTOR)).enumerate_fns,
        timeout=300, workers=1, failing_binaries=failing_binaries,
        log=logs.append)
    assert reading is not None, f"the coverage reading could not be taken: {logs}"
    return {s.fn_name for s in reading.owners.get("t", frozenset())}, logs


def _tools_present() -> None:
    if shutil.which("llvm-profdata") is None or shutil.which("llvm-cov") is None:
        pytest.skip("llvm-profdata / llvm-cov not on PATH")
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"


def test_premise_the_sweep_labels_map_to_the_cargo_targets(tmp_path: Path) -> None:
    project = _workspace(tmp_path)
    failed = _sweep(project)
    assert failed == {("tests/one.rs", "t"), ("tests/two.rs", "t")}
    assert {cargo_target_of(b) for b, _ in failed} == {"one", "two"}


def test_a_name_failing_in_two_binaries_owns_what_both_executed(
        tmp_path: Path) -> None:
    _tools_present()
    project = _workspace(tmp_path)
    by_name = failing_binaries_by_name(_sweep(project))
    assert by_name == {"t": frozenset({"one", "two"})}
    owners, logs = _owners(project, by_name)
    assert {"alpha", "beta"} <= owners
    assert any("failing in several binaries measured on each" in ln for ln in logs)


def test_negative_control_the_name_alone_measures_one_binary(tmp_path: Path) -> None:
    _tools_present()
    project = _workspace(tmp_path)
    owners, _logs = _owners(project, None)
    assert len(owners & {"alpha", "beta"}) == 1
