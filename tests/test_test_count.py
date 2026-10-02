"""The expected-test inventory counts what cargo runs — including doc tests.

task3 announced 149 tests against `expected=124` for `stable` on the merged
anyhow tree and 129 against `expected=104` for `nightly`: an undershoot of
exactly 25 in both, printed on every task3 entry as the nonsense aggregate
`273/228 passed`, and 25 is what `cargo test --doc` reports on that tree. With
that much slack the `reported < expected` guard could not fire in the direction
it was written for.
"""
from __future__ import annotations

from pathlib import Path

# Aliased: an imported name starting with `Test` is collected by pytest as a
# test class and warns about its constructor.
from pipeline_core.application.test_count import TestCountDeps as _CountDeps
from pipeline_core.application.test_count import active_test_inventory_for_cluster


def _deps() -> _CountDeps:
    return _CountDeps(
        enumerate_fns=lambda _p: [],
        mod_records=lambda _p: [],
        file_cfg_predicate=lambda _p, _r: None,
        is_gen_tests_file=lambda _n: False,
        enumerate_doc_items=lambda _p: None,
    )


def test_doc_tests_are_counted_in_the_expected_total(tmp_path: Path) -> None:
    """The 25-per-cluster undershoot in miniature: a src file with a doc test
    and no `#[test]` fn contributes to what cargo runs, so it must contribute to
    what task3 expects."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "/// Example\n"
        "/// ```\n"
        "/// let x = mycrate::f();\n"
        "/// ```\n"
        "pub fn f() {}\n")
    total, names = active_test_inventory_for_cluster(tmp_path, None, _deps())
    assert total == 1
    # Counted, never NAMED: rustdoc identifies it as `src/lib.rs - f (line 1)`,
    # which is not a `#[test]` name and reconciles against nothing.
    assert names == frozenset()


def test_a_file_with_neither_kind_of_test_counts_zero(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "/// Not an example, just prose.\npub fn f() {}\n")
    assert active_test_inventory_for_cluster(tmp_path, None, _deps())[0] == 0


def test_a_stem_scoped_sweep_expects_no_doc_tests(tmp_path: Path) -> None:
    """A sweep that names its targets cannot run the doc target, so counting doc
    tests into ITS denominator is a phantom failure on every iteration.

    `--lib --test stem` drops cargo's doc target: measured on the reconstructed
    anyhow tree 2026-08-06, `cargo test --no-fail-fast --lib --test test_boxed`
    prints no `Doc-tests` section, while the unscoped run reports `running 25
    tests` for it. Counting the 25 anyway put them in `reconcile_sweep`'s deficit
    with no vanished NAME to explain them, so `SweepOracle.intact` could not hold
    and phase-c's `Cluster done` exit became unreachable.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "/// ```\n/// let x = 1;\n/// ```\npub fn f() {}\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_a.rs").write_text("#[test]\nfn integ() {}\n")

    unscoped = active_test_inventory_for_cluster(tmp_path, None, _deps())[0]
    scoped = active_test_inventory_for_cluster(
        tmp_path, None, _deps(),
        tests_dir_stems=frozenset({"t_a"}), include_lib_sources=True)[0]
    # The unscoped walk is the denominator of `cargo test`: the doc test and the
    # integration test. The scoped one is the denominator of
    # `cargo test --lib --test t_a`, which runs only the latter.
    assert unscoped == 2
    assert scoped == 1


# --- doc tests counted per item, under rustdoc's cfg set --------------------
#
# Run 185710-f6135a93 (2026-09-17): the merged tree keeps one cfg-gated copy of
# each divergent item per cluster, doc comment included. The whole-file scan
# counted every copy's examples — 33 where rustdoc ran 25 — so a clean `stable`
# run (150 passed, 2 ignored, rc=0) read as incomplete against 160 expected and
# the run ended non-converged. The shape below is invented: two gated copies of
# one documented fn, a cluster that sets its cfg through RUSTFLAGS.

_TWO_COPIES = (
    "#[cfg(not(any(variant_b, feature = \"variant_b\")))]\n"
    "/// ```\n/// let _ = 1;\n/// ```\n"
    "pub fn make() {}\n"
    "#[cfg(any(variant_b, feature = \"variant_b\"))]\n"
    "/// ```\n/// let _ = 2;\n/// ```\n"
    "/// ```\n/// let _ = 3;\n/// ```\n"
    "pub fn make() {}\n")


def _doc_records() -> list:
    from pipeline_core.domain.models import DocItemRecord
    return [
        DocItemRecord(file="src/lib.rs", line=2,
                      cfg_predicates=('not (any (variant_b , feature = "variant_b"))',),
                      doc=" ```\n let _ = 1;\n ```"),
        DocItemRecord(file="src/lib.rs", line=6,
                      cfg_predicates=('any (variant_b , feature = "variant_b")',),
                      doc=" ```\n let _ = 2;\n ```\n ```\n let _ = 3;\n ```"),
    ]


def _variant_cluster(env_extra: dict[str, str]):
    from pipeline_core.domain.models import BuildCluster
    return BuildCluster(name="b", env_extra=env_extra, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=["variant_b"],
                        prompt_nightly_rule="", is_default=False)


def _base_cluster():
    from pipeline_core.domain.models import BuildCluster
    return BuildCluster(name="a", env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=["variant_b"], active_cfg=[],
                        prompt_nightly_rule="", is_default=True)


def _with_records(records):
    import dataclasses
    return dataclasses.replace(_deps(), enumerate_doc_items=lambda _p: records)


def test_doc_tests_of_a_cfg_gated_copy_count_in_the_cluster_that_compiles_it(
        tmp_path: Path) -> None:
    """A cluster's doc tests are those of the copies its cfg compiles: every
    cluster cargo env hands rustdoc the cfgs it hands rustc
    (`domain.cluster_env`), so the count has one cfg set, however the cluster
    spells its env."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_TWO_COPIES)
    deps = _with_records(_doc_records())
    assert active_test_inventory_for_cluster(tmp_path, _base_cluster(), deps)[0] == 1
    rustflags = _variant_cluster({"RUSTFLAGS": "--cfg variant_b"})
    assert active_test_inventory_for_cluster(tmp_path, rustflags, deps)[0] == 2
    both = _variant_cluster({"RUSTFLAGS": "--cfg variant_b",
                             "RUSTDOCFLAGS": "--cfg variant_b"})
    assert active_test_inventory_for_cluster(tmp_path, both, deps)[0] == 2


def test_without_doc_records_the_text_scan_counts_every_copy(tmp_path: Path) -> None:
    # NEGATIVE CONTROL: the pre-fix reading (no records → whole-file scan)
    # counts all three examples under every cluster.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_TWO_COPIES)
    for cluster in (_base_cluster(), _variant_cluster({"RUSTFLAGS": "--cfg variant_b"})):
        assert active_test_inventory_for_cluster(tmp_path, cluster, _deps())[0] == 3
