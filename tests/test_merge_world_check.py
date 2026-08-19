"""The merge's build gate runs every cfg world, not just the default one.

A body that reads an item the other world's cfg removes compiles clean through
the merge check, the post-merge sweeps and the fix loop's probes — all
default-world builds — and the first build that can see the error is task3's
per-cluster run. Measured 2026-08-19 (run `3579062-476abc3b`):
`ErrorImpl::backtrace` read `vtable.object_backtrace`, a field declared
`#[cfg(all(not(error_generic_member_access), feature = "std"))]`, so the
nightly world failed `E0609` only at task3, after a 6-hour post-merge wave,
and task3-retry burned 3 identical rounds on the structural break.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.generators.task2_generator import (
    StaticSkeletonTask2Generator as _Gen,
)
from pipeline_core.domain.models import BuildCluster


class _Tel:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.events: list[tuple[str, dict]] = []

    def log(self, m: str) -> None:
        self.lines.append(m)

    def emit(self, name: str, data: dict | None = None, **_k: Any) -> None:
        self.events.append((name, data or {}))

    def check_stop(self, *_a: Any) -> None:
        pass


class _Cargo:
    """`run` scripted by cfg world: a command carrying `+nightly`, or an env
    carrying extra RUSTFLAGS, is the divergent world."""

    def __init__(self, world_rc: int, default_rc: int = 0) -> None:
        self.world_rc = world_rc
        self.default_rc = default_rc
        self.calls: list[tuple[str, dict]] = []

    def env(self) -> dict[str, str]:
        return {}

    def run(self, cmd: str, _cwd: Path, env: dict[str, str],
            timeout: int = 0) -> tuple[int, str]:
        self.calls.append((cmd, dict(env)))
        divergent = "+nightly" in cmd or bool(env.get("RUSTFLAGS"))
        rc = self.world_rc if divergent else self.default_rc
        return rc, ("error[E0609]: no field `object_backtrace` on "
                    "`&'static ErrorVTable`" if rc else "")


class _Fill:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def merge_cluster_outputs(self, *, project_dir: Path,
                              cluster_specs: dict, successes: list,
                              prefer_cluster: str | None) -> dict:
        self.calls.append(prefer_cluster)
        return {"preferred": ["src/error.rs::backtrace"] if prefer_cluster
                else [], "prefer_cluster": prefer_cluster or ""}


class _Cfg:
    post_merge_test_timeout = 180


def _clusters() -> dict[str, BuildCluster]:
    stable = BuildCluster(
        name="stable", env_extra={}, tarpaulin_exclude_files=[],
        excluded_cfg=["error_generic_member_access"], active_cfg=[],
        prompt_nightly_rule="")
    nightly = BuildCluster(
        name="nightly",
        env_extra={"RUSTFLAGS": "--cfg error_generic_member_access"},
        tarpaulin_exclude_files=[], excluded_cfg=[],
        active_cfg=["error_generic_member_access"], prompt_nightly_rule="")
    nightly.requires_nightly = True
    return {"stable": stable, "nightly": nightly}


def _merge(world_rc: int, prefer: str | None,
           tmp_path: Path) -> tuple[_Tel, _Cargo, _Fill]:
    tel, cargo, fill = _Tel(), _Cargo(world_rc), _Fill()
    gen = _Gen.__new__(_Gen)
    object.__setattr__(gen, "tel", tel)
    object.__setattr__(gen, "cargo", cargo)
    object.__setattr__(gen, "fill", fill)
    object.__setattr__(gen, "cfg", _Cfg())
    gen._merge_and_verify(project_dir=tmp_path, module="m",
                          cluster_specs=_clusters(), successes=[],
                          prefer_cluster=prefer)
    return tel, cargo, fill


def test_a_world_only_red_merge_is_named_at_merge_time(tmp_path) -> None:
    """The default world is green and the nightly world is red — the recorded
    shape. Pre-fix nothing between the merge and task3 ran the other world's
    check, so the break shipped silently into a 6-hour post-merge wave."""
    tel, cargo, _ = _merge(world_rc=101, prefer=None, tmp_path=tmp_path)
    reds = [d for n, d in tel.events if n == "task2b.merge.world_red"]
    assert reds and reds[0]["world"] == "nightly"
    assert "E0609" in reds[0]["error"]
    assert any("MERGED TREE RED under the 'nightly' cfg world" in ln
               for ln in tel.lines)
    # The check actually ran the divergent world: a +nightly command or
    # cluster RUSTFLAGS reached cargo.
    assert any("+nightly" in cmd or env.get("RUSTFLAGS")
               for cmd, env in cargo.calls)


def test_a_red_world_first_retries_with_the_preference_off(tmp_path) -> None:
    """Same reversion rule as the default-world check: a preference is a
    heuristic, so a world it breaks is first answered by redoing the merge
    with provenance gating; only what is red WITHOUT the preference is a
    fill-side defect worth naming."""
    tel, _, fill = _merge(world_rc=101, prefer="nightly", tmp_path=tmp_path)
    assert fill.calls == ["nightly", None], (
        "a red world must redo the merge with the preference off before "
        "convicting the fills")
    assert any(n == "task2b.merge.preference_reverted" and d.get("world")
               for n, d in tel.events)
    assert any(n == "task2b.merge.world_red" for n, d in tel.events)


def test_green_worlds_stay_silent(tmp_path) -> None:
    tel, _, fill = _merge(world_rc=0, prefer="nightly", tmp_path=tmp_path)
    assert fill.calls == ["nightly"]
    assert not [n for n, _ in tel.events if n == "task2b.merge.world_red"]
    assert not any("MERGED TREE RED" in ln for ln in tel.lines)
