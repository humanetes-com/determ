"""The t1.5 phase runner must hand the compile gate the BAKED CLUSTER ITSELF.

This factory was previously untested, and it was where the toolchain was lost:
it reduced each cluster to ``dict(c.env_extra)`` before calling the gate, so
``requires_nightly`` — which is not an env var — never arrived. The gate then
built anyhow's nightly cluster with the default toolchain while its
``--cfg error_generic_member_access`` was active, failing E0554 at the crate
root for every component on every attempt.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.author_layout_phase import make_layout_phase_runner
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.models import BuildCluster, TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface


def _suite(cluster: str) -> BehaviourSuite:
    """Carries one layout pin: without a harvested pin the component is GATED
    ("refusing to author blind") and the compile gate is never reached."""
    pin = BehaviourSpec(
        behaviour_id="static::repr::0", setup="", when="size_of::<Error>()",
        then=ObservableOutcome.static_assertion(
            StaticAssertKind.SIZE, "assert_eq!(size_of::<Error>(), size_of::<usize>())"),
        subkind=BehaviourSubKind.OUTCOME)
    return BehaviourSuite(cfg_cluster=cluster, specs=(pin,))


def _cluster(name: str, *, env_extra=None, nightly=False) -> BuildCluster:
    return BuildCluster(name=name, env_extra=env_extra or {}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
                        requires_nightly=nightly)


def _surface(cluster: str):
    types = [
        # src-RELATIVE, as the baked surface stores them ("kind.rs", not "src/kind.rs")
        TypeRecord(file="lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
    ]
    return classify_private_surface(types, [], cfg_cluster=cluster)


_AUTHORED = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
             'struct ErrorImpl { inner: Box<dyn StdError> }</LAYOUT_ITEM>')


def _suite_for(project_dir, cluster: str) -> BehaviourSuite:
    return _suite(cluster)


def _run_phase(clusters, seen, tmp_path: Path) -> None:
    def compile_check(pd, name, cluster):
        seen.append((name, cluster))
        return True, ""

    for c in clusters:                       # the scaffold the splice rewrites
        src = tmp_path / c.name / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "error.rs").write_text("struct ErrorImpl { vtable: u8 }\n",
                                      encoding="utf-8")

    runner = make_layout_phase_runner(
        enabled=True,
        load_baked_clusters=lambda pd: list(clusters),
        load_surface=lambda pd, c: _surface(c),
        load_suite=_suite_for,
        api_surface_path=lambda pd, m: tmp_path / "absent.md",
        run_model=lambda p: _AUTHORED,   # must splice, or the gate is never reached
        compile_check=compile_check,
        skeleton_src_dir_for=lambda pd, c: tmp_path / c / "src",
        fn_name_counts=lambda src: {},
        log=lambda m: None,
    )
    runner(tmp_path, "anyhow")


def test_cluster_object_reaches_the_compile_gate(tmp_path) -> None:
    """Not a name, not an env mapping — the object, so the gate can read
    `requires_nightly` (and anything else a future cluster carries)."""
    nightly = _cluster("nightly", env_extra={"RUSTFLAGS": "--cfg egma"}, nightly=True)
    seen: list = []
    _run_phase([_cluster("stable"), nightly], seen, tmp_path)

    by_name = dict(seen)
    assert set(by_name) == {"stable", "nightly"}
    assert by_name["nightly"] is nightly
    assert by_name["nightly"].requires_nightly is True
    assert by_name["stable"].requires_nightly is False
