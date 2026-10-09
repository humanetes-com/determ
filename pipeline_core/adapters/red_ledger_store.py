"""The red ledger on disk (`.phase_c_logs/red_ledger_<cluster>.json`).

Written at every cluster end (`domain/red_ledger`): why each red test is
still red. Task 3 prints it beside the cluster's verdict, and task 4's
per-test pass reads it, so what t2a tried reaches the analysis that runs
after it."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR

__all__ = ["load_red_ledger", "red_ledger_path", "save_red_ledger"]


def red_ledger_path(project_dir: Path, cluster: str) -> Path:
    return project_dir / PHASE_C_LOGS_DIR / f"red_ledger_{cluster}.json"


def save_red_ledger(project_dir: Path, cluster: str,
                    entries: Iterable[Mapping[str, Any]], *,
                    run_id: str = "", close_kind: str = "") -> None:
    p = red_ledger_path(project_dir, cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"cluster": cluster, "run_id": run_id,
                               "close_kind": close_kind,
                               "entries": [dict(e) for e in entries]},
                              sort_keys=True, indent=1), encoding="utf-8")
    tmp.replace(p)


def load_red_ledger(project_dir: Path, cluster: str) -> dict[str, Any]:
    """The stored ledger, or an empty one when none is readable."""
    with suppress(OSError, ValueError, TypeError):
        raw = json.loads(red_ledger_path(project_dir, cluster).read_text(
            encoding="utf-8", errors="replace"))
        if isinstance(raw, dict):
            return raw
    return {"cluster": cluster, "entries": []}
