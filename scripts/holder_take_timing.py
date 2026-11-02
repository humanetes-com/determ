#!/usr/bin/env python3
"""Per-admission timing of measured holders in one run's t2a fills.

WHY. The fill schedule admits a still-stubbed function that a keep of this
fill measured as the first failure of red tests (a "holder") into the per-fn
run in flight (`taken into … ahead of its unclaimed units`) or opens it at the
cursor. Whether that admission is prompt is a timing question that the score
cannot answer, and it was computed by hand from events.jsonl on every reading:
run `15370-6a4cfd45` (60.9 min from first mention to claim), `2426367-f566edfc`,
`3178980-c42f7944` (fill 1 serial), `528728-f4e7de82` (2026-11-02: nine holders
admitted together ran one after another on two workers while two others had
left the pass). This script is that computation.

USAGE.
    python scripts/holder_take_timing.py <run_id> [--events logs/events.jsonl]
        [--cluster stable] [--slack 5]
Reads only events (no GPU, no cargo). Safe on a live run.

HOW TO INTERPRET. One block per `phase_c.fill.blocker_opened`:
  OPEN hh:mm:ss <thread> into=<run label or '(cursor)'> — the admission.
  trigger: the last `phase_c.u2.external_only` before the open whose
    blocking_fns names a holder: its thread, the gap from that keep's
    `fn.fill.done` to the open, and whether the open ran on the keeping
    thread (`same`), on a thread idle at that moment (`idle`), or on a thread
    that had just finished its own unit (`busy-returned`, the late-claim shape
    of 528728 21:28:59 → 21:31:30).
  per holder: start time, worker, delay from the open. `NOT STARTED` means
    no start was seen (a holder written as a joint extra of another unit,
    then refused on moved ground, reads this way).
  IDLE-WITH-HOLDER a–b N worker-s: a stretch (>= 30 worker-s) in which pool
    workers sat outside a unit while admitted holders had not started,
    counted once per second as min(pending holders, idle workers). "Outside
    a unit" includes a worker's own post-unit gate re-read, so a stretch of
    one or two suite runs (about 20 s each on anyhow) after a sibling's keep
    is the gate reading moved ground; minutes of it before a share boundary
    is the clock's reserved-share wait (`RunClock._decide`).
  `plan units started before the holder` lists starts of units that were not
    admitted as holders, between the open and the holder's start: the
    take-order line says this should be empty.
Summary: admissions, holders, how many started within --slack of the open,
the worst delay, and total idle worker-seconds with a holder pending.
Decision rule: batch 2 (369 + 380) reads on-theory when every multi-holder
admission starts side by side, every trigger is `same` or `idle` at one suite
re-read, no plan unit jumps a holder, and idle-while-waiting is ~0. A delay
with idle workers is a clock/share defect, not a gate one, when the start
coincides with the run's share running out.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def _t(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _hms(x: float) -> str:
    return datetime.fromtimestamp(x).strftime("%H:%M:%S")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--events", default="logs/events.jsonl")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--slack", type=float, default=5.0)
    a = ap.parse_args()

    evs: list[dict] = []
    needle = f'"run_id": "{a.run_id}"'
    with Path(a.events).open() as fh:
        for line in fh:
            if needle not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") in {
                "phase_c.fn.fill.start", "phase_c.fn.fill.done",
                "phase_c.fn.fill.fail", "phase_c.fill.blocker_opened",
                "phase_c.u2.external_only", "phase_c.pass.start",
                "phase_c.pass.done",
            }:
                e["_t"] = _t(e["ts"])
                evs.append(e)
    evs.sort(key=lambda e: e["_t"])
    pool_prefix = f"per-fn-{a.cluster}-w"

    def key(d: dict) -> str:
        return f"{d.get('file')}::{d.get('fn')}"

    # Busy intervals per worker, and pass windows.
    busy: dict[str, list[list[float]]] = {}
    passes: list[list[float]] = []
    for e in evs:
        th, k = e["thread"], e["event"]
        if k == "phase_c.pass.start":
            passes.append([e["_t"], float("inf")])
        elif k == "phase_c.pass.done" and passes:
            passes[-1][1] = e["_t"]
        if not th.startswith(pool_prefix):
            continue
        if k == "phase_c.fn.fill.start":
            busy.setdefault(th, []).append([e["_t"], float("inf")])
        elif k in ("phase_c.fn.fill.done", "phase_c.fn.fill.fail"):
            if busy.get(th) and busy[th][-1][1] == float("inf"):
                busy[th][-1][1] = e["_t"]
    workers = sorted(busy)

    def is_busy(th: str, x: float) -> bool:
        return any(s <= x < f for s, f in busy.get(th, []))

    holders_all: set[str] = set()
    for e in evs:
        if e["event"] == "phase_c.fill.blocker_opened":
            holders_all.update(e["data"].get("blockers") or [])

    starts = [e for e in evs if e["event"] == "phase_c.fn.fill.start"]
    n_adm = n_h = n_prompt = 0
    worst = 0.0
    idle_total = 0.0
    for e in evs:
        if e["event"] != "phase_c.fill.blocker_opened":
            continue
        d = e["data"]
        t0 = e["_t"]
        hs = list(d.get("blockers") or [])
        into = d.get("into") or "(cursor)"
        n_adm += 1
        print(f"OPEN {_hms(t0)} {e['thread']} into={into} holders={hs}")
        trig = None
        # Only keeps of the pass in flight: an older keep naming the same
        # holder belongs to an earlier fill.
        p0 = max((s for s, _f in passes if s <= t0), default=0.0)
        for x in evs:
            if x["_t"] > t0:
                break
            if x["_t"] < p0:
                continue
            if x["event"] == "phase_c.u2.external_only" and set(
                    x["data"].get("blocking_fns") or []) & set(hs):
                trig = x
        if trig is not None:
            done = next((x for x in evs if x["_t"] >= trig["_t"]
                         and x["thread"] == trig["thread"]
                         and x["event"] in ("phase_c.fn.fill.done",
                                            "phase_c.fn.fill.fail")), None)
            gap = t0 - done["_t"] if done else float("nan")
            if e["thread"] == trig["thread"]:
                how = "same"
            elif not is_busy(e["thread"], t0 - 0.5) and not any(
                    x["thread"] == e["thread"] and x["event"] in (
                        "phase_c.fn.fill.done", "phase_c.fn.fill.fail")
                    and 0 <= t0 - x["_t"] <= gap + 1
                    for x in evs):
                how = "idle"
            else:
                how = "busy-returned"
            print(f"  trigger: {trig['thread']} keep {key(trig['data'])}"
                  f" done→open {gap:.1f}s, open on {how}")
        else:
            last = max((x for x in evs if x["_t"] <= t0 and x["_t"] >= p0
                        and x["event"] in ("phase_c.fn.fill.done",
                                           "phase_c.fn.fill.fail")),
                       key=lambda x: x["_t"], default=None)
            print("  trigger: no keep of this pass names it in blocking_fns"
                  " (the gate reads first failures from its own suite"
                  " re-read; a measuring run of another route opens at the"
                  " cursor)" + (f"; last unit done {_hms(last['_t'])} on"
                                f" {last['thread']}" if last else ""))
        for h in hs:
            n_h += 1
            st = next((s for s in starts if s["_t"] >= t0
                       and key(s["data"]) == h), None)
            if st is None:
                print(f"  {h}: NOT STARTED")
                continue
            dl = st["_t"] - t0
            worst = max(worst, dl)
            if dl <= a.slack:
                n_prompt += 1
            jumped = [key(s["data"]) for s in starts
                      if t0 < s["_t"] < st["_t"]
                      and key(s["data"]) not in holders_all]
            print(f"  {h}: start {_hms(st['_t'])} on {st['thread'][-2:]}"
                  f" +{dl:.1f}s"
                  + (f" plan units started before the holder={jumped}"
                     if jumped else ""))
    # Idle worker-seconds with a holder pending, counted once per second:
    # min(holders admitted and not started, pool workers outside a unit),
    # after --slack seconds from each holder's admission, inside a pass.
    adm: list[tuple[float, float]] = []
    for e in evs:
        if e["event"] != "phase_c.fill.blocker_opened":
            continue
        for h in e["data"].get("blockers") or []:
            st = next((s for s in starts if s["_t"] >= e["_t"]
                       and key(s["data"]) == h), None)
            if st is not None:
                adm.append((e["_t"] + a.slack, st["_t"]))
    if adm:
        lo = int(min(x for x, _ in adm))
        hi = int(max(y for _, y in adm)) + 1
        episodes: list[list[float]] = []
        for x in range(lo, hi):
            if not any(s <= x < f for s, f in passes):
                continue
            pend = sum(1 for s, f in adm if s <= x < f)
            if not pend:
                continue
            idle = sum(1 for w in workers if not is_busy(w, x))
            n = min(pend, idle)
            idle_total += n
            if n:
                if episodes and x - episodes[-1][1] <= 1:
                    episodes[-1][1] = x
                    episodes[-1][2] += n
                else:
                    episodes.append([x, x, n])
        for s0, s1, n in episodes:
            if n >= 30:
                print(f"IDLE-WITH-HOLDER {_hms(s0)}–{_hms(s1)}"
                      f" {n:.0f} worker-s")
    print(f"SUMMARY admissions={n_adm} holders={n_h} started<= {a.slack:.0f}s:"
          f" {n_prompt} worst delay {worst/60:.1f} min idle worker-s with a"
          f" holder pending {idle_total:.0f}")


if __name__ == "__main__":
    main()
