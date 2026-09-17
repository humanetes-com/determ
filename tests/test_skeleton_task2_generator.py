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

from pipeline_core.adapters.cargo import cluster_cargo_env
from pipeline_core.application.generators.task2_generator import (
    StaticSkeletonTask2Generator,
)
from pipeline_core.domain.attribution import AttributionResult, FailureKind
from pipeline_core.domain.generation import GenerationRequest, SkeletonConfig
from pipeline_core.domain.models import BuildCluster, ClusterFillResult
from pipeline_core.domain.stub_inventory import StubInventory


@dataclass
class _Diag:
    file: str
    code: str
    message: str
    # The real `Diagnostic` carries rustc's structured "consider importing"
    # suggestions, and the pre-pass heal applies them before it reverts anything
    # — a seeded body that merely needs a `use` must not be thrown away. A fake
    # missing the field made the heal raise instead of heal, which read in the
    # assertions as "the poisoned body was never reverted".
    import_fixes: tuple = ()


def _cluster(name: str = "default", *, requires_nightly: bool = False) -> BuildCluster:
    return BuildCluster(
        name=name, env_extra={}, tarpaulin_exclude_files=[], excluded_cfg=[],
        active_cfg=[], prompt_nightly_rule="", is_default=(name == "default"),
        requires_nightly=requires_nightly,
    )


_PER_FN_RESULT = ClusterFillResult(fns_filled=1, bytes_filled=10, attempts=1)
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
    # (diagnostics, rendered) each revert gate passed to the attribution policy
    # — a gate handing over [] is one that fell back to the macro-blind scan.
    implicated_calls: list[tuple[list[_Diag], str]] = field(default_factory=list)
    raw_results: list[tuple[int, str, str]] = field(default_factory=list)
    # run_check_diag results, popped FIFO. Empty → falls back to the `diags`
    # field with rc=0 (a clean check), preserving pre-structured-heal tests.
    diag_results: list[tuple[int, str, list[_Diag]]] = field(default_factory=list)
    run_results: list[tuple[int, str]] = field(default_factory=list)
    attribute_seq: list[tuple[dict[Any, Any], FailureKind]] = field(
        default_factory=list)
    phase_valid_keys: set[str] = field(default_factory=set)
    # (key, status, detail) per mark_phase — a checkpoint that is never written
    # replays its whole stage on the next resume, so the write is the assertion.
    marks: list[tuple[str, str, str]] = field(default_factory=list)
    manifest: list[str] = field(default_factory=list)
    propagate: bool = True
    has_decl_ret: bool = False
    balanced: bool = True
    raise_in_filter: bool = False
    logs: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    # (event, data) — an event's PAYLOAD is the alarm an operator reads; a name
    # alone cannot catch a verdict field that reports the wrong tree state.
    event_data: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # --- Telemetry ---
    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None:
        self.events.append(event)
        self.event_data.append((event, dict(data or {})))

    def check_stop(self, context: str) -> None:
        pass

    def set_current(self, project_dir: Path, label: str) -> None:
        pass

    def mark_phase(self, project_dir: Path, key: str, status: str,
                   detail: str = "") -> None:
        self.marks.append((key, status, detail))

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
        if self.diag_results:
            return self.diag_results.pop(0)
        return (0, "", list(self.diags))

    def build_test_cmd(self, test_stems: list[str],
                       cluster: BuildCluster | None,
                       *, include_lib: bool = False) -> str:
        return "cargo test"

    def env(self) -> dict[str, str]:
        return {}

    def cluster_env(self, cluster: BuildCluster | None) -> dict[str, str]:
        return cluster_cargo_env(cluster)

    def implicated_fns_from_check(
            self, diagnostics: list[_Diag], rendered_output: str,
            target_src_dir: Path) -> list[tuple[str, str]]:
        self.implicated_calls.append((list(diagnostics), rendered_output))
        return list(self.implicated)

    def attribute_failures(self, test_output: str, src_dir: Path,
                           recently_filled: list[tuple[str, str]],
                           test_reverse_map: dict[str, set[tuple[str, str]]],
                           cluster_name: str) -> AttributionResult:
        fr, kind = (self.attribute_seq.pop(0) if self.attribute_seq
                    else ({}, FailureKind.NO_FRONTIER))
        return AttributionResult(fr, FailureKind(kind), dict(fr))

    # --- FillEngine ---
    def run_phase_c_tdd(self, **kw: Any) -> ClusterFillResult:
        return _PER_FN_RESULT

    def fill_fns_one_by_one(self, **kw: Any) -> ClusterFillResult:
        return _PER_FN_RESULT

    def fill_groups(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return dict(_GROUP_RESULT)

    def group_tests(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"name": "g0", "test_files": []}]

    def cleanup_residuals(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return dict(_D_RESULT)

    def scan_unimplemented_functions(self, src_dir: Path) -> StubInventory:
        return StubInventory()

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
        raw_results=[(0, "", ""), (0, "", "")],
        # run_check_diag queue: pre-pass heal, gen_tests leak guard, then
        # validate iter1 FAILS with a structured diagnostic; iter2 falls through
        # to the fake's clean rc=0 default.
        diag_results=[(0, "", []), (0, "", []),
                      (1, "err", [_Diag("src/lib.rs", "E0599", "boom")])],
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
    # Two failing raw checks: the initial cargo check, AND the post-exhaustion
    # recheck that decides whether to salvage (tree compiles) or genuinely
    # bail (still broken) — this fixture simulates "still broken", so the
    # recheck must also fail for the abort path to fire.
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       raw_results=[(1, "err", "boom"), (1, "err", "boom")],
                       run_results=[(0, "ok")])
    fake.generate_fix_loop = lambda **kw: (False, "")  # type: ignore[method-assign]
    pd = _make_project(tmp_path, [c])
    # cluster aborts but generate() still completes (merge of zero successes).
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("cluster aborted" in m for m in fake.logs)


