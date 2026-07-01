"""How a compile frontier is PARTITIONED into repair units (P3.C).

The per-fn and per-impl strategies each turned a `(file, fn)→errors` frontier
into a set of fix units, with different grouping rules that were buried inline in
two ~40-line loops:

* per-fn  — each unique (file, fn) is its own :class:`FnTarget`; a name shared by
  several records (same-name sibling impls) is *deferred* (can't be located
  unambiguously by the per-fn splicer) to the whole-file fallback.
* per-impl — every frontier fn that belongs to an impl block is grouped into one
  :class:`ImplTarget` per block; the rest are free :class:`FnTarget`s.

:class:`FixPlan` names the result and the two pure ``plan_*`` functions name the
rules — so the partition is testable without cargo, the model, or the tree. The
strategies then just RUN the plan.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pipeline_core.domain.fix_target import FnTarget, ImplTarget
from pipeline_core.domain.models import FnLocator, Frontier, ImplBlock


@dataclass(frozen=True, slots=True)
class FixPlan:
    """The repair units a frontier decomposes into, plus the deferred
    (ambiguous) keys that must fall back to the whole-file path."""

    impl_groups: tuple[ImplTarget, ...] = ()
    free_fns: tuple[FnTarget, ...] = ()
    deferred: tuple[tuple[str, str], ...] = ()

    @property
    def has_units(self) -> bool:
        return bool(self.impl_groups or self.free_fns)


def plan_per_fn(
    frontier: Frontier, *,
    name_counts: Mapping[tuple[str, str], int],
    container_index: Mapping[FnLocator, str],
    writable: Callable[[str], bool],
) -> FixPlan:
    """One :class:`FnTarget` per UNIQUE (file, fn); a name with >1 record is
    deferred (same-name sibling impls can't be located unambiguously)."""
    free: list[FnTarget] = []
    deferred: list[tuple[str, str]] = []
    for key in sorted(frontier):
        rel, fn = key
        if not writable(rel):
            continue
        if name_counts.get(key, 0) != 1:
            deferred.append(key)
            continue
        free.append(FnTarget(rel, fn, container_index.get(key, "")))
    return FixPlan(free_fns=tuple(free), deferred=tuple(deferred))


def plan_per_impl(
    frontier: Frontier, *,
    fn_to_block: Mapping[tuple[str, str], ImplBlock],
    container_index: Mapping[FnLocator, str],
    writable: Callable[[str], bool],
) -> FixPlan:
    """Group every impl-member frontier fn into one :class:`ImplTarget` per
    block; the rest are free :class:`FnTarget`s."""
    impl_groups: dict[tuple[str, str], ImplTarget] = {}
    free_keys: set[FnLocator] = set()
    for key in frontier:
        rel, fn = key
        if not writable(rel):
            continue
        block = fn_to_block.get(key)
        if block is None or not block.container:
            free_keys.add(key)
        else:
            impl_groups[(block.file, block.container)] = ImplTarget(
                block.file, block.container, block)
    free = [FnTarget(rel, fn, container_index.get(FnLocator(rel, fn), ""))
            for (rel, fn) in sorted(free_keys)]
    return FixPlan(
        impl_groups=tuple(impl_groups[k] for k in sorted(impl_groups)),
        free_fns=tuple(free),
    )
