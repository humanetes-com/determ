"""Which std formatting traits a piece of Rust test text invokes.

Its own module so both `domain.attribution` (the observable reading) and
`domain.refill_stamp` (the homonym stamp, which imports `domain.attribution`)
can read the one rule without an import cycle.
"""
from __future__ import annotations

__all__ = ["format_traits_used"]


def format_traits_used(test_src: str) -> frozenset[str]:
    """Which std formatting traits a test's OWN source invokes, from its
    format placeholders and stringify calls: `{:?}`/`{:#?}` need `Debug`,
    `{}`/`{:#}`/`.to_string()` need `Display`.

    The single home of the rule. Which TEXT it reads is the caller's
    question: `AttributionResult.observable_traits_for` reads the failing
    observable (`domain.failing_observable`) for the hop, the escalation
    withhold and the sustain; the homonym stamp reads the whole source.
    """
    traits: set[str] = set()
    if "{:?}" in test_src or "{:#?}" in test_src:
        traits.add("Debug")
    if ("{}" in test_src or "{:#}" in test_src
            or ".to_string()" in test_src):
        traits.add("Display")
    return frozenset(traits)
