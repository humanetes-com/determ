"""Fake-adapter unit tests for `StaticSkeletonTask2Generator`.

The 4-phase skeleton generator is a use-case over the `ports.generator` roles
(Cargo, FillEngine, Clusters, GenTests, SourceScan, Telemetry, Workspace). These
tests inject one fake object satisfying every role (mirroring `_Task2Adapter`)
and drive each branch — happy paths (per-group, per-fn fn_walk, per-fn tdd),
resume skips, abort guards, the validate-revert loop, the one-tree loop over
every world, and the publish. No cargo, no LLM, no real pipeline.py.
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
from pipeline_core.domain.pipeline_crash import PipelineCodeCrash
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
    # (cwd, cwd had its own Cargo.toml) per run_check_diag: a check run in a
    # directory without a manifest builds whatever crate sits above it.
    checked_dirs: list[tuple[Path, bool]] = field(default_factory=list)

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
        self.checked_dirs.append((Path(cwd), (Path(cwd) / "Cargo.toml").is_file()))
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
                           cluster_name: str, *, cluster: Any) -> AttributionResult:
        fr, kind = (self.attribute_seq.pop(0) if self.attribute_seq
                    else ({}, FailureKind.NO_FRONTIER))
        return AttributionResult(fr, FailureKind(kind), dict(fr))

    def admit_frontier(self, frontier: Any, src_dir: Path, *,
                       cluster: Any) -> Any:
        return dict(frontier)

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
        recently_filled_max=5,
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
    assert any("tree aborted" in m for m in fake.logs)


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
    assert not any("tree aborted" in m for m in fake.logs)
    assert any("salvaging" in m for m in fake.logs)


def test_cluster_exception_caught(tmp_path: Path) -> None:
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], raise_in_filter=True,
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, [c])
    with pytest.raises(PipelineCodeCrash):
        _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True)))
    assert any("tree crashed" in m for m in fake.logs)


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
    assert any("tree aborted" in m for m in fake.logs)
    assert "task2a/crate" not in [k for k, s_, _d in fake.marks if s_ == "done"]


def test_checkpointed_cluster_reruns_when_its_tree_is_gone(tmp_path: Path) -> None:
    # Marker without artifact is the stale-checkpoint case task2b already
    # guards: the tree's loop runs, it is not fabricated as ok.
    cs = [_cluster("default")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")],
                       phase_valid_keys={"task2a/crate"})
    pd = _make_project(tmp_path, cs)
    ran: list[str] = []
    fake.run_phase_c_tdd = lambda **kw: (  # type: ignore[method-assign]
        ran.append(kw["cluster"].name) or _PER_FN_RESULT)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert ran == ["default"]


def test_a_fresh_cluster_tree_is_a_crate_before_its_first_check(
        tmp_path: Path) -> None:
    """Every fresh cluster entry used to run the ownership backfill's compile
    gate and the pre-pass heal before the tree had its own `Cargo.toml`, so
    cargo built the workspace's stripped shell above it: eight `reverted 0`
    heal rounds and `STILL BROKEN` on a tree that compiles (checkpoint
    `18fb6a03d6`, the entry of run `412606-f903acb4`, replayed 2026-10-17).
    Every check on a seeded tree now runs where the tree's manifest, tests
    and gen_tests siblings already are."""
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, cs, with_gen_tests=True)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    filled = [(d, has) for d, has in fake.checked_dirs
              if ".skeleton-filled" in d.parts]
    assert filled, "the entry checks ran on the cluster trees"
    assert all(has for _d, has in filled), filled
    tree = pd / ".skeleton-filled" / "default"
    assert (tree / "tests").is_symlink()
    assert (tree / "src" / "lib.gen_tests_1.rs").is_file()


def test_control_without_the_crate_root_the_entry_check_has_no_manifest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: with the crate-root setup out of the seed, as
    before the change, the first check on a fresh tree runs where there
    is no manifest."""
    cs = [_cluster("default"), _cluster("nightly")]
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    pd = _make_project(tmp_path, cs)
    monkeypatch.setattr(StaticSkeletonTask2Generator, "_establish_crate_root",
                        lambda *a, **k: None)
    _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True)))
    filled = [has for d, has in fake.checked_dirs if ".skeleton-filled" in d.parts]
    assert filled and not filled[0]


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
    assert any("task2a (the one tree) already done" in m
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




# --- one tree, one loop, every world ---------------------------------------


def _two_worlds() -> list[BuildCluster]:
    return [_cluster("default"),
            replace(_cluster("nightly"), active_cfg=["egma"],
                    env_extra={"RUSTFLAGS": "--cfg egma"})]


def test_two_worlds_are_one_loop_over_one_tree(tmp_path: Path) -> None:
    """On run `1680589-7a6824f8` (2026-10-19) task 2a ran one loop per
    cluster and the two filled trees came out byte-identical. Two worlds now
    run ONE loop, on the default world's tree, whose env makes every test run
    cover each world, over the tests of both worlds; nothing merges, and the
    stage keeps one checkpoint."""
    from pipeline_core.domain.cluster_env import ALL_WORLDS_VAR, WORLD_VAR
    cs = _two_worlds()
    fake = FakeAdapter(clusters_list=cs, run_results=[(0, "ok")])
    by_world = {"default": [("tests/it.rs", "x")],
                "nightly": [("tests/it.rs", "x"), ("tests/n.rs", "y")]}
    fake.filter_tests_for_cluster = (  # type: ignore[method-assign]
        lambda pairs, c: list(by_world[c.name]))
    calls: list[dict[str, Any]] = []
    fake.run_phase_c_tdd = lambda **kw: (  # type: ignore[method-assign]
        calls.append(kw) or _PER_FN_RESULT)
    merged: list[Any] = []
    fake.merge_cluster_outputs = lambda **kw: (  # type: ignore[method-assign]
        merged.append(kw) or {"files_written": 1, "cfg_gated_fns": 0})
    pd = _make_project(tmp_path, cs)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert len(calls) == 1
    kw = calls[0]
    assert kw["cluster"].name == "default"
    assert kw["target_src_dir"] == pd / ".skeleton-filled" / "default"
    assert sorted(w.name for w in kw["worlds"]) == ["default", "nightly"]
    assert kw["cluster_env"][ALL_WORLDS_VAR] == "1"
    assert kw["cluster_env"][WORLD_VAR] == "default"
    assert sorted(r for r, _t in kw["cluster_tests"]) == ["tests/it.rs", "tests/n.rs"]
    assert merged == []
    done = [k for k, st, _ in fake.marks if st == "done"]
    assert "task2a/crate" in done
    assert not [k for k in done if k.startswith("task2a/crate/")]
    assert not (pd / ".skeleton-filled" / "nightly").exists()


def test_one_world_is_the_loop_it_always_was(tmp_path: Path) -> None:
    """Control: a crate with one world runs that world's own identity, and
    its env does not ask the runner for other worlds."""
    from pipeline_core.domain.cluster_env import ALL_WORLDS_VAR
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    calls: list[dict[str, Any]] = []
    fake.run_phase_c_tdd = lambda **kw: (  # type: ignore[method-assign]
        calls.append(kw) or _PER_FN_RESULT)
    pd = _make_project(tmp_path, [c])
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    assert calls[0]["cluster"] is c
    assert ALL_WORLDS_VAR not in calls[0]["cluster_env"]


def test_the_published_src_is_the_tree_the_loop_judged(tmp_path: Path) -> None:
    from pipeline_core.application.reconstruct.publish import published_drift
    cs = _two_worlds()
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])

    def _fill(**kw: Any) -> ClusterFillResult:
        (kw["target_src_dir"] / "src" / "lib.rs").write_text(
            "fn foo() -> i32 {\n    7\n}\n", encoding="utf-8")
        return _PER_FN_RESULT
    fake.run_phase_c_tdd = _fill  # type: ignore[method-assign]
    pd = _make_project(tmp_path, cs)
    assert _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True))) is True
    tree = pd / ".skeleton-filled" / "default"
    assert (pd / "src" / "lib.rs").read_bytes() == (tree / "src" / "lib.rs").read_bytes()
    assert published_drift(tree, pd) == ()
    (pd / "src" / "lib.rs").write_text("fn foo() -> i32 { 8 }\n", encoding="utf-8")
    assert published_drift(tree, pd) == ("src/lib.rs",)


