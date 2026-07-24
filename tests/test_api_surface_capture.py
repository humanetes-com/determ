"""Tests for pipeline_core.adapters.api_surface.ApiSurfaceCapture.

Focused on the pub-items manifest (debt #64): capture() must persist a
STRUCTURED pub-item set (from the same extractor as the type manifest), not
rely on a later regex over the human-readable listing text.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from pipeline_core.adapters.api_surface import ApiSurfaceCapture
from pipeline_core.domain.models import FieldRecord, TypeRecord


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


def test_pub_items_missing_exactly_pub_key_degrades_to_is_pub() -> None:
    # `is_exactly_pub` is absent from records baked by a pre-field artifact or
    # a stale extractor binary. Absent must mean "pre-field behaviour" on BOTH
    # record and dict shapes: a private item stays private (is_pub is the
    # backstop — gating on the default-True field alone would promote it), and
    # a pub item stays pub (missing dict key must not silently drop it).
    from pipeline_core.domain.pub_items import pub_items_from_records
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Hidden", is_pub=False),
        TypeRecord(file="src/lib.rs", kind="struct", name="Shown", is_pub=True),
    ]
    fns = [
        {"file": "src/lib.rs", "fn_name": "legacy_pub", "is_pub": True},
        {"file": "src/lib.rs", "fn_name": "legacy_priv", "is_pub": False},
    ]
    items = pub_items_from_records(types, fns)
    assert ("struct", "Hidden") not in items
    assert ("struct", "Shown") in items
    assert ("fn", "legacy_pub") in items
    assert ("fn", "legacy_priv") not in items


def _capture_with_fields(tmp_path: Path, *, author_private_fields: bool) -> ApiSurfaceCapture:
    cap = _capture(tmp_path)
    return dataclasses.replace(
        cap,
        enumerate_types=lambda walk_root: [
            TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                       is_exactly_pub=True,
                       definition="pub struct Error { inner: Own<ErrorImpl> }",
                       fields=(FieldRecord(name="inner", ty="Own<ErrorImpl>"),)),
        ],
        author_private_fields=author_private_fields)


def _module_with_wrapper(tmp_path: Path) -> Path:
    module_dir = tmp_path / "wrap"
    src = module_dir / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        "pub struct Error { inner: Own<ErrorImpl> }\npub fn new() {}\n")
    return module_dir


def test_api_surface_elides_model_authored_private_fields(tmp_path: Path) -> None:
    """The artefact is read long after strip. If it still carries the original's
    private fields, every downstream prompt hands the model back the layout the
    authoring stage exists to replace."""
    module_dir = _module_with_wrapper(tmp_path)
    _capture_with_fields(tmp_path, author_private_fields=True).capture(
        tmp_path, module_dir, "wrap")
    text = (tmp_path / "wrap.api.md").read_text()
    assert "pub struct Error {" in text          # head kept — it IS the contract
    assert "Own<ErrorImpl>" not in text          # field choice withheld


def test_api_surface_keeps_fields_when_they_stay_frozen(tmp_path: Path) -> None:
    module_dir = _module_with_wrapper(tmp_path)
    _capture_with_fields(tmp_path, author_private_fields=False).capture(
        tmp_path, module_dir, "wrap")
    assert "Own<ErrorImpl>" in (tmp_path / "wrap.api.md").read_text()
