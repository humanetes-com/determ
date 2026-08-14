#!/usr/bin/env python3
"""Pattern census over the anyhow run logs — which stall-pattern classes each
run hit, whether any mechanism reacted, and what lever it took (or lacked).

WHY THIS EXISTS.  The pipeline's stall defenses were accreted one live failure
at a time, each with its own bespoke detector.  Before consolidating them into
one generic detector + a lever vocabulary (the "diagnostician" design,
2026-08-14), we need the evidence: which pattern classes actually recur across
runs, and which instances went SILENT (no mechanism printed a reaction) versus
REACTED.  The classes and their defining instances:

  P1 destroyed-baseline lock — a retry gate compares progress to a high-water
     taken before a deliberate destruction; the unlock needs the progress the
     destruction removed.  Run 259637-36db9aad (2026-08-07): `NOT re-admitting`
     against 81 for 9 hours after a re-stub dropped the tree to 21.
  P2 blocked-majority spend — an iteration spends its model calls on candidates
     whose tests are blocked by still-stubbed siblings, instead of on the
     blockers.  Run 259637-36db9aad final iters: 17 of 17 kept-blocked;
     run 298540-f792975a: 3.73 call-hours burned the same way.
  P3 futile aim — the same action is re-aimed at the same target with zero
     payoff: a component re-authored again for a crash its last re-author
     already failed to clear, or an identical producer-escalation set emitted
     sweep after sweep while the culprit is structurally invisible to the scan.
     Runs 58819-ee78a1d1 (Quoted re-fired attempt 2 on the same crash),
     635043-5f7c61f5 (frame-blind UAF, blame could never land),
     929616-ee20d540 (chain tests escalated to the same five constructors
     every sweep; the severing `source` impl in none of them).
  P4 poisoned evidence — a stored conviction/guidance whose premise the tree
     outgrew steers every later attempt into the same wrong body.  Live
     instance: `context.rs::*impl*::StdErrorforContextError<C,Error>` guidance
     "the only body-only source is `None`" (2026-08-13/14), refuted by the
     frozen tree, gated out only on 2026-08-14.
  P5 limit cycle — the score revisits the same small value set with
     destructions in between (79/74/79/74 across runs 58819-ee78a1d1 and
     929616-ee20d540).

USAGE.
  python scripts/pattern_census.py [LOG ...] [--min-iters N] [--json OUT]
    LOG ...      log files in CHRONOLOGICAL order.  Default: the rotation
                 logs/pipeline_anyhow.log.10 .. .1 then logs/pipeline_anyhow.log
                 (only those that exist).  A run that spans a rotation boundary
                 is stitched back together by concatenating in this order.
    --min-iters  only report runs with at least N iteration summaries
                 (default 2; launches that died in setup are skipped).
    --json OUT   also write the full census (all runs, all evidence refs) as
                 JSON for downstream tooling.
  Read-only: no GPU, no model calls, no writes outside --json OUT.
  Cross-references logs/perf_ledger.jsonl for the operator note on each run id
  when that file exists.

HOW TO INTERPRET THE OUTPUT.  One block per qualifying run:
  run <id>  git=<sha> code=<hash>  <start time>  iters=<n>  <cluster>:pass
      trajectory and final/max pass — the score context for the hits below.
  P<k> HIT   <one-line evidence summary>  [file:line, ...]
      the class fired on this run; the refs are the log lines to read.
  P<k> hit + REACTED(<marker>) — a mechanism printed a reaction (the named
      marker line).  A reacted hit is a pathology the pipeline already answers;
      a SILENT hit is one it does not — those rows are the backlog for the
      generic detector, and the per-class SILENT count in the final aggregate
      is the priority order.
  terminal: how the run ended (death-watch exit/signal, plateau abort,
      traceback), plus the perf-ledger note when one exists.
The aggregate at the bottom counts, per class, total hits and how many were
SILENT.  Decision rule: a class with recurring SILENT hits needs its detector
and lever BEFORE any new bespoke mechanism is added; a class with only REACTED
hits is already owned and its mechanism is a consolidation candidate, not a
gap.  Healthy runs (score climbs, no hits) are listed at the end as negative
controls — a detector that fires on them is miscalibrated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------- line shapes

RE_STARTED = re.compile(
    r"PIPELINE STARTED — (?P<ts>[\d: -]+UTC)?.*?"
    r"(?:code=(?P<code>[0-9a-f]+))?\s*(?:git=(?P<git>[0-9a-f~]+))?\s*"
    r"run_id=(?P<run>[0-9a-f-]+)")
RE_ITER = re.compile(
    r"(?:\[(?P<cluster>\w+)\] )?\[phase-c/tdd\] iter (?P<iter>\d+): cargo test"
    r" rc=\S+ \([^)]*\); pass=(?P<passed>\d+) fail=(?P<fail>\d+)"
    r" oracle=(?P<oracle>\d+) \(improved=(?P<improved>\w+),"
    r" stall=(?P<stall>\d+)/(?P<stall_cap>\d+)\);"
    r" frontier=(?P<frontier>\d+) fn\(s\)[^,]*, (?P<exhausted>\d+) skipped")
# A retrigger's `continue` swallows the iteration summary, so a fired
# iteration's reading survives only on its binaries line (same recovery
# tools/distill_walk.py performs).  `X/Y` per binary is passed/failed.
RE_BINARIES = re.compile(
    r"\[phase-c/tdd\] iter (?P<iter>\d+) binaries: (?P<body>.*)")
RE_BIN_PAIR = re.compile(r" (\d+)/(\d+)(?:,|$)")
RE_NOT_READMIT = re.compile(
    r"moved pass no further than (?P<base>\d+) — NOT re-admitting")
RE_REBASE = re.compile(r"high-water re-based (?P<old>\d+) -> (?P<new>\d+)")
RE_RESTUB = re.compile(
    r"reverted (?P<n>\d+) (?:referencing )?fn\(s\) to re-fill")
RE_FIRED = re.compile(
    r"\[layout-retrigger\] \w+/(?P<comp>\[[^]]*\]): re-authored successfully"
    r" \(attempt (?P<attempt>\d+), cause=(?P<cause>\w+)\)")
RE_REJECTED = re.compile(
    r"re-author was not accepted \(rejection (?P<k>\d+)/(?P<cap>\d+)")
RE_SHRINK = re.compile(r"oracle shrink reproduced on immediate re-sweep")
RE_ESCALATE = re.compile(
    r"\[attribution/escalate\] (?P<test>\S+): accessors of (?P<accs>\[[^]]*\])"
    r" are futile — escalating to (?P<n>\d+) producer fn\(s\): (?P<head>[^(]*)")
RE_NOBODY = re.compile(r"blam(?:ing|ed) NOBODY")
RE_KEPT_BLOCKED = re.compile(
    r"kept (?P<fn>src/\S+) .*blocked by still-stubbed sibling")
RE_FILLED = re.compile(r"\d+/\d+ filled (?P<fn>src/[^\s]+)")
RE_EXH_CONVICTED = re.compile(
    r"skipped (?P<fn>src/\S+) \[[^]]*\] — fully exhausted across iters"
    r" \(retrospective marked")
RE_REVERTED_AFTER = re.compile(
    r"(?P<fn>src/\S+): reverted after \d+ attempt\(s\)")
RE_DEATH = re.compile(
    r"\[death-watch\] pipeline \d+ ended after (?P<dur>\S+) —"
    r" exit=(?P<exit>\d+)(?: signal=(?P<sig>\w+))?")

# Reaction markers: a line matching one of these is the pipeline ANSWERING a
# pattern (the value is the short name shown as REACTED(<name>)).  Grouped by
# the class whose hit they answer.
REACTIONS: dict[str, list[tuple[str, str]]] = {
    "P1": [
        # NOTE: the refusal line itself reads "NOT re-admitting exhausted",
        # so the positive marker must be the full positive phrasing.
        ("high-water re-based", "rebase"),
        ("previously-exhausted fn(s)/block(s)", "readmit"),
    ],
    "P2": [
        # Only the pre-fix-loop keep-and-defer counts: the older consumer
        # deferral line ("deferred N consumer(s) blocked by still-stubbed
        # producer(s)") reacts AFTER the model calls were already spent, which
        # is the burn itself, not an answer to it.
        ("sibling-blocked", "keep-and-defer"),
        ("salvaged_body_kept", "salvage"),
    ],
    "P3": [
        ("already spent on this exact crash", "sig-refuted"),
        ("walk stops here and the stall exit stands", "walk-closed"),
        ("traversal step(s) the futile accessors dispatch", "traversal"),
        ("CHAIN-DERIVED blame", "chain-blame"),
    ],
    "P4": [
        ("rejection(s) retired — they were earned under", "epoch-retire"),
        ("retro-gate: honoured=", "retro-gate"),  # weakest: gate ran at all
    ],
    "P5": [
        ("zero-payoff", "payoff"),
        ("refuted for this signature", "sig-refuted"),
    ],
}

TERMINALS = ("MODULE_PLATEAU_ABORT", "VERDICT MISMATCH", "TIMED OUT",
             "Traceback (most recent call last)")


def default_logs() -> list[Path]:
    root = Path(__file__).resolve().parent.parent / "logs"
    order = [root / f"pipeline_anyhow.log.{i}" for i in range(10, 0, -1)]
    order.append(root / "pipeline_anyhow.log")
    return [p for p in order if p.exists()]


def load_ledger_notes(root: Path) -> dict[str, str]:
    path = root / "logs" / "perf_ledger.jsonl"
    if not path.exists():
        return {}
    notes = {}
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("run_id") and row.get("note"):
            notes[row["run_id"]] = row["note"]
    return notes


# ------------------------------------------------------------------ the model


class Run:
    def __init__(self, run_id: str, git: str, code: str, ts: str, ref: str):
        self.run_id, self.git, self.code, self.ts = run_id, git, code, ts
        self.start_ref = ref
        self.lines: list[tuple[str, str]] = []   # (ref "file:lineno", text)

    # ---- extraction ----
    def iters(self) -> list[dict]:
        """Sweep readings in log order.  A summary line is a full reading; a
        binaries line whose summary never arrived (the retrigger's `continue`
        swallowed it) is recovered as a synthesized reading — pass = sum of
        per-binary passed counts, other fields absent.  Iter numbers restart
        after fires, so ORDER is the trajectory, not the numbers."""
        out: list[dict] = []
        pending: dict | None = None   # binaries reading awaiting its summary
        for ref, text in self.lines:
            if (m := RE_ITER.search(text)):
                d = {k: (int(v) if v.isdigit() else v)
                     for k, v in m.groupdict(default="").items()}
                d["ref"] = ref
                if pending and pending["iter"] == d["iter"]:
                    pending = None          # the summary made it out
                elif pending:
                    out.append(pending)     # swallowed: keep the recovery
                    pending = None
                out.append(d)
            elif (m := RE_BINARIES.search(text)):
                if pending:
                    out.append(pending)
                passed = sum(int(a) for a, _b in
                             RE_BIN_PAIR.findall(m["body"]))
                pending = {"iter": int(m["iter"]), "passed": passed,
                           "cluster": "", "recovered": True, "ref": ref}
        if pending:
            out.append(pending)
        return out

    def find(self, regex: re.Pattern) -> list[tuple[str, re.Match]]:
        return [(ref, m) for ref, text in self.lines
                if (m := regex.search(text))]

    def reactions_for(self, cls: str, after: int = 0) -> list[tuple[str, str]]:
        """Reaction markers AT OR AFTER line index `after` (the first evidence
        line of the hit).  A reaction printed before the pathology set in is
        not an answer to it — run 259637-36db9aad re-admitted fns in its early
        iterations and then locked for nine hours; counting the early line
        would tag the lock REACTED."""
        hits = []
        for needle, name in REACTIONS.get(cls, []):
            for i, (ref, text) in enumerate(self.lines):
                if i >= after and needle in text:
                    hits.append((name, ref))
                    break
        return hits


def split_runs(paths: list[Path]) -> list[Run]:
    runs: list[Run] = []
    cur: Run | None = None
    for path in paths:
        for i, text in enumerate(path.read_text(errors="replace")
                                 .splitlines(), 1):
            ref = f"{path.name}:{i}"
            m = RE_STARTED.search(text)
            if m:
                cur = Run(m["run"], m["git"] or "?", m["code"] or "?",
                          (m["ts"] or "?").strip(), ref)
                runs.append(cur)
            elif cur is not None:
                cur.lines.append((ref, text))
    return runs


# ------------------------------------------------------------------ detectors
# Each detector returns a list of hit dicts: {"evidence": str, "refs": [...]}.
# Empty list = the class did not fire on this run.


def detect_p1(run: Run, iters: list[dict]) -> list[dict]:
    """Destroyed-baseline lock: repeated `NOT re-admitting` against the same
    baseline B, after a deliberate re-stub, while no sweep between the first
    and last refusal reaches B.  A `high-water re-based` line BEFORE the first
    refusal means the baseline was told about the destruction — that is the
    reaction, not the pathology."""
    refusals = run.find(RE_NOT_READMIT)
    restubs = run.find(RE_RESTUB)
    if not refusals or not restubs:
        return []
    order = {ref: i for i, (ref, _t) in enumerate(run.lines)}
    by_base: dict[int, list[str]] = defaultdict(list)
    for ref, m in refusals:
        by_base[int(m["base"])].append(ref)
    hits = []
    for base, refs in by_base.items():
        if len(refs) < 2:
            continue
        # Only sweeps AFTER the first refusal count: a pre-destruction reading
        # equal to B is exactly the stale high-water the class is about.
        first = min(order.get(r, 0) for r in refs)
        later = [d["passed"] for d in iters if order.get(d["ref"], 0) > first]
        if later and max(later) >= base:
            continue   # the run did reach B again: the gate was satisfiable
        hits.append({
            "evidence": f"{len(refs)} refusal(s) against baseline {base}; "
                        f"max pass after the first refusal "
                        f"{max(later) if later else '?'} (< {base}); "
                        f"{len(restubs)} deliberate re-stub(s)",
            "refs": refs[:4]})
    return hits


def detect_p2(run: Run, iters: list[dict]) -> list[dict]:
    """Blocked-majority spend: an iteration whose kept-blocked outcomes cover
    most of the frontier the closing summary reports — the spend went to fns
    whose tests cannot run.  Calibrated on run 259637-36db9aad's tail
    (pipeline_anyhow.log.3): spans of 31 and 15 kept-blocked against
    frontiers of ~17-18, the '17 of 17 kept' instance."""
    spans: list[dict] = [{"kept": [], "filled": 0, "frontier": None}]
    for ref, text in run.lines:
        if (m := RE_ITER.search(text)):
            spans[-1]["frontier"] = int(m["frontier"])
            spans.append({"kept": [], "filled": 0, "frontier": None})
        elif (m := RE_KEPT_BLOCKED.search(text)):
            spans[-1]["kept"].append(ref)
        elif RE_FILLED.search(text):
            spans[-1]["filled"] += 1
    hits = []
    for span in spans:
        k, fr = len(span["kept"]), span["frontier"]
        if fr is not None and fr >= 5 and k >= 3 and k >= 0.7 * fr:
            hits.append({
                "evidence": f"{k} kept-blocked vs frontier {fr} "
                            f"({span['filled']} filled) in one iteration",
                "refs": span["kept"][:4]})
    return hits[:3]


def detect_p3(run: Run, iters: list[dict]) -> list[dict]:
    """Futile aim, two shapes.  (a) the same component re-authored ≥2 times
    while the crash class persists (an oracle-shrink line appears after the
    last fire).  (b) the same test escalated to an identical producer set on
    ≥3 sweeps."""
    hits = []
    fires: dict[str, list[str]] = defaultdict(list)
    for ref, m in run.find(RE_FIRED):
        fires[m["comp"]].append(ref)
    shrink_refs = [ref for ref, _ in run.find(RE_SHRINK)]
    # "after" must be judged on line ORDER, never on the ref strings: a ref is
    # "file:lineno", which sorts neither within a file ("log.1:998" >
    # "log.1:1500") nor across the rotation (log.1 is the NEWEST file but
    # sorts before log.10).  The string compare cost two limit-cycle runs
    # their P3 rows (1135197-c811b168, 2846-58cbbf3b: re-fired components
    # with the crash still reproducing, silently dropped).
    order = {ref: i for i, (ref, _t) in enumerate(run.lines)}
    for comp, refs in fires.items():
        if (len(refs) >= 2 and shrink_refs
                and order.get(shrink_refs[-1], -1) > order.get(refs[-1], -1)):
            hits.append({
                "evidence": f"component {comp} re-authored {len(refs)}x, "
                            "crash class still reproducing after the last fire",
                "refs": refs[:4]})
    esc: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ref, m in run.find(RE_ESCALATE):
        esc[(m["test"], m["head"].strip())].append(ref)
    stuck = {t for (t, _h), refs in esc.items() if len(refs) >= 3}
    if stuck:
        refs = [r for (t, _h), rs in esc.items() if t in stuck for r in rs]
        hits.append({
            "evidence": f"{len(stuck)} test(s) escalated to an identical "
                        f"producer set on >=3 sweeps: {sorted(stuck)[:4]}",
            "refs": refs[:4]})
    # (c) frame-blind crash: the localizer looked and blamed nobody at least
    # twice while the shrink kept reproducing — blame structurally cannot
    # land, so every localizer run is the same action with zero payoff.
    nobody = [ref for ref, _ in run.find(RE_NOBODY)]
    if len(nobody) >= 2 and shrink_refs:
        hits.append({
            "evidence": f"localizer blamed NOBODY {len(nobody)}x with the "
                        "crash reproducing deterministically",
            "refs": nobody[:4]})
    return hits


def detect_p4(run: Run, iters: list[dict]) -> list[dict]:
    """Poisoned evidence (proxy): the same unit either skipped-as-exhausted
    under a standing conviction on ≥3 iterations, or reverted-after-attempts
    ≥3 times — the attempts keep converging to the recorded verdict."""
    hits = []
    for regex, what in ((RE_EXH_CONVICTED, "skipped under conviction"),
                        (RE_REVERTED_AFTER, "reverted after attempts")):
        per_fn: dict[str, list[str]] = defaultdict(list)
        for ref, m in run.find(regex):
            per_fn[m["fn"].rstrip(":")].append(ref)
        for fn, refs in sorted(per_fn.items(), key=lambda kv: -len(kv[1])):
            if len(refs) >= 3:
                hits.append({
                    "evidence": f"{fn} {what} {len(refs)}x",
                    "refs": refs[:4]})
    return hits[:4]


def detect_p5(run: Run, iters: list[dict]) -> list[dict]:
    """Limit cycle: some pass value recurs ≥3 times with dips below it in
    between, and at least one retrigger fire in the run (the cycle is driven
    by deliberate destruction, not by noise)."""
    passes = [(d["passed"], d["ref"]) for d in iters]
    fires = run.find(RE_FIRED)
    if len(passes) < 4 or len(fires) < 2:
        return []
    hits = []
    for v in sorted({p for p, _ in passes}, reverse=True):
        idx = [i for i, (p, _) in enumerate(passes) if p == v]
        if len(idx) < 2:
            continue
        dips = sum(1 for a, b in zip(idx, idx[1:], strict=False)
                   if any(passes[i][0] < v for i in range(a + 1, b)))
        # zero net progress: after cycling, the run never gets above v
        if dips >= 1 and max(p for p, _ in passes[idx[-1]:]) <= v:
            hits.append({
                "evidence": f"pass returns to {v} ({len(idx)} visits, "
                            f"{dips} dip(s) between) and never exceeds it; "
                            f"{len(fires)} retrigger fire(s) this run",
                "refs": [passes[i][1] for i in idx[:4]]})
            break   # the highest cycling value is the finding
    return hits


DETECTORS = {"P1": detect_p1, "P2": detect_p2, "P3": detect_p3,
             "P4": detect_p4, "P5": detect_p5}
CLASS_NAMES = {
    "P1": "destroyed-baseline lock", "P2": "blocked-majority spend",
    "P3": "futile aim", "P4": "poisoned evidence", "P5": "limit cycle"}


# -------------------------------------------------------------------- report


def census(paths: list[Path], min_iters: int) -> tuple[list[dict], dict]:
    root = Path(__file__).resolve().parent.parent
    notes = load_ledger_notes(root)
    out = []
    all_runs = split_runs(paths)
    for run in all_runs:
        iters = run.iters()
        row: dict = {
            "run_id": run.run_id, "git": run.git, "code": run.code,
            "started": run.ts, "start_ref": run.start_ref,
            "iterations": len(iters),
            "trajectory": [d["passed"] for d in iters],
            "clusters": sorted({d["cluster"] for d in iters if d["cluster"]}),
            "hits": {}, "reacted": {}, "terminal": [], "note": notes.get(
                run.run_id, ""),
        }
        order = {ref: i for i, (ref, _t) in enumerate(run.lines)}
        for cls, fn in DETECTORS.items():
            found = fn(run, iters)
            if found:
                row["hits"][cls] = found
                first = min((order.get(r, 0) for h in found
                             for r in h["refs"]), default=0)
                row["reacted"][cls] = run.reactions_for(cls, after=first)
        for ref, m in run.find(RE_DEATH):
            row["terminal"].append(
                f"ended after {m['dur']} exit={m['exit']}"
                + (f" signal={m['sig']}" if m["sig"] else "") + f" [{ref}]")
        for ref, text in run.lines:
            for t in TERMINALS:
                if t in text:
                    row["terminal"].append(f"{t} [{ref}]")
                    break
        row["terminal"] = row["terminal"][:3]
        if len(iters) >= min_iters:
            out.append(row)
    return out, chronics(all_runs)


def chronics(runs: list[Run]) -> dict[str, list[dict]]:
    """Cross-run recurrences — the pattern the per-run detectors cannot see.
    A pathology repaired live and reproduced by the NEXT run (the user's
    'restart from scratch and it fails again') shows up here as the same
    escalation set, the same convicted unit, or the same fired component
    recurring across distinct run ids.  Each row lists the runs, oldest
    first; >=2 runs qualifies because every restart between them was a fresh
    chance to behave differently."""
    esc: dict[tuple[str, str], list[str]] = defaultdict(list)
    conv: dict[str, list[str]] = defaultdict(list)
    fired: dict[str, list[str]] = defaultdict(list)
    nobody: dict[str, int] = defaultdict(int)
    for run in runs:
        seen_e, seen_c, seen_f = set(), set(), set()
        for _ref, m in run.find(RE_ESCALATE):
            key = (m["test"], m["head"].strip())
            if key not in seen_e:
                esc[key].append(run.run_id)
                seen_e.add(key)
        for _ref, m in run.find(RE_EXH_CONVICTED):
            fn = m["fn"].rstrip(":")
            if fn not in seen_c:
                conv[fn].append(run.run_id)
                seen_c.add(fn)
        for _ref, m in run.find(RE_FIRED):
            if m["comp"] not in seen_f:
                fired[m["comp"]].append(run.run_id)
                seen_f.add(m["comp"])
        if run.find(RE_NOBODY):
            nobody[run.run_id] = len(run.find(RE_NOBODY))
    return {
        "escalation": [
            {"what": f"{t} -> {h[:70]}", "runs": r}
            for (t, h), r in sorted(esc.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "conviction": [
            {"what": fn, "runs": r}
            for fn, r in sorted(conv.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "component": [
            {"what": comp, "runs": r}
            for comp, r in sorted(fired.items(), key=lambda kv: -len(kv[1]))
            if len(r) >= 2][:12],
        "frame_blind": [
            {"what": f"blamed NOBODY x{n}", "runs": [rid]}
            for rid, n in nobody.items()],
    }


def print_report(rows: list[dict]) -> None:
    agg: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    healthy = []
    for row in rows:
        traj = row["trajectory"]
        head = (f"run {row['run_id']}  git={row['git']} code={row['code']}  "
                f"{row['started']}  iters={row['iterations']}")
        print(head)
        if traj:
            print(f"  pass: {' '.join(str(p) for p in traj[:20])}"
                  + (" …" if len(traj) > 20 else "")
                  + f"  (max={max(traj)}, final={traj[-1]})")
        if not row["hits"]:
            healthy.append(row["run_id"])
            print("  no pattern hits (negative control)")
        for cls, found in row["hits"].items():
            reacted = row["reacted"].get(cls) or []
            tag = ("REACTED(" + ",".join(n for n, _ in reacted) + ")"
                   if reacted else "SILENT")
            agg[cls].append((row["run_id"], bool(reacted)))
            for h in found:
                print(f"  {cls} {CLASS_NAMES[cls]:<24} {tag:<28} "
                      f"{h['evidence']}")
                print(f"      {' '.join(h['refs'])}")
        for t in row["terminal"]:
            print(f"  terminal: {t}")
        if row["note"]:
            print(f"  ledger note: {row['note'][:160]}")
        print()
    print("=" * 72)
    print("aggregate (hits / SILENT among them) — SILENT rows are the backlog")
    for cls in DETECTORS:
        rows_ = agg.get(cls, [])
        silent = [r for r, ok in rows_ if not ok]
        print(f"  {cls} {CLASS_NAMES[cls]:<24} hits={len(rows_):<3} "
              f"silent={len(silent):<3} {('runs: ' + ', '.join(silent[:6])) if silent else ''}")
    print(f"  healthy negative controls: {', '.join(healthy) or 'none'}")


def print_chronics(ch: dict) -> None:
    print()
    print("=" * 72)
    print("CROSS-RUN CHRONICS — the same thing recurring across restarts;")
    print("every row survived at least one relaunch, so no per-run mechanism")
    print("owns it.  These are the strongest candidates for the diagnostician.")
    titles = {
        "escalation": "identical escalation (test -> producer set) in >=2 runs",
        "conviction": "same unit skipped-under-conviction in >=2 runs",
        "component": "same component re-authored in >=2 runs",
        "frame_blind": "runs where the localizer blamed NOBODY",
    }
    for key, title in titles.items():
        entries = ch.get(key) or []
        if not entries:
            continue
        print(f"\n  {title}:")
        for e in entries:
            print(f"    {e['what']}")
            print(f"      runs: {', '.join(e['runs'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("logs", nargs="*", type=Path)
    ap.add_argument("--min-iters", type=int, default=2)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    paths = args.logs or default_logs()
    missing = [p for p in paths if not p.exists()]
    if missing:
        print(f"missing: {missing}", file=sys.stderr)
        return 2
    rows, chron = census(paths, args.min_iters)
    print_report(rows)
    print_chronics(chron)
    if args.json:
        args.json.write_text(json.dumps(
            {"runs": rows, "chronics": chron}, indent=1))
        print(f"json written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
