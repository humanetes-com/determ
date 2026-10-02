"""The end gate's reading of which remaining stubs the finished tree cannot
reach, taken per baked cluster world (use case).

`domain.dead_code` holds the rule and why it exists; this module gathers its
inputs on one tree: the extractor's fn records rooted at the crate root (the
same root rustc names its files against, so the byte ranges join), which of
them each world compiles (`fn_execution.compiled_under`, the rule the
augmentation criterion uses), and one dead-code analysis per world that
compiles at least one stub.

It reads the reconstructed tree and the compiler's verdict on it. Nothing here
reads the original crate, and nothing it returns reaches a prompt.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import is_stub_record
from pipeline_core.domain.cargo_toml import cargo_default_features
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.dead_code import WorldDeadCode, dead_stub_sites
from pipeline_core.domain.fn_execution import compiled_under
from pipeline_core.domain.models import FnRecord, FnSite, ModRecord


@dataclass(frozen=True, slots=True)
class DeadStubDeps:
    """Collaborators, injected by the composition root."""

    enumerate_fns: Callable[[Path], Sequence[Any] | None]
    enumerate_mods: Callable[[Path], Sequence[ModRecord] | None]
    # (crate root, cluster) -> that world's dead-code reading of the tree.
    measure_world: Callable[[Path, Any], WorldDeadCode]
    log: Callable[[str], None]


def _default_features(project_dir: Path) -> list[str]:
    try:
        return cargo_default_features(
            (project_dir / "Cargo.toml").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []


def read_dead_stub_sites(project_dir: Path, clusters: Sequence[Any],
                         deps: DeadStubDeps) -> frozenset[FnSite]:
    """The stub sites of the tree at `project_dir` that are dead in every
    baked world that compiles them. Empty when there is no stub, no cluster,
    or no reading — the gate then decides exactly as it did without this."""
    if not clusters:
        return frozenset()
    rows = deps.enumerate_fns(project_dir)
    if not rows:
        return frozenset()
    records = [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r))
               for r in rows]
    stubs = [r for r in records if r.fn_name and is_stub_record(r.to_dict())]
    if not stubs:
        return frozenset()
    mods = deps.enumerate_mods(project_dir / "src")
    file_texts: dict[str, str] = {}
    for rel in {r.file for r in stubs}:
        try:
            file_texts[rel] = (project_dir / rel).read_text(encoding="utf-8")
        except OSError:
            continue
    defaults = _default_features(project_dir)
    cfg_sets = {
        str(c.name): CfgSet.of(list(getattr(c, "cfg_true", None) or []),
                               list(getattr(c, "cfg_false", None) or []),
                               defaults)
        for c in clusters}

    def _compiled_in(rec: FnRecord, cluster_name: str) -> bool:
        return compiled_under(rec, cfg_sets[cluster_name], mods, file_texts)

    worlds: list[WorldDeadCode] = []
    for cluster in sorted(clusters, key=lambda c: str(c.name)):
        name = str(cluster.name)
        if not any(_compiled_in(r, name) for r in stubs):
            continue
        world = deps.measure_world(project_dir, cluster)
        worlds.append(world)
        if not world.measured:
            deps.log(f"  [stub-gate/{name}] no dead-code reading "
                     f"({world.reason}) — no stub this cluster compiles is "
                     "read as unreachable.")
    return dead_stub_sites(stubs, worlds, _compiled_in)
