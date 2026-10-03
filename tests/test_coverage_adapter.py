"""Unit tests for the active coverage adapter — `LlvmCovProvider` (COVERAGE_BACKEND=llvm-cov).

The pipeline measures coverage with cargo-llvm-cov, whose only transcription-risk logic is
the pure lcov parser `_parse_lcov` (SF/DA/end_of_record → tarpaulin-compatible files_data)
plus the small selectors around it. These tests drive that logic from lcov *strings* — no
cargo, no subprocess — and stub `_run` for the measure/cache lookups. The grcov
backend is not the configured one but must keep working when it is selected:
its readings are pinned with a stubbed subprocess.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline_core.adapters import coverage as cov
from pipeline_core.domain.coverage import CoverageMeasurement, PerFileCoverage
from pipeline_core.domain.enums import CoverageBackend


def test_per_file_coverage_from_dict_tolerant() -> None:
    # full tarpaulin-shaped dict → verbatim fields.
    d = {"abs_path": "/p/src/f.rs", "path": ["", "p", "src", "f.rs"],
         "traces": [{"line": 1, "stats": {"Line": 3}}], "covered": 1, "coverable": 2}
    pf = PerFileCoverage.from_dict(d)
    assert (pf.abs_path, pf.covered, pf.coverable) == ("/p/src/f.rs", 1, 2)
    assert pf.traces == d["traces"]
    # abs_path absent → derived from path (matches the old consumer fallback).
    pf2 = PerFileCoverage.from_dict({"path": ["a", "b", "c.rs"]})
    assert pf2.abs_path == os.sep.join(["a", "b", "c.rs"])
    # everything absent → safe zero/empty defaults, never KeyError.
    pf3 = PerFileCoverage.from_dict({})
    assert (pf3.abs_path, pf3.path, pf3.traces, pf3.covered, pf3.coverable) == ("", [], [], 0, 0)


def test_load_grcov_json_parses_external_json(tmp_path: Path) -> None:
    # Anti-corruption boundary: a synthetic grcov coveralls JSON on disk →
    # CoverageMeasurement of PerFileCoverage (previously untested path).
    import json as _json
    mod = "boundarytest_xyz"
    grcov_json = tmp_path / "target" / "coverage" / f"grcov_{mod}.json"
    grcov_json.parent.mkdir(parents=True)
    grcov_json.write_text(_json.dumps({"source_files": [
        {"name": "src/f.rs", "coverage": [None, 5, 0, 2]},   # 2 covered, 3 coverable
    ]}), encoding="utf-8")
    m = cov.load_grcov_json(tmp_path, mod)
    assert isinstance(m, CoverageMeasurement)
    assert (m.covered, m.coverable) == (2, 3)
    assert len(m.files) == 1 and isinstance(m.files[0], PerFileCoverage)
    assert m.files[0].abs_path == str(tmp_path / "src/f.rs")
    # traces skip the None (non-coverable) line, keep hit counts.
    assert m.files[0].traces == [
        {"line": 2, "stats": {"Line": 5}},
        {"line": 3, "stats": {"Line": 0}},
        {"line": 4, "stats": {"Line": 2}},
    ]

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
    pct, files, covered, coverable, functions = m       # positional: the four line fields, then the function counts
    assert functions is None                            # no FN record: no function reading
    assert (covered, coverable) == (2, 3)
    assert pct == pytest.approx(2 / 3 * 100.0)
    assert len(files) == 1
    rec = files[0]
    assert rec.abs_path == str(tmp_path / "src" / "f.rs")
    assert rec.path == rec.abs_path.split(os.sep)
    assert rec.covered == 2 and rec.coverable == 3
    # traces sorted by line, carrying the hit count under stats.Line.
    assert rec.traces == [
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
    pct, files, covered, coverable, _functions = _provider()._parse_lcov(lcov, tmp_path)
    assert {f.abs_path.rsplit(os.sep, 1)[-1] for f in files} == {"a.rs", "b.rs"}
    assert (covered, coverable) == (2, 3)  # a: 1/2, b: 1/1


def test_parse_lcov_skips_non_src_files(tmp_path: Path) -> None:
    """Only files under src/ enter the denominator — tests/ and out-of-tree are dropped."""
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov,
                "SF:src/keep.rs\nDA:1,1\nend_of_record\n"
                "SF:tests/drop.rs\nDA:1,1\nend_of_record\n"
                "SF:examples/also_drop.rs\nDA:1,1\nend_of_record\n")
    _pct, files, covered, coverable, _functions = _provider()._parse_lcov(lcov, tmp_path)
    assert [f.abs_path.rsplit(os.sep, 1)[-1] for f in files] == ["keep.rs"]
    assert (covered, coverable) == (1, 1)


def test_parse_lcov_absolute_sf_path_inside_src(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    abs_sf = str(tmp_path / "src" / "abs.rs")
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, f"SF:{abs_sf}\nDA:1,4\nDA:2,0\nend_of_record\n")
    _pct, files, covered, coverable, _functions = _provider()._parse_lcov(lcov, tmp_path)
    assert files[0].abs_path == abs_sf
    assert (covered, coverable) == (1, 2)


def test_parse_lcov_duplicate_da_keeps_max_hit(tmp_path: Path) -> None:
    """A line measured twice (e.g. across test binaries) takes the max hit count."""
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,0\nDA:1,7\nend_of_record\n")
    _pct, files, covered, coverable, _functions = _provider()._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (1, 1)
    assert files[0].traces == [{"line": 1, "stats": {"Line": 7}}]


def test_parse_lcov_ignores_malformed_da(tmp_path: Path) -> None:
    """Non-integer or short DA records are skipped, not fatal."""
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:bad,nope\nDA:5\nDA:2,3\nend_of_record\n")
    _pct, _files, covered, coverable, _functions = _provider()._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (1, 1)  # only DA:2,3 parsed


def test_parse_lcov_file_with_no_da_is_zero_coverable(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/empty.rs\nend_of_record\n")
    pct, files, covered, coverable, _functions = _provider()._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (0, 0)
    assert pct == 0.0  # no coverable lines anywhere → 0, not a ZeroDivisionError
    assert files[0].coverable == 0


def test_parse_lcov_missing_file_returns_zeros(tmp_path: Path) -> None:
    pct, files, covered, coverable, _functions = _provider()._parse_lcov(tmp_path / "absent.lcov", tmp_path)
    assert (pct, files, covered, coverable) == (0.0, [], 0, 0)


def test_parse_lcov_pragma_excludes_marked_lines(tmp_path: Path) -> None:
    """When ignore_pragma is set, source lines carrying the marker are removed from
    the denominator (the coverage-ignore pragma pass)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f.rs").write_text("line1\nline2 // EXCL\nline3\n")
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,1\nDA:2,0\nDA:3,0\nend_of_record\n")
    prov = _provider(_deps(ignore_pragma="// EXCL"))
    _pct, _files, covered, coverable, _functions = prov._parse_lcov(lcov, tmp_path)
    # line 2 dropped → {1 (hit), 3 (miss)} → 1/2
    assert (covered, coverable) == (1, 2)


