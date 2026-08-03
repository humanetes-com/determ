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
from pipeline_core.domain.rust_scan import (
    contains_word,
    declared_member_count,
    declared_members,
    field_reads,
)
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
INFLIGHT_KEY_PREFIX = "inflight:"
PAYOFF_KEY_PREFIX = "payoff:"
DEFAULT_MAX_MECHANICAL = 2

# Attempts-store keys for the CLUSTER-wide payoff budget (below). Prefixed on
# the same rule as `mechanical_key`: a component key is Rust idents joined by
# `|` and can never start with `payoff:`.
PAYOFF_HIGH_KEY = f"{PAYOFF_KEY_PREFIX}high"      # best pass_count seen so far
PAYOFF_PENDING_KEY = f"{PAYOFF_KEY_PREFIX}pending"  # a fire awaiting its verdict
PAYOFF_ZERO_KEY = f"{PAYOFF_KEY_PREFIX}zero"      # consecutive fires that bought nothing
DEFAULT_MAX_ZERO_PAYOFF = 2


def mechanical_key(component_key: str) -> str:
    """Attempts-store key counting a component's CONSECUTIVE mechanical
    failures — the ones that refund the model attempt. Prefixed, so it shares
    the attempts file without colliding with a component key (Rust idents
    joined by `|` can never start with `mech:`)."""
    return f"{MECHANICAL_KEY_PREFIX}{component_key}"


def inflight_key(component_key: str) -> str:
    """Attempts-store key marking an attempt that has been CHARGED but whose
    outcome is not yet known. Same prefixing rule as `mechanical_key`."""
    return f"{INFLIGHT_KEY_PREFIX}{component_key}"


def _is_bookkeeping_key(key: str) -> bool:
    return key.startswith(
        (MECHANICAL_KEY_PREFIX, INFLIGHT_KEY_PREFIX, PAYOFF_KEY_PREFIX))


def settle_retrigger_payoff(
    attempts: Mapping[str, int], best_pass: int | None,
) -> dict[str, int]:
    """Charge the PREVIOUS retrigger with what it bought, in pass_count.

    The per-component cap bounds attempts on ONE component; nothing bounded the
    WALK across components, and `retrigger_layout` `continue`s to the next
    candidate when a component is capped. On `anyhow`/`nightly`, 2026-08-02,
    that produced a period-4 limit cycle: iters 4, 8, 12 and 16 each fired a
    LAYOUT_EXHAUSTION re-author (`Adhoc|AdhocKind` twice, then
    `Boxed|BoxedKind` twice, the 35-name mega component being already at cap),
    each re-stubbed 2-3 bodies for `pass=93 -> 79`, and each refilled back to
    exactly 93 two iters later. Four fires, four returns to the same number,
    16 to 54 minutes apart. 16 non-gated components are recorded for that
    cluster (17 rows, one GATED and therefore never a candidate), so 26
    further attempts stood ready to spend that again on the same result.

    The evidence selecting the next component is attempt-INVARIANT: iters 4
    and 16 had the same pass_count, the same frontier of 1 fn, the same 18
    exhausted siblings and the same error text. A different component was
    chosen only because the previous one was capped, not because anything new
    implicated it. That is the burn `mechanical_budget_spent` bounds one level
    down, and it wants the same treatment one level up.

    So: a fire records the pass_count it fired at (`PAYOFF_PENDING_KEY`), and
    the next decision point settles it. Improvement over the high-water mark
    clears the streak; no improvement charges one zero-payoff fire. The charge
    is idempotent — the pending mark is consumed when read — so the three call
    sites in the TDD driver cannot charge one fire twice.

    `best_pass is None` (or negative, the unset floor) means the caller could
    not measure, and settles nothing: an unmeasured sweep must not convict a
    retrigger that may well have paid.

    Two classes of fire are deliberately never marked pending, so they can
    never be settled here — see `mark_retrigger_pending`: an attempt the
    action REFUNDED, and a fire aimed by evidence other than the plateau."""
    if best_pass is None or best_pass < 0:
        return dict(attempts)
    out = dict(attempts)
    high = out.get(PAYOFF_HIGH_KEY)
    if high is None or best_pass > high:
        out[PAYOFF_HIGH_KEY] = best_pass
        out[PAYOFF_ZERO_KEY] = 0
        out.pop(PAYOFF_PENDING_KEY, None)
        return out
    if out.pop(PAYOFF_PENDING_KEY, None) is not None:
        out[PAYOFF_ZERO_KEY] = out.get(PAYOFF_ZERO_KEY, 0) + 1
    return out


