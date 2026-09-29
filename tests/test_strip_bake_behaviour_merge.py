"""Pins for the bake's behaviour-suite handling: it MERGES into a suite an
earlier phase (t0d) wrote — existing wins — instead of overwriting it, and it
refuses the pipeline's own generated test siblings by name."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.application.strip_bake import (
    BakeArtifactsDeps,
    _behaviour_sources,
    _test_layout_sources,
    bake_skeleton_artifacts,
)
from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    ObservableOutcome,
)
from pipeline_core.domain.models import BuildCluster, SkeletoniseResult

_DOC = '''
/// ```
/// let e = anyhow!("boom");
/// assert_eq!(e.to_string(), "boom");
/// ```
pub fn f() {}
'''
_TEST = '#[test]\nfn t() {\n    assert_eq!(f().to_string(), "boom");\n}\n'


def _deps(logs: list[str], saved: dict[str, BehaviourSuite],
          earlier: BehaviourSuite | None) -> BakeArtifactsDeps:
    def fake_extract(src: Path, out: Path, cluster: object = None) -> SkeletoniseResult:
        Path(out).mkdir(parents=True, exist_ok=True)
        return SkeletoniseResult(True, [{"f": 1}], "")

    def save(pd: Path, suite: BehaviourSuite) -> Path:
        saved[suite.cfg_cluster] = suite
        return pd / "_b.json"

    kw: dict[str, Any] = {}
    if earlier is not None:
        kw["load_behaviour_suite"] = lambda pd, c: earlier
    return BakeArtifactsDeps(
        log=logs.append,
        cfg_scan=lambda pd: [BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[], excluded_cfg=[],
            active_cfg=[], prompt_nightly_rule="", is_default=True)],
        prune_clusters=lambda cl, pd, cargo_toml_path=None: cl,
        fn_name_counts=lambda sd: {},
        extract_skeleton=fake_extract,
        is_gen_tests_file=lambda n: ".gen_tests_" in n,
        gen_tests_origin_cfgs=lambda t: {},
        save_behaviour_suite=save,
        enumerate_types=lambda sd: [],
        enumerate_fns=lambda sd: [],
        enumerate_mods=lambda sd: [],
        enumerate_uses=lambda sd: [],
        save_private_surface=lambda pd, surface: pd / "_ps.json",
        **kw,
    )


def _crate(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_DOC, encoding="utf-8")
    (tmp_path / "src" / "lib.gen_tests_1.rs").write_text(
        "#[test]\nfn g() {\n    assert_eq!(private_thing().to_string(), \"leak\");\n}\n",
        encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.rs").write_text(_TEST, encoding="utf-8")


def test_bake_keeps_the_earlier_suite_and_adds_its_own_harvest(tmp_path: Path) -> None:
    _crate(tmp_path)
    probe = BehaviourSpec(
        "probe::stable::f::0", "fn main() { println!(\"{}\", f()); }", "f",
        ObservableOutcome.stdout("golden"), exercises=frozenset({"src/lib.rs::f"}),
        provenance=BehaviourProvenance.SYNTHESIZED, subkind=BehaviourSubKind.OUTCOME,
        cfg_cluster="stable")
    # the same test-assertion id t0d produced, with exercises already filled
    earlier_ta = BehaviourSpec(
        "testassert::tests/t.rs::t::0", _TEST, "f().to_string()",
        ObservableOutcome.display("boom"), exercises=frozenset({"src/lib.rs::f"}),
        subkind=BehaviourSubKind.OUTCOME, cfg_cluster="stable")
    earlier = BehaviourSuite("stable", (probe, earlier_ta))
    logs: list[str] = []
    saved: dict[str, BehaviourSuite] = {}
    res = bake_skeleton_artifacts(tmp_path, "m", _deps(logs, saved, earlier))
    assert res["ok"]
    suite = saved["stable"]
    ids = [s.behaviour_id for s in suite.specs]
    assert "probe::stable::f::0" in ids, "t0d's probe survives the bake"
    assert any(i.startswith("doctest::") for i in ids), "the bake's own harvest is added"
    kept = next(s for s in suite.specs if s.behaviour_id == "testassert::tests/t.rs::t::0")
    assert kept.exercises == frozenset({"src/lib.rs::f"}), "existing wins: exercises kept"
    assert ids == sorted(ids)
    assert any("2 already on disk from an earlier phase" in ln for ln in logs)


def test_bake_without_an_earlier_suite_writes_its_harvest(tmp_path: Path) -> None:
    _crate(tmp_path)
    logs: list[str] = []
    saved: dict[str, BehaviourSuite] = {}
    # default load_behaviour_suite: nothing earlier
    assert bake_skeleton_artifacts(tmp_path, "m", _deps(logs, saved, None))["ok"]
    ids = [s.behaviour_id for s in saved["stable"].specs]
    assert any(i.startswith("doctest::") for i in ids)
    assert any(i.startswith("testassert::tests/t.rs::t::") for i in ids)
    assert not any(".gen_tests_" in i for i in ids)


def test_source_readers_skip_generated_test_siblings(tmp_path: Path) -> None:
    _crate(tmp_path)
    (tmp_path / "tests" / "x.gen_tests_2.rs").write_text("#[test]\nfn h() {}\n", encoding="utf-8")
    assert [rel for rel, _ in _behaviour_sources(tmp_path / "src")] == ["lib.rs"]
    assert [rel for rel, _ in _test_layout_sources(tmp_path)] == ["tests/t.rs"]


def test_suite_roundtrips_through_json_with_klass(tmp_path: Path) -> None:
    from pipeline_core.adapters.behaviour_store import (
        load_behaviour_suite,
        save_behaviour_suite,
    )
    spec = BehaviourSpec(
        "testassert::tests/t.rs::t::0", _TEST, "f().to_string()",
        ObservableOutcome.type_identity("&str", True),
        subkind=BehaviourSubKind.OUTCOME, cfg_cluster="stable")
    from pipeline_core.domain.behaviour import classified
    suite = BehaviourSuite("stable", (classified(spec),))
    p = save_behaviour_suite(tmp_path, suite)
    assert json.loads(p.read_text())["specs"][0]["klass"] == "IDENTITY"
    assert load_behaviour_suite(tmp_path, "stable") == suite


def test_the_bake_over_a_legacy_store_keeps_its_probes_and_builds_the_book(
        tmp_path: Path) -> None:
    """Run `273278-22529e44` (2026-09-29): an augmented restart's t0d returns
    at the augmented check, so the only stage that re-harvests is this bake;
    it discarded the format-1 store whole (four probe rows lost) and wrote a
    format-2 suite with no book. Plan sentence: on a legacy store the bake
    keeps the probe rows and calls the book. Negative control: with no book
    builder wired the suite is saved with `features` empty — what that
    launch wrote."""
    from dataclasses import replace
    _crate(tmp_path)
    probe = BehaviourSpec(
        "probe::stable::f::0", "fn main() { println!(\"{}\", f()); }", "f",
        ObservableOutcome.stdout("golden"),
        provenance=BehaviourProvenance.SYNTHESIZED, subkind=BehaviourSubKind.OUTCOME,
        cfg_cluster="stable")
    legacy_ta = BehaviourSpec(
        "testassert::tests/t.rs::t::0", "#[test]\n" + _TEST, "f().to_string()",
        ObservableOutcome.display("boom"), subkind=BehaviourSubKind.OUTCOME,
        cfg_cluster="stable")
    legacy = BehaviourSuite("stable", (probe, legacy_ta), format_version=1)
    assert legacy.is_legacy
    asked: list[str] = []

    def book(pd: Path, mod: str, cl: BuildCluster, suite: BehaviourSuite,
             src: Path) -> BehaviourSuite:
        asked.append(f"{mod}:{cl.name}:{src.name}")
        from pipeline_core.domain.behaviour import Feature
        return replace(suite, features=(
            Feature("f", "fn", ("probe::stable::f::0",)),))

    logs: list[str] = []
    saved: dict[str, BehaviourSuite] = {}
    deps = replace(_deps(logs, saved, legacy), build_book=book)
    assert bake_skeleton_artifacts(tmp_path, "m", deps)["ok"]
    suite = saved["stable"]
    ids = {s.behaviour_id for s in suite.specs}
    assert "probe::stable::f::0" in ids
    assert not suite.is_legacy
    assert all("#[test]" not in s.setup for s in suite.specs
               if s.behaviour_id.startswith("testassert::"))
    assert asked == ["m:stable:src"] and suite.features
    assert any("1 probe row(s) the harvest cannot re-measure are kept" in ln
               for ln in logs)

    control = tmp_path / "control"
    control.mkdir()
    _crate(control)
    c_saved: dict[str, BehaviourSuite] = {}
    assert bake_skeleton_artifacts(control, "m", _deps([], c_saved, legacy))["ok"]
    assert not c_saved["stable"].features
