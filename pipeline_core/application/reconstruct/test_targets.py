"""Test-oracle immutability for the fill/fix frontier.

A reconstruction target is a broken PRODUCTION fn. A `#[cfg(test)]`/`#[test]` fn is
the oracle — splicing its body rewrites the spec (the hardcrate `extra_cap_limit`
255→512 corruption). These pure helpers strip such targets from any frontier; the
composition root passes the enumerated records + a log sink.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from pipeline_core.domain.models import FnLocator, FnRecord, Frontier
from pipeline_core.domain.oracle_immutability import is_oracle_fn


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


def filter_test_targets(
    frontier: Frontier, recs: Iterable[dict[str, Any]],
    *, log: Callable[[str], None] | None = None,
) -> Frontier:
    """Return `frontier` without any key that resolves to a test fn (per `recs`)."""
    if not frontier:
        return frontier
    drop = test_fn_keys(recs)
    if not drop:
        return frontier
    kept = {k: v for k, v in frontier.items() if k not in drop}
    if log:
        for f, fn in frontier.keys() - kept.keys():
            log(f"  [fix] skipping test-oracle target {f}::{fn} (immutable, FR-018)")
    return kept
