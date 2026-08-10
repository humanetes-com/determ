"""The typed verdict and the implicating-evidence rule.

The pre-fix control for the rule's flagship consumer (the regression gate's
crash-only branch) was RUN against the pre-change tree: the same
crash-only/no-stub-evidence scenario answered REVERT with summary
`binary crashed: ['unittests src/lib.rs'] (net -2 test(s))` — the exact class
of the four live `net -6` reverts of 2026-08-10 — and answers KEEP under the
shipped gate (pinned in test_cargo_runner.py)."""
from __future__ import annotations

import json

from pipeline_core.domain.verdict import (
    Disposition,
    GateOutcome,
    NegativeEvidence,
    ValidityVector,
    VerdictRecord,
    grade_negative_evidence,
)


def test_newly_red_in_completed_binary_is_negative() -> None:
    e = NegativeEvidence(own_newly_red=frozenset({"t1"}),
                         binary_completed=True)
    assert grade_negative_evidence(e) is GateOutcome.NEGATIVE


def test_crashed_binary_is_unmeasurable_whatever_else_holds() -> None:
    e = NegativeEvidence(own_newly_red=frozenset({"t1"}),
                         binary_completed=False)
    assert grade_negative_evidence(e) is GateOutcome.UNMEASURABLE


def test_blame_wholly_outside_is_unmeasurable() -> None:
    e = NegativeEvidence(own_newly_red=frozenset({"t1"}),
                         binary_completed=True,
                         blame_wholly_outside=True)
    assert grade_negative_evidence(e) is GateOutcome.UNMEASURABLE


def test_blame_inside_or_absent_does_not_exonerate() -> None:
    for blame in (False, None):
        e = NegativeEvidence(own_newly_red=frozenset({"t1"}),
                             binary_completed=True,
                             blame_wholly_outside=blame)
        assert grade_negative_evidence(e) is GateOutcome.NEGATIVE


def test_all_newly_red_blocked_is_unmeasurable() -> None:
    e = NegativeEvidence(own_newly_red=frozenset({"t1", "t2"}),
                         blocked_tests=frozenset({"t1", "t2", "t3"}))
    assert grade_negative_evidence(e) is GateOutcome.UNMEASURABLE


def test_one_unblocked_newly_red_suffices() -> None:
    e = NegativeEvidence(own_newly_red=frozenset({"t1", "t2"}),
                         blocked_tests=frozenset({"t1"}))
    assert grade_negative_evidence(e) is GateOutcome.NEGATIVE


def test_no_newly_red_at_all_is_unmeasurable() -> None:
    # Still-failing-at-baseline is the baseline's fact, not the attempt's.
    assert (grade_negative_evidence(NegativeEvidence())
            is GateOutcome.UNMEASURABLE)


def test_record_round_trips_through_json() -> None:
    rec = VerdictRecord(
        unit="src/error.rs::impl Error::new", route="impl", attempt=3,
        disposition=Disposition.REVERT, reason="regressed sibling",
        outcome=GateOutcome.NEGATIVE,
        validity=ValidityVector(binary_completed=True, baseline_epoch=2),
        evidence_keys=("test_a", "test_b"), model_calls=3,
        prompt_chars=1200)
    row = json.loads(json.dumps(rec.to_json_dict()))
    assert row["disposition"] == "revert"
    assert row["outcome"] == "negative"
    assert row["validity"]["binary_completed"] is True
    assert row["validity"]["baseline_epoch"] == 2
    assert row["evidence_keys"] == ["test_a", "test_b"]


def test_record_defaults_are_total() -> None:
    # Every field beyond the four required ones has a default, and the JSON
    # form never fails — the ledger must be writable from any site with
    # whatever the site knows.
    rec = VerdictRecord(unit="u", route="per-fn", attempt=0,
                        disposition=Disposition.CAP_REFUSAL, reason="")
    row = rec.to_json_dict()
    assert row["outcome"] is None
    assert row["validity"] == {
        "binary_completed": None, "oracle_intact": None,
        "tests_blocked": None, "blame_outside_unit": None,
        "baseline_epoch": None}


def test_every_disposition_the_audit_named_exists() -> None:
    assert {d.value for d in Disposition} == {
        "accept", "keep_deferred", "reject", "revert", "salvage_refusal",
        "conviction", "exhaustion", "cap_refusal"}
