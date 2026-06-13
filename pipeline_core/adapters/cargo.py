"""Cargo adapter — wraps the cargo subprocess primitive.

Satisfies :class:`pipeline_core.ports.Cargo`. Mirrors ``pipeline._run_cargo``: split the
command, run it in ``cwd`` with an explicit ``env`` and ``timeout``, and return a
structured result. Unlike the legacy helper (which collapsed stdout+stderr into one
string and signalled timeout with rc=-1), this keeps the streams separate in a typed
:class:`Process` — strictly more information, behaviour-compatible for callers that read
``returncode`` and the combined text.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Process:
    """A finished subprocess. Satisfies the ports.ProcessResult Protocol.

    The stdout+stderr "combined" layout legacy callers wanted now lives in
    application/reconstruct/cargo_runner (`_combined`), built from these plain
    fields — the port stays a pure result record."""

    returncode: int
    stdout: str
    stderr: str


class CargoCli:
    """Runs one cargo command. rc=-1 signals timeout / binary-not-found (legacy parity)."""

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = 180) -> Process:
        try:
            r = subprocess.run(
                shlex.split(cmd), cwd=cwd, capture_output=True, text=True,
                timeout=timeout, env=env,
            )
            return Process(r.returncode, r.stdout or "", r.stderr or "")
        except subprocess.TimeoutExpired:
            return Process(-1, "", f"TIMEOUT after {timeout}s")
        except FileNotFoundError as e:
            return Process(-1, "", f"FILE NOT FOUND: {e}")
