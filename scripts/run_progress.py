#!/usr/bin/env python3
"""Every number a run moves, per iteration, with its min -> max range.

WHY
---
The one line a live run prints per iteration —

    iter N: cargo test rc=101 (0.6s); pass=32 fail=73 oracle=105
    (improved=False, stall=0/3); frontier=21 fn(s) (merged), 0 skipped as
    exhausted.

— carries five quantities and hides a dozen more, and two of the five are
routinely confused for each other. On 2026-08-14, run `3368-753757e6`, an
in-session monitor reported "87 -> 105" across iterations 1 -> 2 while
`pass` read 32: the 87 and the 105 are `oracle` (the DENOMINATOR — how many
tests returned a verdict at all), not the score. The oracle moves because a
crashed test binary reports nothing for every test it holds. Iter 1 lost 3
binaries — 26 named tests plus 4 further executions — to a heap-use-after-
free blamed on `src/error.rs::drop`, so 87 of the suite's 107 tests were
measured and 20 reported nothing; the crash retrigger then re-authored
`['StdError']` and reverted 25 filled bodies ON PURPOSE, and iter 2 measured
105 with only 2 missing. So in one step: coverage +18, score 39 -> 32,
high-water deliberately re-based 39 -> 32, 8 bodies re-landed. A reader
tracking only `pass` sees a regression and misses the recovered coverage; a
reader tracking only `oracle` sees a recovery and misses that 25 bodies were
destroyed to buy it. Neither number is the run's progress on its own.

The generalisation, and why this script exists: the loop has no single
progress number. It has a VECTOR — measurement coverage, score, high-water,
frontier, exhaustion, stub count, fills landed, bodies accepted/rejected —
and different mechanisms move different components, some of them DOWN on
purpose (the crash/layout retrigger re-stubs bodies; the batch ratchet
restores an older tree). Reading one component alone is how a deliberate
destruction gets read as a regression, and how a recovered denominator gets
read as progress. This prints all of them side by side, per iteration, plus
the min/max/first/last range of each so a glance answers "what has actually
moved in this run".

USAGE
-----
    python scripts/run_progress.py                    # latest run in events.jsonl
    python scripts/run_progress.py --run 3368-753757e6
    python scripts/run_progress.py --cluster stable   # one cluster only
    python scripts/run_progress.py --last 12          # last 12 iterations
    python scripts/run_progress.py --no-log           # events only, skip log parse

Reads `logs/events.jsonl` (structured, authoritative for the score vector)
backwards from the end until the target run's `pipeline.start`, so cost is
proportional to the run, not to the 100MB+ file. Also reads
`logs/pipeline_anyhow.log` (+ `.1`) for the two quantities that exist only as
text — the `N skipped as exhausted` count and the cluster stub total from
`skipped K of N stub(s)` — pass `--no-log` to skip that. Read-only: touches
no tree, starts no model call, costs no GPU.

HOW TO INTERPRET THE OUTPUT
---------------------------
Per-iteration columns, grouped by what owns them:

  MEASUREMENT — how much of the suite was observable this sweep
    full   `oracle_static`: tests the suite defines. Moves only when t1.5
           re-authors the layout and the tests are regenerated (code_id
           changes); a jump here means the denominator itself changed and
           counts across the jump are not comparable.
    meas   `oracle_size` = pass + fail: tests that returned a verdict. This
           is the 87 and the 105.
    miss   full - meas: declared tests the sweep did not COUNT. NOT the
           tests a crash took down — those report nothing and are counted
           as FAILING (the loop's `ORACLE SHRUNK — counting N as failing`,
           and the `vanish` tag in NOTES below is where they are reported).
           What lands here is (a) the verdicts an aborted binary managed to
           PRINT before it died — `pass`/`fail` are summed from libtest
           `test result:` trailers and an aborted binary never prints one —
           and (b) tests that never ran at all: libtest `ignored` (cfg-gated
           to another cluster) plus any static over-count. Cluster `stable`,
           2026-08-29: `miss` 12 was 10 printed-then-dropped verdicts across
           5 aborted binaries and the 2 `requires nightly` tests this
           cluster ignores; it read 2 for the whole preceding week with no
           crash at all. `miss > 0` means the score below is a partial
           reading; a "best tree" is only durable on a full oracle.

  SCORE — the objective
    pass/fail  as reported. `%full` is pass/full, the only percentage that
           compares across sweeps with different `miss`.
    best   running high-water of pass THIS run. When the retrigger re-stubs
           bodies the driver re-bases it — a `hw` note marks the iteration;
           a gate still comparing against a pre-re-base high-water is the
           permanent-lock failure mode (see CLAUDE.md).
    impr/stall  the driver's own verdict and the stall budget (n/max). The
           budget freezes while a re-fill window is open.

  WORK — what the iteration had to spend itself on
    front  frontier: fns eligible for a fill attempt this iteration.
    exh    skipped as permanently exhausted (log-only). Rising `exh` with
           falling `front` is the lockout shape, not a healthy re-climb.
    stubs  cluster stub total at the LAST reading inside this iteration's
           fill work (log-only, from the `skipped K of N stub(s)` line) —
           i.e. the tree the next sweep measures. A `~` suffix means no
           reading landed in this window and the previous one is carried
           forward. This is the reconstruction's real remaining work; it
           floors at the cfg-inactive count (14 for anyhow/stable), never 0.
    filled cumulative fill bar `K/M fillable filled` from the per-fn pass
           header — M is the cluster's fillable total (cfg-gated stubs
           excluded), K the bodies currently on disk. This is the number the
           `stubs` column cannot give: once a pass is down to cfg-gated
           stubs the last `skipped K of N` reading is `14 of 14` and `stubs`
           pins at the floor while K keeps moving. A DROP in K mid-run is a
           retrigger re-stub (206/209 -> 197/209 on run `3368-753757e6`),
           not a parse error. `~` carries the last reading forward.
    fills  bodies that LANDED this iteration, summed across the three fill
           routes (`file.fill.done.fns_filled` + `impl.fill.done.
           members_filled` + successful `fn.fill.done`) — not
           `phase_c.fill.progress`, which only the per-fn route emits.
    acc/rej  verdicts recorded in the window: accepted vs
           rejected/reverted/exhausted. `fills` high with `pass` flat is the
           zero-payoff shape; `acc` 0 with `calls` high is a burning loop.
    calls  model requests issued in the window — the cost of the row.

  NOTES  one tag per mechanism that fired: `retrig(cause,-N)` layout/crash
         retrigger reverted N bodies ON PURPOSE (expect pass to drop and
         miss to shrink next sweep), `vanish(-N/Bb)` N tests lost to B
         crashed binaries, `stubs a->b`, `crash:<fn>` localizer blame,
         `chain-blame` provenance-synthesized blame, `exh-reset/unblock`,
         `zero-payoff` shadow detector, `ratchet`.

The RANGES block then gives, for every quantity, `first -> last` with
`min..max` and the direction that counts as progress. Read it as the run's
whole trajectory: on run `3368-753757e6` at 2 iterations,
`pass 39 -> 32 (min 32 max 39)` sitting next to
`meas 87 -> 105 (min 87 max 105)` and `full 107` says in two lines exactly
what the "87 -> 105" monitor report meant, and what it did not.

The decision rule this feeds: a run is progressing when `stubs` falls AND
`best` rises AND `miss` trends to 0. Any two without the third names the
pathology — stubs falling with best flat is fills that buy nothing, best
rising with miss high is a score measured on a partial suite, miss falling
with stubs flat is a tree that only recovered what a retrigger destroyed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "events.jsonl"
LOGS = [ROOT / "logs" / "pipeline_anyhow.log", ROOT / "logs" / "pipeline_anyhow.log.1"]

CHUNK = 4 << 20
MAX_SCAN = 400 << 20


def read_run_events(path: Path, run_id: str | None) -> tuple[str, list[dict]]:
    """Scan backwards, collecting one run's events; stop at its pipeline.start."""
    size = path.stat().st_size
    pos, tail, out, scanned = size, b"", [], 0
    target = run_id
    while pos > 0 and scanned < MAX_SCAN:
        step = min(CHUNK, pos)
        pos -= step
        scanned += step
        with path.open("rb") as fh:
            fh.seek(pos)
            buf = fh.read(step) + tail
        lines = buf.split(b"\n")
        tail = lines[0] if pos > 0 else b""
        block, done = [], False
        for raw in reversed(lines[1:] if pos > 0 else lines):
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            rid = ev.get("run_id")
            if target is None and rid:
                target = rid
            if rid != target:
                continue
            block.append(ev)
            if ev.get("event") == "pipeline.start":
                done = True
                break
        out.extend(block)
        if done:
            break
    out.reverse()
    return target or "?", out


