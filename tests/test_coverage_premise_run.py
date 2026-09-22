"""The premise run's own contract, with the processes faked: which symbols
are boundaries, what each failure mode reports, and how `_owner_map` splits
a failing run into premise and rendered while a passing or killed run keeps
its full profile. The real-process half (cargo, gdb, llvm-cov) is
`tests/integration/test_coverage_panic_premise.py`; this module keeps the
decision paths in the default suite.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline_core.adapters import coverage_owner as cov
from pipeline_core.domain.models import FnSite

NM_OUT = """\
0000000000012340 T _RNvCsfLfy6EI15iL_7___rustc17rust_begin_unwind
0000000000012350 t _RINvCs3mSbOeLENLV_4test18assert_test_resultuECs8W92hqChbFu_1t
0000000000012360 T _RNvCs1_4core9panicking9panic_fmt
0000000000012370 U rust_begin_unwind
"""

LOOKUP = FnSite("src/lib.rs", "lookup", "")
RENDER = FnSite("src/lib.rs", "fmt", "impl Debug for W")


def _done(cmd, rc=0, out=""):
    return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


def test_boundary_symbols_are_the_defined_panic_handler_and_result_check(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(cov, "_run", lambda cmd, cwd, env=None, **k: _done(cmd, 0, NM_OUT))
    syms = cov._boundary_symbols(tmp_path / "bin")
    assert syms == ("_RNvCsfLfy6EI15iL_7___rustc17rust_begin_unwind",
                    "_RINvCs3mSbOeLENLV_4test18assert_test_resultuECs8W92hqChbFu_1t")
    monkeypatch.setattr(cov, "_run", lambda cmd, cwd, env=None, **k: _done(cmd, 1))
    assert cov._boundary_symbols(tmp_path / "bin") == ()
    monkeypatch.setattr(cov.shutil, "which", lambda n: None)
    assert cov._boundary_symbols(tmp_path / "bin") == ()


def test_premise_run_reports_why_it_has_no_cut(monkeypatch, tmp_path: Path) -> None:
    base = tmp_path / "t-premise.profraw"
    monkeypatch.setattr(cov.shutil, "which", lambda n: None)
    assert cov._premise_run(tmp_path, tmp_path / "b", "t", base, {}, timeout=5,
                            symbols=("x",)).reason == "gdb not on PATH"
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)
    assert "no premise boundary symbol" in cov._premise_run(
        tmp_path, tmp_path / "b", "t", base, {}, timeout=5, symbols=()).reason
    seen: list[list[str]] = []
    monkeypatch.setattr(cov, "_run", lambda cmd, cwd, env=None, **k: (seen.append(cmd), _done(cmd))[1])
    got = cov._premise_run(tmp_path, tmp_path / "b", "t", base, {}, timeout=5,
                           symbols=("sym",))
    assert got.raw is None and got.reason == "no boundary was reached"
    assert seen[0][0] == "/usr/bin/gdb" and "--exact" in seen[0]
    assert "SYMS = ['sym']" in base.with_suffix(".gdb.py").read_text()


def test_premise_run_takes_the_last_non_empty_cut(monkeypatch, tmp_path: Path) -> None:
    base = tmp_path / "t-premise.profraw"
    monkeypatch.setattr(cov.shutil, "which", lambda n: "/usr/bin/" + n)

    def _gdb(cmd, cwd, env=None, **k):
        for i, body in ((1, b"early"), (2, b"final"), (10, b"")):
            Path(f"{base}.cut{i}").write_bytes(body)
        return _done(cmd, 0)

    monkeypatch.setattr(cov, "_run", _gdb)
    got = cov._premise_run(tmp_path, tmp_path / "b", "t", base, {}, timeout=5,
                           symbols=("sym",))
    assert got.raw is not None and got.raw.read_bytes() == b"final"


@pytest.mark.parametrize(("rc", "premise", "owned", "rendered"), [
    (101, True, {LOOKUP}, {RENDER}),       # failed: the report is not owned
    (101, False, {LOOKUP, RENDER}, set()),  # negative control: the pre-cut map
    (0, True, {LOOKUP, RENDER}, set()),     # passed: nothing was reported
])
def test_owner_map_splits_a_failing_run_at_its_premise(
        monkeypatch, tmp_path: Path, rc: int, premise: bool,
        owned: set, rendered: set) -> None:
    binary = tmp_path / "t_bin-0123456789abcdef"
    raw = tmp_path / "full.profraw"
    raw.write_bytes(b"x")
    monkeypatch.setattr(cov, "_exact_run", lambda *a, **k: cov.ExactRun(
        name="t", binary=binary, returncode=rc, raw=raw, completed=True))
    monkeypatch.setattr(cov, "_boundary_symbols", lambda b: ("sym",))
    cut = tmp_path / "cut.profraw"
    cut.write_bytes(b"y")
    monkeypatch.setattr(cov, "_premise_run", lambda *a, **k: cov.PremiseRun(cut))
    monkeypatch.setattr(cov, "_merge_export",
                        lambda raws, objs, dest, *a: {"raw": str(raws[0])})
    monkeypatch.setattr(cov, "_entered_sites", lambda export, tree, index: (
        frozenset({LOOKUP}) if export["raw"] == str(cut)
        else frozenset({LOOKUP, RENDER})))
    reading = cov._owner_map(
        tmp_path, [binary], tmp_path, {}, ["t"], "p", "c", None,
        timeout=5, premise=premise)
    assert reading.owners["t"] == frozenset(owned)
    assert reading.rendered.get("t", frozenset()) == frozenset(rendered)
    assert not reading.premise_fallbacks


def test_owner_map_falls_back_loudly_when_no_cut_is_taken(
        monkeypatch, tmp_path: Path) -> None:
    binary = tmp_path / "t_bin-0123456789abcdef"
    raw = tmp_path / "full.profraw"
    raw.write_bytes(b"x")
    monkeypatch.setattr(cov, "_exact_run", lambda *a, **k: cov.ExactRun(
        name="t", binary=binary, returncode=101, raw=raw, completed=True))
    monkeypatch.setattr(cov, "_boundary_symbols", lambda b: ())
    monkeypatch.setattr(cov, "_merge_export", lambda raws, objs, dest, *a: {})
    monkeypatch.setattr(cov, "_entered_sites",
                        lambda export, tree, index: frozenset({LOOKUP, RENDER}))

    def _boom(*a, **k):
        raise OSError("disk")

    for premise_run, reason in (
            (lambda *a, **k: cov.PremiseRun(None, "no boundary was reached"),
             "no boundary was reached"),
            (_boom, "premise run failed (OSError)")):
        monkeypatch.setattr(cov, "_premise_run", premise_run)
        reading = cov._owner_map(tmp_path, [binary], tmp_path, {}, ["t"],
                                 "p", "c", None, timeout=5)
        assert reading.owners["t"] == frozenset({LOOKUP, RENDER})
        assert reason in reading.premise_fallbacks["t"]


def test_the_knob_turns_the_cut_off(monkeypatch) -> None:
    monkeypatch.delenv("PHASE_C_COVERAGE_PREMISE", raising=False)
    assert cov._premise_cut_enabled()
    monkeypatch.setenv("PHASE_C_COVERAGE_PREMISE", "0")
    assert not cov._premise_cut_enabled()
