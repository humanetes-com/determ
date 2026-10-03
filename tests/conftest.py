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

from pipeline_core.domain import graceful_stop, runtime_paths


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op `time.sleep` for every test — retry loops run instantly.

    Behaviour-preserving: loop iteration counts are unchanged; only the wall
    clock is. No daemon `while True: sleep(...)` loops are started by the unit
    suite, so this cannot spin.
    """
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _isolate_control_dir(monkeypatch: pytest.MonkeyPatch,
                         tmp_path_factory: pytest.TempPathFactory) -> None:
    """Every control channel (graceful stop, shutdown-on-finish, automode,
    operator pass lock and last fire, run registry, vLLM container id) resolves
    under `runtime_paths.control_dir()` at call time; point it at a dir of this
    test's own. One redirect covers every channel, including ones added later.

    The class this closes, recorded three times before it was fixed for good.
    Tests reached machine-wide `/tmp` files a live pipeline uses: the
    graceful-stop sentinel (a test's cleanup deleted a live run's pending stop
    request, and that run kept going 1.5 h; a live stop made 15 tests exit
    with `SystemExit: 0`), the auto-operator sentinel (stakes: a billed,
    unattended operator pass), then the coverage artefacts (`check.sh` emptied
    a live run's coverage overview on 2026-10-03). Each earlier fix
    monkeypatched one module constant, and none of them could reach a path
    built as a literal inside a function body. `xdist` workers race each
    other over a shared path, so the redirect is to a per-test dir, never a
    coordinated use of the real one. The root `conftest.py` also points the
    control dir at a temp dir for the whole process, before any import.
    `tools/check_no_tmp_literals.py` keeps new `/tmp` paths out of the
    pipeline's code."""
    monkeypatch.setenv(runtime_paths.CONTROL_DIR_ENV, str(tmp_path_factory.mktemp("control")))
    monkeypatch.delenv("PIPELINE_RUN_REGISTRY", raising=False)


@pytest.fixture(autouse=True)
def _reset_graceful_stop() -> Generator[None, None, None]:
    """The process-wide stop event outlives a test: a test that trips it would
    stop every later test in the same worker."""
    graceful_stop.reset_for_tests()
    yield
    graceful_stop.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_auto_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fired auto-operator episode spawns a REAL, billed, unattended
    `claude -p` operator pass via operator_run.sh. Its files are already
    isolated (`_isolate_control_dir`); here: force the `.env` AUTO_DEFAULT
    fallback off (`.env` sets it to 1 for real runs), reset the fire/re-arm
    state machine, and stub the launch itself so no test can ever spawn the
    real subprocess even if a future test mistakenly drives stall past the
    trigger with the mode enabled."""
    import pipeline_core.adapters.auto_operator as _auto_operator
    from pipeline_core.domain.auto_operator import AutoOperator

    def _forbid_real_launch() -> None:
        raise AssertionError(
            "test attempted to launch the real operator_run.sh subprocess")

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
