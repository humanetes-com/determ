"""A reading of a tree whose binaries abort costs one reading, not one per
test per world.

Run `630503-f5ca6540` (2026-11-09, cluster `stable`): once a keep made the
crate's error value constructible while its destructor path was still
stubbed, every failing test aborted its binary, and every reading re-ran
those tests one per process, one after another, then did the same in the
second world. Per-thread cargo spacing went from a median of 3.1 s (fill 1)
to 131.3 s (fill 2); fill 2 spent 530.8 of its 634.9 unit-minutes outside
the model.

Pinned here, on a fake cargo port and invented shapes:
- the isolation pool gives the same capture bytes as the serial loop, in
  less wall time, at `PHASE_C_ISOLATION_WORKERS=4`; `1` is the serial
  negative control;
- a binary whose last test cannot start before the deadline keeps its
  shared section at either width;
- the worlds of an every-world run overlap and join in the same order;
  a one-worker pool (the old sequence) takes the summed time;
- a keep reading of the bound tree reuses a completed capture of the same
  bytes and re-runs on changed bytes; a timeout is never reused; an unbound
  tree is measured every call."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline_core.application.reconstruct import cargo_runner as cr
from pipeline_core.domain.test_results import parse_binary_results


def _sleep(seconds: float) -> None:
    # A real wait: `tests/conftest.py` makes `time.sleep` a no-op.
    threading.Event().wait(seconds)


@dataclass(frozen=True)
class _Proc:
    returncode: int
    stdout: str
    stderr: str
    failure: object = None


class _SlowAbortCargo:
    """One binary of ``n`` tests aborts in the shared process. Each single
    test run sleeps longer the EARLIER it is listed, so in a pool they
    finish in reverse order; a run whose timeout is shorter than its sleep
    returns the runner's timeout."""

    def __init__(self, exe: Path, n: int, step: float) -> None:
        self.exe, self.n, self.step = str(exe), n, step
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str],
            timeout: int = 180) -> _Proc:
        if cmd.startswith("cargo test"):
            return _Proc(101, f"\nrunning {self.n} tests\ntest t0 ... ",
                         f"     Running tests/t.rs ({self.exe})\n"
                         "thread 't0' panicked at src/s.rs:4:5:\nnot implemented\n")
        if "--list" in cmd:
            return _Proc(0, "".join(f"t{i}: test\n" for i in range(self.n)), "")
        name = cmd.rsplit(" ", 1)[-1]
        i = int(name[1:])
        delay = self.step * (self.n - i)
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if delay > timeout:
                _sleep(timeout)
                return _Proc(-1, f"\nrunning 1 test\ntest {name} ... ", "")
            _sleep(delay)
        finally:
            with self.lock:
                self.live -= 1
        if i % 2:
            return _Proc(0, f"\nrunning 1 test\ntest {name} ... ok\n\n"
                         "test result: ok. 1 passed; 0 failed; 0 ignored; "
                         "0 measured; 0 filtered out\n", "")
        return _Proc(134, f"\nrunning 1 test\ntest {name} ... ",
                     f"thread '{name}' panicked at src/s.rs:4:5:\nnot implemented\n")


@pytest.fixture
def exe(tmp_path: Path) -> Path:
    p = tmp_path / "t-bin"
    p.write_text("")
    return p


def _timed(cargo: object, tmp_path: Path, timeout: int = 60) -> tuple[float, str]:
    t0 = time.monotonic()
    _rc, out = cr.run_cargo(cargo, "cargo test --no-fail-fast", tmp_path, {},  # type: ignore[arg-type]
                            timeout=timeout)
    return time.monotonic() - t0, out


