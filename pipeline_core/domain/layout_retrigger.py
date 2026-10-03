"""When to re-run t1.5 layout authoring for a cluster — pure domain.

The frozen skeleton's layout gets ONE authoring pass at bake time. Several
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

import hashlib
from collections.abc import Collection, Iterable, Mapping
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


# Failure reasons that describe the splice/locate infrastructure rather than
# anything the model wrote. Three readers now share the one definition: the
# attempt refund and the rejection-checklist filter in `retrigger_layout`, and
# the authoring retry loop in `author_layout` — which is why this lives in the
# domain rather than beside its first caller (`retrigger_layout` imports
# `author_layout`, so the dependency cannot run the other way).
MECHANICAL_MARKERS = ("could not locate a frozen definition",
                      "skeleton file unreadable",
                      "authored blocks missing uid")


def is_mechanical_reason(reason: str) -> bool:
    """Whether a failure reason is ATTEMPT-INVARIANT — the same call would fail
    the same way however the model answered.

    Not a style preference: on run `669347-cf33eae3` (2026-08-15) the component
    `('CastTo',)` logged `could not locate a frozen definition to replace:
    CastTo in ptr.rs` on attempts 1, 2 AND 3, and `('Mut', 'error_mut')` did the
    same — six authoring calls, ~35 minutes, against a definition that is not in
    the file to be replaced. The reason names the store, not the answer, so a
    re-prompt buys a re-run of the lookup."""
    return any(m in reason for m in MECHANICAL_MARKERS)


MECHANICAL_KEY_PREFIX = "mech:"
INFLIGHT_KEY_PREFIX = "inflight:"
INFLIGHT_PID_KEY_PREFIX = "inflightpid:"
MECH_BUILD_KEY_PREFIX = "mechbuild:"
PAYOFF_KEY_PREFIX = "payoff:"
REJECTED_KEY_PREFIX = "rejected:"
REJ_BUILD_KEY_PREFIX = "rejbuild:"
CRASH_SIG_KEY_PREFIX = "sig:"
# The vanished-test SCOPE a refutation was recorded against: one row per test
# name, so the whole thing fits an int-valued store. See
# `crash_component_refutation` for why a refutation has to carry a scope at all.
CRASH_SCOPE_KEY_PREFIX = "sigv:"
# Authoring ROUNDS spent on one crash signature by one component, landed or
# not. Distinct from the destruction cap, which counts only what landed.
CRASH_ROUND_KEY_PREFIX = "sigr:"
CONTRACT_KEY_PREFIX = "contract:"
DEFAULT_MAX_MECHANICAL = 2
DEFAULT_MAX_REJECTIONS = 3
# Authoring rounds one crash signature may buy from one component. Matches the
# destruction cap deliberately: a round that lands and a round that is rejected
# cost the same GPU (~55 min and ~14 model calls, measured on run
# `1920662-40f711b2`'s iteration-2 fire), and the budget here is about the
# spend, not about the damage.
DEFAULT_MAX_CRASH_ROUNDS = 2
# The authoring prompt's CONSTRAINT SET, as an epoch. Bumped whenever a
# constraint the accept path enforces gains (or loses) an up-front statement in
# the prompt — a rejection streak earned before the model was ever told a rule
# is not evidence about a prompt that now states it. Same principle as
# `RetroProvenance`: a verdict earned in another context cannot convict in this
# one.
#   1 — pre-2026-08-14: member eviction was enforced by the accept path only,
#       and reached the model as a past-violation checklist entry or not at all.
#   2 — `members_in_use_text`: the members the filled bodies read are stated as
#       a requirement of the task, beside the member paths the tests read.
#   3 — `derive_requirements_text` states the derive list as a LIMIT as well as
#       a minimum, and says why an invented derive fails the scaffold (E0119
#       against a hand-written impl, E0277/E0369 against the fields' bounds).
#       Two of the three rejections on file for the 34-name `Error` component
#       were exactly that, under a prompt whose closing line was "Add any
#       others the type can support".
REJECTION_CONTRACT_EPOCH = 3

# Attempts-store keys for the CLUSTER-wide payoff budget (below). Prefixed on
# the same rule as `mechanical_key`: a component key is Rust idents joined by
# `|` and can never start with `payoff:`.
PAYOFF_HIGH_KEY = f"{PAYOFF_KEY_PREFIX}high"      # best pass_count seen so far
PAYOFF_PENDING_KEY = f"{PAYOFF_KEY_PREFIX}pending"  # a fire awaiting its verdict
PAYOFF_ZERO_KEY = f"{PAYOFF_KEY_PREFIX}zero"      # consecutive fires that bought nothing
DEFAULT_MAX_ZERO_PAYOFF = 2

# Cause C's own settlement currency (below). A crash fire is settled by
# SIGNATURE REPRODUCTION, never by the pass_count the plateau budget reads: a
# landed re-author opens a per-signature pending claim, and the next cause-C
# classification settles it — the same signature back is a zero-payoff fire, a
# changed signature is new evidence and clears the streak.
PAYOFF_CRASH_PENDING_PREFIX = f"{PAYOFF_KEY_PREFIX}crashsig:"
PAYOFF_CRASH_LAST_PREFIX = f"{PAYOFF_KEY_PREFIX}crashlast:"
PAYOFF_CRASH_ZERO_KEY = f"{PAYOFF_KEY_PREFIX}crash_zero"


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


def inflight_pid_key(component_key: str) -> str:
    """Attempts-store key recording WHICH PROCESS opened the in-flight attempt.

    Written beside `inflight_key` and cleared with it, so a marker that outlived
    its process names the process to ask about. Without it the only available
    match is "the most recently ended run", which is wrong as soon as a run dies
    before it reaches the retrigger at all. Same prefixing rule as
    `mechanical_key`."""
    return f"{INFLIGHT_PID_KEY_PREFIX}{component_key}"


def mechanical_build_key(component_key: str) -> str:
    """Attempts-store key recording WHICH BUILD of the pipeline earned a
    component's mechanical streak. Same prefixing rule as `mechanical_key`."""
    return f"{MECH_BUILD_KEY_PREFIX}{component_key}"


def code_build_stamp(code_id: str | None) -> int:
    """`code_identity`'s 12-hex-char digest as the int the attempts store holds
    (the file is `dict[str, int]`). 0 means "no build known", which is also what
    an absent row reads as — a legacy streak, earned under an unknown build."""
    if not code_id:
        return 0
    try:
        return int(code_id[:12], 16)
    except ValueError:
        return 0


def mechanical_streak_is_stale(
    attempts: Mapping[str, int], component_key: str, code_stamp: int,
) -> bool:
    """Whether the stored mechanical streak was earned by a DIFFERENT build of
    the pipeline than the one asking.

    A mechanical failure is a claim about the code: "this component's authoring
    call does not reach an outcome under this build". Edit the pipeline and the
    claim is untested again — the same principle as `rejection_streak_is_stale`,
    which asks "does the model keep violating a contract it has now been told".

    This exists because the streak can otherwise never be lowered. The only
    clear is `attempts.pop(mechanical_key(...))` on the ACCEPTED-re-author path,
    which sits downstream of the refusal, so `mech: 2` refuses every future run
    forever — the "a gate governing a RETRY becomes a permanent lock" shape.
    Measured on `anyhow` 2026-08-15: runs `462635-c8369188` (SIGTERM at 56m56s)
    and `496645-9b9de7e1` (SIGTERM at 24s) were both stopped by the operator
    while the 34-name construct/vtable component was mid-authoring; each charged
    one mechanical failure through `reconcile_interrupted_attempts`, and the
    next run (`500301-6a66bf86`) localized the crash to an in-crate origin,
    graded that component OWNS, and refused it at the cap with no model attempt
    having failed at all.

    An absent stamp reads as build 0 — legacy, earned under an unknown build —
    and is therefore stale, which is what releases a store poisoned before this
    row existed. A caller that passes `code_stamp=0` (build unknown) opts out:
    0 == 0 keeps the streak, so an unstamped store still refuses under an
    unstamped reader."""
    return attempts.get(mechanical_build_key(component_key), 0) != code_stamp


