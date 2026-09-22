"""Integration: a formatter the failing test runs only while its failure is
reported owns no verdict and forms no pair, read off the real extractor.

The class, run `92187-f71f12bf` (2026-09-22, cluster `stable`): five failing
tests named a crate type and a format trait in their source, so the
format-trait hop attributed them to that type's Display dispatcher and paired
the dispatcher with its cross-file implementer. The sweep's panic-cut
coverage showed none of the five entered either body before failing — the
renderer ran only while `unwrap` printed the error. The implementer screen
redirected to the dispatcher without screening it, and the pair was recorded
before any screen; the measured-owners withholding and the schedule's pair
wait then held 13 runs of the fill behind a pair no failing test runs.

The crate is invented: `Wrap` with an inherent `ok` check, `impl Display for
Wrap` in one file delegating to an inherent `display` on `Inner` in another,
and a test that fails on `ok()` before it formats. The real syn extractor
enumerates it (no cargo); the coverage reading is bound as the pipeline's
cut owner map would state it. Negative controls: the same test whose reading
enters the dispatcher (the pair forms), and an unbound reading (unchanged).

Needs only files and the extractor release binary, so it runs in the default
suite; `scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    behavior_frontier_from_texts,
)
from pipeline_core.domain import coverage_slice as cov
from pipeline_core.domain.attribution import FormatterPair
from pipeline_core.domain.coverage_slice import CoverageSlice
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.rust_fn_headers import locate_fn

pytestmark = [pytest.mark.integration]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")

_WRAP = """\
use core::fmt;
use crate::render::Inner;

pub struct Wrap(pub(crate) Inner);

impl Wrap {
    pub fn new() -> Self {
        Wrap(Inner { code: 0 })
    }

    pub fn ok(&self) -> bool {
        self.0.code == 1
    }
}

impl fmt::Display for Wrap {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        Inner::display(&self.0, f)
    }
}
"""

_RENDER = """\
use core::fmt;

pub(crate) struct Inner {
    pub(crate) code: u32,
}

impl Inner {
    pub(crate) fn display(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "code {}", self.code)
    }
}
"""

_TEST = """\
use shapes::Wrap;

#[test]
fn t_checks_then_renders() {
    let w = Wrap::new();
    assert!(w.ok());
    let _ = format!("{}", w);
}
"""


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _EXTRACTOR.exists():
        pytest.skip("extractor release binary not built (bash pre_run.sh builds it)")
    root = tmp_path_factory.mktemp("render_only_pair")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("mod render;\nmod wrap;\npub use wrap::Wrap;\n")
    (root / "src" / "wrap.rs").write_text(_WRAP)
    (root / "src" / "render.rs").write_text(_RENDER)
    return root


@pytest.fixture(autouse=True)
def _unbind() -> Iterator[None]:
    yield
    cov.bind(None)


class _Structure:
    def __init__(self, root: Path) -> None:
        self._root = root

    def enumerate_fns(self, _src: Path) -> list[dict[str, Any]]:
        recs = CliExtractor(_EXTRACTOR).enumerate_fns(self._root) or []
        return [r.to_dict() for r in recs]


def _hop(tree: Path, entered: list[str] | None) -> tuple[
        dict, set[FormatterPair], list[str]]:
    if entered is not None:
        cov.bind(CoverageSlice(
            owners={"t_checks_then_renders": frozenset(
                FnSite(*u.rsplit("::", 1), "") for u in entered)},
            failing=frozenset({"t_checks_then_renders"})))
    lines: list[str] = []
    deps = AttributionDeps(
        structure=_Structure(tree), classify=lambda **_k: {},
        locate_fn=locate_fn, run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c, is_gen_tests_file=lambda _f: False,
        log=lines.append)
    pairs: set[FormatterPair] = set()
    frontier, _s, _t = behavior_frontier_from_texts(
        deps, "test t_checks_then_renders ... FAILED\n",
        {"tests/t_shapes.rs": _TEST}, tree, pairs_out=pairs)
    return frontier, pairs, lines


def _owners(frontier: dict) -> set[str]:
    return {f"{k.file}::{k.fn_name}" for k, v in frontier.items()
            if "t_checks_then_renders" in v}


def test_premise_the_extractor_splits_dispatcher_and_implementer(tree: Path) -> None:
    """The hop keys on the dispatcher's `impl Display for Wrap` container and
    on an inherent `display` in another file; if the extractor stops
    reporting either the pair can never form and every test here is empty."""
    recs = _Structure(tree).enumerate_fns(tree)
    got = {(r["file"], r["fn_name"], r["container"]) for r in recs}
    assert any(f == "src/wrap.rs" and n == "fmt" and "Display" in c
               for f, n, c in got), got
    assert any(f == "src/render.rs" and n == "display" for f, n, c in got), got


def test_a_test_that_fails_before_rendering_gets_no_format_owner_or_pair(
        tree: Path) -> None:
    frontier, pairs, lines = _hop(tree, ["src/wrap.rs::new", "src/wrap.rs::ok"])
    assert pairs == set()
    assert not _owners(frontier) & {"src/wrap.rs::fmt", "src/render.rs::display"}
    assert any("no format owner and no pair" in ln for ln in lines)


def test_negative_control_the_reading_enters_the_dispatcher(tree: Path) -> None:
    frontier, pairs, _lines = _hop(
        tree, ["src/wrap.rs::new", "src/wrap.rs::ok", "src/wrap.rs::fmt"])
    assert any(p.cross_file for p in pairs), pairs
    assert "src/wrap.rs::fmt" in _owners(frontier)


def test_negative_control_unbound_reading_is_unchanged(tree: Path) -> None:
    frontier, pairs, _lines = _hop(tree, None)
    assert any(p.cross_file for p in pairs), pairs
    assert "src/render.rs::display" in _owners(frontier)
