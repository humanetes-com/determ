"""Behaviour-suite storage — persist harvested/synthesized behaviours as a baked
artifact under ``.skeleton/`` (F10).

Written ONCE in the pre-strip window (Hard Rule 11), read by every post-strip
phase that needs behaviours: the fill prompt (few-shot RIGHT usage) and the t3
oracle. One file per cfg-cluster mirrors the skeleton's per-cluster layout.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.behaviour import BehaviourSuite

_SKELETON_DIR = ".skeleton"


def _suite_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"_behaviours_{cfg_cluster}.json"


def save_behaviour_suite(project_dir: Path, suite: BehaviourSuite) -> Path:
    """Write `suite` to ``.skeleton/_behaviours_<cluster>.json``; return the path."""
    p = _suite_path(project_dir, suite.cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(suite.to_dict(), indent=2, sort_keys=True),
                 encoding="utf-8")
    return p


def load_behaviour_suite(project_dir: Path, cfg_cluster: str) -> BehaviourSuite:
    """Read a cluster's baked behaviour suite. Missing/corrupt → empty suite
    (behaviours are an ADDITIVE oracle; their absence must never break a run)."""
    p = _suite_path(project_dir, cfg_cluster)
    try:
        return BehaviourSuite.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return BehaviourSuite(cfg_cluster=cfg_cluster)
