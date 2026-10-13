#!/usr/bin/env python3
"""Replay one sweep's recorded rank inputs under the shipped class rule and
under the frozen pre-change rule, and optionally write them as a test fixture.

WHY
---
The rank (`pipeline_core/domain/dispatch_rank.py`) orders the frontier by
class first and rotates units by their unclaimed streak only inside a class.
Before 2026-10-12 a never-served unit that was neither held nor owed was in
`OTHER`, behind the held and owed units. The owed set does not drain, so on
runs `2157238-210637e6`, `3163270-ca5a787c` and `35210-6d56e8c9` (cluster
`stable`) 44 such units got 0 slots on four consecutive sweeps with streaks
up to 3: `construct`, `vtable`, every `object_*`, every `src/ptr.rs` body,
`src/wrapper.rs::fmt`. The class rule now puts a never-served unit in
`EVIDENCED`. This script computes that claim on the record instead of
describing it, with the old rule as the negative control.

USAGE
-----
    python scripts/never_served_starvation_fixture.py [RUN_ID] [ITER] \
        [--cap 12] [--sweeps 10] [--write PATH]

Defaults: run `3163270-ca5a787c`, iter 2, cap 12, 10 sweeps. Reads only
`logs/events.jsonl` (`phase_c.dispatch.rank`, `phase_c.dispatch.service_order`,
`phase_c.tdd.owed`, `phase_c.crash.localized`). No GPU, no cargo.
`--write` stores the reconstructed inputs as JSON for
`tests/test_dispatch_rank.py`.

What is reconstructed, not recorded: the static order (batch order, then the
service-order list, which the driver prints in rank order, then the name),
the owed tier (the narrowest tier the owed event gives, clamped at 0 as the
driver clamps it), and the crash grade (each `crash.localized` culprit of the
sweep as `SITED`). An exclusion note is not membership. The standing-flat
exclusion notes a unit and the owed admission then puts every owed unit
back, and the note stays on the event. A unit in the batch, the unclaimed
map, the held set, the owed set, or named at a rank position stays on the
frontier. A noted unit in none of those is off it. Every unit a batch
admits is treated as claimed.

HOW TO INTERPRET THE OUTPUT
---------------------------
    frontier F held H owed O targets T bound B
        targets = never-served units that are neither held, owed nor crash
        culprits; bound = ceil(|EVIDENCED| / max(cap - H, 1)) under the new
        rule.
    <arm> sweep N: targets offered k/T; owed offered m/O
        `pre-change` is the frozen old key, `shipped` is the current one.
    never offered after N sweeps: the targets no batch admitted.

On-theory: `shipped` offers every target within the bound and every owed
unit too. `pre-change` offers 0 targets for as long as the owed units fill the
free slots. A shipped arm that leaves a target unoffered past the bound, or
starves the owed units instead, is off-theory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.domain.dispatch_rank import (  # noqa: E402
    Crash,
    RankClass,
    Service,
    UnitFacts,
    next_streak,
    rotation_bound,
    take,
)

_EVENTS = ("phase_c.dispatch.rank", "phase_c.dispatch.service_order", "phase_c.tdd.owed", "phase_c.crash.localized")


def read_inputs(run_id: str, iter_n: int) -> dict:
    """The sweep's rank inputs, reconstructed from its events."""
    got: dict[str, dict] = {}
    culprits: set[str] = set()
    with (ROOT / "logs" / "events.jsonl").open() as fh:
        for line in fh:
            if run_id not in line or not any(e in line for e in _EVENTS):
                continue
            ev = json.loads(line)
            data = ev.get("data") or {}
            if ev.get("run_id") != run_id or data.get("iter") != iter_n:
                continue
            if ev["event"] == "phase_c.crash.localized":
                culprits.update(data.get("culprits") or ())
            else:
                got[ev["event"]] = data
    rk = got["phase_c.dispatch.rank"]
    so = got["phase_c.dispatch.service_order"]
    ow = got.get("phase_c.tdd.owed") or {"owed": [], "tests": {}}
    tier: dict[str, int] = {}
    for row in (ow.get("tests") or {}).values():
        for u in row.get("owed") or {}:
            tier[u] = min(tier.get(u, 1 << 30), max(row.get("owed_tier", 0), 0))
    # The event's withheld note survives the owed admission that puts the
    # unit back on the frontier. Membership is the batch, the unclaimed
    # map (ranked units whose streak is already positive), the held set,
    # the owed set, and a rank-position reason. A note on anyone else is
    # a unit the frontier did not take back.
    noted = {u for u, r in rk["off_batch"].items() if str(r).startswith("withheld")} | set(rk.get("withheld") or {})
    admitted = (
        set(rk["batch"])
        | set(rk["unclaimed"])
        | set(rk.get("held") or {})
        | set(ow.get("owed") or ())
        | {u for u, r in rk["off_batch"].items() if str(r).startswith("rank ")}
    )
    order = list(dict.fromkeys([*rk["batch"], *so["never_served"], *sorted(rk["unclaimed"]), *sorted(ow["owed"])]))
    frontier = [u for u in order if u not in noted or u in admitted]
    return {
        "run_id": run_id,
        "iter": iter_n,
        "frontier": frontier,
        "held": {u: n for u, n in rk["held"].items() if u in frontier},
        "owed": {u: tier.get(u, 0) for u in ow["owed"] if u in frontier},
        "never_served": [u for u in so["never_served"] if u in frontier],
        "flat": [u for u in so.get("flat") or () if u in frontier],
        "sited": sorted(u for u in culprits if u in frontier),
        "unclaimed": {u: n for u, n in rk["unclaimed"].items() if u in frontier},
        "recorded_batch": rk["batch"],
    }