ITER_RE = re.compile(r"\[phase-c/tdd\] iter (\d+): cargo test rc=")
EXH_RE = re.compile(r"(\d+) skipped as exhausted")
STUB_RE = re.compile(r"cluster (\w+): skipped (\d+) of (\d+) stub\(s\)")
# The per-fn pass header is the only line that prints the cluster's cumulative
# fill bar. STUB_RE alone cannot reconstruct it: once every fillable stub is
# filled or deferred the last reading in a window is `skipped 14 of 14`, which
# pins the column at the cfg-inactive floor while `197/209 fillable filled`
# (the number a human calls "progress") never enters the vector.
FILLED_RE = re.compile(
    r"(\d+) stub\(s\) remaining in cluster '(\w+)' \((\d+)/(\d+) fillable filled")
START_RE = re.compile(r"PIPELINE STARTED .*run_id=(\S+)")
CODE_RE = re.compile(r"code=(\w+)\s+git=(\w+)")
HW_RE = re.compile(r"iter (\d+): high-water re-based (\d+) -> (\d+)")


def read_log(run_id: str) -> tuple[dict[int, dict], str]:
    """Per-iteration text-only quantities: exhausted-skipped, stub total, re-base."""
    lines: list[str] = []
    for path in LOGS:
        if not path.exists():
            continue
        try:
            text = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(text):
            m = START_RE.search(line)
            if m and m.group(1) == run_id:
                lines = text[idx:]
        if lines:
            break
    out: dict[int, dict] = defaultdict(dict)
    # Readings taken before the first iteration summary describe the tree the
    # first iteration entered on — key them to it rather than dropping them.
    cur, code = 0, "?"
    pre: dict[str, object] = {}
    for line in lines:
        if START_RE.search(line):
            if run_id not in line:
                break
            m = CODE_RE.search(line)
            if m:
                code = f"{m.group(1)} git={m.group(2)}"
        m = ITER_RE.search(line)
        if m:
            cur = int(m.group(1))
            if pre:
                out[cur].update({k: v for k, v in pre.items() if k not in out[cur]})
                pre = {}
            e = EXH_RE.search(line)
            if e:
                out[cur]["exh"] = int(e.group(1))
            continue
        m = HW_RE.search(line)
        if m:
            out[int(m.group(1))]["rebase"] = f"{m.group(2)}->{m.group(3)}"
            continue
        m = STUB_RE.search(line)
        if m:
            if cur:
                out[cur]["stubs"] = int(m.group(3))
            else:
                pre["stubs"] = int(m.group(3))
            continue
        m = FILLED_RE.search(line)
        if m:
            reading = (int(m.group(3)), int(m.group(4)))
            if cur:
                out[cur]["filled"] = reading
            else:
                pre["filled"] = reading
    return out, code


