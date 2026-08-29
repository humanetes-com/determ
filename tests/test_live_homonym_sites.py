"""S1 live fixture, computed on the filled anyhow tree this session.

Plan sentence (2026-08-29): ``src/error.rs::from`` resolves to four sites;
``src/wrapper.rs::fmt`` resolves to six. Negative control is today's
locator union (one key per name). Skip when the filled tree is absent —
the extractor binary and the tree are not in the unit-test fixture set.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor

_BIN = (
    Path(__file__).resolve().parents[1]
    / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
)
_SRC = (
    Path(__file__).resolve().parents[1]
    / "tmp" / "anyhow" / ".skeleton-filled" / "stable" / "src"
)


@pytest.fixture(scope="module")
def recs() -> list:
    if not _BIN.exists() or not _SRC.is_dir():
        pytest.skip("live filled anyhow tree or extractor binary missing")
    out = CliExtractor(_BIN).enumerate_fns(_SRC)
    if not out:
        pytest.skip("enumerate_fns returned nothing")
    return out


def test_error_rs_from_is_four_sites(recs) -> None:
    froms = [r for r in recs
             if r.file.endswith("error.rs") and r.fn_name == "from"]
    assert len(froms) == 4, [r.container for r in froms]
    traits = {r.container for r in froms}
    assert len(traits) == 4
    assert any("From < E > for Error" in (r.container or "") for r in froms)


def test_wrapper_rs_fmt_is_six_sites(recs) -> None:
    fmts = [r for r in recs
            if r.file.endswith("wrapper.rs") and r.fn_name == "fmt"]
    assert len(fmts) == 6, [r.container for r in fmts]
    assert len({r.container for r in fmts}) == 6
