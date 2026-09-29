#!/usr/bin/env python3
"""Does behaviour text in a fill prompt move the fill's verdicts, and who wrote the oracle's tests?

WHY THIS EXISTS
    The behaviour book (2026-09-29) replaced the per-fn USAGE_EXAMPLES dump of
    whole test items with setup slices, and added checked RULES to the
    instruction slot of every fill builder. Neither was ever measured against
    verdicts: `scripts/measure_behaviour_prompt_reach.py` counts whether the
    block was PRESENT in prompts, not whether a fill that carried it paid.
    And the growth of the anyhow `stable` oracle from 125 to 184 tests was
    credited to t0c's "each test must pin a listed behaviour" demand without
    a count. This script is the baseline both need: run it on the runs before
    a re-bake, then on the runs after, and compare.

USAGE
    scripts/measure_behaviour_example_effect.py [--project anyhow] [--cluster stable]
        [--run RUN_ID ...] [--since YYYY-MM-DD] [--events logs/events.jsonl]

    Read-only: streams `logs/events.jsonl` (300+ MB is fine, one pass), reads
    `logs/prompts/<sha>.txt`, `tmp/<project>/tests/`, `tmp/<project>/src/` and
    `tmp/<project>/.skeleton/_behaviours_<cluster>.json`. No GPU, no cargo, no
    model; safe during a live run.

HOW TO INTERPRET THE OUTPUT
    (a) per-fn fills by prompt content
        One row per per-fn fill (a `phase_c.fn.fill.start` … `.done` window on
        one thread) whose prompt(s) were found in `logs/prompts`. Columns:
        `examples` = a prompt carried a non-empty <USAGE_EXAMPLES>; `rules` =
        a prompt carried the book's RULES instruction. The grade is the
        unit's state in the FIRST `phase_c.ledger.judged` after the fill:
        `proven` (its attributed tests moved to pass), `flat`, `refuted`,
        `unmeasured`, or `-` (never judged — the run ended, or the landing was
        reverted before a sweep). The line to read is `proven rate`: proven /
        judged, per group. A group with fewer than ~20 judged fills is noise;
        say so rather than read a rate off it.
        Decision rule: if `examples=yes` does not beat `examples=no` on the
        pre-book runs, the whole-test dump was inert and that is the negative
        baseline the book's `rules=yes` row is compared against. `rules=yes`
        beating `rules=no` on post-bake runs, at comparable n, is the reading
        the book was built for; equal rates mean the sentences are in the
        prompt and do not change what the model writes.
    (b) oracle tests by writer
        `#[test]` items under `tests/` and `src/*.gen_tests_*.rs`, split by
        who wrote them: `original` (the crate's own), `t0c pins a behaviour`
        (a pipeline-generated test whose item pins at least one harvested
        behaviour id, by `behaviours_pinned_by`), `t0c other` (generated,
        pins none), `capture replay` (`behaviour_<cluster>_gen_*.rs`, t0d's
        emitted captures). The t0c demand is credited with exactly the first
        pipeline row, nothing else.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline_core.adapters.behaviour_store import load_behaviour_suite  # noqa: E402
from pipeline_core.domain.behaviour_pinning import behaviours_pinned_by  # noqa: E402
from pipeline_core.domain.generation import is_claimed_pipeline_gen_test  # noqa: E402
from pipeline_core.domain.refill_evidence import test_item_source  # noqa: E402
from pipeline_core.domain.rust_scan import attributed_test_fn_names  # noqa: E402

_RULES_MARK = "RULES (observable invariants"


def _prompt_flags(sha: str) -> tuple[bool, bool] | None:
    p = REPO / "logs" / "prompts" / f"{sha}.txt"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ex = False
    at = text.find("<USAGE_EXAMPLES>")
    if at >= 0:
        end = text.find("</USAGE_EXAMPLES>", at)
        ex = bool(text[at + len("<USAGE_EXAMPLES>"):end if end > 0 else None].strip())
    return ex, _RULES_MARK in text


def per_fn_rows(events: Path, runs: set[str], since: str) -> list[dict[str, object]]:
    open_fill: dict[tuple[str, str], dict[str, object]] = {}
    fills: list[dict[str, object]] = []
    pending: dict[tuple[str, str], list[dict[str, object]]] = collections.defaultdict(list)
    with events.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.fn.fill.' not in line and '"llm.request"' not in line \
                    and '"phase_c.ledger.judged"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            run = str(ev.get("run_id", ""))
            if (runs and run not in runs) or str(ev.get("ts", ""))[:10] < since:
                continue
            kind = ev.get("event")
            data = ev.get("data") or {}
            key = (run, str(ev.get("thread", "")))
            if kind == "phase_c.fn.fill.start":
                open_fill[key] = {"run": run, "unit": f"{data.get('file')}::{data.get('fn')}",
                                  "shas": [], "grade": "-"}
            elif kind == "llm.request" and key in open_fill:
                sha = data.get("prompt_sha") or (data.get("prompt") or {}).get("sha256")
                if sha:
                    open_fill[key]["shas"].append(str(sha))  # type: ignore[union-attr]
            elif kind == "phase_c.fn.fill.done" and key in open_fill:
                row = open_fill.pop(key)
                fills.append(row)
                pending[(run, str(row["unit"]))].append(row)
            elif kind == "phase_c.ledger.judged":
                for entry in data.get("units") or ():
                    unit, _, state = str(entry).rpartition(": ")
                    for row in pending.pop((run, unit), ()):
                        row["grade"] = state
    return fills


def section_a(args: argparse.Namespace) -> None:
    rows = per_fn_rows(Path(args.events), set(args.run or ()), args.since)
    table: dict[tuple[str, str], collections.Counter[str]] = collections.defaultdict(collections.Counter)
    unread = 0
    for r in rows:
        flags = [f for f in (_prompt_flags(s) for s in r["shas"]) if f is not None]  # type: ignore[union-attr]
        if not flags:
            unread += 1
            continue
        ex = "yes" if any(f[0] for f in flags) else "no"
        ru = "yes" if any(f[1] for f in flags) else "no"
        table[(ex, ru)][str(r["grade"])] += 1
    print(f"(a) per-fn fills: {len(rows)} window(s), {unread} with no stored prompt")
    for (ex, ru), c in sorted(table.items()):
        judged = sum(v for k, v in c.items() if k != "-")
        rate = f"{c['proven'] / judged:.0%}" if judged else "n/a"
        body = ", ".join(f"{k} {v}" for k, v in sorted(c.items()))
        print(f"    examples={ex:3} rules={ru:3}  judged {judged:4}  proven rate {rate:>4}  ({body})")


def section_b(args: argparse.Namespace) -> None:
    root = REPO / "tmp" / args.project
    suite = load_behaviour_suite(root, args.cluster)
    counts: collections.Counter[str] = collections.Counter()
    files = sorted((root / "tests").rglob("*.rs")) + sorted((root / "src").glob("*.gen_tests_*.rs"))
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        names = list(dict.fromkeys(attributed_test_fn_names(text)))
        if path.name.startswith("behaviour_") and "_gen_" in path.name:
            counts["capture replay"] += len(names)
            continue
        if not is_claimed_pipeline_gen_test(path.name, text):
            counts["original"] += len(names)
            continue
        for n in names:
            pins = behaviours_pinned_by(test_item_source(text, n), suite.specs)
            counts["t0c pins a behaviour" if pins else "t0c other"] += 1
    total = sum(counts.values())
    print(f"(b) oracle tests by writer ({root / 'tests'}, suite format {suite.format_version}, "
          f"{len(suite.specs)} behaviour(s)): {total}")
    for k in ("original", "t0c pins a behaviour", "t0c other", "capture replay"):
        print(f"    {k:22} {counts[k]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--run", action="append")
    ap.add_argument("--since", default="0000-00-00")
    ap.add_argument("--events", default=str(REPO / "logs" / "events.jsonl"))
    args = ap.parse_args()
    section_a(args)
    section_b(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
