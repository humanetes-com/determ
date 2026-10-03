"""Pins for the outcome-behaviour determinism gate."""
from __future__ import annotations

from pipeline_core.domain.outcome_determinism import (
    is_deterministic,
    normalize_observable,
    strip_panic_location,
)


def test_strip_panic_location_keeps_payload_old_format():
    line = "thread 'main' panicked at 'boom', src/lib.rs:12:5"
    assert strip_panic_location(line) == "boom"


def test_strip_panic_location_drops_location_only_new_format():
    text = "thread 'main' panicked at src/lib.rs:12:5:\nboom"
    # location line dropped, message line kept
    assert strip_panic_location(text).strip() == "boom"


def test_two_runs_with_shifting_panic_location_are_deterministic():
    a = "thread 'main' panicked at 'boom', src/error.rs:80:1"
    b = "thread 'main' panicked at 'boom', src/error.rs:44:9"
    assert is_deterministic(a, b)


def test_genuinely_different_output_is_not_deterministic():
    assert not is_deterministic("Error: A", "Error: B")


def test_normalize_trims_trailing_whitespace():
    assert normalize_observable("hello   \nworld  ") == "hello\nworld"
