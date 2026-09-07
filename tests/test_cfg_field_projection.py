"""A struct field the cluster does not compile is not advertised — not in the
type section, not in the `construct:` hint.

The extractor carried cfg on methods only, so `cluster_filter_type_members`
projected methods and whole records per cluster and never fields. Under
`nightly` the type section listed all seven fields of `ErrorVTable`, the
seventh `#[cfg(all(not(error_generic_member_access), feature = "std"))]
object_backtrace`, and the `construct:` hint spelled out the seven-field
literal; the `impl Error` fill wrote exactly that literal in four of five
attempts and got `E0560: struct ErrorVTable has no field named
object_backtrace` (runs `2065433-a1c7219a`, `2131920-86818224`, 2026-09-07).

The fixture is the extractor's OWN record for `ErrorVTable` from the nightly
layout (`.skeleton/nightly/src/error.rs`, run `2131920`'s tree), computed
2026-09-07 with the rebuilt binary; the clusters are what
`load_baked_clusters` yields from that tree's `_clusters.json` and manifest.
The plan's sentence: six fields under the nightly cfg-set, seven under
stable's. The negative control is the pre-fix record shape — the same fields
with no cfg — which renders seven under both.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain.models import BuildCluster, FieldRecord, TypeRecord
from pipeline_core.prompts.sections import format_type_for_prompt

_FIXTURE = (Path(__file__).parent / "fixtures"
            / "errorvtable_record_nightly_layout_2131920_2026-09-07.json")

# As `load_baked_clusters(tmp/anyhow)` yields them (2026-09-07): the manifest's
# default feature `std` on both; nightly adds the bare cfg, stable excludes it.
NIGHTLY = BuildCluster(
    name="nightly", env_extra={"RUSTFLAGS": "--cfg error_generic_member_access"},
    tarpaulin_exclude_files=[], excluded_cfg=[],
    active_cfg=["error_generic_member_access"], prompt_nightly_rule="",
    is_default=False, default_features=["std"])
STABLE = BuildCluster(
    name="stable", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=["error_generic_member_access"], active_cfg=[],
    prompt_nightly_rule="", is_default=True, default_features=["std"])


def _record() -> TypeRecord:
    return TypeRecord.from_dict(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _pre_fix(rec: TypeRecord) -> TypeRecord:
    """The record shape before fields carried cfg."""
    return dataclasses.replace(rec, fields=tuple(
        FieldRecord(name=f.name, ty=f.ty, is_pub=f.is_pub) for f in rec.fields))


def test_the_recorded_errorvtable_has_seven_fields_and_the_gated_ones_carry_their_cfg() -> None:
    rec = _record()
    assert rec.name == "ErrorVTable" and len(rec.fields) == 7
    by_name = {f.name: f.cfg_predicates for f in rec.fields}
    assert by_name["object_backtrace"] == (
        'all (not (error_generic_member_access) , feature = "std")',)
    assert by_name["object_boxed"] == (
        'any (feature = "std" , not (anyhow_no_core_error))',)
    assert by_name["object_drop"] == ()


def test_six_fields_under_nightly_seven_under_stable() -> None:
    rec = _record()
    [nightly] = cluster_filter_type_members([rec], NIGHTLY)
    [stable] = cluster_filter_type_members([rec], STABLE)
    assert [f.name for f in nightly.fields] == [
        "object_drop", "object_ref", "object_boxed", "object_reallocate_boxed",
        "object_downcast", "object_drop_rest"]
    assert len(stable.fields) == 7
    assert "object_backtrace" in {f.name for f in stable.fields}


def test_the_type_section_and_the_construct_hint_omit_the_gated_field_under_nightly() -> None:
    """Every renderer reads the projected record, so the hint the fill copied
    into its literal no longer names the field the cluster rejects."""
    rec = _record()
    [nightly] = cluster_filter_type_members([rec], NIGHTLY)
    out = format_type_for_prompt(nightly, viewer_file="src/error.rs")
    assert "object_backtrace" not in out
    construct = [ln for ln in out.splitlines() if "construct:" in ln]
    assert construct and construct[0].count(": <") == 6
    [stable] = cluster_filter_type_members([rec], STABLE)
    out_stable = format_type_for_prompt(stable, viewer_file="src/error.rs")
    assert "object_backtrace" in out_stable


def test_negative_control_the_pre_fix_record_renders_seven_under_both() -> None:
    pre = _pre_fix(_record())
    for cluster in (NIGHTLY, STABLE):
        [out] = cluster_filter_type_members([pre], cluster)
        assert len(out.fields) == 7
        assert "object_backtrace" in format_type_for_prompt(out, viewer_file="src/error.rs")


def test_undecidable_and_absent_cfg_keep_the_field_and_no_cluster_changes_nothing() -> None:
    rec = TypeRecord(kind="struct", name="Holder", fields=(
        FieldRecord(name="a", ty="u32"),
        FieldRecord(name="b", ty="u32", cfg_predicates=('target_os = "redox"',)),
        FieldRecord(name="c", ty="u32", cfg_predicates=("not(error_generic_member_access)",)),
    ))
    [out] = cluster_filter_type_members([rec], NIGHTLY)
    assert [f.name for f in out.fields] == ["a", "b"]
    assert cluster_filter_type_members([rec], None) == [rec]


def test_field_record_round_trips_its_cfg_and_defaults_to_ungated() -> None:
    f = FieldRecord(name="x", ty="u8", is_pub=True, cfg_predicates=("unix",))
    assert FieldRecord.from_dict(f.to_dict()) == f
    assert FieldRecord.from_dict({"name": "x", "ty": "u8"}).cfg_predicates == ()
