"""Unit tests for the active coverage adapter — `LlvmCovProvider` (COVERAGE_BACKEND=llvm-cov).

The pipeline measures coverage with cargo-llvm-cov, whose only transcription-risk logic is
the pure lcov parser `_parse_lcov` (SF/DA/end_of_record → tarpaulin-compatible files_data)
plus the small selectors around it. These tests drive that logic from lcov *strings* — no
cargo, no subprocess — and stub `_run` for the measure/cache lookups. The unused
tarpaulin/grcov subprocess backends are intentionally out of scope.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline_core.adapters import coverage as cov
from pipeline_core.domain.enums import CoverageBackend

# --------------------------------------------------------------------------- fixtures

@dataclass
class _FakeCluster:
    is_default: bool
    tarpaulin_exclude_files: list[str]
    name: str = "fake"


def _deps(cluster: cov.ClusterView | None = None, *, ignore_pragma: str = "") -> cov.CoverageDeps:
    return cov.CoverageDeps(
        log=lambda _m: None,
        cargo_env=dict,
        cluster=lambda: cluster,
        tarpaulin_bin=Path("/nonexistent/tarp"),
        grcov_bin=Path("/nonexistent/grcov"),
        build_timeout=1,
        ignore_pragma=ignore_pragma,
    )


def _provider(deps: cov.CoverageDeps | None = None) -> cov.LlvmCovProvider:
    return cov.LlvmCovProvider(deps or _deps())


def _write_lcov(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- helpers

def test_glob_to_filename_regex_strips_glob_and_escapes() -> None:
    assert cov.glob_to_filename_regex("*/nightly.rs") == r"nightly\.rs"
    assert cov.glob_to_filename_regex("*/sub.dir.rs") == r"sub\.dir\.rs"
    assert cov.glob_to_filename_regex("plain.rs") == r"plain\.rs"


def test_cluster_exclude_patterns_default_vs_named() -> None:
    # No active cluster → the default-stable exclusion (nightly.rs out of denominator).
    assert cov.cluster_exclude_patterns(_deps()) == ["*/nightly.rs"]
    # Default cluster → same.
    default = _FakeCluster(is_default=True, tarpaulin_exclude_files=["ignored.rs"])
    assert cov.cluster_exclude_patterns(_deps(default)) == ["*/nightly.rs"]
    # Non-default cluster → its own list (a copy, not the cluster's mutable field).
    named = _FakeCluster(is_default=False, tarpaulin_exclude_files=["*/legacy.rs"])
    out = cov.cluster_exclude_patterns(_deps(named))
    assert out == ["*/legacy.rs"]
    assert out is not named.tarpaulin_exclude_files


def test_make_coverage_provider_selects_by_enum_identity() -> None:
    d = _deps()
    assert isinstance(cov.make_coverage_provider(CoverageBackend.LLVM_COV, d), cov.LlvmCovProvider)
    assert isinstance(cov.make_coverage_provider(CoverageBackend.GRCOV, d), cov.GrcovProvider)
    assert isinstance(cov.make_coverage_provider(CoverageBackend.TARPAULIN, d), cov.TarpaulinProvider)


# --------------------------------------------------------------------------- _parse_lcov

def test_parse_lcov_basic_counts_and_schema(tmp_path: Path) -> None:
    """One src file: covered = DA lines with hit>0, coverable = all DA lines; the
    tarpaulin-compatible record (abs_path/path/traces/covered/coverable) is built."""
    from pipeline_core.domain.coverage import CoverageMeasurement
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,5\nDA:2,0\nDA:3,2\nend_of_record\n")
    m = _provider()._parse_lcov(lcov, tmp_path)
    assert isinstance(m, CoverageMeasurement)          # named result, still a tuple
    assert (m.pct, m.covered, m.coverable) == (m[0], m[2], m[3])
    pct, files, covered, coverable = m                  # positional unpack unchanged
    assert (covered, coverable) == (2, 3)
    assert pct == pytest.approx(2 / 3 * 100.0)
    assert len(files) == 1
    rec = files[0]
    assert rec["abs_path"] == str(tmp_path / "src" / "f.rs")
    assert rec["path"] == rec["abs_path"].split(os.sep)
    assert rec["covered"] == 2 and rec["coverable"] == 3
    # traces sorted by line, carrying the hit count under stats.Line.
    assert rec["traces"] == [
        {"line": 1, "stats": {"Line": 5}},
        {"line": 2, "stats": {"Line": 0}},
        {"line": 3, "stats": {"Line": 2}},
    ]


def test_parse_lcov_multiple_files_aggregate(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov,
                "SF:src/a.rs\nDA:1,1\nDA:2,0\nend_of_record\n"
                "SF:src/b.rs\nDA:1,3\nend_of_record\n")
    pct, files, covered, coverable = _provider()._parse_lcov(lcov, tmp_path)
    assert {f["abs_path"].rsplit(os.sep, 1)[-1] for f in files} == {"a.rs", "b.rs"}
    assert (covered, coverable) == (2, 3)  # a: 1/2, b: 1/1


def test_parse_lcov_skips_non_src_files(tmp_path: Path) -> None:
    """Only files under src/ enter the denominator — tests/ and out-of-tree are dropped."""
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov,
                "SF:src/keep.rs\nDA:1,1\nend_of_record\n"
                "SF:tests/drop.rs\nDA:1,1\nend_of_record\n"
                "SF:examples/also_drop.rs\nDA:1,1\nend_of_record\n")
    _pct, files, covered, coverable = _provider()._parse_lcov(lcov, tmp_path)
    assert [f["abs_path"].rsplit(os.sep, 1)[-1] for f in files] == ["keep.rs"]
    assert (covered, coverable) == (1, 1)


def test_parse_lcov_absolute_sf_path_inside_src(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    abs_sf = str(tmp_path / "src" / "abs.rs")
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, f"SF:{abs_sf}\nDA:1,4\nDA:2,0\nend_of_record\n")
    _pct, files, covered, coverable = _provider()._parse_lcov(lcov, tmp_path)
    assert files[0]["abs_path"] == abs_sf
    assert (covered, coverable) == (1, 2)


def test_parse_lcov_duplicate_da_keeps_max_hit(tmp_path: Path) -> None:
    """A line measured twice (e.g. across test binaries) takes the max hit count."""
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,0\nDA:1,7\nend_of_record\n")
    _pct, files, covered, coverable = _provider()._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (1, 1)
    assert files[0]["traces"] == [{"line": 1, "stats": {"Line": 7}}]


def test_parse_lcov_ignores_malformed_da(tmp_path: Path) -> None:
    """Non-integer or short DA records are skipped, not fatal."""
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:bad,nope\nDA:5\nDA:2,3\nend_of_record\n")
    _pct, _files, covered, coverable = _provider()._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (1, 1)  # only DA:2,3 parsed


def test_parse_lcov_file_with_no_da_is_zero_coverable(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/empty.rs\nend_of_record\n")
    pct, files, covered, coverable = _provider()._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (0, 0)
    assert pct == 0.0  # no coverable lines anywhere → 0, not a ZeroDivisionError
    assert files[0]["coverable"] == 0


def test_parse_lcov_missing_file_returns_zeros(tmp_path: Path) -> None:
    pct, files, covered, coverable = _provider()._parse_lcov(tmp_path / "absent.lcov", tmp_path)
    assert (pct, files, covered, coverable) == (0.0, [], 0, 0)


def test_parse_lcov_pragma_excludes_marked_lines(tmp_path: Path) -> None:
    """When ignore_pragma is set, source lines carrying the marker are removed from
    the denominator (the coverage-ignore pragma pass)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f.rs").write_text("line1\nline2 // EXCL\nline3\n")
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,1\nDA:2,0\nDA:3,0\nend_of_record\n")
    prov = _provider(_deps(ignore_pragma="// EXCL"))
    _pct, _files, covered, coverable = prov._parse_lcov(lcov, tmp_path)
    # line 2 dropped → {1 (hit), 3 (miss)} → 1/2
    assert (covered, coverable) == (1, 2)


