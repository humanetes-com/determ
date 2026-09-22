"""Integration: a crate type in TYPES_IN_SCOPE carries every trait impl the
crate writes for it, from any file, under a sentence that says the list is
complete — read off the real extractor release binary.

The class, run `450977-5f48b68a` (2026-09-21, iter-1 fill 2): the file-first
prompt rendered crate types with fields and a `construct:` line and NO trait
impls, while external types in the same section carried theirs. The fill had
to decide whether a crate type satisfied a producer's `StdError` bound; the
impl that settles it lived in another file the prompt did not show. The
trace asked about that impl 14 times, concluded 9 times it was "omitted from
the snippet", and emitted the E0277 body for the fourth run running.

The crate is invented: `Wrap<A, B>` declared in one file, a partially
specialised `impl<A> Marker for Wrap<A, Inner>` and a cfg-gated impl in a
second file, a private `mod local` declaring its own `Marker` trait with an
impl for `Inner` (which must NOT read as the root `Marker`), and a foreign
`impl From<u8> for other::Wrap` keyed by the same bare name (which must not
attach). The negative control is the renderer on a record without the field
— the pre-fix wire shape — which prints no impl line at all.

Impls whose self type is one of their own type parameters (`impl<T: Marker>
Blanket for T`) are not an impl of any one record (2026-09-22 review of the
list above: they were dropped while the per-type sentence claimed every
unlisted trait unimplemented). The extractor emits them once beside the
records; only the section renderer, which prints that list, may point the
per-type sentence at it, and a cap that cannot fit the list beside the
first record keeps the record and leaves the list out.

Needs only files and the extractor release binary, so it runs in the
default suite; `scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain.models import BuildCluster, TypeRecord
from pipeline_core.prompts.sections import (
    BLANKET_IMPLS_HEADER,
    TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN,
    TRAIT_IMPLS_HEADER_WITH_BLANKETS,
    blanket_impl_block,
    format_type_for_prompt,
    join_type_blocks,
    render_types_section,
)

pytestmark = [pytest.mark.integration]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")

_WRAP = """\
pub trait Marker {}
pub trait Other {}
pub struct Inner;
pub struct Wrap<A, B> {
    a: A,
    b: B,
}
impl<A, B: Marker> Other for Wrap<A, B> {}
"""

_IMPLS = """\
use crate::wrap::{Inner, Marker, Wrap};
pub trait Extra {}
impl<A> Marker for Wrap<A, Inner> where A: Clone {}
#[cfg(extra_probe)]
impl Extra for Inner {}
mod other {
    pub struct Wrap;
}
impl From<u8> for other::Wrap {
    fn from(_: u8) -> Self {
        other::Wrap
    }
}
mod local {
    pub trait Marker {}
    impl Marker for super::Inner {}
}
impl<T: Marker> Blanket for T {}
#[cfg(extra_probe)]
impl<T: Marker> NightlyBlanket for T {}
"""

ON = BuildCluster(
    name="with-extra", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=[], active_cfg=["extra_probe"], prompt_nightly_rule="",
    is_default=False)
OFF = BuildCluster(
    name="without-extra", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=["extra_probe"], active_cfg=[], prompt_nightly_rule="",
    is_default=True)


@pytest.fixture(scope="module")
def records(tmp_path_factory: pytest.TempPathFactory) -> dict[str, TypeRecord]:
    if not _EXTRACTOR.exists():
        pytest.skip("extractor release binary not built (bash pre_run.sh builds it)")
    root = tmp_path_factory.mktemp("trait_impls")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("mod wrap;\nmod impls;\n")
    (root / "src" / "wrap.rs").write_text(_WRAP)
    (root / "src" / "impls.rs").write_text(_IMPLS)
    types = CliExtractor(_EXTRACTOR).enumerate_types(root) or []
    return {t.name: t for t in types}


def test_premise_the_extractor_emits_the_field(records: dict[str, TypeRecord]) -> None:
    """The renderer prints a list only when the record carries the field; a
    stale binary without it would silently render nothing."""
    assert records["Wrap"].trait_impls is not None
    assert records["Wrap"].blanket_impls is not None


def test_premise_the_blanket_list_is_emitted_once(tmp_path: Path) -> None:
    """The wire carries the type-parameter impls once, beside the records,
    not as a copy on each record."""
    if not _EXTRACTOR.exists():
        pytest.skip("extractor release binary not built (bash pre_run.sh builds it)")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("mod wrap;\nmod impls;\n")
    (tmp_path / "src" / "wrap.rs").write_text(_WRAP)
    (tmp_path / "src" / "impls.rs").write_text(_IMPLS)
    out = subprocess.run(
        [str(_EXTRACTOR), "--enumerate-types", "--input", str(tmp_path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    assert [t["header"] for t in data["blanket_impls"]] == [
        "impl<T: Marker> Blanket for T", "impl<T: Marker> NightlyBlanket for T"]
    assert all("blanket_impls" not in r for r in data["types"])


def test_a_prompt_on_one_file_lists_the_other_files_partial_impl(
        records: dict[str, TypeRecord]) -> None:
    text = render_types_section([records["Wrap"]], viewer_file="src/wrap.rs")
    assert TRAIT_IMPLS_HEADER_WITH_BLANKETS in text
    assert "impl<A> Marker for Wrap<A, Inner> where A: Clone  // src/impls.rs" in text
    assert "impl<A, B: Marker> Other for Wrap<A, B>  // src/wrap.rs" in text
    assert "From<u8>" not in text


def test_a_same_named_local_trait_is_printed_qualified(
        records: dict[str, TypeRecord]) -> None:
    headers = [t.header for t in records["Inner"].trait_impls or ()]
    assert "impl local::Marker for super::Inner" in headers
    assert "impl Marker for super::Inner" not in headers


def test_a_cfg_gated_impl_is_absent_from_the_cluster_that_does_not_compile_it(
        records: dict[str, TypeRecord]) -> None:
    recs = list(records.values())
    on = {r.name: r for r in cluster_filter_type_members(recs, ON)}
    off = {r.name: r for r in cluster_filter_type_members(recs, OFF)}
    assert "impl Extra for Inner" in format_type_for_prompt(on["Inner"])
    assert "impl Extra for Inner" not in format_type_for_prompt(off["Inner"])


def test_negative_control_the_pre_field_record_renders_no_impl(
        records: dict[str, TypeRecord]) -> None:
    """Today's wire shape before the field: no list, so no claim of
    completeness — and no impl the reader could reason from."""
    legacy = dataclasses.replace(records["Wrap"], trait_impls=None)
    text = format_type_for_prompt(legacy, viewer_file="src/wrap.rs")
    assert "trait impls" not in text
    assert "Marker for Wrap" not in text


# The sentence the per-type list used to print. It covers every impl this
# crate writes, so a dropped blanket read as absence.
_PRE_FIX_ABSENCE = (
    "a trait not listed is NOT implemented for it by this crate")


def test_a_blanket_for_a_type_parameter_is_its_own_list(
        records: dict[str, TypeRecord]) -> None:
    """`impl<T: Bound> Trait for T` is not an impl of the named type. The
    section lists it once, and the per-type sentence does not say a trait
    missing from that type's list is unimplemented by this crate."""
    own = format_type_for_prompt(records["Wrap"])
    assert "Blanket for T" not in own
    # On its own the record cannot point at a list it does not print.
    assert "crate blanket list" not in own
    assert TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN in own
    section = render_types_section([records["Wrap"], records["Inner"]])
    assert section.count("impl<T: Marker> Blanket for T") == 1
    assert _PRE_FIX_ABSENCE not in section
    # Without the field there is no blanket list to point at, and the
    # over-claim (a missing trait is not implemented by this crate) is not
    # what either render prints.
    dropped = format_type_for_prompt(
        dataclasses.replace(records["Wrap"], blanket_impls=None))
    assert "Blanket for T" not in dropped
    assert "crate blanket list" not in dropped
    assert _PRE_FIX_ABSENCE not in dropped