def test_parse_lcov_pragma_unreadable_source_is_tolerated(tmp_path: Path) -> None:
    """Pragma set but the source file is gone → no crash, all DA lines kept."""
    (tmp_path / "src").mkdir()  # no f.rs on disk
    lcov = tmp_path / "c.lcov"
    _write_lcov(lcov, "SF:src/f.rs\nDA:1,1\nDA:2,0\nend_of_record\n")
    prov = _provider(_deps(ignore_pragma="// EXCL"))
    _pct, _files, covered, coverable, _functions = prov._parse_lcov(lcov, tmp_path)
    assert (covered, coverable) == (1, 2)


# --------------------------------------------------------------------------- measure_quick / load_cached / measure

def test_measure_quick_returns_the_whole_measurement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The quick run measures every file; all of them come back, and the
    caller picks its row (`CoverageMeasurement.file_row`)."""
    prov = _provider()
    canned = CoverageMeasurement(50.0, [
        PerFileCoverage.from_dict({"abs_path": str(tmp_path / "src" / "f.rs"), "covered": 3, "coverable": 6}),
        PerFileCoverage.from_dict({"abs_path": str(tmp_path / "src" / "g.rs"), "covered": 1, "coverable": 2}),
    ], 4, 8)
    suffixes: list[str] = []
    monkeypatch.setattr(prov, "_run", lambda pd, mod, suffix="": suffixes.append(suffix) or canned)
    m = prov.measure_quick(tmp_path, "mod")
    assert m is not None and suffixes == ["_quick"]
    assert m.file_lines(tmp_path) == {"src/f.rs": (3, 6), "src/g.rs": (1, 2)}
    assert m.file_row(tmp_path, "src/f.rs") == (3, 6)
    assert m.file_row(tmp_path, "src/other.rs") is None
    # A failed run measured nothing: no reading, not an empty one.
    monkeypatch.setattr(prov, "_run", lambda *a, **k: CoverageMeasurement(0.0, [], 0, 0))
    assert prov.measure_quick(tmp_path, "mod") is None


def test_measure_delegates_to_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _provider()
    sentinel = (99.0, [], 9, 9)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: sentinel)
    assert prov.measure(tmp_path, "mod") == sentinel


def test_load_cached_reads_the_workspace_lcov(tmp_path: Path) -> None:
    """load_cached parses the workspace's last FULL lcov without re-running
    cargo; the lcov lives under the workspace's coverage scratch, not /tmp."""
    prov = _provider()
    (tmp_path / "src").mkdir()
    lcov_path = cov.LlvmCovProvider.lcov_path(tmp_path, "mod")
    assert lcov_path == tmp_path / "target" / "coverage" / "llvmcov_mod.lcov"
    lcov_path.parent.mkdir(parents=True)
    _write_lcov(lcov_path, "SF:src/f.rs\nDA:1,1\nDA:2,0\nend_of_record\n")
    # A newer QUICK lcov is not what a resume restores.
    _write_lcov(cov.LlvmCovProvider.lcov_path(tmp_path, "mod", "_quick"),
                "SF:src/f.rs\nDA:1,1\nDA:2,1\nend_of_record\n")
    result = prov.load_cached(tmp_path, "mod")
    assert result is not None
    _pct, _files, covered, coverable, _functions = result
    assert (covered, coverable) == (1, 2)


