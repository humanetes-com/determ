"""Monitor-side wrapper for the run-health detectors: cached rendering so the
TUI can show findings without re-reading the whole log every frame.

The findings are the log-segment detectors (`domain.run_health.analyze`) plus
the phase-cost ones (`monitor.phase_cost_view`): a phase this code made more
expensive than the earlier codes did, and a phase running past their p90. The
phase-cost table follows the findings."""

from __future__ import annotations

import time
from pathlib import Path

from pipeline_core.domain import model_notes as _mn
from pipeline_core.domain.run_health import HealthFinding, analyze
from pipeline_core.monitor.phase_cost_view import phase_cost_report

_TTL_S = 60.0
_cache: tuple[float, list[str]] = (0.0, [])


def _finding_lines(findings: list[HealthFinding]) -> list[str]:
    out: list[str] = []
    for f in findings:
        out.append(f"{f.severity.symbol} [{f.kind}] {f.subject}")
        out.append(f"   {f.evidence}")
        out.append(f"   → {f.action}")
    return out


def health_lines(log_path: str | Path, project_dir: str | Path,
                 events_log: str | Path | None = None, *,
                 running: bool = False) -> list[str]:
    """Rendered finding lines, recomputed at most every _TTL_S seconds."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < _TTL_S:
        return _cache[1]
    seg: list[str] = []
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
        start = 0
        for i, ln in enumerate(lines):
            if "PIPELINE STARTED" in ln:
                start = i
        seg = lines[start:]
    except OSError:
        pass
    notes: list[str] = []
    for fr in sorted(Path(project_dir).glob("functional_requirements_*.md")):
        try:
            _, section = _mn.slice_model_notes(fr.read_text(encoding="utf-8"))
            notes.extend(_mn.parse_note_bullets(section))
        except OSError:
            pass
    findings = analyze(seg, notes)
    table: list[str] = []
    if events_log is not None:
        try:
            report = phase_cost_report(events_log, running=running)
            findings += report.findings
            table = report.table
        except Exception as exc:  # an unreadable store must not blank the tab
            table = [f"Phase cost: unreadable ({type(exc).__name__}: {exc})"]
    out = _finding_lines(findings) or ["no findings — run looks healthy"]
    if table:
        out += ["", *table]
    _cache = (now, out)
    return out


def health_window(lines: list[str], inner: int, scroll_from_bottom: int) -> list[str]:
    """The visible slice for a viewport of `inner` rows, scrolled
    `scroll_from_bottom` rows up from the end (0 = bottom). The view opens
    scrolled to the top: the findings come first."""
    off = min(scroll_from_bottom, max(0, len(lines) - inner))
    top = max(0, len(lines) - inner - off)
    return lines[top:top + inner]
