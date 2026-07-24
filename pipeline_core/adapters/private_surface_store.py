"""Private-surface marks storage — persist the public-frozen vs
private-replaceable classification as a baked artifact under ``.skeleton/`` (F11
M1).

Written ONCE in the pre-strip bake (Hard Rule 11 — the marks are derived from a
type's *visibility*, an observable structural fact, never from a body), read by
the t1.5 layout-authoring phase to know which private definitions the model may
replace and which public signatures stay frozen. One file per cfg-cluster
mirrors the skeleton's per-cluster layout.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)

_SKELETON_DIR = ".skeleton"


def _surface_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / _SKELETON_DIR / f"_private_surface_{cfg_cluster}.json"


def _item_to_dict(it: PrivateSurfaceItem) -> dict[str, Any]:
    return {
        "kind": it.kind.name,
        "name": it.name,
        "file": it.file,
        "definition": it.definition,
        "references": sorted(it.references),
    }


def _item_from_dict(d: dict[str, Any]) -> PrivateSurfaceItem:
    return PrivateSurfaceItem(
        kind=SurfaceItemKind[str(d["kind"])],
        name=str(d.get("name", "")),
        file=str(d.get("file", "")),
        definition=str(d.get("definition", "")),
        references=frozenset(str(x) for x in (d.get("references") or [])),
    )


def save_private_surface(project_dir: Path, surface: PrivateSurface) -> Path:
    """Write `surface` to ``.skeleton/_private_surface_<cluster>.json``."""
    p = _surface_path(project_dir, surface.cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cfg_cluster": surface.cfg_cluster,
        "replaceable": [_item_to_dict(i) for i in surface.replaceable],
        "frozen_public": sorted(surface.frozen_public),
        "public_referenced": sorted(surface.public_referenced),
        "public_direct_refs": {
            name: sorted(refs)
            for name, refs in surface.public_direct_refs.items()},
        "coupled": [sorted(g) for g in surface.coupled],
        "author_private_fields": surface.author_private_fields,
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def load_private_surface(project_dir: Path, cfg_cluster: str) -> PrivateSurface:
    """Read a cluster's baked private surface. Missing/corrupt → empty surface
    (t1.5 is ADDITIVE — its absence must never break a run; the run proceeds on
    the frozen skeleton exactly as before F11)."""
    p = _surface_path(project_dir, cfg_cluster)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return PrivateSurface(
            cfg_cluster=str(d.get("cfg_cluster", cfg_cluster)),
            replaceable=tuple(_item_from_dict(x) for x in (d.get("replaceable") or [])),
            frozen_public=frozenset(str(x) for x in (d.get("frozen_public") or [])),
            public_referenced=frozenset(
                str(x) for x in (d.get("public_referenced") or [])),
            public_direct_refs={
                str(name): frozenset(str(x) for x in (refs or []))
                for name, refs in (d.get("public_direct_refs") or {}).items()},
            coupled=tuple(
                frozenset(str(x) for x in (g or []))
                for g in (d.get("coupled") or [])),
            # Absent key ⇔ False: a surface baked before field authoring
            # existed was, by definition, baked with it off.
            author_private_fields=bool(d.get("author_private_fields", False)),
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return PrivateSurface(cfg_cluster=cfg_cluster, replaceable=(),
                              frozen_public=frozenset())
