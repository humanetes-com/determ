"""Shared pytest fixtures.

All tests in this suite are OFFLINE (scripted model + cargo seams). No test
should ever incur a real wall-clock sleep: the only `time.sleep` calls reachable
from the engine are network-retry backoffs (`adapters/ask_client.py`,
`adapters/llm_transports.py`, `retry.py`) that fire when a model/transport call
hits a real (absent) server. In a scripted test that backoff is pure dead time —
a single such path once stalled a `check.sh` run for ~25 minutes. Neutralising
`time.sleep` keeps retry *counts* (and thus asserted behaviour) identical while
removing the wall-clock cost. `--timeout` in check.sh is the backstop.
"""
from __future__ import annotations

import time
from collections.abc import Generator

import pytest

from pipeline_core.domain import graceful_stop


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op `time.sleep` for every test — retry loops run instantly.

    Behaviour-preserving: loop iteration counts are unchanged; only the wall
    clock is. No daemon `while True: sleep(...)` loops are started by the unit
    suite, so this cannot spin.
    """
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _isolate_graceful_stop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Generator[None, None, None]:
    """Tests that exercise the real, composition-wired fix loop (e2e tests
    calling `pipeline._phase_c_fill_impl_blocks`/etc directly, not a mocked
    deps object) go through the ACTUAL `check_graceful_stop`, which by
    default reads a hardcoded OS-level path (`/tmp/pipeline_graceful_stop`)
    — ambient state shared with whatever ELSE is running on the machine. A
    real pipeline run's own graceful-stop request (e.g. an operator
    restarting it mid-session) can leave that sentinel file briefly present
    while `check.sh` happens to run, causing every such test to
    self-terminate with `SystemExit: 0` — a real, reproduced failure with
    nothing wrong in the code under test.

    Two prior versions of this fixture tried to coordinate around the SHARED
    real path (delete-then-restore, then hide-and-restore with a
    process-unique temp name) and both failed live: `check.sh` runs pytest
    under xdist, so MULTIPLE WORKER PROCESSES run this autouse fixture
    concurrently, over and over (once per test, ~1500 times) — any scheme
    that touches the real shared path repeatedly across racing processes
    eventually loses the race and silently drops a real pending sentinel
    (reproduced live both times). The actually-robust fix is to never touch
    the shared path AT ALL: redirect `SENTINEL_PATH` to this test's own
    `tmp_path` (pytest's built-in per-test-unique temp dir) for the
    duration of the test. No other test or process can ever see or write to
    it, so there is no shared state left to race on, and the real
    production sentinel is never read, written, or renamed by any test."""
    monkeypatch.setattr(graceful_stop, "SENTINEL_PATH",
                        tmp_path / "graceful-stop-sentinel-unused-by-real-pipeline")
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_auto_operator(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Auto-operator's sentinel is the same ambient-`/tmp`-state hazard as the
    graceful-stop sentinel above (a live pipeline's real /tmp/pipeline_automode
    could be present while check.sh runs) — but the stakes here are higher: a
    fired episode spawns a REAL, billed, unattended `claude -p` operator pass
    via operator_run.sh. Redirect every /tmp path it touches to per-test tmp
    ones, force the `.env` AUTO_DEFAULT fallback off (`.env` sets it to 1 for
    real runs), reset the fire/re-arm state machine, and stub the launch itself
    so no test can ever spawn the real subprocess even if a future test
    mistakenly drives stall past the trigger with the mode enabled. Redirection
    only works because the module reads these as globals at call time: as
    default ARGUMENT values they would bind at def time and monkeypatching the
    module attribute would silently be a no-op."""
    import pipeline_core.adapters.auto_operator as _auto_operator
    from pipeline_core.domain.auto_operator import AutoOperator

    def _forbid_real_launch() -> None:
        raise AssertionError(
            "test attempted to launch the real operator_run.sh subprocess")

    for _attr, _name in (("SENTINEL_PATH", "automode-sentinel"),
                         ("PASS_LOCK_PATH", "operator-pass.lock"),
                         ("LAST_FIRE_PATH", "operator-last-fire")):
        monkeypatch.setattr(_auto_operator, _attr,
                            tmp_path / f"{_name}-unused-by-real-pipeline")
    monkeypatch.setenv("AUTO_DEFAULT", "0")
    monkeypatch.setattr(
        _auto_operator, "_OP", AutoOperator(trigger=_auto_operator._OP.trigger))
    monkeypatch.setattr(_auto_operator, "_launch_operator_pass", _forbid_real_launch)


@pytest.fixture(autouse=True)
def _isolate_observability_sinks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Tests must not write to the PRODUCTION observability sinks
    (logs/pipeline_guards.log, logs/prompts/, logs/events.jsonl): synthetic
    600K-char guard-test prompts landed in the real guards log and prompt
    store, and were then mis-read as live-run evidence during monitoring
    (same ambient-state family as the graceful-stop sentinel incident).
    Redirect the module-level paths to pytest's per-test tmp_path; the real
    pipeline process is unaffected (it re-reads its own module state)."""
    import pipeline
    monkeypatch.setattr(pipeline, "_GUARD_LOG", str(tmp_path / "guards.log"))
    monkeypatch.setattr(pipeline, "_PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(pipeline, "_EVENTS_LOG", str(tmp_path / "events.jsonl"))


@pytest.fixture(autouse=True)
def _turns_through_scripted_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fill route now talks to the model through a transcript
    (`AskClient.ask_turns` → `model_interact.ask_turns`), and the offline
    suite scripts the single-prompt transport (`pipeline._ask_raw`). Route
    the transcript transport to whatever `_ask_raw` is at call time, handing
    it the LAST user turn as the prompt and the system turn as `system` —
    the text a scripted fake sees for the opening turn is the prompt it saw
    before, and a retry's is the retry turn. A test that scripts
    `model_interact.ask_turns` itself overrides this; nothing reaches a real
    server either way."""
    import model_interact

    def _via_raw(messages, **kwargs):
        import pipeline
        system = next((str(m.get("content") or "") for m in messages
                       if m.get("role") == "system"), None)
        last = next((str(m.get("content") or "") for m in reversed(messages)
                     if m.get("role") == "user"), "")
        if not kwargs.get("followup_think"):
            # `AskClient.ask` forwards it only when set; so does this.
            kwargs.pop("followup_think", None)
        return pipeline._ask_raw(last, system, **kwargs)

    monkeypatch.setattr(model_interact, "ask_turns", _via_raw)
