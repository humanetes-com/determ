"""Characterization for the relocated disk-preserving fill merge (P6)."""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_merge import (
    merge_fills_preserving_existing,
)


def test_fills_only_unimplemented_bodies() -> None:
    disk = "fn a() { unimplemented!() }\nfn b() { 1 + 1 }\n"
    model = "fn a() { 42 }\nfn b() { 999 }\n"
    out = merge_fills_preserving_existing(disk, model)
    assert "fn a() { 42 }" in out      # stub filled from model
    assert "fn b() { 1 + 1 }" in out   # already-filled disk body preserved
    assert "999" not in out


def test_empty_model_returns_disk_verbatim() -> None:
    disk = "fn a() { unimplemented!() }\n"
    assert merge_fills_preserving_existing(disk, "") == disk


def test_no_fns_returns_disk_verbatim() -> None:
    disk = "// just a comment\nuse std::fmt;\n"
    assert merge_fills_preserving_existing(disk, "fn x() { 1 }") == disk