def charge_mechanical_failure(
    attempts: dict[str, int], component_key: str, code_stamp: int = 0,
) -> dict[str, int]:
    """Book one mechanical failure against `component_key`, stamped with the
    build that earned it. A streak from another build is REPLACED, not extended:
    the earlier failures were a claim about code that no longer runs."""
    if mechanical_streak_is_stale(attempts, component_key, code_stamp) \
            and attempts.get(mechanical_key(component_key), 0):
        attempts[mechanical_key(component_key)] = 0
    attempts[mechanical_key(component_key)] = (
        attempts.get(mechanical_key(component_key), 0) + 1)
    # Build 0 is "unknown", which is what an absent row already reads as — so an
    # unstamped caller leaves the store exactly as it found it rather than
    # writing a row that asserts nothing.
    if code_stamp:
        attempts[mechanical_build_key(component_key)] = code_stamp
    else:
        attempts.pop(mechanical_build_key(component_key), None)
    return attempts


# Every prefix the attempts store uses for a row that is NOT a component's
# destruction counter. One definition, because two readers already disagreed:
# `tools/reset_retrigger_attempts.py` filtered on `mech:`/`inflight:` alone and
# would have matched a `mechbuild:`/`rejected:`/`sig:` row as a component.
BOOKKEEPING_KEY_PREFIXES = (
    MECHANICAL_KEY_PREFIX, INFLIGHT_KEY_PREFIX, INFLIGHT_PID_KEY_PREFIX,
    MECH_BUILD_KEY_PREFIX, PAYOFF_KEY_PREFIX, REJECTED_KEY_PREFIX,
    REJ_BUILD_KEY_PREFIX, CRASH_SIG_KEY_PREFIX, CRASH_SCOPE_KEY_PREFIX,
    CRASH_ROUND_KEY_PREFIX, CONTRACT_KEY_PREFIX)


def _is_bookkeeping_key(key: str) -> bool:
    return key.startswith(BOOKKEEPING_KEY_PREFIXES)


def contract_key(component_key: str) -> str:
    """Attempts-store key recording the prompt-constraint epoch a component's
    rejection streak was earned under. Same prefixing rule as
    `mechanical_key`."""
    return f"{CONTRACT_KEY_PREFIX}{component_key}"


def rejection_build_key(component_key: str) -> str:
    """Attempts-store key recording WHICH BUILD of the pipeline earned a
    component's rejection streak. Same prefixing rule as `mechanical_key`."""
    return f"{REJ_BUILD_KEY_PREFIX}{component_key}"


def rejection_streak_is_stale(
    attempts: Mapping[str, int], component_key: str, code_stamp: int = 0,
) -> bool:
    """Whether the stored rejection streak predates the current constraint set
    OR was earned by a different build of the pipeline.

    A streak is a claim about what the model does when it is TOLD the rules. On
    run `929616-ee20d540` (2026-08-14) the 34-name `Error` component stood at
    rejection 2/3, both for dropping `ptr`, earned under a prompt that stated
    the member-eviction rule nowhere except as one line in a list of past
    violations. Spending the third and last attempt under a prompt that now
    states it up front would retire the crashed family's only structural escape
    on evidence about a different prompt. An absent stamp reads as epoch 0 —
    legacy, earned in an unknown context — and is therefore also stale.

    The BUILD half is the same argument the epoch makes, applied to the half of
    the constraint set that is not in the prompt text: the accept path's own
    checks, which live in the pipeline and change with it. Its absence was a
    permanent lock of exactly the shape `mechanical_streak_is_stale` was
    written for, and the two sat one `if` apart. Measured on `anyhow`
    2026-08-16, run `1408670-3c074ece`: the 34-name component's store held
    `mech: 2` and `rejected: 3`, and the log printed both readings on the same
    sweep — `2 mechanical failure(s) … earned by a different build … NOT
    refusing this aim` immediately above `3 consecutive re-author(s) REJECTED
    … Not retriggering`. Nothing but an ACCEPTED re-author clears the rejection
    counter, and the refusal sits upstream of the only path that can accept
    one, so every future run refuses on a streak no future run can spend.

    A caller that passes `code_stamp=0` (build unknown) opts out of the build
    half exactly as the mechanical rule does: 0 == 0 keeps the streak."""
    return (attempts.get(contract_key(component_key), 0)
            != REJECTION_CONTRACT_EPOCH
            or attempts.get(rejection_build_key(component_key), 0)
            != code_stamp)


def rejected_key(component_key: str) -> str:
    """Attempts-store key counting a component's CONSECUTIVE rejected
    re-authors — authored output the accept path refused (a frozen public
    declaration changed, a field widened to `pub`, dropped members bodies
    still read, or rustc). Same prefixing rule as `mechanical_key`."""
    return f"{REJECTED_KEY_PREFIX}{component_key}"


def rejection_budget_spent(
    attempts: Mapping[str, int], component_key: str,
    cap: int = DEFAULT_MAX_REJECTIONS, *, code_stamp: int = 0,
) -> bool:
    """Whether this component's last `cap` re-authors were all REJECTED.

    The destruction cap (`attempts[key]`) counts LANDED re-authors — each one
    re-stubs dozens of bodies, which is what the cap of 2 exists to bound. A
    rejected re-author destroys nothing, but charging it there anyway is how
    the 34-name `Error` component reached its cap on two t1.5 rejections
    (2026-08-09/10: `struct ErrorInner` for `pub struct Error`, then a `pub`
    field) with no re-author ever applied — permanently closing the only
    structural escape for an ASAN-localized use-after-free in `object_drop`.

    Rejections still cannot be free: a deterministic crash re-fires every
    iteration and every fire costs authoring calls, so consecutive rejections
    get their own cap, cleared only by an ACCEPTED re-author. Reaching it is
    attempt-invariant — RED at the moment it prints, not a countdown.

    A streak earned under an older constraint set, or by a different build of
    the pipeline, does not count (`rejection_streak_is_stale`): this cap
    answers "does the model keep violating the contract after being told it",
    and neither a prompt that never told it nor an accept path that no longer
    runs can answer that."""
    if rejection_streak_is_stale(attempts, component_key, code_stamp):
        return False
    return attempts.get(rejected_key(component_key), 0) >= cap


