"""Extractor adapter — wraps the Rust skeleton-extractor binary over its JSON CLI.

Satisfies :class:`pipeline_core.ports.Extractor`. Mirrors the invocation patterns in
``pipeline.py`` byte-for-byte (the seam is sacred, CODE_ARCHITECTURE.md §4): each
``enumerate_*`` shells ``[bin, "--<subcmd>", "--input", <dir>, <cfg-args>]``, parses the
stdout JSON, and returns ``None`` on any failure (missing binary, non-zero rc, bad JSON).

Per-item cfg filtering (``--cfg-true`` / ``--cfg-false``) is taken as plain predicate
tuples, not a ``BuildCluster`` object — that keeps the adapter decoupled from the
application layer where ``BuildCluster`` currently lives.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..domain.models import ConstRecord, DispatchFamily, FnRecord, ModRecord, TypeRecord


def _cfg_args(cfg_true: tuple[str, ...], cfg_false: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for pred in cfg_true:
        out.extend(["--cfg-true", pred])
    for pred in cfg_false:
        out.extend(["--cfg-false", pred])
    return out


class CliExtractor:
    """Invokes the skeleton-extractor binary. One method per subcommand."""

    def __init__(self, binary: Path) -> None:
        self._bin = Path(binary)

    # ── enumerate-* : path-based, JSON-list out, None on failure ──────────────
    def _enumerate(
        self,
        subcmd: str,
        src_dir: Path,
        cfg_true: tuple[str, ...],
        cfg_false: tuple[str, ...],
        *,
        timeout: int = 60,
    ) -> list[Any] | None:
        if not self._bin.exists():
            return None
        cmd = [str(self._bin), subcmd, "--input", str(src_dir), *_cfg_args(cfg_true, cfg_false)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        if r.returncode != 0:
            return None
        try:
            data = json.loads(r.stdout)
        except Exception:
            return None
        return data if isinstance(data, list) else None

    def enumerate_fns(self, src_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> list[FnRecord] | None:
        rows = self._enumerate("--enumerate-fns", src_dir, cfg_true, cfg_false)
        return None if rows is None else [FnRecord.from_dict(r) for r in rows]

    def enumerate_types(self, src_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> list[TypeRecord] | None:
        rows = self._enumerate("--enumerate-types", src_dir, cfg_true, cfg_false)
        return None if rows is None else [TypeRecord.from_dict(r) for r in rows]

    def enumerate_dispatch_families(self, src_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> list[DispatchFamily] | None:
        rows = self._enumerate("--enumerate-dispatch-families", src_dir, cfg_true, cfg_false)
        return None if rows is None else [DispatchFamily.from_dict(r) for r in rows]

    def enumerate_feature_gate_cfgs(self, src_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> list[str] | None:
        rows = self._enumerate("--enumerate-feature-gate-cfgs", src_dir, cfg_true, cfg_false)
        return None if rows is None else [str(x) for x in rows]

    def enumerate_mods(self, src_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> list[ModRecord] | None:
        rows = self._enumerate("--enumerate-mods", src_dir, cfg_true, cfg_false)
        return None if rows is None else [ModRecord.from_dict(r) for r in rows]

    def enumerate_consts(self, src_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> list[ConstRecord] | None:
        rows = self._enumerate("--enumerate-consts", src_dir, cfg_true, cfg_false)
        return None if rows is None else [ConstRecord.from_dict(r) for r in rows]

    # ── skeletonise : --input/--output tree, structured report dict ───────────
    def skeletonise(self, input_dir: Path, output_dir: Path, *, cfg_true: tuple[str, ...] = (), cfg_false: tuple[str, ...] = ()) -> dict[str, Any]:
        if not self._bin.exists():
            return {"ok": False, "reports": [], "stderr": f"skeleton-extractor binary missing at {self._bin}. Run `bash setup.sh` to build it."}
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        cmd = [str(self._bin), "--input", str(input_dir), "--output", str(output_dir), "--report", *_cfg_args(cfg_true, cfg_false)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reports": [], "stderr": "skeleton-extractor timed out (>120s)"}
        except Exception as e:
            return {"ok": False, "reports": [], "stderr": f"skeleton-extractor invocation failed: {e}"}
        if r.returncode != 0:
            return {"ok": False, "reports": [], "stderr": r.stderr.strip()}
        try:
            reports = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError as e:
            return {"ok": False, "reports": [], "stderr": f"could not parse JSON report: {e}"}
        return {"ok": True, "reports": reports, "stderr": r.stderr.strip()}

    # ── merge-clusters : cross-cluster AST merge, bool out ────────────────────
    def merge_clusters(self, out_path: Path, cluster_files: list[tuple[str, Path, str]]) -> bool:
        if not self._bin.exists():
            return False
        cmd = [str(self._bin), "--merge-clusters", "--merge-output", str(out_path)]
        for name, src_path, cfg_pred in cluster_files:
            cmd += ["--cluster", f"{name}:{src_path}:{cfg_pred}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception:
            return False
        return r.returncode == 0
