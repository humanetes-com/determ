"""The end gate's reading of which remaining stubs nothing in the finished
tree or its oracle can call, taken per baked cluster world (use case).

`domain.stub_removal` holds the rule and why it exists; this module gathers
its inputs on one tree: the extractor's fn records rooted at the crate root
(the same root rustc names its files against, so the lint spans, the MIR and
the byte ranges join), which of them each world compiles
(`fn_execution.compiled_under`, the rule the augmentation criterion uses),
and per world that compiles a stub: rustc's dead-code analysis (library and
test profile), and, only when a stub there is not already lint-dead, the
unmodified tree's MIR in both profiles, the public index, the fixed point
over the tree's units, and the removal build of each remaining stub's bundle.

It reads the reconstructed tree and the compiler's verdict on it. Nothing here
reads the original crate, and nothing it returns reaches a prompt.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import is_stub_record
from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.cargo_toml import cargo_default_features
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.dead_code import WorldDeadCode, names_record
from pipeline_core.domain.fn_execution import compiled_under, site_label
from pipeline_core.domain.fn_reachability import WorldReading, public_surface
from pipeline_core.domain.models import FnRecord, FnSite, ModRecord
from pipeline_core.domain.stub_removal import (
    CompilerView,
    ItemKey,
    WorldUnreached,
    dead_units,
    is_trait_impl_method,
    removal_bundles,
    units_of,
    unreached_stub_proofs,
)


@dataclass(frozen=True, slots=True)
class UnreachedStubDeps:
    """Collaborators, injected by the composition root."""

    enumerate_fns: Callable[[Path], Sequence[Any] | None]
    enumerate_mods: Callable[[Path], Sequence[ModRecord] | None]
    # (crate root, cluster) -> that world's dead-code reading of the tree.
    measure_dead: Callable[[Path, Any], WorldDeadCode]
    # (crate root, cluster) -> MIR (library and test profile) and rustdoc.
    read_compiler_view: Callable[[Path, Any], WorldReading]
    # (crate root, cluster, stub unit -> bundle, always-deleted items) ->
    # (stub unit keys whose bundle's deletion builds, reason there is none).
    measure_removal: Callable[[Path, Any, Mapping[ItemKey, frozenset[ItemKey]],
                               frozenset[ItemKey]],
                              tuple[frozenset[ItemKey], str]]
    log: Callable[[str], None]


def _default_features(project_dir: Path) -> list[str]:
    try:
        return cargo_default_features(
            (project_dir / "Cargo.toml").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []


def _world(project_dir: Path, cluster: Any, stubs: Sequence[FnRecord],
           records: Sequence[FnRecord], file_texts: dict[str, str],
           deps: UnreachedStubDeps) -> WorldUnreached:
    """One world's reading. `stubs` are the stubs this world compiles."""
    name = str(cluster.name)
    dead = deps.measure_dead(project_dir, cluster)
    if not dead.measured:
        deps.log(f"  [stub-gate/{name}] no dead-code reading ({dead.reason}) — "
                 "no stub this cluster compiles is read as unreached.")
        return WorldUnreached(name, dead)
    unit_of = units_of(records)
    # Only a stub the lint does not already prove needs the compiler view.
    open_stubs = [r for r in stubs
                  if is_trait_impl_method(r) or not names_record(r, dead.spans)]
    if not open_stubs:
        return WorldUnreached(name, dead)
    view = deps.read_compiler_view(project_dir, cluster)
    if not view.measured:
        deps.log(f"  [stub-gate/{name}] no compiler view ({view.reason}) — only "
                 "the lint's dead set is read as unreached in this cluster.")
        return WorldUnreached(name, dead)
    units = dead_units(records, CompilerView(
        dead, (view.mir, view.mir_test), public_surface(view.rustdoc)), file_texts)
    bundles = removal_bundles(open_stubs, records, units)
    base = frozenset(k for k in units.seeds)
    removable, reason = deps.measure_removal(project_dir, cluster, bundles, base)
    if reason:
        deps.log(f"  [stub-gate/{name}] no removal reading ({reason}) — only "
                 "the lint's dead set is read as unreached in this cluster.")
        removable = frozenset()
    kept: dict[FnSite, str] = {}
    for r in open_stubs:
        u = unit_of.get((r.file, r.sig_byte_start, r.fn_name))
        if u is None:
            why = "it has no removable item"
        elif u.key in removable:
            continue
        elif u.key in bundles:
            why = ("without it and the dead code that alone uses it, the crate or a "
                   "test target stops building or another fn's MIR changes"
                   if not reason else "no removal reading")
        else:
            why = units.live_why.get(u.key, "a live use")
        kept[record_site(r)] = why
    for site, why in sorted(kept.items(), key=lambda kv: site_label(kv[0])):
        deps.log(f"  [stub-gate/{name}] kept {site_label(site)}: {why}")
    return WorldUnreached(name, dead, removable=removable, kept=kept)


def read_unreached_stub_proofs(project_dir: Path, clusters: Sequence[Any],
                               deps: UnreachedStubDeps) -> dict[FnSite, str]:
    """Site -> proof for the stub sites of the tree at `project_dir` nothing
    can call in any baked world that compiles them. Empty when there is no
    stub, no cluster, or no reading — the gate then decides exactly as it
    did without this."""
    if not clusters:
        return {}
    rows = deps.enumerate_fns(project_dir)
    if not rows:
        return {}
    records = [r if isinstance(r, FnRecord) else FnRecord.from_dict(dict(r))
               for r in rows]
    stubs = [r for r in records if r.fn_name and is_stub_record(r.to_dict())]
    if not stubs:
        return {}
    mods = deps.enumerate_mods(project_dir / "src")
    file_texts: dict[str, str] = {}
    for rel in {r.file for r in records}:
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

    worlds: list[WorldUnreached] = []
    for cluster in sorted(clusters, key=lambda c: str(c.name)):
        name = str(cluster.name)
        mine = [r for r in stubs if _compiled_in(r, name)]
        if not mine:
            continue
        worlds.append(_world(project_dir, cluster, mine, records, file_texts, deps))
    return unreached_stub_proofs(stubs, records, worlds, _compiled_in)
