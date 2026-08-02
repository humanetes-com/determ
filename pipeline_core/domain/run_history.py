"""Cross-RUN history — the view no single pass could see.

`run_health.analyze` reads ONE segment, so every question it can answer is
scoped to the current run: "flat for 2 readings" means two iterations of this
process, not two days. That scoping hid a real plateau for five consecutive
runs and ~46 hours — each run independently read as "a bug was found, fixed,
restarted", and the fact that none of the five had moved the score was stated
nowhere, because no artifact spanned them.

This module is the missing span. It splits a multi-run log into per-run
summaries and answers the two questions that are only askable across runs:

  * is the score the same as the run before it, and the one before that?
  * did the reconstruction get wiped, making earlier scores describe a tree
    that no longer exists?

Pure domain: the caller supplies already-read lines and the workspace's branch
creation time. Token scans only — no regex (repo rule).
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.run_health import (
    HealthFinding,
    HealthKind,
    IterReading,
    Severity,
    count_fills_landed,
    parse_iter_readings,
)

__all__ = [
    "RunSummary",
    "analyze_history",
    "creation_time_from_reflog",
    "parse_run_summaries",
    "split_runs",
]

_START_MARKER = "PIPELINE STARTED"


def _field_after(text: str, marker: str, stop: str = " ") -> str:
    """The token following ``marker``, up to ``stop``. Empty when absent."""
    i = text.find(marker)
    if i == -1:
        return ""
    rest = text[i + len(marker):].lstrip()
    end = rest.find(stop)
    return rest if end == -1 else rest[:end]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One pipeline run, as read from its slice of the log.

    `first`/`last` are the run's first and last scored TDD iterations. A run
    that never reached a `cargo test` reading has both None — it is reported,
    never silently dropped, because a run that produced no score at all is
    itself a finding."""

    run_id: str
    started: str            # "2026-07-28 10:10:26 UTC" as logged
    git_sha: str
    first: IterReading | None
    last: IterReading | None
    iterations: int
    fills_landed: int
    reverts: int

    @property
    def moved(self) -> bool:
        """Did the pass count change at all within this run?"""
        if self.first is None or self.last is None:
            return False
        return self.first.pass_count != self.last.pass_count

    @property
    def score(self) -> int | None:
        return self.last.pass_count if self.last is not None else None

    @property
    def oracle(self) -> int | None:
        return self.last.oracle if self.last is not None else None


def creation_time_from_reflog(out: str) -> str:
    """The branch-creation timestamp inside ``git reflog show`` output, empty
    when the entry is absent. The creation entry is the OLDEST line and ends in
    "branch: Created from …"::

        1dbe186 pipeline@{2026-07-27 04:32:34}: branch: Created from HEAD

    The caller is responsible for rendering that date in UTC (the zone the run
    log's headers use) — this only reads back whatever git was asked to print.
    """
    for line in reversed(out.splitlines()):
        if "branch: Created from" not in line:
            continue
        start = line.find("@{")
        end = line.find("}", start)
        if start != -1 and end != -1:
            return line[start + 2:end]
    return ""


def split_runs(lines: list[str]) -> list[list[str]]:
    """Slice a multi-run log at its `PIPELINE STARTED` markers, OLDEST first.

    Lines before the first marker are dropped: they belong to a run whose
    header was rotated out, so nothing can be attributed to it truthfully."""
    runs: list[list[str]] = []
    cur: list[str] | None = None
    for ln in lines:
        if _START_MARKER in ln:
            cur = [ln]
            runs.append(cur)
        elif cur is not None:
            cur.append(ln)
    return runs


def parse_run_summaries(lines: list[str]) -> list[RunSummary]:
    """Multi-run log lines → one summary per run, OLDEST first."""
    out: list[RunSummary] = []
    for seg in split_runs(lines):
        header = seg[0]
        readings = parse_iter_readings(seg)
        reverts = sum(
            1 for ln in seg
            if "REVERTED" in ln or ": reverted after " in ln
        )
        out.append(RunSummary(
            run_id=_field_after(header, "run_id="),
            started=_field_after(header, f"{_START_MARKER} — ", "  ").strip(),
            git_sha=_field_after(header, "git="),
            first=readings[0] if readings else None,
            last=readings[-1] if readings else None,
            iterations=len(readings),
            fills_landed=count_fills_landed(seg),
            reverts=reverts,
        ))
    return out


def _plateau_tail(runs: list[RunSummary]) -> list[RunSummary]:
    """The longest run of trailing runs that all END on the same pass count."""
    scored = [r for r in runs if r.score is not None]
    if not scored:
        return []
    target = scored[-1].score
    tail: list[RunSummary] = []
    for r in reversed(scored):
        if r.score != target:
            break
        tail.append(r)
    tail.reverse()
    return tail


def analyze_history(
    runs: list[RunSummary],
    *,
    workspace_created: str = "",
    min_runs: int = 2,
) -> list[HealthFinding]:
    """Findings that only exist ACROSS runs.

    `workspace_created` is the workspace branch's creation timestamp (from
    ``git reflog show <branch>``) as ``YYYY-MM-DD HH:MM:SS`` in the SAME zone
    the log header uses, so the two are ordered by a plain prefix compare.
    Empty disables that detector rather than guessing — "no reflog" and "never
    reset" are different facts and must not collapse into one silence."""
    findings: list[HealthFinding] = []

    tail = _plateau_tail(runs)
    if len(tail) >= min_runs:
        # Work that LANDED while the score sat still is the damning part: it
        # separates "nothing is being attempted" from "everything attempted is
        # being discarded or is invisible to the oracle".
        fills = sum(r.fills_landed for r in tail)
        reverts = sum(r.reverts for r in tail)
        oracles = {r.oracle for r in tail if r.oracle is not None}
        comparable = len(oracles) <= 1
        findings.append(HealthFinding(
            HealthKind.PLATEAU_ACROSS_RUNS,
            Severity.ALERT if comparable else Severity.WARN,
            f"pass={tail[-1].score}",
            f"unchanged across {len(tail)} consecutive run(s) "
            f"(since {tail[0].started or 'unknown'}); "
            f"{fills} fill(s) landed and {reverts} revert(s) in that span"
            + ("" if comparable else
               f"; oracle moved {sorted(oracles)} — scores NOT comparable"),
            "state the plateau in RUNS and HOURS, never in iterations — no "
            "single segment can show this. Before fixing another unit, check "
            "whether the previous runs' fixes were ever validated: a run that "
            "is killed and restarted each pass never gets to prove one",
        ))

    if workspace_created:
        _cut = workspace_created[:19]
        older = [r for r in runs if r.started and r.started[:19] < _cut]
        if older:
            findings.append(HealthFinding(
                HealthKind.WORKSPACE_RESET, Severity.ALERT,
                f"workspace recreated {workspace_created}",
                f"{len(older)} logged run(s) predate the current "
                "reconstruction — their scores describe a tree that no longer "
                "exists",
                "do NOT compare a score across this boundary, and do not read "
                "a drop across it as a regression; the reconstruction restarted "
                "from stubs",
            ))

    return findings