def short(name: str) -> str:
    return name.rsplit("::", 1)[-1].rsplit("/", 1)[-1]


def notes_for(evs: list[dict]) -> str:
    tags: list[str] = []
    for ev in evs:
        kind, d = ev.get("event", ""), ev.get("data") or {}
        if kind == "phase_c.layout_retrigger.fired":
            tags.append(f"retrig({d.get('cause','?')},-{d.get('reverted_count',0)})")
        elif kind == "phase_c.tdd.oracle_vanished":
            tags.append(f"vanish(-{d.get('missing',0)}/{d.get('crashed_binaries',0)}b)")
        elif kind == "phase_c.tdd.stub_progress":
            tags.append(f"stubs {d.get('stubs_prev')}->{d.get('stubs_now')}")
        elif kind == "phase_c.crash.localized":
            blamed = d.get("blamed") or []
            tags.append("crash:" + (short(str(blamed[-1])) if blamed else "?"))
        elif kind == "phase_c.crash.unlocalized":
            tags.append("crash:NOBODY")
        elif kind == "phase_c.crash.chain_blame":
            tags.append("chain-blame")
        elif kind == "phase_c.exhaustion.reset":
            tags.append(f"exh-reset({d.get('cleared',0)})")
        elif kind == "phase_c.exhaustion.unblocked":
            tags.append(f"exh-unblock({d.get('cleared',0)})")
        elif kind == "phase_c.tdd.zero_payoff_shadow":
            tags.append("zero-payoff")
        elif kind == "ratchet.save":
            tags.append("ratchet")
        elif kind == "phase_c.checkpoint":
            tags.append(f"ckpt({d.get('pass_count','?')})")
    seen, uniq = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return " ".join(uniq)


