"""Pins for the private-surface marks store (baked artifact)."""
from __future__ import annotations

import dataclasses
import json

from pipeline_core.adapters.private_surface_store import (
    load_private_surface,
    save_private_surface,
)
from pipeline_core.domain.private_surface import (
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)


def _surface(cluster="stable"):
    return PrivateSurface(
        cfg_cluster=cluster,
        replaceable=(
            PrivateSurfaceItem(
                kind=SurfaceItemKind.STRUCT, name="ErrorImpl", file="src/error.rs",
                definition="struct ErrorImpl { vtable: &'static ErrorVTable }",
                references=frozenset({"ErrorVTable"}),
            ),
            PrivateSurfaceItem(
                kind=SurfaceItemKind.STRUCT, name="ErrorVTable", file="src/error.rs",
                definition="struct ErrorVTable {}", references=frozenset(),
            ),
        ),
        frozen_public=frozenset({"Error"}),
        public_referenced=frozenset({"ErrorImpl"}),
        public_direct_refs={"Error": frozenset({"ErrorImpl"})},
    )


def test_round_trip_preserves_surface(tmp_path):
    save_private_surface(tmp_path, _surface())
    got = load_private_surface(tmp_path, "stable")
    assert got.cfg_cluster == "stable"
    assert {i.name for i in got.replaceable} == {"ErrorImpl", "ErrorVTable"}
    assert got.frozen_public == frozenset({"Error"})
    impl = next(i for i in got.replaceable if i.name == "ErrorImpl")
    assert impl.references == frozenset({"ErrorVTable"})
    assert impl.kind is SurfaceItemKind.STRUCT
    assert got.public_referenced == frozenset({"ErrorImpl"})
    assert got.public_direct_refs == {"Error": frozenset({"ErrorImpl"})}


def test_missing_file_is_empty_surface_not_error(tmp_path):
    got = load_private_surface(tmp_path, "nightly")
    assert got.is_empty
    assert got.cfg_cluster == "nightly"


def test_per_cluster_isolation(tmp_path):
    save_private_surface(tmp_path, _surface("stable"))
    save_private_surface(tmp_path, PrivateSurface(
        cfg_cluster="nightly", replaceable=(), frozen_public=frozenset()))
    assert not load_private_surface(tmp_path, "stable").is_empty
    assert load_private_surface(tmp_path, "nightly").is_empty


def test_freeze_scope_stamp_round_trips(tmp_path):
    s = dataclasses.replace(_surface(), author_private_fields=True)
    save_private_surface(tmp_path, s)
    assert load_private_surface(tmp_path, "stable").author_private_fields is True


def test_surface_baked_before_the_stamp_existed_reads_as_frozen(tmp_path):
    save_private_surface(tmp_path, _surface())
    p = tmp_path / ".skeleton" / "_private_surface_stable.json"
    d = json.loads(p.read_text())
    del d["author_private_fields"]
    p.write_text(json.dumps(d))
    assert load_private_surface(tmp_path, "stable").author_private_fields is False