def test_skeleton_fix_loop_exhaustion_salvaged_when_tree_still_compiles(
        tmp_path: Path) -> None:
    # Same exhausted fix-loop verdict as above, but this time the
    # post-exhaustion recheck finds the tree compiles clean (the exhaustion
    # was over one fn's test/behavioral correctness, not syntax) — the
    # cluster must NOT abort; it falls through to Phase C instead of
    # discarding every other file the fix loop already got right.
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       raw_results=[(1, "err", "boom"), (0, "", "")],
                       run_results=[(0, "ok")])
    fake.generate_fix_loop = lambda **kw: (False, "")  # type: ignore[method-assign]
    pd = _make_project(tmp_path, [c])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert not any("cluster aborted" in m for m in fake.logs)
    assert any("salvaging" in m for m in fake.logs)


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


def test_completed_cluster_is_checkpointed_per_cluster(tmp_path: Path) -> None:
    # Without a per-cluster marker, re-entering task2a replays cluster 1's whole
    # Phase A→D before reaching the cluster that needs work. A cluster that ran
    # to completion records its own key.
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, cs)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    keys = [k for k, status, _ in fake.marks if status == "done"]
    assert "task2a/crate/default" in keys
    assert "task2a/crate/nightly" in keys


def test_aborted_cluster_is_not_checkpointed(tmp_path: Path) -> None:
    # A cluster that never produced a tree must stay re-runnable: checkpointing
    # an abort is how a resume inherits a fragment as though it were finished.
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       raw_results=[(1, "err", "boom"), (1, "err", "boom")],
                       run_results=[(0, "ok")])
    fake.generate_fix_loop = lambda **kw: (False, "")  # type: ignore[method-assign]
    pd = _make_project(tmp_path, [c])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("cluster aborted" in m for m in fake.logs)
    assert "task2a/crate/default" not in [k for k, _s, _d in fake.marks]


def test_checkpointed_cluster_is_skipped_but_still_seeds_the_next(
        tmp_path: Path) -> None:
    # The resume the whole change exists for: cluster 1 is done and its tree is
    # on disk, cluster 2 is not. Cluster 1's Phase A→D must not re-run — and
    # cluster 2 must still be seeded FROM ITS TREE, since the seed is read off
    # the tree, not off the run that produced it.
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")],
                       phase_valid_keys={"task2a/crate/default"})
    pd = _make_project(tmp_path, cs)
    done_tree = pd / ".skeleton-filled" / "default" / "src"
    done_tree.mkdir(parents=True)
    (done_tree / "lib.rs").write_text("fn foo() -> i32 {\n    42\n}\n",
                                      encoding="utf-8")
    filled_for: list[str] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        filled_for.append(Path(kw["target_src_dir"]).name)
        lib = kw["target_src_dir"] / "src" / "lib.rs"
        assert "42" in lib.read_text(encoding="utf-8")
        return _PER_FN_RESULT

    fake.run_phase_c_tdd = _fill  # type: ignore[method-assign]
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert filled_for == ["nightly"]
    assert any("already reconstructed" in m for m in fake.logs)


def test_checkpointed_cluster_reruns_when_its_tree_is_gone(tmp_path: Path) -> None:
    # Marker without artifact is the stale-checkpoint case task2b/task2c already
    # guard: the cluster runs, it is not fabricated as ok.
    cs = [_cluster("default")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")],
                       phase_valid_keys={"task2a/crate/default"})
    pd = _make_project(tmp_path, cs)
    ran: list[str] = []
    fake.run_phase_c_tdd = lambda **kw: (  # type: ignore[method-assign]
        ran.append(kw["cluster"].name) or _PER_FN_RESULT)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert ran == ["default"]


