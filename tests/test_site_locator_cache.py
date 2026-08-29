"""A fill-phase-long site locator reads a pre-splice snapshot.

``_make_record_locator`` memoizes records and file text on first use.
``focus_diagnostics`` then prefers a locator hit over the fresh
``container_line_window``. Negative control: the same closure after a
rewrite still names the old container. A newly built locator sees the
splice. Production (``fill_impl_blocks``) constructs one per attempt.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.reconstruct.attribution import (
    _make_record_locator,
    make_fn_site_locator,
)
from pipeline_core.domain.models import FnSite

_V1 = """\
impl B for X {
    fn f() {
        foo();
    }
}
"""

_V2 = """\
impl A for Y {
    fn f() {
        drop((x, y));
    }
}
impl B for X {
    fn f() {
        foo();
    }
}
"""


def _one_fn(text: str, file: str, container_prefix: str, marker: str) -> dict:
    c_at = text.index(container_prefix)
    brace = text.index("{", c_at)
    fn_at = text.index("fn f()", c_at)
    mark_at = text.index(marker, fn_at)
    end = text.index("\n    }", mark_at) + len("\n    }")
    return {
        "file": file,
        "fn_name": "f",
        "container": text[c_at:brace].strip(),
        "sig_byte_start": fn_at,
        "body_byte_end": end,
        "body_text": marker,
    }


def _enumerate(src_dir: Path) -> list[dict]:
    text = (src_dir / "lib.rs").read_text(encoding="utf-8")
    recs = []
    if "impl A for Y" in text:
        recs.append(_one_fn(text, "lib.rs", "impl A for Y", "drop((x, y));"))
    if "impl B for X" in text:
        recs.append(_one_fn(text, "lib.rs", "impl B for X", "foo();"))
    return recs


def test_cached_locator_overrides_fresh_file_text(tmp_path: Path) -> None:
    """Reproduction: line 3 is impl B, then the same line is impl A;
    the first closure still reports B."""
    src = tmp_path
    (src / "lib.rs").write_text(_V1, encoding="utf-8")
    structure = SimpleNamespace(enumerate_fns=_enumerate)
    stale = _make_record_locator(structure, src)
    hit = stale("lib.rs", 3)
    assert hit is not None
    assert "impl B for X" in hit[1]["container"]

    (src / "lib.rs").write_text(_V2, encoding="utf-8")
    still = stale("lib.rs", 3)
    assert still is not None
    assert "impl B for X" in still[1]["container"], (
        "cached text+recs must still name the pre-splice container")

    fresh = make_fn_site_locator(structure, src)
    site = fresh("lib.rs", 3)
    assert site == FnSite("lib.rs", "f", "impl A for Y")


def test_fill_impl_builds_locator_per_diagnostic() -> None:
    """Production contract: no fill-phase-long ``_locate_site``."""
    src = (Path(__file__).resolve().parents[1]
           / "pipeline_core" / "application" / "reconstruct" / "impl_fill.py")
    text = src.read_text(encoding="utf-8")
    assert "_locate_site =" not in text
    assert "locate=_site_locator()" in text