def facts_of(inp: dict, streak: dict[str, int]) -> dict[str, UnitFacts]:
    never, flat = set(inp["never_served"]), set(inp["flat"])
    return {
        u: UnitFacts(
            static_index=i,
            service=(Service.NEVER if u in never else Service.FLAT if u in flat else Service.SERVED),
            held_tests=inp["held"].get(u, 0),
            crash=Crash.SITED if u in inp["sited"] else None,
            owed_tier=inp["owed"].get(u),
            unclaimed=streak.get(u, 0),
        )
        for i, u in enumerate(inp["frontier"])
    }


def pre_change_key(f: UnitFacts) -> tuple[int, ...]:
    """Frozen copy of the key before never-served units joined `EVIDENCED`:
    class on owed or fault site only, raw crash ordinal, un-owed tier 0."""
    if f.held_tests > 0:
        return (0, -f.unclaimed, -f.held_tests, f.static_index)
    if f.owed_tier is not None or f.crash in (Crash.BLAMED, Crash.SITED):
        crash = int(f.crash) if f.crash is not None else len(Crash)
        return (1, -f.unclaimed, crash, f.owed_tier or 0, int(f.service), f.static_index)
    return (2, -f.unclaimed, 0 if f.crash is Crash.CHAIN else 1, int(f.service), f.static_index)


def replay(inp: dict, *, cap: int, sweeps: int, shipped: bool) -> list[tuple[str, ...]]:
    """The batches of `sweeps` sweeps from the recorded streaks, every
    admitted unit claimed."""
    streak = dict(inp["unclaimed"])
    held = set(inp["held"])
    out: list[tuple[str, ...]] = []
    for _ in range(sweeps):
        facts = facts_of(inp, streak)
        if shipped:
            ranked = sorted(facts, key=lambda k: (facts[k].key(), k))
        else:
            ranked = sorted(facts, key=lambda k: (pre_change_key(facts[k]), k))
        batch = take(ranked, held, cap, cohort=None, traversed=()).keys
        out.append(batch)
        streak = next_streak(streak, inp["frontier"], set(batch) - held)
    return out


def targets_of(inp: dict) -> list[str]:
    taken = set(inp["held"]) | set(inp["owed"]) | set(inp["sited"])
    return [u for u in inp["never_served"] if u not in taken]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", default="3163270-ca5a787c")
    ap.add_argument("iter", nargs="?", type=int, default=2)
    ap.add_argument("--cap", type=int, default=12)
    ap.add_argument("--sweeps", type=int, default=10)
    ap.add_argument("--write")
    a = ap.parse_args()
    inp = read_inputs(a.run_id, a.iter)
    targets = targets_of(inp)
    facts = facts_of(inp, inp["unclaimed"])
    evidenced = sum(1 for f in facts.values() if f.rank_class is RankClass.EVIDENCED)
    bound = rotation_bound(evidenced, len(inp["held"]), a.cap)
    print(
        f"{a.run_id} iter {a.iter}: frontier {len(inp['frontier'])} held "
        f"{len(inp['held'])} owed {len(inp['owed'])} targets {len(targets)} "
        f"evidenced {evidenced} bound {bound}"
    )
    for name, shipped in (("pre-change", False), ("shipped", True)):
        batches = replay(inp, cap=a.cap, sweeps=a.sweeps, shipped=shipped)
        seen: set[str] = set()
        for n, b in enumerate(batches, 1):
            seen |= set(b)
            if n in (1, 2, bound, a.sweeps):
                print(
                    f"  {name} sweep {n}: targets offered "
                    f"{len(set(targets) & seen)}/{len(targets)}; owed offered "
                    f"{len(set(inp['owed']) & seen)}/{len(inp['owed'])}"
                )
        missing = sorted(set(targets) - seen)
        print(f"  {name} never offered after {a.sweeps} sweeps: {len(missing)} {missing[:6]}")
    if a.write:
        Path(a.write).write_text(json.dumps(inp, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {a.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
