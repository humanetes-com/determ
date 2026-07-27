"""What a reconstruction may never rewrite — the test oracle, as a predicate over
parsed fn records.

A `#[test]` fn IS the specification. Any route that stubs, reverts or re-fills one
rewrites the spec: the reverted test can no longer fail, so a run reads green
having proven nothing, and a fix loop handed such a target repairs the assertion
instead of the code.

The FILE is the load-bearing signal, not `cfg_predicates`. Inline `#[cfg(test)]
mod`s are relocated pre-strip into `<stem>.gen_tests_N.rs` siblings declared
`#[cfg(test)] #[path…] mod gen_tests_N;` at the parent, so the fns inside a
sibling carry no test cfg of their own — enumerating a live tree returned
`cfg_predicates == []` for all 57 such records. A cfg-only guard is a no-op
there. It also misses the un-attributed fixture items a test needs just as much
as its `#[test]` fns:

    // error.gen_tests_1.rs — is_test=False, cfg_predicates=[], and still oracle
    impl fmt::Display for DisplayOnlyMsg { fn fmt(&self, ...) -> ... { ... } }

so membership of a `gen_tests` sibling is itself disqualifying, whatever the fn
looks like.
"""
from __future__ import annotations

from collections.abc import Iterable

from pipeline_core.domain.generation import is_gen_tests_sibling_name
from pipeline_core.domain.models import FnRecord


def is_oracle_fn(rec: FnRecord) -> bool:
    """True when `rec` is oracle content — a test fn, or any fn living in a
    `gen_tests` sibling. Such a record is never a fill, re-fill or revert
    target."""
    return (rec.is_test
            or "test" in rec.cfg_predicates
            or is_gen_tests_sibling_name(rec.file))


def production_fns(recs: Iterable[FnRecord]) -> list[FnRecord]:
    """`recs` minus every oracle fn — the only records a rewrite may target."""
    return [r for r in recs if not is_oracle_fn(r)]