def charge_rejection(
    attempts: dict[str, int], component_key: str, code_stamp: int = 0,
) -> int:
    """Book one REJECTED re-author against `component_key`, stamped with the
    build and the constraint epoch that earned it, and return how many earlier
    rejections were retired as stale.

    A stale streak is REPLACED, not extended — the earlier rejections were a
    claim about a prompt or an accept path that no longer runs, and continuing
    the count would read the first rejection under the current rules as the
    third. The sibling of `charge_mechanical_failure`, and the write it pairs
    with `rejection_budget_spent`'s read: stamping only on the read side is how
    a streak becomes unspendable."""
    retired = 0
    if rejection_streak_is_stale(attempts, component_key, code_stamp):
        retired = attempts.pop(rejected_key(component_key), 0)
    attempts[contract_key(component_key)] = REJECTION_CONTRACT_EPOCH
    # Build 0 is "unknown", which is what an absent row already reads as, so an
    # unstamped caller leaves the store as it found it rather than writing a
    # row that asserts nothing — `charge_mechanical_failure`'s rule.
    if code_stamp:
        attempts[rejection_build_key(component_key)] = code_stamp
    else:
        attempts.pop(rejection_build_key(component_key), None)
    attempts[rejected_key(component_key)] = (
        attempts.get(rejected_key(component_key), 0) + 1)
    return retired


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


def crash_signature(evidence: CrashEvidence) -> str:
    """Stable fingerprint of a crash, for cross-fire memory.

    Two cause-C fires are aimed at THE SAME crash when the same expected tests
    vanished and the localizer blames the same fns (for the recorded cycle,
    run `58819-ee78a1d1` 2026-08-13: `test_large_alignment` +
    `test_no_backtrace_fallback` vanishing from `test_downcast`, blame empty,
    at iterations 3 and 5 identically). The blamed set is part of the
    signature on purpose: a localizer that finally produces an origin-path
    blame CHANGES the signature, which re-opens the component walk
    legitimately — new evidence is exactly what a repeat fire is supposed to
    require. `consecutive_sweeps` and `suspect_fns` are excluded: the first is
    a counter, the second is static reachability that can widen as fills land
    without the crash itself changing.

    A chain-derived blame contributes a MARKER, never its fn list. The list
    would flap: it includes producers, and producers are matched against live
    body text (`constructs_type`), so every landed fill could change the set
    and re-open the walk on a crash that never changed — an unbounded
    destruction license. The marker gives exactly the two transitions the
    walk's own exit line promises: empty → chain when the synthesis first
    lands (the unlock — refutations and the zero-payoff streak were booked
    under the old signature), and chain → frames if a real blame ever
    arrives.

    ONE-TIME MIGRATION, 2026-08-15. The hashing rule below never changed;
    what the TDD driver PUTS in `blamed_fns` did. It used to hand the crash
    evidence keys minus the chain keys — that is every frame of every site
    (`crash_evidence_by_locator` keys by access frames too) plus the
    unlocalizable branch's synthesized subject fns — and now hands
    `blamed_locators`, the origin blame alone. So every cause-C signature
    moves once: frame-derived from an all-frames list to origin-only,
    frame-blind from a frame residue to empty (the `;c=chain` marker
    unchanged), subject-synthesis from a subject list to empty. Stores
    written before that edit therefore re-key, and the exposure was measured
    on the live one (`_layout_retrigger_attempts_stable.json`, cluster
    `stable`, read 2026-08-15): four `sig:` refutation rows are orphaned, of
    which `StdError`'s two are moot (that component sits at attempts 2 of a
    cap of 2) and `Buf`/`Indented` are at 1 of 2 — at most one extra fire
    each, and only if their old crash recurs. One `payoff:crashlast:` row
    re-keys, restarting the crash zero-payoff streak from 0 of
    DEFAULT_MAX_ZERO_PAYOFF. That is the sanctioned "new evidence changes the
    signature" transition rather than an accident: what the signature reads
    genuinely changed, and it changed because the old reading was the
    crashing test's own frames."""
    vanished = ",".join(sorted(evidence.vanished))
    blamed = ",".join(sorted(
        f"{b.file}::{b.fn_name}" for b in evidence.blamed_fns))
    raw = f"v={vanished};b={blamed}"
    if evidence.chain_blamed:
        raw += ";c=chain"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def crash_blame_signature(evidence: CrashEvidence) -> str:
    """`crash_signature` with the VANISHED SET LEFT OUT — the half of the
    fingerprint that says *what the localizer knows*, not *how much damage is
    currently visible*.

    Same construction, same marker, one field short, so the two move together
    if the hashing rule ever changes. This is the key a refutation's scope is
    filed under: two sweeps share it exactly when the blame and the marker are
    the same, which is precisely the condition under which the vanished sets
    are comparable at all."""
    blamed = ",".join(sorted(
        f"{b.file}::{b.fn_name}" for b in evidence.blamed_fns))
    raw = f"b={blamed}"
    if evidence.chain_blamed:
        raw += ";c=chain"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def crash_sig_key(sig: str, component_key: str) -> str:
    """Attempts-store key counting LANDED cause-C re-authors of one component
    against one crash signature. Same prefixing rule as `mechanical_key`."""
    return f"{CRASH_SIG_KEY_PREFIX}{sig}:{component_key}"


def crash_scope_key(blame_sig: str, component_key: str, test: str) -> str:
    """One row per vanished TEST NAME in a refutation's scope.

    A row per name rather than a serialized set because the attempts store is
    `Mapping[str, int]` and has to stay one: it is read by
    `tools/reset_retrigger_attempts.py`, by the budget predicates here, and by
    `reconcile_interrupted_attempts`, none of which should learn a second value
    shape to carry one field. Membership is the only query the subset rule
    makes, and a row per name answers it directly."""
    return f"{CRASH_SCOPE_KEY_PREFIX}{blame_sig}:{component_key}:{test}"


def crash_round_key(sig: str, component_key: str) -> str:
    """Attempts-store key counting AUTHORING ROUNDS one component has spent on
    one crash, landed or rejected. Same prefixing rule as `mechanical_key`.

    Callers pass the BLAME signature (`crash_blame_signature`), never the
    vanished-inclusive `crash_signature`: the loop shrinks the vanished set by
    working, and a budget whose key its debtor moves by making progress is
    re-bought once per distinct subset. The blame moves only when the localizer
    learns something new, which is the one event that honestly deserves a fresh
    budget."""
    return f"{CRASH_ROUND_KEY_PREFIX}{sig}:{component_key}"


@dataclass(frozen=True, slots=True)
class CrashRefutation:
    """Why (or whether) a component is refused for the crash in front of it.

    A bool would do for the decision and would not do for the LOG, which is the
    half that was missing: the silent version of the subset case is what cost
    run `1920662-40f711b2` its night, and a reader has to be able to see "this
    is the same crash minus two tests we fixed" without re-deriving it."""

    refuted: bool
    #: The vanished set the refusal was recorded against (V).
    scope: frozenset[str] = frozenset()
    #: V minus the crash in front of us — what the loop has repaired since.
    fixed: frozenset[str] = frozenset()
    #: True when the hash-exact row matched, i.e. an identical crash.
    exact: bool = False
    #: A refutation with no scope on file. The caller should write the current
    #: vanished set as the scope so the NEXT shrink is covered.
    needs_scope: bool = False


def crash_refutation_scope(
    attempts: Mapping[str, int], blame_sig: str, component_key: str,
) -> frozenset[str]:
    """The vanished test names a refutation for this (blame, component) was
    recorded against."""
    prefix = f"{CRASH_SCOPE_KEY_PREFIX}{blame_sig}:{component_key}:"
    return frozenset(k[len(prefix):] for k, v in attempts.items()
                     if v and k.startswith(prefix) and len(k) > len(prefix))