def mark_retrigger_pending(
    attempts: Mapping[str, int], best_pass: int | None,
) -> dict[str, int]:
    """Record that a retrigger has REACHED AN OUTCOME at `best_pass`, so the
    next decision point can settle what it bought. See
    `settle_retrigger_payoff`.

    A caller that did not measure a pass_count records NOTHING: payoff is a
    claim about a number, and a fire nobody scored can neither be convicted of
    buying nothing nor credited with buying something. This also keeps the
    attempts store byte-identical for callers that never pass `best_pass`.

    Marked AFTER the attempt, never beside the charge that opens it, and only
    when the attempt was not refunded. The two failure classes the action
    refunds — a splice/locate miss, and a process killed inside the authoring
    call (`reconcile_interrupted_attempts`) — bought no model evidence, and the
    rule the refund exists to enforce is that no budget may be spent by them.
    A budget one level up is still a budget: charged at the open, two refunded
    attempts would stop a whole cluster's walk, and the refusal below would
    report bodies re-stubbed by attempts that reverted nothing."""
    if best_pass is None or best_pass < 0:
        return dict(attempts)
    out = dict(attempts)
    out[PAYOFF_PENDING_KEY] = 1
    out.setdefault(PAYOFF_HIGH_KEY, best_pass)
    return out


def payoff_budget_spent(
    attempts: Mapping[str, int], cap: int = DEFAULT_MAX_ZERO_PAYOFF,
) -> bool:
    """Whether this CLUSTER's last `cap` PLATEAU retriggers each left the
    pass_count where they found it. Refusing here lets the TDD driver's own
    plateau exit stand, which is what the module docstring always claimed a
    capped component would degrade to — true per component, false for the
    cluster.

    A plateau budget, and only a plateau budget. The claim it rests on is that
    the evidence is attempt-invariant, which is a statement about the
    fills-exhausted route: cause A's cluster-wide build failure and cause C's
    crashed binaries are new events, each with its own streak, and neither is
    the same unchanged error text a fourth time. Refusing them here would also
    be terminal — a cluster whose test binaries do not build scores nothing, so
    the high-water mark that clears this streak can never move again, and cause
    A is the only route that acts while no test can report a verdict.

    Self-clearing: any sweep that beats the high-water mark resets the streak,
    so a cluster that starts moving again gets its retriggers back."""
    return attempts.get(PAYOFF_ZERO_KEY, 0) >= cap


