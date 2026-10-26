"""Pins for the exercises writer: the line→unit mapping (domain, cross-checked
against `attribution.line_to_fn_record`'s contract and the src/ rooting) and
the per-spec coverage run (adapter, cargo/grcov faked)."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.behaviour_exercises import exercises_by_coverage
from pipeline_core.domain.attribution import line_to_fn_record
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    ObservableOutcome,
)
from pipeline_core.domain.behaviour_coverage import (
    coveralls_hit_lines,
    normalize_record_files,
    units_for_hit_lines,
)
from pipeline_core.domain.behaviour_coverage import test_command as command_for
from pipeline_core.domain.behaviour_coverage import test_target_of as target_of
from pipeline_core.domain.models import BuildCluster

_SRC = (
    "pub fn a() -> u8 {\n"        # line 1
    "    1\n"                      # line 2
    "}\n"                          # line 3
    "\n"
    "pub fn b() -> u8 {\n"        # line 5
    "    2 // — non-ascii comment\n"   # line 6
    "}\n"                          # line 7
    "#[test]\nfn t() { a(); }\n"   # lines 8-9
)


def _records(prefix: str = "src/") -> list[dict[str, object]]:
    b = _SRC.encode("utf-8")
    a_sig, a_end = 0, b.index(b"}\n") + 1
    b_sig = b.index(b"pub fn b")
    b_end = b.index(b"}\n", b_sig) + 1
    t_sig = b.index(b"fn t()")
    return [
        {"file": f"{prefix}lib.rs", "fn_name": "a", "sig_byte_start": a_sig, "body_byte_end": a_end},
        {"file": f"{prefix}lib.rs", "fn_name": "b", "sig_byte_start": b_sig, "body_byte_end": b_end},
        {"file": f"{prefix}lib.rs", "fn_name": "t", "sig_byte_start": t_sig,
         "body_byte_end": len(b), "is_test": True},
    ]


def test_coveralls_hit_lines_reads_nonzero_counts():
    data = {"source_files": [
        {"name": "src/lib.rs", "coverage": [1, 1, None, None, 0, 0, 1]},
        {"name": "src/other.rs", "coverage": [0, None]},
    ]}
    assert coveralls_hit_lines(data) == {"src/lib.rs": [1, 2, 7]}
    assert coveralls_hit_lines({"source_files": [{"name": "", "coverage": [1]}]}) == {}
    assert coveralls_hit_lines({}) == {}


def test_units_for_hit_lines_matches_line_to_fn_record_contract():
    hits = {"src/lib.rs": [2, 6]}
    texts = {"src/lib.rs": _SRC}
    got = units_for_hit_lines(hits, texts, _records())
    assert got == frozenset({"src/lib.rs::a", "src/lib.rs::b"})
    # the same answer the attribution helper gives, line by line
    for ln in (2, 6):
        rec = line_to_fn_record(_SRC, ln, _records())
        assert f"src/lib.rs::{rec['fn_name']}" in got
    # a hit on the test fn is not a unit; a hit on a blank line maps nowhere
    assert units_for_hit_lines({"src/lib.rs": [4, 9]}, texts, _records()) == frozenset()
    empty_span = [{"file": "src/lib.rs", "fn_name": "a", "sig_byte_start": 4, "body_byte_end": 4}]
    assert units_for_hit_lines({"src/lib.rs": [2]}, texts, empty_span) == frozenset()


def test_records_enumerated_from_src_are_rerooted_before_the_join():
    bare = _records(prefix="")
    assert [r["file"] for r in normalize_record_files(bare)] == ["src/lib.rs"] * 3
    got = units_for_hit_lines({"src/lib.rs": [2]}, {"src/lib.rs": _SRC}, bare)
    assert got == frozenset({"src/lib.rs::a"})
    # negative control: without the re-rooting the join is empty
    assert units_for_hit_lines({"lib.rs": [2]}, {"src/lib.rs": _SRC}, _records()) == frozenset()


def _ta(name: str, ref: str) -> BehaviourSpec:
    return BehaviourSpec(name, "#[test]\nfn x() {}", "x", ObservableOutcome.display("v"),
                         subkind=BehaviourSubKind.OUTCOME, source_ref=ref)


def test_test_command_from_source_ref():
    s = _ta("testassert::tests/test_fmt.rs::test_altdebug::0", "tests/test_fmt.rs::test_altdebug")
    assert target_of(s) == ("test_fmt", "test_altdebug")
    assert command_for(s) == ["cargo", "test", "--quiet", "--test", "test_fmt",
                              "--", "--exact", "test_altdebug"]
    assert command_for(_ta("doctest::lib.rs::3", "lib.rs:3")) is None
    no_name = _ta("testassert::tests/test_fmt.rs::", "tests/test_fmt.rs::")
    assert target_of(no_name) is None
    empty_stem = _ta("testassert::x", "tests/::t")
    assert target_of(empty_stem) is None
    assert target_of(_ta("testassert::x", "no-sep")) is None


# --- adapter, cargo + grcov faked ------------------------------------------------------

def _cluster() -> BuildCluster:
    return BuildCluster(name="stable", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def _artifact(kind: str, name: str, exe: str | None) -> str:
    return json.dumps({"reason": "compiler-artifact", "fresh": True, "executable": exe,
                       "target": {"kind": [kind], "name": name}})


def _exercise_run(tmp_path: Path, *, name_binaries: bool) -> list[str]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_SRC, encoding="utf-8")
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []
    debug = tmp_path / "target" / "t0d-stable" / "debug"
    test_exe = str(debug / "deps" / "t-0123")
    example_exe = str(debug / "examples" / "x")

    def fake_run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int):
        calls.append(cmd)
        envs.append(env)
        if cmd[:2] == ["cargo", "test"] and "--no-run" in cmd:
            out = "\n".join([_artifact("lib", "demo", None), _artifact("test", "t", test_exe)])
            return 0, out if name_binaries else "", ""
        if cmd[:2] == ["cargo", "test"]:
            name = cmd[-1]
            if name == "test_fails":
                return 101, "", "test failed"
            profraw = Path(env["LLVM_PROFILE_FILE"].replace("%p-%m", "1-2"))
            profraw.write_bytes(b"prof")
            return 0, "", ""
        if cmd[:2] == ["cargo", "run"]:
            profraw = Path(env["LLVM_PROFILE_FILE"].replace("%p-%m", "1-2"))
            profraw.write_bytes(b"prof")
            assert (cwd / "examples" / f"{cmd[-1]}.rs").exists()
            # The program's own stdout is interleaved with cargo's messages.
            out = "\n".join(['{"reason": "x"}', "2", _artifact("example", cmd[-1], example_exe)])
            return 0, out if name_binaries else "out", ""
        # grcov: one file, hits depend on which spec ran (by profraw dir)
        out_json = Path(cmd[cmd.index("--output-path") + 1])
        spec_dir = out_json.parent.name
        cov = [1, 1, None, None, 0, 0, 0] if spec_dir == "spec_000" else [0, 0, None, None, 1, 1, 1]
        out_json.write_text(json.dumps({"source_files": [
            {"name": "src/lib.rs", "coverage": cov},
            {"name": "tests/t.rs", "coverage": [1]}]}))
        return 0, "", ""

    specs = [
        _ta("testassert::tests/t.rs::test_a::0", "tests/t.rs::test_a"),
        BehaviourSpec("probe::stable::b::0", "fn main() { b(); }", "b",
                      ObservableOutcome.stdout("2"), subkind=BehaviourSubKind.OUTCOME),
        _ta("testassert::tests/t.rs::test_fails::0", "tests/t.rs::test_fails"),
        BehaviourSpec("doctest::lib.rs::1", "", "let x = a();", None),
    ]
    logs: list[str] = []
    got, n = exercises_by_coverage(tmp_path, _cluster(), specs, _records(prefix=""),
                                   run=fake_run, log=logs.append, grcov_bin=Path("/x/grcov"))
    assert got == {
        "testassert::tests/t.rs::test_a::0": frozenset({"src/lib.rs::a"}),
        "probe::stable::b::0": frozenset({"src/lib.rs::b"}),
    }, "a failing run is absent (never an empty set); the doc-test is skipped"
    assert n == len(calls), "every cargo/grcov call is one invocation"
    assert calls[0] == ["cargo", "test", "--no-run", "--quiet", "--message-format=json"], \
        "one instrumented build first"
    assert ["cargo", "test", "--quiet", "--test", "t", "--", "--exact", "test_a"] in calls
    assert any(c[:5] == ["cargo", "run", "--quiet", "--message-format=json", "--example"]
               for c in calls)
    assert all("-C instrument-coverage" in e["RUSTFLAGS"] for e in envs)
    assert all(e["CARGO_INCREMENTAL"] == "0" for e in envs)
    assert all(e["CARGO_TARGET_DIR"] == str(tmp_path / "target" / "t0d-stable") for e in envs)
    grcov_bins = [c[c.index("--binary-path") + 1] for c in calls if c and c[0].endswith("grcov")]
    if name_binaries:
        # grcov reads the one binary that wrote the profraw, never the directory.
        assert grcov_bins == [test_exe, example_exe]
        assert not any("HEALTH" in ln for ln in logs)
    else:
        assert grcov_bins == [str(debug), str(debug)]
    profile_files = {e["LLVM_PROFILE_FILE"] for e in envs if "LLVM_PROFILE_FILE" in e}
    assert len(profile_files) >= 3, "each spec runs under its own LLVM_PROFILE_FILE"
    assert any("doc-test spec(s) skipped" in ln for ln in logs)
    assert any("test_fails" in ln and "run failed" in ln for ln in logs)
    assert not (tmp_path / "examples").exists()
    return logs


def test_exercises_by_coverage_runs_each_spec_alone_and_maps_hits(tmp_path: Path) -> None:
    _exercise_run(tmp_path, name_binaries=True)


def test_exercises_fall_back_to_the_directory_and_say_so(tmp_path: Path) -> None:
    """The pre-change reading: cargo names no binary, grcov is given the
    world's debug directory (correct, and one load of every binary in it per
    spec), and the run says how many specs took that path."""
    logs = _exercise_run(tmp_path, name_binaries=False)
    assert any("HEALTH ‼ cargo named no binary for 2 spec(s)" in ln for ln in logs)


def test_executables_are_read_off_cargo_messages() -> None:
    from pipeline_core.domain.cargo_artifacts import executables_from_messages
    out = "\n".join([
        _artifact("lib", "demo", None),
        _artifact("test", "t", "/t/deps/t-1"),
        "plain program output",
        "{not json",
        "[1, 2]",
        '{"reason": "build-finished", "success": true}',
        _artifact("example", "x", "/t/examples/x"),
    ])
    assert executables_from_messages(out) == {
        ("test", "t"): "/t/deps/t-1", ("example", "x"): "/t/examples/x"}


def test_exercises_without_grcov_returns_nothing(tmp_path: Path, monkeypatch) -> None:
    import pipeline_core.adapters.behaviour_exercises as mod
    monkeypatch.setattr(mod, "grcov_binary", lambda: None)
    logs: list[str] = []
    assert exercises_by_coverage(tmp_path, None, [_ta("testassert::tests/t.rs::x::0", "tests/t.rs::x")],
                                 [], run=lambda *a: (0, "", ""), log=logs.append) == ({}, 0)
    assert any("grcov not found" in ln for ln in logs)