def crash_component_refutation(
    attempts: Mapping[str, int], sig: str, component_key: str, *,
    blame_sig: str | None = None,
    vanished: Collection[str] = (),
) -> CrashRefutation:
    """Whether this component's re-author was already spent on THIS crash and
    the crash survived it.

    Counts LANDED fires only. A REJECTED or REFUNDED fire changed nothing in
    the tree, so it proves nothing about the crash and must not immunize its
    component: run `58819-ee78a1d1` iter 1 fired on the 33-name mega component
    and had all three authorings rejected (dropped `ptr`, 25 live reads) —
    treating that as a refutation would have retired the one component that
    plausibly OWNS the drop-glue fault without a single landed test of it.

    The refutation this DOES record is the recorded limit cycle: `Quoted`
    landed attempt 1 at iter 3, the same 3 bodies re-filled, and the identical
    crash reproduced at iter 5 — at which point the walk re-picked `Quoted` on
    registry order, because nothing remembered that the crash had already
    survived its re-author. Iterations 3-6 oscillated 79/74/79/74 for it.

    THE MATCH IS SUBSET-AWARE, AND A HASH-EXACT MATCH ALONE COST A NIGHT.
    `crash_signature` hashes the vanished set, so a refutation was keyed to the
    exact damage visible when it was recorded — and the loop's whole job is to
    reduce that damage. Run `1920662-40f711b2` (2026-08-16/17): at iteration 1
    the retrigger refused this component in as many words, `vanished` being
    `{test_large_alignment, test_no_backtrace_fallback}`. The promoted dispatch
    then filled `object_drop_front`, `tests/test_downcast.rs` went 0/7 to 4/3,
    pass 79 to 83, and `vanished` became `{test_no_backtrace_fallback}` — a
    STRICT SUBSET. Different set, different sha, no row, aim re-opened. It
    fired twice more; the second landed, reverted 54 bodies and took the
    cluster 83 to 25. Net for 9h48m and 191 model calls: −46 tests.

    This is the function's own docstring argument applied to the field it was
    not applied to. `crash_signature` already refuses to hash a chain-derived
    blame's fn list, on the grounds that "every landed fill could change the
    set and re-open the walk on a crash that never changed — an unbounded
    destruction license". `vanished` does that by a shorter route, and it is
    the worse field to key on, because the excluded ones flap in both
    directions while a shrinking vanished set is unambiguously the loop working.

    So: refuted for a crash whose vanished set is a SUBSET of the recorded
    scope and whose blame marker is unchanged — that is the same crash minus
    what got fixed. The aim re-opens on exactly the two things a repeat fire is
    supposed to require:

    * a vanished test OUTSIDE the scope — a binary going down that was not down
      before, which is new damage and new evidence;
    * a change of blame marker — `blame_sig` moves on the empty → chain →
      frames transitions `crash_signature`'s docstring already sanctions.

    Backward compatible in both directions. A store written before the scope
    existed has no rows, so `scope` is empty, the subset branch is inert and
    the behaviour is the old hash-exact one; `needs_scope` then tells the caller
    to backfill from the crash it is refusing right now, which is a valid scope
    by construction — the refusal being made is a refusal against that set."""
    if attempts.get(crash_sig_key(sig, component_key), 0) > 0:
        scope = (crash_refutation_scope(attempts, blame_sig, component_key)
                 if blame_sig else frozenset())
        return CrashRefutation(refuted=True, scope=scope or frozenset(vanished),
                               exact=True, needs_scope=not scope)
    if not blame_sig or not vanished:
        return CrashRefutation(refuted=False)
    scope = crash_refutation_scope(attempts, blame_sig, component_key)
    now = frozenset(vanished)
    if scope and now <= scope:
        return CrashRefutation(refuted=True, scope=scope, fixed=scope - now)
    return CrashRefutation(refuted=False, scope=scope)


def crash_component_refuted(
    attempts: Mapping[str, int], sig: str, component_key: str, *,
    blame_sig: str | None = None,
    vanished: Collection[str] = (),
) -> bool:
    """`crash_component_refutation`'s decision alone, for callers that only
    need the bool."""
    return crash_component_refutation(
        attempts, sig, component_key,
        blame_sig=blame_sig, vanished=vanished).refuted


def record_crash_scope(
    attempts: Mapping[str, int], blame_sig: str, component_key: str,
    vanished: Collection[str],
) -> dict[str, int]:
    """File the vanished set a refutation is answerable to. Idempotent, and
    additive on purpose: re-recording a LARGER set widens the scope, which is
    the honest direction — a crash that took more binaries down and was then
    re-authored against was refuted against all of them."""
    out = dict(attempts)
    for test in vanished:
        out[crash_scope_key(blame_sig, component_key, test)] = 1
    return out


def crash_rounds_spent(
    attempts: Mapping[str, int], sig: str, component_key: str,
) -> int:
    """How many authoring rounds this component has already spent on this exact
    crash signature — landed, rejected, it makes no difference to the GPU."""
    return attempts.get(crash_round_key(sig, component_key), 0)


def crash_round_budget_spent(
    attempts: Mapping[str, int], sig: str, component_key: str,
    cap: int = DEFAULT_MAX_CRASH_ROUNDS,
) -> bool:
    """Whether one crash signature has bought all the authoring rounds it may
    from one component.

    THE HOLE THIS CLOSES. A rejected re-author refutes nothing and destroys
    nothing, and both of those are correct — `crash_component_refutation`
    counts landed fires only, and the destruction cap counts what landed. The
    consequence nobody had priced is that a rejected round is therefore FREE:
    nothing anywhere recorded that a signature had already been taken to the
    model and come back with nothing. Run `1920662-40f711b2`, iteration 2: three
    authoring attempts against the 31-name `Error` component, all three rejected
    at the compile gate (`E0255 Backtrace defined multiple times`, `E0119
    conflicting Clone for Own<_>`, `E0255` again), 55 minutes and ~14 model
    calls, destruction cap correctly not charged — and then iteration 4 re-fired
    on the identical vanished set and that one landed, for −58 tests.

    Deliberately NOT a widening of the destruction cap. A rejected fire must
    still not count as a destruction for the ratchet's purposes; it must only
    stop being free."""
    return crash_rounds_spent(attempts, sig, component_key) >= max(cap, 1)


def charge_crash_round(
    attempts: Mapping[str, int], sig: str, component_key: str,
) -> dict[str, int]:
    """Book an authoring round against a crash signature.

    Charged on the OUTCOME, not at the open, and the asymmetry with the
    destruction cap beside it is deliberate. That cap is charged before the
    authoring call and refunded after, so a process killed mid-authoring cannot
    retry for free; this budget cannot use that shape, because the refund would
    have to survive the kill and `reconcile_interrupted_attempts` — which is
    what does the surviving — has no way to know WHICH crash signature the dead
    attempt was aimed at. Charging on the outcome gives the same rule with
    nothing to reconcile: a REJECTED round is charged (the model was asked and
    answered, and its answer failing the compile gate is exactly the spend this
    bounds), a REFUNDED one is not (no model evidence was bought), and a kill
    charges nothing — which `mechanical_budget_spent` already bounds at
    `DEFAULT_MAX_MECHANICAL` per component."""
    out = dict(attempts)
    key = crash_round_key(sig, component_key)
    out[key] = out.get(key, 0) + 1
    return out


