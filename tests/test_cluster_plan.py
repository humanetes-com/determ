"""Unit tests for the P5 cluster planner (`pipeline_core.domain.cluster_plan`) —
the pure derivation of `BuildCluster`s from cfg facts (Hard Rule 8). Drives the
classification rules directly on constructed facts, no extractor/Cargo.toml I/O."""
from __future__ import annotations

from pipeline_core.domain.cfg import CfgAtom
from pipeline_core.domain.cluster_plan import (
    NOISE_THRESHOLD,
    cluster_name_for_custom,
    mod_gate_map,
    plan_clusters,
    predicate_counts,
)
from pipeline_core.domain.models import CfgOccurrence, ModRecord


def _occ(*atoms: CfgAtom) -> CfgOccurrence:
    return CfgOccurrence(file="src/lib.rs", atoms=atoms)


def test_predicate_counts_keys_by_cluster_key() -> None:
    occs = [
        _occ(CfgAtom("feature", '"async"')),
        _occ(CfgAtom("nightly")),
        _occ(CfgAtom("feature", '"async"'), CfgAtom("target_os", '"linux"')),
    ]
    counts = predicate_counts(occs)
    assert counts == {"async": 2, "nightly": 1}      # target_os contributes nothing


def test_mod_gate_map_only_simple_leaf_gates_in_crate_root() -> None:
    recs = [
        ModRecord(name="extra", cfg_predicates=("extra_cap",), declared_in="lib.rs"),
        ModRecord(name="combo", cfg_predicates=("all(a, b)",), declared_in="lib.rs"),
        ModRecord(name="other", cfg_predicates=("nightly",), declared_in="helper.rs"),
    ]
    gates = mod_gate_map(recs)
    assert gates == {"extra_cap": {"src/extra.rs"}}   # combo (non-leaf) + non-root dropped


def test_custom_cfg_always_clusters_and_is_active() -> None:
    counts = {"extra_cap": 1}                          # below noise floor, but custom → cluster
    clusters = plan_clusters(counts, {}, cargo_features=[], default_features=[],
                             feature_gate_cfgs=[])
    assert [c.name for c in clusters] == ["stable", "extra_cap"]
    custom = clusters[1]
    assert custom.active_cfg == ["extra_cap"]
    assert custom.env_extra == {"RUSTFLAGS": "--cfg extra_cap"}
    assert clusters[0].excluded_cfg == ["extra_cap"]  # stable excludes the custom cfg


def test_declared_feature_below_threshold_is_noise() -> None:
    counts = {"async": NOISE_THRESHOLD - 1}
    clusters = plan_clusters(counts, {}, cargo_features=["async"], default_features=[],
                             feature_gate_cfgs=[])
    assert [c.name for c in clusters] == ["stable"]   # below floor → no cluster


def test_declared_feature_above_threshold_clusters() -> None:
    counts = {"async": NOISE_THRESHOLD}
    clusters = plan_clusters(counts, {}, cargo_features=["async"], default_features=[],
                             feature_gate_cfgs=[])
    assert [c.name for c in clusters] == ["stable", "feature/async"]
    assert clusters[1].active_cfg == ["async"]
    assert clusters[1].env_extra == {}                # a feature, not a --cfg toggle


def test_default_feature_never_clusters() -> None:
    counts = {"std": 100}
    clusters = plan_clusters(counts, {}, cargo_features=["std"], default_features=["std"],
                             feature_gate_cfgs=[])
    assert [c.name for c in clusters] == ["stable"]   # default-on → always compiled


def test_build_target_predicate_never_clusters() -> None:
    counts = {"unix": 50, "target_os": 50}
    clusters = plan_clusters(counts, {}, cargo_features=[], default_features=[],
                             feature_gate_cfgs=[])
    assert [c.name for c in clusters] == ["stable"]


def test_nightly_feature_gate_flips_requires_nightly() -> None:
    counts = {"provide_any": 1}
    clusters = plan_clusters(counts, {}, cargo_features=[], default_features=[],
                             feature_gate_cfgs=["provide_any"])
    nightly = clusters[1]
    assert nightly.name == "nightly" and nightly.requires_nightly is True


def test_gated_files_attach_to_stable_excluded_cfg() -> None:
    counts = {"extra_cap": 1}
    gates = {"extra_cap": {"src/extra.rs"}}
    clusters = plan_clusters(counts, gates, cargo_features=[], default_features=[],
                             feature_gate_cfgs=[])
    assert clusters[0].gated_files == {"src/extra.rs"}    # stable skips the gated file
    assert clusters[1].gated_files == set()               # the cfg's own cluster compiles it


def test_cluster_name_for_custom_known_and_sanitized() -> None:
    assert cluster_name_for_custom("provide_any") == "nightly"
    assert cluster_name_for_custom("io_uring_support") == "io_uring"


# --- #23: build-script reachability prunes phantom clusters ---

def test_bs_declared_but_unset_custom_cfg_is_dropped() -> None:
    # anyhow shape: 4 cfgs DECLARED via rustc-check-cfg but set on no available
    # toolchain (old-rustc / probe-only / CI-only) → no clusters; egma is set
    # under nightly → the one real extra cluster.
    counts = {"anyhow_no_core_error": 9, "anyhow_build_probe": 8,
              "anyhow_nightly_testing": 7, "error_generic_member_access": 6}
    declared = frozenset({"anyhow_no_core_error", "anyhow_build_probe",
                          "anyhow_nightly_testing", "error_generic_member_access"})
    clusters = plan_clusters(
        counts, {}, cargo_features=[], default_features=[], feature_gate_cfgs=[],
        bs_set_default=frozenset(), bs_set_nightly=frozenset({"error_generic_member_access"}),
        bs_declared=declared)
    assert [c.name for c in clusters] == ["stable", "nightly"]
    assert clusters[1].active_cfg == ["error_generic_member_access"]
    assert clusters[1].requires_nightly is True       # set-only-under-nightly ⇒ nightly


def test_bs_set_under_default_cfg_is_intrinsic_not_a_cluster() -> None:
    # On an OLD rustc build.rs sets no_core_error by default → it's part of stable,
    # never a separate toggle.
    counts = {"anyhow_no_core_error": 9}
    clusters = plan_clusters(
        counts, {}, cargo_features=[], default_features=[], feature_gate_cfgs=[],
        bs_set_default=frozenset({"anyhow_no_core_error"}),
        bs_declared=frozenset({"anyhow_no_core_error"}))
    assert [c.name for c in clusters] == ["stable"]
    assert clusters[0].excluded_cfg == []


def test_bs_unmentioned_custom_cfg_stays_a_cluster() -> None:
    # A `--cfg` the build script never mentions = a user knob → keep (pre-#23).
    counts = {"extra_cap": 1}
    clusters = plan_clusters(
        counts, {}, cargo_features=[], default_features=[], feature_gate_cfgs=[],
        bs_declared=frozenset({"some_other_cfg"}))
    assert [c.name for c in clusters] == ["stable", "extra_cap"]
