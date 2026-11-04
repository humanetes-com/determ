#!/usr/bin/env python3
"""Census: sibling-blocked verdicts that name a blocker already filled in the same fill.

WHY. The per-fn sibling-blocked check (`sibling_blocked_u2_failures`, called from
`fill_one_fn`) decides that no rewrite of a unit can pass its failing tests because
a still-stubbed sibling holds every one of them. Its stub set is the per-fn pass's
START scan (`PerFnPassCtx.still_stubbed`, built once in `phase_c_engine`). A fill
now runs for hours and pulls blockers in as it goes, so a sibling filled earlier in
the same fill is still reported as a stub. On run `994565-a5b000bb` (2026-11-04,
fill 1) `src/error.rs::msg` was kept at minute ~10 and `src/error.rs::construct_from_adhoc`
was later refused the fix loop and STOPPED as unpayable "needs still-stubbed sibling
src/error.rs::msg", although the crash-settle line had just blamed its own body.

USAGE.
    python scripts/stale_sibling_block_census.py [logs/events.jsonl] [--run RUN_ID]
Reads events only; no GPU, no cargo. Safe while a pipeline is live.

HOW TO INTERPRET.
- `runs with the check firing` / `total fires`: `phase_c.fn.u2_sibling_blocked` events.
- `fires naming an already-landed blocker`: fires where a unit in `blocked_on` had a
  `phase_c.fill.progress` (a kept fill) earlier in the same iteration of the same run.
  Each one is a verdict taken on a stub set that no longer described the tree.
  The unit is matched by `file::fn`, so two same-named fns in one file can make a
  false positive; read the listed examples before quoting a count.
- Decision rule: after the fix the second count must be 0 on every run recorded
  after the fix's code_id; any non-zero row on new code means a reader still holds
  a frozen stub set.
"""
import collections
import json
import sys


def main(argv: list[str]) -> int:
    path = "logs/events.jsonl"
    only = None
    args = list(argv)
    if "--run" in args:
        i = args.index("--run")
        only = args[i + 1]
        del args[i:i + 2]
    if args:
        path = args[0]
    want = ('"phase_c.fn.u2_sibling_blocked"', '"phase_c.fill.progress"',
            '"phase_c.tdd.iter"')
    landed: dict[str, set[str]] = collections.defaultdict(set)
    tot: collections.Counter[str] = collections.Counter()
    stale: collections.Counter[str] = collections.Counter()
    examples: dict[str, list[tuple[str, list[str]]]] = collections.defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(w in line for w in want):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            run = e.get("run_id") or "?"
            if only and run != only:
                continue
            d = e.get("data") or {}
            ev = e.get("event")
            if ev == "phase_c.tdd.iter":
                landed[run] = set()
            elif ev == "phase_c.fill.progress":
                landed[run].add(f"{d.get('file')}::{d.get('fn')}")
            elif ev == "phase_c.fn.u2_sibling_blocked":
                tot[run] += 1
                hit = [b for b in d.get("blocked_on") or [] if b in landed[run]]
                if hit:
                    stale[run] += 1
                    examples[run].append((f"{d.get('file')}::{d.get('fn')}", hit))
    print(f"runs with the check firing: {len(tot)}  total fires: {sum(tot.values())}")
    print(f"fires naming an already-landed blocker: {sum(stale.values())} "
          f"in {len(stale)} run(s)")
    for run in stale:
        print(f"  {run}: {stale[run]} of {tot[run]}")
        for unit, hit in examples[run][:3]:
            print(f"      {unit} <- named {', '.join(hit)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
