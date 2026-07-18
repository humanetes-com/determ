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

from pipeline_core.domain import cfg as _cfg
from pipeline_core.domain.test_count import count_test_fns_cfg_aware


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
    assert count_test_fns_cfg_aware(txt, set(), set()) == 3
    # when extra_cap IS the active cluster, the gated test counts and not() drops.
    assert count_test_fns_cfg_aware(
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
    assert count_test_fns_cfg_aware(
        txt, set(), {"extra_cap", "feature = \"extra_cap\""}) == 0
    # extra_cap cluster: predicate true → both tests counted.
    assert count_test_fns_cfg_aware(
        txt, {"extra_cap", "feature = \"extra_cap\""}, set()) == 2
    # no top-level gate → unaffected (both counted).
    assert count_test_fns_cfg_aware(
        "#[test]\nfn a() {}\n#[test]\nfn b() {}\n", set(),
        {"extra_cap", "feature = \"extra_cap\""}) == 2


# --- ratchet sweep 2026-07-04 ---------------------------------------------- #

def test_count_test_fns_comment_lines_in_attr_block_skipped() -> None:
    # Comment lines inside the attribute block are skipped by the backtrack,
    # so the cfg gate above them still applies.
    txt = ("#[cfg(extra_cap)]\n"
           "// a comment between attrs\n"
           "#[test]\nfn gated() {}\n")
    assert count_test_fns_cfg_aware(txt, set(), set()) == 0


def test_count_test_fns_in_records_gating() -> None:
    from pipeline_core.domain.test_count import count_test_fns_in_records
    recs = [
        {"is_test": True, "cfg_predicates": []},
        {"is_test": False, "cfg_predicates": []},          # not a test
        {"is_test": True, "cfg_predicates": ["extra_cap"]},  # custom off → dropped
        {"is_test": True},                                  # no predicates key
    ]
    assert count_test_fns_in_records(recs, set(), set()) == 2  # type: ignore[arg-type]


def test_undeclared_gen_tests_sibling_still_counts(tmp_path) -> None:
    """A sibling whose parent lost its `mod gen_tests_N;` decl is a HIDDEN
    test file — it must still raise the expected count (never let the count
    shrink in the same stroke that hid the tests). A sibling declared behind
    a cluster gate that evaluates False is deliberate exclusion, not hiding."""
    from pipeline_core.application.test_count import (
        TestCountDeps,
        count_active_tests_for_cluster,
    )

    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "Cargo.toml").write_text("[package]\nname='c'\n")
    body = "#[cfg(test)]\nmod gen_tests {\n    #[test]\n    fn t() {}\n}\n"
    # Parent declares sibling 1; sibling 2 exists but is UNDECLARED (hidden);
    # sibling 3 is declared behind a False gate (deliberately excluded).
    (src / "lib.rs").write_text(
        '#[cfg(test)]\n#[path = "lib.gen_tests_1.rs"]\nmod gen_tests_1;\n'
        '#[cfg(nightly_only)]\n#[path = "lib.gen_tests_3.rs"]\nmod gen_tests_3;\n'
    )
    (src / "lib.gen_tests_1.rs").write_text(body)
    (src / "lib.gen_tests_2.rs").write_text(body)
    (src / "lib.gen_tests_3.rs").write_text(body)

    deps = TestCountDeps(
        enumerate_fns=lambda p: None,
        mod_records=lambda p: None,
        file_cfg_predicate=lambda d, n: None,
        is_gen_tests_file=lambda n: ".gen_tests_" in n,
    )

    class _Cluster:
        cfg_true: list[str] = []
        cfg_false = ["nightly_only"]

    total = count_active_tests_for_cluster(tmp_path, _Cluster(), deps)
    # declared(1) + hidden-undeclared(1); the False-gated sibling 3 excluded.
    assert total == 2


def test_nightly_rules_for_cluster_shapes() -> None:
    from types import SimpleNamespace

    from pipeline_core.domain.cluster_plan import nightly_rules_for

    assert nightly_rules_for(None) == ""
    nd = SimpleNamespace(is_default=False, prompt_nightly_rule="RULE", excluded_cfg=["x"])
    assert nightly_rules_for(nd) == "RULE"
    default_excl = SimpleNamespace(is_default=True, excluded_cfg=["egma", "abc"])
    out = nightly_rules_for(default_excl)
    assert "`abc`, `egma`" in out and "Do NOT write tests" in out
    assert nightly_rules_for(SimpleNamespace(is_default=True, excluded_cfg=[])) == ""


