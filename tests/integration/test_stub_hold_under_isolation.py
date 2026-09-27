"""Integration: under test isolation, a still-stubbed fn that failing tests
stop in is still named as the fn to fill.

Real cargo, the pipeline's runner (`run_cargo` with isolation), the real syn
extractor's function locator and stub records, and `test_outcome.stub_hold_of`.
No model.

The class, run `3453127-045b05b1` (2026-09-19, cluster `stable`): isolating
aborted binaries one test per process gave every test a verdict and closed
every section, so the stub-abort reading — which read unfinished sections and
vanished tests — went silent. 82 of 141 failing tests stopped inside one stub
and the batch cut it for two iterations. The shape here is invented: `release`
is a stub reached from `Drop`; `a_drops` fails inside it on an ordinary drop,
`b_red_then_drop` fails its own assertion and aborts when the unwind reaches
the stub, `c_passes` never touches a handle.

Asserted: under isolation the binary completes, the hold names `release` for
`a_drops` only (`b_red_then_drop` first fails in its own source). Negative
control: the binary-level reading on the same isolated capture names
nothing. Premise: without isolation the shared process loses `c_passes`.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.attribution import make_fn_site_locator
from pipeline_core.application.reconstruct.cargo_runner import run_cargo
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_outcome import outcomes_of, stub_hold_of
from pipeline_core.domain.test_report import stub_abort_reading
from pipeline_core.domain.test_results import parse_binary_results

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "stub-hold"
_CMD = "cargo test --offline --no-fail-fast -- --test-threads=1"

_LIB = """pub struct Handle {
    pub id: u32,
}

impl Handle {
    pub fn new(id: u32) -> Handle {
        Handle { id }
    }
}

fn release(_id: u32) {
    unimplemented!()
}

impl Drop for Handle {
    fn drop(&mut self) {
        release(self.id)
    }
}
"""

_TESTS = """use handles::Handle;

#[test]
fn a_drops() {
    let h = Handle::new(1);
    assert_eq!(h.id, 1);
}

#[test]
fn b_red_then_drop() {
    let h = Handle::new(2);
    assert_eq!(h.id, 3);
}

