"""A regression verdict carries what the reddened tests PRINTED, not only
their names.

The instance (run `3003-93e9a9bf`, 2026-09-02, cluster `stable`): a Debug
formatter rewrite reddened ten previously-passing tests; the suite capture
held `left: "hello<disabled>"` against `right: "hello"`, the gate's detail
named the tests and dropped the values, the next prompt's prior-regression
section listed the names with a literal extraction (`they assert: "hello",
…`), and the next reply reddened the same ten. These tests pin the contract
at the writer (the gate attaches the outputs; the two renderers print them)
and keep the pre-fix rendering reachable as the negative control.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pipeline_core.application.reconstruct.block_evidence import (
    render_prior_regression,
)
from pipeline_core.domain.retro_gate import regressed_tests_from_excerpt
from pipeline_core.domain.test_report import regressed_test_outputs
from pipeline_core.domain.test_results import (
    REGRESSED_OUTPUT,
    TestStateReading,
    compare_readings,
    parse_binary_results,
    render_regression_detail,
)

_RES = "test result: FAILED. {p} passed; {f} failed; 0 ignored"

# One binary's capture in cargo's own layout: the verdict lines, then the
# `---- name stdout ----` section libtest prints for a failing assertion.
_AFTER = (
    "running 2 tests\n"
    "test display_delegates_for_anyhow_literal ... ok\n"
    "test debug_for_anyhow_literal ... FAILED\n"
    "\n"
    "failures:\n"
    "\n"
    "---- debug_for_anyhow_literal stdout ----\n"
    "\n"
    "thread 'debug_for_anyhow_literal' (1234) panicked at "
    "tests/wrapper_gen_6.rs:29:5:\n"
    "assertion `left == right` failed\n"
    '  left: "hello<disabled>"\n'
    ' right: "hello"\n'
    "note: run with `RUST_BACKTRACE=1` environment variable to display a "
    "backtrace\n"
    "\n"
    "\n"
    "failures:\n"
    "    debug_for_anyhow_literal\n"
    "\n" + _RES.format(p=1, f=1) + "\n"
)


def _reading(body: str) -> TestStateReading:
    return TestStateReading(parse_binary_results(body))


def _baseline_green() -> TestStateReading:
    return _reading(
        "running 2 tests\n"
        "test display_delegates_for_anyhow_literal ... ok\n"
        "test debug_for_anyhow_literal ... ok\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored\n")


def _run_cargo(rc: int, out: str):
    def fake(cmd: str, cwd: Path, env: dict[str, str], timeout: int = 0):
        return rc, out
    return fake


# --- the extractor ---------------------------------------------------------

def test_regressed_outputs_carry_left_and_right_by_bare_name() -> None:
    out = regressed_test_outputs(_AFTER, ["debug_for_anyhow_literal"])
    said = out["debug_for_anyhow_literal"]
    assert 'left: "hello<disabled>"' in said
    assert 'right: "hello"' in said
    assert "panicked at tests/wrapper_gen_6.rs:29:5" in said


def test_regressed_outputs_match_on_the_leaf_of_a_qualified_name() -> None:
    out = regressed_test_outputs(_AFTER, ["wrapper::debug_for_anyhow_literal"])
    assert set(out) == {"debug_for_anyhow_literal"}


def test_regressed_outputs_skip_names_with_no_captured_section() -> None:
    assert regressed_test_outputs(_AFTER, ["display_delegates_for_anyhow_literal"]) == {}
    assert regressed_test_outputs("", ["debug_for_anyhow_literal"]) == {}
    assert regressed_test_outputs(_AFTER, []) == {}


# --- the verdict detail ------------------------------------------------------

def test_detail_renders_what_the_reddened_test_printed() -> None:
    cmp = compare_readings(_baseline_green(), _reading(_AFTER))
    assert cmp.newly_red == frozenset({"debug_for_anyhow_literal"})
    enriched = replace(cmp, newly_red_output=regressed_test_outputs(
        _AFTER, cmp.newly_red))
    detail = render_regression_detail(enriched)
    assert 'left: "hello<disabled>"' in detail
    assert 'right: "hello"' in detail
    assert "debug_for_anyhow_literal said:" in detail
    # The historical head is intact: the name-list parser every retro record
    # is stamped through still reads the same names off it.
    assert regressed_tests_from_excerpt(detail) == ("debug_for_anyhow_literal",)


def test_detail_without_outputs_is_the_pre_fix_names_only_rendering() -> None:
    """Negative control: a comparison built without a capture renders the
    verdict the pipeline shipped before the outputs existed — names, counts,
    no `left:` line. This is the text the retro prompt `ccfeb0a9…` of run
    `3003-93e9a9bf` received."""
    cmp = compare_readings(_baseline_green(), _reading(_AFTER))
    detail = render_regression_detail(cmp)
    assert "REGRESSED previously-passing test(s): ['debug_for_anyhow_literal']" in detail
    assert "left:" not in detail
    assert "said:" not in detail


# --- the gate attaches them from its own capture ---------------------------

def test_gate_attaches_outputs_from_the_capture_it_compared() -> None:
    # Imported here: pytest would otherwise collect the gate's own name.
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    res = test_regression_gate(
        _run_cargo(101, _AFTER), "cargo test", Path("."), {},
        _baseline_green(), enabled=True, timeout=120, guard=lambda *_: None)
    assert res is not None
    cmp, detail = res
    assert cmp.newly_red_output["debug_for_anyhow_literal"].count("left:") == 1
    assert 'left: "hello<disabled>"' in detail


# --- the next prompt's prior-regression section ------------------------------

def test_prior_regression_section_shows_the_values_beside_the_names() -> None:
    outputs = regressed_test_outputs(_AFTER, ["debug_for_anyhow_literal"])
    section = render_prior_regression(
        ("debug_for_anyhow_literal",), 'assert_eq!(format!("{:?}", err), "hello")',
        outputs)
    assert "debug_for_anyhow_literal said:" in section
    assert 'left: "hello<disabled>"' in section
    assert "they assert:" in section


def test_prior_regression_section_without_outputs_is_names_and_pins_only() -> None:
    """Negative control: the section as it rendered on run `3003-93e9a9bf`
    (names plus the pins extraction, no values)."""
    section = render_prior_regression(
        ("debug_for_anyhow_literal",), '"hello", format!("{:#?}", …)')
    assert "debug_for_anyhow_literal" in section and "they assert:" in section
    assert "said:" not in section and "left:" not in section


def test_record_key_is_stable() -> None:
    # The pair record stores the outputs under this key; the next prompt
    # reads them back under it.
    assert REGRESSED_OUTPUT == "regressed_output"


def test_observations_merge_into_the_sweep_evidence_by_bare_name() -> None:
    from pipeline_core.domain.test_report import merge_observations
    evidence = {"mod::t_a": "panicked at x", "t_b": "panicked at y"}
    observed = {"t_a": 'observed at line 3: s = "v"', "t_c": "observed at line 9: z = 1", "t_b": ""}
    merged = merge_observations(evidence, observed)
    assert merged["mod::t_a"] == 'panicked at x\nobserved at line 3: s = "v"'
    assert merged["t_b"] == "panicked at y"
    assert merged["t_c"] == "observed at line 9: z = 1"


# --- what became of the attempt that reddened them ----------------------------

def test_the_label_says_what_became_of_the_attempt() -> None:
    """The section's first sentence is a claim about the tree. A revert that
    a later keep superseded is "an EARLIER attempt", and a keep that
    forgave losses is not a revert at all. The default renders the revert
    wording unchanged (the negative control: the only wording before the
    stamp)."""
    from pipeline_core.domain.test_results import RegressedAttempt
    names, pins = ("t_keep",), '"hello"'
    default = render_prior_regression(names, pins)
    assert default.startswith("the PREVIOUS attempt at these blocks was REVERTED")
    assert render_prior_regression(
        names, pins, attempt=RegressedAttempt.REVERTED) == default
    later = render_prior_regression(
        names, pins, {"t_keep": "left: 1\nright: 2"},
        RegressedAttempt.REVERTED_THEN_KEPT)
    assert later.startswith("an EARLIER attempt") and "was KEPT" in later
    assert "PREVIOUS attempt" not in later
    assert "that earlier attempt reddened them" in later
    kept = render_prior_regression(
        names, pins, {"t_keep": "left: 1\nright: 2"}, RegressedAttempt.KEPT)
    assert "KEPT on a net gain" in kept and "FAIL on the tree now" in kept
    assert "the kept body produced" in kept and "REVERTED" not in kept


def test_a_record_without_the_stamp_reads_as_the_revert_that_wrote_it() -> None:
    from pipeline_core.domain.test_results import (
        REGRESSED_ATTEMPT,
        RegressedAttempt,
        regressed_attempt_of,
    )
    legacy = {"regressed": ["t_keep"], "last_failed_body": ""}
    assert regressed_attempt_of(legacy) is RegressedAttempt.REVERTED
    # The pair route sets the body on every revert and clears it on every
    # keep, so an empty body beside reddened tests is a keep after the revert.
    assert (regressed_attempt_of(legacy, keep_clears_failed_body=True)
            is RegressedAttempt.REVERTED_THEN_KEPT)
    assert (regressed_attempt_of({**legacy, "last_failed_body": "<FN/>"},
                                 keep_clears_failed_body=True)
            is RegressedAttempt.REVERTED)
    stamped = {**legacy, REGRESSED_ATTEMPT: "kept"}
    assert (regressed_attempt_of(stamped, keep_clears_failed_body=True)
            is RegressedAttempt.KEPT)


def test_a_conviction_stamps_its_reddened_tests_as_reverted() -> None:
    from pipeline_core.domain.retro_gate import (
        BlockedOnStubs,
        ConvictionEvidence,
        RetroProvenance,
        stamp_conviction,
    )
    from pipeline_core.domain.test_results import REGRESSED_ATTEMPT
    prov = RetroProvenance.current("c", "")
    ev = ConvictionEvidence(frozenset({"t_x"}))
    rec = stamp_conviction({"error_count": 0}, prov, ev,
                           BlockedOnStubs(frozenset()), regressed=("t_keep",))
    assert rec[REGRESSED_ATTEMPT] == "reverted"
    again = stamp_conviction({**rec, REGRESSED_ATTEMPT: "kept"}, prov, ev,
                             BlockedOnStubs(frozenset()), regressed=())
    assert REGRESSED_ATTEMPT not in again