def test_second_cluster_seeds_shared_bodies_from_first(tmp_path: Path) -> None:
    # Debt #36: the default cluster fills `foo`; the nightly cluster must start
    # from that body (seeded into its stub) instead of re-filling it.
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    seen_at_fill: list[str] = []

    def _fill(**kw: Any) -> ClusterFillResult:
        lib = kw["target_src_dir"] / "src" / "lib.rs"
        text = lib.read_text(encoding="utf-8")
        seen_at_fill.append(text)
        if "unimplemented!()" in text:
            lib.write_text("fn foo() -> i32 {\n    42\n}\n", encoding="utf-8")
        return _PER_FN_RESULT

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

    def _fill(**kw: Any) -> ClusterFillResult:
        calls["n"] += 1
        if calls["n"] == 2:  # only the SECOND cluster produces a body
            (kw["target_src_dir"] / "src" / "lib.rs").write_text(
                "fn foo() -> i32 {\n    7\n}\n", encoding="utf-8")
        return _PER_FN_RESULT

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
    # The task2a skip is honored only when every cluster's filled tree exists.
    fd = pd / ".skeleton-filled" / c.name / "src"
    fd.mkdir(parents=True)
    (fd / "lib.rs").write_text("fn foo() {}\n", encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("task2a (per-cluster reconstruction) already done" in m
               for m in fake.logs)


def test_resume_task2a_stale_checkpoint_reruns_missing_cluster(tmp_path: Path) -> None:
    """A task2a done-flag written under a DIFFERENT cluster set must not be
    trusted: if a baked cluster has no .skeleton-filled tree, the skip branch
    would fabricate ok=True for it and the merge would silently drop its
    entire reconstruction. The checkpoint is honored only when every baked
    cluster's filled tree is on disk."""
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c],
        phase_valid_keys={"task2a/crate", "task2b/crate", "task2c/crate"},
        manifest=["src/lib.rs"], run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c])
    (pd / "src" / "lib.rs").write_text("fn foo() {}\n", encoding="utf-8")
    # No .skeleton-filled/<cluster> tree on disk → checkpoint is stale.
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert any("stale checkpoint" in m for m in fake.logs)
    assert not any("task2a (per-cluster reconstruction) already done" in m
                   for m in fake.logs)


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


def test_post_merge_green_exit_sweeps_every_cluster_oracle(
        tmp_path: Path) -> None:
    """The default-env run measures only the default cluster's cfg-set — on
    2026-08-12 this loop logged `PASSED (13 passed)` with the 17 extra_cap
    tests unmeasured. A sibling cluster RED under its own env must stop the
    stage from standing behind the tree (no task2c `done` marker)."""
    c1, c2 = _cluster(), _cluster("extra_cap")
    fake = FakeAdapter(
        clusters_list=[c1, c2],
        # default-env sweep green; per-cluster sweep: default green,
        # extra_cap RED under its own cfg.
        run_results=[(0, "ok"), (0, "ok"),
                     (101, "test t9 ... FAILED\n"
                           "test result: FAILED. 3 passed; 1 failed; 0 ignored")])
    pd = _make_project(tmp_path, [c1, c2])
    ok = _gen(fake, _cfg(per_fn=True, tdd=True))._post_merge_fill_pass(
        project_dir=pd, clusters=fake.clusters_list, module="crate",
        functional_req="")
    assert ok is False
    assert "post_merge.cluster_oracle_red" in fake.events


def test_post_merge_green_exit_returns_true_when_all_clusters_green(
        tmp_path: Path) -> None:
    # NEGATIVE CONTROL: both clusters green under their own env → unchanged
    # green exit.
    c1, c2 = _cluster(), _cluster("extra_cap")
    fake = FakeAdapter(clusters_list=[c1, c2],
                       run_results=[(0, "ok"), (0, "ok"), (0, "ok")])
    pd = _make_project(tmp_path, [c1, c2])
    ok = _gen(fake, _cfg(per_fn=True, tdd=True))._post_merge_fill_pass(
        project_dir=pd, clusters=fake.clusters_list, module="crate",
        functional_req="")
    assert ok is True
    assert "post_merge.cluster_oracle_red" not in fake.events


_ORIGINAL_ALSO_FAILS = (
    "     Running tests/basic.rs (target/debug/deps/basic-1)\n"
    "\nrunning 2 tests\ntest a ... ok\ntest b ... ok\n\n"
    "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    "     Running tests/compiletest.rs (target/debug/deps/compiletest-2)\n"
    "\nrunning 1 test\ntest ui ... FAILED\n\n"
    "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
)


