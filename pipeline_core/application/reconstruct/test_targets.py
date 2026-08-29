"""Test-oracle immutability for the fill/fix frontier.

A reconstruction target is a broken PRODUCTION fn. A `#[cfg(test)]`/`#[test]` fn is
the oracle — splicing its body rewrites the spec (the hardcrate `extra_cap_limit`
255→512 corruption). These pure helpers strip such targets from any frontier; the
composition root passes the enumerated records + a log sink.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.oracle_immutability import is_oracle_fn


def _as_locator(key: FnLocator | FnSite | tuple[str, ...]) -> FnLocator:
    if isinstance(key, FnSite) or (isinstance(key, tuple) and len(key) == 3):
        return FnLocator(key[0], key[1])
    if isinstance(key, FnLocator) or (isinstance(key, tuple) and len(key) == 2):
        return FnLocator(key[0], key[1])
    return FnLocator(str(key), "")


def is_test_fn_record(rec: dict[str, Any]) -> bool:
    """True when an extractor fn record is oracle content. Delegates to the one
    domain predicate so this dict-shaped seam and the typed revert scope cannot
    drift: a `test` cfg alone misses every fn in a `gen_tests` sibling (they
    carry the cfg on the parent's `mod` decl, not on themselves), which is where
    the relocated oracle actually lives."""
    return is_oracle_fn(FnRecord.from_dict(rec))


def test_fn_keys(recs: Iterable[dict[str, Any]]) -> set[FnLocator]:
    """The `(file, fn_name)` keys of every test fn among `recs`."""
    return {FnLocator(r.get("file", ""), r.get("fn_name", ""))
            for r in recs if is_test_fn_record(r)}


def filter_test_targets[K: (FnLocator, FnSite)](
    frontier: Mapping[K, set[str]], recs: Iterable[dict[str, Any]],
    *, log: Callable[[str], None] | None = None,
) -> dict[K, set[str]]:
    """Return `frontier` without any key that resolves to a test fn (per `recs`).

    Keys may be :class:`FnLocator` (dispatch) or :class:`FnSite` (conviction);
    a site drops when its ``(file, fn_name)`` is an oracle fn. Conviction
    after site-at-write is a SiteFrontier; dropping oracle keys must not
    require collapsing back to locators.
    """
    if not frontier:
        return {}
    drop = test_fn_keys(recs)
    if not drop:
        return dict(frontier)
    kept = {k: v for k, v in frontier.items() if _as_locator(k) not in drop}
    if log:
        for k in frontier:
            if k not in kept:
                loc = _as_locator(k)
                log(f"  [fix] skipping test-oracle target {loc.file}::{loc.fn_name} "
                    f"(immutable: splicing it would rewrite the spec)")
    return kept
