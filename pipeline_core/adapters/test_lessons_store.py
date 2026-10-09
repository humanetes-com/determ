"""The lesson store on disk (`.phase_c_logs/test_lessons.json`): what task 4
wrote per red test for the next t2a (`domain/test_lessons`). Kept in its own
file: the retrospective store carries conviction stamps whose semantics a
lesson does not share."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.test_lessons import LessonStore
from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR

__all__ = ["lessons_path", "load_lessons", "save_lessons"]


def lessons_path(project_dir: Path) -> Path:
    return project_dir / PHASE_C_LOGS_DIR / "test_lessons.json"


def load_lessons(project_dir: Path) -> LessonStore:
    with suppress(OSError, ValueError, TypeError):
        raw = json.loads(lessons_path(project_dir).read_text(
            encoding="utf-8", errors="replace"))
        if isinstance(raw, dict):
            return LessonStore(raw)
    return LessonStore()


def save_lessons(project_dir: Path, store: LessonStore) -> None:
    p = lessons_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store.to_dict(), sort_keys=True, indent=1),
                   encoding="utf-8")
    tmp.replace(p)
