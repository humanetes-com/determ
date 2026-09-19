"""Integration: filling the first link of a stub chain moves every test's
ground to the next link, and the premise says so.

Real cargo, the pipeline's runner (`run_cargo` with isolation), the real syn
extractor's function locator, and `test_outcome.premise_of`. No model.

The class, run `3383938-c5d24499` (2026-09-19): a fill's plan is made from
the sweep's reading of where each failing test first fails. The first keep
of fill 1 filled `construct`, every failing test's first failure moved to
the next still-stubbed function on its path, and the rest of the fill kept a
plan in which that function had no run. The shape here is invented:
`Handle::make` is the first link, `release` (called from `Drop`) the second.

Asserted: every failing test's premise entry moves from `make` to
`release`, and a body growing above the `release` stub moves nothing (the
site is the function, not the line). Negative control: a premise taken with
no locator identifies crate sites by file only and cannot see the move.

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
from pipeline_core.domain.test_outcome import (
    outcomes_of,
    premise_moved,
    premise_of,
    truncated_failing,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")
_TARGET = Path(tempfile.gettempdir()) / "determ-integration-target" / "premise-chain"
_CMD = "cargo test --offline --no-fail-fast"

_LIB = """pub struct Handle {
    pub id: u32,
}

impl Handle {
    pub fn make(id: u32) -> Handle {
        MAKE
    }
}

PAD
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
fn builds_one() {
    let h = Handle::make(3);
    assert_eq!(h.id, 3);
}

#[test]
fn builds_two() {
    let h = Handle::make(4);
    assert_eq!(h.id, 4);
}
"""


def _crate(root: Path, make: str, pad: str = "") -> Path:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "handles"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "src" / "lib.rs").write_text(
        _LIB.replace("MAKE", make).replace("PAD", pad))
    (root / "tests" / "h.rs").write_text(_TESTS)
    return root


def _premise(root: Path, tag: str, *, locate: bool = True):
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET / tag)
    env["RUST_BACKTRACE"] = "0"
    _rc, out = run_cargo(CargoCli(), _CMD, root, env, timeout=600)
    outcomes = outcomes_of(out)
    loc = (make_fn_site_locator(CliStructureReader(CliExtractor(_EXTRACTOR)), root)
           if locate else None)
    return outcomes, premise_of(outcomes, loc)


def test_filling_the_first_link_moves_every_tests_ground(tmp_path: Path) -> None:
    assert _EXTRACTOR.exists(), "build the release extractor (bash pre_run.sh)"
    o_stub, p_stub = _premise(_crate(tmp_path / "s", "unimplemented!()"), "stub")
    o_fill, p_fill = _premise(_crate(tmp_path / "f", "Handle { id }"), "fill")
    assert truncated_failing(o_stub) == {"builds_one", "builds_two"}
    assert {v[1][2] for k, v in p_stub.items() if not v[0]} == {"make"}
    assert {v[1][2] for k, v in p_fill.items() if not v[0]} == {"release"}
    assert premise_moved(p_stub, p_fill) == ("builds_one", "builds_two")


def test_a_body_growing_above_the_stub_is_not_a_move(tmp_path: Path) -> None:
    _o1, p1 = _premise(_crate(tmp_path / "a", "Handle { id }"), "fill")
    pad = "fn filler() -> u32 {\n    1 + 1\n}\n\n" * 3
    _o2, p2 = _premise(_crate(tmp_path / "b", "Handle { id }", pad), "fill")
    assert premise_moved(p1, p2) == ()


def test_negative_control_no_locator_cannot_see_the_move(tmp_path: Path) -> None:
    _o1, p1 = _premise(_crate(tmp_path / "s", "unimplemented!()"), "stub",
                       locate=False)
    _o2, p2 = _premise(_crate(tmp_path / "f", "Handle { id }"), "fill",
                       locate=False)
    assert premise_moved(p1, p2) == ()
