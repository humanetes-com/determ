"""Hourly run-health detectors (2026-07-10, after the poisoned-notes finding).

Each detector encodes one issue CLASS that a deep manual analysis found once
and must now be found automatically every hour (user directive). Pure logic:
tools/run_health.py feeds it the current segment's log lines + the FR notes
and prints the operator block. Findings are TYPED (starvation.py archetype) —
extend the enum + detector + tests when a new class shows up, don't eyeball.

Token scans only — no regex (repo rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport
from pipeline_core.domain.model_notes import is_bogus_regression_note

__all__ = [
    "HealthFinding",
    "HealthKind",
    "IterReading",
    "Severity",
    "analyze",
    "parse_iter_readings",
]


class Severity(StrEnum):
    """How loudly a finding asks to be read — and the symbol it renders as.

    The symbol lives here because it used to live in three places: the
    `{"info": "·", "warn": "⚠", "alert": "‼"}` literal was copy-pasted into
    tools/run_health.py, tools/run_trend.py and monitor/health_view.py. When
    `red` was added to this file for the two findings that mean "the loop is
    buying nothing" and "there is no evidence at all", none of the three copies
    learned about it, and every one of them raised `KeyError: 'red'` on the
    first red finding. Observed 2026-08-02 against the live anyhow run: the
    operator report printed two findings, then died on the third — a
    ZERO_PAYOFF_BURN that had been true for four consecutive runs. A report
    that crashes on its own most serious finding is worse than one that never
    ran, because the two findings it did print read as the whole story.

    `severity` is typed as this enum rather than `str` for the same reason
    `kind` is: a bare string let the producers' vocabulary and the renderers'
    vocabulary drift apart with nothing to notice. Adding a member here is now
    a type error at every site that must render it.
    """

    INFO = "info"
    WARN = "warn"
    ALERT = "alert"
    RED = "red"

    @property
    def symbol(self) -> str:
        return _SEVERITY_SYMBOL[self]


_SEVERITY_SYMBOL: dict[Severity, str] = {
    Severity.INFO: "·",
    Severity.WARN: "⚠",
    Severity.ALERT: "‼",
    Severity.RED: "🔴",
}


class HealthKind(StrEnum):
    POISONED_NOTES = "poisoned_notes"            # invalid feedback reaching prompts
    ATTRIBUTION_FUTILITY = "attribution_futility"  # same unit stopped identically N times
    ZERO_PAYOFF_BURN = "zero_payoff_burn"        # many attempts, no pass improvement
    FROZEN_COUNTER = "frozen_counter"            # stub counter can't move in this phase
    STALL_BUDGET = "stall_budget"                # decision distance to stall-out
    REPEATED_HEAL = "repeated_heal"              # pre-pass heal firing on the same file again
    SYSTEMIC_SIGNATURE = "systemic_signature"    # same failure shape across unrelated files
    FROZEN_ZERO_TREE = "frozen_zero_tree"        # pass=0 fail=0 for N iters — tree likely broken
    HEAL_SKELETON_LOSS = "heal_skeleton_loss"    # whole-file skeleton restore discarded fills
    ORACLE_DRIFT = "oracle_drift"                # denominator changed mid-run — counts not comparable
    INVENTORY_OVERCOUNT = "inventory_overcount"  # static oracle permanently too big — cluster can never complete
    CAPPED_RETRY_FUTILITY = "capped_retry_futility"  # budgeted mechanism failing in an attempt-invariant way
    CRASH_UNLOCALIZED = "crash_unlocalized"      # tests vanished but no crash site was produced
    CLUSTER_COMPILE_BROKEN = "cluster_compile_broken"  # test binaries never built — no fill route can fix it
    MEMBERSHIP_FILTER_EMPTIED = "membership_filter_emptied"  # cfg filter dropped 100% of the stubs on disk
    ROUTING_DROP = "routing_drop"                # model answered; the body matched no container and was discarded
    PLATEAU_ACROSS_RUNS = "plateau_across_runs"  # same score in N consecutive RUNS — invisible inside any one of them
    WORKSPACE_RESET = "workspace_reset"          # reconstruction was wiped; scores before it describe a tree that no longer exists
    NO_SEGMENT_EVIDENCE = "no_segment_evidence"  # nothing to analyse — every segment detector is silently vacuous


@dataclass(frozen=True, slots=True)
class HealthFinding:
    kind: HealthKind
    severity: Severity
    subject: str           # what it is about (unit, file, counter…)
    evidence: str          # one line of concrete evidence
    action: str            # what the operator/autopilot should do about it


@dataclass(frozen=True, slots=True)
class IterReading:
    iter_n: int
    pass_count: int
    fail_count: int
    stall: int
    stall_max: int
    oracle: int | None = None   # the denominator (tests reporting a verdict)


def _int_after(text: str, marker: str) -> int | None:
    i = text.find(marker)
    if i == -1:
        return None
    digits = ""
    for ch in text[i + len(marker):]:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else None


def _names(items: tuple[str, ...], cap: int = 6) -> str:
    """Render identities, not just a count — a bare number cannot distinguish a
    correct filter from a broken one. Capped so one finding stays one line."""
    head = ", ".join(items[:cap])
    return head if len(items) <= cap else f"{head}, +{len(items) - cap} more"


def parse_iter_readings(lines: list[str]) -> list[IterReading]:
    """`iter N: cargo test … pass=X fail=Y … stall=S/M` lines → readings."""
    out: list[IterReading] = []
    for ln in lines:
        if "cargo test rc=" not in ln or "pass=" not in ln:
            continue
        it = _int_after(ln, "iter ")
        p = _int_after(ln, "pass=")
        f = _int_after(ln, "fail=")
        s = _int_after(ln, "stall=")
        i = ln.find("stall=")
        m = _int_after(ln[i:], "/") if i != -1 else None
        if None in (it, p, f, s, m):
            continue
        oracle = _int_after(ln, "oracle=")   # may be absent on older lines
        out.append(IterReading(it, p, f, s, m, oracle))  # type: ignore[arg-type]
    return out


def count_fills_landed(lines: list[str]) -> int:
    """Successful body-fills that LANDED in the segment (per-fn `filled`,
    per-impl `FILLED`/`KEPT`). This is the real-time progress signal: pass= is
    only re-measured once per multi-hour TDD sweep, so a segment can splice N
    correct bodies while the pass reading stays frozen. Counting landed fills
    lets zero_payoff_burn tell a genuinely frozen tree from mid-sweep progress
    instead of crying wolf on every fill batch that hasn't reached the next
    sweep boundary yet."""
    n = 0
    for ln in lines:
        per_fn = "[phase-c/per-fn]" in ln and " filled " in ln
        per_impl = "[phase-c/impl]" in ln and ("FILLED " in ln or "KEPT " in ln)
        if per_fn or per_impl:
            n += 1
    return n


def _unit_of(ln: str, tag: str) -> str:
    """The unit id between a `] ` prefix and the `: <tag>` suffix."""
    end = ln.find(f": {tag}")
    if end == -1:
        return ""
    start = ln.rfind("] ", 0, end)
    return ln[start + 2:end].strip() if start != -1 else ln[:end].strip()


def analyze(
    segment_lines: list[str],
    notes: list[str],
    *,
    burn_min_attempts: int = 40,
    futile_min_stops: int = 2,
) -> list[HealthFinding]:
    findings: list[HealthFinding] = []

    # -- 0. is there anything to analyse at all? ----------------------------
    # Every check below reads `segment_lines`. On an empty segment they all
    # return nothing, which is indistinguishable from "nothing is wrong" —
    # the report prints a clean bill of health for a run it cannot see. This
    # happened for hours when the active log was renamed out from under the
    # writer: `segment lines=0` sat in the header while the live run appended
    # to an archive. An analyser with no input has to say so, loudly, because
    # every downstream "GOOD: the grep was empty" is then meaningless.
    if not segment_lines:
        findings.append(HealthFinding(
            HealthKind.NO_SEGMENT_EVIDENCE, Severity.RED, "run log",
            "0 segment line(s) — no evidence to analyse, so every "
            "segment-scoped detector below is vacuous, NOT green",
            "check the log the pipeline is actually writing to "
            "(`ls -l /proc/<pid>/fd/1`) before trusting any empty grep",
        ))

    # -- 1. poisoned model notes ------------------------------------------
    bogus = sum(1 for n in notes if is_bogus_regression_note(n))
    if bogus:
        findings.append(HealthFinding(
            HealthKind.POISONED_NOTES, Severity.WARN, "FR Model Notes",
            f"{bogus} provably-invalid [regression] note(s) on disk "
            "(non-dropping aggregate)",
            "reader filters them at prompt time; if this COUNT GROWS the "
            "writer guard regressed — investigate record_tdd_regression",
        ))

    # -- 2. trend + stall decision distance --------------------------------
    readings = parse_iter_readings(segment_lines)
    if readings:
        last = readings[-1]
        flat = 0
        for r in reversed(readings):
            if r.pass_count == last.pass_count:
                flat += 1
            else:
                break
        remaining = last.stall_max - last.stall
        sev = (Severity.ALERT if remaining <= 1
               else Severity.WARN if flat >= 2 else Severity.INFO)
        findings.append(HealthFinding(
            HealthKind.STALL_BUDGET, sev, "tdd driver",
            f"pass={last.pass_count} fail={last.fail_count}, flat for {flat} "
            f"reading(s), stall {last.stall}/{last.stall_max}",
            f"{remaining} non-improving iteration(s) left before the inner "
            "loop stalls out; at stall-out below the historical best, "
            "escalate along the call graph via a tree diff",
        ))

    # -- 3. zero-payoff burn ------------------------------------------------
    attempts = sum(1 for ln in segment_lines
                   if "U2 test failed" in ln or "compile failed" in ln)
    maximal = sum(1 for ln in segment_lines if "maximal last-resort" in ln)
    if readings and attempts >= burn_min_attempts:
        delta = readings[-1].pass_count - readings[0].pass_count
        landed = count_fills_landed(segment_lines)
        # Landing fills is NOT evidence of progress, and suppressing on it
        # hid the worst case this detector exists to catch: bodies splicing
        # in at full rate while the score never moves (observed: 410 fills
        # across four runs, pass delta 0 every time — the detector was
        # silent for all of them because `landed` was never 0). What the old
        # suppression was really guarding against is a delta read BEFORE the
        # sweep that would move it, so gate on that instead: a delta needs
        # two readings to exist at all. With ≥2 readings a flat score is a
        # measured fact, and fills landing into it makes the finding
        # stronger, not weaker.
        if delta <= 0 and len(readings) >= 2:
            what = (f"{landed} fill(s) LANDED and the score still did not move"
                    if landed else "no fills landed")
            findings.append(HealthFinding(
                HealthKind.ZERO_PAYOFF_BURN,
                Severity.RED if landed else Severity.WARN, "fill/re-fill loop",
                f"{attempts} failed attempt(s) incl. {maximal} maximal, "
                f"pass delta {delta:+d} across {len(readings)} sweep "
                f"reading(s), {what}",
                "the loop is buying nothing — when fills land into a flat "
                "score the bodies are not what the oracle is blocked on; "
                "find the one unit the failing tests are all downstream of "
                "before spending another sweep",
            ))

    # -- 3b. shared failure set across unrelated files ----------------------
    # A unit's U2 rejection names ITS OWN failing tests, so the SIZE of that
    # set is a property of the unit. When units in several unrelated files all
    # report the same size, they are being judged against one shared set — the
    # frontier is convicting them for failures they do not own, and no attempt
    # count can escape that. (Observed: `errs=8` on context.rs, error.rs,
    # kind.rs, lib.rs and ensure.rs at once — the 8 tests futility escalation
    # had unioned onto every producer in the crate.)
    by_errs: dict[int, set[str]] = {}
    for ln in segment_lines:
        if "U2 test failed" not in ln:
            continue
        n = _int_after(ln, "errs=")
        unit = _unit_of(ln, "U2 test failed")
        if n is None or n < 2 or not unit:
            continue
        by_errs.setdefault(n, set()).add(unit)
    for n, units in sorted(by_errs.items()):
        files = {u.split("::", 1)[0] for u in units}
        if len(files) >= 3 and len(units) >= 4:
            findings.append(HealthFinding(
                HealthKind.SYSTEMIC_SIGNATURE, Severity.ALERT, "U2 acceptance oracle",
                f"{len(units)} unit(s) across {len(files)} file(s) all rejected "
                f"with errs={n}: {', '.join(sorted(units)[:4])}",
                "unrelated units cannot own the same failing set — inspect what "
                "the frontier attributes to them (starvation_miner names the "
                "tests); a shared set means conviction, not evidence",
            ))

    # -- 4. attribution futility -------------------------------------------
    stops: dict[str, int] = {}
    for ln in segment_lines:
        if "identical errors after attempt" in ln and "stopping" in ln:
            unit = _unit_of(ln, "identical errors")
            if unit:
                stops[unit] = stops.get(unit, 0) + 1
    futile = sorted((u for u, c in stops.items() if c >= futile_min_stops),
                    key=lambda u: -stops[u])
    if futile:
        top = ", ".join(f"{u}×{stops[u]}" for u in futile[:4])
        findings.append(HealthFinding(
            HealthKind.ATTRIBUTION_FUTILITY, Severity.WARN, "attribution",
            f"{len(futile)} unit(s) stopped identically ≥{futile_min_stops}×: {top}",
            "every attributed fn is being rewritten to the same dead end — "
            "the defect likely sits in the CONSTRUCTION path of the type the "
            "test exercises — escalate along the call graph",
        ))

    # -- 5. frozen stub counter / phase -------------------------------------
    membership = None
    for ln in reversed(segment_lines):
        membership = ClusterMembershipReport.parse(ln)
        if membership is not None and not membership.frontier:
            break
        membership = None
    # A filter that drops its whole input is a contradiction, not a phase: the
    # stubs on disk and the cluster come from the same bake, so "nothing here
    # belongs to this cluster" means the filter is wrong, not the tree. Ranked
    # above the frozen counter because the counter reads healthy while it holds.
    if membership is not None and membership.emptied:
        findings.append(HealthFinding(
            HealthKind.MEMBERSHIP_FILTER_EMPTIED, Severity.ALERT, "cluster cfg filter",
            f"cluster {membership.cluster_name}: 0 of {membership.total} stub(s) "
            f"survived the cfg-set — dropped: {_names(membership.skipped)}",
            "no fill route can receive work; every 'behavioral re-fill' reading "
            "below this is about an empty frontier. Compare the filter's verdict "
            "to the extractor's own per-fn cfg list (tools/probe_stub_frontier.py) "
            "before interpreting any trend number",
        ))
    behavioral = sum(1 for ln in segment_lines if "U2 test failed" in ln)
    # Suppressed while the filter is emptied: "the counter is at its ceiling,
    # judge progress by pass=" is the wrong advice when the frontier is empty
    # for a bug — it is the exact reading that made 9.5h look like a phase.
    if (membership is not None and membership.skipped and behavioral
            and not membership.emptied):
        findings.append(HealthFinding(
            HealthKind.FROZEN_COUNTER, Severity.INFO, "stub-fill counter",
            f"{len(membership.skipped)} of {membership.total} stub(s) are "
            f"cfg-gated to other clusters ({membership.kept} active); run is in "
            f"behavioral re-fill. Gated: {_names(membership.skipped)}",
            "the N/121-style progress counter is at its ceiling for this "
            "cluster and will NOT move — judge progress by pass=, not fills",
        ))

    # -- 5b. bodies the model returned and routing threw away ----------------
    # Pure pipeline loss: the LLM call was paid for and answered, then the body
    # matched no container and was discarded. Invisible in pass/fail — the fn
    # just stays stubbed and reads as "hard". Names the rejected headers so the
    # canonicaliser can be compared against the container it should have hit.
    drops: dict[str, int] = {}
    for ln in segment_lines:
        if "does not match any impl in this file" not in ln:
            continue
        head = ln.split("raw_block=", 1)[1].split(" raw_canon=", 1)[0].strip() \
            if "raw_block=" in ln else ln.strip()
        drops[head] = drops.get(head, 0) + 1
    if drops:
        findings.append(HealthFinding(
            HealthKind.ROUTING_DROP, Severity.ALERT, "impl body routing",
            f"{sum(drops.values())} returned body(ies) discarded across "
            f"{len(drops)} header(s): {_names(tuple(drops))}",
            "the model answered and the pipeline dropped it — never read this as "
            "a model failure. Compare canon_container() of the rejected header "
            "against the containers of that file before touching any prompt",
        ))

    # -- 6. repeated pre-pass heal ------------------------------------------
    heals: dict[str, int] = {}
    for ln in segment_lines:
        if "[pre-pass-heal]" in ln and "implicated:" in ln:
            subj = ln.split("implicated:", 1)[1].strip()
            heals[subj] = heals.get(subj, 0) + 1
    for subj, c in heals.items():
        if c >= 2:
            findings.append(HealthFinding(
                HealthKind.REPEATED_HEAL, Severity.ALERT, subj,
                f"pre-pass heal fired {c}× on the same implication",
                "something re-poisons the tree between passes — find the "
                "writer, do not let the heal loop mask it",
            ))

    # -- 7. frozen-zero tree: pass=0 fail=0 for ≥3 trailing iters -------
    # No test ever reports a verdict → the crate is not compiling/parsing, but
    # the loop marches on marking fns exhausted from a broken state (the ~30h
    # error.rs corruption freeze had zero alarm). A pass=0 fail=0 STREAK is the
    # tree-broken signature (a legitimately hard cluster still reports fails).
    if readings:
        zero_streak = 0
        for r in reversed(readings):
            if r.pass_count == 0 and r.fail_count == 0:
                zero_streak += 1
            else:
                break
        if zero_streak >= 3:
            findings.append(HealthFinding(
                HealthKind.FROZEN_ZERO_TREE, Severity.ALERT, "tdd driver",
                f"pass=0 fail=0 for {zero_streak} consecutive iter(s) — no test "
                "reported a verdict",
                "the tree likely does not parse/compile and the heal is NOT "
                "firing; every unit is being mis-exhausted from a broken state "
                "— inspect the cluster tree, do not trust exhaustion",
            ))

    # -- 7b. oracle (denominator) drift --------------------------------
    # The test corpus grew 61→68 between runs unflagged, making pass counts
    # incomparable. A within-run change in the number of tests reporting a
    # verdict is the same class of hazard (a test target started/stopped
    # compiling) — surface it so pass= is never compared across a drift.
    oracles = [r.oracle for r in readings if r.oracle is not None and r.oracle > 0]
    if len(set(oracles)) > 1:
        findings.append(HealthFinding(
            HealthKind.ORACLE_DRIFT, Severity.WARN, "tdd driver",
            f"oracle size varied {min(oracles)}→{max(oracles)} within the run",
            "the denominator moved — pass counts across these iters are NOT "
            "comparable; a test target's compilation likely flipped",
        ))

    # -- 7c. unlocalized crashes -------------------------------------------
    # A shrunken oracle names WHICH tests vanished; the crash localizer names
    # WHERE. Sweeps with vanish but NO `[phase-c/crash]` blame line mean the
    # localizer is broken/disabled (gdb missing, ASAN build failing) — the
    # run is back to blind vanish accounting, the state that produced the
    # pass=14 plateau. One localized site in the segment suppresses (the
    # localizer works; per-sweep gaps are then reproduction flakiness).
    # Only a shrink that NAMES vanished tests implies a crash to localize. A
    # shrink that is purely an unnamed shortfall is a test-inventory gap; the
    # localizer is right not to blame anything, and counting it here reports a
    # broken localizer that is in fact working correctly.
    # "compile failed — no test binaries ran" shrinks are excluded: nothing
    # crashed, so a silent localizer is correct there, not broken. (The legacy
    # ambiguous rendering "crashed or failed to compile" predates the split
    # and still counts — it may hide a real crash.)
    shrunk = sum(1 for ln in segment_lines
                 if "ORACLE SHRUNK" in ln and "named test(s) reported" in ln
                 and "compile failed — no test binaries ran" not in ln)
    blamed = sum(1 for ln in segment_lines
                 if "[phase-c/crash]" in ln and "blamed" in ln)
    no_localize = sum(1 for ln in segment_lines
                      if "no crash could be localized" in ln)
    # A shrink whose localization was deliberately DEFERRED (the vanished tests
    # reach >=half still-stubbed fns, so a stub panic — with no native fault —
    # is the likely cause) is correct handling, not a broken localizer. Fire
    # only on an UNEXPLAINED shrink: the localizer RAN and found nothing
    # (no_localize) or never logged at all — never when the segment's shrinks
    # were all deferred as stub-confounded. (Existence, not exact counts: the
    # ORACLE SHRUNK line double-logs on the deterministic re-sweep.)
    deferred = any("[phase-c/crash]" in ln and "deferring localization" in ln
                   for ln in segment_lines)
    unexplained = no_localize > 0 or (shrunk > 0 and not deferred)
    if shrunk and not blamed and unexplained:
        findings.append(HealthFinding(
            HealthKind.CRASH_UNLOCALIZED, Severity.ALERT, "crash localizer",
            f"{shrunk} ORACLE SHRUNK sweep(s), 0 localized crash site(s)"
            + (f" ({no_localize} explicit no-localize line(s))"
               if no_localize else " (localizer never logged at all)")
            + ("; some deferred as stub-confounded" if deferred else ""),
            "vanished tests with no faulting stack = the localizer is not "
            "running or cannot reproduce — check gdb availability, the ASAN "
            "nightly build, and the [phase-c/crash] wiring before trusting "
            "any revert/exhaustion verdicts",
        ))

    # -- 7c-bis. cluster build broken --------------------------------------
    # A sweep whose test binaries never COMPILED is categorically different
    # from a failing one: no fill route can repair it. The per-fn route
    # compiles the lib plus its own unit and passes, so it keeps filling
    # bodies at full GPU cost while every test binary stays unbuildable; the
    # crash retrigger deliberately treats such a sweep as neutral (its
    # vanished names are not crash evidence), so it neither advances nor
    # resets. Net effect with no detector: the run reads as an ordinary
    # plateau — flat pass, rising stall — and burns until the stall budget
    # runs out. Recurrence across >=2 iters separates a real structural break
    # from one transient mid-batch red tree.
    _cb: set[int] = set()
    for ln in segment_lines:
        if "ORACLE SHRUNK" in ln and "compile failed — no test binaries ran" in ln:
            it = _int_after(ln, "iter ")
            if it is not None:
                _cb.add(it)
    if len(_cb) >= 2:
        _digest = next(
            (ln.split("distinct rustc error(s): ", 1)[1].strip()
             for ln in reversed(segment_lines)
             if "distinct rustc error(s): " in ln), "")
        findings.append(HealthFinding(
            HealthKind.CLUSTER_COMPILE_BROKEN, Severity.ALERT, "tdd driver",
            f"cluster test binaries failed to compile on iter(s) "
            f"{','.join(str(i) for i in sorted(_cb))}"
            + (f" — {_digest}" if _digest else ""),
            "no fill route can fix a build that never links: read the rustc "
            "digest on the ORACLE SHRUNK line, decide whether the frozen "
            "scaffold or a spliced body is at fault, and stop the run rather "
            "than paying for fills whose tests can never report",
        ))

    # -- 7d. static inventory over-count -----------------------------------
    # An ORACLE SHRUNK whose shortfall is UNNAMED (every expected test reported;
    # the total is still short) is not a crash — the static test inventory
    # counted a "test" that never becomes a runnable verdict, so the expected
    # denominator is bigger than the suite can ever report. Distinct from
    # ORACLE_DRIFT (a denominator that MOVED): this one is constant and does not
    # heal, so `SweepOracle.intact` can never hold and the cluster can never be
    # declared done even at 100% real pass — every sweep charges a phantom
    # failing test. Live class: two `#[rustversion::nightly]`/`not(nightly)`
    # variants of one test fn, counted twice though only one ever compiles.
    # Recurrence across >=2 iters proves it is structural, not a one-off race.
    _oc: set[int] = set()
    for ln in segment_lines:
        if "ORACLE SHRUNK" in ln and "unnamed test(s) short" in ln:
            it = _int_after(ln, "iter ")
            if it is not None:
                _oc.add(it)
    overcount_iters = sorted(_oc)
    if overcount_iters:
        findings.append(HealthFinding(
            HealthKind.INVENTORY_OVERCOUNT,
            Severity.ALERT if len(overcount_iters) >= 2 else Severity.WARN,
            "tdd driver",
            f"unnamed oracle shortfall on iter(s) "
            f"{','.join(str(i) for i in overcount_iters[:8])} — every expected "
            "test reported, the expected total is still higher",
            "the STATIC inventory over-counts (a test that never compiles to a "
            "verdict, e.g. two attribute-macro-gated variants of one fn counted "
            "twice) — fix count_test_fns_in_records / the extractor; the cluster "
            "can never reach 'done' while the denominator is inflated",
        ))

    # -- 8. skeleton-fallback fill loss (pairs with the heal hierarchy) ------------------
    for ln in segment_lines:
        if "restored whole file from skeleton" not in ln:
            continue
        lost = _int_after(ln, "(")   # "(N fill(s) lost" — 0 when absent
        findings.append(HealthFinding(
            HealthKind.HEAL_SKELETON_LOSS, Severity.WARN,
            _unit_of(ln, "restored") or "heal",
            f"whole-file skeleton restore discarded ~{lost or 0} fill(s)",
            "a writer left the file unparseable past surgical/best-tree/git "
            "recovery — find the writer bypassing the splice gate",
        ))

    # -- 9. capped-retry futility (session-241 plateau class) ---------------
    # A budgeted mechanism (layout re-trigger, t1.5 authoring) burning its cap
    # on a failure MODE retries cannot change. The overnight pass=14 burn: the
    # crash-retrigger failed attempt 1 with "missing authored items" (a
    # protocol/coverage defect — attempt-invariant) and the operator waited
    # out the remaining budget. Structural reasons alert on FIRST occurrence;
    # repeated compile-fails of the same component alert too (same design
    # re-rejected); a later success line for that component suppresses.
    _STRUCTURAL = ("missing authored items",
                   "could not locate a frozen definition")
    fail_reasons: dict[str, list[str]] = {}
    succeeded: set[str] = set()
    for ln in segment_lines:
        for tag in ("[layout-retrigger] ", "[t1.5] "):
            i = ln.find(tag)
            if i == -1:
                continue
            rest = ln[i + len(tag):]
            subj, sep, what = rest.partition(":")
            if not sep:
                continue
            if "re-authored successfully" in what or "authored layout compiles" in what:
                succeeded.add(subj.strip())
            elif ("still did not compile" in what
                  or "did not compile" in what
                  or any(r in what for r in _STRUCTURAL)):
                fail_reasons.setdefault(subj.strip(), []).append(what.strip())
    for subj, reasons in fail_reasons.items():
        if subj in succeeded:
            continue
        structural = [r for r in reasons
                      if any(s in r for s in _STRUCTURAL)]
        if structural:
            findings.append(HealthFinding(
                HealthKind.CAPPED_RETRY_FUTILITY, Severity.ALERT, subj,
                f"attempt-invariant failure: {structural[0][:120]}",
                "retries CANNOT fix a coverage/protocol defect — stop the "
                "pipeline and fix the mechanism now; do not wait out the "
                "remaining attempt budget",
            ))
        elif len(reasons) >= 2:
            findings.append(HealthFinding(
                HealthKind.CAPPED_RETRY_FUTILITY, Severity.ALERT, subj,
                f"{len(reasons)} re-author attempt(s) failed with no success "
                "line in this segment",
                "the same design keeps getting rejected — read the actual "
                "authoring prompt/reply and the compile errors before the "
                "cap exhausts silently",
            ))

    return findings