#[test]
fn c_passes() {
    assert_eq!(1 + 1, 2);
}
"""


def _crate(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "handles"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "src" / "lib.rs").write_text(_LIB)
    (root / "tests" / "h.rs").write_text(_TESTS)
    return root


def _run(root: Path, isolation: str) -> str:
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    env["RUST_BACKTRACE"] = "0"
    prev = os.environ.get("PHASE_C_TEST_ISOLATION")
    os.environ["PHASE_C_TEST_ISOLATION"] = isolation
    try:
        _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    finally:
        if prev is None:
            os.environ.pop("PHASE_C_TEST_ISOLATION", None)
        else:
            os.environ["PHASE_C_TEST_ISOLATION"] = prev
    return out


def _stub_reader(root: Path):
    structure = CliStructureReader(CliExtractor(_EXTRACTOR))
    stubs = {FnSite(r["file"], r["fn_name"], r.get("container") or "")
             for r in structure.enumerate_fns(root) or []
             if r.get("has_unimplemented_body")}
    return make_fn_site_locator(structure, root), (lambda s: s in stubs)


def test_under_isolation_the_stub_holds_the_test_that_stops_in_it(
        tmp_path: Path) -> None:
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"
    root = _crate(tmp_path / "c")
    out = _run(root, "1")
    assert all(b.completed for b in parse_binary_results(out))
    locate, is_stub = _stub_reader(root)
    hold = stub_hold_of(out, locate, is_stub)
    release = FnSite("src/lib.rs", "release", "")
    assert hold.blockers == {release}
    assert hold.held_by(release) == {"a_drops"}
    assert outcomes_of(out)["c_passes"].passed


def test_negative_control_the_binary_reading_is_silent_under_isolation(
        tmp_path: Path) -> None:
    root = _crate(tmp_path / "c")
    out = _run(root, "1")
    locate, _ = _stub_reader(root)
    assert stub_abort_reading(out, locate).by_binary == {}


def test_premise_the_shared_process_loses_the_neighbour(tmp_path: Path) -> None:
    out = _run(_crate(tmp_path / "c"), "0")
    assert "c_passes" not in outcomes_of(out)


# ---- the schedule's hollow and bodies inputs (full_pre_run contracts) ----
#
# The loop hands the fill schedule `hollow` = the stub hold of the sweep's
# capture as locators (plus the session slices' stubs), and a `bodies_sha`
# that joins the extractor's records to each run's targets. Both are read
# from real tools; these tests derive them the way the loop does and assert
# the DECISION they feed: a run writing the stub a failing test stops in is
# tier 1, and a run's identity moves when its body does.

def test_the_stub_hold_puts_the_stub_writer_in_the_blockers_tier(
        tmp_path: Path) -> None:
    from pipeline_core.domain.attribution import locator_of
    from pipeline_core.domain.fill_schedule import RouteKind, RouteRun, schedule
    from pipeline_core.domain.models import FnLocator
    root = _crate(tmp_path / "c")
    out = _run(root, "1")
    locate, is_stub = _stub_reader(root)
    hold = stub_hold_of(out, locate, is_stub)
    # The loop's conversion (`phase_c_tdd`, `stub_hold_hollow=`).
    hollow = {t: frozenset(locator_of(s) for s in ss)
              for t, ss in hold.by_test.items()}
    assert hollow == {"a_drops": frozenset({FnLocator("src/lib.rs", "release")})}
    runs = [
        RouteRun(RouteKind.FILE, "other", frozenset({FnLocator("src/lib.rs", "new")}),
                 target_tests=frozenset({"a_drops"}), seq=0),
        RouteRun(RouteKind.PER_FN, "stub", frozenset({FnLocator("src/lib.rs", "release")}),
                 target_tests=frozenset({"a_drops"}),
                 stubs_in_targets=frozenset({FnLocator("src/lib.rs", "release")}),
                 seq=1),
    ]
    s = schedule(runs, [], oracle_fp="o", hollow=hollow)
    levels = {r.run.label: r.level for r in s.ordered}
    assert levels == {"stub": 1, "other": 2}
    assert s.ordered[0].run.label == "stub"


def test_a_runs_body_identity_joins_the_extractor_records(tmp_path: Path) -> None:
    from pipeline_core.domain.fill_schedule import bodies_sha_of, bodies_sha_on_tree
    from pipeline_core.domain.models import FnLocator, FnRecord
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"
    structure = CliStructureReader(CliExtractor(_EXTRACTOR))
    target = frozenset({FnLocator("src/lib.rs", "new")})   # the loop's spelling

    def _records(root: Path) -> list[FnRecord]:
        return [FnRecord.from_dict(r) for r in structure.enumerate_fns(root) or []]

    root = _crate(tmp_path / "a")
    before = bodies_sha_on_tree(target, _records(root))
    assert before and before != bodies_sha_of(()), (
        "the extractor's records did not join the loop's target — every run "
        "of these targets would share one identity")
    lib = root / "src" / "lib.rs"
    lib.write_text(lib.read_text().replace("Handle { id }", "Handle { id: id + 1 }"))
    assert bodies_sha_on_tree(target, _records(root)) != before
    # The same join from records rooted at `src/` (`lib.rs`): the pre-fix
    # join compared the raw and the normalized spelling against `src/lib.rs`
    # targets only, and joined nothing.
    rooted = _records(root / "src")
    assert rooted and all(not r.file.startswith("src/") for r in rooted)
    assert bodies_sha_on_tree(target, rooted) != bodies_sha_of(())
    pre_fix = [r for r in rooted
               if FnLocator(r.file, r.fn_name) in target]
    assert pre_fix == []