def record_crash_fire(
    attempts: Mapping[str, int], sig: str, component_key: str, *,
    blame_sig: str | None = None,
    vanished: Collection[str] = (),
) -> dict[str, int]:
    """Book a LANDED cause-C fire: the component is refuted for this signature
    the moment the crash is seen again, and the fire's payoff claim is opened
    against signature reproduction (see `settle_crash_payoff`).

    The refutation's SCOPE is written in the same call, because a refutation
    without one degrades to the hash-exact match that the subset rule exists to
    replace — see `crash_component_refutation`."""
    out = dict(attempts)
    key = crash_sig_key(sig, component_key)
    out[key] = out.get(key, 0) + 1
    out[f"{PAYOFF_CRASH_PENDING_PREFIX}{sig}"] = 1
    if blame_sig and vanished:
        out = record_crash_scope(out, blame_sig, component_key, vanished)
    return out


def settle_crash_payoff(
    attempts: Mapping[str, int], sig: str,
) -> dict[str, int]:
    """Charge previous cause-C fires with what they bought, in crash survival.

    The plateau budget settles in pass_count and deliberately exempts cause C
    ("a cluster whose binaries do not build cannot move best_pass") — a
    rationale that is true of cause A and FALSE of cause C: 13 of 14 binaries
    kept reporting verdicts through the recorded cycle, so the crash channel
    needed its own settlement currency, and it has one — the crash itself. A
    pending claim whose signature is BACK settles as a zero-payoff fire; a
    changed signature clears the streak (the evidence moved, so the walk is no
    longer aimed by evidence that already failed).

    The streak reset keys on the LAST-SEEN signature, never on a pending
    claim: once the budget refuses the walk, no further fire opens a claim, so
    a pending-keyed reset could never clear the streak again — a changed crash
    would arrive to find the budget permanently spent by its predecessor. The
    `crashlast:` marker is what a refused walk still updates."""
    out = dict(attempts)
    last = [k for k in out if k.startswith(PAYOFF_CRASH_LAST_PREFIX)]
    changed = bool(last) and all(
        k[len(PAYOFF_CRASH_LAST_PREFIX):] != sig for k in last)
    for key in last:
        del out[key]
    out[f"{PAYOFF_CRASH_LAST_PREFIX}{sig}"] = 1
    for key in [k for k in out if k.startswith(PAYOFF_CRASH_PENDING_PREFIX)]:
        old_sig = key[len(PAYOFF_CRASH_PENDING_PREFIX):]
        del out[key]
        if old_sig == sig:
            out[PAYOFF_CRASH_ZERO_KEY] = out.get(PAYOFF_CRASH_ZERO_KEY, 0) + 1
    if changed:
        out.pop(PAYOFF_CRASH_ZERO_KEY, None)
    return out


def crash_payoff_budget_spent(
    attempts: Mapping[str, int], cap: int = DEFAULT_MAX_ZERO_PAYOFF,
) -> bool:
    """Whether this cluster's last `cap` LANDED cause-C re-authors each left
    the identical crash reproducing.

    Bounds the WALK across components on an unchanged signature — the
    per-signature refutation above already stops a repeat of the same
    component, but with 6 components in the recorded OWNS tier the walk could
    still land up to one destruction per candidate (~2.1h per cycle measured
    on run `58819-ee78a1d1`, and one of those candidates re-stubs every body
    spelling 33 names) before the tier emptied. Self-clearing: a changed
    signature resets the streak in `settle_crash_payoff`, so the budget only
    ever refuses aim by evidence that has already failed twice."""
    return attempts.get(PAYOFF_CRASH_ZERO_KEY, 0) >= cap


