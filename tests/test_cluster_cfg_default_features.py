"""A cluster's evaluation context knows the crate's default-on features.

`cfg_true` is what a cluster adds on TOP of the default build — the list that
reaches cargo, RUSTFLAGS and the skeletoniser. "Is this item compiled here"
is a different question, and ten reconstruct-side call sites answered it with
`cfg_true` alone. `is_off_for_cargo` reads a `feature = "X"` absent from
`default_features` as provably off, so a crate whose default features include
`shim` had the arm the build compiles reported as EXCLUDED and the dead arm
as kept.

The crate here is synthetic (`shim`, a `Widget`): the screen must not know
which crate it is on. The live-records computation is in the addendum to
`plans/2026-09-01.md`.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.structure import load_baked_clusters
from pipeline_core.application.reconstruct.cluster_membership import (
    cfg_dead_type_records,
    cluster_filter_type_methods,
)
from pipeline_core.domain.cfg import CfgSet, cluster_cfg_set
from pipeline_core.domain.models import BuildCluster, MethodRecord, TypeRecord

DEFAULTED = BuildCluster(
    name="stable", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=["probe"], active_cfg=[], prompt_nightly_rule="",
    is_default=True, default_features=["shim"],
)
UNAWARE = BuildCluster(
    name="stable", env_extra={}, tarpaulin_exclude_files=[],
    excluded_cfg=["probe"], active_cfg=[], prompt_nightly_rule="",
    is_default=True,
)

# The standard no-std shim: the live arm is a `use`, so the only ITEM the
# extractor can emit for that name is the arm the build does NOT compile.
DEAD_ARM = TypeRecord(
    file="src/shim.rs", kind="enum", name="Widget",
    definition='#[cfg(not(feature = "shim"))]\npub(crate) enum Widget {}')
LIVE_TYPE = TypeRecord(
    file="src/w.rs", kind="struct", name="Gadget",
    definition="pub struct Gadget { pub n: u32 }",
    methods=(MethodRecord(name="run", sig="fn run(&self)"),))
UNDECIDABLE = TypeRecord(
    file="src/t.rs", kind="struct", name="Target",
    definition='#[cfg(target_os = "redox")]\npub struct Target {}')
# A dead cfg on an inner FIELD gates that field, never the type.
INNER_CFG = TypeRecord(
    file="src/h.rs", kind="struct", name="Holder",
    definition=('pub struct Holder {\n'
                '    #[cfg(not(feature = "shim"))]\n    pub a: u32,\n}'))


def test_a_default_feature_is_decidably_on() -> None:
    """The evaluation context decides a default-on feature true, so the arm
    the build compiles is live and its `not(...)` twin is dead."""
    ctx = cluster_cfg_set(DEFAULTED)
    assert ctx.evaluate('feature = "shim"') is True
    assert ctx.evaluate('not(feature = "shim")') is False
    assert ctx.excludes_for_cargo('feature = "shim"') is False
    assert ctx.excludes_for_cargo('not(feature = "shim")') is True


def test_negative_control_without_defaults_the_answer_is_inverted() -> None:
    """The pre-fix context, which every reconstruct-side site built: the
    compiled arm reads as excluded and the dead arm as kept. Without this
    reproducing, the test above proves nothing."""
    pre = CfgSet.of(set(UNAWARE.cfg_true), set(UNAWARE.cfg_false))
    assert pre.excludes_for_cargo('feature = "shim"') is True
    assert pre.excludes_for_cargo('not(feature = "shim")') is False


def test_a_cfg_dead_type_record_is_not_advertised() -> None:
    """A type whose own leading cfg the cluster decides False is dropped from
    TYPES_IN_SCOPE: a dead arm rendered as the definition asserts something
    false about a name that really resolves elsewhere."""
    recs = [DEAD_ARM, LIVE_TYPE, UNDECIDABLE, INNER_CFG]
    assert cfg_dead_type_records(recs, DEFAULTED) == frozenset({"Widget"})
    names = [r.name for r in cluster_filter_type_methods(recs, DEFAULTED)]
    assert names == ["Gadget", "Target", "Holder"]


def test_an_inner_field_cfg_never_deletes_its_type() -> None:
    """Only the leading attribute block speaks for the item; a dead cfg on a
    field gates the field."""
    assert cfg_dead_type_records([INNER_CFG], DEFAULTED) == frozenset()


def test_undecidable_stays_and_no_cluster_changes_nothing() -> None:
    """Conservative on uncertainty, and inert without a cluster — the two
    properties every caller's branch already relies on."""
    assert cfg_dead_type_records([UNDECIDABLE], DEFAULTED) == frozenset()
    recs = [DEAD_ARM, LIVE_TYPE]
    assert cluster_filter_type_methods(recs, None) == recs
    assert cfg_dead_type_records(recs, None) == frozenset()


