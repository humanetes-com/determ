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
from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.cfg_dead_sites import CfgDeadSites
from pipeline_core.domain.models import ImplBlock, ModRecord
from pipeline_core.domain.stub_inventory import StubInventory


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
        self._last_membership_line: str = ""

    def invalidate(self) -> None:
        """Clear both caches. Call after any operation that changes the on-disk
        mod declarations in lib.rs/main.rs (strip, task1 reset, task2
        regeneration). Cheap enough that callers should err on the side of
        invalidation."""
        self._mod_records.clear()

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

    def scan_unimplemented_records(self, src_dir: Path) -> list[dict[str, Any]] | None:
        """Full FnRecord dicts for every fn whose body is still unimplemented.
        None when the extractor is unavailable. Records include `container` and
        `sig_text` for disambiguating same-named fns."""
        recs = self._enumerate_fns(src_dir)
        if recs is None:
            return None
        # Same widening as the name-only scan: the extractor's flag only sees a
        # literal stub, so a body that merely re-declares its own fn (empty
        # outer fn, no `unimplemented!()`) would read as filled forever.
        return [r for r in recs
                if r.get("has_unimplemented_body")
                or is_unimplemented_body(str(r.get("body_text", "")),
                                         str(r.get("fn_name", "")))]

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

    def cluster_dead_sites(self, target_src_dir: Path, cluster: Any) -> CfgDeadSites:
        return _membership.cluster_dead_sites(
            target_src_dir, cluster, enumerate_fns=self._enumerate_fns,
            mod_records=self.mod_records)

    def dead_sites_among(self, recs: list[dict[str, Any]], target_src_dir: Path,
                         cluster: Any) -> CfgDeadSites:
        return _membership.dead_sites_among(
            recs, target_src_dir, cluster, mod_records=self.mod_records)

    def _membership_log(self, msg: str) -> None:
        """The membership report re-prints on every scan of an unchanged tree
        — 29 identical `skipped N of M stub(s) not active` lines in one
        hardcrate run (2026-08-12). One line per distinct verdict is signal;
        a repeat of the previous line is not."""
        if msg == self._last_membership_line:
            return
        self._last_membership_line = msg
        self._log(msg)

    def scan_unimplemented_for_cluster(
        self, target_src_dir: Path, cluster: Any,
    ) -> StubInventory:
        return _membership.scan_unimplemented_for_cluster(
            target_src_dir, cluster,
            enumerate_fns=self._enumerate_fns,
            mod_records=self.mod_records, log=self._membership_log)

    def scan_unimplemented_records_for_cluster(
        self, target_src_dir: Path, cluster: Any,
    ) -> list[dict[str, Any]] | None:
        return _membership.scan_unimplemented_records_for_cluster(
            target_src_dir, cluster,
            scan_records=self.scan_unimplemented_records,
            fn_belongs=self.fn_belongs_to_cluster, log=self._membership_log)

    def collect_impl_blocks_for_cluster(
        self, target_src_dir: Path, cluster: Any,
    ) -> list[ImplBlock] | None:
        return _membership.collect_impl_blocks_for_cluster(
            target_src_dir, cluster,
            collect_impl_blocks=self._collect_impl_blocks,
            fn_belongs=self.fn_belongs_to_cluster)
