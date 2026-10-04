"""LogTelemetry — the concrete `ports.reconstruct.Telemetry`.

Routes the reconstruction use-cases' log/emit/set_current through the ambient
:mod:`pipeline_core.log` facade (wired to the real sinks once by the composition
root's ``set_sinks``) and the state adapter. Replaces the `_ReconstructAdapter`
telemetry pass-through methods so the use-cases receive a real adapter (DI)."""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.phase_registry import PHASE_ENTER, PHASE_EXIT, PHASE_PROGRESS
from ..domain.test_results import build_test_name_index, per_file_test_stats
from ..log import emit, log
from ..phases import current_scope

PHASE_EVENTS = frozenset({PHASE_ENTER, PHASE_EXIT, PHASE_PROGRESS})


class JsonlEventEmitter:
    """The structured-event sink: append one JSON record per event to
    ``events.jsonl`` — one JSON object per line; cheap to write, crash-safe,
    tail-friendly, SQLite-ingestable. Fixed fields: ts (ISO 8601 UTC, ms),
    product, pipeline_sha, event (dot.notation verb, e.g. "llm.request");
    optional: task, cluster, module, file, attempt, duration_ms, data.
    Prompts/responses are content-addressed to logs/prompts/{sha256}.txt so
    events stay small (~90% of context repeats across calls → strong dedup).
    Holds a live reference to the run-context dict (product / pipeline_sha /
    run_id), which the composition root mutates via `init_events`. Never
    raises — observability must not crash the pipeline.

    Every event other than a phase announcement is stamped with `in_scope` /
    `in_phase`: the phase scope open in the emitting context
    (`phases.current_scope`), so a call or a cargo run names the phase that
    paid for it. Observers (`observe`) see each finished record in-process —
    the run-stats recorder folds them into per-phase cost."""

    def __init__(
        self, events_log: Callable[[], str], run_context: dict[str, str],
    ) -> None:
        # Path resolved LIVE (a provider, not a captured string) so the
        # composition root can rebind the sink — and tests redirect it.
        self._events_log = events_log
        self._ctx = run_context
        self._observers: list[Callable[[dict[str, Any]], None]] = []

    @property
    def events_log(self) -> str:
        return self._events_log()

    def observe(self, observer: Callable[[dict[str, Any]], None]) -> None:
        """Call `observer(record)` after every emitted record. An observer
        that raises is skipped for that record, never propagated."""
        self._observers.append(observer)

    def emit(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] | None = None
        try:
            record = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "product": self._ctx.get("product", "unknown"),
                "pipeline_sha": self._ctx.get("pipeline_sha", "unknown"),
                "run_id": self._ctx.get("run_id", ""),
                "event": event,
                # Emitting thread — per-thread stream logs key on this name.
                "thread": threading.current_thread().name,
                # OS thread id — distinguishes a real name-collision from reentrancy.
                "tid": threading.get_ident(),
            }
            if event not in PHASE_EVENTS:
                scope = current_scope()
                if scope is not None:
                    record["in_scope"], record["in_phase"] = scope
            for k, v in fields.items():
                if v is not None:
                    record[k] = v
            line = json.dumps(record, default=str) + "\n"
            with open(self._events_log(), "a") as fh:
                fh.write(line)
            # The verdict ledger: every judging-gate decision doubles into a
            # dedicated sibling file, so "what was judged, on what evidence,
            # at what cost" is one grep instead of a session-long audit of a
            # 100 MB event stream. Same row, same crash-safety contract.
            if event == "phase_c.verdict":
                _vpath = Path(self._events_log()).with_name("verdicts.jsonl")
                with open(_vpath, "a") as fh:
                    fh.write(line)
            # Phase announcements double into a small sibling file for the
            # same reason: the monitor reads events.jsonl through an 8 MB tail
            # window, and a run that fills for hours pushes its own setup and
            # strip phases out of it. The phase record is a few hundred lines
            # per run and is read whole.
            if event in PHASE_EVENTS:
                _ppath = Path(self._events_log()).with_name("phases.jsonl")
                with open(_ppath, "a") as fh:
                    fh.write(line)
        except Exception:
            pass  # observability must never crash the pipeline
        # Outside the write's try: an unwritable log must not also blind the
        # in-process observers to a record that was built.
        if record is not None and "event" in record:
            for observer in self._observers:
                with suppress(Exception):
                    observer(record)

    @contextmanager
    def phase(self, prefix: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Emit `{prefix}.start` on enter and `{prefix}.done` on exit with auto
        duration_ms. Yields a mutable dict whose keys become extra `.done` fields
        (`data` deep-merges). On exception, `.done` still fires with
        `data.error = <ExceptionName>`, then the exception re-raises."""
        t0 = time.monotonic()
        self.emit(f"{prefix}.start", **fields)
        extra: dict[str, Any] = {}
        err: str | None = None
        try:
            yield extra
        except BaseException as _e:
            err = type(_e).__name__
            raise
        finally:
            merged: dict[str, Any] = {**fields, **{k: v for k, v in extra.items() if k != "data"}}
            merged_data: dict[str, Any] = {**(fields.get("data") or {}), **(extra.get("data") or {})}
            if err is not None:
                merged_data["error"] = err
            if merged_data:
                merged["data"] = merged_data
            merged["duration_ms"] = int((time.monotonic() - t0) * 1000)
            self.emit(f"{prefix}.done", **merged)
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