def _reference_failing(cluster: str, output: str) -> Any:
    """A usable reference that fails exactly what `output` fails and passes
    what it passes — built from the pipeline's own parser, so the pairs are the
    ones the ruling compares."""
    from pipeline_core.domain.reference_floor import (
        ClusterReference,
        ReferenceMeasurement,
        pairs_of,
    )
    from pipeline_core.domain.test_results import parse_binary_results
    passed, failed, _, _ = pairs_of(parse_binary_results(output))
    return ReferenceMeasurement(key="k", clusters={cluster: ClusterReference(
        cluster=cluster, passed=passed, failed=failed,
        announced=len(passed) + len(failed), reported=len(passed) + len(failed),
        rc=101)})


def test_post_merge_cluster_failing_only_what_the_original_fails_is_not_red(
        tmp_path: Path) -> None:
    """Run 185710-f6135a93 (2026-09-17): task2a closed `nightly` on the
    reference floor (its one failure is a trybuild test the original crate also
    fails), task3 passed it on the same floor, and this sweep alone refused the
    merged tree as RED under its own cfg, so task2c was never marked done. The
    per-cluster sweep rules on the same floor."""
    import dataclasses
    c1, c2 = _cluster(), _cluster("extra_cap")
    fake = FakeAdapter(clusters_list=[c1, c2],
                       run_results=[(0, "ok"), (0, "ok"),
                                    (101, _ORIGINAL_ALSO_FAILS)])
    pd = _make_project(tmp_path, [c1, c2])
    ref = _reference_failing("extra_cap", _ORIGINAL_ALSO_FAILS)
    gen = dataclasses.replace(_gen(fake, _cfg(per_fn=True, tdd=True)),
                              reference_measurement=lambda _pd: ref)
    ok = gen._post_merge_fill_pass(project_dir=pd, clusters=fake.clusters_list,
                                   module="crate", functional_req="")
    assert ok is True
    assert "post_merge.cluster_floor_met" in fake.events
    assert "post_merge.cluster_oracle_red" not in fake.events


def test_post_merge_cluster_red_without_a_reference_or_past_its_floor(
        tmp_path: Path) -> None:
    # NEGATIVE CONTROLS: the same sweep with no reference wired (the pre-fix
    # gate), and with a reference whose original PASSES the failing test, both
    # still refuse the merged tree.
    import dataclasses
    passing_ui = _ORIGINAL_ALSO_FAILS.replace(
        "test ui ... FAILED", "test ui ... ok").replace(
        "FAILED. 0 passed; 1 failed", "ok. 1 passed; 0 failed")
    for wired in (None, _reference_failing("extra_cap", passing_ui)):
        c1, c2 = _cluster(), _cluster("extra_cap")
        fake = FakeAdapter(clusters_list=[c1, c2],
                           run_results=[(0, "ok"), (0, "ok"),
                                        (101, _ORIGINAL_ALSO_FAILS)])
        pd = _make_project(tmp_path / str(wired is None), [c1, c2])
        gen = _gen(fake, _cfg(per_fn=True, tdd=True))
        if wired is not None:
            gen = dataclasses.replace(gen, reference_measurement=lambda _pd, r=wired: r)
        ok = gen._post_merge_fill_pass(
            project_dir=pd, clusters=fake.clusters_list, module="crate",
            functional_req="")
        assert ok is False
        assert "post_merge.cluster_oracle_red" in fake.events


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
        attribute_seq=[(dict(frontier), FailureKind.RUNTIME),
                       (dict(frontier), FailureKind.RUNTIME),
                       (dict(frontier), FailureKind.RUNTIME)],
    )
    pd = _make_project(tmp_path, [c])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert fake.events.count("post_merge.fill.iter") >= 1


