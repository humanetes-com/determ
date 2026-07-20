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

from pipeline_core.domain.model_notes import is_bogus_regression_note

__all__ = [
    "HealthFinding",
    "HealthKind",
    "IterReading",
    "analyze",
    "parse_iter_readings",
]


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
    CAPPED_RETRY_FUTILITY = "capped_retry_futility"  # budgeted mechanism failing in an attempt-invariant way
    CRASH_UNLOCALIZED = "crash_unlocalized"      # tests vanished but no crash site was produced


@dataclass(frozen=True, slots=True)
class HealthFinding:
    kind: HealthKind
    severity: str          # "info" | "warn" | "alert"
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
    oracle: int | None = None   # F6: the denominator (tests reporting a verdict)


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
        oracle = _int_after(ln, "oracle=")   # F6 — may be absent on older lines
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

    # -- 1. poisoned model notes ------------------------------------------
    bogus = sum(1 for n in notes if is_bogus_regression_note(n))
    if bogus:
        findings.append(HealthFinding(
            HealthKind.POISONED_NOTES, "warn", "FR Model Notes",
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
        sev = "alert" if remaining <= 1 else ("warn" if flat >= 2 else "info")
        findings.append(HealthFinding(
            HealthKind.STALL_BUDGET, sev, "tdd driver",
            f"pass={last.pass_count} fail={last.fail_count}, flat for {flat} "
            f"reading(s), stall {last.stall}/{last.stall_max}",
            f"{remaining} non-improving iteration(s) left before the inner "
            "loop stalls out; at stall-out below the historical best, "
            "escalate (attribution debt #82 / tree diff)",
        ))

    # -- 3. zero-payoff burn ------------------------------------------------
    attempts = sum(1 for ln in segment_lines
                   if "U2 test failed" in ln or "compile failed" in ln)
    maximal = sum(1 for ln in segment_lines if "maximal last-resort" in ln)
    if readings and attempts >= burn_min_attempts:
        delta = readings[-1].pass_count - readings[0].pass_count
        landed = count_fills_landed(segment_lines)
        # Only cry wolf when the tree is genuinely frozen. A stale per-sweep
        # pass delta ≤ 0 while bodies are still landing = mid-sweep, not a
        # burning loop — pass= just hasn't been re-measured since the last
        # sweep boundary. Suppress on landed fills to avoid alarm fatigue.
        if delta <= 0 and landed == 0:
            findings.append(HealthFinding(
                HealthKind.ZERO_PAYOFF_BURN, "warn", "fill/re-fill loop",
                f"{attempts} failed attempt(s) incl. {maximal} maximal, "
                f"pass delta {delta:+d} across the segment, no fills landed",
                "the loop is buying nothing — check MODEL_NOTES quality and "
                "attribution before letting it continue",
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
            HealthKind.ATTRIBUTION_FUTILITY, "warn", "attribution",
            f"{len(futile)} unit(s) stopped identically ≥{futile_min_stops}×: {top}",
            "every attributed fn is being rewritten to the same dead end — "
            "the defect likely sits in the CONSTRUCTION path of the type the "
            "test exercises (debt #82: escalate along the call graph)",
        ))

    # -- 5. frozen stub counter / phase -------------------------------------
    cfg_skipped = None
    for ln in reversed(segment_lines):
        if "stub(s) not active in this cluster's cfg-set" in ln:
            cfg_skipped = _int_after(ln, "skipped ")
            break
    behavioral = sum(1 for ln in segment_lines if "U2 test failed" in ln)
    if cfg_skipped and behavioral:
        findings.append(HealthFinding(
            HealthKind.FROZEN_COUNTER, "info", "stub-fill counter",
            f"{cfg_skipped} remaining stub(s) are cfg-gated to other clusters; "
            "run is in behavioral re-fill",
            "the N/121-style progress counter is at its ceiling for this "
            "cluster and will NOT move — judge progress by pass=, not fills",
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
                HealthKind.REPEATED_HEAL, "alert", subj,
                f"pre-pass heal fired {c}× on the same implication",
                "something re-poisons the tree between passes — find the "
                "writer, do not let the heal loop mask it",
            ))

    # -- 7. frozen-zero tree (F7): pass=0 fail=0 for ≥3 trailing iters -------
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
                HealthKind.FROZEN_ZERO_TREE, "alert", "tdd driver",
                f"pass=0 fail=0 for {zero_streak} consecutive iter(s) — no test "
                "reported a verdict",
                "the tree likely does not parse/compile and the heal is NOT "
                "firing; every unit is being mis-exhausted from a broken state "
                "— inspect the cluster tree, do not trust exhaustion",
            ))

    # -- 7b. oracle (denominator) drift (F6) --------------------------------
    # The test corpus grew 61→68 between runs unflagged, making pass counts
    # incomparable. A within-run change in the number of tests reporting a
    # verdict is the same class of hazard (a test target started/stopped
    # compiling) — surface it so pass= is never compared across a drift.
    oracles = [r.oracle for r in readings if r.oracle is not None and r.oracle > 0]
    if len(set(oracles)) > 1:
        findings.append(HealthFinding(
            HealthKind.ORACLE_DRIFT, "warn", "tdd driver",
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
    shrunk = sum(1 for ln in segment_lines
                 if "ORACLE SHRUNK" in ln and "named test(s) reported" in ln)
    blamed = sum(1 for ln in segment_lines
                 if "[phase-c/crash]" in ln and "blamed" in ln)
    no_localize = sum(1 for ln in segment_lines
                      if "no crash could be localized" in ln)
    if shrunk and not blamed:
        findings.append(HealthFinding(
            HealthKind.CRASH_UNLOCALIZED, "alert", "crash localizer",
            f"{shrunk} ORACLE SHRUNK sweep(s), 0 localized crash site(s)"
            + (f" ({no_localize} explicit no-localize line(s))"
               if no_localize else " (localizer never logged at all)"),
            "vanished tests with no faulting stack = the localizer is not "
            "running or cannot reproduce — check gdb availability, the ASAN "
            "nightly build, and the [phase-c/crash] wiring before trusting "
            "any revert/exhaustion verdicts",
        ))

    # -- 8. skeleton-fallback fill loss (F7 pairs with F2) ------------------
    for ln in segment_lines:
        if "restored whole file from skeleton" not in ln:
            continue
        lost = _int_after(ln, "(")   # "(N fill(s) lost" — 0 when absent
        findings.append(HealthFinding(
            HealthKind.HEAL_SKELETON_LOSS, "warn",
            _unit_of(ln, "restored") or "heal",
            f"whole-file skeleton restore discarded ~{lost or 0} fill(s)",
            "a writer left the file unparseable past surgical/best-tree/git "
            "recovery — find the writer bypassing the F1 splice gate",
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
                HealthKind.CAPPED_RETRY_FUTILITY, "alert", subj,
                f"attempt-invariant failure: {structural[0][:120]}",
                "retries CANNOT fix a coverage/protocol defect — stop the "
                "pipeline and fix the mechanism now; do not wait out the "
                "remaining attempt budget",
            ))
        elif len(reasons) >= 2:
            findings.append(HealthFinding(
                HealthKind.CAPPED_RETRY_FUTILITY, "alert", subj,
                f"{len(reasons)} re-author attempt(s) failed with no success "
                "line in this segment",
                "the same design keeps getting rejected — read the actual "
                "authoring prompt/reply and the compile errors before the "
                "cap exhausts silently",
            ))

    return findings
