"""When to re-run t1.5 layout authoring for a cluster (F11 M3) — pure domain.

The frozen skeleton's layout gets ONE authoring pass at bake time (M2). Two
distinct signals say that pass produced a layout the reconstruction cannot live
with, and BOTH must re-trigger a whole-cluster re-author (coarse but correct —
the same recovery a compile-broken skeleton gets):

  A. DESIGN_CONFLICT — the compiler rejects the authored shape itself
     (E0053/E0046/E0407-class): the layout contradicts what a body must be.
  B. LAYOUT_EXHAUSTION — the layout COMPILES, but the cluster's fills exhaust
     with failures concentrated on private-type usage (E0308/E0599/E0609 naming
     the authored private types): no body can satisfy the tests UNDER this
     layout — a behavioural impossibility only visible mid-fill (the gap the
     original design missed by re-triggering on compile-conflict alone).

Typed, not string-matched: the trigger reason is an enum a caller acts on.
"""
from __future__ import annotations

from enum import Enum, auto

from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.domain.starvation import StarvationCause

# Diagnostics that, when concentrated on the authored private types, mean the
# LAYOUT (not the body) is the wall — a type/trait/field mismatch on those types.
_LAYOUT_ATTRIBUTABLE_CODES = ("E0308", "E0599", "E0609", "E0277")


class LayoutRetriggerCause(Enum):
    NONE = auto()
    DESIGN_CONFLICT = auto()     # cause A — authored shape rejected by the compiler
    LAYOUT_EXHAUSTION = auto()   # cause B — compiles, but no body passes under it


def _errors_hit_private_types(
    error_text: str, private_type_names: frozenset[str],
) -> bool:
    """True iff a layout-attributable code AND an authored private type name both
    appear — the fingerprint of 'the layout, not the body, is wrong'."""
    if not private_type_names:
        return False
    if not any(code in error_text for code in _LAYOUT_ATTRIBUTABLE_CODES):
        return False
    return any(contains_word(error_text, name) for name in private_type_names)


def classify_layout_retrigger(
    *,
    starvation_cause: StarvationCause,
    fills_exhausted: bool,
    error_text: str,
    private_type_names: frozenset[str],
) -> LayoutRetriggerCause:
    """Decide whether the cluster's layout should be re-authored.

    A DESIGN_CONFLICT starvation always re-triggers (cause A). Otherwise, if the
    cluster's fills exhausted AND the residual errors are layout-attributable on
    the authored private types, re-trigger for cause B. Else NONE."""
    if starvation_cause is StarvationCause.DESIGN_CONFLICT:
        return LayoutRetriggerCause.DESIGN_CONFLICT
    if fills_exhausted and _errors_hit_private_types(error_text, private_type_names):
        return LayoutRetriggerCause.LAYOUT_EXHAUSTION
    return LayoutRetriggerCause.NONE


def should_retrigger(cause: LayoutRetriggerCause) -> bool:
    return cause is not LayoutRetriggerCause.NONE
