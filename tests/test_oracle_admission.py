"""Pins for authoring-time admission of a generated test block.

The incident these lock down: an augmented test asserted `"other error: boxed_err"`
for the display of a boxed `io::Error` that renders as `"boxed_err"`. It compiled,
so it entered the reconstruction oracle — where no implementation can satisfy it,
and the fns it covers are re-attempted forever. It was checkable the moment it was
written: the implementation it specified was still on disk.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.tasks.task0c import (
    _COMPILE_FIX_HEAD,
    _RUNTIME_FIX_HEAD,
    _engine_runtime_gate,
)
from pipeline_core.domain.oracle_admission import judge_block, weakening_reason
from pipeline_core.domain.rust_scan import attributed_test_fn_names, count_attrs

BLOCK = """#[cfg(test)]
mod gen_tests_2 {
    use super::*;

    #[test]
    fn test_from_boxed_basic() {
        let boxed: Box<dyn StdError + Send + Sync> = Box::new(io::Error::new(io::ErrorKind::Other, "boxed_err"));
        assert_eq!(Error::from_boxed(boxed).to_string(), "other error: boxed_err");
    }

    #[test]
    fn test_from_boxed_with_io_not_found() {
        let boxed: Box<dyn StdError + Send + Sync> = Box::new(io::Error::new(io::ErrorKind::NotFound, "missing"));
        assert!(Error::from_boxed(boxed).to_string().contains("missing"));
    }
}
"""

FAILED_OUTPUT = """
running 2 tests
test error::gen_tests_2::test_from_boxed_basic ... FAILED
test error::gen_tests_2::test_from_boxed_with_io_not_found ... ok

failures:

---- error::gen_tests_2::test_from_boxed_basic stdout ----
thread 'error::gen_tests_2::test_from_boxed_basic' panicked at src/error.gen_tests_2.rs:15:5:
assertion `left == right` failed
  left: "boxed_err"
 right: "other error: boxed_err"

failures:
    error::gen_tests_2::test_from_boxed_basic

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out
"""

PASSED_OUTPUT = """
running 2 tests
test error::gen_tests_2::test_from_boxed_basic ... ok
test error::gen_tests_2::test_from_boxed_with_io_not_found ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_wrong_expectation_is_refused_admission() -> None:
    adm = judge_block(BLOCK, FAILED_OUTPUT, run_ok=False)
    assert not adm.admitted
    assert adm.failed_names == {"test_from_boxed_basic"}
    # the fix prompt must carry the actual left/right, not just the test name
    detail = adm.detail(3_000)
    assert 'left: "boxed_err"' in detail
    assert "test_from_boxed_basic" in detail


def test_all_green_is_admitted() -> None:
    adm = judge_block(BLOCK, PASSED_OUTPUT, run_ok=True)
    assert adm.admitted
    assert adm.declared == {"test_from_boxed_basic", "test_from_boxed_with_io_not_found"}
    assert adm.detail(3_000) == ""


def test_foreign_failure_never_convicts_this_block() -> None:
    """A test that slips past the name filter fails the RUN but not the block:
    conviction is by declared name, so an unrelated red test costs no re-author."""
    out = PASSED_OUTPUT + "\ntest other::gen_tests_2::test_elsewhere ... FAILED\n"
    adm = judge_block(BLOCK, out, run_ok=False)
    assert adm.admitted


def test_no_verdict_on_a_dead_run_counts_as_failure() -> None:
    """A block that aborts the binary reports nothing — silence plus a non-zero
    exit is a crash, never a pass."""
    out = "running 2 tests\ntest error::gen_tests_2::test_from_boxed_basic ... ok\n"
    adm = judge_block(BLOCK, out, run_ok=False)
    assert not adm.admitted
    assert adm.vanished == {"test_from_boxed_with_io_not_found"}
    assert "no verdict" in adm.detail(3_000)


def test_declared_names_survive_an_intervening_attribute() -> None:
    block = "#[test]\n#[should_panic]\nfn test_boom() { panic!() }\n"
    assert attributed_test_fn_names(block) == ["test_boom"]


def test_count_attrs_reads_every_attribute_form() -> None:
    assert count_attrs("#[ignore]\n#[ignore = \"why\"]\n", "ignore") == 2
    assert count_attrs('#[should_panic(expected = "x")]', "should_panic") == 1
    assert count_attrs("#[ignored_by_us]", "ignore") == 0


def test_silencing_a_failure_is_rejected() -> None:
    before = "#[test]\nfn test_a() { assert_eq!(f(), 1); }\n"
    assert weakening_reason(before, "#[test]\n#[ignore]\nfn test_a() { assert_eq!(f(), 1); }\n")
    assert weakening_reason(before, "#[test]\n#[should_panic]\nfn test_a() { f(); }\n")
    assert weakening_reason(before, "// nothing left\n") == "dropped every #[test] fn"


def test_correcting_or_narrowing_a_block_is_allowed() -> None:
    """Fixing the expectation, and dropping one test of several, are both honest
    outcomes — only silencing is not."""
    before = ("#[test]\nfn test_a() { assert_eq!(f(), 1); }\n"
              "#[test]\nfn test_b() { assert_eq!(g(), 2); }\n")
    assert weakening_reason(before, before.replace("f(), 1", "f(), 7")) is None
    assert weakening_reason(before, "#[test]\nfn test_b() { assert_eq!(g(), 2); }\n") is None


def test_runtime_gate_runs_only_this_block_and_judges_it() -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_kw: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout=FAILED_OUTPUT, stderr="")

    adm = _engine_runtime_gate(
        project_dir=Path("."), module_name="crate_x", pass_n=2, block=BLOCK,
        subprocess=SimpleNamespace(run=_run), cargo_env=lambda: {}, timeout=60)
    assert calls[0][:4] == ["cargo", "test", "-p", "crate_x"]
    assert "gen_tests_2::" in calls[0]
    assert not adm.admitted


def test_fix_heads_render_and_teach_the_honest_correction() -> None:
    """The runtime head must survive `.format` (its Rust examples carry braces)
    and must name the failure modes it is there to prevent."""
    rendered = _RUNTIME_FIX_HEAD.format(rel_path="src/x.rs", directive="", history="",
                                        err="assertion failed")
    assert "#[ignore]" in rendered and "#[should_panic]" in rendered
    assert "WRONG" in rendered and "RIGHT" in rendered
    assert "{" not in rendered.replace("{ /* … */ }", "")
    assert "does not compile" in _COMPILE_FIX_HEAD.format(
        rel_path="src/x.rs", directive="", history="", err="E0308")
