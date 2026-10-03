"""Tests for monitor.py graceful stop action (the 'g' key).

When the operator presses 'g' and confirms with 'y', monitor.py must only
create the graceful stop sentinel file without launching continue.sh or any
other process. Pressing 'g' when a graceful stop is already pending cancels
the request by removing the sentinel file. Pressing 'g' when the pipeline is
not running reports that the pipeline was not running without touching disk
or launching continue.sh.
"""
from __future__ import annotations

import contextlib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import monitor
from pipeline_core.domain import graceful_stop


@pytest.fixture(autouse=True)
def _reset_monitor_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Ensure clean monitor state and redirect sentinel path for each test."""
    sentinel = graceful_stop.sentinel_path()   # under this test's control dir

    monitor._confirm = None
    monitor._graceful_pending = False
    monitor._action_msg = ""
    with contextlib.suppress(FileNotFoundError):
        sentinel.unlink()

    yield

    monitor._confirm = None
    monitor._graceful_pending = False
    monitor._action_msg = ""
    with contextlib.suppress(FileNotFoundError):
        sentinel.unlink()


def test_confirmed_g_while_running_creates_sentinel_without_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmed 'g' while running must create the sentinel file and NOT launch continue.sh.

    Negative control: the prior implementation invoked _detach with
    ['bash', '.../continue.sh', '--graceful'], which caused unwanted reload
    attempts or stamp mismatch failures instead of a clean stop at the next
    checkpoint.
    """
    monkeypatch.setattr(monitor, "_pipeline_running", lambda: True)
    detached_calls: list[Any] = []
    monkeypatch.setattr(monitor, "_detach", lambda cmd: detached_calls.append(cmd))

    assert not graceful_stop.sentinel_path().exists()
    assert not monitor._graceful_pending
    assert monitor._confirm is None

    # Step 1: press 'g' -> enters confirm state
    monitor._run_action("g")
    assert monitor._confirm == "graceful"
    assert not monitor._graceful_pending
    assert not graceful_stop.sentinel_path().exists()
    assert detached_calls == []
    assert "confirm" in monitor._action_msg.lower()
    assert "exit" in monitor._action_msg.lower()
    assert "reload" not in monitor._action_msg.lower()

    # Step 2: press 'y' -> confirms graceful stop
    monitor._run_action("y")
    assert monitor._confirm is None
    assert monitor._graceful_pending is True
    assert graceful_stop.sentinel_path().exists()
    assert detached_calls == []  # Crucial: _detach / continue.sh is NOT called
    assert "exit" in monitor._action_msg.lower() or "stop" in monitor._action_msg.lower()
    assert "reload" not in monitor._action_msg.lower()
    assert "restart" not in monitor._action_msg.lower()


def test_g_while_not_running_does_not_detach_and_creates_no_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing 'g' when pipeline is not running must NOT launch continue.sh
    and must NOT leave a sentinel file on disk.
    """
    monkeypatch.setattr(monitor, "_pipeline_running", lambda: False)
    detached_calls: list[Any] = []
    monkeypatch.setattr(monitor, "_detach", lambda cmd: detached_calls.append(cmd))

    assert not graceful_stop.sentinel_path().exists()

    monitor._run_action("g")

    assert detached_calls == []
    assert not graceful_stop.sentinel_path().exists()
    assert not monitor._graceful_pending
    assert monitor._confirm is None
    assert "not running" in monitor._action_msg.lower()


def test_g_while_pending_cancels_and_removes_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing 'g' while a graceful stop is already pending removes the sentinel file
    and resets the pending state (cancel).
    """
    monkeypatch.setattr(monitor, "_pipeline_running", lambda: True)
    detached_calls: list[Any] = []
    monkeypatch.setattr(monitor, "_detach", lambda cmd: detached_calls.append(cmd))

    # Pre-condition: pending graceful stop with sentinel file on disk
    graceful_stop.sentinel_path().touch()
    monitor._graceful_pending = True
    monitor._confirm = None

    monitor._run_action("g")

    assert not graceful_stop.sentinel_path().exists()
    assert not monitor._graceful_pending
    assert monitor._confirm is None
    assert detached_calls == []
    assert "cancelled" in monitor._action_msg.lower()


def test_g_confirmation_cancelled_with_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing 'n' during confirmation cancels the graceful stop request."""
    monkeypatch.setattr(monitor, "_pipeline_running", lambda: True)
    detached_calls: list[Any] = []
    monkeypatch.setattr(monitor, "_detach", lambda cmd: detached_calls.append(cmd))

    monitor._run_action("g")
    assert monitor._confirm == "graceful"

    monitor._run_action("n")
    assert monitor._confirm is None
    assert not monitor._graceful_pending
    assert not graceful_stop.sentinel_path().exists()
    assert detached_calls == []
    assert "cancelled" in monitor._action_msg.lower()