def test_active_test_inventory_names_and_stem_scope(tmp_path) -> None:
    # Two tests/ binaries + a src-side inline test. The stem-scoped inventory
    # (a `cargo test --test a` sweep's denominator) must cover ONLY tests/a.rs;
    # the full walk counts everything. Names come from the record path.
    from pipeline_core.application.test_count import (
        TestCountDeps,
        active_test_inventory_for_cluster,
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.rs").write_text(
        "#[test]\nfn t_a1() {}\n#[test]\nfn t_a2() {}\n", encoding="utf-8")
    (tmp_path / "tests" / "b.rs").write_text(
        "#[test]\nfn t_b() {}\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "#[test]\nfn t_lib() {}\n", encoding="utf-8")

    def _recs(p):
        return [
            {"file": "tests/a.rs", "fn_name": "t_a1", "is_test": True,
             "cfg_predicates": []},
            {"file": "tests/a.rs", "fn_name": "t_a2", "is_test": True,
             "cfg_predicates": []},
            {"file": "tests/b.rs", "fn_name": "t_b", "is_test": True,
             "cfg_predicates": []},
            {"file": "src/lib.rs", "fn_name": "t_lib", "is_test": True,
             "cfg_predicates": []},
        ]

    deps = TestCountDeps(
        enumerate_fns=_recs,
        mod_records=lambda p: None,
        file_cfg_predicate=lambda d, n: None,
        is_gen_tests_file=lambda n: ".gen_tests_" in n,
    )

    class _Cluster:
        cfg_true: list[str] = []
        cfg_false: list[str] = []

    n, names = active_test_inventory_for_cluster(
        tmp_path, _Cluster(), deps, tests_dir_stems=frozenset({"a"}))
    assert n == 2 and names == frozenset({"t_a1", "t_a2"})
    full_n, full_names = active_test_inventory_for_cluster(
        tmp_path, _Cluster(), deps)
    assert full_n == 4 and full_names == frozenset(
        {"t_a1", "t_a2", "t_b", "t_lib"})


def test_active_test_inventory_subdir_module_counts_into_root_binary(
        tmp_path) -> None:
    # tests/common/mod.rs has no binary of its own — its #[test] fns compile
    # INTO tests/a.rs (which declares `mod common;`). The stem-scoped
    # inventory must count them; a subdir NOT included by any selected root
    # binary stays out.
    from pipeline_core.application.test_count import (
        TestCountDeps,
        active_test_inventory_for_cluster,
    )
    (tmp_path / "tests" / "common").mkdir(parents=True)
    (tmp_path / "tests" / "orphan").mkdir()
    (tmp_path / "tests" / "a.rs").write_text(
        "mod common;\n#[test]\nfn t_a() {}\n", encoding="utf-8")
    (tmp_path / "tests" / "common" / "mod.rs").write_text(
        "#[test]\nfn t_common() {}\n", encoding="utf-8")
    (tmp_path / "tests" / "orphan" / "mod.rs").write_text(
        "#[test]\nfn t_orphan() {}\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("", encoding="utf-8")

    def _recs(p):
        return [
            {"file": "tests/a.rs", "fn_name": "t_a", "is_test": True,
             "cfg_predicates": []},
            {"file": "tests/common/mod.rs", "fn_name": "t_common",
             "is_test": True, "cfg_predicates": []},
            {"file": "tests/orphan/mod.rs", "fn_name": "t_orphan",
             "is_test": True, "cfg_predicates": []},
        ]

    deps = TestCountDeps(
        enumerate_fns=_recs,
        mod_records=lambda p: None,
        file_cfg_predicate=lambda d, n: None,
        is_gen_tests_file=lambda n: ".gen_tests_" in n,
    )

    class _Cluster:
        cfg_true: list[str] = []
        cfg_false: list[str] = []

    n, names = active_test_inventory_for_cluster(
        tmp_path, _Cluster(), deps, tests_dir_stems=frozenset({"a"}))
    assert n == 2 and names == frozenset({"t_a", "t_common"})
