"""The unpinned blocking bodies on disk (`.phase_c_logs/pin_gaps.json`).

Written when a pin pass leaves a blocking body without a test, read on the
next iteration so that body is asked again after the tree has changed.
Kept apart from the lesson store: a gap is a site, not a sentence about a
red test.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.attribution import canon_site
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR

__all__ = ["pin_gaps_path", "load_pin_gaps", "save_pin_gaps"]


def pin_gaps_path(project_dir: Path) -> Path:
    return project_dir / PHASE_C_LOGS_DIR / "pin_gaps.json"


def load_pin_gaps(project_dir: Path) -> tuple[FnSite, ...]:
    with suppress(OSError, ValueError, TypeError):
        raw = json.loads(pin_gaps_path(project_dir).read_text(
            encoding="utf-8", errors="replace"))
        if isinstance(raw, list):
            out: list[FnSite] = []
            for row in raw:
                if isinstance(row, (list, tuple)) and len(row) == 3:
                    out.append(canon_site(FnSite(str(row[0]), str(row[1]), str(row[2]))))
            return tuple(out)
    return ()


def save_pin_gaps(project_dir: Path, sites: Sequence[FnSite]) -> None:
    path = pin_gaps_path(project_dir)
    if not sites:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [[s.file, s.fn_name, s.container] for s in sites]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)
