"""FileFailureJournal — the concrete `ports.reconstruct.FailureJournal`.

The append-only failure log on disk. Moved out of pipeline.py's
`load_failure_log`/`append_failure_log` free functions + the `_ReconstructAdapter`
binding so the reconstruction use-cases receive a real adapter directly (DI, not
a composition-root pass-through). The entry format is the domain
:class:`FailureEntry`; this layer only does file I/O."""
from __future__ import annotations

from pathlib import Path

from ..domain.failure_journal import JOURNAL_HEADER, FailureEntry
from ..io import read


class FileFailureJournal:
    """Reads/appends the markdown failure log fed back into TASK 4."""

    def load_failure_log(self, log_path: Path) -> str:
        """The full log (entries are tiny — no truncation needed)."""
        return read(log_path)

    def append_failure_log(
        self, log_path: Path, iteration: int | str, failure_type: str,
        current_pass: int, prev_pass_count: int, summary: str,
    ) -> None:
        entry = FailureEntry(
            iteration=iteration, failure_type=failure_type,
            pass_count=current_pass, prev_pass_count=prev_pass_count, summary=summary,
        )
        with log_path.open("a") as f:
            if log_path.stat().st_size == 0:
                f.write(JOURNAL_HEADER)
            f.write(entry.render())
