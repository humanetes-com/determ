"""cfg-aware expected-test count — the cargo-faithful fix for the 2026-06-16
hardcrate over-count bug (technical_debt #15).

A `#[cfg(extra_cap)]` test whose predicate is in NEITHER cfg_true NOR cfg_false
(a custom `--cfg`/feature toggle that defined no cluster) used to evaluate
UNDECIDABLE → counted, but cargo (default build) never sets `extra_cap` → it
doesn't run → "16 reported / 18 expected" false-incomplete, which drove the fix
loop to defeat the cfg gating. `_cfg_false_for_cargo` now treats custom-off cfgs
as cargo does (OFF) while leaving builtin/target cfgs undecidable (never
over-excludes — FR-018)."""
from __future__ import annotations

import pipeline
from pipeline_core.domain import cfg as _cfg


def _excludes(pred, cfg_true=frozenset(), cfg_false=frozenset(), defaults=frozenset()):
    return _cfg.CfgSet.of(set(cfg_true), set(cfg_false), frozenset(defaults)).excludes_for_cargo(pred)


def _leaf_atoms(pred):
    return {a.normalized for a in _cfg.parse_cfg_pred(pred).atoms()}


def test_custom_off_toggle_is_excluded_like_cargo() -> None:
    # THE bug: extra_cap in neither set → cargo default build never runs it.
    assert _excludes("extra_cap") is True
    # the hardcrate's exact predicate (bare flag OR matching feature) → off.
    assert _excludes('any(extra_cap, feature = "extra_cap")') is True
    # already-excluded predicate stays excluded (microcrate default cluster path).
    assert _excludes("extra_cap", cfg_false={"extra_cap"}) is True


def test_does_not_over_exclude_what_cargo_runs() -> None:
    # `#[cfg(not(extra_cap))]` RUNS in the default build → must be counted.
    assert _excludes("not(extra_cap)") is False
    # builtin/target cfgs are undecidable to us but cargo may run them → counted.
    assert _excludes("unix") is False
    assert _excludes('target_os = "linux"') is False
    # a default feature is ON even when the cluster sets omit it → counted.
    assert _excludes('feature = "std"', defaults={"std"}) is False
    # a cfg enabled in THIS cluster → counted.
    assert _excludes("extra_cap", cfg_true={"extra_cap"}) is False


def test_non_default_feature_off_is_excluded() -> None:
    assert _excludes('feature = "async"') is True
    assert _excludes('feature = "async"', defaults={"std"}) is True


def test_leaf_atoms_flattens_operators() -> None:
    assert _leaf_atoms('any(extra_cap, feature = "extra_cap")') == {
        "extra_cap", 'feature="extra_cap"'}
    assert _leaf_atoms("not(all(unix, nightly))") == {"unix", "nightly"}


def test_count_test_fns_excludes_custom_gated_only() -> None:
    txt = (
        "#[cfg(extra_cap)]\n#[test]\nfn gated() {}\n"      # custom off → dropped
        "#[test]\nfn plain() {}\n"                          # ungated → kept
        "#[cfg(not(extra_cap))]\n#[test]\nfn default_only() {}\n"  # runs in default → kept
        "#[cfg(unix)]\n#[test]\nfn on_unix() {}\n"          # builtin → kept
    )
    assert pipeline._count_test_fns_cfg_aware(txt, set(), set()) == 3
    # when extra_cap IS the active cluster, the gated test counts and not() drops.
    assert pipeline._count_test_fns_cfg_aware(
        txt, {"extra_cap"}, {"feature=\"extra_cap\""}) == 3


def test_file_level_cfg_gate_excludes_whole_file() -> None:
    # A file-level `#![cfg(P)]` with P false for the cluster gates EVERY test in
    # the file (cargo compiles it to an empty binary). Regression for the
    # hardcrate `tests/cap.rs` phantom: expected-count must match cargo-runs,
    # else the cluster is permanently 'incomplete' and the fix loop churns.
    txt = (
        "#![cfg(any(extra_cap, feature = \"extra_cap\"))]\n"
        "#[test]\nfn a() {}\n#[test]\nfn b() {}\n"
    )
    # stable cluster: extra_cap + feature both false → whole file excluded → 0.
    assert pipeline._count_test_fns_cfg_aware(
        txt, set(), {"extra_cap", "feature = \"extra_cap\""}) == 0
    # extra_cap cluster: predicate true → both tests counted.
    assert pipeline._count_test_fns_cfg_aware(
        txt, {"extra_cap", "feature = \"extra_cap\""}, set()) == 2
    # no top-level gate → unaffected (both counted).
    assert pipeline._count_test_fns_cfg_aware(
        "#[test]\nfn a() {}\n#[test]\nfn b() {}\n", set(),
        {"extra_cap", "feature = \"extra_cap\""}) == 2


# --- ratchet sweep 2026-07-04 ---------------------------------------------- #

def test_count_test_fns_comment_lines_in_attr_block_skipped() -> None:
    # Comment lines inside the attribute block are skipped by the backtrack,
    # so the cfg gate above them still applies.
    txt = ("#[cfg(extra_cap)]\n"
           "// a comment between attrs\n"
           "#[test]\nfn gated() {}\n")
    assert pipeline._count_test_fns_cfg_aware(txt, set(), set()) == 0


def test_count_test_fns_in_records_gating() -> None:
    from pipeline_core.domain.test_count import count_test_fns_in_records
    recs = [
        {"is_test": True, "cfg_predicates": []},
        {"is_test": False, "cfg_predicates": []},          # not a test
        {"is_test": True, "cfg_predicates": ["extra_cap"]},  # custom off → dropped
        {"is_test": True},                                  # no predicates key
    ]
    assert count_test_fns_in_records(recs, set(), set()) == 2
