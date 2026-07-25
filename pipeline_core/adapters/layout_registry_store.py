"""Crate-wide authored-layout registry — uid → the ONE definition this crate
uses for that private item, whichever cfg-cluster authored it first.

Why crate-level and not per-cluster (unlike `layout_outcome_store.py`): the
merged crate has exactly one definition of each private struct/fn. When a later
cluster is seeded with an earlier cluster's filled bodies, those bodies read the
earlier cluster's INVENTED field names — so a second, independently authored
layout for the same type makes every seeded body reference a field that does not
exist and the cluster never compiles. Both writers of layout (the t1.5 phase and
the mid-run re-trigger) therefore agree through this single file rather than
each holding their own copy.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.authored_layout import AuthoredItem

_REGISTRY_PATH = Path(".skeleton") / "_layout_registry.json"


def registry_path(project_dir: Path) -> Path:
    return Path(project_dir) / _REGISTRY_PATH


def load_registry(project_dir: Path) -> dict[str, AuthoredItem]:
    """The crate's fixed definitions, or {} when nothing has been authored yet.
    A malformed/absent file reads as empty — adoption is an optimisation over
    re-authoring, never a hard dependency."""
    p = registry_path(project_dir)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, AuthoredItem] = {}
    for uid, row in raw.items():
        if isinstance(row, dict) and row.get("definition"):
            out[str(uid)] = AuthoredItem(
                name=str(row.get("name", uid)), kind=str(row.get("kind", "")),
                definition=str(row["definition"]))
    return out


def save_registry(project_dir: Path, registry: dict[str, AuthoredItem]) -> Path:
    p = registry_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {uid: {"name": i.name, "kind": i.kind, "definition": i.definition}
         for uid, i in registry.items()}, indent=2, sort_keys=True),
        encoding="utf-8")
    return p


def upsert_registry(
    project_dir: Path, items: tuple[tuple[str, AuthoredItem], ...],
) -> Path | None:
    """Record definitions a re-author just committed, OVERWRITING any previous
    entry for those uids — a re-trigger is a deliberate redesign of the crate's
    layout, so it becomes the definition later clusters adopt. Clusters that
    already adopted the superseded one keep it: their trees are already spliced,
    and this store is the only place the discrepancy is visible."""
    if not items:
        return None
    reg = load_registry(project_dir)
    reg.update(dict(items))
    return save_registry(project_dir, reg)
