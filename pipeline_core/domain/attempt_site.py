"""Quote a regressed test's crate panic site from the text that produced it.

A regression reading is taken while the attempt is on the tree; the gate
then reverts it. What the reddened test printed names a crate site —
`panicked at src/error.rs:498:38: misaligned pointer dereference` — and that
coordinate is into the ATTEMPT's text. Every prompt that later carries the
reading renders the reverted tree, where line 498 is other code. Run
`4029650-3353ba0c` (2026-09-19, fill 5): the file, impl and per-fn retries of
`src/error.rs` were each told `src/error.rs:498:38` and shown a file whose
line 498 was `if opt.is_some() {` in the kept `downcast_ref`; three routes
re-wrote the same misaligned cast for 1h37m.

:func:`quote_attempt_sites` resolves each crate-source panic site against
the text the caller reads NOW — which must be the attempt, i.e. called at
the measurement, before any revert — and appends the enclosing fn and the
quoted lines, labelled as the attempt's own. A test-source or runtime site
is left alone: test sources are the fixed oracle and every prompt already
holds them, the runtime is not the model's to change.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from pipeline_core.domain.test_outcome import SiteKind, site_kind_of
from pipeline_core.domain.test_results import ATTEMPT_QUOTE_MARK

__all__ = ["ATTEMPT_QUOTE_MARK", "quote_attempt_outputs", "quote_attempt_sites"]

_PANIC_AT = re.compile(r"panicked at (?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)")
# The opening line of a Rust fn (`fn name`, `pub(crate) unsafe fn name`, …).
_FN_OPEN = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(?:async\s+)?"
    r"(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def _enclosing_fn(lines: list[str], line_no: int, *, max_up: int = 200) -> str | None:
    for i in range(line_no, max(0, line_no - max_up), -1):
        m = _FN_OPEN.match(lines[i - 1])
        if m:
            return m.group("name")
    return None


def quote_attempt_sites(
    said: str, read_attempt: Callable[[str], str | None], *, context: int = 2,
) -> str:
    """`said` with every crate-source panic site it names quoted from
    `read_attempt` (distinct sites, in order). `read_attempt` maps the path
    as printed (`src/error.rs`) to that file's text, or None; an unreadable
    file or a line past its end quotes nothing for that site."""
    if not said or ATTEMPT_QUOTE_MARK in said:
        return said
    quotes: list[str] = []
    seen: set[tuple[str, int]] = set()
    for m in _PANIC_AT.finditer(said):
        file, line = m.group("file"), int(m.group("line"))
        if (file, line) in seen or site_kind_of(file) is not SiteKind.CRATE:
            continue
        seen.add((file, line))
        text = read_attempt(file)
        if not text:
            continue
        lines = text.splitlines()
        if not 1 <= line <= len(lines):
            continue
        lo, hi = max(1, line - context), min(len(lines), line + context)
        fn = _enclosing_fn(lines, line)
        where = f", inside `fn {fn}`" if fn else ""
        quoted = "\n".join(
            f"{'>' if n == line else ' '} {n:>4} | {lines[n - 1]}"
            for n in range(lo, hi + 1))
        quotes.append(
            f"at {file}:{line}{where} — {ATTEMPT_QUOTE_MARK} (that attempt was "
            "reverted: the file shown to you does not hold this line):\n"
            + quoted)
    if not quotes:
        return said
    return said + "\n" + "\n".join(quotes)


def quote_attempt_outputs(
    outputs: Mapping[str, str], read_attempt: Callable[[str], str | None],
) -> dict[str, str]:
    """:func:`quote_attempt_sites` over a test -> printed-output map."""
    return {name: quote_attempt_sites(said, read_attempt)
            for name, said in outputs.items()}