def reconcile_interrupted_attempts(
    attempts: Mapping[str, int],
) -> tuple[dict[str, int], tuple[str, ...]]:
    """`(repaired attempts, component keys refunded)` — settle every attempt
    that was charged and never resolved.

    The counter is incremented and persisted BEFORE the layout-authoring call,
    which is right: the process must not be able to retry an in-flight attempt
    for free after a crash. But it also means a kill anywhere in that window —
    a `--force` restart, an OOM, the operator replacing the process — spends a
    capped attempt and buys NO model evidence, which is exactly what
    `mechanical_budget_spent` reserves a refund for. Two of the four attempts
    ever spent on `anyhow`'s 35-name mega component went that way, silently:
    a `pre-layout-retrigger checkpoint` with no matching outcome line, and
    nothing in the pipeline classified a restart as mechanical.

    The marker is written beside the increment and cleared by whichever outcome
    the attempt reaches, so a marker that OUTLIVED the process that wrote it is
    proof of an interrupted attempt. Refunding it here — at load, before any
    cap is read — makes the budget mean what it says: model attempts, not
    process lifetimes.

    Counted as a mechanical failure rather than erased, on the same bound the
    splice/locate refunds use: an authoring call that reliably kills the process
    would otherwise re-fire for ever on a budget nothing can exhaust."""
    out = dict(attempts)
    refunded: list[str] = []
    for key in sorted(k for k in out if k.startswith(INFLIGHT_KEY_PREFIX)):
        component = key[len(INFLIGHT_KEY_PREFIX):]
        del out[key]
        if not component or _is_bookkeeping_key(component):
            continue
        out[component] = max(0, out.get(component, 0) - 1)
        out[mechanical_key(component)] = out.get(mechanical_key(component), 0) + 1
        refunded.append(component)
    return out, tuple(refunded)


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
    sweeps the shrink persisted.

    `blamed_fns` is a SECOND, stronger channel: the fns the crash localizer put
    in the faulting stack (gdb frames, or ASAN's use-after-free origin). The two
    are different grades of evidence and must not be merged into one set —
    `suspect_fns` says a vanished test *could* have called this fn, `blamed_fns`
    says the fault *was observed* there. Only the second can separate two
    components that the same vanished test reaches, and that is precisely what
    cause C could not do while it aimed by record order."""
    vanished: frozenset[str]
    suspect_fns: frozenset[FnLocator]
    consecutive_sweeps: int = 0
    blamed_fns: frozenset[FnLocator] = frozenset()


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


def _implicates(
    locators: frozenset[FnLocator],
    fn_records: list[FnRecord],
    component_names: frozenset[str],
) -> bool:
    """Whether any of `locators` either IS a component member by name or has a
    filled signature/body that references a component type/fn name — i.e. the
    crash reaches THIS component's scheme rather than an unrelated body."""
    if any(fn.fn_name in component_names for fn in locators):
        return True
    return bool(set(find_fns_referencing(fn_records, component_names)) & locators)


def classify_crash_retrigger(
    evidence: CrashEvidence,
    fn_records: list[FnRecord],
    component_names: frozenset[str],
) -> LayoutRetriggerCause:
    """Cause C decision for ONE frozen component. RUNTIME_CRASH iff the crash
    implicates this component (`_implicates`). No compile-error text is required:
    the defining feature of the crash class is a clean compile.

    **When the localizer produced blame, the blame DECIDES and reachability is
    not consulted.** A vanished test statically reaches most of the crate, so
    reverse-map suspects implicate several components at once and the caller,
    scanning components in record order, takes whichever is recorded first. That
    is how five sessions of cause C fired at `ChainState` and `Quoted` while
    every ASAN sweep of every iter blamed `src/error.rs::downcast`, which neither
    component owns — each mis-aimed fire cost a capped attempt, a layout-
    authoring call, and a re-stub the ratchet then had to undo (measured: 112 →
    97, twice). A component the observed fault does not touch is not a cause-C
    candidate, so returning NONE here is what stops the caller falling through to
    it. Blame is absent (empty) whenever localization was skipped or found no
    native fault, and then this is exactly the reachability rule it always was."""
    if not evidence.vanished:
        return LayoutRetriggerCause.NONE
    if evidence.blamed_fns:
        return (LayoutRetriggerCause.RUNTIME_CRASH
                if _implicates(evidence.blamed_fns, fn_records, component_names)
                else LayoutRetriggerCause.NONE)
    if not evidence.suspect_fns:
        return LayoutRetriggerCause.NONE
    return (LayoutRetriggerCause.RUNTIME_CRASH
            if _implicates(evidence.suspect_fns, fn_records, component_names)
            else LayoutRetriggerCause.NONE)


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


# The rate the window is sized against: bodies that actually re-fill per graced
# iter. 3 is the MEASURED rate (run `1135197`, a 47-body re-stub), not a guess —
# sizing against an optimistic rate silently under-grants, and the cap below is
# then what expires the window mid-recovery.
DEFAULT_REFILL_PER_ITER = 3
# Runaway bound only. It must NOT bind at observed component sizes, or the
# window stops being sized to its work exactly where the evidence came from:
# the largest re-stub seen is 47 bodies -> ceil(47/3) = 16 iters. When it does
# bind, `RefillGrace.clipped` says so and the grant logs it.
#
# Raised 20 -> 40 once the bound was seen to BIND on the case that matters:
# `anyhow`'s 35-name mega component reverts ~100 bodies, wanting ceil(100/3) =
# 34 iters, so every grant for it announced `clipped=True` — the window
# declaring, before the first re-fill, that it would expire mid-recovery. A cap
# that expires the window it just granted makes the re-author unwinnable no
# matter how many attempts the budget holds, which is why raising this is the
# precondition for ever spending one on that component. Safe to raise because
# the window is gated on PROGRESS, not on the count: `consume` closes it the
# first iter that lands zero fills, so a bigger bound buys iters only while
# bodies keep landing and can never hold a stalled re-author open.
DEFAULT_REFILL_GRACE_MAX_ITERS = 40


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
      * the crash retrigger convicts on "the oracle shrank for N sweeps";
      * the stall budget convicts on "N iters of fills moved no test".

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
    what keeps it safe: it survives only while fills keep landing, and the
    high-water snapshot is never re-taken below its own mark, so an expiring
    grace reverts to exactly the tree a one-iter grace would have. The cost of
    being generous is bounded iters; the cost of being stingy is a run that can
    never repair a layout-dependent crash at all.

    `restubbed == 0` is a FIRED-but-failed attempt: nothing was re-stubbed, so
    the window suppresses the shrink evidence (no back-to-back re-fire) but
    excuses no breach and freezes no score — there is no deliberate re-stub to
    explain either.

    `granted_at` is the iter that opened the window, and it exists because the
    window is granted and spent by the SAME loop: a grant that lands after the
    iter's read sites must not be spent by that iter's own scoring, or it expires
    before anything can read it (which is how the failed-attempt window was a
    silent no-op). One iter of window means one iter of reads.
    """
    iters_left: int
    restubbed: int
    fills_at_grant: int
    granted_at: int = 0
    clipped: bool = False

    @classmethod
    def none(cls) -> RefillGrace:
        return cls(iters_left=0, restubbed=0, fills_at_grant=0)

    @classmethod
    def for_restub(
        cls, restubbed: int, fills_so_far: int,
        per_iter: int = DEFAULT_REFILL_PER_ITER,
        max_iters: int = DEFAULT_REFILL_GRACE_MAX_ITERS,
        granted_at: int = 0,
    ) -> RefillGrace:
        """A window for `restubbed` bodies: one iter per `per_iter` bodies,
        never fewer than one (the smallest re-stub keeps the original
        semantics exactly) and never more than `max_iters`. `clipped` records
        that the cap, not the work, decided the size — the window is then NOT
        sized to its bodies and expiring short is expected, so say so rather
        than leaving a future 60-body re-stub to look like a 20-body one."""
        sized = -(-max(restubbed, 0) // max(per_iter, 1))
        bound = max(max_iters, 1)
        return cls(iters_left=min(max(sized, 1), bound),
                   restubbed=max(restubbed, 0),
                   fills_at_grant=fills_so_far,
                   granted_at=granted_at,
                   clipped=sized > bound)

    @classmethod
    def for_failed_attempt(cls, fills_so_far: int,
                           granted_at: int = 0) -> RefillGrace:
        """One iter, nothing re-stubbed — a fired attempt that did not land."""
        return cls(iters_left=1, restubbed=0, fills_at_grant=fills_so_far,
                   granted_at=granted_at)

    @property
    def active(self) -> bool:
        """Whether this sweep's shrink is the window's own doing, so neither the
        vanish streak nor the accelerated confirm may read it as crash evidence.
        True for a fired-but-failed attempt too: it re-stubbed nothing, but its
        evidence is the same sweep it just acted on, so re-firing on it is the
        back-to-back burn this window exists to stop."""
        return self.iters_left > 0

    @property
    def bodies_in_flight(self) -> bool:
        """Whether the window is holding re-stubbed bodies that have yet to
        re-fill — the state in which the pass count cannot move on the tree's
        own merits. The consumers that must not read a DEPRESSED SCORE (the
        ratchet's breach, the stall budget, the abandon-the-bet restore) key on
        this; only `active` keys on the shrink."""
        return self.active and self.restubbed > 0

    def consume(self, fills_so_far: int) -> RefillGrace:
        """The window after one graced iter has run its fills. Survives only
        while fills keep LANDING: an iter that filled nothing has no re-author
        in flight to protect, whatever budget is left. Progress is measured in
        fills rather than pass count on purpose — the blocked-consumer regime
        above fills bodies for several iters before any test can move.

        Counted in the loop's own fill total, so ANY fill keeps the window open,
        not only a re-fill of a re-stubbed body. That is deliberate: the loop
        learns which fns filled only after this point, and the same "did
        anything fill" condition is what re-admits the exhausted re-stubs into
        the next iter's frontier — a window that outlived their re-admission
        would protect work that can no longer run."""
        if not self.active or fills_so_far <= self.fills_at_grant:
            return RefillGrace.none()
        return RefillGrace(iters_left=self.iters_left - 1,
                           restubbed=self.restubbed,
                           fills_at_grant=fills_so_far,
                           granted_at=self.granted_at,
                           clipped=self.clipped)


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


def filled_body_text(fn_records: list[FnRecord]) -> str:
    """The crate's already-written implementation code as one blob — every
    production fn that currently has a real body.

    Stubs contribute nothing by construction: an `unimplemented!()` body reads
    no field, which is exactly why a scaffold of them compiles against any
    layout at all. Oracle fns are excluded (`production_fns`) because a test
    body reads the PUBLIC surface, and the public surface is not what a
    re-author is allowed to change."""
    return "\n".join(
        f.body_text for f in production_fns(fn_records)
        if not f.has_unimplemented_body and f.body_text.strip())


def dropped_members_in_use(
    current_def: str, authored_def: str, filled_bodies: str,
) -> dict[str, int]:
    """Members the CURRENT definition declares, an AUTHORED replacement does
    not, and the crate's filled bodies still read — name → read count, reported
    only when the replacement declares FEWER storage slots than the definition
    it replaces.

    This is the judgement the compile gate cannot make. A re-authored layout is
    accepted on the strength of the SKELETON compiling, and a skeleton's bodies
    are all `unimplemented!()`: no body constructs the type or reads a field, so
    every layout that merely parses compiles there — including one that has
    nowhere left to put the crate's data. Measured on `anyhow`/nightly
    (2026-08-01): a re-author reduced `ErrorVTable` from 7 fn-pointer fields to
    1 and replaced `ErrorImpl`'s payload field `_error: E` with
    `PhantomData<E>`, which stores nothing. It compiled, it passed the layout
    pins — `Error` is a `NonNull` to `ErrorImpl`, so no repr pin can see through
    it — and the cluster went from 93 passing tests to a tree in which
    `construct<E>(error: E, …)` has no field to put `error` into. The evidence
    that says so was already on disk: 12 reads of the dropped vtable slots and
    20 of the dropped `ErrorImpl` fields, in bodies that compiled.

    The slot-count condition is what separates an EMPTIED layout from a merely
    re-shaped one, and it is not optional. A re-trigger reverts every fn that
    references the component precisely so those fns can be re-written against
    the new shape — so "a filled body reads this member" is the normal state of
    affairs for any genuine redesign, and vetoing on it alone would refuse
    almost every one, turning the crate's escape hatch into a no-op. Rewriting
    `struct ErrorImpl { vtable: u8 }` as `struct ErrorImpl { msg: String }`
    trades one slot for one and is exactly the redesign this mechanism exists to
    obtain. Reducing seven fn pointers to one is not a trade.

    A re-SPELLING is not a reduction either, which is why the count comes from
    `declared_member_count`: `struct Error { ptr: Own<ErrorImpl> }` written as
    `struct Error(NonNull<ErrorImpl>)` loses the name 34 bodies read while
    keeping the storage, and those bodies can say `.0`."""
    if (declared_member_count(authored_def)
            >= declared_member_count(current_def)):
        return {}
    gone = declared_members(current_def) - declared_members(authored_def)
    return {n: c for n, c in field_reads(filled_bodies, gone).items() if c}
