"""Flow-invariant audit — periodic + on-demand run-health reporting.

Relocated from `pipeline.py`: the run-loop's audit-thread resident
becomes composition-owned. Holds the shared `RunState` by reference — `init_log`
/ run-registration populate it before the first report, and because it is the
same object those writes are visible here — runs the domain audit
(`pipeline_core.audit`) over the events log + prompts dir, and reports via the
injected log + emit ports. Owns no pipeline globals; every input is injected.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.audit import FAIL, audit
from pipeline_core.domain.run_state import RunState


@dataclass(frozen=True, slots=True)
class FlowAuditor:
    run_state: RunState
    events_log: Callable[[], str]
    prompts_dir: Path
    log: Callable[[str], None]
    emit: Callable[..., None]
    plateau_n: int = 3
    interval_sec: int = 900

    def report(self, context: str) -> None:
        if not self.run_state.audit_enabled:
            return
        try:
            verdicts = audit(
                Path(self.events_log()), self.prompts_dir, self.run_state.log_path,
                self.run_state.audit_project_dir, self.run_state.run_started_iso,
                self.plateau_n)
            self.log(f"  [audit] flow invariants ({context}):")
            for v in verdicts:
                self.log(f"    {v.check.ljust(22)} {v.status:4s} {v.evidence}")
            self.emit("flow_audit.report", data={
                "context": context,
                "failed": sum(1 for v in verdicts if v.status == FAIL),
                "verdicts": [{"check": v.check, "status": v.status,
                              "evidence": v.evidence} for v in verdicts],
            })
        except Exception as exc:
            self.log(f"  [audit] skipped: {exc}")

    def start_thread(self) -> None:
        def _loop() -> None:
            while True:
                time.sleep(self.interval_sec)
                self.report(f"periodic/{self.interval_sec}s")

        threading.Thread(target=_loop, name="flow-audit", daemon=True).start()
