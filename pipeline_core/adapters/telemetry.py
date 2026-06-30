"""LogTelemetry — the concrete `ports.reconstruct.Telemetry`.

Routes the reconstruction use-cases' log/emit/set_current through the ambient
:mod:`pipeline_core.log` facade (wired to the real sinks once by the composition
root's ``set_sinks``) and the state adapter. Replaces the `_ReconstructAdapter`
telemetry pass-through methods so the use-cases receive a real adapter (DI)."""
from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..domain.test_results import build_test_name_index, per_file_test_stats
from ..log import emit, log
from .state import mark_phase, set_current


def emit_per_file_test_overview(project_dir: Path, output: str, *, context: str) -> None:
    """Attribute the latest cargo-test output to files (domain
    per_file_test_stats; name-based, interleaving-proof) and publish the
    per-file pass/total table to the log + a tests.file_stats event, which
    the monitor merges into its compile overview. No test lines → no-op."""
    try:
        project_dir = Path(project_dir)
        known = {
            str(p.relative_to(project_dir)).replace(os.sep, "/")
            for d in ("src", "tests") if (project_dir / d).is_dir()
            for p in (project_dir / d).rglob("*.rs")
        }
        texts: dict[str, str] = {}
        if (project_dir / "tests").is_dir():
            for p in (project_dir / "tests").rglob("*.rs"):
                with suppress(OSError):
                    texts[str(p.relative_to(project_dir)).replace(os.sep, "/")] = (
                        p.read_text(encoding="utf-8", errors="replace")
                    )
        stats = per_file_test_stats(output, known, build_test_name_index(texts))
    except Exception as exc:
        log(f"  [test overview] skipped: {exc}")
        return
    if not stats:
        return
    ordered = sorted(stats.items(), key=lambda kv: (-kv[1]["failed"], kv[0]))
    tot_p = sum(s["passed"] for _, s in ordered)
    tot_t = sum(s["passed"] + s["failed"] for _, s in ordered)
    log(f"  [test overview] {context} — {tot_p}/{tot_t} tests passing:")
    name_w = max(len(f.split("/")[-1]) for f, _ in ordered)
    for f, s in ordered:
        short = f.split("/")[-1]
        total = s["passed"] + s["failed"]
        mark = "✓" if s["failed"] == 0 and total else "✗"
        ign = f"  ({s['ignored']} ignored)" if s["ignored"] else ""
        log(f"    {short.ljust(name_w)}  {mark} tests {s['passed']}/{total}{ign}")
    emit("tests.file_stats", data={
        "context": context, "passed": tot_p, "total": tot_t,
        "files": [{"file": f, **s} for f, s in ordered],
    })


class LogTelemetry:
    def log(self, msg: str) -> None:
        log(msg)

    def emit(self, event: str, **fields: Any) -> None:
        emit(event, **fields)

    def set_current(self, project_dir: Path, label: str) -> None:
        set_current(project_dir, label)

    def mark_phase(self, project_dir: Path, phase_key: str, status: str, *,
                   detail: str = "", source_hash: str | None = None) -> None:
        # Also satisfies ports.augment.AugmentTelemetry (one telemetry adapter for
        # both the reconstruct and augment use-cases).
        mark_phase(project_dir, phase_key, status, detail=detail, source_hash=source_hash)
