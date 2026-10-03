#!/usr/bin/env python3
"""Which cursor-identity rule would have preserved the pass cursor, measured.

WHY THIS EXISTS
---------------
`cursor_mismatch` (pipeline_core/domain/tdd_floor.py) drops the open pass's
cursor whenever `code_id` moves, and `code_identity` hashes pipeline.py plus
EVERY .py under pipeline_core. So editing a log line drops the cursor as surely
as rewriting `order_frontier` does. The measured cost, 2026-08-17 -> 08-20:
sixteen code identities in four days, each restarting the escalation crawl at 0
of 43 units, in a period whose whole purpose was to take ONE reading — the sweep
after a complete 43-unit pass. The same code ran twice exactly once (runs
`396995` + `626784`) and the pass completed in about five GPU-hours.

Three candidate rules were on the table and the argument for each is
symmetrical, so the choice is made by measurement rather than by taste:

  R1  cluster + code_id                    — today's rule
  R2  cluster + dispatch_identity          — a hash over only the modules that
                                             determine which units the frontier
                                             holds and in what order
  R3  cluster only                         — leaning on `take_batch`, which
                                             intersects the carried cursor with
                                             the LIVE frontier, so a stale key
                                             is dropped rather than obeyed

R2's risk is that the modules any honest `dispatch_identity` must contain are
exactly the modules a fix session touches (last session's three moves were the
retro gate, the dispatch loop and route selection — all three inside it). R3's
risk is the mirror: a fix that makes a previously-failed attempt worth retrying
has its retry delayed to the next pass. This script measures both.

USAGE
-----
    python scripts/cursor_identity_replay.py [--project anyhow]
        [--since 2026-08-17] [--until 2026-08-21] [--cluster stable]
        [--events logs/events.jsonl] [--json OUT.json]

No GPU, no launch, no workspace mutation: it reads `logs/pipeline_<project>.log*`
for the `PIPELINE STARTED` lines (run_id, code_id, git sha) and streams
`logs/events.jsonl` for `phase_c.tdd.iter`. `git diff --name-only A B` is the
only subprocess and it is read-only. A full 234 MB event store takes ~30 s.

HOW TO INTERPRET THE OUTPUT
---------------------------
One block per rule, over the transitions in the window. A "transition" is a
consecutive pair of runs on the same cluster whose `code_id` differs — i.e.
exactly the events where today's rule throws the cursor away.

  preserved        how many of those transitions the rule would have KEPT the
                   cursor across. R1 is 0 by construction; R3 is all of them.
  units_carried    summed over preserved transitions: how many already-attempted
                   units the relaunch would not have re-dispatched. This is the
                   saving, in model calls, and it is the number 198.2's cost is
                   made of.
  delay_p50/p90    over preserved transitions: how many iterations the resumed
                   run took to CLOSE the pass it inherited. This is the cost —
                   an attempt the code change made worth retrying waits this
                   long before the pass reopens and offers it again. It is an
                   UPPER bound: every recorded run opened its first pass with
                   an empty cursor, so it walked a whole cohort where a carried
                   cursor would walk the remainder.
  within one       of those, how many had that delay inside ONE traversal of
  traversal        their OWN cohort, `ceil(frontier / dispatch cap)` read off
                   the run's first iteration. This is the claim being tested —
                   "a retry is delayed by at most the rest of the open pass" —
                   and it is per-transition because the cohort is not a
                   constant (28 at cap 12 is 3 iterations, 59 is 5).
  unrecoverable    transitions where R2 cannot be evaluated because both runs
                   report the same git sha — the change was uncommitted, which
                   is the normal state here. R2's `preserved` is therefore an
                   UPPER bound: an uncommitted edit to a hashed module is
                   invisible to this script and would have dropped the cursor.

  dropped by       R2 only: which module of the hash set appeared in the git
                   diff of a dropping transition, most frequent first. A module
                   at or near 100 % is the finding — it means R2 collapses to
                   R1 and the module list is not the lever.

THE DECISION RULE, PRE-REGISTERED
---------------------------------
Ship R2 if its preserved fraction is >= 0.30 over the window AND no single
module accounts for > 80 % of its drops. Otherwise ship R3, provided its
retry delay stays inside one traversal of the run's own cohort on >= 90 % of
preserved transitions. If neither passes, ship nothing: the
finding is that the cursor's LIFETIME, not its identity, is the wrong shape.
The rule is written here, in the script, so the result cannot be read backwards
into the conclusion that motivated the work.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# The modules any honest `dispatch_identity` must contain: they decide which
# units the frontier holds, in what order it is walked, which route answers a
# unit, and which unit is skipped before it is ever offered.
DISPATCH_MODULES = (
    "pipeline_core/application/reconstruct/scheduling.py",   # order_frontier
    "pipeline_core/domain/dispatch_batch.py",                # take_batch, cohort
    "pipeline_core/domain/dispatch.py",
    "pipeline_core/domain/impl_routing.py",                  # route selection
    "pipeline_core/domain/retro_gate.py",                    # the skip decision
    "pipeline_core/adapters/retros.py",
    "pipeline_core/application/reconstruct/phase_c_tdd.py",  # eligibility filter
)

_START = re.compile(
    r"PIPELINE STARTED — (?P<ts>[\d-]+ [\d:]+) UTC\s+"
    r"pipeline\.py=(?P<py>\w+)\s+code=(?P<code>\w+)\s+git=(?P<git>\w+)\s+"
    r"run_id=(?P<run>[\w-]+)")


@dataclass(frozen=True, slots=True)
class RunStart:
    run_id: str
    code_id: str
    git: str
    ts: str


@dataclass
class RunIters:
    """One (run, cluster) pair's iteration stream, in arrival order."""

    run_id: str
    cluster: str
    first_ts: str = ""
    iters: list[dict] = field(default_factory=list)

    @property
    def cursor_at_exit(self) -> int:
        """Units the OPEN pass had dispatched when the run stopped — what a
        carried cursor would hand the next process. Everything dispatched
        since the last iteration that closed a pass."""
        total = 0
        for it in self.iters:
            if it.get("pass_completed"):
                total = 0
            else:
                total += int(it.get("dispatched") or 0)
        return total

    @property
    def iters_to_pass_close(self) -> int:
        """How many iterations this run took to close its FIRST pass — the
        delay a retry deferred to the next pass would actually wait.

        An UPPER bound on the delay under R3, and deliberately so: every run in
        the record ran under R1, i.e. it opened its first pass with an EMPTY
        cursor and had to walk the whole cohort. A carried cursor makes that
        first pass the REMAINDER of the previous one, which is shorter by the
        units already attempted. The measured number can only overstate the
        wait."""
        for n, it in enumerate(self.iters, start=1):
            if it.get("pass_completed"):
                return n
        return len(self.iters)

    @property
    def traversal_len(self) -> int:
        """`ceil(cohort / dispatch cap)` for this run's first pass, from the
        frontier it opened on and the batch it took — the length of ONE full
        traversal, which is the bound the delay is judged against. Per-run,
        because the cohort is not a constant: 28 at cap 12 is 3 iterations,
        59 at cap 12 is 5, and pinning the comparison to one crate-week's 43
        would compare a run against another run's cohort."""
        for it in self.iters:
            frontier = int(it.get("frontier_size") or 0)
            cap = int(it.get("dispatched") or 0)
            if frontier and cap:
                return -(-frontier // cap)
        return 0


def read_run_starts(log_glob: list[Path]) -> dict[str, RunStart]:
    out: dict[str, RunStart] = {}
    for p in log_glob:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _START.finditer(text):
            out[m["run"]] = RunStart(run_id=m["run"], code_id=m["code"],
                                     git=m["git"], ts=m["ts"])
    return out


def read_iters(events: Path, project: str) -> dict[tuple[str, str], RunIters]:
    out: dict[tuple[str, str], RunIters] = {}
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.tdd.iter"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("product") not in (project, None):
                continue
            run = str(ev.get("run_id") or "")
            data = ev.get("data") or {}
            cluster = str(data.get("cluster") or "")
            if not run or not cluster:
                continue
            key = (run, cluster)
            entry = out.get(key)
            if entry is None:
                entry = out[key] = RunIters(run_id=run, cluster=cluster,
                                            first_ts=str(ev.get("ts") or ""))
            entry.iters.append(data)
    return out


def git_changed(root: Path, a: str, b: str) -> list[str] | None:
    """Files that changed between two commits, or None when the pair cannot be
    resolved (unknown sha, shallow history, identical sha)."""
    if not a or not b or a == b:
        return None
    try:
        res = subprocess.run(["git", "-C", str(root), "diff", "--name-only",
                              a, b], capture_output=True, text=True,
                             timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]


@dataclass
class Transition:
    cluster: str
    prev: RunIters
    nxt: RunIters
    prev_start: RunStart
    next_start: RunStart
    changed: list[str] | None          # None = unrecoverable (same/unknown sha)

    @property
    def dispatch_modules_touched(self) -> list[str]:
        if self.changed is None:
            return []
        return [m for m in DISPATCH_MODULES if m in self.changed]


def build_transitions(starts: dict[str, RunStart],
                      streams: dict[tuple[str, str], RunIters],
                      root: Path, cluster_filter: str | None,
                      since: str | None, until: str | None) -> list[Transition]:
    by_cluster: dict[str, list[RunIters]] = {}
    for (run, cluster), stream in streams.items():
        if cluster_filter and cluster != cluster_filter:
            continue
        if run not in starts:
            continue          # a run whose log has rotated away
        ts = stream.first_ts[:10]
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        by_cluster.setdefault(cluster, []).append(stream)

    out: list[Transition] = []
    diff_cache: dict[tuple[str, str], list[str] | None] = {}
    for cluster, streams_in in by_cluster.items():
        streams_in.sort(key=lambda s: s.first_ts)
        for prev, nxt in zip(streams_in, streams_in[1:], strict=False):
            ps, ns = starts[prev.run_id], starts[nxt.run_id]
            if ps.code_id == ns.code_id:
                continue      # not a transition: the same code ran twice
            key = (ps.git, ns.git)
            if key not in diff_cache:
                diff_cache[key] = git_changed(root, ps.git, ns.git)
            out.append(Transition(cluster=cluster, prev=prev, nxt=nxt,
                                  prev_start=ps, next_start=ns,
                                  changed=diff_cache[key]))
    return out


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def report(transitions: list[Transition]) -> dict:
    total = len(transitions)
    rules: dict[str, dict] = {}

    for rule in ("R1", "R2", "R3"):
        preserved: list[Transition] = []
        unrecoverable = 0
        for t in transitions:
            if rule == "R1":
                continue
            if rule == "R3":
                preserved.append(t)
                continue
            if t.changed is None:
                unrecoverable += 1
                continue
            if not t.dispatch_modules_touched:
                preserved.append(t)
        carried = [t.prev.cursor_at_exit for t in preserved]
        delays = [t.nxt.iters_to_pass_close for t in preserved]
        bounded = [t for t in preserved
                   if t.nxt.traversal_len
                   and t.nxt.iters_to_pass_close <= t.nxt.traversal_len]
        rules[rule] = {
            "preserved": len(preserved),
            "transitions": total,
            "fraction": (len(preserved) / total) if total else 0.0,
            "units_carried": sum(carried),
            "delay_p50": _pct(delays, 0.5),
            "delay_p90": _pct(delays, 0.9),
            "delay_within_traversal": len(bounded),
            "delay_measurable": len([t for t in preserved
                                     if t.nxt.traversal_len]),
            "unrecoverable": unrecoverable,
        }

    dropped: Counter[str] = Counter()
    for t in transitions:
        for mod in t.dispatch_modules_touched:
            dropped[mod] += 1
    rules["R2"]["dropped_by"] = dropped.most_common()
    return rules


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--cluster", default=None)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    logs = sorted((root / "logs").glob(f"pipeline_{args.project}.log*"))
    starts = read_run_starts(logs)
    streams = read_iters(root / args.events, args.project)
    transitions = build_transitions(starts, streams, root, args.cluster,
                                    args.since, args.until)
    rules = report(transitions)

    window = f"{args.since or 'start'} .. {args.until or 'now'}"
    print(f"cursor-identity replay — project={args.project} window={window} "
          f"cluster={args.cluster or 'all'}")
    print(f"  run starts parsed: {len(starts)}   "
          f"(run, cluster) iteration streams: {len(streams)}")
    print(f"  code_id transitions in window: {len(transitions)}")
    if not transitions:
        print("  nothing to measure — widen the window or check the log glob.")
        return 1
    trav = None
    for name in ("R1", "R2", "R3"):
        r = rules[name]
        print(f"\n  {name}: preserved {r['preserved']}/{r['transitions']} "
              f"({r['fraction']:.0%})   units_carried={r['units_carried']}   "
              f"delay p50={r['delay_p50']} p90={r['delay_p90']}   "
              f"within one traversal {r['delay_within_traversal']}/"
              f"{r['delay_measurable']}"
              + (f"   unrecoverable={r['unrecoverable']}"
                 if r.get("unrecoverable") else ""))
        if name == "R2" and r["dropped_by"]:
            worst = r["dropped_by"][0]
            drops = sum(c for _, c in r["dropped_by"])
            print("     dropped by: " + ", ".join(
                f"{Path(m).name}×{c}" for m, c in r["dropped_by"]))
            print(f"     worst module accounts for {worst[1]}/{drops} "
                  f"({worst[1] / drops:.0%}) of R2 drops"
                  if drops else "")
        if name == "R3":
            trav = (r["delay_within_traversal"], r["delay_measurable"])

    r2 = rules["R2"]
    drops = sum(c for _, c in r2["dropped_by"])
    worst_share = (r2["dropped_by"][0][1] / drops) if drops else 0.0
    print("\n  DECISION (rule pre-registered in this script's header):")
    if r2["fraction"] >= 0.30 and worst_share <= 0.80:
        print("    -> R2: dispatch_identity. Preserved fraction clears 0.30 "
              "and no single module dominates its drops.")
    else:
        print(f"    -> NOT R2 (preserved {r2['fraction']:.0%}, worst module "
              f"{worst_share:.0%} of drops).")
        within, measurable = trav or (0, 0)
        share = (within / measurable) if measurable else 0.0
        if share >= 0.90:
            print(f"    -> R3: cluster only. The retry delay stays inside one "
                  f"traversal on {within}/{measurable} ({share:.0%}) of "
                  "preserved transitions, and that measurement is an upper "
                  "bound (every recorded run opened its pass with an empty "
                  "cursor).")
        else:
            print(f"    -> NEITHER: R3's delay exceeds one traversal on "
                  f"{measurable - within}/{measurable} transitions. The "
                  "finding is that the cursor's LIFETIME, not its identity, "
                  "is the wrong shape — ship nothing and say so.")

    if args.json:
        Path(args.json).write_text(json.dumps(rules, indent=2), encoding="utf-8")
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
