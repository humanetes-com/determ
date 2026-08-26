"""Layout-authoring PHASE (F11 t1.5) — the composition step that runs
:func:`author_layout` for every cfg-cluster between bake and strip, then
recomputes the leaf-first order over the rewritten scaffold.

Placement (plan_2 M2): bake writes the frozen skeleton + private-surface marks +
behaviour suite; THIS phase lets the model re-author each cluster's private
layout so the reconstruction is not forced to reproduce the original's unsafe
internals; THEN the leaf-first order + fn-ref-counts are recomputed over the
rewritten scaffold (the model may have renamed/replaced private helper sigs, so
counts keyed to the frozen names would be stale). Strip runs after.

Fully DI + degrade-safe: an empty surface, an unavailable model, or a compile
adapter that always says "no" all reduce to a no-op — the run proceeds on the
frozen skeleton exactly as before F11.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    AuthorLayoutResult,
    author_layout,
)
from pipeline_core.domain.authored_layout import AuthoredItem
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSuite,
    OutcomeKind,
)
from pipeline_core.domain.private_surface import PrivateSurface, PrivateSurfaceItem
from pipeline_core.domain.rust_idents import camelcase_type_names


def _spec_pins_types(spec: BehaviourSpec) -> frozenset[str]:
    """The type names one STATIC_ASSERTION spec pins (empty for non-static)."""
    if spec.then is None or spec.then.kind is not OutcomeKind.STATIC_ASSERTION:
        return frozenset()
    return frozenset(camelcase_type_names(spec.then.static_expr))


def make_pin_selector(
    suite: BehaviourSuite, surface: PrivateSurface,
) -> Callable[[tuple[PrivateSurfaceItem, ...]], list[BehaviourSpec]]:
    """A per-component pin selector: the STATIC_ASSERTION specs in `suite` whose
    pinned type names intersect the component's item names. This is what feeds
    the t1.5 prompt AND drives the M13 gate (empty selection on a layout-
    sensitive component = harvest gap).

    A pin often names only the PUBLIC type (``size_of::<Error>()``), never the
    private types behind it — so a direct name match alone misses every such
    pin. When a spec pins a FROZEN-PUBLIC name, it also applies to whichever
    component is reachable from THAT public type's own direct references
    (``surface.reachable_from_public``), not the whole crate's merged
    layout-sensitive set — so a pin on one public type never leaks onto an
    unrelated public type's private component."""
    static_specs = [(s, _spec_pins_types(s)) for s in suite.specs
                    if s.then is not None
                    and s.then.kind is OutcomeKind.STATIC_ASSERTION]

    def select(comp: tuple[PrivateSurfaceItem, ...]) -> list[BehaviourSpec]:
        names = {i.name for i in comp}
        out = []
        for s, pinned in static_specs:
            if pinned & names:
                out.append(s)
                continue
            for pub_name in pinned & surface.frozen_public:
                if names & surface.reachable_from_public(pub_name):
                    out.append(s)
                    break
        return out

    return select


@dataclass(frozen=True)
class LayoutPhaseDeps:
    """Collaborators for the t1.5 phase. All injected — the composition root
    binds them to the resident model transport, skeleton IO, and cargo check."""
    log: Callable[[str], None]
    load_surface: Callable[[Path, str], PrivateSurface]
    load_suite: Callable[[Path, str], BehaviourSuite]
    public_surface_text: Callable[[Path, str], str]
    symbol_inventory_text: Callable[[Path, str], str]
    author_deps_for: Callable[[Path, str], AuthorLayoutDeps]
    recompute_leaf_order: Callable[[Path, str], None]
    # F11 M3 precondition: persist which component ended up authored/gated/
    # frozen, so a later re-trigger can find "still on the original scaffold"
    # without re-deriving reachability. Optional — an omitted store is a no-op
    # (additive; the phase runs exactly as before F11 M3 existed).
    save_layout_outcomes: Callable[[Path, AuthorLayoutResult], Path] | None = None
    # Crate-wide layout registry (uid → the crate's ONE definition of that
    # private item). Read once at phase start so a re-run adopts what a previous
    # pass fixed, persisted after each cluster so the mid-run re-trigger writes
    # into the same store. Optional — omitted, adoption still works WITHIN this
    # phase via the in-memory registry, it just does not outlive the process.
    load_registry: Callable[[Path], dict[str, AuthoredItem]] | None = None
    save_registry: Callable[[Path, dict[str, AuthoredItem]], Path] | None = None
    # The freeze scope CONFIGURED for this run, compared against the one the
    # surface was BAKED under. They can only differ across a config change, and
    # only a re-bake can close the gap: the replaceable set and the elided API
    # artefact are both frozen at bake time, on live source that is gone by now.
    author_private_fields: bool = False