def test_the_pool_writes_the_serial_bytes_in_less_time(
        exe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    n, step = 4, 0.15
    total = step * sum(range(1, n + 1))  # 1.5 s summed
    monkeypatch.setenv("PHASE_C_ISOLATION_WORKERS", "1")
    serial_cargo = _SlowAbortCargo(exe, n, step)
    serial_wall, serial = _timed(serial_cargo, tmp_path)
    monkeypatch.setenv("PHASE_C_ISOLATION_WORKERS", "4")
    pooled_cargo = _SlowAbortCargo(exe, n, step)
    pooled_wall, pooled = _timed(pooled_cargo, tmp_path)
    (b,) = parse_binary_results(pooled)
    assert b.completed and b.passed == {"t1", "t3"} and b.aborted == {"t0", "t2"}
    assert pooled == serial
    assert pooled_cargo.peak == 4 and pooled_wall < total * 0.6
    # NEGATIVE CONTROL: width 1 is the serial loop, one process at a time,
    # and takes the summed time.
    assert serial_cargo.peak == 1 and serial_wall >= total


@pytest.mark.parametrize("width", ["1", "2"])
def test_a_binary_whose_last_test_cannot_start_keeps_its_shared_section(
        exe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        width: str) -> None:
    monkeypatch.setenv("PHASE_C_TEST_ISOLATION", "1")
    monkeypatch.setenv("PHASE_C_ISOLATION_WORKERS", width)
    # Six tests of 0.35-2.1 s against a 1 s deadline: at either width some
    # test is still queued when the deadline passes.
    _wall, out = _timed(_SlowAbortCargo(exe, 6, 0.35), tmp_path, timeout=1)
    (b,) = parse_binary_results(out)
    assert not b.completed


# --- the worlds ---------------------------------------------------------------


def test_the_worlds_of_one_reading_overlap_and_join_in_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_every_world_test_run import (
        ALT_RUN,
        BASE_RUN,
        _Cargo,
        _loop_env,
        _runner,
    )

    class _Sleepy(_Cargo):
        def run(self, cmd, cwd, *, env, timeout=180):  # type: ignore[no-untyped-def]
            _sleep(0.4)
            return super().run(cmd, cwd, env=env, timeout=timeout)

    by_world = {"base": (0, BASE_RUN), "alt": (101, ALT_RUN)}
    t0 = time.monotonic()
    rc, text = _runner(_Sleepy(by_world)).run(
        "cargo test --no-fail-fast", tmp_path, _loop_env())
    overlapped = time.monotonic() - t0
    assert rc == 101 and overlapped < 0.75

    # NEGATIVE CONTROL: a one-worker pool is the old sequence of worlds.
    from pipeline_core.threads import ContextPool as real_pool
    monkeypatch.setattr(cr, "ContextPool",
                        lambda max_workers, **kw: real_pool(max_workers=1, **kw))
    t0 = time.monotonic()
    rc_s, text_s = _runner(_Sleepy(by_world)).run(
        "cargo test --no-fail-fast", tmp_path, _loop_env())
    assert time.monotonic() - t0 >= 0.8
    assert (rc_s, text_s) == (rc, text)


# --- the measurement cache ------------------------------------------------------


_GREEN = "test result: ok. 5 passed; 0 failed; 0 ignored"


class _Counting:
    def __init__(self, rc: int = 0, out: str = _GREEN) -> None:
        self.calls = 0
        self.rc, self.out = rc, out

    def __call__(self, cmd: str, cwd: Path, env: dict[str, str], *,
                 timeout: int = 120) -> tuple[int, str]:
        self.calls += 1
        return self.rc, self.out


@pytest.fixture
def tree(tmp_path: Path):
    root = tmp_path / "cluster"
    (root / "src").mkdir(parents=True)
    (root / "src" / "lib.rs").write_text("fn f() {}\n")
    (root / "target").mkdir()
    cr.bind_measure_cache(root)
    yield root
    cr.bind_measure_cache(None)


def test_the_same_bytes_are_measured_once(tree: Path) -> None:
    run = _Counting()
    a = cr.measure_test_state(run, "cargo test", tree, {})
    (tree / "target" / "noise").write_text("build output is not the tree")
    b = cr.measure_test_state(run, "cargo test", tree, {})
    assert run.calls == 1 and a is not None and b is not None
    assert a.pass_count == b.pass_count == 5


def test_changed_bytes_or_env_are_measured_again(tree: Path) -> None:
    run = _Counting()
    cr.measure_test_state(run, "cargo test", tree, {})
    (tree / "src" / "lib.rs").write_text("fn f() { 1; }\n")
    cr.measure_test_state(run, "cargo test", tree, {})
    cr.measure_test_state(run, "cargo test", tree, {"RUSTFLAGS": "--cfg x"})
    assert run.calls == 3


def test_a_timeout_is_never_reused(tree: Path) -> None:
    run = _Counting(-1, "")
    assert cr.measure_test_state(run, "cargo test", tree, {}) is None
    assert cr.measure_test_state(run, "cargo test", tree, {}) is None
    assert run.calls == 2


def test_negative_control_an_unbound_tree_is_measured_every_call(
        tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cr.bind_measure_cache(None)
    run = _Counting()
    cr.measure_test_state(run, "cargo test", tree, {})
    cr.measure_test_state(run, "cargo test", tree, {})
    assert run.calls == 2
    cr.bind_measure_cache(tree)
    monkeypatch.setenv("PHASE_C_MEASURE_CACHE", "0")
    cr.measure_test_state(run, "cargo test", tree, {})
    cr.measure_test_state(run, "cargo test", tree, {})
    assert run.calls == 4


def test_the_ground_after_a_keep_reads_the_judgment_s_capture(tree: Path) -> None:
    """The fill's ground is read after each keep, on the bytes the keep's
    judgment just measured (`phase_c_tdd._ground_now`, review 2026-11-09)."""
    run = _Counting()
    cr.measure_test_state(run, "cargo test", tree, {}, timeout=120)
    rc, out = cr.measured_capture(run, "cargo test", tree, {}, 120)
    assert run.calls == 1 and (rc, out) == (0, _GREEN)


def test_a_stored_run_longer_than_the_asked_timeout_is_not_served(
        tree: Path) -> None:
    class _Slow(_Counting):
        def __call__(self, cmd, cwd, env, *, timeout=120):  # type: ignore[no-untyped-def]
            _sleep(1.1)
            return super().__call__(cmd, cwd, env, timeout=timeout)

    run = _Slow()
    cr.measured_capture(run, "cargo test", tree, {}, 120)
    cr.measured_capture(run, "cargo test", tree, {}, 1)
    assert run.calls == 2
    cr.measured_capture(run, "cargo test", tree, {}, 120)
    assert run.calls == 2
