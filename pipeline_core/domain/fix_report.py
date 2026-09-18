"""Typed outcome of one fix-loop attempt.

`generate_fix_loop` (and the inner per-fn / per-impl loops) passed an untyped
`dict[str, Any]` between the attempt and the loop driver — keys discovered by
`.get()`, control flow by key-presence sniffing (`record.get("abort")`). That
implicit record is now an explicit value object with four named outcomes
(:class:`FixKind`) and the fields the loop and the prompt-history narrative
actually read.

`as_record()` is the anti-corruption bridge to the legacy callback-loop dict
convention (`pipeline_core.application.refine_loop.run_callback_loop`); it emits
exactly the keys each original site emitted, so behaviour is byte-identical
until the loop itself consumes :class:`FixReport` directly. Construct via the
factories — each maps to one explicit outcome — never the bare constructor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class FixKind(StrEnum):
    """What one fix attempt did — the outcomes the dict record left implicit.

    APPLIED — a fix was produced and written/spliced; the loop re-verifies.
    NOOP    — the model returned nothing usable (no fix / no body / no splice);
              not a dead-end, the loop's stall budget decides whether to retry.
    ABORT   — unrecoverable for this unit (check timed out, fn not located);
              the loop stops immediately.
    """

    APPLIED = "applied"
    NOOP = "noop"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class FixReport:
    """One fix attempt's typed result. Build via the factories below."""

    kind: FixKind
    summary: str | None = None
    files_changed_str: str | None = None
    error_preview: str | None = None
    error_sig: str | None = None
    reason: str | None = None

    @property
    def abort(self) -> bool:
        return self.kind is FixKind.ABORT

    @classmethod
    def aborted(cls, reason: str, summary: str | None = None) -> FixReport:
        """Unrecoverable for this unit (timeout, fn not located)."""
        return cls(FixKind.ABORT, reason=reason, summary=summary)

    @classmethod
    def unchanged(cls, summary: str, error_preview: str) -> FixReport:
        """The model produced nothing usable this attempt."""
        return cls(FixKind.NOOP, summary=summary,
                   files_changed_str="no files changed",
                   error_preview=error_preview)

    @classmethod
    def changed(cls, summary: str, files_changed_str: str, error_preview: str,
                error_sig: str | None = None) -> FixReport:
        """A fix was produced and applied (written or spliced)."""
        return cls(FixKind.APPLIED, summary=summary,
                   files_changed_str=files_changed_str,
                   error_preview=error_preview, error_sig=error_sig)

    def as_record(self) -> dict[str, Any]:
        """Legacy bridge to the `run_callback_loop` dict convention. Emits only
        the keys the originating site emitted, so the record is content-equal to
        the hand-built dict (the prompt-history narrative and the loop's
        `.get("abort")`/`"summary"` reads see no change)."""
        rec: dict[str, Any] = {}
        if self.abort:
            rec["abort"] = True
        if self.reason is not None:
            rec["reason"] = self.reason
        if self.summary is not None:
            rec["summary"] = self.summary
        if self.files_changed_str is not None:
            rec["files_changed_str"] = self.files_changed_str
        if self.error_preview is not None:
            rec["error_preview"] = self.error_preview
        if self.error_sig is not None:
            rec["error_sig"] = self.error_sig
        return rec


@dataclass(frozen=True, slots=True)
class UnitRunOutcome:
    """How one fix unit's inner loop ended: the spliced unit-ids plus the
    termination verdict the escalation policy reads (fn → enclosing impl →
    whole file). `progressed` = the file's error floor dropped below its
    opening value at least once during the loop."""

    changed: tuple[str, ...] = ()
    clean: bool = False
    floor_stalled: bool = False
    progressed: bool = False

    @property
    def escalate(self) -> bool:
        """Identical-error stall: the stall budget was spent without EVER
        improving on the opening error count — the unit's scope is too small
        (the real defect lives in a sibling member or elsewhere in the file),
        so retrying at the same scope cannot converge."""
        return self.floor_stalled and not self.progressed and not self.clean
