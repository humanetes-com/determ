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

The same argument reaches one directory further, and missing it cost a cluster.
Cargo builds every file under the crate's `tests/` root as an integration test;
nothing there ships in the lib, and none of it needs `#[cfg(test)]` because the
whole directory is already test-only. So a FIXTURE there — `impl Display for
TestError`, `DetectDrop::new`, `impl Debug for Enum<T>` — enumerates as
`is_test=False, cfg_predicates=[]`, exactly the shape a production fn has.
Measured on the live `nightly` tree (2026-08-01): 105 of 291 fn records live
under `tests/`, 71 carry a test attribute and **34 do not** — and those 34 were
indistinguishable from fill targets.

They stayed unreachable only because the Rust extractor's `walkdir` did not
follow symlinks, and each cluster tree's `tests` IS a symlink to the shared
`tests/`; enabling `follow_links` made the crate's own tests visible for the
first time and turned this gap live in the same run. The first fill phase after
that rewrote ten of the 34 (`tests/drop/mod.rs` 5, `tests/test_ensure.rs` 4,
`tests/test_source.rs` 2). Two did not compile — `DetectDrop { has_dropped:
Arc::clone(&has_dropped.atomic) }` for a `Flag` field (E0308) and
`Debug::fmt(&self.0, …)` for a fieldless enum (E0609) — the cluster went red,
spent both layout-retrigger attempts on the wrong cause and STOPPED at
`pass=0`, having scored 93/103 one iteration earlier.

Because `tests/` is a symlink SHARED by every cluster tree, one such splice
corrupts the oracle for all of them at once, and the pipeline commits it.
"""
from __future__ import annotations

from collections.abc import Iterable

from pipeline_core.domain.generation import is_gen_tests_sibling_name
from pipeline_core.domain.models import FnRecord

# Directory names whose contents are test material in every language this
# pipeline reconstructs. `pipeline_core.adapters.discovery` imports this rather
# than keeping a second copy: what counts as oracle is a domain fact, and two
# definitions of it would drift silently in exactly the direction that hurts.
TEST_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", "test", "spec", "specs", "__tests__", "testing"})


def is_oracle_path(file: str) -> bool:
    """True when the crate-relative path `file` holds oracle source.

    Two disqualifying shapes: a `gen_tests` sibling (relocated inline tests,
    which sit in `src/` beside the code they exercise), and any file under a
    test directory. The directory check looks at every parent component, not
    just the first, so it holds whether the enumerating walk was rooted at the
    crate (`tests/drop/mod.rs`) or somewhere above it, and so it also covers
    the fixture sub-crates a compiletest suite keeps under `tests/`.
    """
    parts = file.replace("\\", "/").split("/")
    return (any(p.lower() in TEST_DIR_NAMES for p in parts[:-1])
            or is_gen_tests_sibling_name(parts[-1]))


def is_oracle_fn(rec: FnRecord) -> bool:
    """True when `rec` is oracle content — a test fn, or any fn whose FILE is
    oracle (a `gen_tests` sibling or anything under a test root). Such a record
    is never a fill, re-fill or revert target."""
    return (rec.is_test
            or "test" in rec.cfg_predicates
            or is_oracle_path(rec.file))


def production_fns(recs: Iterable[FnRecord]) -> list[FnRecord]:
    """`recs` minus every oracle fn — the only records a rewrite may target."""
    return [r for r in recs if not is_oracle_fn(r)]
