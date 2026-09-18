"""Legacy single-command TASK 3 path.

The single-shot test runner retained for pipelines without baked clusters
(non-skeleton backends, plain-cargo projects). Body relocated verbatim from
``pipeline.py::_task3_legacy_single``; the pure domain helpers
(:data:`OUTPUT_SEPARATOR`, :func:`parse_test_counts`, :class:`TestOutcome`) are
imported, while the pipeline-coupled collaborators (phase/emit/log telemetry,
cargo env, command parsing, prompt-content store, the inline-retry fix loop, and
file read/write) are injected via :class:`LegacyTestRunDeps`. No import of
pipeline.py; the composition-root shim binds the resident collaborators by name
per call so the e2e monkeypatch seams still intercept.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.test_results import OUTPUT_SEPARATOR, TestOutcome, parse_test_counts


@dataclass(frozen=True, slots=True)
class LegacyTestRunDeps:
    """Pipeline-coupled collaborators the legacy single-shot runner needs."""

    phase: Callable[..., AbstractContextManager[dict[str, Any]]]  # _phase factory
    emit: Callable[..., None]                    # _emit
    log: Callable[[str], None]                   # _log
    cargo_env: Callable[[], dict[str, str]]      # _cargo_env
    parse_cmd: Callable[[str], list[str]]        # _parse_cmd
    store_prompt_content: Callable[[str], Any]   # _store_prompt_content
    task3_inline_retry: Callable[..., tuple[bool, str]]  # _task3_inline_retry
    read: Callable[[Any], str]
    write: Callable[[Any, str], None]
    test_timeout: int


def run_legacy_single(
    project_dir: Path, test_command: str, iteration: Any, output_dir: Any,
    genesis_path: Any, deps: LegacyTestRunDeps,
) -> tuple[bool, str]:
    """Legacy single-shot path. Uses the discovered ``test_command`` and the
    lenient classifier. New skeleton-backend runs never hit this path."""
    output_file = Path(output_dir) / f"tests_output_{iteration}.txt"
    env = deps.cargo_env()
    _attempt = iteration if isinstance(iteration, int) else None

    with deps.phase("task", task="task3", attempt=_attempt,
                    data={"command": test_command, "iteration": iteration}) as done:
        deps.emit("test.start", attempt=_attempt, data={"command": test_command})
        _t_start = time.monotonic()
        try:
            result = subprocess.run(
                deps.parse_cmd(test_command),
                cwd=project_dir, capture_output=True, text=True,
                timeout=deps.test_timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            msg = f"TIMEOUT: tests exceeded {deps.test_timeout} seconds."
            genesis = deps.read(genesis_path)
            deps.write(output_file,
                       f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
            deps.log(f"  {msg}")
            deps.emit("test.fail",
                      duration_ms=int((time.monotonic() - _t_start) * 1000),
                      data={"reason": "timeout", "timeout_s": deps.test_timeout})
            done["data"] = TestOutcome.timeout().as_event_data()
            return False, msg
        except FileNotFoundError as e:
            msg = (f"ERROR: test command not found: {e}\n"
                   f"Command was: {test_command}\n"
                   "Ensure the required runtime is installed and on PATH.")
            genesis = deps.read(genesis_path)
            deps.write(output_file,
                       f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
            deps.log(f"  {msg}")
            deps.emit("test.fail",
                      duration_ms=int((time.monotonic() - _t_start) * 1000),
                      data={"reason": "command_not_found", "error": str(e)[:200]})
            done["data"] = TestOutcome.command_not_found().as_event_data()
            sys.exit(1)

        test_output = result.stdout
        if result.stderr:
            test_output += "\n--- stderr ---\n" + result.stderr
        genesis = deps.read(genesis_path)
        deps.write(output_file,
                   f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{test_output}")
        passed   = result.returncode == 0
        _pass_n, _fail_n, _ = parse_test_counts(test_output)
        deps.log(f"  Tests {'PASSED' if passed else 'FAILED'}. Output -> {output_file}")
        deps.emit("test.pass" if passed else "test.fail",
                  duration_ms=int((time.monotonic() - _t_start) * 1000),
                  data={"returncode": result.returncode,
                        "pass_count": _pass_n, "fail_count": _fail_n,
                        "output": deps.store_prompt_content(test_output)})
        if not passed:
            retry_passed, retry_output = deps.task3_inline_retry(
                project_dir=project_dir,
                test_command=test_command,
                iteration=iteration,
                initial_test_output=test_output,
            )
            if retry_passed:
                passed = True
                test_output = retry_output
                deps.write(output_file,
                           f"=== genesis.md (iteration {iteration}) ===\n{genesis}"
                           f"\n{OUTPUT_SEPARATOR}\n{test_output}")
                _pass_n, _fail_n, _ = parse_test_counts(test_output)
                deps.log(f"  Tests PASSED via inline retry. Output -> {output_file}")
                deps.emit("test.pass", duration_ms=int(
                    (time.monotonic() - _t_start) * 1000),
                          data={"returncode": 0, "pass_count": _pass_n,
                                "fail_count": _fail_n, "via": "task3_inline_retry",
                                "output": deps.store_prompt_content(test_output)})
            elif retry_output != test_output:
                test_output = retry_output
                deps.write(output_file,
                           f"=== genesis.md (iteration {iteration}) ===\n{genesis}"
                           f"\n{OUTPUT_SEPARATOR}\n{test_output}")
                _pass_n, _fail_n, _ = parse_test_counts(test_output)
        done["data"] = TestOutcome.from_counts(passed, _pass_n, _fail_n).as_event_data()
        return passed, test_output