def run_layout_phase(
    project_dir: Path, module_name: str, cluster_names: list[str],
    deps: LayoutPhaseDeps,
) -> dict[str, AuthorLayoutResult]:
    """Author the private layout for every cfg-cluster. Returns per-cluster
    results (empty dict when nothing was authorable). Never raises on a single
    cluster's failure — the frozen skeleton is always a valid fallback."""
    results: dict[str, AuthorLayoutResult] = {}
    # Crate-wide layout registry, uid → the definition the first cluster to
    # reach this item committed and no later cluster has DISPROVED. A crate has
    # one definition per private type, so later clusters adopt it instead of
    # inventing a second, incompatible one — otherwise the bodies seeded into
    # them from an earlier cluster read fields that no longer exist and the
    # cluster never compiles. Precedence is by evidence, not by arrival: see the
    # drop below.
    registry: dict[str, AuthoredItem] = (
        deps.load_registry(project_dir) if deps.load_registry else {})
    for cluster in cluster_names:
        try:
            surface = deps.load_surface(project_dir, cluster)
        except Exception as e:
            deps.log(f"  [t1.5] {cluster}: could not load private surface ({e}) — skipping.")
            continue
        if surface.author_private_fields != deps.author_private_fields:
            deps.log(
                f"  [t1.5] {cluster}: WARNING — this workspace was baked with "
                f"author_private_fields={surface.author_private_fields}, the "
                f"run is configured for {deps.author_private_fields}. The "
                "baked surface wins (the setting also elided the pre-strip API "
                "artefact, which cannot be re-derived post-strip). Re-bake "
                "with `bash restart.sh --y` to apply the new scope.")
        if surface.is_empty:
            deps.log(f"  [t1.5] {cluster}: no private-replaceable layout — frozen skeleton kept.")
            continue
        suite = deps.load_suite(project_dir, cluster)
        select_pins = make_pin_selector(suite, surface)
        res = author_layout(
            surface,
            module_name=module_name,
            public_surface=deps.public_surface_text(project_dir, cluster),
            select_pins=select_pins,
            symbol_inventory=deps.symbol_inventory_text(project_dir, cluster),
            deps=deps.author_deps_for(project_dir, cluster),
            registry=registry,
            behaviour_specs=suite.specs,
        )
        results[cluster] = res
        # A definition this cluster DISPROVED is dropped before anything is
        # stored, and only then does first-cluster-wins apply to what is left:
        # the slot is empty, so this cluster's replacement lands in it, and if
        # no replacement was authored the uid simply has no crate-wide
        # definition — the next cluster authors one instead of adopting a
        # measured wrong answer, and `reconcile_layout` (whose rule is that the
        # registry outranks a per-cluster record) has nothing to reinstall.
        #
        # What plain `setdefault` cost, on `anyhow` 2026-08-06: the registry
        # restored from the `task0c-augmented-anyhow` tag held `pub struct Error
        # { inner: core::ptr::NonNull<ErrorImpl> }`, authored before the
        # autotrait pins routed to a component and so never gated on `Error:
        # Send`. Both clusters adopted it, both were refused by the pin probe
        # with `error[E0277]: NonNull<ErrorImpl> cannot be sent between threads
        # safely`, and both re-authored a correct one (`stable`: `inner:
        # Own<ErrorImpl>`; `nightly`: `NonNull` plus `unsafe impl Send`/`Sync`),
        # each verified against all 6 pins. `setdefault` kept the refuted text
        # and `reconcile_layout` reinstalled it over both trees. Nothing
        # compiled: `pass=0 fail=112`, the compile-blocked frontier narrowed to
        # the 1 fn the diagnostics implicated, and the run filled 0 of 118 stubs
        # in 70 minutes. The tag is re-cut AFTER this phase, so the disproved
        # text was also what every later `--restart-from-augmented` restored.
        dropped: list[str] = []
        for comp_res in res.components:
            for uid in comp_res.refuted_uids:
                if registry.pop(uid, None) is not None:
                    dropped.append(uid)
        for comp_res in res.components:
            for uid, item in comp_res.authored_items:
                registry.setdefault(uid, item)   # first cluster to reach it wins
        if dropped:
            orphaned = [uid for uid in dropped if uid not in registry]
            deps.log(
                f"  [t1.5] {cluster}: dropped {len(dropped)} crate-wide layout "
                f"definition(s) the compile+pin gate disproved here — "
                f"{len(dropped) - len(orphaned)} replaced by what this cluster "
                f"authored, {len(orphaned)} left with no crate-wide definition "
                f"(the next cluster authors its own): {', '.join(sorted(dropped))}")
        if deps.save_registry is not None:
            try:
                deps.save_registry(project_dir, registry)
            except Exception as e:
                deps.log(f"  [t1.5] {cluster}: layout-registry persist failed "
                         f"(non-fatal, adoption stays in-memory): {e}")
        if deps.save_layout_outcomes is not None:
            try:
                deps.save_layout_outcomes(project_dir, res)
            except Exception as e:
                deps.log(f"  [t1.5] {cluster}: layout-outcomes persist failed (non-fatal): {e}")
        authored = sum(1 for c in res.components if c.authored)
        adopted = sum(1 for c in res.components if c.adopted)
        deps.log(
            f"  [t1.5] {cluster}: authored {authored}/{len(res.components)} "
            f"component(s) ({adopted} adopted from an earlier cluster); "
            f"{len(res.gated)} gated (harvest gap); "
            f"{len(res.components) - authored - len(res.gated)} left frozen.")
        # The model may have renamed private helpers → recompute leaf-first
        # order + fn-ref-counts over the REWRITTEN scaffold (plan_2 M2).
        if authored:
            try:
                deps.recompute_leaf_order(project_dir, cluster)
            except Exception as e:
                deps.log(f"  [t1.5] {cluster}: leaf-order recompute failed (non-fatal): {e}")
    return results


