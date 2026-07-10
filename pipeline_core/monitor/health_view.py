"""Monitor-side wrapper for the run-health detectors: cached rendering so the
TUI can show findings without re-reading the whole log every frame."""

from __future__ import annotations

import time
from pathlib import Path

from pipeline_core.domain import model_notes as _mn
from pipeline_core.domain.run_health import analyze

_TTL_S = 60.0
_cache: tuple[float, list[str]] = (0.0, [])


def health_lines(log_path: str | Path, project_dir: str | Path) -> list[str]:
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
    if not findings:
        out = ["no findings — run looks healthy"]
    else:
        sym = {"info": "·", "warn": "⚠", "alert": "‼"}
        out = []
        for f in findings:
            out.append(f"{sym[f.severity]} [{f.kind}] {f.subject}")
            out.append(f"   {f.evidence}")
            out.append(f"   → {f.action}")
    _cache = (now, out)
    return out
