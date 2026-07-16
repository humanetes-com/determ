"""Cargo adapter — wraps the cargo subprocess primitive.

Satisfies :class:`pipeline_core.ports.Cargo`. Mirrors ``pipeline._run_cargo``: split the
command, run it in ``cwd`` with an explicit ``env`` and ``timeout``, and return a
structured result. Unlike the legacy helper (which collapsed stdout+stderr into one
string and signalled timeout with rc=-1), this keeps the streams separate in a typed
:class:`Process` — strictly more information, behaviour-compatible for callers that read
``returncode`` and the combined text.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.cluster_scope import ClusterScope
from ..domain.enums import ProcessFailure
from ..domain.models import BuildCluster
from ..domain.test_command import parse_cmd


def run_build_check(
    project_dir: Path,
    build_cmd: str,
    *,
    cargo_env: Callable[[], dict[str, str]],
    build_timeout: int,
) -> tuple[bool, str]:
    """Run the compile-only check command in `project_dir`. Returns (success,
    output) with stdout+stderr; failure-safe on timeout / missing command."""
    try:
        result = subprocess.run(
            parse_cmd(build_cmd),
            cwd=project_dir, capture_output=True, text=True,
            timeout=build_timeout, env=cargo_env(),
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired as e:
        partial = (_partial_text(e.stdout) + "\n" + _partial_text(e.stderr)).strip()
        return False, (partial + "\n" if partial else "") + (
            f"Build check timed out after {build_timeout}s.")
    except FileNotFoundError as e:
        return False, f"Build check command not found: {e}"


def base_cargo_env() -> dict[str, str]:
    """The static cargo env: cargo on PATH, warnings suppressed (clean error
    context for the LLM), no terminal colour. Cluster overrides layer on top."""
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".cargo" / "bin") + ":" + env.get("PATH", "")
    env["RUSTFLAGS"] = "-A warnings"
    env["CARGO_TERM_COLOR"] = "never"
    # Deliberately NOT setting RUST_BACKTRACE=1: it makes Debug-asserting tests
    # panic, which aborts cargo-llvm-cov before it writes lcov (0/0 coverage).
    return env


def apply_cluster_env(env: dict[str, str], cluster: BuildCluster | None) -> None:
    """Layer a cluster's env contribution onto ``env`` in place: its ``env_extra``
    (RUSTFLAGS is APPENDED so the base ``-A warnings`` survives; others set), and
    ``RUSTUP_TOOLCHAIN=nightly`` when the cluster requires the nightly toolchain."""
    if cluster and cluster.env_extra:
        for k, v in cluster.env_extra.items():
            if k == "RUSTFLAGS":
                env["RUSTFLAGS"] = env["RUSTFLAGS"] + " " + v
            else:
                env[k] = v
    if cluster and cluster.requires_nightly:
        env["RUSTUP_TOOLCHAIN"] = "nightly"


class CargoEnv:
    """Builds the cargo subprocess env for the *currently active* cluster. A
    ``Callable[[], dict[str, str]]`` (the zero-arg seam every cargo call binds),
    reading the active cluster from an injected :class:`ClusterScope` at call
    time — so a mid-run cluster activation is reflected without a global."""

    def __init__(self, scope: ClusterScope) -> None:
        self._scope = scope

    def __call__(self) -> dict[str, str]:
        env = base_cargo_env()
        apply_cluster_env(env, self._scope.active)
        return env


@dataclass(frozen=True, slots=True)
class Process:
    """A finished subprocess. Satisfies the ports.ProcessResult Protocol.

    The stdout+stderr "combined" layout legacy callers wanted now lives in
    application/reconstruct/cargo_runner (`_combined`), built from these plain
    fields — the port stays a pure result record."""

    returncode: int
    stdout: str
    stderr: str
    # rc=-1 made explicit: which failure CLASS produced no exit
    # code. None for any normally-exited process (including rc!=0 failures).
    failure: ProcessFailure | None = None




def _partial_text(captured: object) -> str:
    """TimeoutExpired.stdout/.stderr may be bytes (pre-kill partial capture)
    or None regardless of text=True — normalise to str."""
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return str(captured)


class CargoCli:
    """Runs one cargo command. rc=-1 signals timeout / binary-not-found (legacy parity)."""

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = 180) -> Process:
        try:
            r = subprocess.run(
                shlex.split(cmd), cwd=cwd, capture_output=True, text=True,
                timeout=timeout, env=env,
            )
            return Process(r.returncode, r.stdout or "", r.stderr or "")
        except subprocess.TimeoutExpired as e:
            # Keep the partial output: for a HUNG test it names which test was
            # running when the clock ran out — the one fact the fix loop and
            # the operator need. Discarding it is a feedback-fidelity loss.
            return Process(-1, _partial_text(e.stdout),
                           _partial_text(e.stderr) + f"\nTIMEOUT after {timeout}s",
                           failure=ProcessFailure.TIMEOUT)
        except FileNotFoundError as e:
            return Process(-1, "", f"FILE NOT FOUND: {e}",
                           failure=ProcessFailure.BINARY_MISSING)
