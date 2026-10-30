"""A reverted write's reddened tests carry the values their failed
assertions read, and those values survive into every later prompt.

Run `3808231-81b847f7` (2026-10-29, fill 3): a reverted pair reddened
thirteen tests; the first printed `assertion failed: dbg.contains("outer
ctx")` and nothing else, so the next prompt's "what they printed" slot named
the assertion and no value. The probe that renders `observed at line N:
expr = value` ran on the sweep tree and inside sessions, never on a write a
regression gate was about to revert. Claims: the gate asks the fill's bound
probe about exactly the reddened tests whose output named no value, while
the write is on the tree, and puts the lines in the comparison and the
detail; `said_lines` keeps those lines past its cap; the per-fn prompt
renders the stamp a reverted write left. Negative controls: no probe bound,
the output as before; a test that printed `left:` is not asked about.
Invented shape.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from pipeline_core.application.reconstruct import assert_probe_runner
from pipeline_core.application.reconstruct.block_evidence import (
    render_sibling_regression,
)
from pipeline_core.application.reconstruct.cargo_runner import (
    test_regression_gate as regression_gate,
)
from pipeline_core.domain.sibling_regression import stamped, union_of
from pipeline_core.domain.test_results import (
    TestStateReading,
    parse_binary_results,
    printed_no_operands,
    said_lines,
)
from pipeline_core.prompts.context import PerFnSections
from pipeline_core.prompts.per_fn import build_per_fn_prompt

BASE = ("    Running tests/a.rs (t/a-1)\n"
        "running 3 tests\ntest x ... ok\ntest y ... ok\ntest z ... ok\n"
        "test result: ok. 3 passed; 0 failed; 0 ignored\n")
AFTER = ("    Running tests/a.rs (t/a-1)\n"
         "running 3 tests\ntest x ... ok\ntest y ... FAILED\ntest z ... FAILED\n"
         "\nfailures:\n\n---- y stdout ----\n"
         "thread 'y' panicked at tests/a.rs:7:5:\n"
         "assertion failed: dbg.contains(\"outer\")\n"
         "\n---- z stdout ----\n"
         "thread 'z' panicked at tests/a.rs:12:5:\n"
         "assertion `left == right` failed\n  left: 1\n right: 2\n"
         "\nfailures:\n    y\n    z\n\n"
         "test result: FAILED. 1 passed; 2 failed; 0 ignored\n")
OBSERVED_Y = 'observed at line 7: dbg = "()"'


def _run_cargo(cmd: str, cwd: Path, env: dict[str, str], *,
               timeout: int = 180) -> tuple[int, str]:
    return 101, AFTER


def _gate() -> tuple[dict[str, str], str]:
    res = regression_gate(
        _run_cargo, "cargo test", Path("."), {},
        TestStateReading(parse_binary_results(BASE)), enabled=True,
        timeout=120, guard=lambda *_a: None)
    assert res is not None
    return dict(res[0].newly_red_output), res[1]


def test_the_gate_probes_the_reddened_tests_that_printed_no_value() -> None:
    asked: list[list[str]] = []

    def probe(names: Collection[str]) -> Mapping[str, str]:
        asked.append(sorted(names))
        return {"y": OBSERVED_Y}

    assert_probe_runner.bind(probe)
    try:
        outputs, detail = _gate()
    finally:
        assert_probe_runner.bind(None)
    assert asked == [["y"]]
    assert OBSERVED_Y in outputs["y"]
    assert OBSERVED_Y in detail
    assert "observed" not in outputs["z"]


def test_negative_control_no_probe_bound_leaves_the_output_as_before() -> None:
    outputs, detail = _gate()
    assert "dbg.contains" in outputs["y"]
    assert "observed at line" not in detail


def test_a_failing_probe_does_not_decide_the_gate() -> None:
    def probe(names: Collection[str]) -> Mapping[str, str]:
        raise RuntimeError("scratch crate did not build")

    assert_probe_runner.bind(probe)
    try:
        outputs, _detail = _gate()
    finally:
        assert_probe_runner.bind(None)
    assert "observed at line" not in outputs["y"]


def test_which_outputs_name_no_value() -> None:
    assert printed_no_operands('assertion failed: dbg.contains("outer")')
    assert not printed_no_operands("assertion `left == right` failed\n left: 1")
    assert not printed_no_operands(
        f'assertion failed: dbg.contains("outer")\n{OBSERVED_Y}')


def test_probe_lines_survive_the_cap_of_a_long_capture() -> None:
    # A test that failed in two worlds prints its panic twice; the probe
    # line appended after it used to fall past the eight-line cap.
    printed = "\n".join(f"line {i}" for i in range(12))
    kept = said_lines(f"{printed}\n{OBSERVED_Y}", max_lines=8)
    assert kept[-1] == OBSERVED_Y
    assert kept[:8] == [f"line {i}" for i in range(8)]


def test_the_per_fn_prompt_renders_what_a_reverted_write_broke() -> None:
    rec = stamped(None, tests=["y"],
                  outputs={"y": f'assertion failed: dbg.contains("outer")\n'
                                f"{OBSERVED_Y}"},
                  by="file src/show.rs")
    tests, outputs, writers = union_of((rec,))
    section = render_sibling_regression(tests, '"outer"', outputs, writers)
    assert OBSERVED_Y in section and "file src/show.rs" in section

    def _prompt(prior: str) -> str:
        return build_per_fn_prompt(
            "fn show() -> String { String::new() }\n", "show",
            "fn show() -> String", [], "show", "c", "src/show.rs",
            PerFnSections(macros="", notes="", container="", types="",
                          invariants="", siblings="", crate_idioms="",
                          bound_methods="", prior_regression=prior))

    assert "<PRIOR_REGRESSION>" in _prompt(section)
    assert OBSERVED_Y in _prompt(section)
    # Control: no stamp, no section.
    assert "<PRIOR_REGRESSION>" not in _prompt("")
