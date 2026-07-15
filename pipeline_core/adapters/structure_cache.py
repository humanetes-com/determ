"""Run-scoped caches over the syn extractor's structure queries, plus the
cluster-membership convenience bindings that consume them.

Why an adapter: the mod-records and cluster-specific-symbol caches are mutable
run state over extractor I/O. Holding them in one object (constructed once in
`build_app`) gives the invalidation hook a real owner — call `invalidate()`
after any operation that rewrites lib.rs/main.rs mod declarations (strip,
task1 reset, task2 regeneration); stale mod records misclassify cluster
membership.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct import cluster_membership as _membership
from pipeline_core.domain.cfg import cluster_specific_symbol_names
from pipeline_core.domain.models import FnLocator, ImplBlock, ModRecord


class StructureCache:
    """Per-directory caches + cluster-membership queries bound to them."""

    def __init__(
        self,
        *,
        enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
        enumerate_mods: Callable[[Path], list[ModRecord] | None],
        collect_impl_blocks: Callable[[Path], list[ImplBlock] | None],
        log: Callable[[str], None],
    ) -> None:
        self._enumerate_fns = enumerate_fns
        self._enumerate_mods = enumerate_mods
        self._collect_impl_blocks = collect_impl_blocks
        self._log = log
        self._mod_records: dict[str, list[ModRecord]] = {}
        self._cluster_specific_syms: dict[str, frozenset[str]] = {}

    def invalidate(self) -> None:
        """Clear both caches. Call after any operation that changes the on-disk
        mod declarations in lib.rs/main.rs (strip, task1 reset, task2
        regeneration). Cheap enough that callers should err on the side of
        invalidation."""
        self._mod_records.clear()
        self._cluster_specific_syms.clear()

    def mod_records(self, src_dir: Path) -> list[ModRecord] | None:
        """ModRecords for `src_dir` via the syn extractor, cached per dir.
        None when the extractor is unavailable."""
        key = str(src_dir.resolve())
        if key not in self._mod_records:
            recs = self._enumerate_mods(src_dir)
            if recs is None:
                return None
            self._mod_records[key] = recs
        return self._mod_records[key]

    def cluster_specific_symbols(
        self, project_dir: Path | None, contexts: list[Any],
    ) -> frozenset[str]:
        """Crate fn names compiled OUT under some cluster, cached per project.
        The non-test fn cfg gates are stable across a cluster's augmentation
        (added gen_tests siblings are `#[cfg(test)]`-only → never
        cluster-specific), so caching is safe. Cleared by `invalidate()`."""
        if project_dir is None:
            return frozenset()
        key = str(project_dir)
        if key not in self._cluster_specific_syms:
            recs = self._enumerate_fns(project_dir) or []
            self._cluster_specific_syms[key] = cluster_specific_symbol_names(recs, contexts)
        return self._cluster_specific_syms[key]

    def scan_unimplemented_records(self, src_dir: Path) -> list[dict[str, Any]] | None:
        """Full FnRecord dicts for every fn whose body is still unimplemented.
        None when the extractor is unavailable. Records include `container` and
        `sig_text` for disambiguating same-named fns."""
        recs = self._enumerate_fns(src_dir)
        if recs is None:
            return None
        return [r for r in recs if r.get("has_unimplemented_body")]

    def file_cfg_predicate(self, target_src_dir: Path, file_rel: str) -> str | None:
        return _membership.file_cfg_predicate(
            target_src_dir, file_rel, mod_records=self.mod_records)

    def fn_belongs_to_cluster(
        self,
        target_src_dir: Path,
        file_rel: str,
        fn_name: str,
        cluster: Any,
        fn_cfg_predicates: list[str] | None = None,
    ) -> bool:
        return _membership.fn_belongs_to_cluster(
            target_src_dir, file_rel, fn_name, cluster, fn_cfg_predicates,
            mod_records=self.mod_records)

    def scan_unimplemented_for_cluster(
        self, target_src_dir: Path, cluster: Any,
    ) -> list[FnLocator]:
        return _membership.scan_unimplemented_for_cluster(
            target_src_dir, cluster,
            enumerate_fns=self._enumerate_fns,
            mod_records=self.mod_records, log=self._log)

    def scan_unimplemented_records_for_cluster(
        self, target_src_dir: Path, cluster: Any,
    ) -> list[dict[str, Any]] | None:
        return _membership.scan_unimplemented_records_for_cluster(
            target_src_dir, cluster,
            scan_records=self.scan_unimplemented_records,
            fn_belongs=self.fn_belongs_to_cluster, log=self._log)

    def collect_impl_blocks_for_cluster(
        self, target_src_dir: Path, cluster: Any,
    ) -> list[ImplBlock] | None:
        return _membership.collect_impl_blocks_for_cluster(
            target_src_dir, cluster,
            collect_impl_blocks=self._collect_impl_blocks,
            fn_belongs=self.fn_belongs_to_cluster)
