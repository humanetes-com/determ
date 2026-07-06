"""Hardcrate complexity selector (R10.7) — level-N strips traps above N from a crate
copy, leaving a clean crate (no complexity cfg ever reaches the pipeline). Pure file
ops, hermetic (no cargo)."""
from __future__ import annotations

import shutil
from pathlib import Path

from tools.hardcrate_select import TRAPS, _as_list, max_level, select

_FIXTURE = Path(__file__).parent / "fixtures" / "hardcrate"


def _stage(tmp_path: Path) -> Path:
    dst = tmp_path / "hc"
    shutil.copytree(_FIXTURE, dst, ignore=shutil.ignore_patterns("target", ".git"))
    return dst


def test_level0_strips_every_trap_to_baseline(tmp_path: Path) -> None:
    crate = _stage(tmp_path)
    removed = select(crate, 0)
    assert removed == sorted(TRAPS)                       # all traps gone at level 0
    lib = (crate / "src" / "lib.rs").read_text()
    for name, t in TRAPS.items():
        for rel in _as_list(t["src"]):
            assert not (crate / rel).exists(), name       # src deleted
        for rel in _as_list(t["test"]):
            assert not (crate / rel).exists(), name       # test deleted
        for anchor in t["lib"]:
            assert anchor not in lib                       # lib mod/use lines removed
    # baseline survives, and the cap_limit dual-form (the extra_cap cluster toggle) stays.
    assert (crate / "src" / "counter.rs").exists()
    assert (crate / "tests" / "baseline.rs").exists()
    assert "fn cap_limit()" in lib and "#[cfg(not(any(extra_cap" in lib


def test_level_at_max_keeps_all_traps(tmp_path: Path) -> None:
    crate = _stage(tmp_path)
    assert select(crate, max_level()) == []               # nothing stripped
    for t in TRAPS.values():
        for rel in _as_list(t["src"]):
            assert (crate / rel).exists()
        for rel in _as_list(t["test"]):
            assert (crate / rel).exists()


def test_level7_link_trap_is_multi_file(tmp_path: Path) -> None:
    # `link` (level 7) is the first multi-src-file trap — stripping level 6 removes
    # ONLY `link` (both halves + the shared test), keeping `guarded` (level 6) intact.
    crate = _stage(tmp_path)
    removed = select(crate, 6)
    assert removed == ["link"]
    assert not (crate / "src" / "link_a.rs").exists()
    assert not (crate / "src" / "link_b.rs").exists()
    assert not (crate / "tests" / "link_pair.rs").exists()
    assert (crate / "src" / "guarded.rs").exists()
    assert (crate / "tests" / "guarded.rs").exists()
    lib = (crate / "src" / "lib.rs").read_text()
    assert "mod link_a;" not in lib and "mod link_b;" not in lib
    assert "mod guarded;" in lib


def test_strip_removes_cfg_attr_above_anchor(tmp_path: Path) -> None:
    # `mod cap;` / `pub use cap::wrapping_total;` each carry a preceding #[cfg] line;
    # stripping cap must remove BOTH the anchor and its attr (and leave cap_limit's
    # own #[cfg] lines intact).
    crate = _stage(tmp_path)
    before = (crate / "src" / "lib.rs").read_text()
    assert before.count("#[cfg(any(extra_cap") >= 3      # cap mod + cap use + cap_limit
    select(crate, 0)
    after = (crate / "src" / "lib.rs").read_text()
    assert "mod cap;" not in after and "cap::wrapping_total" not in after
    # cap_limit's gates remain (it is baseline, not the cap trap).
    assert "fn cap_limit()" in after
    assert "#[cfg(any(extra_cap, feature = \"extra_cap\"))]" in after
