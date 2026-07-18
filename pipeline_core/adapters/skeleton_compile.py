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
    ok, _ = compile_skeleton_check(
        project_dir, cfg_cluster, env_extra=env_extra, timeout=timeout)
    return ok


_ERROR_EXCERPT_CHARS = 5000


def compile_skeleton_check(
    project_dir: Path,
    cfg_cluster: str,
    *,
    env_extra: Mapping[str, str] | None = None,
    timeout: int = 240,
) -> tuple[bool, str]:
    """``compile_skeleton_ok`` plus a bounded compiler-error excerpt, so a
    failed layout-authoring attempt can feed the model WHAT failed instead of
    a bare "did not compile" (feedback fidelity). rustc puts the primary
    diagnostics first — keep the head."""
    skel_dir = skeleton_dir_for(project_dir, cfg_cluster)
    if not (skel_dir / "src").is_dir():
        return False, "skeleton src/ missing"
    if not _ensure_crate_manifest(project_dir, skel_dir):
        return False, "crate manifest missing"
    env = dict(os.environ)
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})
    try:
        proc = subprocess.run(
            ["cargo", "check", "--lib", "--quiet"],
            cwd=skel_dir, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cargo check did not run: {exc}"
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or "")[:_ERROR_EXCERPT_CHARS]
