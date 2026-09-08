#!/usr/bin/env python3
"""Distil one fill's slice sessions from `logs/events.jsonl` into a fixture.

WHY. The slice session's keep rule is judged turn by turn, and the only
record of what each turn measured is the `phase_c.session.turn` series the
harness emits. Run `3014175-d98ed09e` (2026-09-08, cluster `stable`, iter
3) recorded two sessions whose best judged state was measured, printed and
then thrown away: session #1 (+5 on turn 1, ended `kept 1 / net -4`) and
session #4 (`test_debug_format_with_context` green on turns 1, 4, 5, ended
on turn 7's tree where it is red). A fix to the keep rule is validated by
replaying those series through the real `run_slice_session`
(`tests/sessions/harness.py`), so the series must come from the record —
never from a hand-made three-turn toy.

WHAT IT READS. For `--run RUN_ID --iter N`, inside the fill of iteration N
(from the N-th `phase_c.tdd.iter` to the next):
  - each `phase_c.session.start`: the aimed tests, the editable units as
    `(file, fn, container)` sites, the files, the turn budget and the
    baseline pass count;
  - its `phase_c.session.turn` events: `turn`, `kind` (judged / compile /
    request / rejected / no_change / inconclusive / capped), the units the
    change set routed to, `targets_failing`, `newly_red`, `crashed`,
    `pass_delta`, the number of rejected bodies;
  - its `phase_c.session.end`: outcome, turns, kept and reverted sites;
  - the `phase_c.route.run` row of kind `session` that follows it: the
    ledger's outcome, net delta, minutes and calls.

WHAT IT CANNOT READ, and what the harness models instead. The events carry
no body text: a turn's bodies are replayed as markers (`turn<N>`), so a
unit whose last body was byte-identical to its baseline (the record's
`kept` excludes it) is counted as changed by the replay. The events carry
no reading for a tree state the loop never judged (a peel's intermediate
states, a change set on top of a restored state): the harness reads such
a state as the reading of the newest judged turn present on the tree —
`tests/sessions/harness.py` names that model.

USAGE
    python tools/distill_session.py --run 3014175-d98ed09e --iter 3 \
        -o tests/fixtures/sessions/3014175-d98ed09e_iter3.json
    # --events PATH  the events file (default logs/events.jsonl)

HOW TO READ THE OUTPUT. JSON: `run_id`, `iter`, `cluster`, `sessions`
([{index, ts, tests, units: [{file, fn, container}], files, max_turns,
baseline_pass, turns: [{turn, kind, units: [{file, fn, container}],
targets_failing, newly_red, crashed, pass_delta, rejected}], end: {outcome,
turns, model_calls, kept: [sites], reverted: [sites]}, row: {outcome,
net_pass_delta, minutes, model_calls, kept, reverted}}]). `index` is the
session's position in the fill (the `#N` of its schedule label). The
recorded test names the sessions it replays by index and asserts the plan's
sentences about where each one ends.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def _events(path: Path, run_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if run_id not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if str(ev.get("run_id", "")) != run_id:
                continue
            out.append(ev)
    return out


def _kind(ev: dict[str, Any]) -> str:
    return str(ev.get("event") or ev.get("kind") or "")


def _site(raw: dict[str, Any]) -> dict[str, str]:
    return {"file": str(raw.get("file", "")), "fn": str(raw.get("fn", "")),
            "container": str(raw.get("container", ""))}


def distil(run_id: str, iter_n: int, *, events_path: Path) -> dict[str, Any]:
    evs = _events(events_path, run_id)
    starts = [i for i, e in enumerate(evs) if _kind(e) == "phase_c.tdd.iter"
              and int((e.get("data") or {}).get("iter", -1)) == iter_n]
    if not starts:
        raise SystemExit(f"no phase_c.tdd.iter {iter_n} for run {run_id}")
    start = starts[-1]
    end = next((i for i in range(start + 1, len(evs))
                if _kind(evs[i]) == "phase_c.tdd.iter"), len(evs))
    fill = evs[start:end]
    cluster = str((evs[start].get("data") or {}).get("cluster", ""))
    sessions: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for e in fill:
        k = _kind(e)
        d = e.get("data") or {}
        if k == "phase_c.session.start":
            cur = {
                "index": len(sessions), "ts": str(e.get("ts", "")),
                "tests": list(d.get("tests") or []),
                "units": [_site(u) for u in (d.get("units") or [])],
                "files": list(d.get("files") or []),
                "max_turns": int(d.get("max_turns", 8) or 8),
                "baseline_pass": d.get("baseline_pass"),
                "turns": [], "end": None, "row": None,
            }
            sessions.append(cur)
        elif k == "phase_c.session.turn" and cur is not None:
            cur["turns"].append({
                "turn": int(d.get("turn", 0) or 0),
                "kind": str(d.get("kind", "")),
                "units": [_site(u) for u in (d.get("units") or [])],
                "targets_failing": list(d.get("targets_failing") or []),
                "newly_red": list(d.get("newly_red") or []),
                "crashed": list(d.get("crashed") or []),
                "pass_delta": int(d.get("pass_delta", 0) or 0),
                "rejected": len(d.get("rejected") or []),
            })
        elif k == "phase_c.session.end" and cur is not None:
            cur["end"] = {
                "outcome": str(d.get("outcome", "")),
                "turns": int(d.get("turns", 0) or 0),
                "model_calls": int(d.get("model_calls", 0) or 0),
                "kept": [_site(u) for u in (d.get("kept") or [])],
                "reverted": [_site(u) for u in (d.get("reverted") or [])],
            }
        elif k == "phase_c.route.run" and d.get("kind") == "session":
            # The row follows its session's end; bind it to the latest
            # session without a row.
            for s in reversed(sessions):
                if s["row"] is None:
                    s["row"] = {
                        "label": str(d.get("label", "")),
                        "outcome": str(d.get("outcome", "")),
                        "net_pass_delta": int(d.get("net_pass_delta", 0) or 0),
                        "minutes": float(d.get("minutes", 0.0) or 0.0),
                        "model_calls": int(d.get("model_calls", 0) or 0),
                        "kept": [list(x) for x in (d.get("kept") or [])],
                        "reverted": [list(x) for x in (d.get("reverted") or [])],
                    }
                    break
            cur = None
    return {"run_id": run_id, "iter": iter_n, "cluster": cluster,
            "sessions": sessions}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--events", type=Path, default=REPO / "logs" / "events.jsonl")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args(argv)
    fixture = distil(args.run, args.iter, events_path=args.events)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    for s in fixture["sessions"]:
        row = s["row"] or {}
        print(f"  #{s['index']}: {len(s['tests'])} test(s), {len(s['units'])} "
              f"unit(s), {len(s['turns'])} turn(s), end "
              f"{(s['end'] or {}).get('outcome', '?')}, row "
              f"{row.get('outcome', '?')} net {row.get('net_pass_delta', '?')}")
    print(f"wrote {args.out}: {len(fixture['sessions'])} session(s) of "
          f"{fixture['run_id']} iter {fixture['iter']} ({fixture['cluster']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
