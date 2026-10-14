"""Layout-outcome storage — persist WHICH connected component t1.5 authored,
gated, or left frozen (the layout re-trigger's precondition), and — for authored components —
the per-uid locate-keys a later re-author needs to find the authored items in
the LIVE skeleton (the baked frozen definitions no longer exist there once a
splice landed, so locating by them fails every attempt).

Written once per t1.5 pass (`application/author_layout_phase.py::run_layout_phase`)
and upserted per component by the layout re-trigger
(`application/retrigger_layout.py`) when it re-authors one. One file per
cfg-cluster mirrors the skeleton's per-cluster layout, matching
`private_surface_store.py`'s convention.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pipeline_core.application.author_layout import (
    AuthorLayoutResult,
    ComponentResult,
    LayoutComponentRecord,
    LayoutOutcome,
    component_outcome,
)

_SKELETON_DIR = ".skeleton"


def _outcomes_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"_layout_outcomes_{cfg_cluster}.json"


def _row(names: tuple[str, ...] | frozenset[str], outcome: LayoutOutcome,
         defs: tuple[tuple[str, str], ...]) -> dict[str, object]:
    return {
        "item_names": sorted(names),
        "outcome": outcome.name,
        "defs": dict(defs),
    }


def save_layout_outcomes(project_dir: Path, result: AuthorLayoutResult) -> Path:
    """Write `result`'s per-component outcomes to
    ``.skeleton/_layout_outcomes_<cluster>.json``."""
    p = _outcomes_path(project_dir, result.cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        _row(c.item_names, component_outcome(c), c.authored_defs)
        for c in result.components
    ]
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def update_layout_outcome(
    project_dir: Path, cfg_cluster: str, component: ComponentResult,
) -> Path | None:
    """Upsert ONE component's row (matched by its item-name set) after a
    re-author changed its outcome — without this, a successfully re-authored
    component stays FROZEN in the store forever, so a later trigger re-fires
    on it and every splice attempt fails to locate the (long-gone) frozen
    defs. Missing/corrupt store → None (additive mechanism, never breaks)."""
    p = _outcomes_path(project_dir, cfg_cluster)
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    key = sorted(component.item_names)
    new_row = _row(component.item_names, component_outcome(component),
                   component.authored_defs)
    replaced = False
    for i, row in enumerate(rows):
        if sorted(row.get("item_names") or []) == key:
            rows[i] = new_row
            replaced = True
            break
    if not replaced:
        rows.append(new_row)
    p.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    return p


def save_layout_records(
    project_dir: Path, cfg_cluster: str,
    records: Sequence[LayoutComponentRecord],
) -> Path:
    """Rewrite a cluster's outcome rows. The caller passes the full list:
    this replaces the file, it does not merge by uid. Used when a stored
    locate key has to become the carried definition the skeleton now holds."""
    p = _outcomes_path(project_dir, cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [_row(r.names, r.outcome, r.defs) for r in records]
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def load_layout_records(
    project_dir: Path, cfg_cluster: str,
) -> list[LayoutComponentRecord]:
    """Read a cluster's per-component outcome records. Missing/corrupt → empty
    list (the layout re-trigger caller is additive — its absence must never break a run;
    it simply finds nothing to retrigger). Rows written before locate-keys
    existed load with empty ``defs`` — an AUTHORED row without defs is simply
    not re-authorable and gets skipped loudly by the consumer."""
    p = _outcomes_path(project_dir, cfg_cluster)
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
        out: list[LayoutComponentRecord] = []
        for row in rows:
            names = frozenset(str(x) for x in (row.get("item_names") or []))
            outcome = LayoutOutcome[str(row.get("outcome", "FROZEN"))]
            defs = tuple(sorted(
                (str(k), str(v)) for k, v in (row.get("defs") or {}).items()))
            out.append(LayoutComponentRecord(names, outcome, defs))
        return out
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return []


def _attempts_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"_layout_retrigger_attempts_{cfg_cluster}.json"


def load_retrigger_attempts(project_dir: Path, cfg_cluster: str) -> dict[str, int]:
    """Per-component retrigger attempt counts (key = sorted item names joined
    by ``|``), so the cap in `application/retrigger_layout.py` survives a
    pipeline restart, not just a single process's memory."""
    p = _attempts_path(project_dir, cfg_cluster)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_retrigger_attempts(
    project_dir: Path, cfg_cluster: str, attempts: dict[str, int],
) -> Path:
    p = _attempts_path(project_dir, cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(attempts, indent=2, sort_keys=True), encoding="utf-8")
    return p


def _rejections_path(project_dir: Path, cfg_cluster: str) -> Path:
    return (Path(project_dir) / _SKELETON_DIR
            / f"_layout_retrigger_rejections_{cfg_cluster}.json")


def load_rejection_notes(
    project_dir: Path, cfg_cluster: str,
) -> dict[str, list[str]]:
    """Per-component checklist of constraints rejected re-authors violated
    (key = sorted item names joined by ``|``). Sibling of the attempts store,
    string-valued and therefore its own file: the attempts store is typed
    dict[str, int] and every reader does arithmetic on it."""
    p = _rejections_path(project_dir, cfg_cluster)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): [str(x) for x in v] for k, v in raw.items()
                if isinstance(v, list)}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def save_rejection_notes(
    project_dir: Path, cfg_cluster: str, notes: dict[str, list[str]],
) -> Path:
    p = _rejections_path(project_dir, cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(notes, indent=2, sort_keys=True), encoding="utf-8")
    return p
