"""Cover make_failed_attempt_body: the failure-kind labels and the empty-body /
empty-error branches.

The label is the lesson every later prompt for the fn reads out of the file.
Run `2585062-494e630e` (2026-09-08) wrote `did not compile — do not repeat
this approach` over the textbook `Own::new` one-liner after the body had
COMPILED and the test process aborted in a still-stubbed drop-path fn; the
next prompts for `ptr.rs` carried the correct body as the thing not to write.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_rules import (
    FAILED_ATTEMPT_SENTINEL,
    AttemptFailure,
    is_unimplemented_body,
    make_failed_attempt_body,
)

_OWN_NEW = "Self { ptr: unsafe { core::ptr::NonNull::new_unchecked(Box::into_raw(ptr)) } }"


def test_failed_body_with_content() -> None:
    body = make_failed_attempt_body("let x = 1;", "error[E0308]: mismatch", 2,
                                    failure=AttemptFailure.COMPILE)
    assert FAILED_ATTEMPT_SENTINEL in body
    assert "//   let x = 1;" in body
    assert "//   error[E0308]: mismatch" in body
    assert is_unimplemented_body(body)  # ends in unimplemented!()
    assert "did not compile — do not repeat this approach" in body
    assert "CARGO ERROR" in body


def test_failed_body_empty_inputs_use_placeholders() -> None:
    body = make_failed_attempt_body("", "", 1, failure=AttemptFailure.COMPILE)
    assert "(no body captured)" in body
    assert "(no error captured)" in body


def test_an_abort_through_stubs_never_calls_the_body_wrong() -> None:
    # The live `ptr.rs::new` case: compiled, aborted through the still-
    # stubbed `vtable()`. The comment names the stub sites and says the body
    # was not shown wrong; "did not compile" and "do not repeat" appear
    # nowhere.
    body = make_failed_attempt_body(
        _OWN_NEW, "Your body ABORTED the test process. …", 2,
        failure=AttemptFailure.ABORT,
        stub_sites=[("src/error.rs", 669), ("src/error.rs", 553)])
    assert is_unimplemented_body(body)
    assert f"//   {_OWN_NEW}" in body
    assert "did not compile" not in body and "do not repeat" not in body
    assert "src/error.rs:553, src/error.rs:669" in body
    assert "NOT shown wrong" in body
    assert "ABORT EVIDENCE" in body


def test_an_abort_after_the_units_own_panic_names_the_first_panic() -> None:
    body = make_failed_attempt_body(
        "boom()", "…", 1, failure=AttemptFailure.ABORT,
        stub_sites=[("src/error.rs", 669)],
        own_panics=["tests/t.rs:9:5: assertion failed: boom"])
    assert "own panic came first" in body
    assert "tests/t.rs:9:5: assertion failed: boom" in body
    assert "src/error.rs:669" in body
    assert "did not compile" not in body


def test_an_abort_with_no_panic_located_is_labelled_undefined_behaviour() -> None:
    body = make_failed_attempt_body("bad()", "…", 1, failure=AttemptFailure.ABORT)
    assert "no panic located" in body and "undefined behaviour" in body
    assert "did not compile" not in body


def test_a_test_failure_is_labelled_to_correct_not_to_avoid() -> None:
    body = make_failed_attempt_body("x()", "assertion failed", 3,
                                    failure=AttemptFailure.TEST)
    assert "COMPILED but its attributed tests still failed" in body
    assert "do not avoid it" in body
    assert "TEST FAILURE" in body
    assert "did not compile" not in body
