"""The boundary invariant inside the real driver: the tree a sweep scores must
parse, and a file the extractor cannot parse goes to the heal hierarchy NOW —
whichever writer left it that way — instead of after three identical red
sweeps and a cluster abort.

Plan sentences, each with its control in the same module:

* a cluster tree with one unparseable `src` file at iteration start is healed
  BEFORE the sweep, and the sweep runs on a tree that parses — control: with
  no heal wired the boundary only reports and the sweep reads the broken
  tree (the pre-fix shape);
* a sweep whose diagnostic is uncoded (parse-level) under `src/` on a file the
  extractor confirms unparseable is healed and NOT charged: the next
  iteration sweeps the healed tree and the red-build refusal never fires —
  control: with no heal wired the identical red sweep repeats and the
  refusal fires on the third (run `2973-66e50ad9`, 2026-09-04);
* the post-merge wave takes the same reading on the merged tree before its
  own sweep, healing through the merged best-tree snapshot — control: the
  wave composed without a structure reader sweeps the broken tree;
* the legacy per-fn route takes the same reading before its per-pass test —
  control: with no heal wired it reports and the pass test reads the
  broken tree.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from pipeline_core.domain.models import BuildCluster, FnLocator
from tests.test_tdd_ratchet import _driver_deps

_LAYOUT = """\
pub struct Own { pub ptr: usize }

