"""Does the SIBLING_REFERENCE steer the retry toward the pattern LAST_ERROR just
rejected? (pure domain)

The per-impl prompt carries two sections that can contradict each other on
attempt 2. `LAST_ERROR` is the previous attempt's own rustc verdict, quoted
whole — including the source line the compiler points at. `SIBLING_REFERENCE`
is the same-named fn from ANOTHER impl block of the file, introduced with
"follow the same structural pattern". The sibling compiles because its impl
header bounds its types differently; when its body carries the very expression
the error rejected, the two sections disagree and the sibling wins — the model
reads a worked example as stronger evidence than a diagnostic.

The instance (run `902902-daee577d`, 2026-08-20, prompt `11899c17478d`): the
target was `impl<C> StdError for ContextError<C, Error>` (`Error` is this
crate's own type, which does NOT implement `core::error::Error`); attempt 1
wrote `Some(&self.error)` and got `E0277: the trait bound `Error:
core::error::Error` is not satisfied … 148 |         Some(&self.error)`.
Attempt 2's prompt carried that diagnostic in `LAST_ERROR` AND, under "follow
the same structural pattern", the `impl<C, E> StdError for ContextError<C, E>`
sibling whose body contains `Some(&self.error)` (bounded `E: StdError`, so it
compiles there). The model wrote `Some(&self.error)` again — `identical errors
after attempt 2 — stopping`. The path the unit needed was in the same prompt
(`Own::by_ref`, the `ErrorImpl::…(self.inner.by_ref())` previews), unused.

The test is EXPRESSION-based, not body-equality. The sibling at that moment
was not byte-equal to the failing attempt — it had grown a `TypeId` branch
around the same `Some(&self.error)` — so "sibling == last body" would have
missed the one instance this exists for. What the compiler quotes is the
expression the error is ABOUT; if that expression sits in the sibling, the
sibling is steering toward it, whatever else surrounds it.

What it deliberately does not do: it does not drop the sibling. The same body
is also visible as a RELATED block, so removing one section would not remove
the pattern from the prompt; it would only remove the place where the
contradiction can be named. The builder flips the prefix instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# rustc's quoted source line inside a diagnostic: `148 |         Some(&self.error)`.
# Label/caret lines (`    |              ^^^^`) have no line number and do not match.
_QUOTED_SOURCE = re.compile(r"^\s*\d+\s*\|(.*)$")

# Quoted lines that are structure, not an expression: a signature, an impl
# header, a where clause, a lone brace. A sibling SHARES these with every
# same-named fn by construction, so matching them would flip the prefix on any
# error that merely points at the fn's header (E0053, E0046).
_STRUCTURAL = re.compile(
    r"^(pub(\([^)]*\))?\s+)?"
    r"(((unsafe|const|async|extern(\s+\"[^\"]*\")?)\s+)*fn\s"
    r"|(struct|enum|union|trait|type|mod|use)\s)"
    r"|^impl\b|^where\b|^[{}()\[\];,]*$")


def _canon(text: str) -> str:
    """Whitespace-folded identity — the same fold `landed_change` uses, so an
    indentation difference between the diagnostic and the sibling is not a
    difference."""
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class SiblingSteer:
    """The rejected expressions the sibling reference carries (canonical,
    in diagnostic order, deduplicated). Empty = the two sections agree."""

    shared: tuple[str, ...] = ()

    @property
    def contradicts(self) -> bool:
        return bool(self.shared)


def rejected_expressions(last_error: str, *, min_chars: int = 6) -> tuple[str, ...]:
    """The source lines rustc quoted in the previous attempt's diagnostics —
    the expressions the errors are ABOUT — canonicalised, structural lines
    dropped, deduplicated, in order. `min_chars` drops fragments too short to
    name a pattern (`}`, `None`, `)`)."""
    out: list[str] = []
    for raw in last_error.splitlines():
        m = _QUOTED_SOURCE.match(raw)
        if not m:
            continue
        expr = _canon(m.group(1))
        if len(expr) < min_chars or _STRUCTURAL.match(expr):
            continue
        if expr not in out:
            out.append(expr)
    return tuple(out)


def sibling_steer(sibling_section: str, last_error: str) -> SiblingSteer:
    """Which of the previous attempt's rejected expressions the sibling
    reference contains. Attempt 1 (no `last_error`) and a block with no
    sibling both read as agreement."""
    sib = _canon(sibling_section)
    if not sib or not last_error:
        return SiblingSteer()
    return SiblingSteer(tuple(
        e for e in rejected_expressions(last_error) if e in sib))
