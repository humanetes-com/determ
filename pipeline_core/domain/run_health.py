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
        out.append(IterReading(it, p, f, s, m))  # type: ignore[arg-type]
    return out


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
        if delta <= 0:
            findings.append(HealthFinding(
                HealthKind.ZERO_PAYOFF_BURN, "warn", "fill/re-fill loop",
                f"{attempts} failed attempt(s) incl. {maximal} maximal, "
                f"pass delta {delta:+d} across the segment",
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

    return findings
