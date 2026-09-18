"""CargoBuildScriptReader — runs a crate's ``build.rs`` under a chosen
toolchain and reports the cfgs it emits.

Satisfies :class:`pipeline_core.ports.build_script.BuildScriptReader`. Uses an
ISOLATED target dir per toolchain so the captured ``output`` is unambiguously that
toolchain's (no cross-toolchain contamination), and ``cargo check`` (build scripts
run for check too, no codegen) so the probe is cheap and incremental across runs.
Best-effort: a missing toolchain, absent ``build.rs``, or a crate that fails to
compile all yield the build-script facts anyway (the ``output`` is written before
the lib is compiled) or ``None`` when nothing can be captured.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from ..domain.build_script import BuildScriptCfg, parse_build_script_output
from .cargo import base_cargo_env

_PROBE_TIMEOUT = 180


class CargoBuildScriptReader:
    """Runs ``cargo check`` and parses the build-script ``output`` files."""

    def emitted_cfgs(self, project_dir: Path, toolchain: str | None) -> BuildScriptCfg | None:
        if not (project_dir / "build.rs").exists():
            return None  # no build script → no build-script-controlled cfgs

        target_dir = project_dir / "target" / ".cfgprobe" / (toolchain or "default")
        env = base_cargo_env()
        env["CARGO_TARGET_DIR"] = str(target_dir)
        if toolchain:
            env["RUSTUP_TOOLCHAIN"] = toolchain

        # Best effort: a missing toolchain (OSError) or a hang (timeout) is fine —
        # if a prior probe already wrote an output we still read it below.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            subprocess.run(
                ["cargo", "check", "--quiet"],
                cwd=project_dir, capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT, env=env,
            )

        outputs = list(target_dir.rglob("build/*/output"))
        if not outputs:
            return None

        set_cfgs: set[str] = set()
        declared: set[str] = set()
        for out in outputs:
            try:
                facts = parse_build_script_output(out.read_text(errors="replace"))
            except OSError:
                continue
            set_cfgs |= facts.set_cfgs
            declared |= facts.declared_cfgs
        return BuildScriptCfg(set_cfgs=frozenset(set_cfgs), declared_cfgs=frozenset(declared))
