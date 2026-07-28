"""Cross-run history pins.

The defect these encode: every `run_health` detector reads ONE segment, so
"flat for 2 readings" means two iterations of the current process. Five
consecutive runs sat at `pass=36` for ~46 hours and each one independently read
as healthy, because no artifact spanned them. These tests pin the span.
"""

from __future__ import annotations

from pipeline_core.domain.run_health import HealthKind
from pipeline_core.domain.run_history import (
    analyze_history,
    creation_time_from_reflog,
    parse_run_summaries,
    split_runs,
)


def _header(started: str, run_id: str, sha: str = "abc1234") -> str:
    return (f"  PIPELINE STARTED — {started} UTC  pipeline.py=b67eb777 "
            f"git={sha}  run_id={run_id}")


def _iter(n: int, passed: int, failed: int, oracle: int = 122) -> str:
    return (f"  [stable] [phase-c/tdd] iter {n}: cargo test rc=101 (0.1s); "
            f"pass={passed} fail={failed} oracle={oracle} "
            f"(improved=False, stall=1/3); frontier=2 fn(s) (merged), "
            f"0 skipped as exhausted.")


def _run(started: str, run_id: str, scores: list[int], *, fills: int = 0,
         reverts: int = 0, oracle: int = 122) -> list[str]:
    lines = [_header(started, run_id)]
    for i, s in enumerate(scores, start=1):
        lines.append(_iter(i, s, oracle - s, oracle))
    for _ in range(fills):
        lines.append("  [phase-c/per-fn] 1/2 filled src/error.rs::x (+1 bytes)")
    for _ in range(reverts):
        lines.append("  [phase-c/impl] src/error.rs::Error: REVERTED 1 fn(s).")
    return lines


# --- splitting + summarising -------------------------------------------------

def test_split_drops_lines_before_the_first_header() -> None:
    # A rotated archive can begin mid-run; those lines belong to a run whose
    # header is gone, so nothing can be attributed to it truthfully.
    lines = ["  orphan line from a rotated-out run", *_run("2026-07-28 01:11:35", "a", [10])]
    segs = split_runs(lines)
    assert len(segs) == 1
    assert "orphan" not in "".join(segs[0])


def test_summaries_are_oldest_first_and_carry_run_identity() -> None:
    lines = [*_run("2026-07-27 19:19:17", "old-1", [11, 13]),
             *_run("2026-07-28 01:11:35", "new-2", [34, 36])]
    runs = parse_run_summaries(lines)
    assert [r.run_id for r in runs] == ["old-1", "new-2"]
    assert runs[0].started == "2026-07-27 19:19:17 UTC"
    assert runs[0].git_sha == "abc1234"
    assert (runs[1].first.pass_count, runs[1].last.pass_count) == (34, 36)
    assert runs[1].score == 36 and runs[1].oracle == 122
    assert runs[1].moved and runs[1].iterations == 2


def test_run_with_no_scored_iteration_is_kept_not_dropped() -> None:
    # A run that never produced a score is itself a finding; silently dropping
    # it would make the history look healthier than it was.
    runs = parse_run_summaries(_run("2026-07-28 10:10:26", "dead", []))
    assert len(runs) == 1
    assert runs[0].score is None and not runs[0].moved


def test_fills_and_reverts_are_counted_per_run() -> None:
    runs = parse_run_summaries(
        _run("2026-07-28 10:10:26", "r", [36], fills=13, reverts=4))
    assert runs[0].fills_landed == 13 and runs[0].reverts == 4


# --- the plateau detector ----------------------------------------------------

def test_plateau_across_runs_fires_on_the_live_shape() -> None:
    # The real incident: three runs ending at 36 while fills kept landing.
    lines = [*_run("2026-07-27 19:19:17", "r1", [36, 36], fills=5),
             *_run("2026-07-28 01:11:35", "r2", [36, 36], fills=18, reverts=4),
             *_run("2026-07-28 10:10:26", "r3", [36, 36], fills=13, reverts=2)]
    findings = analyze_history(parse_run_summaries(lines))
    plateau = [f for f in findings if f.kind is HealthKind.PLATEAU_ACROSS_RUNS]
    assert len(plateau) == 1
    assert plateau[0].severity == "alert"
    assert "3 consecutive run(s)" in plateau[0].evidence
    assert "36 fill(s) landed" in plateau[0].evidence   # work landed, score didn't
    assert "2026-07-27 19:19:17" in plateau[0].evidence


def test_plateau_silent_while_the_score_is_still_moving() -> None:
    lines = [*_run("2026-07-27 19:19:17", "r1", [11, 13]),
             *_run("2026-07-28 01:11:35", "r2", [34, 36])]
    assert analyze_history(parse_run_summaries(lines)) == []


