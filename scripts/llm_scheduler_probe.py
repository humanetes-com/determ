#!/usr/bin/env python3
"""Replay one recorded fill boundary into the local model as a scheduler of last resort.

WHY THIS EXISTS
  Run `38934-2999cc94` (2026-09-26, cluster `stable`) spent fills 5 and 6
  (~350 minutes, +9 tests) around 13 stack-overflow aborts whose two culprit
  units (`construct_from_boxed`, `Boxed::new`) sat in the dispatch batch with
  crash evidence and reached no route: the landed-change audit said
  `no route attempted` after fill 5 and again after fill 6. The question this
  probe answers is the one an LLM-driven fallback scheduler would stand on:
  given the state the deterministic planner had at that boundary — and
  nothing else — does the model open the units the planner dropped, and does
  it write the members of the cycle in ONE run rather than one at a time?
  It is a reading, not a validation: one boundary, a few samples.

USAGE
  .venv/bin/python scripts/llm_scheduler_probe.py --out DIR \
      [--run 38934-2999cc94] [--iter 6] [--variant A|B|C|all] [--samples 1]
  Preconditions: vLLM on :8000 serving MODEL from .env. Reads logs/events.jsonl,
  logs/route_ledger/anyhow/index.jsonl and one retained prompt under
  logs/prompts/ (the fill-6 kind.rs file prompt, for the evidence block the
  pipeline actually sent). Writes only under DIR: state_<V>.txt (the prompt),
  reply_<V>_<n>.txt (content), reasoning_<V>_<n>.txt. Touches no tree, no
  ledger, no event log. Local endpoint only. Cost: one call per variant-sample,
  ~3-10 minutes each on an idle GPU at these prompt sizes; the calls run
  serially. A running pipeline shares the GPU and slows both.

VARIANTS (what the model is shown about the crash)
  A  the CRASH_EVIDENCE block exactly as the file-first prompt carried it
     (frames plus the "detector did not report an in-crate origin ... UNKNOWN"
     paragraph, once per crashed binary).
  B  the detector's own stack excerpt for one binary (24 access frames) and
     the SUMMARY line, with the note that the other binaries print the same.
  C  no frames: the test names, "stack overflow", and the list of units the
     pipeline had ranked on crash evidence.
  The state is otherwise identical across variants.

HOW TO INTERPRET
  Read reply_<V>_<n>.txt. The questions, in order:
   1. Does the plan open `src/error.rs::construct_from_boxed`? (the unit no
      route attempted). Yes/no per variant.
   2. Does it put `construct_from_boxed` and `kind.rs::new` in ONE run's write
      set (joint), or rewrite `new` alone (the pipeline's own choice, which
      returned flat twice)?
   3. Does it leave the pipeline's other runs (sessions, the pair) in, out or
      reordered, and does its reason for that match the score history?
   4. C vs B vs A: the variant where it first names the cycle tells which input
      the model needs. A model that names it only in B says the pipeline's own
      evidence text hides it; only in A or C says nothing about the text.
  A hit in one variant of one sample is not a rate. Repeat with --samples
  before treating a difference between variants as real. The probe never
  executes the plan and its output is not evidence about any body.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "events.jsonl"
LEDGER = ROOT / "logs" / "route_ledger" / "anyhow" / "index.jsonl"
PROMPTS = ROOT / "logs" / "prompts"
# The fill-6 `src/kind.rs` file-first prompt of run 38934-2999cc94: the block the
# pipeline sent under the two-member cycle (sweep 6's report).
DEFAULT_EVIDENCE_PROMPT = (
    "e502e95b6dff4e12e0c7891766c1368698a08219d02ddf6244b807173cde0f93")


def _events(run: str) -> list[dict]:
    out = []
    needle = f'"run_id": "{run}"'
    with EVENTS.open() as fh:
        for line in fh:
            if needle in line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _at(evs: list[dict], name: str, it: int) -> list[dict]:
    return [e["data"] for e in evs if e["event"] == name
            and (e["data"].get("iter", e["data"].get("iteration")) == it)]


def _ledger(run: str) -> list[dict]:
    rows = []
    with LEDGER.open() as fh:
        for line in fh:
            if run in line:
                r = json.loads(line)
                if r.get("run_id") == run:
                    rows.append(r)
    return rows


def _short(unit) -> str:
    if isinstance(unit, (list, tuple)):
        return f"{unit[0]}::{unit[1]}"
    return str(unit)


def _evidence_block(sha: str) -> str:
    text = (PROMPTS / f"{sha}.txt").read_text(errors="replace")
    m = re.search(r"<CRASH_EVIDENCE>\n(.*?)</CRASH_EVIDENCE>", text, re.S)
    if not m:
        raise SystemExit(f"no CRASH_EVIDENCE block in prompt {sha}")
    return m.group(1).strip()


def build_state(run: str, it: int, variant: str, sha: str) -> str:
    evs = _events(run)
    rows = _ledger(run)
    hist = []
    for n in range(1, it + 1):
        for d in _at(evs, "phase_c.tdd.iter", n):
            hist.append((n, d["pass_count"], d["fail_count"]))
    now = _at(evs, "phase_c.tdd.iter", it)[0]
    failed = sorted(now["failed"])
    crashed = {}
    for d in _at(evs, "phase_c.crash.localized", it):
        d = {**d, "test": re.sub(r"\x1b\[[0-9;]*m", "", d["test"] or "")}
        crashed[d["test"]] = d
    owner = _at(evs, "phase_c.coverage.owner_map", it)[0]
    esc = {d["test"]: d["unserved"]
           for d in _at(evs, "phase_c.attribution.escalation_deferred", it)}
    svc = _at(evs, "phase_c.dispatch.service_order", it)[0]
    prev = _at(evs, "phase_c.tdd.landed_change", it - 1)[0]
    sched = _at(evs, "phase_c.fill.schedule", it)[0]

    # Per-unit history over the run, from the route ledger: (iteration, route, outcome).
    hist_by_unit: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["iteration"] >= it:
            continue
        for t in r.get("targets", []):
            hist_by_unit[_short(t)].append(
                f"fill {r['iteration']} {r['kind']}:{r['outcome']}"
                + (f"({r['reason']})" if r["outcome"] == "deferred" else ""))

    lines: list[str] = []
    a = lines.append
    a("STATE AT THE FILL BOUNDARY (recorded run, cluster stable, crate anyhow)")
    a("")
    a("Score by sweep (passing / failing of 184): "
      + ", ".join(f"sweep {n}: {p}/{f}" for n, p, f in hist))
    a("Minutes and model calls per fill so far: " + ", ".join(
        f"fill {n}: {sum(r['minutes'] or 0 for r in rows if r['iteration'] == n):.0f} min, "
        f"{sum(r['model_calls'] or 0 for r in rows if r['iteration'] == n)} calls, "
        f"net {sum(r['net_pass_delta'] or 0 for r in rows if r['iteration'] == n):+d}"
        for n in range(1, it)))
    a("")
    a(f"FAILING TESTS ({len(failed)}):")
    for t in failed:
        if t in crashed:
            a(f"  {t} — test binary `{crashed[t]['binary']}` aborted: stack overflow")
        elif t in owner["unmeasured"]:
            a(f"  {t} — its binary aborted before this test reported (no verdict of its own)")
        else:
            a(f"  {t} — assertion failure")
    a("")
    a(f"Coverage measured the executed slice for {len(owner['measured'])} failing "
      f"test(s) ({', '.join(owner['measured'])}); {len(owner['unmeasured'])} "
      "failing tests are UNMEASURED because their binary did not complete, so "
      "no executed slice exists for them.")
    a("Static attribution of unmeasured tests: units the escalation had not yet "
      "served for them: " + "; ".join(
          f"{t} -> {', '.join(u)}" for t, u in sorted(esc.items())[:4]) + " (…)")
    a("")
    a("CRASH EVIDENCE the pipeline holds for the aborted tests:")
    if variant == "A":
        a(_evidence_block(sha))
    elif variant == "B":
        d = next(iter(crashed.values()))
        a(f"Sanitizer report for `{d['binary']}` (the other {len(crashed)-1} "
          "crashed binaries print the same stack):")
        a(d["raw_report"])
    else:
        names = sorted({f"{f[0]}::{f[2]}" for d in crashed.values()
                        for f in d["frames"]})
        a(f"{len(crashed)} test binaries died with a stack overflow "
          f"(tests: {', '.join(sorted(crashed))}). The units the pipeline ranked "
          "on crash evidence: " + ", ".join(names) + ".")
    a("")
    a("SERVICE HISTORY OF THE CANDIDATE UNITS (never served = no route has "
      "written it; flat/re-buy = a graded try returned nothing):")
    a("  never served: " + ", ".join(svc["never_served"]))
    a(f"  flat: {svc['flat']}   re-buy: {svc['rebuy']}   dead: {svc['dead']}")
    a("Route history per unit (only units with a history):")
    for u in sorted(hist_by_unit):
        if u in svc["never_served"] or u in svc["rebuy"] or u in prev["unattempted"] or any(
                u in (d.get("targets") or []) for d in sched["runs"]):
            a(f"  {u}: " + "; ".join(hist_by_unit[u][-6:]))
    a("")
    a("LAST FILL'S OUTCOME (landed-change audit): "
      f"{prev['dispatched']} units dispatched; landed a change: "
      f"{', '.join(prev['landed'])}; NO ROUTE ATTEMPTED: {', '.join(prev['unattempted'])}; "
      f"unchanged: {', '.join(prev['unchanged'])}; reverted: {', '.join(prev['reverted'])}; "
      f"declined: {', '.join(prev['declined'])}.")
    a("")
    a(f"THE DETERMINISTIC PLANNER'S SCHEDULE FOR THE NEXT FILL (budget "
      f"{sched['budget_minutes']:.0f} min):")
    for i, r in enumerate(sched["runs"], 1):
        a(f"  {i}. {r['label']} — writes {', '.join(r['targets'][:16])}"
          + (" (…)" if len(r["targets"]) > 16 else "")
          + f" — aimed at {len(r.get('target_tests') or [])} failing test(s)")
    for r in sched["deferred"]:
        a(f"  deferred: {r['label']} ({r.get('reason', '?')})")
    return "\n".join(lines)


TASK = """You are the scheduler of last resort for a loop that fills the function bodies of a
Rust crate from its tests. The deterministic planner's schedule for the next fill
is at the end of the state. The score has been growing more slowly each fill.

