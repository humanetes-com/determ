"""C12 (gentest prelude), C7 (crate facts), C4 (cluster analysis),
C5 (skeleton bake) relocations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.adapters.structure import (
    fn_name_counts,
    load_baked_clusters,
    source_hash,
)
from pipeline_core.application.reconstruct import cluster_membership as cm
from pipeline_core.application.strip_bake import (
    BakeArtifactsDeps,
    bake_skeleton_artifacts,
)
from pipeline_core.domain.generation import derive_gentest_prelude
from pipeline_core.domain.models import BuildCluster, SkeletoniseResult
from pipeline_core.domain.rust_idioms import no_std_invariant_note
from pipeline_core.domain.rust_macros import collect_crate_macros


# ----- C12 -----
def test_derive_gentest_prelude_adds_crate_lints() -> None:
    allow, prelude = derive_gentest_prelude("#![deny(warnings)]\n#![forbid(unsafe_code)]\n")
    assert "warnings" in allow and "unsafe_code" in allow
    assert "unused" in allow  # base set preserved
    assert "use std::error::Error as StdError;" in prelude


def test_derive_gentest_prelude_empty_source() -> None:
    allow, _ = derive_gentest_prelude("")
    assert allow == "#[allow(clippy::all, non_snake_case, unreachable_patterns, unused)]"


# ----- C7 -----
def test_no_std_invariant_note() -> None:
    assert no_std_invariant_note("#![no_std]\nfn x(){}").startswith("crate is #![no_std]")
    assert no_std_invariant_note("fn main(){}") == ""


def test_collect_crate_macros_and_truncation() -> None:
    files = [("a.rs", "#[macro_export]\nmacro_rules! m { () => {} }\n")]
    out = collect_crate_macros(files, 9999)
    assert "// from a.rs" in out and "macro_rules! m" in out and "#[macro_export]" in out
    assert collect_crate_macros([("b.rs", "macro_rules! m { () => {} }")], 5).endswith(
        "(macros truncated)")
    assert collect_crate_macros([("c.rs", "fn x() {}")], 100) == ""  # no macros


# ----- C4 -----
@dataclass
class _C:
    name: str = ""
    cfg_true: tuple = ()
    cfg_false: tuple = ()
    active_cfg: tuple = ()
    is_default: bool = False


def test_cluster_cfg_attr_default_negates_others() -> None:
    stable = _C(name="stable", is_default=True)
    nightly = _C(name="nightly", active_cfg=("egma",))
    assert cm.cluster_cfg_attr(stable, [stable, nightly]) == 'not(any(egma, feature = "egma"))'
    assert cm.cluster_cfg_attr(nightly, [stable, nightly]) == 'any(egma, feature = "egma")'
    assert cm.cluster_cfg_attr(stable, [stable]) == ""  # no other clusters
    assert cm.cluster_cfg_attr(None, []) == ""


def test_filter_tests_for_cluster_none_passthrough() -> None:
    tf = [("a.rs", "fn x(){}")]
    assert cm.filter_tests_for_cluster(tf, None) is tf


def test_cluster_gate_lazy_symbols_not_called_for_nondefault() -> None:
    nightly = _C(name="nightly", active_cfg=("egma",))
    called: list[int] = []
    cm.cluster_gate_for_sibling(nightly, "src", [], "", [nightly],
                                lambda: called.append(1) or frozenset())
    assert not called  # non-default cluster → returns base gate before symbol branch


def test_propagate_or_quarantine_sibling(tmp_path: Path) -> None:
    gt = tmp_path / "x.gen_tests_2.rs"
    gt.write_text("// test", encoding="utf-8")
    # absent from a non-empty manifest → quarantined, returns False
    assert cm.propagate_or_quarantine_sibling(gt, {"y.gen_tests_1": ""}, "x", 2) is False
    assert (tmp_path / "x.gen_tests_2.rs.broken").exists()
    # empty manifest → propagate
    gt2 = tmp_path / "z.gen_tests_1.rs"
    gt2.write_text("// t", encoding="utf-8")
    assert cm.propagate_or_quarantine_sibling(gt2, {}, "z", 1) is True


# ----- C5 -----
def test_fn_name_counts(tmp_path: Path) -> None:
    (tmp_path / "lib.rs").write_text("fn foo(){ foo(); }\nfn bar(){}\n", encoding="utf-8")
    counts = fn_name_counts(tmp_path)
    assert counts.get("foo", 0) >= 2 and "bar" in counts
    assert fn_name_counts(tmp_path / "missing") == {}


def test_source_hash_changes_with_source(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn a(){}\n", encoding="utf-8")
    kw = {"is_gen_tests_sibling_name": lambda n: False, "strip_gen_tests_decls": lambda t: t}
    h1 = source_hash(tmp_path, **kw)
    assert len(h1) == 16
    (tmp_path / "src" / "lib.rs").write_text("fn b(){}\n", encoding="utf-8")
    assert source_hash(tmp_path, **kw) != h1


def test_load_baked_clusters(tmp_path: Path) -> None:
    assert load_baked_clusters(tmp_path) == []
    skel = tmp_path / ".skeleton"
    skel.mkdir()
    (skel / "_clusters.json").write_text(
        json.dumps({"clusters": [{"name": "stable", "is_default": True}]}), encoding="utf-8")
    out = load_baked_clusters(tmp_path)
    assert len(out) == 1 and out[0].name == "stable" and out[0].is_default


def _bake_deps(logs: list[str]) -> BakeArtifactsDeps:
    def fake_extract(src: Path, out: Path, cluster: object = None) -> SkeletoniseResult:
        Path(out).mkdir(parents=True, exist_ok=True)
        return SkeletoniseResult(True, [{"f": 1}], "")

    return BakeArtifactsDeps(
        log=lambda m: logs.append(m),
        cfg_scan=lambda pd: [BuildCluster(
            name="default", env_extra={}, tarpaulin_exclude_files=[], excluded_cfg=[],
            active_cfg=[], prompt_nightly_rule="", is_default=True)],
        prune_clusters=lambda cl, pd, cargo_toml_path=None: cl,
        fn_name_counts=lambda sd: {"foo": 1},
        extract_skeleton=fake_extract,
        is_gen_tests_file=lambda n: False,
        gen_tests_origin_cfgs=lambda t: {},
    )


def test_bake_skeleton_artifacts_writes_manifests(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn foo(){}\n", encoding="utf-8")
    res = bake_skeleton_artifacts(tmp_path, "mymod", _bake_deps([]))
    assert res["ok"] and res["clusters"] == ["default"]
    assert (tmp_path / ".skeleton" / "_clusters.json").exists()
    assert (tmp_path / ".skeleton" / "default" / "_leaf_order.json").exists()
    assert (tmp_path / ".skeleton" / "_gen_tests_origin.json").exists()


def test_bake_skeleton_artifacts_no_source_refuses(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()  # empty
    res = bake_skeleton_artifacts(tmp_path, "m", _bake_deps([]))
    assert res["ok"] is False and "no source" in res["reason"]


def test_bake_skeleton_artifacts_extractor_failure(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn x(){}\n", encoding="utf-8")
    deps = _bake_deps([])
    bad = BakeArtifactsDeps(
        log=deps.log, cfg_scan=deps.cfg_scan, prune_clusters=deps.prune_clusters,
        fn_name_counts=deps.fn_name_counts,
        extract_skeleton=lambda s, o, cluster=None: SkeletoniseResult(False, [], "boom"),
        is_gen_tests_file=deps.is_gen_tests_file,
        gen_tests_origin_cfgs=deps.gen_tests_origin_cfgs)
    res = bake_skeleton_artifacts(tmp_path, "m", bad)
    assert res["ok"] is False and "boom" in res["reason"]