def test_a_crash_in_the_one_tree_raises_and_publishes_nothing(
        tmp_path: Path) -> None:
    """A pipeline exception in the loop ends the run on PipelineCodeCrash
    (run `486681-a304b30a`, 2026-10-10: a caught TypeError was logged as an
    exhausted cluster and checkpointed). Nothing is checkpointed and nothing
    reaches `src/`."""
    cs = _two_worlds()
    fake = FakeAdapter(clusters_list=cs, cluster_tests=[("tests/it.rs", "x")],
                       run_results=[(0, "ok")])
    fake.run_phase_c_tdd = lambda **kw: ClusterFillResult(  # type: ignore[method-assign]
        failed_tests=frozenset({"t"}), ran_tests=frozenset({"t"}),
        crashed="TypeError: run() got an unexpected keyword argument 'x'",
        crash_traceback="Traceback (most recent call last): ...")
    pd = _make_project(tmp_path, cs)
    before = (pd / "src").exists() and sorted((pd / "src").rglob("*"))
    with pytest.raises(PipelineCodeCrash) as exc:
        _run(pd, _gen(fake, _cfg(per_fn=True, tdd=True)))
    assert exc.value.crash.cluster == "default"
    assert not [k for k, st, _ in fake.marks if st == "done"
                and k.startswith("task2")]
    assert ((pd / "src").exists() and sorted((pd / "src").rglob("*"))) == before
