"""Per-test isolation of aborted binaries, and stable doc-test identity.

Both are the measurement half of one rule: every test carries its own
verdict, and a comparison asks per test whether a passing test was lost.
Run `3213361-9d582ae7` (2026-09-19) reverted a correct first constructor
because one test's abort erased its binary's other verdicts, and because a
doc test renamed by a line shift read as newly red. Shapes here are
invented; no crate-under-reconstruction name appears.
"""
from __future__ import annotations

from dataclasses import dataclass as _dc
from pathlib import Path

import pytest

from pipeline_core.application.reconstruct import cargo_runner as cr
from pipeline_core.application.reconstruct.crash_localizer import (
    CrashLocalizerDeps,
    localize_crashes,
)
from pipeline_core.domain.test_isolation import (
    IsolatedStatus,
    IsolatedTest,
    harness_args,
    is_isolatable_test_run,
    listed_tests,
    listing_args,
    read_single_test_run,
    render_isolated_section,
    run_args,
    splice_isolated_sections,
)
from pipeline_core.domain.test_results import (
    ABORT_MARK,
    BinaryResult,
    TestStateReading,
    compare_readings,
    parse_binary_results,
    stable_test_keys,
)


def test_only_commands_that_run_tests_are_isolated() -> None:
    assert is_isolatable_test_run("cargo test --no-fail-fast")
    assert is_isolatable_test_run("cargo +nightly test -- a b")
    assert not is_isolatable_test_run("cargo test --no-run")
    assert not is_isolatable_test_run("cargo test -- --list")
    assert not is_isolatable_test_run("cargo check --all-targets")
    assert not is_isolatable_test_run("./target/debug/deps/x --exact t")


def test_harness_args_split_flags_from_filters() -> None:
    flags, filters = harness_args(
        "cargo test -- --test-threads 4 --skip slow alpha beta -q")
    assert filters == ("alpha", "beta")
    assert flags == ("--skip", "slow")
    assert listing_args("cargo test -- alpha --exact") == [
        "--list", "--format", "terse", "--exact", "alpha"]
    assert run_args("cargo test -- --skip slow alpha", "m::t") == [
        "--skip", "slow", "--test-threads=1", "--exact", "m::t"]


def test_listed_tests_reads_terse_listing() -> None:
    out = "m::a: test\nm::b: test\nbench_x: benchmark\n\n2 tests, 1 benchmark\n"
    assert listed_tests(out) == ("m::a", "m::b")


def _single(name: str, verdict: str, body: str = "") -> str:
    lines = ["", "running 1 test", f"test {name} ... {verdict}"]
    if verdict == "FAILED":
        lines += ["", "failures:", "", f"---- {name} stdout ----", body, "",
                  "failures:", f"    {name}", ""]
    lines.append(f"test result: {'ok' if verdict != 'FAILED' else 'FAILED'}. "
                 f"{int(verdict == 'ok')} passed; {int(verdict == 'FAILED')} "
                 "failed; 0 ignored; 0 measured; 9 filtered out")
    return "\n".join(lines)


def test_single_run_outcomes() -> None:
    ok = read_single_test_run("t", 0, _single("t", "ok"),
                              timed_out=False, timeout=5)
    assert ok.status is IsolatedStatus.PASSED
    bad = read_single_test_run(
        "t", 101, _single("t", "FAILED", "thread 't' panicked at tests/x.rs:3:5:\nboom"),
        timed_out=False, timeout=5)
    assert bad.status is IsolatedStatus.FAILED and "boom" in bad.body
    died = read_single_test_run(
        "t", 134,
        "\nrunning 1 test\ntest t ... \n--- stderr ---\n"
        "thread 't' panicked at src/a.rs:9:5:\nnot implemented\n",
        timed_out=False, timeout=5)
    assert died.status is IsolatedStatus.ABORTED
    assert "not implemented" in died.body
    hung = read_single_test_run("t", -1, "", timed_out=True, timeout=5)
    assert hung.status is IsolatedStatus.HUNG


