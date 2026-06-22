"""Per-file generation-constraints reader (P6).

Loads `project_configs/{project}.constraints.toml` — optional per-source-file
constraint text injected into the task0c generation prompt. The `[stable]` table
is the base; `[nightly]` entries override it when a non-default cluster is active.
Pure I/O lifted out of the pipeline.py god-file; the composition root injects the
configs directory, the active-cluster predicate, and the log sink.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileConstraintsReader:
    configs_dir: Path
    is_nightly: Callable[[], bool]
    log: Callable[[str], None]

    def load(self, project_dir: Path) -> dict[str, str]:
        """Map of source-file basename → constraint text ({} if no config / parse error)."""
        config_path = self.configs_dir / f"{project_dir.name}.constraints.toml"
        if not config_path.exists():
            return {}
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.log(f"  WARNING: could not load file constraints from {config_path}: {e}")
            return {}
        result: dict[str, str] = dict(data.get("stable", {}))
        if self.is_nightly():
            result.update(data.get("nightly", {}))
        return result
