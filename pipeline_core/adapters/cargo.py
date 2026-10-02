"""Cargo adapter — wraps the cargo subprocess primitive.

Satisfies :class:`pipeline_core.ports.Cargo`. Mirrors ``pipeline._run_cargo``: split the
command, run it in ``cwd`` with an explicit ``env`` and ``timeout``, and return a
structured result. Unlike the legacy helper (which collapsed stdout+stderr into one
string and signalled timeout with rc=-1), this keeps the streams separate in a typed
:class:`Process` — strictly more information, behaviour-compatible for callers that read
``returncode`` and the combined text.

When ``run`` returns after a timeout, no process of the group it started is
alive (`adapters.process.run_bounded`).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.cluster_env import overlay_cluster_env
from ..domain.cluster_scope import ClusterScope
from ..domain.enums import ProcessFailure
from ..domain.models import BuildCluster
from ..domain.test_command import parse_cmd
from .process import run_bounded


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
    """Layer a cluster's env contribution onto ``env`` in place. The rule is
    :func:`domain.cluster_env.overlay_cluster_env`: the cluster's flags are
    APPENDED so the base ``-A warnings`` survives, every cfg it sets for rustc
    is set for rustdoc too, and ``RUSTUP_TOOLCHAIN=nightly`` is set when the
    cluster requires the nightly toolchain."""
    overlay_cluster_env(env, cluster)


def cluster_cargo_env(cluster: BuildCluster | None) -> dict[str, str]:
    """The full cargo env for ONE named cluster, independent of the active scope.

    Base env, then :func:`apply_cluster_env` — so RUSTFLAGS is `-A warnings`
    plus the cluster's `--cfg`, and RUSTUP_TOOLCHAIN=nightly is SET when the
    cluster requires it. This is the mapping every cargo the loop composes for
    itself must run under, not just the sweep's own command line: the sweep
    carries the toolchain as `cargo +nightly` in the command, so an env that
    carried only the cfg went unnoticed there and broke every cargo built from
    the env alone. Run 216855-03b2a21b (2026-09-02, cluster nightly): the
    assert probe's stage recorded rustflags `['--cfg',
    'error_generic_member_access']` under the stable rustc, the lib failed
    E0554 four times, and every sweep printed `probe produced no observation
    (rc=101)` — the session's observed-values slot was empty for the exact
    tests the plateau was about.
    """
    env = base_cargo_env()
    apply_cluster_env(env, cluster)
    return env


class CargoEnv:
    """Builds the cargo subprocess env for the *currently active* cluster. A
    ``Callable[[], dict[str, str]]`` (the zero-arg seam every cargo call binds),
    reading the active cluster from an injected :class:`ClusterScope` at call
    time — so a mid-run cluster activation is reflected without a global.
    Delegates to :func:`cluster_cargo_env` so the overlay rule has one owner."""

    def __init__(self, scope: ClusterScope) -> None:
        self._scope = scope

    def __call__(self) -> dict[str, str]:
        return cluster_cargo_env(self._scope.active)


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
    """Runs one cargo command. rc=-1 signals timeout / binary-not-found (legacy parity).

    Timeout kills the process GROUP: cargo is the child, the test binary is
    the grandchild, and a hung grandchild is exactly the process the bound
    was written for. When this returns after a timeout, no process of that
    group is alive.
    """

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = 180) -> Process:
        r = run_bounded(shlex.split(cmd), cwd=cwd, env=env, timeout=float(timeout))
        return Process(r.returncode, r.stdout, r.stderr, failure=r.failure)
