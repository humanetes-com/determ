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

Needs only files and the extractor release binary, so it runs in the
default suite; `scripts/run_integration.sh` runs it with the rest.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain.models import BuildCluster, TypeRecord
from pipeline_core.prompts.sections import (
    TRAIT_IMPLS_HEADER,
    format_type_for_prompt,
    join_type_blocks,
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


def test_a_prompt_on_one_file_lists_the_other_files_partial_impl(
        records: dict[str, TypeRecord]) -> None:
    text = format_type_for_prompt(records["Wrap"], viewer_file="src/wrap.rs")
    assert TRAIT_IMPLS_HEADER in text
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


def test_the_section_cap_drops_whole_records_never_cuts_a_list(
        records: dict[str, TypeRecord]) -> None:
    blocks = [format_type_for_prompt(records[n]) for n in ("Wrap", "Inner")]
    cap = len(blocks[0]) + 10
    out = join_type_blocks(blocks, cap)
    assert out == blocks[0]