def test_parse_lcov_pragma_unreadable_source_is_tolerated(tmp_path: Path) -> None:
    """Pragma set but the source file is gone → no crash, all DA lines kept."""
    (tmp_path / "src").mkdir()  # no f.rs on disk
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,1\nDA:2,0\nend_of_record\n")
    prov = _provider(_deps(ignore_pragma="// EXCL"))
    _pct, _files, covered, coverable = prov._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (1, 2)


# --------------------------------------------------------------------------- measure_file / load_cached / measure

def test_measure_file_returns_matching_file_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """measure_file runs a quick measurement then looks up rel_path by abs_path."""
    prov = _provider()
    target_abs = str(tmp_path / "src" / "f.rs")
    canned = (50.0, [{"abs_path": target_abs, "covered": 3, "coverable": 6}], 3, 6)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: canned)
    assert prov.measure_file(tmp_path, "mod", "src/f.rs") == (3, 6)
    # Not in the file list → None.
    assert prov.measure_file(tmp_path, "mod", "src/other.rs") is None


def test_measure_delegates_to_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _provider()
    sentinel = (99.0, [], 9, 9)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: sentinel)
    assert prov.measure(tmp_path, "mod") == sentinel


def test_load_cached_reads_existing_lcov(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_cached parses the provider's on-disk lcov without re-running cargo."""
    prov = _provider()
    (tmp_path / "src").mkdir()
    module = f"covtest_{os.getpid()}"
    # Redirect the /tmp lcov path into tmp_path so the test is hermetic.
    lcov_path = tmp_path / "cached.lcov"
    monkeypatch.setattr(prov, "_lcov_path", lambda mod, suffix="": lcov_path)
    _write_lcov(lcov_path, "SF:src/f.rs\nDA:1,1\nDA:2,0\nend_of_record\n")
    result = prov.load_cached(tmp_path, module)
    assert result is not None
    _pct, _files, covered, coverable = result
    assert (covered, coverable) == (1, 2)


def test_load_cached_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _provider()
    monkeypatch.setattr(prov, "_lcov_path", lambda mod, suffix="": tmp_path / "nope.lcov")
    assert prov.load_cached(tmp_path, "mod") is None