def test_post_merge_wave_is_capped_and_the_cursor_walks(
        tmp_path: Path, monkeypatch) -> None:
    """One post-merge iteration was one unbounded wager: the whole eligible
    frontier went to the engine with no mid-wave sweep, so run
    `3579062-476abc3b` landed 120 fills over 6 h and one crashing binary
    voided all of them on a single reading. The cap slices the frontier and
    the stall budget charges per completed traversal, so the plateau exit
    still buys `max_stalls` traversals, not `max_stalls` slices."""
    monkeypatch.setenv("POST_MERGE_MAX_DISPATCH", "2")
    c = _cluster()
    frontier = {(f"src/f{i}.rs", f"fn{i}"): {"t"} for i in range(5)}
    flat = (101, "test result: FAILED. 3 passed; 2 failed")
    fake = FakeAdapter(
        clusters_list=[c], run_results=[flat] * 12,
        attribute_seq=[(dict(frontier), FailureKind.RUNTIME)] * 12)
    batches: list[set] = []
    fake.fill_fns_one_by_one = lambda **kw: (
        batches.append(set(kw["tdd_frontier_keys"])), _PER_FN_RESULT)[1]
    pd = _make_project(tmp_path, [c])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert batches and all(len(b) <= 2 for b in batches), \
        "the cap must hold every iteration"
    # Two full traversals before the plateau exit (max_stalls=2): 3 batches
    # each (2+2+1), every unit dispatched exactly twice.
    assert len(batches) == 6, f"got {len(batches)} batch(es)"
    counts: dict = {}
    for b in batches:
        for k in b:
            counts[k] = counts.get(k, 0) + 1
    assert set(counts) == set(frontier) and set(counts.values()) == {2}, (
        "a capped flat run must spend whole traversals — a partial cohort "
        "at the exit is the premature close the 2026-08-18 night recorded")
    stop = [ln for ln in fake.logs if "plateau" in ln and "stopping" in ln]
    assert stop and "5 of 5 frontier fn(s) were dispatched" in stop[0]


def test_post_merge_below_floor_needs_a_reproduced_breach(
        tmp_path: Path) -> None:
    """A crashed binary removes numerator and denominator together, so one
    below-floor reading can be noise: the overnight run's `32/2` (96 tests
    announced-but-silent) convicted and destroyed the 120-fill wave three
    times. Only a reproduced breach earns the destructive restore."""
    c = _cluster()
    frontier = {("src/lib.rs", "foo"): {"t"}}
    good = (101, "test result: FAILED. 5 passed; 1 failed")
    crashy = (101, "test result: FAILED. 1 passed; 1 failed")
    fake = FakeAdapter(
        clusters_list=[c],
        # iter1 sets the floor at 5; iter2 reads a 1 (noise); the confirm
        # re-sweep reads 5 again → keep the tree.
        run_results=[good, crashy, good, good],
        attribute_seq=[(dict(frontier), FailureKind.RUNTIME)] * 4)
    pd = _make_project(tmp_path, [c])
    (pd / "src" / "lib.rs").write_text("fn foo() {}\n", encoding="utf-8")
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert "post_merge.breach_not_reproduced" in fake.events
    assert "post_merge.tree_restored" not in fake.events, (
        "a single unreproduced below-floor reading destroyed the wave — "
        "the recorded crash-noise conviction")


def test_post_merge_reproduced_breach_restores_then_resweeps(
        tmp_path: Path) -> None:
    """A reproduced breach restores — and the iteration then re-enters for a
    fresh sweep instead of filling from the discarded tree's attribution
    (the recorded run filled the restored tree from the crashed wave's
    output, then exhausted units on the mismatch). A restored tree that
    re-measures below the floor re-bases the high-water rather than looping
    restore -> sweep -> restore."""
    c = _cluster()
    frontier = {("src/lib.rs", "foo"): {"t"}}
    good = (101, "test result: FAILED. 5 passed; 1 failed")
    crashy = (101, "test result: FAILED. 1 passed; 1 failed")
    fake = FakeAdapter(
        clusters_list=[c],
        # iter1 floor=5; iter2 reads 1, confirm reads 1 (reproduced) →
        # restore + continue; iter3 fresh sweep still 1 → re-base, no second
        # restore.
        run_results=[good, crashy, crashy, crashy, crashy, crashy],
        attribute_seq=[(dict(frontier), FailureKind.RUNTIME)] * 6)
    fills_by_iter: list[int] = []
    fake.fill_fns_one_by_one = lambda **kw: (
        fills_by_iter.append(1), _PER_FN_RESULT)[1]
    pd = _make_project(tmp_path, [c])
    (pd / "src" / "lib.rs").write_text("fn foo() {}\n", encoding="utf-8")
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert "post_merge.tree_restored" in fake.events
    restore_count = fake.events.count("post_merge.tree_restored")
    assert restore_count == 1, (
        f"{restore_count} restores — the restore/re-sweep pair must not loop")
    assert "post_merge.highwater_rebased" in fake.events
    # The iteration that restored did not fill from the stale attribution:
    # iter1 filled, iter2 restored+continued (no fill), iter3+ resumed.
    assert any("re-based" in ln for ln in fake.logs)


