"""The ask ledger on disk (`.phase_c_logs/ask_ledger_<cluster>.json`).

Every (function, red test) fingerprint a fill has asked
(`domain/candidate_tiers.AskLedger`), so a resumed process does not re-owe a
question the previous one already put to the model. The ledger carries its
oracle; a moved oracle reads as an empty ledger
(`AskLedger.for_oracle`)."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

from pipeline_core.domain.candidate_tiers import AskLedger
from pipeline_core.domain.workspace_layout import PHASE_C_LOGS_DIR

__all__ = ["ask_ledger_path", "load_ask_ledger", "save_ask_ledger"]


def ask_ledger_path(project_dir: Path, cluster: str) -> Path:
    return project_dir / PHASE_C_LOGS_DIR / f"ask_ledger_{cluster}.json"


def load_ask_ledger(project_dir: Path, cluster: str) -> AskLedger:
    """The stored ledger, or an empty one when none (or an unreadable one)
    is on disk."""
    p = ask_ledger_path(project_dir, cluster)
    with suppress(OSError, ValueError, TypeError, AttributeError):
        return AskLedger.from_dict(json.loads(p.read_text(encoding="utf-8")))
    return AskLedger()


def save_ask_ledger(project_dir: Path, cluster: str, ledger: AskLedger) -> None:
    p = ask_ledger_path(project_dir, cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger.to_dict(), sort_keys=True), encoding="utf-8")
    tmp.replace(p)
