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

from pipeline_core.domain.models import FnLocator, FnRecord
from pipeline_core.domain.rust_scan import contains_word
from pipeline_core.domain.starvation import StarvationCause

# Diagnostics that, when concentrated on the authored private types, mean the
# LAYOUT (not the body) is the wall — a type/trait/field mismatch on those
# types. E0308/E0599/E0609/E0277 are cause B's body-vs-layout mismatches;
# E0053/E0046/E0407 are cause A's frozen-shape rejections — both classes only
# count as layout evidence when they ALSO name one of this cluster's private
# types (see `classify_layout_retrigger`).
_LAYOUT_ATTRIBUTABLE_CODES = (
    "E0308", "E0599", "E0609", "E0277", "E0053", "E0046", "E0407")


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

    BOTH causes require `_errors_hit_private_types` — independent textual
    evidence that one of THIS cluster's authored/frozen private types is
    actually implicated, not just an ordinary retriable body bug. This is
    deliberately NOT "any DESIGN_CONFLICT retriggers": `starvation.py`'s
    classifier returns DESIGN_CONFLICT for an E0609 "no field X on Y" line
    whenever Y's name merely APPEARS in the prompt (a proxy for "not a lookup
    gap"), regardless of whether X genuinely doesn't exist ANYWHERE — that
    proxy is silent on receiver-type mixups (a body method treating `self` as
    the wrong sibling type), which are fixable by an ordinary retry, not a
    layout problem. Retriggering a whole-cluster private-layout re-author is
    expensive and disruptive (§ `retrigger_layout.py`); requiring the private
    type's own name in the error text is the same fingerprint LAYOUT_EXHAUSTION
    already uses, now applied uniformly so a plain mislabeled bug can't fire it."""
    if not _errors_hit_private_types(error_text, private_type_names):
        return LayoutRetriggerCause.NONE
    if starvation_cause is StarvationCause.DESIGN_CONFLICT:
        return LayoutRetriggerCause.DESIGN_CONFLICT
    if fills_exhausted:
        return LayoutRetriggerCause.LAYOUT_EXHAUSTION
    return LayoutRetriggerCause.NONE


def should_retrigger(cause: LayoutRetriggerCause) -> bool:
    return cause is not LayoutRetriggerCause.NONE


def find_fns_referencing(
    fn_records: list[FnRecord], names: frozenset[str],
) -> list[FnLocator]:
    """Every already-filled fn whose signature OR body names one of `names` —
    the surgical revert scope after a component is re-authored: ONLY fns that
    actually touch the retriggered types get reverted to stubs, everything
    else in the cluster keeps its existing fill untouched."""
    out: list[FnLocator] = []
    for f in fn_records:
        haystack = f.sig_text + " " + f.body_text
        if any(contains_word(haystack, n) for n in names):
            out.append(FnLocator(f.file, f.fn_name))
    return out