def test_rendered_section_parses_to_per_test_outcomes() -> None:
    text = render_isolated_section([
        IsolatedTest("a", IsolatedStatus.PASSED),
        IsolatedTest("b", IsolatedStatus.FAILED, "thread 'b' panicked at t.rs:1:1:\nx"),
        IsolatedTest("c", IsolatedStatus.ABORTED,
                     "thread 'c' panicked at src/s.rs:4:5:\nnot implemented"),
        IsolatedTest("d", IsolatedStatus.ABORTED, "(exit status -11)"),
        IsolatedTest("e", IsolatedStatus.IGNORED),
    ])
    (b,) = parse_binary_results(text)
    assert b.completed
    assert b.passed == {"a"} and b.failed == {"b", "c", "d"}
    assert b.ignored == {"e"}
    assert b.aborted == {"c", "d"}
    # Only the abort that printed no panic is a native fault.
    assert b.aborted_silent == {"d"}
    assert (b.pass_count, b.fail_count) == (1, 3)
    assert ABORT_MARK in text


def test_splice_replaces_the_aborted_section_and_its_stderr() -> None:
    stdout = "\n".join([
        "running 1 test", "test p ... ok", "",
        "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
        "", "running 2 tests", "test q ... ok", "test r ... "])
    stderr = "\n".join([
        "     Running tests/one.rs (target/debug/deps/one-1)",
        "     Running tests/two.rs (target/debug/deps/two-2)",
        "thread 'r' panicked at src/s.rs:4:5:", "not implemented",
        "error: test failed, to rerun pass `--test two`",
        "error: 1 target failed:", "    `--test two`"])
    rebuilt = render_isolated_section([
        IsolatedTest("q", IsolatedStatus.PASSED),
        IsolatedTest("r", IsolatedStatus.ABORTED,
                     "thread 'r' panicked at src/s.rs:4:5:\nnot implemented")])
    got = splice_isolated_sections(stdout, stderr, {1: rebuilt})
    assert got is not None
    out, err = got
    one, two = parse_binary_results(out + "\n--- stderr ---\n" + err)
    assert one.label == "tests/one.rs" and one.passed == {"p"}
    assert two.label == "tests/two.rs" and two.completed
    assert two.passed == {"q"} and two.aborted == {"r"}
    # The whole-run abort's lines no longer describe any test; cargo's
    # final summary stays.
    assert "to rerun pass" not in err and "1 target failed" in err


def test_an_ambiguous_capture_is_left_alone() -> None:
    # A headerless section cannot be located for a splice.
    stdout = "test q ... ok\ntest result: ok. 1 passed; 0 failed"
    assert splice_isolated_sections(stdout, "", {0: "x"}) is None


def _bin(label: str, passed: set[str], failed: set[str]) -> BinaryResult:
    return BinaryResult(label=label, completed=True,
                        passed=frozenset(passed), failed=frozenset(failed),
                        pass_count=len(passed), fail_count=len(failed))


def test_doc_test_identity_survives_a_line_shift() -> None:
    doc = "Doc-tests widgets"
    base = TestStateReading((_bin(doc, {"src/a.rs - Gadget::new (line 10)"},
                                  {"src/a.rs - Gadget::get (line 40)",
                                   "src/a.rs - Gadget::get (line 55)"}),))
    after = TestStateReading((_bin(doc, {"src/a.rs - Gadget::new (line 10)"},
                                   {"src/a.rs - Gadget::get (line 47)",
                                    "src/a.rs - Gadget::get (line 62)"}),))
    cmp = compare_readings(base, after)
    assert cmp.newly_red == frozenset() and not cmp.regressed
    # The k-th example of a name is still the k-th: the second example going
    # green is newly green, not a rename.
    fixed = TestStateReading((_bin(doc, {"src/a.rs - Gadget::new (line 10)",
                                         "src/a.rs - Gadget::get (line 62)"},
                                   {"src/a.rs - Gadget::get (line 47)"}),))
    assert compare_readings(base, fixed).newly_green == {
        "src/a.rs - Gadget::get (line 62)"}


def test_negative_control_raw_names_read_the_shift_as_new_failures() -> None:
    b = _bin("Doc-tests widgets", set(), {"src/a.rs - Gadget::get (line 40)"})
    c = _bin("Doc-tests widgets", set(), {"src/a.rs - Gadget::get (line 47)"})
    assert c.failed - b.failed  # the pre-fix comparison: a "new" failure
    keys_b, keys_c = stable_test_keys(b), stable_test_keys(c)
    assert {keys_c[n] for n in c.failed} == {keys_b[n] for n in b.failed}


