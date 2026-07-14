"""Layout-outcome storage — persist WHICH connected component t1.5 authored,
gated, or left frozen (F11 M3's precondition).

Written once per t1.5 pass (`application/author_layout_phase.py::run_layout_phase`),
read later by the F11 M3 re-trigger mechanism (`application/retrigger_layout.py`) to
find "which private types are still on the ORIGINAL frozen scaffold" without
re-deriving reachability from scratch mid-run. One file per cfg-cluster mirrors
the skeleton's per-cluster layout, matching `private_surface_store.py`'s convention.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.application.author_layout import (
    AuthorLayoutResult,
    LayoutOutcome,
    component_outcome,
)

_SKELETON_DIR = ".skeleton"


def _outcomes_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"_layout_outcomes_{cfg_cluster}.json"


def save_layout_outcomes(project_dir: Path, result: AuthorLayoutResult) -> Path:
    """Write `result`'s per-component outcomes to
    ``.skeleton/_layout_outcomes_<cluster>.json``."""
    p = _outcomes_path(project_dir, result.cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "item_names": sorted(c.item_names),
            "outcome": component_outcome(c).name,
        }
        for c in result.components
    ]
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def load_layout_outcomes(
    project_dir: Path, cfg_cluster: str,
) -> list[tuple[frozenset[str], LayoutOutcome]]:
    """Read a cluster's baked per-component outcomes. Missing/corrupt → empty
    list (the F11 M3 caller is additive — its absence must never break a run;
    it simply finds nothing to retrigger)."""
    p = _outcomes_path(project_dir, cfg_cluster)
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        out: list[tuple[frozenset[str], LayoutOutcome]] = []
        for row in rows:
            names = frozenset(str(x) for x in (row.get("item_names") or []))
            outcome = LayoutOutcome[str(row.get("outcome", "FROZEN"))]
            out.append((names, outcome))
        return out
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return []


def _attempts_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"_layout_retrigger_attempts_{cfg_cluster}.json"


def load_retrigger_attempts(project_dir: Path, cfg_cluster: str) -> dict[str, int]:
    """Per-component retrigger attempt counts (key = sorted item names joined
    by ``|``), so the cap in `application/retrigger_layout.py` survives a
    pipeline restart, not just a single process's memory."""
    p = _attempts_path(project_dir, cfg_cluster)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_retrigger_attempts(
    project_dir: Path, cfg_cluster: str, attempts: dict[str, int],
) -> Path:
    p = _attempts_path(project_dir, cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(attempts, indent=2, sort_keys=True), encoding="utf-8")
    return p