Decide the next fill yourself. Use only the facts above.

Return JSON only, with these keys:
  "assessment": two or three sentences: why the recent fills bought so little.
  "runs": an ordered list. Each item: {"route": "file"|"impl"|"per-fn"|"session"|"pair",
          "write_set": [units written together in ONE change, as "src/x.rs::name"],
          "reason": one sentence tied to a fact above}.
  "left_out": units or planned runs you would NOT spend this fill on, each with a reason.
  "most_decisive_fact": the single observation that changed your plan the most.

Choose write sets only from units named in the state. A write set is committed or
reverted as a whole. Budget: about {budget} minutes; runs cost 8-25 minutes each."""


def call_model(prompt: str, model: str, base_url: str) -> tuple[str, str]:
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key="none", timeout=3600)
    content, reasoning = [], []
    with client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            max_tokens=40000, stream=True,
            extra_body={"chat_template_kwargs": {"reasoning_strength": "high"}},
    ) as stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            r = getattr(d, "reasoning", None) or getattr(d, "reasoning_content", None)
            if r:
                reasoning.append(r)
            if d.content:
                content.append(d.content)
    return "".join(content), "".join(reasoning)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="38934-2999cc94")
    ap.add_argument("--iter", type=int, default=6)
    ap.add_argument("--variant", default="all", choices=["A", "B", "C", "all"])
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--evidence-prompt", default=DEFAULT_EVIDENCE_PROMPT)
    ap.add_argument("--dry-run", action="store_true",
                    help="write the prompts, call nothing")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model = os.getenv("MODEL")
    if not model:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("MODEL="):
                model = line.split("=", 1)[1].strip()
    base = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
    variants = ["A", "B", "C"] if args.variant == "all" else [args.variant]
    for v in variants:
        state = build_state(args.run, args.iter, v, args.evidence_prompt)
        budget = re.search(r"budget (\d+) min", state)
        prompt = state + "\n\n" + TASK.replace(
            "{budget}", budget.group(1) if budget else "200")
        (out / f"state_{v}.txt").write_text(prompt)
        print(f"variant {v}: prompt {len(prompt)} chars", flush=True)
        if args.dry_run:
            continue
        for n in range(1, args.samples + 1):
            content, reasoning = call_model(prompt, model, base)
            (out / f"reply_{v}_{n}.txt").write_text(content)
            (out / f"reasoning_{v}_{n}.txt").write_text(reasoning)
            print(f"variant {v} sample {n}: reply {len(content)} chars, "
                  f"reasoning {len(reasoning)} chars", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
