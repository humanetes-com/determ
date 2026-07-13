"""Skeleton compile-check for the t1.5 layout gate (F11 M2).

t1.5 accepts a model-authored private layout ONLY if the rewritten skeleton
still compiles with ``unimplemented!()`` bodies — the exact invariant the baked
skeleton holds. This runs ``cargo check`` (lib only, no test targets) over the
per-cluster skeleton crate.

CONSERVATIVE BY DESIGN: any error, timeout, or non-zero exit returns ``False``.
A false negative merely leaves a component frozen (no-op, safe); a false
positive would splice a broken scaffold into t2a — so we never guess ``True``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

_SKELETON_DIR = ".skeleton"


def skeleton_dir_for(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / cfg_cluster


def skeleton_src_dir_for(project_dir: Path, cfg_cluster: str) -> Path:
    return skeleton_dir_for(project_dir, cfg_cluster) / "src"


def _ensure_crate_manifest(project_dir: Path, skel_dir: Path) -> bool:
    """The per-cluster skeleton is just ``src/`` — give it the crate's
    ``Cargo.toml`` so cargo can build it as a lib. Idempotent."""
    dst = skel_dir / "Cargo.toml"
    if dst.exists():
        return True
    src = project_dir / "Cargo.toml"
    if not src.exists():
        return False
    try:
        shutil.copy(src, dst)
        return True
    except OSError:
        return False


def compile_skeleton_ok(
    project_dir: Path,
    cfg_cluster: str,
    *,
    env_extra: Mapping[str, str] | None = None,
    timeout: int = 240,
) -> bool:
    """True iff the per-cluster skeleton crate passes ``cargo check`` (lib only).
    Everything that is not a clean rc==0 → False (see module docstring)."""
    skel_dir = skeleton_dir_for(project_dir, cfg_cluster)
    if not (skel_dir / "src").is_dir():
        return False
    if not _ensure_crate_manifest(project_dir, skel_dir):
        return False
    env = dict(os.environ)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    try:
        proc = subprocess.run(
            ["cargo", "check", "--lib", "--quiet"],
            cwd=skel_dir, env=env, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
