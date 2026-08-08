#!/usr/bin/env python3
"""Turn a recorded run's log into a walk fixture.

A walk is a replay of one cluster's TDD iterations through the real control
loop, with cargo and the model stubbed, used to assert that a fix makes a
DECISION reachable. Hand-transcribing the numbers off a log is how the first
such pins were written and it does not scale past the run you are staring at,
so this reads them instead.

Everything the fixture holds is already printed once per iteration:

    [stable] [phase-c/tdd] iter 4: cargo test rc=101 (0.1s); pass=26 fail=84
    oracle=110 (improved=False, stall=0/3); frontier=18 fn(s) (merged),
    24 skipped as exhausted.

plus the lines that record what the loop DID with that reading — a retrigger
firing and how many bodies it reverted, a withheld ratchet breach and the
high-water it was measured against, a re-admission granted or refused. Those
are the decisions a walk asserts on, and the refusals are the ones that matter:
a line saying `no further than 81` on a tree that peaked at 26 is the deadlock
in one string.

A run whose log rotated mid-flight spans several files. Pass them in order:

    python tools/distill_walk.py --cluster stable --run 259637-36db9aad \\
        logs/pipeline_anyhow.log.1 logs/pipeline_anyhow.log \\
        -o tests/fixtures/walks/259637-36db9aad.json

Iterations the loop never summarised still appear. Every retrigger path
`continue`s before the iteration's own emit, so the iteration a crash fired in
prints its retrigger line and no `cargo test rc=` line at all — dropping it
would silently renumber the walk around exactly the event it exists to replay.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_STARTED = re.compile(r"PIPELINE STARTED.*?run_id=(?P<run>[\w-]+)")

# The per-iteration summary. `rc` and the timing are read but not stored: the
# walk replays a scripted verdict, so the recorded exit code is a fact about
# cargo, not a decision the loop made.
_SWEEP = re.compile(
    r"\[(?P<cluster>[^\]]+)\] \[phase-c/tdd\] iter (?P<iter>\d+): cargo test "
    r"rc=(?P<rc>-?\d+).*?; pass=(?P<passed>\d+) fail=(?P<failed>\d+) "
    r"oracle=(?P<oracle>\d+) \(improved=(?P<improved>\w+), "
    r"stall=(?P<stall>\d+)/(?P<max_stall>\d+)\); "
    r"frontier=(?P<frontier>\d+) fn\(s\) \((?P<kind>[^)]*)\), "
    r"(?P<exhausted>\d+) skipped as exhausted")

_RETRIGGER = re.compile(
    r"\[(?P<cluster>[^\]]+)\] \[phase-c/tdd\] iter (?P<iter>\d+): CRASH layout "
    r"retrigger fired \(cause=(?P<cause>[^)]*)\) — reverted (?P<reverted>\d+) fn")

_WITHHELD = re.compile(
    r"\[(?P<cluster>[^\]]+)\] \[phase-c/tdd\] iter (?P<iter>\d+): batch ratchet — "
    r"breach \(pass_count (?P<now>\d+)<(?P<against>\d+)\) NOT charged")

_READMITTED = re.compile(
    r"\[(?P<cluster>[^\]]+)\] \[phase-c/tdd\] iter (?P<iter>\d+): re-admitted "
    r"(?P<count>\d+) previously-exhausted")

_REFUSED = re.compile(
    r"\[(?P<cluster>[^\]]+)\] \[phase-c/tdd\] iter (?P<iter>\d+): (?P<fills>\d+) "
    r"fill\(s\) since the last check moved pass no further than (?P<against>\d+) "
    r"— NOT re-admitting")


def _blank(iter_n: int) -> dict[str, Any]:
    return {"iter": iter_n}


def distill(paths: list[Path], cluster: str, run: str | None) -> dict[str, Any]:
    """Read `paths` in order and return the fixture for `cluster`.

    When `run` is given, lines before that run's START banner are skipped and
    reading stops at the next banner — a rotated log holds several runs and the
    iteration numbers restart in each, so merging two of them would produce a
    walk that never happened.
    """
    by_iter: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    active = run is None
    seen_run: str | None = None

    def slot(n: int) -> dict[str, Any]:
        if n not in by_iter:
            by_iter[n] = _blank(n)
            order.append(n)
        return by_iter[n]

    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                started = _STARTED.search(line)
                if started is not None:
                    found = started.group("run")
                    if run is not None:
                        if found == run:
                            active, seen_run = True, found
                        elif active:
                            # The next run's banner. Anything after it belongs
                            # to a different tree.
                            return _assemble(by_iter, order, cluster, seen_run)
                    continue
                if not active:
                    continue
                m = _SWEEP.search(line)
                if m is not None and m.group("cluster") == cluster:
                    s = slot(int(m.group("iter")))
                    s.update(
                        passed=int(m.group("passed")),
                        failed=int(m.group("failed")),
                        oracle=int(m.group("oracle")),
                        improved=m.group("improved") == "True",
                        stall=int(m.group("stall")),
                        max_stall=int(m.group("max_stall")),
                        frontier=int(m.group("frontier")),
                        kind=m.group("kind"),
                        exhausted=int(m.group("exhausted")))
                    continue
                m = _RETRIGGER.search(line)
                if m is not None and m.group("cluster") == cluster:
                    slot(int(m.group("iter")))["retrigger"] = {
                        "cause": m.group("cause"),
                        "reverted": int(m.group("reverted"))}
                    continue
                m = _WITHHELD.search(line)
                if m is not None and m.group("cluster") == cluster:
                    slot(int(m.group("iter")))["breach_withheld_against"] = \
                        int(m.group("against"))
                    continue
                m = _READMITTED.search(line)
                if m is not None and m.group("cluster") == cluster:
                    slot(int(m.group("iter")))["readmitted"] = int(m.group("count"))
                    continue
                m = _REFUSED.search(line)
                if m is not None and m.group("cluster") == cluster:
                    s = slot(int(m.group("iter")))
                    s["readmission_refused_against"] = int(m.group("against"))
                    s["fills"] = int(m.group("fills"))
    return _assemble(by_iter, order, cluster, seen_run)


def _assemble(by_iter: dict[int, dict[str, Any]], order: list[int],
              cluster: str, run: str | None) -> dict[str, Any]:
    iterations = [by_iter[n] for n in sorted(order)]
    # The high-water the run was already holding when its first recorded
    # iteration ran. The withheld-breach line carries it verbatim (`21<81`),
    # and it is the whole premise of a re-stub walk: without it the collapse
    # has nothing to have fallen FROM.
    entry_best = 0
    for it in iterations:
        for key in ("breach_withheld_against", "readmission_refused_against"):
            if key in it:
                entry_best = max(entry_best, int(it[key]))
    return {
        "run_id": run,
        "cluster": cluster,
        "entry_best": entry_best,
        "iterations": iterations,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--run", default=None,
                    help="run_id to isolate; omit to read every line given")
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args(argv)

    fixture = distill(args.logs, args.cluster, args.run)
    if not fixture["iterations"]:
        print(f"no iterations for cluster {args.cluster!r} in "
              f"{[str(p) for p in args.logs]}", file=sys.stderr)
        return 1
    text = json.dumps(fixture, indent=2) + "\n"
    if args.out is None:
        print(text, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"{args.out}: {len(fixture['iterations'])} iteration(s), "
              f"entry_best={fixture['entry_best']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
