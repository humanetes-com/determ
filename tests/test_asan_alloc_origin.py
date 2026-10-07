"""A use-after-free whose free stack is the test's own is blamed on the
allocation stack's crate frames.

Fixture: the two heap-use-after-free reports of run `486447-5207101c`'s fill-6
sanitized sweep (`logs/asan/asan-20261007T120652754001.txt`, the
`tests/test_context.rs` and `tests/test_downcast.rs` sections). Both free
stacks hold only the test (`test_context.rs:124`, `test_downcast.rs:83`)
inside libtest, and the localizer blamed NOBODY. The allocation stacks name
`Error::construct_from_context` / `Error::context` / `ext_context` and
`Error::construct_from_std` / `Error::new`.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.crash_site import (
    origin_or_allocation,
    parse_asan_reports,
    relativize_frame_files,
)
from pipeline_core.domain.oracle_immutability import is_oracle_path

_FIX = (Path(__file__).parent / "fixtures" / "crash"
        / "asan_uaf_alloc_origin_486447_fill6.txt")
_ROOT = "/home/user/determ/tmp/anyhow/.skeleton-filled/stable"


def _crate_funcs(frames: list[dict[str, object]]) -> list[str]:
    return [str(f.get("func")) for f in frames
            if str(f.get("file") or "").startswith("src/")]


def test_both_reports_are_blamed_on_their_constructors() -> None:
    reports = parse_asan_reports(_FIX.read_text(encoding="utf-8"))
    assert [r.label for r in reports] == ["tests/test_context.rs",
                                          "tests/test_downcast.rs"]
    want = (("construct_from_context", "context", "ext_context"),
            ("construct_from_std", "new"))
    for report, names in zip(reports, want, strict=True):
        free = relativize_frame_files(report.origin, _ROOT)
        assert _crate_funcs(free) == []          # the run's NOBODY
        frames, from_alloc = origin_or_allocation(
            free, relativize_frame_files(list(report.alloc), _ROOT),
            is_oracle_path)
        assert from_alloc
        funcs = " ".join(_crate_funcs(frames))
        for n in names:
            assert n in funcs


def test_a_free_stack_with_a_crate_frame_stays_the_origin() -> None:
    """Negative control: a crate frame on the free stack is the origin, the
    allocation stack is not consulted."""
    free = [{"func": "anyhow::error::object_drop", "file": "src/error.rs",
             "line": 600},
            {"func": "t::test", "file": "tests/t.rs", "line": 3}]
    alloc = [{"func": "anyhow::Error::new", "file": "src/error.rs", "line": 34}]
    frames, from_alloc = origin_or_allocation(free, alloc, is_oracle_path)
    assert frames == free and not from_alloc
    # And no crate frame anywhere: still the free stack, still unattributed.
    frames, from_alloc = origin_or_allocation(
        [free[1]], [{"func": "std::alloc", "file": "/rustc/x.rs"}],
        is_oracle_path)
    assert not from_alloc
