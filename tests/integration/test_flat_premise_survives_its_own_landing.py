"""Integration: a FLAT claim on a kept body stands until something other
than that body moves, read off the real extractor.

The class, run `143676-7dadc500` (2026-09-24, cluster `stable`): the flat
premise's `context` slot digests every body of the unit's file, the unit's
own body included, and the landed row was stamped with the digest taken at
DISPATCH — before the body landed. So the next dispatch always read the
premise as moved, `drop_standing_flat` never fired, the fill schedule ranked
every flat re-buy `served`, and one unit was graded flat 11 times on 8
distinct bodies under one oracle. The landing is now stamped over the tree
as the landing left it.

The crate is invented: `Meter` with two inherent fns in one file. The real
syn extractor enumerates it before and after a landing (no cargo, no
model); the premise is computed by `evidence_premise`, the row by
`body_ledger.rows_for_units`, the verdict by `flat_standing_unchanged` — the
three functions the driver chains. Negative controls: the dispatch-time stamp
(the claim never stands) and a later landing of the SIBLING (the claim is
released, as it must be: the unit's next prompt quotes a file it has not
seen).

Needs only files and the extractor release binary, so it runs in the default
suite; `scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerKey,
    flat_standing_unchanged,
    rows_for_units,
)
from pipeline_core.domain.evidence_premise import evidence_fingerprints
from pipeline_core.domain.models import FnLocator

pytestmark = [pytest.mark.integration]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")

_METER = """\
pub struct Meter {
    pub(crate) raw: u32,
}

impl Meter {
    pub fn level(&self) -> u32 {
        LEVEL
    }

    pub fn scale(&self) -> u32 {
        SCALE
    }
}
"""
_UNIT = FnLocator("src/meter.rs", "level")
_KEY = LedgerKey("p", "c", "src/meter.rs", "level")


def _write(root: Path, level: str, scale: str) -> None:
    (root / "src" / "meter.rs").write_text(
        _METER.replace("LEVEL", level).replace("SCALE", scale))


def _records(root: Path) -> list[dict[str, Any]]:
    recs = CliExtractor(_EXTRACTOR).enumerate_fns(root) or []
    return [r.to_dict() for r in recs]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    if not _EXTRACTOR.exists():
        pytest.skip("extractor release binary not built (bash pre_run.sh builds it)")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("mod meter;\npub use meter::Meter;\n")
    _write(tmp_path, "self.raw", "self.raw * 2")
    return tmp_path


def _flat_row(recs: list[dict[str, Any]], stamp: str):
    [row] = rows_for_units(
        [(_UNIT.file, _UNIT.fn_name)], recs, project="p", cluster="c",
        state=BodyState.FLAT, items={}, file_of={}, file_fp={}, run_id="r",
        iteration=2, epoch=0, oracle_fp="o",
        convicting_of={(_UNIT.file, _UNIT.fn_name): ["t_level"]},
        evidence_of={(_UNIT.file, _UNIT.fn_name): stamp})
    return row


def _stands(row, recs_now: list[dict[str, Any]]) -> bool:
    now = evidence_fingerprints([_UNIT], recs_now)[_UNIT]
    return _KEY in flat_standing_unchanged(
        {_KEY: row}, oracle_fp_now="o",
        convicting_now={_KEY: frozenset({"t_level"})},
        evidence_now={_KEY: now})


def test_premise_the_extractor_quotes_the_units_own_body(root: Path) -> None:
    """The fact the class rests on: the file context the premise digests
    carries the unit's own body, so a landing changes it."""
    before = evidence_fingerprints([_UNIT], _records(root))[_UNIT]
    _write(root, "self.raw + 1", "self.raw * 2")
    assert evidence_fingerprints([_UNIT], _records(root))[_UNIT] != before


def test_a_flat_claim_stamped_at_the_landing_stands(root: Path) -> None:
    _write(root, "self.raw + 1", "self.raw * 2")           # the landing
    at_landing = _records(root)
    row = _flat_row(at_landing,
                    evidence_fingerprints([_UNIT], at_landing)[_UNIT])
    assert _stands(row, _records(root)), (
        "nothing arrived after the landing: the same question, excluded")


def test_negative_control_the_dispatch_stamp_never_stands(root: Path) -> None:
    at_dispatch = evidence_fingerprints([_UNIT], _records(root))[_UNIT]
    _write(root, "self.raw + 1", "self.raw * 2")           # the landing
    row = _flat_row(_records(root), at_dispatch)
    assert not _stands(row, _records(root)), (
        "the pre-fix stamp reads the unit's own landing as new evidence")


def test_a_sibling_landing_after_it_releases_the_claim(root: Path) -> None:
    _write(root, "self.raw + 1", "self.raw * 2")           # the landing
    at_landing = _records(root)
    row = _flat_row(at_landing,
                    evidence_fingerprints([_UNIT], at_landing)[_UNIT])
    _write(root, "self.raw + 1", "self.raw << 1")          # a later sibling
    assert not _stands(row, _records(root)), (
        "the next prompt quotes a sibling body the flat attempt never saw")
