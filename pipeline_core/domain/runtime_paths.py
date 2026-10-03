"""Where the pipeline's files outside the workspace live: the one owner of `/tmp`.

Two kinds of runtime file exist, and they need different homes.

**Control channels** are machine-wide on purpose. A graceful-stop request, the
power-off and auto-operator toggles, the operator pass lock, the run registry
and the vLLM container id are written by one process (the monitor, a shell
launcher, the docker script) and read by another, so they live at a fixed path
both sides know. :func:`control_path` is that path for in-process callers:
``<control dir>/<channel>``, the control dir being ``$DETERM_CONTROL_DIR`` or
``/tmp``. The shell launchers (`lib/graceful_stop.sh`, `operator_run.sh`,
`docker_vllm.sh`, ...) name the same files under `/tmp` literally, so the
override is an isolation seam for in-process callers (the test suite points it
at a per-test dir), not a deployment knob: set in `.env`, it would split the
Python side from the shell side.

**Per-run artefacts** (coverage tool outputs, saved stream logs) belong to one
workspace and must not live here at all. They were `/tmp/<tool>_<module>`
files, which the test suite wrote and deleted under a live run: a `check.sh`
emptied the live run's coverage overview and left a test's `microcrate` row in
its place (2026-10-03, run `69851-f9e61bdd`). They now live under the
workspace's ignored `target/` (`workspace_layout.COVERAGE_SCRATCH_DIR`,
`workspace_layout.STREAM_LOG_DIR`), where a test's workspace is its own temp
dir by construction.

`tools/check_no_tmp_literals.py` (a `check.sh` gate) refuses a `/tmp` string
literal anywhere in the pipeline's Python except this module, so a new runtime
file has to be named here and choose one of the two homes.

Paths resolve at CALL time: a caller that bound one at import (a module
constant, a default argument) would keep the real `/tmp` path after the test
suite redirected the control dir.
"""
from __future__ import annotations

import os
from pathlib import Path

CONTROL_DIR_ENV = "DETERM_CONTROL_DIR"
_DEFAULT_CONTROL_DIR = "/tmp"

# The control channels, by file name under the control dir.
GRACEFUL_STOP = "pipeline_graceful_stop"
SHUTDOWN_ON_FINISH = "pipeline_shutdown_on_finish"
AUTOMODE = "pipeline_automode"
OPERATOR_PASS_LOCK = "pipeline_operator_pass.lock"
OPERATOR_LAST_FIRE = "pipeline_operator_last_fire"
RUN_REGISTRY = "determ_pipelines.json"
VLLM_CONTAINER_ID = "vllm_container.id"


def control_dir() -> Path:
    """The directory every control channel lives in (read on every call)."""
    return Path(os.environ.get(CONTROL_DIR_ENV) or _DEFAULT_CONTROL_DIR)


def control_path(channel: str) -> Path:
    """The file of one control channel, e.g. ``control_path(GRACEFUL_STOP)``."""
    return control_dir() / channel


def run_registry_path() -> Path:
    """The run registry: ``$PIPELINE_RUN_REGISTRY`` when set (its own,
    older override), else the registry channel under the control dir."""
    return Path(os.environ.get("PIPELINE_RUN_REGISTRY") or control_path(RUN_REGISTRY))
