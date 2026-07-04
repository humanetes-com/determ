"""Fake-adapter unit tests for `StaticSkeletonTask2Generator` (P4 slice 4).

The 4-phase skeleton generator is a use-case over the `ports.generator` roles
(Cargo, FillEngine, Clusters, GenTests, SourceScan, Telemetry, Workspace). These
tests inject one fake object satisfying every role (mirroring `_Task2Adapter`)
and drive each branch — happy paths (per-group, per-fn fn_walk, per-fn tdd),
resume skips, abort guards, the validate-revert loop, and the post-merge loop
(pass / stall / timeout / fill). No cargo, no LLM, no real pipeline.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from pipeline_core.application.generators.task2_generator import (
    StaticSkeletonTask2Generator,
)
from pipeline_core.domain.generation import GenerationRequest, SkeletonConfig
from pipeline_core.domain.models import BuildCluster


@dataclass
class _Diag:
    file: str
    code: str
    message: str


def _cluster(name: str = "default", *, requires_nightly: bool = False) -> BuildCluster:
    return BuildCluster(
        name=name, env_extra={}, tarpaulin_exclude_files=[], excluded_cfg=[],
        active_cfg=[], prompt_nightly_rule="", is_default=(name == "default"),
        requires_nightly=requires_nightly,
    )


_PER_FN_RESULT = {"fns_filled": 1, "bytes_filled": 10, "attempts": 1,
                  "remaining": 0, "errors": []}
_GROUP_RESULT = {"groups_processed": 1, "bytes_filled": 10, "errors": []}
_D_RESULT = {"stubs_total": 0, "stubs_exercised": 0, "files_updated": 0,
             "stubs_remaining": 0}


@dataclass
class FakeAdapter:
    """One object satisfying every generator role; records calls, returns canned
    data. `raw_results`/`run_results` are popped FIFO per cargo invocation."""
    clusters_list: list[BuildCluster]
    cluster_tests: list[tuple[str, str]] = field(default_factory=list)
    origin: dict[str, str] = field(default_factory=dict)
    leaf_order: dict[str, int] = field(default_factory=dict)
    diags: list[_Diag] = field(default_factory=list)
    implicated: list[tuple[str, str]] = field(default_factory=list)
    raw_results: list[tuple[int, str, str]] = field(default_factory=list)
    run_results: list[tuple[int, str]] = field(default_factory=list)
    attribute_seq: list[tuple[dict[Any, Any], str]] = field(default_factory=list)
    phase_valid_keys: set[str] = field(default_factory=set)
    manifest: list[str] = field(default_factory=list)
    propagate: bool = True
    has_decl_ret: bool = False
    balanced: bool = True
    raise_in_filter: bool = False
    logs: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    # --- Telemetry ---
    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None:
        self.events.append(event)

    def check_stop(self, context: str) -> None:
        pass

    def set_current(self, project_dir: Path, label: str) -> None:
        pass

    def mark_phase(self, project_dir: Path, key: str, status: str,
                   detail: str = "") -> None:
        pass

    def phase_valid(self, project_dir: Path, key: str) -> bool:
        return key in self.phase_valid_keys

    # --- Workspace ---
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def load_manifest(self, manifest_path: Path) -> list[str]:
        return list(self.manifest)

    def save_manifest(self, manifest_path: Path, files: list[str]) -> None:
        self.manifest = list(files)

    # --- Cargo ---
    def run_raw(self, cmd: list[str], cwd: Path, env: dict[str, str],
                timeout: int) -> tuple[int, str, str]:
        return self.raw_results.pop(0) if self.raw_results else (0, "", "")

    def run(self, cmd: str, cwd: Path, env: dict[str, str],
            timeout: int = 180) -> tuple[int, str]:
        return self.run_results.pop(0) if self.run_results else (0, "ok")

    def run_check_diag(self, cmd: str, cwd: Path, env: dict[str, str],
                       timeout: int = 180) -> tuple[int, str, list[_Diag]]:
        return (0, "", list(self.diags))

    def build_test_cmd(self, test_stems: list[str],
                       cluster: BuildCluster | None) -> str:
        return "cargo test"

    def env(self) -> dict[str, str]:
        return {}

    def implicated_fns_from_cargo_errors(
            self, cargo_output: str, target_src_dir: Path) -> list[tuple[str, str]]:
        return list(self.implicated)

    def attribute_failures(self, test_output: str, src_dir: Path,
                           recently_filled: list[tuple[str, str]],
                           test_reverse_map: dict[str, set[tuple[str, str]]],
                           cluster_name: str) -> tuple[dict[Any, Any], str]:
        return self.attribute_seq.pop(0) if self.attribute_seq else ({}, "none")

    # --- FillEngine ---
    def run_phase_c_tdd(self, **kw: Any) -> dict[str, Any]:
        return dict(_PER_FN_RESULT)

    def fill_fns_one_by_one(self, **kw: Any) -> dict[str, Any]:
        return dict(_PER_FN_RESULT)

    def fill_groups(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return dict(_GROUP_RESULT)

    def group_tests(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"name": "g0", "test_files": []}]

    def cleanup_residuals(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return dict(_D_RESULT)

    def scan_unimplemented_functions(self, src_dir: Path) -> list[tuple[str, str]]:
        return []

    def merge_cluster_outputs(self, **kw: Any) -> dict[str, Any]:
        return {"files_written": 1, "cfg_gated_fns": 0}

    def generate_fix_loop(self, **kw: Any) -> tuple[bool, str]:
        return (True, "")

    def reset_exhaustion_flags(self, project_dir: Path, reason: str) -> None:
        pass

    def build_test_reverse_map(
            self, test_files: list[Path], src_dir: Path) -> dict[str, set[tuple[str, str]]]:
        return {}

    # --- Clusters ---
    def load_baked(self, project_dir: Path) -> list[BuildCluster]:
        return list(self.clusters_list)

    def load_baked_leaf_order(self, project_dir: Path,
                              cluster_name: str) -> dict[str, int]:
        return dict(self.leaf_order)

    def filter_tests_for_cluster(self, test_files: list[tuple[str, str]],
                                 cluster: BuildCluster | None) -> list[tuple[str, str]]:
        if self.raise_in_filter:
            raise RuntimeError("boom")
        return list(self.cluster_tests)

    def cluster_cfg_attr(self, cluster: BuildCluster) -> str:
        return ""

    # --- GenTests ---
    def build_decl(self, sibling_name: str, n: int | str, allow_line: str,
                   cluster_cfg_attr: str = "") -> str:
        return f'\n#[path="{sibling_name}"] mod gen_tests_{n};\n'

    def has_decl(self, src_text: str, pass_n: int) -> bool:
        return self.has_decl_ret

    def strip_decl_n(self, parent_text: str, pass_n: int) -> str:
        return parent_text

    def derive_prelude(self, project_dir: Path) -> tuple[str, str]:
        return ("#[allow(unused)]", "")

    def load_origin(self, project_dir: Path) -> dict[str, str]:
        return dict(self.origin)

    def propagate_or_quarantine_sibling(self, gt: Path, origin_manifest: dict[str, str],
                                        parent_stem: str, pass_n: int,
                                        tag: str = "") -> bool:
        return self.propagate

    def is_gen_tests_file(self, name: str) -> bool:
        return ".gen_tests_" in name

    # --- SourceScan ---
    def locate_fn(self, file_text: str, fn_name: str) -> tuple[str, int, int, int] | None:
        idx = file_text.find("{")
        if idx < 0:
            return None
        return (f"fn {fn_name}()", 0, idx + 1, len(file_text) - 1)

    def rust_delimiters_balanced(self, text: str) -> bool:
        return self.balanced


def _make_project(tmp_path: Path, clusters: list[BuildCluster], *,
                  with_gen_tests: bool = False) -> Path:
    """Build a project dir with baked skeletons + tests on disk."""
    pd = tmp_path / "crate"
    (pd / "src").mkdir(parents=True)
    (pd / "Cargo.toml").write_text("[package]\nname='c'\n", encoding="utf-8")
    (pd / "genesis.md").write_text("# genesis\n", encoding="utf-8")
    (pd / "functional_requirements.md").write_text("# fr\n", encoding="utf-8")
    tdir = pd / "tests"
    tdir.mkdir()
    (tdir / "it.rs").write_text("#[test] fn t() {}", encoding="utf-8")
    for c in clusters:
        sk = pd / ".skeleton" / c.name / "src"
        sk.mkdir(parents=True)
        (sk / "lib.rs").write_text("fn foo() -> i32 {\n    unimplemented!()\n}\n",
                                   encoding="utf-8")
    if with_gen_tests:
        (pd / "src" / "lib.rs").write_text("fn foo() -> i32 { 42 }\n", encoding="utf-8")
        (pd / "src" / "lib.gen_tests_1.rs").write_text("#[test] fn g() {}",
                                                       encoding="utf-8")
    return pd


def _cfg(*, per_fn: bool, tdd: bool, parallelism: int = 1,
         hard_cap: int = 10_000) -> SkeletonConfig:
    return SkeletonConfig(
        task2_model="m", parallelism=parallelism, phase_c_per_fn=per_fn,
        phase_c_tdd=tdd, phase_c_max_passes=1, validate_iters=2,
        post_merge_max_stalls=2, post_merge_hard_cap=hard_cap,
        post_merge_test_timeout=10, recently_filled_max=5,
    )


def _gen(fake: FakeAdapter, cfg: SkeletonConfig) -> StaticSkeletonTask2Generator:
    return StaticSkeletonTask2Generator(
        tel=fake, cargo=fake, fill=fake, clusters=fake, gentests=fake,
        scan=fake, ws=fake, cfg=cfg)


def _req(pd: Path, **over: Any) -> GenerationRequest:
    base = GenerationRequest(
        project_dir=pd, genesis_path=pd / "genesis.md",
        functional_req_path=pd / "functional_requirements.md",
        manifest_path=pd / "manifest.json",
        existing_test_files=["tests/it.rs"], current_module="crate",
    )
    return replace(base, **over)


def _run(pd: Path, gen: StaticSkeletonTask2Generator) -> bool:
    return gen.generate(_req(pd))


def test_abort_when_no_baked_clusters(tmp_path: Path) -> None:
    fake = FakeAdapter(clusters_list=[])
    pd = _make_project(tmp_path, [])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is False
    assert any("no baked clusters" in m for m in fake.logs)


def test_abort_when_no_test_files(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c])
    pd = _make_project(tmp_path, [c])
    ok = _gen(fake, _cfg(per_fn=True, tdd=True)).generate(
        _req(pd, existing_test_files=[]))  # none on disk
    assert ok is False


def test_happy_per_group(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    assert _run(pd, _gen(fake, _cfg(per_fn=False, tdd=False))) is True
    assert fake.manifest  # manifest written from merged src


def test_happy_per_fn_fn_walk_with_validate_revert(tmp_path: Path) -> None:
    c = _cluster()
    # Phase-A check ok; validate iter1 fails (triggers revert), iter2 ok.
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (1, "err", "boom"), (0, "", "")],
        implicated=[("src/lib.rs", "foo")],
        leaf_order={"foo": 1}, run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    # filled lib.rs must be non-stub so the revert path fires.
    filled = pd / ".skeleton-filled" / c.name / "src"
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    assert (filled / "lib.rs").exists()
    assert any("reverted" in m for m in fake.logs)


def test_happy_per_fn_tdd_skips_phase_d(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, [c])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("skeleton-D] skipped" in m for m in fake.logs)


def test_leak_guard_quarantines_sibling(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        diags=[_Diag("src/lib.gen_tests_1.rs", "E0658", "unstable")],
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("CLUSTER LEAK" in m for m in fake.logs)
    # the offending sibling was renamed to .broken
    broken = pd / ".skeleton-filled" / c.name / "src" / "lib.gen_tests_1.rs.broken"
    assert broken.exists()


def test_skeleton_fix_loop_aborts_cluster(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       raw_results=[(1, "err", "boom")], run_results=[(0, "ok")])
    fake.generate_fix_loop = lambda **kw: (False, "")  # type: ignore[method-assign]
    pd = _make_project(tmp_path, [c])
    # cluster aborts but generate() still completes (merge of zero successes).
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("cluster aborted" in m for m in fake.logs)


def test_cluster_exception_caught(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], raise_in_filter=True,
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, [c])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("cluster crashed" in m for m in fake.logs)


def test_clusters_run_serially_even_with_parallelism(tmp_path: Path) -> None:
    # Debt #36: cluster-level parallelism is gone — shared-fn seeding needs the
    # previous cluster's accepted bodies on disk before the next cluster starts.
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, cs)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True, parallelism=2))) is True
    assert any("runs clusters serially" in m for m in fake.logs)


def test_second_cluster_seeds_shared_bodies_from_first(tmp_path: Path) -> None:
    # Debt #36: the default cluster fills `foo`; the nightly cluster must start
    # from that body (seeded into its stub) instead of re-filling it.
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    seen_at_fill: list[str] = []

    def _fill(**kw: Any) -> dict[str, Any]:
        lib = kw["target_src_dir"] / "src" / "lib.rs"
        text = lib.read_text(encoding="utf-8")
        seen_at_fill.append(text)
        if "unimplemented!()" in text:
            lib.write_text("fn foo() -> i32 {\n    42\n}\n", encoding="utf-8")
        return dict(_PER_FN_RESULT)

    fake.run_phase_c_tdd = _fill  # type: ignore[method-assign]
    pd = _make_project(tmp_path, cs)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    # default saw the stub; nightly saw the ALREADY-SEEDED body.
    assert "unimplemented!()" in seen_at_fill[0]
    assert "42" in seen_at_fill[1] and "unimplemented!()" not in seen_at_fill[1]
    assert any("[ownership] seeded 1 shared fn" in m for m in fake.logs)


def test_premerge_backfill_adopts_sibling_body(tmp_path: Path) -> None:
    # Debt #36: a stub left in one cluster's tree adopts the body a sibling
    # cluster accepted, so the merge never emits a dead unimplemented!() variant.
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    calls = {"n": 0}

    def _fill(**kw: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 2:  # only the SECOND cluster produces a body
            (kw["target_src_dir"] / "src" / "lib.rs").write_text(
                "fn foo() -> i32 {\n    7\n}\n", encoding="utf-8")
        return dict(_PER_FN_RESULT)

    fake.run_phase_c_tdd = _fill  # type: ignore[method-assign]
    pd = _make_project(tmp_path, cs)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("pre-merge backfill: 1 stub body(ies) adopted" in m
               for m in fake.logs)
    default_lib = pd / ".skeleton-filled" / "default" / "src" / "lib.rs"
    assert "unimplemented!()" not in default_lib.read_text(encoding="utf-8")


def test_resume_skips_all_phases(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c],
        phase_valid_keys={"task2a/crate", "task2b/crate", "task2c/crate"},
        manifest=["src/lib.rs"], run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c])
    (pd / "src" / "lib.rs").write_text("fn foo() {}\n", encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("already done" in m for m in fake.logs)


def test_no_tests_for_cluster_note(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[],  # filter → empty
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, [c])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True


# --- _post_merge_fill_pass direct tests ------------------------------------

def _post(fake: FakeAdapter, pd: Path, cfg: SkeletonConfig) -> None:
    _gen(fake, cfg)._post_merge_fill_pass(
        project_dir=pd, clusters=fake.clusters_list, module="crate",
        functional_req="")


def test_post_merge_hard_cap_zero_noop(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c])
    pd = _make_project(tmp_path, [c])
    _post(fake, pd, _cfg(per_fn=True, tdd=True, hard_cap=0))
    assert not fake.events


def test_post_merge_pass_first_iter(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], run_results=[(0, "ok")])
    pd = _make_project(tmp_path, [c])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert "post_merge.fill.iter" in fake.events


def test_post_merge_timeout(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], run_results=[(-1, "")])
    pd = _make_project(tmp_path, [c])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert "post_merge.fill.iter" in fake.events


def test_post_merge_fill_then_stall_exit(tmp_path: Path) -> None:
    c = _cluster()
    frontier = {("src/lib.rs", "foo"): {"t"}}
    fake = FakeAdapter(
        clusters_list=[c],
        # iter1 fails+fills, iter2 no-improve, iter3 no-improve → stall_exit
        run_results=[(1, "fail"), (1, "fail"), (1, "fail")],
        attribute_seq=[(dict(frontier), "panic"),
                       (dict(frontier), "panic"),
                       (dict(frontier), "panic")],
    )
    pd = _make_project(tmp_path, [c])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert fake.events.count("post_merge.fill.iter") >= 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
