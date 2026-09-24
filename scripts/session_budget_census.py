#!/usr/bin/env python3
"""Does the printed turn budget change what a slice session's model does?

WHY THIS EXISTS
  The session prompt prints "Session budget: N turn(s)" at the top and a
  countdown after every turn (`domain/slice_session.py::render_budget_turn`).
  One read call (`2a7bcba0…`, turn 2 of 8, 2026-09-2x) reasoned "Given
  limited time … we can only make one change set per turn" before
  committing, which raised the question whether the countdown makes the
  model commit a change set instead of probing (requesting a file, a test,
  an expansion) and whether those commits fail more. The countdown is a
  prompt fact the harness does not need: the cap is enforced by the loop
  (`run_slice_session`, `max_turns`) whatever the prompt says. Printing it is
  justified only if it does no harm; this script measures whether it does,
  before anything changes.

USAGE
  python scripts/session_budget_census.py [--since YYYY-MM-DD]

  Reads logs/events.jsonl (phase_c.session.start / .turn / .end, and the
  llm.request / llm.response rows between them on the same (run_id, tid))
  and logs/thinking/<response sha>.txt (the reasoning of each reply, when
  the transport recorded it). Offline, no GPU, ~10 s over 300 MB of events.
  Sessions run on the main thread one at a time, so (run_id, tid) joins a
  call to the open session unambiguously.

HOW TO INTERPRET THE OUTPUT
  One row per "turns remaining when the reply was written" (max_turns minus
  the turn's number; 0 = the last turn):
    turns        replies in that bucket
    thinking     how many of them have a recorded reasoning trace
    scarce       share of traced replies whose reasoning uses budget
                 language ("limited time", "turns left", "last turn",
                 "only one change set", "budget", ...) — see SCARCITY below
    request      share of replies that asked for context (a probe)
    commit       share that returned a change set (judged + compile +
                 rejected + inconclusive)
    commit_cold  share of change sets whose PREVIOUS turn was not a request,
                 i.e. committed straight off the last result
    compile      share of change sets that did not compile
    worse        share of judged change sets put back as worse than the
                 session's best state (`restored_to` set)
  Above the table: the model's generic hedge ("Given constraints, …",
  "Given limited time, …") in session replies against the control — the
  per-fn / per-impl / file-first replies, whose prompts print no budget. Equal
  rates mean the hedge is the model's habit, not the countdown's effect, and
  a quote of it is no evidence either way. `scarce` counts only language that
  names TURNS, which the control shows at ~2 %.
  Then the same commit outcomes split by whether the reply's reasoning used
  turn-budget language, pooled, and over the middle turns only: the first
  turn is always cold and never `worse` (nothing is best yet), the last is
  told it is the last, and both would confound the split.

  Decision rule. If `scarce` rises as turns remaining fall AND the scarce
  replies commit cold more often or fail more often (compile / worse) than
  the non-scarce replies at the same remaining-turn bucket, the countdown is
  steering the model: print only the final-turn instruction (which feeds
  PREVIOUS_SESSION_NOTES) and keep the cap in the harness. If scarcity
  language is rare, or scarce and non-scarce replies fail at the same rate,
  the countdown is harmless and the change would buy nothing — leave it.
  Buckets with fewer than ~20 replies are noise; read the pooled split.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re

THINK = "logs/thinking"
# Language that names the TURN budget — only the countdown can put it there.
SCARCITY = re.compile(
    r"turns? (?:left|remaining)|remaining turns?|(?:limited|few) turns?"
    r"|last turn|final turn|only (?:one|1) turn|(?:turn|session) budget"
    r"|no more turns|out of turns|running out",
    re.I)
# The model's generic hedge ("Given constraints, simplest is …", "Given
# limited time, …"). It is counted separately, and against the non-session
# fill routes (whose prompts print no budget) as a control: if it is as
# frequent there, it is the model's habit and not the countdown's effect.
GENERIC = re.compile(r"given (?:the )?(?:limited|constraints)|limited time", re.I)
FILL_START = {"phase_c.fn.fill.start", "phase_c.impl.fill.start",
              "phase_c.file.fill.start"}
FILL_END = {"phase_c.fn.fill.done", "phase_c.fn.fill.fail",
            "phase_c.impl.fill.done", "phase_c.impl.fill.fail",
            "phase_c.file.fill.done", "phase_c.file.fill.revert"}
COMMIT = {"judged", "compile", "rejected", "inconclusive"}


def thinking(sha: str | None) -> str | None:
    if not sha:
        return None
    path = os.path.join(THINK, sha + ".txt")
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def load(since: str) -> tuple[list[dict], list[str | None]]:
    """Every session reply: its turn, the session's max, the turn's kind, the
    judged turn's restore, and the response sha of the call that wrote it.
    Second: the response shas of the non-session fill routes' calls — the
    control population, whose prompts print no turn budget."""
    rows: list[dict] = []
    control: list[str | None] = []
    open_: dict[tuple, dict] = {}
    fills: set[tuple] = set()
    with open("logs/events.jsonl", errors="replace") as f:
        for line in f:
            if line[8:18] < since or ('"phase_c.' not in line
                                      and '"llm.response"' not in line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ev = d.get("event")
            key = (d.get("run_id"), d.get("tid"))
            data = d.get("data") or {}
            if ev in FILL_START:
                fills.add(key)
            elif ev in FILL_END:
                fills.discard(key)
            elif ev == "llm.response" and key in fills and key not in open_:
                control.append((data.get("response") or {}).get("sha256"))
            if ev == "phase_c.session.start":
                open_[key] = {"max": int(data.get("max_turns") or 0),
                              "responses": [], "turns": []}
            elif ev == "llm.response" and key in open_:
                open_[key]["responses"].append(
                    (data.get("response") or {}).get("sha256"))
            elif ev == "phase_c.session.turn" and key in open_:
                open_[key]["turns"].append(data)
            elif ev == "phase_c.session.end" and key in open_:
                s = open_.pop(key)
                prev_kind = ""
                for i, t in enumerate(s["turns"]):
                    n = int(t.get("turn") or i + 1)
                    sha = s["responses"][n - 1] if n - 1 < len(s["responses"]) else None
                    rows.append({
                        "remaining": s["max"] - n, "kind": t.get("kind"),
                        "prev": prev_kind,
                        "worse": t.get("restored_to") is not None,
                        "sha": sha})
                    prev_kind = t.get("kind") or ""
    return rows, control


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "    -"


def outcome_row(rs: list[dict]) -> str:
    commits = [r for r in rs if r["kind"] in COMMIT]
    judged = [r for r in commits if r["kind"] == "judged"]
    return (f"commit {pct(len(commits), len(rs))}  "
            f"commit_cold {pct(sum(r['prev'] != 'request' for r in commits), len(commits))}  "
            f"compile {pct(sum(r['kind'] == 'compile' for r in commits), len(commits))}  "
            f"worse {pct(sum(r['worse'] for r in judged), len(judged))}  "
            f"(n={len(rs)}, change sets {len(commits)}, judged {len(judged)})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01")
    args = ap.parse_args()
    rows, control = load(args.since)
    for r in rows:
        t = thinking(r["sha"])
        r["traced"] = t is not None
        r["scarce"] = bool(t and SCARCITY.search(t))
        r["generic"] = bool(t and GENERIC.search(t))
    traced_all = [r for r in rows if r["traced"]]
    ctl = [t for t in (thinking(s) for s in control) if t is not None]
    print(f"session replies since {args.since}: {len(rows)} "
          f"(reasoning traced for {len(traced_all)})")
    print(f"generic hedge ('given constraints', 'limited time'): session "
          f"{pct(sum(r['generic'] for r in traced_all), len(traced_all))} of "
          f"traced replies; control (fn/impl/file fills, no budget printed) "
          f"{pct(sum(bool(GENERIC.search(t)) for t in ctl), len(ctl))} of "
          f"{len(ctl)}; turn-budget language in the control "
          f"{pct(sum(bool(SCARCITY.search(t)) for t in ctl), len(ctl))}")
    by: dict[int, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by[r["remaining"]].append(r)
    print("remaining  turns thinking  scarce  request  " + "outcomes")
    for rem in sorted(by, reverse=True):
        rs = by[rem]
        traced = [r for r in rs if r["traced"]]
        print(f"{rem:>9}  {len(rs):>5} {len(traced):>8}  "
              f"{pct(sum(r['scarce'] for r in traced), len(traced))}  "
              f"{pct(sum(r['kind'] == 'request' for r in rs), len(rs))}  "
              + outcome_row(rs))
    traced = [r for r in rows if r["traced"]]
    print("pooled, by reasoning language:")
    print("  scarce      " + outcome_row([r for r in traced if r["scarce"]]))
    print("  not scarce  " + outcome_row([r for r in traced if not r["scarce"]]))
    # The first turn cannot be `worse` (nothing is best yet) and is always
    # cold; the last turn is told it is the last. The middle turns are the
    # comparison the decision rule reads.
    mid = [r for r in traced if 0 < r["remaining"] and r["prev"]]
    print("pooled, same split, middle turns only (not the first, not the last):")
    print("  scarce      " + outcome_row([r for r in mid if r["scarce"]]))
    print("  not scarce  " + outcome_row([r for r in mid if not r["scarce"]]))


if __name__ == "__main__":
    main()
