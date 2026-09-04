"""The stubs still on a tree, carried at BOTH cardinalities by name.

A stub is a fn body the loop has not filled, and the extractor reports one
RECORD per implementation site: `fmt` in each Debug/Display impl, `from` in
each `From<…>` impl, `provide` under each cfg world. The dispatch key is the
`(file, fn_name)` locator, which several of those sites share. Every consumer
therefore has to choose which of the two it counts, iterates or hands on, and
until 2026-09-04 that choice was made by accident, per call site, on a bare
`list[FnLocator]` that repeated a locator once per site:

- the frontier happened to take `frozenset(...)` and was right;
- every `N of M stub(s)` / `remaining=N` count printed the SITE number under
  a sentence that read as units (`skipped 15 of 95 stub(s)`, run
  `2973-66e50ad9`, with `src/context.rs::provide` listed twice);
- the ledger re-seed iterated the raw list and handed the splicer the same
  unit once per site — two identical body spans applied to one text corrupt
  the file — so seven bodies that build were rejected as "did not build",
  `src/context.rs` was left unparseable, and the cluster was aborted.

This type makes the choice explicit: `sites` is the extractor's truth,
`units` is the dispatch view, and there is deliberately no `__len__` and no
`__iter__` — a caller that wants a number or a sequence names `site_count`,
`unit_count`, `unit_list()` or `sites`, and a log line can print both.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.models import FnLocator, FnSite

__all__ = ["StubInventory", "StubSite"]


@dataclass(frozen=True, slots=True)
class StubSite:
    """One implementation site whose body is still a stub.

    `cfg_predicates` is the extractor's per-fn attribute list: `()` means the
    extractor positively determined the fn is ungated, `None` means no
    authoritative list exists (the text-walk fallback produced this site) and
    a caller may then fall back to reading predicates out of the source. The
    distinction is why this is an optional rather than `()` for both."""

    site: FnSite
    cfg_predicates: tuple[str, ...] | None = None

    @property
    def locator(self) -> FnLocator:
        return FnLocator(self.site.file, self.site.fn_name)

    @property
    def label(self) -> str:
        """`file::fn` plus the container when there is one, so two `fmt`
        impls in one file never read as the same site named twice."""
        base = f"{self.site.file}::{self.site.fn_name}"
        return f"{base} [{self.site.container}]" if self.site.container else base


@dataclass(frozen=True, slots=True)
class StubInventory:
    """Every stub site on a tree, in extractor order, plus the unit view."""

    sites: tuple[StubSite, ...] = ()

    # --- constructors ---------------------------------------------------

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]], *,
                     keep: Callable[[Mapping[str, Any]], bool] | None = None,
                     ) -> StubInventory:
        """One site per extractor record that `keep` accepts (all, by default).
        The record's `cfg_predicates` are authoritative here, so an absent key
        means "ungated", never "unknown"."""
        out: list[StubSite] = []
        for r in records:
            if keep is not None and not keep(r):
                continue
            out.append(StubSite(
                FnSite(str(r.get("file", "")), str(r.get("fn_name", "")),
                       str(r.get("container", "") or "")),
                tuple(str(c) for c in (r.get("cfg_predicates") or ()))))
        return cls(tuple(out))

    @classmethod
    def of_units(cls, units: Iterable[FnLocator | tuple[str, str]], *,
                 cfg_predicates: tuple[str, ...] | None = None,
                 ) -> StubInventory:
        """One site per DISTINCT locator with no container — the shape the
        text-walk fallback and test fixtures produce. Repeats collapse: a
        caller that holds a multiset has already lost the sites, and this
        constructor must not invent them."""
        seen: dict[FnLocator, None] = {}
        for u in units:
            seen.setdefault(FnLocator(u[0], u[1]), None)
        return cls(tuple(StubSite(FnSite(k.file, k.fn_name, ""), cfg_predicates)
                         for k in seen))

    # --- the two cardinalities, by name ----------------------------------

    @property
    def site_count(self) -> int:
        return len(self.sites)

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def units(self) -> frozenset[FnLocator]:
        return frozenset(s.locator for s in self.sites)

    def unit_list(self) -> list[FnLocator]:
        """Distinct locators in first-seen (source) order — for ranking and
        for any consumer that needs a stable sequence."""
        return list(dict.fromkeys(s.locator for s in self.sites))

    @property
    def by_unit(self) -> dict[FnLocator, tuple[StubSite, ...]]:
        out: dict[FnLocator, list[StubSite]] = {}
        for s in self.sites:
            out.setdefault(s.locator, []).append(s)
        return {k: tuple(v) for k, v in out.items()}

    def unit_names(self) -> frozenset[str]:
        return frozenset(s.site.fn_name for s in self.sites)

    def site_pairs(self) -> tuple[tuple[FnLocator, tuple[str, ...] | None], ...]:
        """One `(locator, cfg_predicates)` pair PER SITE — the shape the
        completion-time cfg verdict reads, where every hollow site is its own
        finding. The name says which cardinality the caller just chose."""
        return tuple((s.locator, s.cfg_predicates) for s in self.sites)

    # --- derived views ---------------------------------------------------

    def filter(self, keep: Callable[[StubSite], bool]) -> StubInventory:
        return StubInventory(tuple(s for s in self.sites if keep(s)))

    def render_count(self) -> str:
        n_s, n_u = self.site_count, self.unit_count
        if n_s == n_u:
            return f"{n_u} stub(s)"
        return f"{n_s} stub site(s) in {n_u} unit(s)"

    def __contains__(self, item: object) -> bool:
        if isinstance(item, StubSite):
            return item in self.sites
        if isinstance(item, FnSite):
            return any(s.site == item for s in self.sites)
        if isinstance(item, tuple) and len(item) == 2:
            return FnLocator(str(item[0]), str(item[1])) in self.units
        return False

    def __bool__(self) -> bool:
        return bool(self.sites)