def test_post_merge_batch_that_breaks_a_cfg_world_is_reverted(
        tmp_path: Path, monkeypatch) -> None:
    """The second cfg-world seam: a fill written INTO the merged tree passes
    only default-world gates (the engine's splice check, the sweep), so a
    body reading an item another world's cfg removes ships clean and
    surfaces at task3 — one boundary after the merge-time world check. The
    gate is per batch: a divergent world that was green and went red after
    the batch convicts the batch fns its own compiler implicates."""
    c1, c2 = _cluster(), _cluster("nightly")
    c2.env_extra = {"RUSTFLAGS": "--cfg error_generic_member_access"}
    key = ("src/lib.rs", "foo")
    flat = (101, "test result: FAILED. 3 passed; 2 failed")
    fake = FakeAdapter(
        clusters_list=[c1, c2],
        # baseline world check green; iter1 sweep flat; post-batch world
        # check RED; post-revert recheck green; iter2 sweep flat + world
        # green; iter3 sweep flat → plateau exit.
        run_results=[(0, "ok"), flat, (101, "error[E0609]: no field"),
                     (0, "ok"), flat, (0, "ok"), flat],
        attribute_seq=[({key: {"t"}}, FailureKind.RUNTIME)] * 3,
        diag_results=[(101, "error[E0609]: no field",
                       [_Diag("src/lib.rs", "E0609", "no field")])],
        implicated=[key],
    )
    reverts: list[list] = []
    monkeypatch.setattr(
        StaticSkeletonTask2Generator, "_revert_implicated",
        lambda self, tag, implicated, *a, **k: (
            reverts.append(list(implicated)), len(implicated))[1])
    pd = _make_project(tmp_path, [c1, c2])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert "post_merge.world_broken_by_batch" in fake.events
    data = dict(fake.event_data)["post_merge.world_broken_by_batch"]
    assert data["world"] == "nightly"
    assert reverts == [[key]], (
        "the batch fn the red world's compiler implicates must be reverted "
        "— the default-world splice gate accepted a body one world cannot "
        "build")


def test_post_merge_world_red_at_entry_is_inherited_not_relitigated(
        tmp_path: Path, monkeypatch) -> None:
    """A world already red when the loop starts was broken upstream (the
    merge-time check names it); charging every later batch with it would
    revert innocent fills each iteration."""
    c1, c2 = _cluster(), _cluster("nightly")
    c2.env_extra = {"RUSTFLAGS": "--cfg error_generic_member_access"}
    key = ("src/lib.rs", "foo")
    flat = (101, "test result: FAILED. 3 passed; 2 failed")
    red = (101, "error[E0609]: no field")
    fake = FakeAdapter(
        clusters_list=[c1, c2],
        # baseline RED; every post-batch check stays red.
        run_results=[red, flat, red, flat, red, flat],
        attribute_seq=[({key: {"t"}}, FailureKind.RUNTIME)] * 3,
    )
    monkeypatch.setattr(
        StaticSkeletonTask2Generator, "_revert_implicated",
        lambda self, *a, **k: (_ for _ in ()).throw(
            AssertionError("an inherited world break convicted a batch")))
    pd = _make_project(tmp_path, [c1, c2])
    _post(fake, pd, _cfg(per_fn=True, tdd=True))
    assert "post_merge.world_broken_by_batch" not in fake.events