def test_load_cached_missing_returns_none(tmp_path: Path) -> None:
    assert _provider().load_cached(tmp_path, "mod") is None


def test_a_failed_llvm_cov_run_does_not_return_the_previous_measurement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cargo llvm-cov` exiting non-zero without writing used to leave the
    PREVIOUS lcov at the output path, and `_run` parsed it as this run's
    reading. The output is deleted before the tool runs, so "the file
    exists" means "this run wrote it"."""
    (tmp_path / "src").mkdir()
    stale = cov.LlvmCovProvider.lcov_path(tmp_path, "mod")
    stale.parent.mkdir(parents=True)
    _write_lcov(stale, "SF:src/f.rs\nDA:1,1\nDA:2,1\nend_of_record\n")

    class _Failed:
        returncode, stdout, stderr = 101, "", "error: could not compile"

    monkeypatch.setattr(cov.subprocess, "run", lambda *a, **k: _Failed())
    m = _provider().measure(tmp_path, "mod")
    assert (m.pct, m.files, m.covered, m.coverable) == (0.0, [], 0, 0)
    assert not stale.exists()


def test_grcov_readings_are_one_measurement_in_the_workspace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both grcov readings run the same grcov command (cluster exclusions
    included), write into the workspace's coverage scratch, and come back
    whole. A resume restores the FULL one even when a quick one is newer."""
    import json as _json
    grcov_bin = tmp_path / "grcov"
    grcov_bin.touch()
    named = _FakeCluster(is_default=False, tarpaulin_exclude_files=["*/legacy.rs"])
    deps = cov.CoverageDeps(log=lambda _m: None, cargo_env=dict, cluster=lambda: named,
                            tarpaulin_bin=Path("/nonexistent/tarp"), grcov_bin=grcov_bin,
                            build_timeout=1, ignore_pragma="")
    ws = tmp_path / "ws"
    ws.mkdir()
    grcov_cmds: list[list[str]] = []
    hits = iter([[1, 0], [1, 1]])   # quick reading first, then the full one

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    def _run(cmd: list[str], **kw: object) -> _Done:
        if cmd[0] == "cargo":
            env = kw["env"]
            assert isinstance(env, dict)
            Path(env["LLVM_PROFILE_FILE"]).parent.joinpath("1-1.profraw").touch()
        else:
            grcov_cmds.append(cmd)
            out = Path(cmd[cmd.index("--output-path") + 1])
            out.write_text(_json.dumps({"source_files": [
                {"name": "src/a.rs", "coverage": next(hits)},
                {"name": "src/b.rs", "coverage": [None, 3]}]}), encoding="utf-8")
        return _Done()

    monkeypatch.setattr(cov.subprocess, "run", _run)
    quick = cov.quick_coverage_grcov(deps, ws, "mod")
    full = cov.run_grcov(deps, ws, "mod")
    assert quick is not None
    assert quick.file_lines(ws) == {"src/a.rs": (1, 2), "src/b.rs": (1, 1)}
    assert full.file_lines(ws) == {"src/a.rs": (2, 2), "src/b.rs": (1, 1)}

    def _shape(c: list[str]) -> list[str]:
        out = list(c)
        out[1] = "<profraw>"
        out[out.index("--output-path") + 1] = "<out>"
        return out

    assert _shape(grcov_cmds[0]) == _shape(grcov_cmds[1])
    assert grcov_cmds[0][grcov_cmds[0].index("*/legacy.rs") - 1] == "--ignore"
    scratch = ws / "target" / "coverage"
    assert [Path(c[c.index("--output-path") + 1]).parent for c in grcov_cmds] == [scratch, scratch]
    # The quick JSON is newer, and still not what a resume restores.
    (scratch / "grcov_quick_mod.json").touch()
    restored = cov.load_grcov_json(ws, "mod")
    assert restored is not None and restored.covered == 3


# --------------------------------------------------------------------------- function records
#
# The lcov reader used to keep only `DA:` lines; the function-execution
# criterion (run `301146-2d8a5f06`, 2026-09-17) reads `FN:`/`FNDA:` into
# `(file, first line) -> max count`. The per-binary duplication below is the
# shape cargo-llvm-cov emitted for an invented crate the same day: one record
# per function per binary, the lib's copy at 0 and the test binary's at the
# real count, under different crate hashes in the mangled name.

_FN_LCOV = """\
SF:src/lib.rs
FN:1,_RNvCsLIB_1m6called
FN:5,_RNvCsLIB_1m5never
FN:9,_RINvCsLIB_1m7genericpEB2_
FN:1,_RNvCsTEST_1m6called
FN:5,_RNvCsTEST_1m5never
FN:9,_RINvCsTEST_1m7genericINtB_3u32EB2_
FN:9,_RINvCsTEST_1m7genericINtB_3u64EB2_
FNDA:0,_RNvCsLIB_1m6called
FNDA:0,_RNvCsLIB_1m5never
FNDA:0,_RINvCsLIB_1m7genericpEB2_
FNDA:3,_RNvCsTEST_1m6called
FNDA:0,_RNvCsTEST_1m5never
FNDA:0,_RINvCsTEST_1m7genericINtB_3u32EB2_
FNDA:2,_RINvCsTEST_1m7genericINtB_3u64EB2_
DA:1,3
DA:5,0
end_of_record
SF:tests/t.rs
FN:1,_RNvCsTEST_1t1t
FNDA:1,_RNvCsTEST_1t1t
DA:1,1
end_of_record
"""


def test_parse_lcov_reads_function_records_by_first_line_max_over_copies(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "f.lcov"
    _write_lcov(lcov, _FN_LCOV)
    m = _provider()._parse_lcov(lcov, tmp_path)
    assert m.functions == {("src/lib.rs", 1): 3, ("src/lib.rs", 5): 0, ("src/lib.rs", 9): 2}
    # Line fields unchanged by the function records.
    assert (m.covered, m.coverable) == (1, 2)


def test_parse_lcov_without_function_records_reports_none_not_zero(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    lcov = tmp_path / "f.lcov"
    _write_lcov(lcov, "SF:src/lib.rs\nDA:1,3\nDA:5,0\nend_of_record\n")
    assert _provider()._parse_lcov(lcov, tmp_path).functions is None


def test_lcov_and_json_export_give_one_map_for_one_shape(tmp_path: Path) -> None:
    """The two readers of the same measurement agree: the lcov FN line is the
    export's first region line, and both collapse copies by max."""
    from pipeline_core.adapters.coverage_owner import _body_counts
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    lcov = tmp_path / "f.lcov"
    _write_lcov(lcov, _FN_LCOV)
    src = str(crate / "src" / "lib.rs")

    def _fn(name: str, line: int, count: int) -> dict[str, object]:
        return {"name": name, "count": count, "filenames": [src],
                "regions": [[line, 5, line + 2, 2, count, 0, 0, 0]]}

    export = {"data": [{"functions": [
        _fn("called", 1, 0), _fn("never", 5, 0), _fn("generic", 9, 0),
        _fn("called", 1, 3), _fn("never", 5, 0), _fn("generic", 9, 0), _fn("generic", 9, 2),
    ]}]}
    assert _body_counts(export, crate) == _provider()._parse_lcov(lcov, crate).functions