# --- the runner half, driven through a fake cargo port -----------------------



@_dc(frozen=True)
class _Proc:
    returncode: int
    stdout: str
    stderr: str
    failure: object = None


class _FakeCargo:
    """A shared-process run where `a` aborts the binary before `b` reports,
    and single-test runs that give each test its own outcome: `a` dies on a
    stub panic during cleanup, `b` passes, `c` dies on a signal with no
    panic."""

    def __init__(self, exe: Path) -> None:
        self.exe = str(exe)
        self.calls: list[str] = []

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str],
            timeout: int = 180) -> _Proc:
        self.calls.append(cmd)
        if cmd.startswith("cargo test"):
            return _Proc(101, "\nrunning 3 tests\ntest a ... ",
                         f"     Running tests/t.rs ({self.exe})\n"
                         "thread 'a' panicked at src/s.rs:4:5:\nnot implemented\n"
                         "error: test failed, to rerun pass `--test t`\n")
        if "--list" in cmd:
            return _Proc(0, "a: test\nb: test\nc: test\n", "")
        name = cmd.rsplit(" ", 1)[-1]
        if name == "b":
            return _Proc(0, "\nrunning 1 test\ntest b ... ok\n\n"
                         "test result: ok. 1 passed; 0 failed; 0 ignored; "
                         "0 measured; 2 filtered out\n", "")
        if name == "a":
            return _Proc(134, "\nrunning 1 test\ntest a ... ",
                         "thread 'a' panicked at tests/t.rs:3:5:\nown\n"
                         "thread 'a' panicked at src/s.rs:4:5:\nnot implemented\n")
        return _Proc(139, "\nrunning 1 test\ntest c ... ", "")


@pytest.fixture
def exe(tmp_path: Path) -> Path:
    p = tmp_path / "t-bin"
    p.write_text("")
    return p


def test_run_cargo_rebuilds_an_aborted_binary_per_test(
        exe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    cargo = _FakeCargo(exe)
    rc, out = cr.run_cargo(cargo, "cargo test --no-fail-fast", tmp_path, {})
    (b,) = parse_binary_results(out)
    assert rc == 101 and b.completed and b.label == "tests/t.rs"
    assert b.passed == {"b"} and b.aborted == {"a", "c"}
    assert b.aborted_silent == {"c"}
    assert "to rerun pass" not in out
    assert any("--exact c" in c for c in cargo.calls)


def test_negative_control_isolation_off_keeps_the_dead_section(
        exe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "0")
    cargo = _FakeCargo(exe)
    _rc, out = cr.run_cargo(cargo, "cargo test --no-fail-fast", tmp_path, {})
    (b,) = parse_binary_results(out)
    assert not b.completed and "b" not in b.passed
    assert len(cargo.calls) == 1


def test_a_missing_executable_keeps_the_original_section(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    cargo = _FakeCargo(tmp_path / "gone")
    _rc, out = cr.run_cargo(cargo, "cargo test", tmp_path, {})
    assert not parse_binary_results(out)[0].completed


def test_the_localizer_reruns_a_silent_abort_by_its_own_test(
        exe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    _rc, out = cr.run_cargo(_FakeCargo(exe), "cargo test", tmp_path, {})
    seen: list[object] = []

    def gdb(exe_path: Path, cwd: Path, env: dict[str, str], *,
            test_filter: str | None = None) -> None:
        seen.append(test_filter)
        return None

    localize_crashes(CrashLocalizerDeps(gdb_localize=gdb, asan_sweep=None,
                                        log=lambda _m: None),
                     out, "cargo test", tmp_path, {})
    # `a` printed its panics (a stub abort, attributable from its block);
    # only the silent `c` is a native fault worth a gdb run.
    assert seen == ["c"]


def test_single_run_with_no_section_is_a_failure_never_a_pass() -> None:
    t = read_single_test_run("x", 1, "error: could not execute",
                             timed_out=False, timeout=5)
    assert t.status is IsolatedStatus.FAILED and "could not execute" in t.body
