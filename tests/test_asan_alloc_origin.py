"""A use-after-free whose free stack is the test's own: the origin is the
crate body the free happened under, else the allocation stack's crate frames.

Fixtures (both recorded sanitizer sweeps, sliced to their reports):

- `asan_uaf_alloc_origin_486447_fill6.txt`: run `486447-5207101c`'s fill-6
  sweep (`tests/test_context.rs`, `tests/test_downcast.rs`). Both free stacks
  hold only the test (`test_context.rs:124:19`, `test_downcast.rs:83:16`),
  the localizer blamed NOBODY, and the allocation stacks name
  `Error::construct_from_context` / `Error::construct_from_std`.
- `asan_uaf_shared_call_79929_fill5.txt`: run `79929-5a16b1d7`'s sweep under
  the fill-5 destructor write (`logs/asan/asan-20261023T165437178556.txt`).
  `test_downcast.rs` frees at `:17:37`, the access stack's caller of
  `Error::downcast::<&str>`, whose drop of the released value is the access;
  the allocation stack names `construct_from_std`. `test_convert.rs` frees by
  `drop(box_dyn)` at `:15:5` and is written at `:17` — two different calls.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.crash_site import (
    OriginSource,
    parse_asan_reports,
    relativize_frame_files,
    release_origin,
)
from pipeline_core.domain.oracle_immutability import is_oracle_path

_DIR = Path(__file__).parent / "fixtures" / "crash"
_FIX_486447 = _DIR / "asan_uaf_alloc_origin_486447_fill6.txt"
_FIX_79929 = _DIR / "asan_uaf_shared_call_79929_fill5.txt"
_ROOT = "/home/user/determ/tmp/anyhow/.skeleton-filled/stable"


def _crate_funcs(frames: list[dict[str, object]]) -> list[str]:
    return [str(f.get("func")) for f in frames
            if str(f.get("file") or "").startswith("src/")]


def _stacks(path: Path) -> dict[str, tuple[list, list, list]]:
    return {r.label: (relativize_frame_files(r.origin, _ROOT),
                      relativize_frame_files(list(r.alloc), _ROOT),
                      relativize_frame_files(list(r.access), _ROOT))
            for r in parse_asan_reports(path.read_text(encoding="utf-8"))}


def test_a_free_under_the_accessed_call_is_blamed_on_the_body_it_entered(
) -> None:
    """Every recorded test-only free whose call is on the access stack names
    the body that call entered, both runs."""
    for path, label, name in (
            (_FIX_79929, "tests/test_downcast.rs", "downcast::<&str>"),
            (_FIX_486447, "tests/test_downcast.rs",
             "downcast::<test_downcast::drop::DetectDrop>"),
            (_FIX_486447, "tests/test_context.rs",
             "downcast::<test_context::HighLevel>")):
        free, alloc, access = _stacks(path)[label]
        assert _crate_funcs(free) == []          # the runs' NOBODY
        frames, source = release_origin(free, alloc, access, is_oracle_path)
        assert source is OriginSource.SHARED_CALL, (path.name, label)
        assert len(frames) == 1 and name in str(frames[0]["func"])
        assert "construct_from" not in " ".join(_crate_funcs(frames))


def test_control_without_the_access_stack_the_constructor_is_blamed() -> None:
    """Negative control: the same reports read from the free and allocation
    stacks alone (the rule before 2026-10-23) blame the constructors."""
    want = {(_FIX_79929, "tests/test_downcast.rs"): ("construct_from_std",),
            (_FIX_486447, "tests/test_context.rs"):
                ("construct_from_context", "context", "ext_context"),
            (_FIX_486447, "tests/test_downcast.rs"):
                ("construct_from_std", "new")}
    for (path, label), names in want.items():
        free, alloc, _access = _stacks(path)[label]
        frames, source = release_origin(free, alloc, (), is_oracle_path)
        assert source is OriginSource.ALLOCATION
        funcs = " ".join(_crate_funcs(frames))
        for n in names:
            assert n in funcs


def test_a_free_in_another_call_names_nobody() -> None:
    """`test_convert`: freed at `:15`, written at `:17`, no crate frame on the
    free or allocation stack. Nothing names the body, so nothing is blamed."""
    free, alloc, access = _stacks(_FIX_79929)["tests/test_convert.rs"]
    frames, source = release_origin(free, alloc, access, is_oracle_path)
    assert source is OriginSource.FREE_STACK
    assert _crate_funcs(frames) == []


def test_a_free_stack_with_a_crate_frame_stays_the_origin() -> None:
    """A crate frame on the free stack is the origin; neither other stack is
    consulted."""
    free = [{"func": "c::object_drop", "file": "src/lib.rs", "line": 60,
             "col": 5},
            {"func": "t::test", "file": "tests/t.rs", "line": 3, "col": 9}]
    access = [{"func": "c::read", "file": "src/lib.rs", "line": 9, "col": 1},
              {"func": "t::test", "file": "tests/t.rs", "line": 3, "col": 9}]
    alloc = [{"func": "c::new", "file": "src/lib.rs", "line": 34, "col": 1}]
    frames, source = release_origin(free, alloc, access, is_oracle_path)
    assert frames == free and source is OriginSource.FREE_STACK
    # No crate frame anywhere: still the free stack, still unattributed.
    frames, source = release_origin(
        [free[1]], [{"func": "std::alloc", "file": "/rustc/x.rs"}], [],
        is_oracle_path)
    assert source is OriginSource.FREE_STACK


def test_the_call_must_match_to_the_column() -> None:
    """Two calls on one test line are two calls: a free under the first is
    not under the second. The allocation stack answers instead."""
    free = [{"func": "free", "file": "/rustc/asan.cpp", "line": 1, "col": 1},
            {"func": "t::test", "file": "tests/t.rs", "line": 7, "col": 13}]
    access = [{"func": "c::read", "file": "src/lib.rs", "line": 9, "col": 1},
              {"func": "c::take", "file": "src/lib.rs", "line": 20, "col": 5},
              {"func": "t::test", "file": "tests/t.rs", "line": 7, "col": 30}]
    alloc = [{"func": "c::new", "file": "src/lib.rs", "line": 34, "col": 1}]
    frames, source = release_origin(free, alloc, access, is_oracle_path)
    assert source is OriginSource.ALLOCATION
    assert _crate_funcs(frames) == ["c::new"]
    # Same column: the outermost crate frame inside the call is the origin.
    access[2]["col"] = 13
    frames, source = release_origin(free, alloc, access, is_oracle_path)
    assert source is OriginSource.SHARED_CALL
    assert _crate_funcs(frames) == ["c::take"]
