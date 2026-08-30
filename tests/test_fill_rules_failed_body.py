"""Cover make_failed_attempt_body empty-body / empty-error branches."""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_rules import (
    FAILED_ATTEMPT_SENTINEL,
    is_unimplemented_body,
    make_failed_attempt_body,
)


def test_failed_body_with_content() -> None:
    body = make_failed_attempt_body("let x = 1;", "error[E0308]: mismatch", 2)
    assert FAILED_ATTEMPT_SENTINEL in body
    assert "//   let x = 1;" in body
    assert "//   error[E0308]: mismatch" in body
    assert is_unimplemented_body(body)  # ends in unimplemented!()
    assert "did not compile — do not repeat this approach" in body


def test_failed_body_empty_inputs_use_placeholders() -> None:
    body = make_failed_attempt_body("", "", 1)
    assert "(no body captured)" in body
    assert "(no error captured)" in body
