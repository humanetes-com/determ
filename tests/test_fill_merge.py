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


def test_count_stub_bodies() -> None:
    from pipeline_core.application.reconstruct.fill_merge import count_stub_bodies
    text = ("fn a() { unimplemented!() }\n"
            "fn b() { 1 + 1 }\n"
            "fn c() {\n    unimplemented!()\n}\n")
    assert count_stub_bodies(text) == 2
    assert count_stub_bodies("// no fns\n") == 0


def test_backfill_stub_bodies_earliest_donor_wins() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { unimplemented!() }\nfn b() { 5 }\n"
    donor1 = "fn a() { 1 }\n"
    donor2 = "fn a() { 2 }\nfn b() { 999 }\n"
    out, n = backfill_stub_bodies(target, [donor1, donor2])
    assert n == 1
    assert "fn a() { 1 }" in out          # earliest donor won
    assert "fn b() { 5 }" in out          # filled body never overwritten
    assert "999" not in out and "fn a() { 2 }" not in out


def test_backfill_stub_bodies_no_stubs_short_circuits() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { 1 }\n"
    out, n = backfill_stub_bodies(target, ["fn a() { 2 }\n"])
    assert (out, n) == (target, 0)


def test_backfill_stub_bodies_donor_stub_adds_nothing() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { unimplemented!() }\n"
    out, n = backfill_stub_bodies(target, ["fn a() { unimplemented!() }\n"])
    assert n == 0
    assert "unimplemented!()" in out
