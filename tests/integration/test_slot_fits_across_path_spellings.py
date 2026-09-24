"""Integration: a fn-pointer field in TYPES_IN_SCOPE names the crate fns that
fit it, read off the real extractor release binary through the structure
reader every fill route uses, however each side spells the paths.

The class, measured 2026-09-24 (`scripts/prompt_sufficiency_census.py`, fills
since 2026-09-01): 143 compile errors quoted back to retries named a fn that
does not exist, nearly all fn-pointer table slots written as if a fn of the
slot's name existed (`object_drop_rest` 24 times). The prompt listed the
table's fields and every fn and never said which fits which. Computing that
list on the live `stable` tree exposed the second half: the table's fields
are printed with full paths (`crate::ptr::Own<crate::error::ErrorImpl>`)
while most slot fns declare `Own<ErrorImpl>`, and the signature comparison
the installable offer already used matched nothing for three of seven slots.

The crate is invented: a `Table` whose fn-pointer fields are written with
crate paths, slot fns declared with short paths, one generic over its
argument, one associated fn, one trait declaration (never a candidate), and
one cfg-gated fn that a cluster excluding its cfg must drop. The negative
control is the pre-change comparison — exact type text — on the same
records.

Needs only files and the extractor release binary, so it runs in the
default suite; `scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain.installable_fns import fn_decl_shape, fn_pointer_shape
from pipeline_core.domain.models import BuildCluster, TypeRecord
from pipeline_core.prompts.sections import render_types_section

pytestmark = [pytest.mark.integration]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")

_CELL = """\
pub struct Slot<T>(pub T);
pub struct Item;
"""

_TABLE = """\
use crate::cell::{Item, Slot};
use core::any::TypeId;

pub(crate) struct Table {
    release: unsafe fn(crate::cell::Slot<crate::cell::Item>),
    release_rest: unsafe fn(crate::cell::Slot<crate::cell::Item>, core::any::TypeId),
    describe: fn(&crate::cell::Item) -> u32,
}

unsafe fn release_whole<T>(s: Slot<Item>) { unimplemented!() }
unsafe fn release_front<T>(s: Slot<Item>, id: TypeId) { unimplemented!() }
fn release_generic<X>(s: X, id: TypeId) { unimplemented!() }
#[cfg(gate_probe)]
unsafe fn release_gated(s: Slot<Item>, id: TypeId) { unimplemented!() }
fn unrelated(n: u8) -> u8 { n }

pub(crate) struct Maker;
impl Maker {
    unsafe fn release_assoc(s: Slot<Item>, id: TypeId) { unimplemented!() }
}

pub trait Hook {
    fn release_decl(s: Slot<Item>, id: TypeId);
}
"""

WITH = BuildCluster(
    name="with-gate", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=[], active_cfg=["gate_probe"], prompt_nightly_rule="",
    is_default=False)
WITHOUT = BuildCluster(
    name="without-gate", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=["gate_probe"], active_cfg=[], prompt_nightly_rule="",
    is_default=True)


@pytest.fixture(scope="module")
def table(tmp_path_factory: pytest.TempPathFactory) -> TypeRecord:
    if not _EXTRACTOR.exists():
        pytest.skip("extractor release binary not built (bash pre_run.sh builds it)")
    root = tmp_path_factory.mktemp("slot_fits")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("mod cell;\nmod table;\n")
    (root / "src" / "cell.rs").write_text(_CELL)
    (root / "src" / "table.rs").write_text(_TABLE)
    recs = CliStructureReader(CliExtractor(_EXTRACTOR)).enumerate_types(root) or []
    return next(r for r in recs if r.name == "Table")


def _fits(rec: TypeRecord, field: str) -> list[str]:
    f = next(f for f in rec.fields if f.name == field)
    assert f.fits is not None, f"{field} read without the crate's fns"
    return [c.name for c in f.fits]


def test_premise_the_two_sides_are_spelled_differently(table: TypeRecord) -> None:
    """The extractor prints the field with its crate paths, and the
    declaration keeps the source's short ones — so an exact comparison of
    the two cannot match, which is what the pre-change rule did."""
    release_rest = next(f for f in table.fields if f.name == "release_rest")
    field = fn_pointer_shape(release_rest.ty)
    decl = fn_decl_shape("unsafe fn release_front<T>(s: Slot<Item>, id: TypeId)")
    assert field is not None and decl is not None
    assert "crate" in field[1][0] and field[1] != decl[1]


def test_every_fn_pointer_field_names_the_fns_that_fit(table: TypeRecord) -> None:
    assert _fits(table, "release") == ["release_whole<T>"]
    assert _fits(table, "release_rest") == [
        "release_front<T>", "release_generic<X>", "release_gated",
        "Maker::release_assoc"]
    assert _fits(table, "describe") == []


def test_a_cluster_that_does_not_compile_a_candidate_drops_it(table: TypeRecord) -> None:
    (on,) = cluster_filter_type_members([table], WITH)
    (off,) = cluster_filter_type_members([table], WITHOUT)
    assert "release_gated" in _fits(on, "release_rest")
    assert "release_gated" not in _fits(off, "release_rest")


def test_the_type_section_prints_each_list_and_says_none(table: TypeRecord) -> None:
    (off,) = cluster_filter_type_members([table], WITHOUT)
    text = render_types_section([off], viewer_file="src/table.rs")
    assert "COMPLETE list of crate fns it can hold" in text
    assert ("      fits: release_front<T>, release_generic<X>, "
            "Maker::release_assoc") in text
    assert "      fits: NONE — no crate fn is declared" in text
    assert "release_decl" not in text.split("fits:", 1)[1]


def test_negative_control_the_exact_comparison_finds_nothing(table: TypeRecord) -> None:
    """The pre-change rule, on the same records: exact type text. It finds
    no fn for either slot the crate can fill."""
    decls = ["unsafe fn release_whole<T>(s: Slot<Item>)",
             "unsafe fn release_front<T>(s: Slot<Item>, id: TypeId)"]
    for field in ("release", "release_rest"):
        ty = next(f.ty for f in table.fields if f.name == field)
        want = fn_pointer_shape(ty)
        assert want is not None
        exact = [d for d in decls
                 if (s := fn_decl_shape(d)) is not None
                 and s[1] == want[1] and s[2] == want[2]]
        assert exact == []
