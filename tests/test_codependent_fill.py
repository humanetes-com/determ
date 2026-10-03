"""Regression: a per-fn fill that COMPILES
but whose attributed test fails because a SIBLING is still an unfilled stub must be
kept + deferred, not reverted/marked GENUINELY_HARD. The robust signal (immune to
first-panic-order attribution) is the failure TYPE: stub-panic vs assertion."""
from __future__ import annotations

import pipeline


def test_unimplemented_panic_is_codependent() -> None:
    detail = ("thread 'tests::add_and_div_compose' panicked at src/lib.rs:48:5:\n"
              "not implemented")
    assert pipeline._failure_hit_unfilled_stub(detail) is True


def test_todo_panic_is_codependent() -> None:
    assert pipeline._failure_hit_unfilled_stub("panicked: not yet implemented") is True


def test_assertion_mismatch_is_this_fns_fault() -> None:
    detail = ("thread 'tests::add_works' panicked at src/lib.rs:51:9:\n"
              "assertion `left == right` failed\n  left: 6\n right: 5")
    assert pipeline._failure_hit_unfilled_stub(detail) is False


def test_empty_detail_is_not_codependent() -> None:
    assert pipeline._failure_hit_unfilled_stub("") is False
    assert pipeline._failure_hit_unfilled_stub(None) is False  # type: ignore[arg-type]
