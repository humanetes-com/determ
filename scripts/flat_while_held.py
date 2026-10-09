#!/usr/bin/env python3
"""FLAT ledger rows graded while every convicting test stopped first inside a
DIFFERENT still-stubbed unit at the grading sweep.

WHY THIS EXISTS
  body_ledger.classify's docstring says a test "blocked behind a stubbed
  sibling ... said nothing — UNMEASURED", but the code checks only that the
  test RAN. A test that ran and panicked inside another stub cannot observe
  the graded body, yet the row is minted FLAT and the unit is ranked as a
  spent re-buy. The fixture for the slice-liveness plan in technical_debt.md.

USAGE
  python3 scripts/flat_while_held.py logs/body_ledger/anyhow/index.jsonl logs/events.jsonl
  (scripts/pattern_census.py reads `flat_while_held` for its P46 row.)

HOW TO INTERPRET
  Per run: FLAT rows whose every convicting test was held elsewhere (the
  class), FLAT rows with at least one observing test (negative control), and
  rows with no stub_holds reading at that sweep (not judged). A row's
  `iteration` is the grading sweep.
"""
import json
import sys
from collections import defaultdict


def flat_while_held(index: str, events: str) -> dict[str, dict[str, object]]:
    """run -> {"held": n, "observed": n, "unread": n, "examples": [...]}."""
    flat = []
    with open(index, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("state") == "flat" and r.get("run_id"):
                flat.append(r)
    runs = {r["run_id"] for r in flat}
    held: dict[tuple[str, str, int], dict[str, str]] = {}
    with open(events, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"phase_c.crash.stub_holds"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            d = e.get("data") or e
            run = e.get("run_id") or d.get("run_id")
            if run not in runs:
                continue
            m = {}
            for stub, tests in (d.get("held") or {}).items():
                for t in tests:
                    m[t] = stub
            held[(run, d.get("cluster"), d.get("iter"))] = m
    out: dict[str, dict[str, object]] = defaultdict(
        lambda: {"held": 0, "observed": 0, "unread": 0, "examples": []})
    for r in flat:
        row = out[r["run_id"]]
        key = (r["run_id"], r["cluster"], r["iteration"])
        unit = f'{r["file"]}::{r["fn"]}'
        conv = r.get("convicting") or []
        if key not in held or not conv:
            row["unread"] = int(row["unread"]) + 1  # type: ignore[call-overload]
            continue
        m = held[key]
        if all(t in m and m[t] != unit for t in conv):
            row["held"] = int(row["held"]) + 1  # type: ignore[call-overload]
            ex = row["examples"]
            assert isinstance(ex, list)
            ex.append(f"{unit} (iter {r['iteration']}: {conv[0]} held by {m[conv[0]]})")
        else:
            row["observed"] = int(row["observed"]) + 1  # type: ignore[call-overload]
    return dict(out)


def main(argv: list[str]) -> None:
    tot = [0, 0, 0]
    for run, row in flat_while_held(argv[1], argv[2]).items():
        h, o, u = int(row["held"]), int(row["observed"]), int(row["unread"])  # type: ignore[call-overload]
        tot = [tot[0] + h, tot[1] + o, tot[2] + u]
        print(f"{run}: flat-while-held-elsewhere {h}, flat with an observing test {o}, "
              f"no holds reading {u}")
        for x in list(row["examples"])[:4]:  # type: ignore[call-overload]
            print("     ", x)
    print("TOTAL held-elsewhere / observed / no-reading:", tot)


if __name__ == "__main__":
    main(sys.argv)
