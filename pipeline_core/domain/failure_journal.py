"""Failure-journal domain — the append-only record of each reconstruction
iteration's outcome, fed back into TASK 4 prompts so the model avoids repeating
failed approaches.

Pure VO: the entry's wire format (the markdown block + the pass-count trend
phrasing) used to be inlined in `pipeline.append_failure_log`. Making it an
explicit :class:`FailureEntry` with a `render()` keeps the format in one typed
place and lets the adapter stay a thin file-append. Stdlib only, no I/O."""
from __future__ import annotations

from typing import NamedTuple

# Written once, the first time the log is appended to (the file starts empty).
JOURNAL_HEADER = (
    "# Failure Log\n\nRunning record of every iteration. "
    "Most recent entry is at the bottom.\n"
)


class FailureEntry(NamedTuple):
    """One iteration's outcome as recorded in the failure log."""

    iteration: int | str
    failure_type: str
    pass_count: int
    prev_pass_count: int
    summary: str

    @property
    def trend(self) -> str:
        """Human phrasing of the pass-count delta vs the previous iteration."""
        delta = self.pass_count - self.prev_pass_count
        if delta > 0:
            return f"+{delta} improvement"
        if delta < 0:
            return f"{delta} regression"
        return "no change"

    def render(self) -> str:
        """The markdown block appended for this iteration."""
        return (
            f"\n## Iteration {self.iteration}\n"
            f"- **Failure type:** {self.failure_type}\n"
            f"- **Tests passing:** {self.pass_count} "
            f"(was {self.prev_pass_count}, {self.trend})\n"
            f"- **Summary:** {self.summary}\n"
        )
