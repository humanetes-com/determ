"""t0d's reach-round outcomes on disk (adapter).

`.skeleton/_reach_<cluster>.json`, a sibling of `_behaviours_<cluster>.json`,
rewritten whole on each t0d pass, an empty list included: the outcomes are a
statement about THIS pass's probes (`domain/reach_probe.py`), and a file left
standing from an earlier pass would read as current. t0c reads it to quote a
measured miss beside a target's route. A missing or unreadable file reads as
no outcomes: the prompt then carries the route alone, as it would without the
round.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.reach_probe import ReachOutcome, outcomes_from_json, outcomes_to_json


def reach_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / ".skeleton" / f"_reach_{cfg_cluster}.json"


def save_reach(project_dir: Path, cfg_cluster: str, outcomes: list[ReachOutcome]) -> Path:
    p = reach_path(project_dir, cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(outcomes_to_json(cfg_cluster, outcomes), encoding="utf-8")
    return p


def load_reach(project_dir: Path, cfg_cluster: str) -> dict[str, ReachOutcome]:
    try:
        return outcomes_from_json(reach_path(project_dir, cfg_cluster).read_text(encoding="utf-8"))
    except OSError:
        return {}
