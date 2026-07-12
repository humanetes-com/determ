"""Typed tree-health classification (F7, pure domain).

The tdd loop read `pass=0 fail=0 frontier=None` for ~30h across restarts while
the crate did not PARSE, and marched on marking fns exhausted from that broken
state with no alarm. This turns the raw cargo result into an explicit health
verdict so the loop can heal-then-halt on UNPARSEABLE instead of mis-exhausting.

A rustc PARSE error has no E-code (`code == ""`) and a distinctive message
(unclosed/unexpected delimiter, "expected one of …"); a TYPE error carries an
E-code (E0308 …). That distinction is the discriminator — no regex.
"""

from __future__ import annotations

from enum import Enum, auto

from pipeline_core.domain.models import Diagnostic


class TreeHealth(Enum):
    OK = auto()             # rc == 0 — compiles (tests may still fail on assertions)
    TESTS_FAILING = auto()  # compiled/ran; ≥1 test reported a verdict
    UNPARSEABLE = auto()    # a rustc PARSE error — heal the file, never exhaust
    UNCOMPILABLE = auto()   # compile error(s), but the tree parses


# Messages rustc emits for PARSE failures (no E-code). Kept narrow so a type
# error's "expected X, found Y" (which DOES carry an E-code) is never misread.
_PARSE_MARKERS = (
    "unclosed delimiter",
    "unexpected closing delimiter",
    "mismatched closing delimiter",
    "unexpected token",
    "expected one of",
    "expected item",
    "expected expression",
    "this file contains an unclosed",
)


def _is_parse_diag(d: Diagnostic) -> bool:
    if d.code:                      # E-coded ⇒ a resolved/type error, not a parse error
        return False
    msg = (d.message or "").lower()
    return any(m in msg for m in _PARSE_MARKERS)


def classify_tree_health(
    rc: int, diagnostics: list[Diagnostic],
    pass_count: int, fail_count: int,
) -> TreeHealth:
    """Verdict from a `run_check_diag`/test result. UNPARSEABLE dominates:
    a tree that does not parse cannot be judged and must be healed, never used
    as evidence to exhaust a fn."""
    if any(_is_parse_diag(d) for d in diagnostics):
        return TreeHealth.UNPARSEABLE
    if rc == 0:
        return TreeHealth.OK
    if pass_count > 0 or fail_count > 0:
        return TreeHealth.TESTS_FAILING
    return TreeHealth.UNCOMPILABLE
