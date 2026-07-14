"""Tests for pipeline_core.adapters.api_surface.ApiSurfaceCapture.

Focused on the pub-items manifest (debt #64): capture() must persist a
STRUCTURED pub-item set (from the same extractor as the type manifest), not
rely on a later regex over the human-readable listing text.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.api_surface import ApiSurfaceCapture
from pipeline_core.domain.models import TypeRecord


def _capture(tmp_path: Path, *, with_manifest_path: bool = True) -> ApiSurfaceCapture:
    def api_surface_path(project_dir: Path, module_name: str) -> Path:
        return project_dir / f"{module_name}.api.md"

    def type_manifest_path(project_dir: Path, module_name: str) -> Path:
        return project_dir / f"{module_name}.types.json"

    def pub_items_manifest_path(project_dir: Path, module_name: str) -> Path:
        return project_dir / f"{module_name}.pub_items.json"

    return ApiSurfaceCapture(
        api_surface_path=api_surface_path,
        type_manifest_path=type_manifest_path,
        condense_to_signatures=lambda text, filename="": text,
        strip_gen_tests_decls=lambda text: text,
        enumerate_types=lambda walk_root: [
            TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True),
        ],
        enumerate_fns=lambda walk_root: [
            {"file": "src/lib.rs", "fn_name": "new", "is_pub": True, "is_exactly_pub": True},
            {"file": "src/lib.rs", "fn_name": "_private", "is_pub": False, "is_exactly_pub": False},
        ],
        is_test_file=lambda p, root: False,
        skip_dir_names=frozenset(),
        log=lambda msg: None,
        pub_items_manifest_path=(pub_items_manifest_path if with_manifest_path else None),
    )


def _make_module(tmp_path: Path) -> Path:
    module_dir = tmp_path / "mymod"
    src = module_dir / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text("pub struct Error;\npub fn new() {}\n")
    return module_dir


def test_capture_writes_pub_items_manifest(tmp_path: Path) -> None:
    module_dir = _make_module(tmp_path)
    cap = _capture(tmp_path)
    cap.capture(tmp_path, module_dir, "mymod")
    manifest_path = tmp_path / "mymod.pub_items.json"
    assert manifest_path.exists()
    items = json.loads(manifest_path.read_text())
    assert ["struct", "Error"] in items
    assert ["fn", "new"] in items
    # Private fn must not appear.
    assert not any(item == ["fn", "_private"] for item in items)


def test_capture_skips_manifest_when_path_fn_not_provided(tmp_path: Path) -> None:
    module_dir = _make_module(tmp_path)
    cap = _capture(tmp_path, with_manifest_path=False)
    cap.capture(tmp_path, module_dir, "mymod")
    assert not (tmp_path / "mymod.pub_items.json").exists()