impl Own {
    pub fn depth(&self) -> usize {
        unimplemented!()
    }
}
"""
_BROKEN = _LAYOUT + "}\n"      # one closing brace too many: syn refuses it
_UNIT = FnLocator("src/a.rs", "depth")
_SWEEP = ("test t1 ... ok\ntest t2 ... FAILED\n"
          "test result: FAILED. 1 passed; 1 failed; 0 ignored")
_RED_UNCODED = (
    "error: unexpected closing delimiter: `}`\n"
    " --> src/a.rs:8:1\n"
    "error: could not compile `anyhow` (lib) due to 1 previous error\n"
)


def _parses(p: Path) -> bool:
    t = p.read_text(encoding="utf-8")
    return t.count("{") == t.count("}")


def _run(monkeypatch, tmp_path: Path, *, cargo, heal, events: list, logs: list,
         max_stalls: str = "1") -> None:
    from pipeline_core.application.reconstruct.phase_c_tdd import run_phase_c_tdd
    monkeypatch.setenv("TDD_MAX_STALLS", max_stalls)
    monkeypatch.setenv("TDD_FLAT_SCORE_REPEATS", "0")
    monkeypatch.setenv("TDD_NO_VERDICT_SWEEPS", "0")
    deps = _driver_deps(
        cargo, lambda pd, cl, stems, lib=False: (2, frozenset({"t1", "t2"})),
        events, None, frontier={_UNIT: {"t2"}}, logs=logs,
        stubs=[_UNIT], tree_stubs=[_UNIT])
    deps = replace(deps, parse_ok=_parses)
    run_phase_c_tdd(
        deps, project_dir=tmp_path, target_src_dir=tmp_path, tests_dir_files=[],
        cluster_tests=[("tests/t.rs", "fn t1() {}")], module="m",
        cluster=BuildCluster(
            name="stable", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule=""),
        check_cmd="cargo check", cluster_env={}, leaf_counts=None, fr_path=None,
        test_cmd_builder=lambda stems, include_lib=False: "cargo test",
        tag="[stable]", heal_file=heal)


def _tree(tmp_path: Path, text: str) -> Path:
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.rs").write_text(text, encoding="utf-8")
    return src / "a.rs"


def test_an_unparseable_file_is_healed_before_the_first_sweep(
        monkeypatch, tmp_path: Path) -> None:
    a = _tree(tmp_path, _BROKEN)
    seen_at_sweep: list[bool] = []
    healed: list[str] = []
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        seen_at_sweep.append(_parses(a))
        return 101, _SWEEP

    def _heal(rel: str) -> bool:
        healed.append(rel)
        a.write_text(_LAYOUT, encoding="utf-8")
        return True

    _run(monkeypatch, tmp_path, cargo=_cargo, heal=_heal, events=events, logs=logs)
    assert healed == ["src/a.rs"]
    assert seen_at_sweep and seen_at_sweep[0] is True, (
        "the first sweep must run on a tree that parses")
    assert any("TREE UNPARSEABLE before the sweep — src/a.rs" in ln for ln in logs)
    ev = [d for k, d in events if k == "phase_c.tdd.unparseable_tree"]
    assert ev and ev[0]["files"] == ["src/a.rs"] and ev[0]["healed"] == ["src/a.rs"]
    assert ev[0]["when"] == "before the sweep"


def test_control_with_no_heal_wired_the_boundary_only_reports(
        monkeypatch, tmp_path: Path) -> None:
    a = _tree(tmp_path, _BROKEN)
    seen_at_sweep: list[bool] = []
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        seen_at_sweep.append(_parses(a))
        return 101, _SWEEP

    _run(monkeypatch, tmp_path, cargo=_cargo, heal=None, events=events, logs=logs)
    assert seen_at_sweep and seen_at_sweep[0] is False, (
        "the pre-fix shape: the sweep reads the broken tree")
    assert any("NO heal wired" in ln for ln in logs)
    ev = [d for k, d in events if k == "phase_c.tdd.unparseable_tree"]
    assert ev and ev[0]["healed"] == []


def test_an_uncoded_sweep_diagnostic_is_healed_and_not_charged(
        monkeypatch, tmp_path: Path) -> None:
    """A writer that runs between the boundary check and the sweep (here the
    cargo fake itself breaks the file, standing in for a mid-iteration
    restore) leaves an uncoded parse error on the sweep. The reaction: heal,
    do not charge, sweep again."""
    a = _tree(tmp_path, _LAYOUT)
    calls: list[int] = []
    healed: list[str] = []
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    def _cargo(*_a: Any, **_k: Any) -> tuple[int, str]:
        calls.append(1)
        if len(calls) == 1:
            a.write_text(_BROKEN, encoding="utf-8")
            return 101, _RED_UNCODED
        return 101, _SWEEP

    def _heal(rel: str) -> bool:
        healed.append(rel)
        a.write_text(_LAYOUT, encoding="utf-8")
        return True

    _run(monkeypatch, tmp_path, cargo=_cargo, heal=_heal, events=events, logs=logs)
    assert healed == ["src/a.rs"]
    assert len(calls) >= 2, "the next iteration swept the healed tree"
    ev = [d for k, d in events if k == "phase_c.tdd.unparseable_tree"]
    assert ev and ev[0]["when"] == "in the sweep's diagnostics"
    assert not [k for k, _d in events if k == "phase_c.tdd.red_build_refusal"]
    assert not any("RED — the cluster cannot compile" in ln for ln in logs)


def test_control_with_no_heal_the_identical_red_sweep_repeats_until_the_refusal(
        monkeypatch, tmp_path: Path) -> None:
    """Today's path, kept as the guard for coded errors and for a heal that
    cannot help: three identical red sweeps, then the refusal."""
    _tree(tmp_path, _BROKEN)
    events: list[tuple[str, dict]] = []
    logs: list[str] = []
    _run(monkeypatch, tmp_path,
         cargo=lambda *_a, **_k: (101, _RED_UNCODED), heal=None,
         events=events, logs=logs, max_stalls="0")
    assert [k for k, _d in events if k == "phase_c.tdd.red_build_refusal"]
    assert any("IDENTICAL across 3 consecutive" in ln for ln in logs)


# --- the post-merge wave --------------------------------------------------

def _merged_wave(tmp_path: Path, *, with_reader: bool):
    from types import SimpleNamespace

    from pipeline_core.application.generators.task2_generator import (
        StaticSkeletonTask2Generator,
    )
    from tests.test_skeleton_task2_generator import (
        FakeAdapter,
        _cfg,
        _cluster,
        _make_project,
    )
    c = _cluster()
    fake = FakeAdapter(clusters_list=[c], run_results=[(0, "ok")] * 4)
    pd = _make_project(tmp_path, [c])
    lib = pd / "src" / "lib.rs"
    lib.write_text(_BROKEN, encoding="utf-8")
    best = pd / ".best_tree" / "_merged"
    best.mkdir(parents=True)
    (best / "lib.rs").write_text(_LAYOUT, encoding="utf-8")
    seen_at_sweep: list[bool] = []
    real_run = fake.run

    def _run(cmd: str, cwd: Path, env: dict, timeout: int = 180):
        seen_at_sweep.append(_parses(lib))
        return real_run(cmd, cwd, env, timeout)

    fake.run = _run  # type: ignore[method-assign]
    gen = StaticSkeletonTask2Generator(
        tel=fake, cargo=fake, fill=fake, clusters=fake, gentests=fake,
        scan=fake, ws=fake, cfg=_cfg(per_fn=True, tdd=True),
        structure=SimpleNamespace(parse_ok=_parses) if with_reader else None)
    gen._post_merge_fill_pass(project_dir=pd, clusters=[c], module="crate",
                              functional_req="")
    return lib, seen_at_sweep, fake


def test_the_post_merge_wave_heals_an_unparseable_merged_file_before_its_sweep(
        tmp_path: Path) -> None:
    lib, seen, fake = _merged_wave(tmp_path, with_reader=True)
    assert seen and seen[0] is True, "the merged sweep must run on a tree that parses"
    assert lib.read_text(encoding="utf-8") == _LAYOUT, "restored from the merged best tree"
    ev = [d for k, d in fake.event_data if k == "phase_c.tdd.unparseable_tree"]
    assert ev and ev[0]["when"] == "before the post-merge sweep"
    assert ev[0]["files"] == ["src/lib.rs"] and ev[0]["healed"] == ["src/lib.rs"]
    assert ev[0]["cluster"] == "merged" and ev[0]["iter"] == 1
    assert any("post-merge iter 1: TREE UNPARSEABLE before the post-merge sweep"
               " — src/lib.rs" in ln for ln in fake.logs)
    assert any("[heal] src/lib.rs: restored from best-tree snapshot" in ln
               for ln in fake.logs)


def test_control_the_post_merge_wave_without_a_reader_sweeps_the_broken_tree(
        tmp_path: Path) -> None:
    lib, seen, fake = _merged_wave(tmp_path, with_reader=False)
    assert seen and seen[0] is False, "the pre-fix shape: no boundary, red sweep"
    assert lib.read_text(encoding="utf-8") == _BROKEN
    assert "phase_c.tdd.unparseable_tree" not in fake.events


# --- the legacy per-fn route -----------------------------------------------

def _legacy_route(tmp_path: Path, *, heal):
    from pipeline_core.domain.enums import FillUnit
    from tests.test_fill_engine_usecases import FakeFill, _engine, _mk_ws
    ws = _mk_ws(tmp_path)
    lib = ws / "src" / "lib.rs"
    lib.write_text(_BROKEN, encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "t.rs").write_text("#[test] fn t() {}", encoding="utf-8")
    live = {"file": "src/lib.rs", "fn_name": "depth", "sig_text": "fn depth()"}
    fake = FakeFill(stub_tuples=[], stub_records=[live], run=(0, ""),
                    parse_ok=_parses)
    seen_at_test: list[bool] = []
    real_run = fake.run

    def _run(*a, **k):
        if a and a[0] == "cargo test":
            seen_at_test.append(_parses(lib))
        return real_run(*a, **k)

    fake.run = _run  # type: ignore[method-assign]
    _engine(fake, FillUnit.FN).run(
        ws, ws / "src", [ws / "tests" / "t.rs"], "m", "stable",
        cluster=None, cluster_env={}, max_passes=1,
        skip_end_pass_test=False, heal_file=heal)
    return lib, seen_at_test, fake


def test_the_legacy_per_fn_route_heals_before_its_pass_test(tmp_path: Path) -> None:
    healed: list[str] = []

    # The engine's target here is the `src/` dir itself (the module's
    # fixture shape), so the boundary names files relative to it — the
    # same root rule the driver applies to a crate-root target.
    def _heal(rel: str) -> bool:
        healed.append(rel)
        (tmp_path / "src" / rel).write_text(_LAYOUT, encoding="utf-8")
        return True

    lib, seen, fake = _legacy_route(tmp_path, heal=_heal)
    assert healed == ["lib.rs"]
    assert seen and seen[0] is True, "the pass test must run on a tree that parses"
    ev = [d for k, d in fake.event_data if k == "phase_c.tdd.unparseable_tree"]
    assert ev and ev[0]["when"] == "before the pass test"
    assert ev[0]["files"] == ["lib.rs"] and ev[0]["healed"] == ["lib.rs"]
    assert any("[phase-c/per-fn] iter 1: TREE UNPARSEABLE before the pass test"
               " — lib.rs" in ln for ln in fake.logs)


def test_control_the_legacy_route_with_no_heal_reads_the_broken_tree(
        tmp_path: Path) -> None:
    lib, seen, fake = _legacy_route(tmp_path, heal=None)
    assert seen and seen[0] is False
    assert any("NO heal wired" in ln for ln in fake.logs)
    ev = [d for k, d in fake.event_data if k == "phase_c.tdd.unparseable_tree"]
    assert ev and ev[0]["healed"] == []
