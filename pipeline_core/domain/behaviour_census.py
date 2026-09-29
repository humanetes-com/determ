"""What a behaviour suite can express — the counts a census prints and the
t0d reading line logs, from ONE definition.

The three "defect observables" are the literals the 98-tree failures pin
(2026-08-26, `scripts/behaviour_suite_census.py`): the TypeId identity of an
`Error::msg` payload (`is::<&str>` / `downcast::<&str>`), the `{:#?}`
alternate Debug strings (`EXPECTED_ALTDEBUG_*`), and the `ensure!` message
(`Condition failed: … (lhs vs rhs)`). A suite "has" one of them only when a
spec carrying the literal ALSO carries a runtime Then — the bake's suite had
the `downcast_ref::<io::Error>` doc-fence text in an ASSERTION spec whose
`then` is null by invariant, which is present-and-uncheckable. The script and
the phase used to keep separate copies of these patterns; a copy that drifts
makes the phase log a reading the census cannot reproduce, so both import
this module.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from pipeline_core.domain.behaviour import (
    RUNTIME_OUTCOME_KINDS,
    BehaviourSpec,
    example_code,
)

# label → pattern; searched over the example's code and the Then's value.
DEFECT_OBSERVABLES: dict[str, re.Pattern[str]] = {
    "identity of Error::msg payload (is::<&str> / downcast::<&str>)":
        re.compile(r"(is|downcast(?:_ref|_mut)?)\s*::\s*<\s*&\s*str\s*>"),
    "alternate Debug ({:#?} / EXPECTED_ALTDEBUG)":
        re.compile(r"\{:#\?\}|EXPECTED_ALTDEBUG"),
    "ensure message (Condition failed: … (lhs vs rhs))":
        re.compile(r"Condition failed:"),
}

# The short names the t0d reading line prints, in the order it prints them.
DEFECT_OBSERVABLE_SHORT_NAMES: tuple[tuple[str, str], ...] = (
    ("identity", "identity of Error::msg payload (is::<&str> / downcast::<&str>)"),
    ("alt-debug", "alternate Debug ({:#?} / EXPECTED_ALTDEBUG)"),
    ("ensure-message", "ensure message (Condition failed: … (lhs vs rhs))"),
)


def _spec_text_matches(spec: BehaviourSpec, rx: re.Pattern[str]) -> bool:
    if rx.search(example_code(spec)):
        return True
    return spec.then is not None and bool(rx.search(spec.then.value or ""))


def defect_observable_presence(
    specs: Iterable[BehaviourSpec],
) -> dict[str, tuple[int, int]]:
    """label → (specs carrying the literal, of those with a RUNTIME then)."""
    specs = tuple(specs)
    out: dict[str, tuple[int, int]] = {}
    for label, rx in DEFECT_OBSERVABLES.items():
        hits = [s for s in specs if _spec_text_matches(s, rx)]
        with_rt = [s for s in hits if s.has_runtime_then]
        out[label] = (len(hits), len(with_rt))
    return out


def defect_observable_flags(specs: Iterable[BehaviourSpec]) -> dict[str, bool]:
    """short name → True iff at least one spec carries the literal WITH a
    runtime Then (the acceptance the harvest change is judged by)."""
    presence = defect_observable_presence(specs)
    return {short: presence[label][1] > 0
            for short, label in DEFECT_OBSERVABLE_SHORT_NAMES}


def runtime_then_counts(specs: Iterable[BehaviourSpec]) -> Counter[str]:
    """OutcomeKind name → count, over specs whose Then is a runtime kind."""
    return Counter(s.then.kind.name for s in specs
                   if s.then is not None and s.then.kind in RUNTIME_OUTCOME_KINDS)


def klass_histogram(specs: Iterable[BehaviourSpec]) -> Counter[str]:
    """BehaviourClass name → count."""
    return Counter(s.klass.name for s in specs)


def render_counts(counts: Counter[str]) -> str:
    """`k1 n1, k2 n2` sorted by key, or `none`."""
    if not counts:
        return "none"
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
