"""When to re-run t1.5 layout authoring for a cluster — pure domain.

The frozen skeleton's layout gets ONE authoring pass at bake time (M2). Several
distinct signals say that pass produced a layout the reconstruction cannot live
with, and EACH must re-trigger a whole-cluster re-author (coarse but correct):

  A. DESIGN_CONFLICT — the compiler rejects the authored shape itself
     (E0053/E0046/E0407-class): the layout contradicts what a body must be.
     Reached from a fill attempt's errors, or — `classify_compile_retrigger` —
     from a cluster-wide BUILD failure in which the lib compiled and only the
     test binaries did not. That second route is the only one that acts while
     the tree cannot produce a single test verdict, so it fires on the first
     such sweep rather than waiting out a stall budget that measures fills
     nothing can run.
  B. LAYOUT_EXHAUSTION — the layout COMPILES, but the cluster's fills exhaust
     with failures concentrated on private-type usage (E0308/E0599/E0609 naming
     the authored private types): no body can satisfy the tests UNDER this
     layout — a behavioural impossibility only visible mid-fill (the gap the
     original design missed by re-triggering on compile-conflict alone).

Typed, not string-matched: the trigger reason is an enum a caller acts on.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

from pipeline_core.domain.models import FnLocator, FnRecord
from pipeline_core.domain.oracle_immutability import production_fns
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


MECHANICAL_KEY_PREFIX = "mech:"
DEFAULT_MAX_MECHANICAL = 2


def mechanical_key(component_key: str) -> str:
    """Attempts-store key counting a component's CONSECUTIVE mechanical
    failures — the ones that refund the model attempt. Prefixed, so it shares
    the attempts file without colliding with a component key (Rust idents
    joined by `|` can never start with `mech:`)."""
    return f"{MECHANICAL_KEY_PREFIX}{component_key}"


def mechanical_budget_spent(
    attempts: Mapping[str, int], component_key: str,
    cap: int = DEFAULT_MAX_MECHANICAL,
) -> bool:
    """Whether this component has already failed mechanically `cap` times in a
    row without the model being at fault.

    A mechanical failure (splice/locate miss, a propagation that could not
    reach the live tree) REFUNDS the attempt, so a pipeline bug can never
    exhaust the budget meant for the model. The consequence is that the very
    same failure re-fires on the next sweep, and every re-fire spends a full
    layout-authoring model call on a bug no model output can change. This
    bounds the refund: unbounded retries of an attempt-invariant failure is the
    burn the refund was supposed to prevent, aimed at a different budget."""
    return attempts.get(mechanical_key(component_key), 0) >= cap


class LayoutRetriggerCause(Enum):
    NONE = auto()
    DESIGN_CONFLICT = auto()     # cause A — authored shape rejected by the compiler
    LAYOUT_EXHAUSTION = auto()   # cause B — compiles, but no body passes under it
    RUNTIME_CRASH = auto()       # cause C — compiles, but bodies filled under the
    #                              layout crash whole test binaries (their tests
    #                              VANISH from the sweep output with no compile
    #                              error at all — the unsafe-scheme fingerprint)


@dataclass(frozen=True, slots=True)
class CrashEvidence:
    """Cause C's typed payload from the TDD driver: which expected tests
    reported no verdict (their binary crashed), which fns those tests
    statically reach (reverse-map targets), and for how many consecutive
    sweeps the shrink persisted."""
    vanished: frozenset[str]
    suspect_fns: frozenset[FnLocator]
    consecutive_sweeps: int = 0


@dataclass(frozen=True, slots=True)
class CompileEvidence:
    """Cause A's typed payload from the TDD driver: the cluster's test build
    failed outright, so no fill attempt exists to classify and no test ran to
    crash. `digest` is the deduped rustc diagnostic text; `lib_clean` records
    that the LIBRARY itself compiled; `consecutive_sweeps` is how many sweeps in
    a row have failed to build."""
    digest: str
    lib_clean: bool
    consecutive_sweeps: int = 0


def classify_compile_retrigger(
    evidence: CompileEvidence,
    component_names: frozenset[str],
) -> LayoutRetriggerCause:
    """Cause A decision for ONE component, from a cluster-wide BUILD failure.

    `lib_clean` is the load-bearing gate, not a heuristic: a fn body lives inside
    the lib, so a build where the lib succeeded and only the test binaries failed
    cannot be explained by any body a fill wrote — what the binaries link against
    is the frozen/authored shape, which is exactly what a re-author replaces.
    Without that gate a single bad fill that broke the lib would trigger a
    whole-component re-author and mass revert.

    A build failure is the strongest form of cause A: not one fill's opinion but
    the whole cluster failing to compile, and every sweep spent under it is
    wasted outright — no test can report a verdict. So it needs no streak."""
    if not evidence.lib_clean or not evidence.digest.strip():
        return LayoutRetriggerCause.NONE
    if not _errors_hit_private_types(evidence.digest, component_names):
        return LayoutRetriggerCause.NONE
    return LayoutRetriggerCause.DESIGN_CONFLICT


def classify_crash_retrigger(
    evidence: CrashEvidence,
    fn_records: list[FnRecord],
    component_names: frozenset[str],
) -> LayoutRetriggerCause:
    """Cause C decision for ONE frozen component. RUNTIME_CRASH iff a suspect
    fn (a reverse-map target of a vanished test) either IS a component member
    by name or its filled signature/body references a component type/fn name —
    the crash implicates THIS component's scheme, not an unrelated body. No
    compile-error text is required: the defining feature of the crash class is
    a clean compile."""
    if not evidence.vanished or not evidence.suspect_fns:
        return LayoutRetriggerCause.NONE
    if any(fn.fn_name in component_names for fn in evidence.suspect_fns):
        return LayoutRetriggerCause.RUNTIME_CRASH
    referencing = set(find_fns_referencing(fn_records, component_names))
    if referencing & set(evidence.suspect_fns):
        return LayoutRetriggerCause.RUNTIME_CRASH
    return LayoutRetriggerCause.NONE


def crash_evidence_confounded(
    fn_records: list[FnRecord], component_names: frozenset[str],
) -> bool:
    """Cause C cannot tell 'the authored scheme crashes' from 'the component's
    own still-stubbed fns crash' — a stub panic inside drop/vtable code aborts
    the test binary exactly like a bad pointer scheme (observed live: a fresh
    restart after a successful re-author left 102 reverted stubs, the sweep
    shrank, and the retrigger burned its last attempt re-authoring a layout
    whose bodies were never tried). While half or more of the component's own
    fns are still stubs the vanish evidence is confounded: fills must run
    first."""
    members = [r for r in fn_records if r.fn_name in component_names]
    if not members:
        return False
    stubbed = sum(1 for r in members if r.has_unimplemented_body)
    return stubbed * 2 >= len(members)


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


DEFAULT_REFILL_PER_ITER = 4
DEFAULT_REFILL_GRACE_MAX_ITERS = 12


@dataclass(frozen=True, slots=True)
class RefillGrace:
    """The window in which a successful retrigger's DELIBERATE re-stub must not
    be read as evidence, sized to the number of bodies it authorised for re-fill.

    A re-author reverts every fn referencing the component to a stub so each
    re-fills under the new shape. Until they do, the tree is WORSE than before by
    construction: a stub panic inside drop/vtable code aborts whole test binaries,
    so the pass count collapses and the oracle shrinks. Two consumers read those
    signals and both are wrong during the window:

      * the batch ratchet convicts on "this sweep measures last iter's fills";
      * the crash retrigger convicts on "the oracle shrank for N sweeps".

    Left ungated, they destroy the re-author and then re-fire on the crash it was
    about to fix — the ratchet's revert is a whole-`src/` restore, so it takes the
    new DEFINITION down with the stubs, while the capped attempt stays spent.

    A FIXED one-iter window (the first fix) is sized to the mechanism's smallest
    case, not to its work. Measured live on `anyhow` run `1135197`: a 47-body
    re-stub landed **3** re-fills in its one graced iter — 25 of the 31 frontier
    fns were consumers blocked on still-stubbed producers, so they can only fill
    in dependency order, a few per iter. Iters 1-5 then ran the full cycle twice
    (pass 111 → 20 → 111 → 20 → 111, the two 20s identical), burning both capped
    `StdError` attempts and two 47-body authoring calls for nothing.

    So the window is sized to the bodies and then gated on PROGRESS, which is
    what keeps it safe: it survives only while re-fills keep landing, and the
    high-water snapshot is never re-taken below its own mark, so an expiring
    grace reverts to exactly the tree a one-iter grace would have. The cost of
    being generous is bounded iters; the cost of being stingy is a run that can
    never repair a layout-dependent crash at all.

    `restubbed == 0` is a FIRED-but-failed attempt: nothing was re-stubbed, so
    the window suppresses the shrink evidence (no back-to-back re-fire) but
    excuses no breach — there is no deliberate re-stub to explain one.
    """
    iters_left: int
    restubbed: int
    fills_at_grant: int

    @classmethod
    def none(cls) -> RefillGrace:
        return cls(iters_left=0, restubbed=0, fills_at_grant=0)

    @classmethod
    def for_restub(
        cls, restubbed: int, fills_so_far: int,
        per_iter: int = DEFAULT_REFILL_PER_ITER,
        max_iters: int = DEFAULT_REFILL_GRACE_MAX_ITERS,
    ) -> RefillGrace:
        """A window for `restubbed` bodies: one iter per `per_iter` bodies,
        never fewer than one (the smallest re-stub keeps the original
        semantics exactly) and never more than `max_iters`."""
        sized = -(-max(restubbed, 0) // max(per_iter, 1))
        return cls(iters_left=min(max(sized, 1), max(max_iters, 1)),
                   restubbed=max(restubbed, 0),
                   fills_at_grant=fills_so_far)

    @classmethod
    def for_failed_attempt(cls, fills_so_far: int) -> RefillGrace:
        """One iter, nothing re-stubbed — a fired attempt that did not land."""
        return cls(iters_left=1, restubbed=0, fills_at_grant=fills_so_far)

    @property
    def active(self) -> bool:
        """Whether this sweep's shrink is the window's own doing, so neither the
        vanish streak nor the accelerated confirm may read it as crash evidence."""
        return self.iters_left > 0

    @property
    def excuses_breach(self) -> bool:
        """Whether a ratchet breach on this sweep has a deliberate re-stub to
        explain it. Only a window that actually re-stubbed bodies does."""
        return self.active and self.restubbed > 0

    def consume(self, fills_so_far: int) -> RefillGrace:
        """The window after one graced iter has run its fills. Survives only
        while re-fills keep LANDING: an iter that filled nothing has no re-author
        in flight to protect, whatever budget is left. Progress is measured in
        fills rather than pass count on purpose — the blocked-consumer regime
        above fills bodies for several iters before any test can move."""
        if not self.active or fills_so_far <= self.fills_at_grant:
            return RefillGrace.none()
        return RefillGrace(iters_left=self.iters_left - 1,
                           restubbed=self.restubbed,
                           fills_at_grant=fills_so_far)


def find_fns_referencing(
    fn_records: list[FnRecord], names: frozenset[str],
) -> list[FnLocator]:
    """Every already-filled fn whose signature OR body names one of `names` —
    the surgical revert scope after a component is re-authored: ONLY fns that
    actually touch the retriggered types get reverted to stubs, everything
    else in the cluster keeps its existing fill untouched.

    Oracle fns are excluded before the name test, not after: a test body names
    the very types a re-author replaces (`Error`, `chain`, `backtrace`), so an
    unfiltered scope reverts the suite that is supposed to judge the re-author
    — observed live, 37 reverts that stubbed out whole `gen_tests` files."""
    out: list[FnLocator] = []
    for f in production_fns(fn_records):
        haystack = f.sig_text + " " + f.body_text
        if any(contains_word(haystack, n) for n in names):
            out.append(FnLocator(f.file, f.fn_name))
    return out
