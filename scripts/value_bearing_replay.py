#!/usr/bin/env python3
"""Replay every recorded task 0d `when` through the shipped value-bearing gate.

WHY
---
Task 0d refuses a proposed probe program before any cargo run when
`value_bearing_reason(when)` (`domain/behaviour_probe.py`) names a reason.
The refusal is counted as a miss and logged nowhere per program, and rule-probe
misses are not written to `_behaviour_misses_<world>.json`, so its effect is
only visible by re-parsing the model's replies. On run `2355041-eae39441`
(2026-10-27) it refused 16 of the 25 whens the shared rule-probe ask proposed,
and the behaviour tests in the oracle fell from 22 to 11 per world. Every one
of the 16 was an operator nested in a closure (`(|| -> Result<()> { ensure!(a
>= b); Ok(()) })().unwrap_err()`) or in a macro argument (`ensure!(x > y)
.unwrap_err()`): the gate scanned the text, not the when's top-level value.
Over the whole record (10 runs, 2026-10-04 to 2026-10-27, 634 whens) the
substring gate refused 110, all of them that shape or a qualified path
`<T as Trait>::f(..)` read as `<`.

USAGE
-----
    python scripts/value_bearing_replay.py              # every run on record
    python scripts/value_bearing_replay.py --run 2355041-eae39441
    python scripts/value_bearing_replay.py --phase t0d.rule_probes --show 20

Reads `logs/events.jsonl` (`llm.response` rows whose phase starts with `t0d`)
and the reply bodies in `logs/prompts/<sha>.txt`, parses them with the shipped
`parse_behaviour_specs`, and judges each `when` with the shipped gate. A reply
seen twice (memoised across worlds) is counted once. Read-only, no cargo, no
model call, no GPU; a few seconds over a 400 MB events file.

HOW TO INTERPRET THE OUTPUT
---------------------------
One line per (date, run, phase):

    2026-10-27 2355041-eae39441 t0d.rule_probes  whens=25 refused=16  (compares (==) 6, combines (||) 9, ...)

  whens    whens parsed from that phase's replies in that run.
  refused  whens the gate refuses before cargo. Each is a program that was
           never run, never repaired and never captured.
  (...)    the gate's reasons, first words, with counts.

Then `--show N` refused whens, most frequent first. The decision rule: a
refused when whose VALUE is not the boolean of a top-level comparison or
logic operator (a closure call, a macro or call whose argument compares, a
qualified path) is a false refusal and the gate is wrong. A refused when like
`format!("{}", b) == "world"` or `a.is::<X>() && b.is::<Y>()` is the gate
working. After the top-level gate ships, the first kind should read 0.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.domain.behaviour_probe import (  # noqa: E402
    parse_behaviour_specs,
    value_bearing_reason,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", help="one run id")
    ap.add_argument("--phase", help="one phase, e.g. t0d.rule_probes")
    ap.add_argument("--show", type=int, default=10, help="refused whens to print")
    args = ap.parse_args()

    whens: dict[tuple[str, str, str], int] = collections.Counter()
    refused: dict[tuple[str, str, str], collections.Counter[str]] = collections.defaultdict(collections.Counter)
    examples: collections.Counter[str] = collections.Counter()
    seen: set[str] = set()
    with open(ROOT / "logs" / "events.jsonl", errors="replace") as f:
        for line in f:
            if '"llm.response"' not in line or '"t0d' not in line:
                continue
            ev = json.loads(line)
            phase = ev.get("in_phase") or ""
            run = ev.get("run_id") or "?"
            if not phase.startswith("t0d") or (args.run and run != args.run) or (args.phase and phase != args.phase):
                continue
            resp = ev.get("data", {}).get("response")
            sha = resp.get("sha256") if isinstance(resp, dict) else None
            path = ROOT / "logs" / "prompts" / f"{sha}.txt"
            if not sha or sha in seen or not os.path.exists(path):
                continue
            seen.add(sha)
            key = (ev["ts"][:10], run, phase)
            for spec in parse_behaviour_specs(path.read_text(errors="replace")):
                whens[key] += 1
                why = value_bearing_reason(spec.when)
                if why is not None:
                    refused[key][" ".join(why.split()[:3])] += 1
                    examples[spec.when.strip()] += 1

    total = sum(whens.values())
    bad = sum(sum(c.values()) for c in refused.values())
    for key in sorted(whens):
        reasons = ", ".join(f"{r} {n}" for r, n in refused[key].most_common())
        print(f"{key[0]} {key[1]:<20} {key[2]:<18} whens={whens[key]:<4} "
              f"refused={sum(refused[key].values()):<4} ({reasons})")
    print(f"TOTAL whens={total} refused={bad}")
    for when, n in examples.most_common(args.show):
        print(f"  {n:>3}  {when[:160]}")


if __name__ == "__main__":
    main()