def test_the_pointer_is_printed_only_beside_the_list(
        records: dict[str, TypeRecord]) -> None:
    """Every cap, from one that fits nothing to one that fits all: a section
    whose sentence points at the crate blanket list prints that list."""
    recs = [records["Wrap"], records["Inner"]]
    full = render_types_section(recs)
    for cap in range(1, len(full) + 2, 7):
        out = render_types_section(recs, cap=cap)
        assert len(out) <= cap
        if "crate blanket list" in out:
            assert BLANKET_IMPLS_HEADER in out


def test_a_first_record_that_cannot_sit_beside_the_list_is_kept(
        records: dict[str, TypeRecord]) -> None:
    """The first record outranks the blanket list. Negative control: the
    list printed first and the record dropped whole, which left a section
    with no type in it."""
    recs = [records["Wrap"], records["Inner"]]
    blanket = blanket_impl_block(recs)
    plain = format_type_for_prompt(records["Wrap"])
    cap = len(plain) + 1
    assert len(blanket) + 2 + len(plain) > cap
    out = render_types_section(recs, cap=cap)
    assert out.startswith(plain.split("\n")[0]) and "Wrap" in out
    assert BLANKET_IMPLS_HEADER not in out
    assert TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN in out
    pre_fix = join_type_blocks(
        [blanket, *(b.replace(TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN,
                              TRAIT_IMPLS_HEADER_WITH_BLANKETS)
                    for b in (plain,))], cap)
    assert pre_fix == blanket


def test_a_cfg_gated_blanket_leaves_the_cluster_that_does_not_compile_it(
        records: dict[str, TypeRecord]) -> None:
    recs = list(records.values())
    on = {r.name: r for r in cluster_filter_type_members(recs, ON)}
    off = {r.name: r for r in cluster_filter_type_members(recs, OFF)}
    assert "NightlyBlanket" in render_types_section([on["Wrap"]])
    assert "NightlyBlanket" not in render_types_section([off["Wrap"]])
    assert "Blanket for T" in render_types_section([off["Wrap"]])


def test_a_cap_that_lands_inside_the_impl_list_omits_the_list(
        records: dict[str, TypeRecord]) -> None:
    block = format_type_for_prompt(records["Wrap"])
    at = block.index("trait impls (COMPLETE")
    out = join_type_blocks([block], at + 30)
    assert "trait impls (COMPLETE" not in out
    assert "Marker for Wrap" not in out
    assert "not a statement that any impl is absent" in out
    assert len(out) <= at + 30
    assert "Wrap" in out


def test_the_section_cap_drops_whole_records_never_cuts_a_list(
        records: dict[str, TypeRecord]) -> None:
    blocks = [format_type_for_prompt(records[n]) for n in ("Wrap", "Inner")]
    cap = len(blocks[0]) + 10
    out = join_type_blocks(blocks, cap)
    assert out == blocks[0]
