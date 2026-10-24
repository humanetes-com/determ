"""Integration: a test whose process aborts still owns what it entered
before its first panic, and an abort is not a hang.

Real cargo, the instrumented coverage reading (`measure_coverage_slice`,
llvm tools, gdb, the syn extractor). No model.

The class, run `497151-a6fa205b` (2026-09-22, cluster `stable`, iter 4):
once `construct` had a body, 92 failing tests aborted even when run alone.
Each one's first panic unwinds into a still-stubbed drop path, the stub
panics during cleanup, and the runtime aborts (rc -6). An aborted process
writes no profile, so 91 of 150 failing tests had no owner set, not even
the positive evidence a run truncated inside the crate keeps, and the
`[coverage]` line printed each of the 28 aborted binaries as "hung inside
… (killed after 120s)".

The crate is invented: `make` returns a guard whose `Drop` calls the stub
`release`; the failing test holds a guard and panics in `fails_here`.
Asserted: the test is unmeasured (its run did not reach its end, so
negative claims stay withheld) AND owns `make` and `fails_here`, read from
the debugger's flush at its first panic; `release`, which runs only in the
cleanup, is not owned; the binary is not reported hung. Premise: the
process really aborts (the reading lists the binary with a signal rc).
Negative control: with the cut off (`PHASE_C_COVERAGE_PREMISE=0`) the test
has no owner set.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.coverage_owner import ProbeWorld
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo + coverage + gdb; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = (Path(tempfile.gettempdir()) / "determ-integration-target"
           / "abort_owner")
_CLUSTER = "main"

_LIB = """pub struct Guard {
    pub id: u32,
}

impl Drop for Guard {
    fn drop(&mut self) {
        release(self.id);
    }
}

pub fn release(_id: u32) {
    unimplemented!()
}

pub fn make() -> Guard {
    Guard { id: 7 }
}

pub fn fails_here(g: &Guard) -> u32 {
    if g.id == 7 {
        panic!("wrong id");
    }
    g.id
}

pub fn untouched() -> u32 {
    3
}
"""

_TESTS = {
    "guard.rs": """#[test]
fn aborts_in_cleanup() {
    let g = guards::make();
    let v = guards::fails_here(&g);
    assert_eq!(v, 1);
}
""",
}

_FAILING = ("aborts_in_cleanup",)


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(
        '[package]\nname = "guards"\nversion = "0.1.0"\nedition = "2021"\n')
    (cluster / "src" / "lib.rs").write_text(_LIB)
    (project / "tests").mkdir()
    for name, text in _TESTS.items():
        (project / "tests" / name).write_text(text)
    (cluster / "tests").symlink_to(project / "tests")
    return project


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("RUSTFLAGS", None)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    env["RUST_BACKTRACE"] = "0"
    return env


def _reading(project: Path, monkeypatch: pytest.MonkeyPatch, premise: str):
    from pipeline_core.adapters.coverage_owner import measure_coverage_slice
    monkeypatch.setenv("PHASE_C_COVERAGE_PREMISE", premise)
    logs: list[str] = []
    reading = measure_coverage_slice(
        project, _CLUSTER, failing_tests=_FAILING, worlds=(ProbeWorld("", _env()),),
        enumerate_fns=CliStructureReader(CliExtractor(_EXTRACTOR)).enumerate_fns,
        timeout=300, workers=1, log=logs.append)
    assert reading is not None, f"the coverage reading could not be taken: {logs}"
    return reading, logs


def _tools_present() -> None:
    for tool in ("llvm-profdata", "llvm-cov", "gdb", "nm"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH")
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"


@pytest.fixture(scope="module")
def cut(tmp_path_factory: pytest.TempPathFactory):
    _tools_present()
    mp = pytest.MonkeyPatch()
    try:
        project = _workspace(tmp_path_factory.mktemp("abort_on"))
        yield _reading(project, mp, "1")
    finally:
        mp.undo()


def test_premise_the_process_aborts(cut) -> None:
    reading, logs = cut
    assert reading.unmeasured_binaries == frozenset({"guard"}), logs
    assert any("guard (rc=-6)" in ln for ln in logs), logs


def test_an_aborting_test_owns_what_ran_before_its_first_panic(cut) -> None:
    reading, logs = cut
    assert "aborts_in_cleanup" in reading.unmeasured_tests
    owned = {s.fn_name for s in reading.owners["aborts_in_cleanup"]}
    assert {"make", "fails_here"} <= owned, logs
    assert "release" not in owned
    assert "untouched" not in owned
    assert any("owned up to their first panic" in ln for ln in logs), logs


def test_the_ranking_map_keeps_the_flush_over_an_older_record(cut) -> None:
    """The map the ranking reads is the measured map plus the records that
    stand in for tests with none (`phase_c_tdd`, `records_standing_in`). A
    retrospective record of this test from an earlier tree must not replace
    the set measured now; a plain overwrite does (negative control)."""
    from pipeline_core.domain.coverage_slice import records_standing_in
    from pipeline_core.domain.models import FnLocator
    reading, _logs = cut
    old = {"aborts_in_cleanup": frozenset({FnLocator("src/lib.rs", "untouched")})}
    ranking = dict(reading.owners)
    ranking.update(records_standing_in(old, reading.owners, set(_FAILING)))
    assert {s.fn_name for s in ranking["aborts_in_cleanup"]} >= {
        "make", "fails_here"}
    blind = dict(reading.owners)
    blind.update(old)
    assert {getattr(s, "fn_name", None) for s in blind["aborts_in_cleanup"]} == {
        "untouched"}


def test_an_abort_is_not_a_hang(cut) -> None:
    reading, logs = cut
    assert not reading.hung_tests
    assert not any("hung inside" in ln for ln in logs), logs


def test_negative_control_without_the_cut_the_test_owns_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tools_present()
    project = _workspace(tmp_path)
    reading, _logs = _reading(project, monkeypatch, "0")
    assert "aborts_in_cleanup" in reading.unmeasured_tests
    assert "aborts_in_cleanup" not in reading.owners