def test_plateau_silent_on_a_single_run() -> None:
    # Flatness inside ONE run is `stall_budget`'s job; this detector must not
    # duplicate it, or every ordinary stall reads as a multi-run plateau.
    lines = _run("2026-07-28 10:10:26", "only", [36, 36, 36])
    assert analyze_history(parse_run_summaries(lines)) == []


def test_plateau_downgrades_and_says_so_when_the_oracle_moved() -> None:
    # Equal pass counts measured against different denominators were never
    # comparable; claiming a plateau at full confidence would be a false alert.
    lines = [*_run("2026-07-27 19:19:17", "r1", [36], oracle=111),
             *_run("2026-07-28 01:11:35", "r2", [36], oracle=122)]
    f = analyze_history(parse_run_summaries(lines))[0]
    assert f.severity == "warn"
    assert "NOT comparable" in f.evidence and "[111, 122]" in f.evidence


def test_plateau_counts_only_the_trailing_flat_span() -> None:
    lines = [*_run("2026-07-27 19:19:17", "r1", [13]),
             *_run("2026-07-28 01:11:35", "r2", [36]),
             *_run("2026-07-28 10:10:26", "r3", [36])]
    f = analyze_history(parse_run_summaries(lines))[0]
    assert "2 consecutive run(s)" in f.evidence


# --- the workspace-reset detector -------------------------------------------

def test_workspace_reset_flags_runs_that_predate_the_current_tree() -> None:
    # `--clean` deletes the commits but the branch-creation reflog entry
    # survives; without this, a 93 → 36 drop across the boundary reads as a
    # catastrophic regression instead of a restart from stubs.
    lines = [*_run("2026-07-26 08:00:00", "before", [93]),
             *_run("2026-07-28 10:10:26", "after", [36])]
    findings = analyze_history(parse_run_summaries(lines),
                               workspace_created="2026-07-27 06:32:11")
    reset = [f for f in findings if f.kind is HealthKind.WORKSPACE_RESET]
    assert len(reset) == 1 and reset[0].severity == "alert"
    assert "1 logged run(s) predate" in reset[0].evidence


def test_workspace_reset_silent_when_every_run_postdates_the_tree() -> None:
    lines = [*_run("2026-07-28 01:11:35", "a", [36]),
             *_run("2026-07-28 10:10:26", "b", [36])]
    findings = analyze_history(parse_run_summaries(lines),
                               workspace_created="2026-07-27 06:32:11")
    assert not [f for f in findings if f.kind is HealthKind.WORKSPACE_RESET]


def test_creation_time_is_read_from_the_oldest_created_from_entry() -> None:
    out = (
        "c35c2b8 pipeline@{2026-07-27 04:36:53}: commit: pipeline: task0b done\n"
        "1dbe186 pipeline@{2026-07-27 04:32:34}: branch: Created from HEAD\n"
    )
    assert creation_time_from_reflog(out) == "2026-07-27 04:32:34"


def test_creation_time_empty_when_the_branch_was_never_created_here() -> None:
    out = "c35c2b8 pipeline@{2026-07-27 04:36:53}: commit: pipeline: task0b done\n"
    assert creation_time_from_reflog(out) == ""


def test_workspace_reset_boundary_is_compared_in_one_zone() -> None:
    # The reading and the log headers must both be UTC. `git reflog
    # --date=format:` renders LOCAL time, so on a +0200 machine the boundary
    # lands two hours late and every run started inside that window is reported
    # as predating a workspace it actually postdates. This pins the failure that
    # skew produces, so the tool's `--date=format-local:` + TZ=UTC cannot be
    # reverted silently.
    lines = _run("2026-07-27 05:00:00", "after-the-real-reset", [36])
    utc = analyze_history(parse_run_summaries(lines),
                          workspace_created="2026-07-27 04:32:34")
    assert not [f for f in utc if f.kind is HealthKind.WORKSPACE_RESET]

    local_skewed = analyze_history(parse_run_summaries(lines),
                                   workspace_created="2026-07-27 06:32:34")
    assert [f for f in local_skewed if f.kind is HealthKind.WORKSPACE_RESET], (
        "a local-time reading must be detectable as a false positive")


def test_workspace_reset_silent_without_a_reflog_reading() -> None:
    # "No reflog" and "never reset" are different facts — an empty reading must
    # disable the detector, never assert that nothing was wiped.
    lines = _run("2026-07-26 08:00:00", "before", [93])
    findings = analyze_history(parse_run_summaries(lines), workspace_created="")
    assert not [f for f in findings if f.kind is HealthKind.WORKSPACE_RESET]