def landed(evs: list[dict]) -> int:
    """Bodies that actually landed in this window, summed over the three fill
    routes. Counted from the per-route done events rather than from
    `phase_c.fill.progress`, which only the per-fn route emits — a file-first
    iteration reports every one of its fills through `file.fill.done`."""
    n = 0
    for e in evs:
        kind, d = e.get("event", ""), e.get("data") or {}
        if kind == "phase_c.file.fill.done":
            n += int(d.get("fns_filled", 0) or 0)
        elif kind == "phase_c.impl.fill.done":
            n += int(d.get("members_filled", 0) or 0)
        elif kind == "phase_c.fn.fill.done" and d.get("success"):
            n += 1
    return n


def route_table(evs: list[dict]) -> list[tuple[str, int, int, int, float]]:
    """Per fill route: units dispatched, bodies landed, failures, model calls,
    mean fix attempts. The routes are not interchangeable — file-first fills a
    whole file in one prompt and reverts the file on a compile failure, impl
    fills a block and can salvage part of it, per-fn fills one body. A route
    with many starts and few landings is where the run's time is going."""
    rows = []
    for label, start, done, fail, count, attempts in (
        ("file-first", "phase_c.file.fill.start", "phase_c.file.fill.done",
         "phase_c.file.fill.revert", "fns_filled", None),
        ("impl-block", "phase_c.impl.fill.start", "phase_c.impl.fill.done",
         "phase_c.impl.fill.fail", "members_filled", "fix_attempts"),
        ("per-fn", "phase_c.fn.fill.start", "phase_c.fn.fill.done",
         "phase_c.fn.fill.fail", None, "fix_attempts"),
    ):
        starts = sum(1 for e in evs if e.get("event") == start)
        dones = [e.get("data") or {} for e in evs if e.get("event") == done]
        fails = sum(1 for e in evs if e.get("event") == fail)
        filled = (sum(int(d.get(count, 0) or 0) for d in dones) if count
                  else sum(1 for d in dones if d.get("success")))
        tries = [int(d.get(attempts, 0) or 0) for d in dones] if attempts else []
        mean = (sum(tries) / len(tries)) if tries else 0.0
        if starts or filled or fails:
            rows.append((label, starts, filled, fails, mean))
    return rows


