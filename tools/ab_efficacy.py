#!/usr/bin/env python3
"""A/B efficacy comparator (does richer FR+genesis help convergence?).

The pipeline's deliverable is `functional_requirements_{crate}.md` + `genesis.md`
such that a fresh run converges in as few iterations as possible. This tool measures
that hypothesis: run the SAME crate twice — (A) seeded with enriched docs from a
prior converged run, (B) from blank stubs — and compare the convergence cost. This
tool is the deterministic measurement half; the two live runs are the operator's.

It reads the STRUCTURED events the pipeline already emits (no log scraping) and
segments the shared `logs/events.jsonl` into per-run slices at each `pipeline.start`,
so two runs in one file compare directly. Best run on the HARDCRATE: there
the model must actually recover, so docs *can* move the metrics — on the happy-path
microcrate every run converges in iter 1 regardless, which is why this comparison never ran.

Convergence-cost metrics (lower = cheaper = docs helped):
  iterations       max `module.iter` — outer repair loops
  task4_runs       `task.start{task=task4}` — genesis-analysis invocations
  task2_runs       `task.start{task=task2}` — full regenerations
  ratchet_restores `ratchet.restore` — iterations that REGRESSED below best
  fills            `phase_c.impl.fill.{done,fail}` — per-impl fill attempts

Usage:
  tools/ab_efficacy.py --list                       # enumerate runs in the file
  tools/ab_efficacy.py --a <idx|ts> --b <idx|ts>    # A=enriched, B=blank
  tools/ab_efficacy.py                              # compare the last two runs
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Lower-is-better convergence-cost metrics, in report order.
COST_FIELDS = ("iterations", "task4_runs", "task2_runs", "ratchet_restores", "fills")


def load_events(path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSON-lines events file, skipping any malformed line. Never raises."""
    out: list[dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def split_runs(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Segment a shared events stream into per-run slices at each `pipeline.start`
    (events before the first start — an interrupted prior run — are dropped)."""
    runs: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] | None = None
    for e in events:
        if e.get("event") == "pipeline.start":
            cur = [e]
            runs.append(cur)
        elif cur is not None:
            cur.append(e)
    return runs


@dataclass(frozen=True)
class RunMetrics:
    started: str
    product: str
    pipeline_sha: str
    iterations: int
    task4_runs: int
    task2_runs: int
    ratchet_saves: int
    ratchet_restores: int
    fills: int
    converged: bool


def run_metrics(run: list[dict[str, Any]]) -> RunMetrics:
    """Reduce one run's event slice to its convergence-cost metrics (pure)."""
    head = run[0] if run else {}

    def count(event: str, **eq: Any) -> int:
        return sum(1 for e in run if e.get("event") == event
                   and all(e.get(k) == v for k, v in eq.items()))

    iters = [int(e.get("iteration", 0)) for e in run if e.get("event") == "module.iter"]
    converged = any(
        e.get("event") == "task.done" and e.get("task") == "task3"
        and bool((e.get("data") or {}).get("passed"))
        for e in run
    )
    return RunMetrics(
        started=str(head.get("ts", "")),
        product=str(head.get("product", "")),
        pipeline_sha=str(head.get("pipeline_sha", "")),
        iterations=max(iters) if iters else (1 if converged else 0),
        task4_runs=count("task.start", task="task4"),
        task2_runs=count("task.start", task="task2"),
        ratchet_saves=count("ratchet.save"),
        ratchet_restores=count("ratchet.restore"),
        fills=count("phase_c.impl.fill.done") + count("phase_c.impl.fill.fail"),
        converged=converged,
    )


def compare(a: RunMetrics, b: RunMetrics) -> dict[str, Any]:
    """Per-metric A−B deltas + an overall verdict. Convention: A = enriched-docs,
    B = blank. A cost metric LOWER in A = docs helped on that channel."""
    deltas = {f: getattr(a, f) - getattr(b, f) for f in COST_FIELDS}
    helped = sum(1 for d in deltas.values() if d < 0)
    hurt = sum(1 for d in deltas.values() if d > 0)
    if not a.converged or not b.converged:
        verdict = "inconclusive (a run did not converge — compare only converged pairs)"
    elif helped and not hurt:
        verdict = "docs help (A cheaper on every differing channel)"
    elif hurt and not helped:
        verdict = "docs hurt (A more expensive)"
    elif not helped and not hurt:
        verdict = "no effect (identical cost)"
    else:
        verdict = "mixed (cheaper on some channels, costlier on others)"
    return {"deltas": deltas, "helped": helped, "hurt": hurt, "verdict": verdict}


def _select(runs: list[RunMetrics], key: str) -> int:
    """Resolve an index ('0','-1') or a started-ts prefix to a run position."""
    try:
        i = int(key)
        return i if i >= 0 else len(runs) + i
    except ValueError:
        for i, r in enumerate(runs):
            if r.started.startswith(key):
                return i
    raise SystemExit(f"no run matches {key!r}; use --list")


def _fmt(r: RunMetrics) -> str:
    conv = "conv" if r.converged else "STUCK"
    return (f"{r.product:10s} {r.started[:19]} {r.pipeline_sha[:8]} [{conv}] "
            f"iters={r.iterations} t4={r.task4_runs} t2={r.task2_runs} "
            f"restore={r.ratchet_restores} fills={r.fills}")


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--events", type=Path, default=here / "logs/events.jsonl")
    ap.add_argument("--list", action="store_true", help="enumerate runs and exit")
    ap.add_argument("--a", default=None, help="enriched run (index or ts prefix)")
    ap.add_argument("--b", default=None, help="blank run (index or ts prefix)")
    args = ap.parse_args(argv)

    runs = [run_metrics(r) for r in split_runs(load_events(args.events))]
    if not runs:
        print(f"no runs in {args.events}")
        return 1
    if args.list or (args.a is None and args.b is None and len(runs) < 2):
        for i, r in enumerate(runs):
            print(f"  [{i}] {_fmt(r)}")
        return 0

    ai = _select(runs, args.a) if args.a is not None else len(runs) - 2
    bi = _select(runs, args.b) if args.b is not None else len(runs) - 1
    a, b = runs[ai], runs[bi]
    res = compare(a, b)
    print(f"A (enriched) [{ai}] {_fmt(a)}")
    print(f"B (blank)    [{bi}] {_fmt(b)}")
    print("  delta (A-B, negative = docs cheaper):")
    for f in COST_FIELDS:
        d = res["deltas"][f]
        print(f"    {f:16s} {d:+d}")
    print(f"  VERDICT: {res['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