def test_rehydration_reads_the_crates_own_default_features(
        tmp_path: Path) -> None:
    """The defaults come from the manifest at load time, not from the frozen
    cluster plan, so a tree baked before this existed gets the corrected cfg
    world with no re-bake."""
    (tmp_path / ".skeleton").mkdir(parents=True)
    (tmp_path / ".skeleton" / "_clusters.json").write_text(json.dumps({
        "module": "m",
        "clusters": [{"name": "stable", "env_extra": {},
                      "tarpaulin_exclude_files": [], "excluded_cfg": [],
                      "active_cfg": [], "prompt_nightly_rule": "",
                      "is_default": True}],
    }))
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "m"\n\n[features]\ndefault = ["shim"]\nshim = []\n')
    (cluster,) = load_baked_clusters(tmp_path)
    assert cluster.default_features == ["shim"]
    assert cluster_cfg_set(cluster).evaluate('not(feature = "shim")') is False


def test_a_manifest_without_defaults_keeps_the_old_context(
        tmp_path: Path) -> None:
    """No `[features] default`, or no manifest at all, is the empty set —
    every predicate stays exactly as undecidable as before."""
    (tmp_path / ".skeleton").mkdir(parents=True)
    (tmp_path / ".skeleton" / "_clusters.json").write_text(json.dumps({
        "module": "m",
        "clusters": [{"name": "stable", "env_extra": {},
                      "tarpaulin_exclude_files": [], "excluded_cfg": [],
                      "active_cfg": [], "prompt_nightly_rule": "",
                      "is_default": True}],
    }))
    (cluster,) = load_baked_clusters(tmp_path)
    assert cluster.default_features == []
    assert cluster_cfg_set(cluster).evaluate('feature = "shim"') is None


# The three prompt-side consumers of the cluster's cfg world that had their
# own open-coded context, and therefore the pre-fix inverted one, after the
# one reader existed: the multi-variant macro section, the cluster-filtered
# skeleton index, and the merged view the post-merge wave fills under.

SHIM_MACRO_FILE = "\n".join([
    '#[cfg(feature = "shim")]',
    "macro_rules! widget {",
    "    () => { Some(std::widget::Widget::capture()) };",
    "}",
    '#[cfg(not(feature = "shim"))]',
    "macro_rules! widget {",
    "    () => { None };",
    "}",
    "",
])

SHIM_SKELETON = "\n".join([
    '#[cfg(feature = "shim")]',
    "pub(crate) use std::widget::Widget;",
    '#[cfg(not(feature = "shim"))]',
    "pub(crate) enum Widget {}",
    "",
])


def test_the_macro_invariants_section_shows_the_default_features_arm(
        tmp_path: Path) -> None:
    """A macro defined once per arm of a default-on feature renders its
    compiled expansion, so a fill learns what the shim's alias stands for
    instead of reading every arm in the file block."""
    from pipeline_core.application.reconstruct.macro_invariants import (
        compute_cluster_macro_invariants,
    )
    (tmp_path / "shim.rs").write_text(SHIM_MACRO_FILE)
    section = compute_cluster_macro_invariants(tmp_path, DEFAULTED)
    assert "std::widget::Widget::capture()" in section
    assert "None" not in section
    # Negative control: the context without defaults decides neither arm.
    assert compute_cluster_macro_invariants(tmp_path, UNAWARE) == ""


def test_the_cluster_skeleton_index_drops_the_dead_shim_arm() -> None:
    """The cluster-filtered index elides the item the build does not compile
    and keeps the `use` it does."""
    from pipeline_core.prompts.skeleton_index import (
        format_skeleton_index_for_cluster,
    )
    files = [("src/shim.rs", SHIM_SKELETON)]
    fixed = format_skeleton_index_for_cluster(files, DEFAULTED)
    assert "enum Widget" not in fixed
    assert "use std::widget::Widget" in fixed
    # Negative control: without the defaults the dead arm is undecidable and
    # therefore kept.
    assert "enum Widget" in format_skeleton_index_for_cluster(files, UNAWARE)


def test_the_merged_view_carries_the_crates_default_features() -> None:
    """The post-merge wave fills under the merged identity; its cfg world
    must decide the default-on feature exactly as every member does."""
    from pipeline_core.domain.cluster_plan import merged_view_cluster
    other = BuildCluster(
        name="feature/probe", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=[], active_cfg=["probe"], prompt_nightly_rule="",
        is_default=False, default_features=["shim"],
    )
    merged = merged_view_cluster([DEFAULTED, other])
    assert merged is not None
    assert merged.default_features == ["shim"]
    assert cluster_cfg_set(merged).evaluate('not(feature = "shim")') is False
    # Negative control: members built before the field existed give the
    # pre-fix merged world.
    pre = merged_view_cluster([UNAWARE, UNAWARE])
    assert pre is not None
    assert cluster_cfg_set(pre).excludes_for_cargo('feature = "shim"') is True
