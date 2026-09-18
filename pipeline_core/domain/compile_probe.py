"""The result of one scoped compile check, as a named value.

The inner per-unit loops ran `cargo check`, rendered the diagnostics to plain
text, and built a (file, fn)→errors frontier from the STRUCTURED diagnostics
(never the ANSI text — the colour codes defeat plain-text parsing → an empty
frontier → the loop wrongly believes the unit is clean). :class:`CompileProbe`
bundles those three outputs so a :class:`~pipeline_core.domain.fix_target.FixTarget`
can render its clean/dirty verdict from `rc` + `frontier` without the checking
side knowing anything about targets.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline_core.domain.models import Frontier


@dataclass(frozen=True, slots=True)
class CompileProbe:
    """One `cargo check` result: exit code, ANSI-stripped render, the structured
    (file, fn)→{error sigs} frontier, and the raw diagnostic count per file."""

    rc: int
    rendered: str
    frontier: Frontier = field(default_factory=dict)
    errors_by_file: dict[str, int] = field(default_factory=dict)

    def errors_in_file(self, file_rel: str) -> int:
        """Raw diagnostic count in one file — a unit's lower-is-better floor
        metric (the gauge senses repair as this file's errors fall)."""
        return self.errors_by_file.get(file_rel, 0)
