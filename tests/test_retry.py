"""Unit tests for pipeline_core.retry — with_retry control flow.

Covers the network-error backoff/re-raise branches and the failure-exhaustion
path. time.sleep is monkeypatched so the backoff branches don't actually wait.
"""
from __future__ import annotations

import pytest

from pipeline_core import retry


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(retry, "MAX_RETRIES", 3)
    monkeypatch.setattr(retry.time, "sleep", lambda *_: None)


def test_success_first_try():
    assert retry.with_retry(lambda: "ok") == "ok"


def test_all_attempts_fail_returns_false():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        return False

    assert retry.with_retry(always_fail) is False
    assert calls["n"] == 3  # MAX_RETRIES attempts consumed


def test_network_error_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")  # name matches network-error filter
        return "recovered"

    assert retry.with_retry(flaky) == "recovered"
    assert calls["n"] == 3


def test_non_network_exception_reraised():
    def boom():
        raise ValueError("fatal")

    with pytest.raises(ValueError, match="fatal"):
        retry.with_retry(boom)


def test_explicit_retries_override():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        return False

    assert retry.with_retry(always_fail, retries=1) is False
    assert calls["n"] == 1