def test_validate_reverts_every_same_name_fn(tmp_path: Path) -> None:
    """`fmt` lives in several impls; the bare-name implication must revert
    every occurrence, not just the first."""
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        # run_check_diag queue: pre-pass heal, gen_tests leak guard, then
        # validate iter1 FAILS with a structured diagnostic; iter2 falls through
        # to the fake's clean rc=0 default.
        diag_results=[(0, "", []), (0, "", []),
                      (1, "err", [_Diag("src/lib.rs", "E0599", "boom")])],
        implicated=[("src/lib.rs", "fmt")],
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    two_fmts = (
        "impl Debug for E {\n"
        "    fn fmt(&self, f: &mut F) -> R { broken_a() }\n"
        "}\n"
        "impl Display for E {\n"
        "    fn fmt(&self, f: &mut F) -> R { broken_b() }\n"
        "}\n"
    )
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    (filled / "lib.rs").write_text(two_fmts, encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    text = (filled / "lib.rs").read_text(encoding="utf-8")
    assert "broken_a" not in text and "broken_b" not in text
    assert text.count("unimplemented!()") == 2
    assert any("reverted 2 fn(s)" in m for m in fake.logs)


def test_validate_restores_file_from_skeleton_when_nothing_revertible(
        tmp_path: Path) -> None:
    """Corruption outside any locatable fn body (e.g. a splice that broke out
    of a doc-comment) leaves zero revertible fns — the implicated file must be
    restored wholesale from the skeleton instead of looping identically."""
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        # run_check_diag queue: pre-pass heal, gen_tests leak guard, then
        # validate iter1 FAILS with a structured diagnostic; iter2 falls through
        # to the fake's clean rc=0 default.
        diag_results=[(0, "", []), (0, "", []),
                      (1, "err", [_Diag("src/lib.rs", "E0599", "boom")])],
        implicated=[("src/lib.rs", "fmt")],  # not locatable in the corrupt file
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    (filled / "lib.rs").write_text("/// # fn fmt(&self) {\nstray tokens }\n",
                                   encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    text = (filled / "lib.rs").read_text(encoding="utf-8")
    skel = (pd / ".skeleton" / c.name / "src" / "lib.rs").read_text(
        encoding="utf-8")
    # Corruption gone, skeleton content back (later steps may append
    # gen_tests decls, so containment — not byte equality).
    assert "stray tokens" not in text and skel.strip() in text
    assert any("restored whole file from skeleton" in m for m in fake.logs)


def test_prepass_heal_reverts_poisoned_seeded_body(tmp_path: Path) -> None:
    """A broken body admitted without a compile gate (donor seeding / resume)
    must be reverted BEFORE the fill pass — not after a full pass of
    downstream_only failures burns LLM calls."""
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        # phase-A ok; validate ok (run_raw). The PRE-PASS check now runs through
        # run_check_diag, so its failure — with a structured diagnostic — is
        # supplied via diag_results, and the fn is attributed by the diagnostics
        # implicator (not the rendered-text one).
        raw_results=[(0, "", ""), (0, "", "")],
        diag_results=[(1, "err", [_Diag("src/lib.rs", "E0605", "boom")])],
        implicated=[("src/lib.rs", "foo")],
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    (filled / "lib.rs").write_text(
        "fn foo() -> i32 { broken_donor_body() }\n", encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    text = (filled / "lib.rs").read_text(encoding="utf-8")
    assert "broken_donor_body" not in text and "unimplemented!()" in text
    assert any("[pre-pass-heal]" in m for m in fake.logs)
    assert "phase_c.cluster.prepass_heal" in fake.events


def test_validate_gate_attributes_from_structured_diagnostics(
        tmp_path: Path) -> None:
    """skeleton-validate must hand the STRUCTURED diagnostics to the attribution
    policy. Handing over only rendered text loses the macro-expansion call-site
    chain, so an error hidden inside a macro implicates nobody and this gate —
    whose no-attribution branch gives up outright — strands the cluster red."""
    c = _cluster()
    boom = _Diag("src/lib.rs", "E0605", "non-primitive cast")
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        diag_results=[(0, "", []), (0, "", []), (1, "err", [boom])],
        implicated=[("src/lib.rs", "foo")],
        leaf_order={"foo": 1}, run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    # The failing check's diagnostics reached the policy — not an empty list.
    assert any(diags == [boom] for diags, _ in fake.implicated_calls), \
        fake.implicated_calls
    assert any("reverted" in m for m in fake.logs)


def test_halt_verdict_comes_from_a_fresh_check_not_the_stale_loop_text(
        tmp_path: Path) -> None:
    """The halt alarm must describe the tree AS IT IS when it fires. The loop's
    last check predates its own revert+refill, so a tree the reverts healed back
    to compiling was still announced as unrecoverable — with an error excerpt
    nobody could reproduce."""
    c = _cluster()
    boom = _Diag("src/lib.rs", "E0599", "boom")
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        # pre-pass heal, gen_tests leak guard, validate iter1 + iter2 both FAIL,
        # then the halt-path re-check finds the reverted tree compiling clean.
        diag_results=[(0, "", []), (0, "", []),
                      (1, "stale err", [boom]), (1, "stale err", [boom]),
                      (0, "", [])],
        implicated=[("src/lib.rs", "foo")],
        leaf_order={"foo": 1}, run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    (filled / "lib.rs").write_text("fn foo() -> i32 { broken() }\n",
                                   encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    halted = [d for ev, d in fake.event_data if ev == "phase_c.cluster.halted"]
    assert len(halted) == 1, fake.events
    assert halted[0]["health"] == "OK"
    assert halted[0]["first_errors"] == []
    assert any("HALTED (OK) — validate budget exhausted, but the tree compiles"
               in m for m in fake.logs), fake.logs


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


def test_prepass_heal_loops_until_the_tree_parses(tmp_path: Path) -> None:
    """A syntax error ABORTS the parse, so rustc names ONE unparseable file per
    invocation. With two corrupted files a single heal round repairs one and
    hands the fill pass a tree that still cannot build a single test binary —
    the whole cluster then reads 0 passing for the rest of the run."""
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        # Round 1 blames lib.rs; only once that parses does error.rs surface.
        diag_results=[
            (1, "err", [_Diag("src/lib.rs", "", "unexpected closing delimiter")]),
            (1, "err", [_Diag("src/error.rs", "", "unexpected closing delimiter")]),
            (0, "", []),
        ],
        implicated=[],          # a syntax error carries NO fn attribution
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    sk = pd / ".skeleton" / c.name / "src"
    (sk / "error.rs").write_text("fn bar() -> i32 {\n    unimplemented!()\n}\n",
                                 encoding="utf-8")
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    for name in ("lib.rs", "error.rs"):
        (filled / name).write_text("fn x() { 1 }\n}\n", encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    rounds = [m for m in fake.logs if "[pre-pass-heal] round" in m
              and "does not compile" in m]
    assert len(rounds) == 2, rounds
    assert "src/lib.rs" in rounds[0] and "src/error.rs" in rounds[1]
    assert any("tree compiles again after 2 round(s)" in m for m in fake.logs)
    assert not any("STILL BROKEN" in m for m in fake.logs)


def test_hollow_bodies_are_un_nested_before_any_heal_can_revert_them(
        tmp_path: Path) -> None:
    """A body that only re-declares its own fn compiles and holds no
    `unimplemented!()`, so nothing downstream can see it — but the surplus brace
    such a reply carries IS a parse error the heal loop reacts to by reverting
    the fn to a stub, discarding a body that only needed un-nesting."""
    c = _cluster()
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        diag_results=[(0, "", [])],
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    (filled / "lib.rs").write_text(
        "fn foo() -> i32 {\n    fn foo() -> i32 {\n        41 + 1\n    }\n}\n",
        encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    text = (filled / "lib.rs").read_text(encoding="utf-8")
    assert text.count("fn foo") == 1 and "41 + 1" in text
    assert "unimplemented!()" not in text          # repaired, not reverted
    assert any("[hollow-repair] lib.rs::foo" in m for m in fake.logs), fake.logs
    assert "phase_c.cluster.hollow_repair" in fake.events


def test_prepass_heal_stops_when_a_round_changes_nothing(tmp_path: Path) -> None:
    """Same culprit after a heal ⇒ no restore point left. Loop, don't spin."""
    c = _cluster()
    same = [_Diag("src/lib.rs", "", "unexpected closing delimiter")]
    fake = FakeAdapter(
        clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
        raw_results=[(0, "", ""), (0, "", "")],
        diag_results=[(1, "err", same), (1, "err", same), (1, "err", same)],
        implicated=[],
        run_results=[(0, "ok")],
    )
    pd = _make_project(tmp_path, [c], with_gen_tests=True)
    filled = pd / ".skeleton-filled" / c.name / "src"
    filled.mkdir(parents=True)
    (filled / "lib.rs").write_text("fn x() { 1 }\n}\n", encoding="utf-8")
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=False))) is True
    rounds = [m for m in fake.logs if "[pre-pass-heal] round" in m
              and "does not compile" in m]
    assert len(rounds) == 1, rounds
    assert any("STILL BROKEN after 1 round(s)" in m for m in fake.logs)
    assert "phase_c.cluster.prepass_heal_failed" in fake.events


def test_the_env_handed_to_the_loop_is_the_clusters_own_not_the_active_scopes(
        tmp_path: Path) -> None:
    """A nightly cluster's env carries RUSTUP_TOOLCHAIN and keeps -A warnings
    beside the cfg. The sweep never noticed the old shape because its command
    line says `cargo +nightly`; the assert probe and the coverage instrument,
    which compose their own cargo from this env alone, ran on stable with the
    cfg on and failed E0554 (run 216855-03b2a21b, cluster nightly)."""
    nightly = BuildCluster(
        name="nightly", env_extra={"RUSTFLAGS": "--cfg error_generic_member_access"},
        tarpaulin_exclude_files=[], excluded_cfg=[],
        active_cfg=["error_generic_member_access"], prompt_nightly_rule="",
        requires_nightly=True)
    fake = FakeAdapter(clusters_list=[nightly])
    gen = _gen(fake, _cfg(per_fn=False, tdd=True))
    env = gen._cluster_env(nightly)
    assert env["RUSTUP_TOOLCHAIN"] == "nightly"
    assert env["RUSTFLAGS"] == "-A warnings --cfg error_generic_member_access"
    # Negative control: the old composition — the active scope's env (never
    # set in task2) with env_extra merged by dict.update — is what the live
    # probe stage recorded: rustflags ['--cfg', 'error_generic_member_access'],
    # no toolchain.
    old = fake.env()
    old.update(nightly.env_extra)
    assert old == {"RUSTFLAGS": "--cfg error_generic_member_access"}
    assert "RUSTUP_TOOLCHAIN" not in old