COLS = [
    ("iter", 4), ("rc", 4), ("pass", 5), ("fail", 5), ("meas", 5), ("full", 5),
    ("miss", 5), ("%full", 6), ("best", 5), ("impr", 5), ("stall", 6),
    ("front", 6), ("exh", 4), ("stubs", 6), ("filled", 9), ("fills", 6), ("acc", 4),
    ("rej", 4), ("calls", 6),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=None, help="run id (default: latest in events.jsonl)")
    ap.add_argument("--cluster", default=None, help="restrict to one cluster")
    ap.add_argument("--last", type=int, default=0, help="show only the last N iterations")
    ap.add_argument("--no-log", action="store_true", help="skip the log parse")
    ap.add_argument("--events", default=str(EVENTS))
    args = ap.parse_args()

    path = Path(args.events)
    if not path.exists():
        print(f"no event stream at {path}", file=sys.stderr)
        return 2
    run_id, evs = read_run_events(path, args.run)
    if not evs:
        print(f"no events for run {run_id}", file=sys.stderr)
        return 2

    start = next((e for e in evs if e.get("event") == "pipeline.start"), evs[0])
    iters = [e for e in evs if e.get("event") == "phase_c.tdd.iter"
             and (e.get("data") or {}).get("pass_count") is not None
             and (not args.cluster or (e.get("data") or {}).get("cluster") == args.cluster)]
    if not iters:
        print(f"run {run_id}: no scored TDD iterations yet "
              f"({len(evs)} event(s) since {start.get('ts','?')})", file=sys.stderr)
        return 1

    clusters = sorted({(e.get("data") or {}).get("cluster") for e in iters})
    logdata, code = ({}, "?") if args.no_log else read_log(run_id)

    # Split every event into the window of the iteration it followed.
    bounds = [(e.get("ts", ""), e) for e in iters]
    windows: list[list[dict]] = [[] for _ in iters]
    pos = 0
    for ev in evs:
        ts = ev.get("ts", "")
        while pos + 1 < len(bounds) and ts >= bounds[pos + 1][0]:
            pos += 1
        windows[pos].append(ev)

    rows, best, ranges = [], 0, defaultdict(list)
    last_stubs: int | None = None
    last_filled: tuple[int, int] | None = None
    for idx, ev in enumerate(iters):
        d = ev.get("data") or {}
        it = d.get("iter", 0)
        p, f = int(d.get("pass_count", 0)), int(d.get("fail_count", 0))
        meas = int(d.get("oracle_size", p + f))
        full = int(d.get("oracle_static", meas) or meas)
        best = max(best, p)
        win = windows[idx]
        verdicts = Counter((e.get("data") or {}).get("disposition")
                           for e in win if e.get("event") == "phase_c.verdict")
        lg = logdata.get(it, {})
        # The stub total is only printed when a fill route reports; an
        # iteration with no reading has not changed it, so carry the last one
        # forward and mark it `~` rather than showing a hole.
        if "stubs" in lg:
            last_stubs = lg["stubs"]
            stubs: object = last_stubs
        else:
            stubs = f"{last_stubs}~" if last_stubs is not None else "-"
        if "filled" in lg:
            last_filled = lg["filled"]
            filled_col = f"{last_filled[0]}/{last_filled[1]}"
            ranges["filled"].append(last_filled[0])
        else:
            filled_col = (f"{last_filled[0]}/{last_filled[1]}~"
                          if last_filled is not None else "-")
        row = {
            "iter": it, "rc": d.get("rc", ""), "pass": p, "fail": f, "meas": meas,
            "full": full, "miss": max(0, full - meas),
            "%full": f"{100.0 * p / full:.0f}%" if full else "-",
            "best": best, "impr": "yes" if d.get("improved") else "no",
            "stall": f"{d.get('stall', '?')}/3", "front": d.get("frontier_size", 0),
            "exh": lg.get("exh", "-"), "stubs": stubs, "filled": filled_col,
            "fills": landed(win),
            "acc": verdicts.get("accept", 0),
            "rej": sum(v for k, v in verdicts.items() if k != "accept"),
            "calls": sum(1 for e in win if e.get("event") == "llm.request"),
        }
        note = notes_for(win)
        if lg.get("rebase"):
            note = f"hw {lg['rebase']} " + note
        row["_note"] = note.strip()
        rows.append(row)
        for k in ("pass", "fail", "meas", "full", "miss", "best", "front", "fills",
                  "acc", "rej", "calls"):
            ranges[k].append(row[k])
        for k in ("exh", "stubs"):
            if isinstance(row[k], int):
                ranges[k].append(row[k])

    sd = start.get("data") or {}
    print(f"run {run_id}  started {start.get('ts','?')}  "
          f"pipeline_sha {start.get('pipeline_sha','?')}  code {code}")
    print(f"model {sd.get('model','?')} via {sd.get('backend','?')}")
    print(f"cluster(s) {', '.join(c or '?' for c in clusters)}  "
          f"{len(rows)} scored iteration(s)  {len(evs)} event(s)")
    print()

    show = rows[-args.last:] if args.last else rows
    print("  ".join(h.rjust(w) for h, w in COLS) + "   NOTES")
    print("-" * (sum(w + 2 for _, w in COLS) + 30))
    for r in show:
        print("  ".join(str(r[h]).rjust(w) for h, w in COLS) + "   " + r["_note"])

    routes = route_table(evs)
    if routes:
        print()
        print("FILL ROUTES  (run total — each route counts a different unit)")
        print(f"  {'route':<11} {'dispatched':>10} {'bodies':>7} {'failed':>7} "
              f"{'mean fix attempts':>18}")
        for label, starts, filled, fails, mean in routes:
            shown = f"{mean:.1f}" if mean else "-"
            print(f"  {label:<11} {starts:>10} {filled:>7} {fails:>7} {shown:>18}")

    print()
    print("RANGES  (first -> last, min..max, and which direction is progress)")
    goal = {
        "pass": "up", "fail": "down", "meas": "up (toward full)", "full": "fixed",
        "miss": "down to 0", "best": "up (re-based on purpose after a retrigger)",
        "front": "context", "exh": "down", "stubs": "down (floors at cfg-inactive)",
        "filled": "up (numerator; a drop is a retrigger re-stub)",
        "fills": "context", "acc": "up", "rej": "down", "calls": "cost",
    }
    for k in ("pass", "fail", "meas", "full", "miss", "best", "stubs", "filled",
              "exh", "front", "fills", "acc", "rej", "calls"):
        v = ranges.get(k) or []
        if not v:
            print(f"  {k:<6} (not observed)")
            continue
        print(f"  {k:<6} {v[0]} -> {v[-1]}   min {min(v)} max {max(v)}"
              f"   [{goal[k]}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