def reconcile_interrupted_attempts(
    attempts: Mapping[str, int], *,
    stopped_pids: Collection[int] = (), code_stamp: int = 0,
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
    would otherwise re-fire for ever on a budget nothing can exhaust.

    …EXCEPT when the process was stopped from outside. `stopped_pids` holds the
    pids the death record says ended on a deliberate stop (SIGTERM/SIGINT), and
    the pid that opened each attempt is on file beside its marker
    (`inflight_pid_key`). Such an attempt is refunded with NO mechanical charge:
    the bound above answers "this component's authoring call kills the process",
    and an operator restart is not evidence about the authoring call at all.
    Measured on `anyhow` 2026-08-15 — two operator SIGTERMs during the 34-name
    construct/vtable component's re-author (runs `462635-c8369188` and
    `496645-9b9de7e1`) took `mech:` to its cap of 2, and the next run refused a
    component it had just graded OWNS off an in-crate ASAN origin. A death with
    no record, or one killed by a signal an OOM also sends, keeps the charge:
    that is the case the bound was written for."""
    out = dict(attempts)
    refunded: list[str] = []
    for key in sorted(k for k in out if k.startswith(INFLIGHT_KEY_PREFIX)):
        component = key[len(INFLIGHT_KEY_PREFIX):]
        del out[key]
        opener = out.pop(inflight_pid_key(component), None)
        if not component or _is_bookkeeping_key(component):
            continue
        out[component] = max(0, out.get(component, 0) - 1)
        if opener is None or opener not in stopped_pids:
            charge_mechanical_failure(out, component, code_stamp)
        refunded.append(component)
    return out, tuple(refunded)


def mechanical_budget_spent(
    attempts: Mapping[str, int], component_key: str,
    cap: int = DEFAULT_MAX_MECHANICAL, code_stamp: int = 0,
) -> bool:
    """Whether this component has already failed mechanically `cap` times in a
    row without the model being at fault.

    A mechanical failure (splice/locate miss, a propagation that could not
    reach the live tree) REFUNDS the attempt, so a pipeline bug can never
    exhaust the budget meant for the model. The consequence is that the very
    same failure re-fires on the next sweep, and every re-fire spends a full
    layout-authoring model call on a bug no model output can change. This
    bounds the refund: unbounded retries of an attempt-invariant failure is the
    burn the refund was supposed to prevent, aimed at a different budget.

    A streak earned under another BUILD does not count
    (`mechanical_streak_is_stale`): this cap answers "does this code reach an
    outcome here", and code that has since been edited has not answered it."""
    if mechanical_streak_is_stale(attempts, component_key, code_stamp):
        return False
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

    `blamed_fns` is a SECOND, stronger channel: the ORIGIN blame locators —
    where the localizer placed the fault itself (`origin_blamed_locators`:
    one locator per site, gdb's faulting frame or ASAN's use-after-free
    origin). Not the family union `blamed_locators` builds for dispatch.
    Not the crash EVIDENCE keys, which are a different set for a different
    purpose: `crash_evidence_by_locator` keys by every frame so that callers
    up the stack receive the report too, and a set built that way names the
    innocent router of execution — or, when the origin is unattributed and
    the only frames are the crashed test's own, names nobody in the crate at
    all (run `263217-d41a5bb5`, 2026-08-15). The two are different grades of
    evidence and must not be merged into one set — `suspect_fns` says a
    vanished test *could* have called this fn, `blamed_fns` says the fault
    *was observed* there. Only the second can separate two components that
    the same vanished test reaches, and that is precisely what cause C could
    not do while it aimed by record order.

    `chain_blamed` is a THIRD channel: the construction chain of the crashed
    tests (attributed fns plus the producers of their types), synthesized by
    the TDD driver only when the crash is deterministic AND an origin-capable
    detector looked and placed no frame in-crate — the refcount-corruption
    class where every stack resolves to std/test code (run
    `635043-5f7c61f5`, 2026-08-14). The two blame channels never co-exist in
    one payload — the synthesis refuses to run while a real frame exists.

    Only `blamed_fns` AIMS the walk. `chain_blamed` and `suspect_fns` are
    inferences and grade nothing (`crash_candidate_grade`); what the chain
    contributes here is the signature marker (`crash_signature`) — the
    transition that re-opens a walk closed under a blameless signature when
    a real frame later arrives — and the evidence-shape line of a declined
    walk. Its prompt/frontier/exhaustion roles live in the driver."""
    vanished: frozenset[str]
    suspect_fns: frozenset[FnLocator]
    consecutive_sweeps: int = 0
    blamed_fns: frozenset[FnLocator] = frozenset()
    chain_blamed: frozenset[FnLocator] = frozenset()


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


class BlameGrade(Enum):
    """How strongly an observed fault implicates ONE component. Ordered."""
    NONE = 0
    REFERENCES = 1   # a component name occurs in the faulting fn's own text
    OWNS = 2         # the faulting fn IS one of the component's items, or is a
    #                  method whose SELF TYPE is one of them

    def __lt__(self, other: BlameGrade) -> bool:
        return self.value < other.value


def self_type_names(container: str) -> frozenset[str]:
    """The identifier tokens naming the type a method BELONGS to, given an
    `FnRecord.container` (`""` | `"impl X for Y"` | `"impl X"` | `"trait T"`).

    Only the self type counts, never the trait: `impl StdError for ContextError`
    declares a method OF `ContextError` that merely satisfies `StdError`. Reading
    both is what let a use-after-free in `src/error.rs` implicate `context.rs`'s
    private `ext::StdError` trait — two symbols that share a token and nothing
    else. Generic arguments are kept (`ContextError<C, E>` yields `ContextError`,
    `C`, `E`); a stray type parameter can only ever widen ownership to a
    component that declares a one-letter item, and none do."""
    body = container.strip()
    if body.startswith("trait "):
        body = body[len("trait "):]
    elif body.startswith("impl"):
        body = body[len("impl"):]
        # `for` is a keyword here, so the LAST one separates trait from self
        # type; a lifetime/bound elsewhere cannot introduce another.
        marker = body.rfind(" for ")
        body = body[marker + len(" for "):] if marker >= 0 else body
    else:
        return frozenset()
    out: set[str] = set()
    token = ""
    for ch in body:
        if ch.isalnum() or ch == "_":
            token += ch
        else:
            if token:
                out.add(token)
            token = ""
    if token:
        out.add(token)
    return frozenset(out)


def crash_blame_grade(
    locators: frozenset[FnLocator],
    fn_records: list[FnRecord],
    component_names: frozenset[str],
) -> BlameGrade:
    """How strongly the faulting fns implicate THIS component.

    The two grades are different claims and the caller must be able to rank
    them. OWNS says the fault was observed inside code this component declares —
    its own free fn, or a method of its own type. REFERENCES says only that the
    faulting fn's text contains one of the component's names, which for a
    reconstruction mid-flight is true of most of the crate: `StdError` occurs in
    the signature of nearly every fn in `src/error.rs`, and the component of
    that name is a five-line private helper trait in `src/context.rs` that no
    one of them can even see."""
    if any(fn.fn_name in component_names for fn in locators):
        return BlameGrade.OWNS
    by_locator = {FnLocator(f.file, f.fn_name): f for f in fn_records}
    for loc in locators:
        rec = by_locator.get(loc)
        if rec is not None and self_type_names(rec.container) & component_names:
            return BlameGrade.OWNS
    if set(find_fns_referencing(fn_records, component_names)) & locators:
        return BlameGrade.REFERENCES
    return BlameGrade.NONE


def classify_crash_retrigger(
    evidence: CrashEvidence,
    fn_records: list[FnRecord],
    component_names: frozenset[str],
) -> LayoutRetriggerCause:
    """Cause C decision for ONE frozen component. RUNTIME_CRASH iff the crash
    implicates this component at any grade (`crash_blame_grade`). No
    compile-error text is required: the defining feature of the crash class is a
    clean compile.

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
    native fault, and then NOTHING is admitted: the reachability rule this
    classifier started as, and the chain-derived rule that stood between
    them from 2026-08-14 to 2026-09-06, both aimed a re-author by inference,
    and every fire either bought was at a component the fault never touched
    (`crash_candidate_grade` carries the measurements). A frame-blind crash
    is worked by the fill loop, which receives the chain as prompt evidence.

    Admitting a component is NOT choosing it. This still returns RUNTIME_CRASH
    for every component the fault so much as mentions, and the caller walks them
    in registry order — see `crash_candidate_grade` and `top_blame_tier` for the
    rank that decides between them.

    Admission is also not the fire. A frame-derived blame whose named body
    is already filled is a fill, not a layout re-author:
    `crash_blame_is_filled_body` sits in `retrigger_layout` before apply
    and revert. Changing only this classifier would not stop the fire —
    the action hardcodes `cause = RUNTIME_CRASH` after grading."""
    return (LayoutRetriggerCause.NONE
            if crash_candidate_grade(evidence, fn_records, component_names)
            is BlameGrade.NONE
            else LayoutRetriggerCause.RUNTIME_CRASH)


def crash_blame_is_filled_body(
    evidence: CrashEvidence,
    fn_records: list[FnRecord],
) -> bool:
    """True when every origin-blamed locator has a filled (not stub) body.

    Cause C re-authors types. A use-after-free whose named body already
    compiles is a fill: the layout walk's first move is to revert that
    body. Live, run `1869206-7fd52754` blamed `into_boxed_dyn_error`
    (frame_derived, filled), graded OWNS, hardcoded RUNTIME_CRASH, and
    opened a re-author of Error — then died locating Mut in ptr.rs. The
    fill loop already had the blame as crash evidence.

    Empty `blamed_fns` is not this claim — chain / unattributed /
    reachability stay on the layout route. A locator with no record
    cannot be shown to be filled — False, layout route stays. A stub
    blamed body is False — layout route stays.
    """
    if not evidence.blamed_fns:
        return False
    by_loc = {FnLocator(r.file, r.fn_name): r for r in fn_records}
    for loc in evidence.blamed_fns:
        rec = by_loc.get(loc)
        if rec is None or rec.has_unimplemented_body:
            return False
        body = (rec.body_text or "").strip()
        if not body or body in ("unimplemented!()", "::core::unimplemented!()"):
            return False
    return True


def crash_candidate_grade(
    evidence: CrashEvidence,
    fn_records: list[FnRecord],
    component_names: frozenset[str],
) -> BlameGrade:
    """`classify_crash_retrigger`'s decision, kept as the GRADE it was made at.

    The caller needs the grade because cause C picks ONE component out of many
    and the registry order it walks carries no evidence at all. Measured on
    cluster `stable`, 2026-08-08T08:19Z: ASAN localized a heap-use-after-free to
    `src/error.rs::object_reallocate_boxed`, a declared member of the 34-name
    `Error|ErrorImpl|ErrorVTable|…|object_reallocate_boxed|vtable` component at
    registry index 13. The walk never reached index 13. Index 3 is `StdError` —
    a five-line private helper trait declared inside `mod ext` in
    `src/context.rs` — admitted because the blamed fn's signature returns
    `Box<dyn StdError + Send + Sync>`, where `StdError` is the crate's alias for
    `std::error::Error` and not that trait at all. It re-authored the unrelated
    trait and reverted 29 bodies that merely spell the token, taking the cluster
    from 49 passing to 24."""
    if not evidence.vanished:
        return BlameGrade.NONE
    # THE AIM READS OBSERVED FRAMES ONLY. `chain_blamed` and `suspect_fns`
    # are inferences — the construction chain of the crashed tests, and
    # what those tests statically reach — and an inference cannot say the
    # fault was observed INSIDE code a component declares, which is what
    # the OWNS tier means and what the walk spends a re-author on. Grading
    # an inference at the tier below is no answer either: every component
    # the inference merely mentions becomes a REFERENCES candidate, and the
    # walk takes the first in registry order — the pre-tier defect (five
    # sessions firing at `ChainState` and `Quoted` while every ASAN sweep
    # blamed `src/error.rs::downcast`). So without a frame nothing is
    # admitted, and the walk's declined line names the evidence shape.
    #
    # Measured, run `1577437-c93d5c26` iter 3 (2026-09-06, cluster
    # `stable`): `blamed_fns` empty, 63 chain-blamed fns. Grading the chain
    # put 8 of 16 components in the OWNS tier — `src/context.rs::fmt` and
    # `::write_str` are methods of `Quoted`, so `Quoted` was one — and
    # attempt 2 fired on `Quoted`: re-authored a type 0 of 373 enumerated
    # fns reference, reverted nothing, reset the stall budget, and left the
    # crash exactly as it was. Computed on the same records, the recorded
    # reverse-map suspects (34 fns) grade the same 8 components OWNS by the
    # same methods, so withholding the chain alone would have re-opened the
    # identical fire through reachability. Both channels keep every other
    # role — prompt evidence, exhaustion void, frontier entry, and for the
    # chain the signature marker (`crash_signature`) — none of which is an
    # aim.
    #
    # The residue case this function used to fall through on — run
    # `263217-d41a5bb5` (2026-08-15), the crashing test's own two
    # `gen_tests` frames handed as `blamed_fns`, grading NONE everywhere —
    # is closed at its cause: the driver hands `origin_blamed_locators`
    # (the origin blame, no family union), which for that class is empty.
    # An empty aim declines, and the
    # decline is loud (`retrigger_layout`'s declined-walk line prints the
    # tier and the evidence shape), which is the whole of what that
    # 308-minute silence needed.
    if not evidence.blamed_fns:
        return BlameGrade.NONE
    return crash_blame_grade(
        evidence.blamed_fns, fn_records, component_names)


def top_blame_tier(grades: Iterable[BlameGrade]) -> BlameGrade:
    """The strongest grade any candidate reached, NONE when none did.

    The policy the caller applies with it: only candidates AT this grade may be
    re-authored, and when every one of them is unavailable — capped, confounded,
    already failing mechanically — the crash walk STOPS rather than demoting to
    the tier below. "The fault is in code this component owns, and this component
    may not be re-authored again" is a complete answer; turning it into "so
    re-author something the fault only mentions" is how a spent budget on the
    right component became a spent attempt on the wrong one, twice in two days
    (81 → 21 on 08-07, 49 → 24 on 08-08)."""
    return max(grades, default=BlameGrade.NONE)


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
    so the pass count collapses and the oracle shrinks. Four consumers read those
    signals and all four are wrong during the window:

      * the batch ratchet convicts on "this sweep measures last iter's fills";
      * the crash retrigger convicts on "the oracle shrank for N sweeps";
      * the stall budget convicts on "N iters of fills moved no test";
      * the exhaustion re-admission gate demands a score move past a high-water
        the re-stub itself made unreachable — the one that does not merely
        mis-report but DEADLOCKS, and the one nobody thought of for six months.
        It is not gated here: a window is a bounded excuse, and that gate needs
        the high-water to stop being a lie at all. See `domain/pass_highwater.py`.

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
    # THE WINDOW'S SECOND AND THIRD DENOMINATORS. It is sized in ITERATIONS and
    # nothing bounds what an iteration costs: run `1920662-40f711b2` measured
    # 588 minutes for 6 iterations (98 min/iteration), so the 18 iterations its
    # 54-body re-stub bought are ~29 hours with the batch ratchet, the stall
    # budget and the crash streak all standing down. An exemption with one
    # denominator can only be as right as that denominator is.
    #
    # `max_minutes` is the wall-clock ceiling (0 disables it, which is what
    # every caller that does not measure time gets). `highwater_at_grant` is the
    # score the window was re-based FROM: once the re-climb reaches it the
    # re-fills have demonstrably finished paying, the ordinary gates describe
    # the tree again, and holding them down further protects nothing. -1 means
    # the granting site had no high-water to record.
    granted_at_minute: float = 0.0
    max_minutes: float = 0.0
    highwater_at_grant: int = -1
    # Whether any sweep inside this window has read BELOW `highwater_at_grant`.
    # A re-climb presupposes a drop, and without this the "reached the mark"
    # close fires on the window's very first sweep whenever the re-stub cost no
    # tests — which is the common case when the reverted bodies' tests were
    # already failing. Walk `3368-753757e6` is exactly that: 25 bodies reverted,
    # pass 39 before and 39 after, so an unqualified rule closed the window at
    # once and protected nothing.
    dipped: bool = False
    # The vanished-test reading of the sweep the fire acted on — a reading
    # taken on the PRE-revert tree, where every body the fire re-stubbed was
    # still filled. It bounds the window's explanatory power (see
    # `explains_vanish`): a crash recorded here cannot be a stub artifact of
    # this window, because it happened before the stubs existed.
    vanished_at_grant: frozenset[str] = frozenset()

    @classmethod
    def none(cls) -> RefillGrace:
        return cls(iters_left=0, restubbed=0, fills_at_grant=0)

    @classmethod
    def for_restub(
        cls, restubbed: int, fills_so_far: int,
        per_iter: int = DEFAULT_REFILL_PER_ITER,
        max_iters: int = DEFAULT_REFILL_GRACE_MAX_ITERS,
        granted_at: int = 0,
        vanished_at_grant: frozenset[str] = frozenset(),
        granted_at_minute: float = 0.0,
        max_minutes: float = 0.0,
        highwater_at_grant: int = -1,
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
                   clipped=sized > bound,
                   granted_at_minute=granted_at_minute,
                   max_minutes=max_minutes,
                   highwater_at_grant=highwater_at_grant,
                   vanished_at_grant=vanished_at_grant)

    @classmethod
    def for_failed_attempt(cls, fills_so_far: int,
                           granted_at: int = 0,
                           vanished_at_grant: frozenset[str] = frozenset(),
                           ) -> RefillGrace:
        """One iter, nothing re-stubbed — a fired attempt that did not land."""
        return cls(iters_left=1, restubbed=0, fills_at_grant=fills_so_far,
                   granted_at=granted_at,
                   vanished_at_grant=vanished_at_grant)

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

    def explains_vanish(self, vanished: Iterable[str]) -> bool:
        """Whether this sweep's vanished set could be the window's own doing.

        The window's suppression premise is that a stub panic inside the
        re-stubbed bodies aborts test binaries, so a shrink during the window
        proves nothing about the layout. That premise is checkable per test:
        a name already vanished at the moment the window was granted crashed
        on the pre-revert tree, where every re-stubbed body was still filled —
        a stub cannot explain it. Only NEW vanished names beyond the grant
        reading are within the window's explanatory power. On run
        `3368-753757e6` (2026-08-15) the same 3 binaries were `already crashed
        at baseline` on every graced sweep while the 25-body window suppressed
        the crash-evidence machinery for ceil(25/3)=9 iterations — ~20h of
        re-fill sweeps that two consecutive readings had already measured as
        zero-payoff (156 fills, pass 39 flat).

        An empty grant reading (a compile-broke fire, older callers) keeps
        the old behavior: every vanish is within the window's power."""
        return bool(set(vanished) - set(self.vanished_at_grant)) \
            or not self.vanished_at_grant

    def dipped_by(self, pass_count: int) -> bool:
        """Whether the window has seen the score below the mark it was re-based
        from, this sweep included. `pass_count` of -1 means "not measured" and
        can neither set nor clear it."""
        return self.dipped or (
            0 <= pass_count < self.highwater_at_grant)

    def early_close_reason(
        self, now_minutes: float = 0.0, pass_count: int = -1,
    ) -> str:
        """Why this window should close before its iteration budget runs out,
        or "" — the SECOND and THIRD denominators, stated as one rule so the
        driver's log line and `consume` cannot drift apart.

        The stand-down itself is right and stays: the drop was deliberate, so
        no gate may charge it. What was missing is that "deliberate" has an
        expiry, and an exemption denominated in iterations alone cannot express
        it. Two things end it earlier than the count:

        * THE RE-CLIMB REACHED THE MARK IT WAS RE-BASED FROM. At that point the
          re-fills have finished paying by the window's own definition of
          paying, and every gate it is holding down now describes the tree
          accurately again. This is the cheap one and it needs no configuration
          — but it requires a DIP first, because a re-climb presupposes a drop.
          Without that qualifier the clause fires on the window's first sweep
          whenever the re-stub cost no tests, which is what happens when the
          reverted bodies' tests were already failing: walk `3368-753757e6`
          reverted 25 bodies with pass 39 on both sides of the fire, and an
          unqualified rule closed a 9-iteration window immediately.
        * THE WALL CLOCK. The window is granted in iterations and nothing bounds
          what an iteration costs. Run `1920662-40f711b2` (2026-08-16/17):
          588 minutes for 6 iterations, so the 18 iterations its 54-body
          re-stub bought were ~29 hours of a run with the batch ratchet, the
          stall budget and the crash streak all disarmed. `max_minutes=0`
          disables this, which is what a caller with no clock gets."""
        if not self.active:
            return ""
        if (self.highwater_at_grant >= 0 and self.dipped_by(pass_count)
                and pass_count >= self.highwater_at_grant):
            return (f"the re-climb reached {pass_count}, the high-water this "
                    f"window was re-based from ({self.highwater_at_grant}) — "
                    "the re-fills have finished paying and the ordinary gates "
                    "describe this tree again")
        elapsed = now_minutes - self.granted_at_minute
        if self.max_minutes > 0 and elapsed >= self.max_minutes:
            return (f"the wall-clock ceiling — {elapsed:.0f} min since the "
                    f"window opened against a {self.max_minutes:.0f}-min cap, "
                    f"with {self.iters_left} iter(s) of the budget unspent. An "
                    "exemption counted in iterations cannot bound what an "
                    "iteration costs")
        return ""

    def consume(self, fills_so_far: int, now_minutes: float = 0.0,
                pass_count: int = -1) -> RefillGrace:
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
        would protect work that can no longer run.

        …and it closes on whichever of its three denominators comes first — see
        `early_close_reason` for the other two."""
        if not self.active or fills_so_far <= self.fills_at_grant:
            return RefillGrace.none()
        if self.early_close_reason(now_minutes, pass_count):
            return RefillGrace.none()
        return RefillGrace(iters_left=self.iters_left - 1,
                           restubbed=self.restubbed,
                           fills_at_grant=fills_so_far,
                           granted_at=self.granted_at,
                           clipped=self.clipped,
                           granted_at_minute=self.granted_at_minute,
                           max_minutes=self.max_minutes,
                           highwater_at_grant=self.highwater_at_grant,
                           dipped=self.dipped_by(pass_count),
                           vanished_at_grant=self.vanished_at_grant)


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
    # Records are per impl site and the revert is per locator (every
    # occurrence): collapse here, in first-seen order, so the caller's
    # count and the revert's input both speak in units.
    return list(dict.fromkeys(out))


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


def members_in_use(current_def: str, filled_bodies: str) -> dict[str, int]:
    """Members the CURRENT definition declares and the crate's filled bodies
    still read — name → read count. The PROMPT half of
    `dropped_members_in_use`, computed from the same two texts before the
    authoring call instead of from the reply after it."""
    live = declared_members(current_def)
    return {n: c for n, c in field_reads(filled_bodies, live).items() if c}


def members_in_use_text(by_item: Mapping[str, Mapping[str, int]]) -> str:
    """The in-use members as an authoring instruction, or "" when no filled
    body reads any of them (which is every t1.5 bake — nothing is filled yet,
    so this note costs the initial authoring nothing).

    WHY THIS EXISTS AS A PROMPT AND NOT ONLY AS A GATE. `dropped_members_in_use`
    can only REJECT, and a rejection costs a whole authoring attempt against a
    budget of three, after which `rejection_budget_spent` refuses the component
    for good. On run `929616-ee20d540` (2026-08-14) the 34-name `Error`
    component reached rejection 2/3 with `authored Error drops member(s) this
    crate's already-written bodies still read: `ptr` (29 reads)` — the same
    violation as rejection 1/3 (`ptr`, 25 reads), whose text was already in the
    persisted checklist the fire prepends to the prompt. A constraint appended
    as one of a dozen past violations was not enough; it is stated here as a
    requirement of the task instead, next to the member paths the tests read.

    WHAT IT DELIBERATELY DOES NOT SAY. Not "keep these members" — the veto is
    calibrated to slot COUNT, not to identity, because reverting the fns that
    read a component is the whole point of a re-author and "a filled body reads
    this member" is therefore true of every genuine redesign. Re-shaping and
    re-spelling stay open (`struct Error { ptr: Own<ErrorImpl> }` as
    `struct Error(NonNull<ErrorImpl>)` is a rename the bodies can follow via
    `.0`); what is refused is a replacement with fewer storage slots than the
    live bodies need. The note says exactly that, so a reply that satisfies it
    is not a reply that gave up on the redesign."""
    rows = [f"  - `{item}`: " + ", ".join(
                f"`{n}` ({c} read{'s' if c != 1 else ''})"
                for n, c in sorted(members.items(), key=lambda kv: (-kv[1], kv[0])))
            for item, members in sorted(by_item.items()) if members]
    if not rows:
        return ""
    return ("\n\nMEMBERS THIS CRATE'S ALREADY-WRITTEN BODIES READ — these "
            "bodies are on disk and they compile; they are the data the layout "
            "has to be able to hold:\n" + "\n".join(rows)
            + "\nYou are free to re-shape and to re-spell: a tuple struct whose "
            "bodies reach the same storage through `.0`, a different type for a "
            "slot, a different name. You are NOT free to emit a definition with "
            "FEWER storage slots than the one it replaces — a design with "
            "nowhere left to put this data is rejected before it is compiled, "
            "and the attempt is spent.\n")
