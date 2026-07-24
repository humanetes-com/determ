"""Pins for the auto-operator adapter (sentinel I/O + `.env` fallback + the
detached-launch call, mirroring `pipeline_core.adapters.power`'s
SHUTDOWN_ON_FINISH sentinel authority). The autouse `_isolate_auto_operator`
fixture in conftest.py already redirects SENTINEL_PATH to a per-test tmp path,
forces AUTO_DEFAULT=0, and stubs the real launch — tests here further
monkeypatch as needed to exercise both branches without ever touching the
real /tmp/pipeline_automode or spawning operator_run.sh."""
from __future__ import annotations

import fcntl
import time

import pipeline_core.adapters.auto_operator as auto_operator


def test_sentinel_presence_overrides_env_default(monkeypatch, tmp_path) -> None:
    sentinel = tmp_path / "sentinel"
    monkeypatch.setenv("AUTO_DEFAULT", "0")
    assert not auto_operator.auto_operator_enabled(sentinel)   # no file, env off
    monkeypatch.setenv("AUTO_DEFAULT", "1")
    assert auto_operator.auto_operator_enabled(sentinel)       # no file, env on
    monkeypatch.setenv("AUTO_DEFAULT", "0")
    sentinel.touch()
    assert auto_operator.auto_operator_enabled(sentinel)       # file present wins


def test_seed_creates_sentinel_only_when_default_on_and_absent(
        monkeypatch, tmp_path) -> None:
    sentinel = tmp_path / "sentinel"
    monkeypatch.setenv("AUTO_DEFAULT", "0")
    auto_operator.seed_auto_operator_default(sentinel)
    assert not sentinel.exists()                                # default off — no seed

    monkeypatch.setenv("AUTO_DEFAULT", "1")
    auto_operator.seed_auto_operator_default(sentinel)
    assert sentinel.exists()                                    # default on — seeded

    sentinel.unlink()                                           # user disabled mid-run
    auto_operator.seed_auto_operator_default(sentinel)
    # Each pipeline start re-applies AUTO_DEFAULT fresh — seeding is a
    # startup-only action, so re-invoking here models the NEXT start, which
    # legitimately re-arms per the (still 1) .env default.
    assert sentinel.exists()

    # ...and fails OFF in the other direction: the sentinel lives in /tmp and
    # outlives every pipeline restart, so a default-off start must clear a
    # stale one rather than leave a billed pass armed forever.
    monkeypatch.setenv("AUTO_DEFAULT", "0")
    auto_operator.seed_auto_operator_default(sentinel)
    assert not sentinel.exists()


def test_cooldown_survives_the_restart_a_pass_performs(monkeypatch, tmp_path) -> None:
    """The in-process `armed` flag dies with the pipeline that an operator pass
    kills; only the on-disk timestamp stops the restarted process from firing
    the next pass the moment the same plateau reappears."""
    last_fire = tmp_path / "last-fire"
    monkeypatch.setattr(auto_operator, "LAST_FIRE_PATH", last_fire)
    monkeypatch.setenv("AUTO_COOLDOWN_SEC", "7200")

    assert not auto_operator._cooling_down()                    # no file → free
    auto_operator._record_fire()
    assert auto_operator._cooling_down()                        # just fired
    assert not auto_operator._cooling_down(now=time.time() + 7201)

    last_fire.write_text("not-a-timestamp\n", encoding="utf-8")
    assert not auto_operator._cooling_down()                    # unreadable → free


def test_pass_lock_blocks_a_second_concurrent_pass(tmp_path) -> None:
    lock = tmp_path / "pass.lock"
    assert not auto_operator._pass_running(lock)                # nothing holds it
    with open(lock, "w") as held:                               # a live pass
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert auto_operator._pass_running(lock)
    assert not auto_operator._pass_running(lock)                # released on exit


def test_check_auto_operator_fires_once_and_logs(monkeypatch, tmp_path) -> None:
    sentinel = tmp_path / "sentinel"
    sentinel.touch()                                            # mode ON
    monkeypatch.setenv("AUTO_COOLDOWN_SEC", "0")                # isolate the in-process guard
    monkeypatch.setattr(auto_operator, "_OP",
                        type(auto_operator._OP)(trigger=2))

    launched: list[str] = []

    class _Proc:
        def poll(self) -> None:
            return None

    def _fake_launch() -> _Proc:
        launched.append("fired")
        return _Proc()

    monkeypatch.setattr(auto_operator, "_launch_operator_pass", _fake_launch)

    logged: list[str] = []
    auto_operator.check_auto_operator(logged.append, 1, "iter 1", sentinel=sentinel)
    assert launched == [] and logged == []                     # below trigger

    auto_operator.check_auto_operator(logged.append, 2, "iter 2", sentinel=sentinel)
    assert launched == ["fired"]
    assert len(logged) == 1 and "iter 2" in logged[0]

    auto_operator.check_auto_operator(logged.append, 2, "iter 3", sentinel=sentinel)
    assert launched == ["fired"]                                # no re-fire while running

    # Same stall, but a FRESH state machine — this is the pipeline the pass
    # just killed and restarted. The in-process guard is gone; the on-disk
    # cooldown is the only thing left holding the episode closed.
    monkeypatch.setattr(auto_operator, "_OP", type(auto_operator._OP)(trigger=2))
    monkeypatch.setenv("AUTO_COOLDOWN_SEC", "7200")
    auto_operator.check_auto_operator(logged.append, 2, "iter 1 (restarted)",
                                      sentinel=sentinel)
    assert launched == ["fired"]


def test_check_auto_operator_never_fires_when_disabled(monkeypatch, tmp_path) -> None:
    sentinel = tmp_path / "sentinel"                            # never created → OFF
    monkeypatch.setenv("AUTO_DEFAULT", "0")
    monkeypatch.setattr(auto_operator, "_OP",
                        type(auto_operator._OP)(trigger=1))

    def _fail_launch():
        raise AssertionError("must not launch while disabled")

    monkeypatch.setattr(auto_operator, "_launch_operator_pass", _fail_launch)
    auto_operator.check_auto_operator(lambda _m: None, 99, "iter", sentinel=sentinel)
