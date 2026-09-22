"""Integration: a failing test owns what ran BEFORE its failure was reported,
not what reporting it ran.

Real cargo, the instrumented coverage reading (`measure_coverage_slice`,
llvm tools, gdb, the syn extractor). No model.

The class, run `450977-5f48b68a` (2026-09-21, cluster `stable`): the
downcast tests `.unwrap()` an `Err`, and `unwrap` formats the error's Debug
inside the panic machinery — which walks the error chain, indents causes and
reaches the pointer accessors. Those bodies run after the premise failed and
cannot change it, yet the owner map put them on the frontier and fills
re-bought them 10–31 times each. The cut is the panic handler's entry (a
hook is too late: std formats the payload before any hook) and, for a test
that returns `Err`, the harness's result check.

The crate is invented: `Wrapper`'s Debug walks a `Links` iterator. Four
tests: one `.unwrap()`s an `Err(Wrapper)`; one asserts on
`format!("{:?}")` directly (its renderer runs BEFORE the panic, so it keeps
it); one returns `Err(Wrapper)` (the harness renders it after the body
returned); one catches a panic, then calls `make`, then fails (the LAST
boundary is the cut, so `make` stays owned). Negative control: the same
reading with the cut off (`PHASE_C_COVERAGE_PREMISE=0`, the pre-fix owner
map) owns the iterator for the unwrap test.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

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
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "premise"
_CLUSTER = "main"

_LIB = """use std::fmt;

pub struct Wrapper {
    pub name: &'static str,
    pub inner: Option<Box<Wrapper>>,
}

pub struct Links<'a> {
    cur: Option<&'a Wrapper>,
}

impl<'a> Iterator for Links<'a> {
    type Item = &'a Wrapper;
    fn next(&mut self) -> Option<Self::Item> {
        let c = self.cur?;
        self.cur = c.inner.as_deref();
        Some(c)
    }
}

impl Wrapper {
    pub fn links(&self) -> Links<'_> {
        Links { cur: Some(self) }
    }
}

impl fmt::Debug for Wrapper {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for l in self.links() {
            write!(f, "{}/", l.name)?;
        }
        Ok(())
    }
}

pub fn make() -> Wrapper {
    Wrapper { name: "a", inner: Some(Box::new(Wrapper { name: "b", inner: None })) }
}

pub fn lookup(ok: bool) -> Result<u8, Wrapper> {
    if ok { Ok(1) } else { Err(make()) }
}
"""

_TESTS = {
    "unwrap.rs": """#[test]
fn unwraps_err() {
    let v = widgets::lookup(false).unwrap();
    assert_eq!(v, 1);
}
""",
    "render.rs": """#[test]
fn renders() {
    assert_eq!(format!("{:?}", widgets::make()), "wrong");
}
""",
    "returns.rs": """#[test]
fn returns_err() -> Result<(), widgets::Wrapper> {
    widgets::lookup(false)?;
    Ok(())
}
""",
    "caught.rs": """#[test]
fn caught_then_fails() {
    let _ = std::panic::catch_unwind(|| panic!("early"));
    let w = widgets::make();
    assert_eq!(w.name, "z");
}
""",
}

_FAILING = ("unwraps_err", "renders", "returns_err", "caught_then_fails")


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    cluster = project / ".skeleton-filled" / _CLUSTER
    (cluster / "src").mkdir(parents=True)
    (cluster / "Cargo.toml").write_text(
        '[package]\nname = "widgets"\nversion = "0.1.0"\nedition = "2021"\n')
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
        project, _CLUSTER, failing_tests=_FAILING, cluster_env=_env(),
        enumerate_fns=CliStructureReader(CliExtractor(_EXTRACTOR)).enumerate_fns,
        timeout=300, workers=1, log=logs.append)
    assert reading is not None, f"the coverage reading could not be taken: {logs}"
    return reading, logs


def _names(sites) -> set[str]:
    return {s.fn_name for s in sites}


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
        project = _workspace(tmp_path_factory.mktemp("premise_on"))
        yield _reading(project, mp, "1")
    finally:
        mp.undo()


def test_premise_panic_entry_formats_after_break(cut) -> None:
    """std formats the panic payload after the handler's entry: at the cut
    the Debug impl has not run, in the full profile it has. Fails loudly if
    the runtime ever formats before the handler."""
    reading, _logs = cut
    assert "fmt" in _names(reading.rendered.get("unwraps_err", ()))
    assert "fmt" not in _names(reading.owners["unwraps_err"])


def test_an_unwrap_failure_owns_its_premise_not_its_report(cut) -> None:
    reading, logs = cut
    owned = _names(reading.owners["unwraps_err"])
    assert {"lookup", "make"} <= owned
    assert not ({"next", "links", "fmt"} & owned)
    assert any("premise cut for 4/4" in ln for ln in logs), logs


def test_a_rendering_assertion_keeps_its_renderer(cut) -> None:
    reading, _logs = cut
    assert {"make", "fmt", "links", "next"} <= _names(reading.owners["renders"])


def test_a_returned_err_is_cut_at_the_harness_result_check(cut) -> None:
    reading, _logs = cut
    owned = _names(reading.owners["returns_err"])
    assert "lookup" in owned
    assert not ({"next", "fmt"} & owned)


def test_a_caught_panic_does_not_cut_early(cut) -> None:
    reading, _logs = cut
    assert "make" in _names(reading.owners["caught_then_fails"])


def test_negative_control_the_full_profile_owns_the_iterator(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tools_present()
    project = _workspace(tmp_path)
    reading, logs = _reading(project, monkeypatch, "0")
    assert {"next", "fmt"} <= _names(reading.owners["unwraps_err"])
    assert not reading.rendered
    assert any("premise cut OFF" in ln for ln in logs)