def make_layout_phase_runner(
    *,
    enabled: bool,
    load_baked_clusters: Callable[[Path], list[Any]],
    load_surface: Callable[[Path, str], PrivateSurface],
    load_suite: Callable[[Path, str], BehaviourSuite],
    api_surface_path: Callable[[Path, str], Path],
    run_model: Callable[[str], str],
    # (project_dir, cluster name, the baked cluster itself). The cluster object
    # travels whole: its toolchain is not expressible as an env mapping.
    compile_check: Callable[[Path, str, Any], tuple[bool, str]],
    skeleton_src_dir_for: Callable[[Path, str], Path],
    fn_name_counts: Callable[[Path], dict[str, int]],
    log: Callable[[str], None],
    # Installs/removes the layout-pin probe in the skeleton crate root so the
    # compile gate decides the pins. Absent ⇒ pins stay advisory (prior gate).
    write_pin_probe: Callable[[Path, str, str | None], None] | None = None,
    # The crate's write-protected in-crate tests (rel path → text). Their member
    # paths are a constraint on the authored layout — see AuthorLayoutDeps.
    read_oracle_sources: Callable[[Path], dict[str, str]] | None = None,
    # Wider write-protected set (siblings + the crate's `tests/`), for the
    # derive requirement only — see AuthorLayoutDeps.frozen_test_sources.
    read_frozen_tests: Callable[[Path], dict[str, str]] | None = None,
    save_layout_outcomes: Callable[[Path, AuthorLayoutResult], Path] | None = None,
    # Crate-wide layout registry — see LayoutPhaseDeps.
    load_registry: Callable[[Path], dict[str, AuthoredItem]] | None = None,
    save_registry: Callable[[Path, dict[str, AuthoredItem]], Path] | None = None,
    author_private_fields: bool = False,
) -> Callable[[Path, str], None]:
    """Assemble the t1.5 phase runner from resident ports (composition helper —
    keeps pipeline.py thin per the shrink goal). Returns a no-op when disabled or
    when no clusters were baked. All IO/model/cargo access is injected."""

    def _runner(project_dir: Path, module_name: str) -> None:
        if not enabled:
            return
        clusters = load_baked_clusters(project_dir) or []
        if not clusters:
            return
        cluster_by_name = {c.name: c for c in clusters}

        def _public_surface_text(pd: Path, cluster: str) -> str:
            try:
                p = api_surface_path(pd, module_name)
                return p.read_text(encoding="utf-8") if p.exists() else ""
            except Exception:
                return ""

        def _author_deps_for(pd: Path, cluster: str) -> AuthorLayoutDeps:
            src = skeleton_src_dir_for(pd, cluster)

            def _read(rel: str) -> str | None:
                try:
                    return (src / rel).read_text(encoding="utf-8")
                except OSError:
                    return None

            def _write(rel: str, txt: str) -> None:
                fp = src / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(txt, encoding="utf-8")

            return AuthorLayoutDeps(
                run_model=run_model, read_skeleton=_read, write_skeleton=_write,
                compile_check=lambda: compile_check(
                    pd, cluster, cluster_by_name.get(cluster)),
                log=log,
                write_pin_probe=(
                    None if write_pin_probe is None
                    else (lambda src: write_pin_probe(pd, cluster, src))),
                oracle_sources=(
                    None if read_oracle_sources is None
                    else (lambda: read_oracle_sources(pd))),
                frozen_test_sources=(
                    None if read_frozen_tests is None
                    else (lambda: read_frozen_tests(pd))))

        def _recompute_leaf_order(pd: Path, cluster: str) -> None:
            src = skeleton_src_dir_for(pd, cluster)
            (src.parent / "_leaf_order.json").write_text(
                json.dumps(fn_name_counts(src), sort_keys=True), encoding="utf-8")

        deps = LayoutPhaseDeps(
            log=log, load_surface=load_surface, load_suite=load_suite,
            public_surface_text=_public_surface_text,
            symbol_inventory_text=lambda pd, c: "",
            author_deps_for=_author_deps_for,
            recompute_leaf_order=_recompute_leaf_order,
            save_layout_outcomes=save_layout_outcomes,
            load_registry=load_registry, save_registry=save_registry,
            author_private_fields=author_private_fields)
        run_layout_phase(project_dir, module_name,
                         [c.name for c in clusters], deps)

    return _runner
