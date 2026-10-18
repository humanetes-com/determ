#!/usr/bin/env python3
"""Were the recorded wrong-body fills told the intent of the test they failed?

WHY. Entry 291 asks whether a plain-language use-case layer would move a
verdict. It can only convert a wrong-body fill whose prompt LACKED the
intent; a fill whose prompt already quoted the failing assertion and the
observed value and still wrote the wrong body is the model's class (329's
class M), and restating the same test in words adds nothing to it. The repo
holds instances of the second kind (the file-first fill that quoted the test
and re-emitted the wrong constructor). Step 1 of 291, read on the source
2026-10-17: every fill builder already carries the book through
`layout_authoring.behaviour_must_instruction` (file plan line, per-impl,
per-fn, fix builders, session turn 0), so a phrase layer would reach the
prompts through that one slot. This script is step 2: the count that
decides whether a step 3 exists.

USAGE.
    .venv/bin/python scripts/behaviour_intent_census.py [--since 2026-10-01]
        [--events logs/events.jsonl] [--examples 5]
  Read-only: `scripts/fill_join.py`'s per-fill join over the events, and the
  prompt store `logs/prompts/<sha>.txt`. No model, no cargo, no GPU.

HOW TO INTERPRET THE OUTPUT.
  wrong-body fills   fills the pipeline classed `wrong` (`phase_c.fill.class`)
                     in runs since --since, with at least one prompt on disk.
  intent present     the fill's prompts quote an assertion of a test
                     (`assert…!(` inside a quoted test, or the sweep's own
                     words: `observed at line`, `actual =`, `said:`, a
                     `panicked at … tests/` line) — the model was shown what
                     the test checks.
  intent absent      none of those markers in any of the fill's prompts.
  book rules shown   prompts that carry the book's RULES block.
  DECISION RULE. A plain-language layer is worth a step 3 only when
  `intent absent` is a material share of wrong-body fills (the conventions
  give no fixed threshold; read it beside the absolute count) — and then
  only through the builder slot named above. When `intent present`
  dominates, the wrong bodies are model-limited with the words in hand and
  291 closes as not warranted for the score.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import fill_join  # noqa: E402

_PROMPTS = REPO / "logs" / "prompts"
_INTENT = (
    re.compile(r"assert(?:_eq|_ne)?!\s*\("),
    re.compile(r"observed at line \d+"),
    re.compile(r"\bactual\s*="),
    re.compile(r"\bsaid:"),
    re.compile(r"panicked at [^\n]*tests/"),
)
_RULES = re.compile(r"\bRULES\b")


def _run_ids(events: Path, since: str) -> list[str]:
    seen: dict[str, None] = {}
    with open(events, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase_c.fill.class"' not in line:
                continue
            ts = line[8:18]
            if ts < since:
                continue
            m = re.search(r'"run_id": "([^"]+)"', line)
            if m:
                seen.setdefault(m.group(1), None)
    return list(seen)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="2026-10-01")
    ap.add_argument("--events", type=Path, default=REPO / "logs" / "events.jsonl")
    ap.add_argument("--examples", type=int, default=5)
    a = ap.parse_args()
    counts: Counter[str] = Counter()
    by_route: Counter[tuple[str, str]] = Counter()
    rules_shown = 0
    examples: dict[str, list[str]] = {"present": [], "absent": []}
    for run in _run_ids(a.events, a.since):
        for f in fill_join.build_fill_table(a.events, run):
            if f.fill_class != "wrong":
                continue
            texts = []
            for c in f.calls:
                p = _PROMPTS / f"{c.prompt_sha}.txt"
                if p.is_file():
                    texts.append(p.read_text(encoding="utf-8", errors="replace"))
            if not texts:
                continue
            joined = "\n".join(texts)
            kind = "present" if any(r.search(joined) for r in _INTENT) else "absent"
            counts[kind] += 1
            by_route[(f.route, kind)] += 1
            rules_shown += bool(_RULES.search(joined))
            if len(examples[kind]) < a.examples:
                examples[kind].append(f"{run} {f.route} {f.unit} "
                                      f"({f.calls[0].prompt_sha[:12]})")
    total = sum(counts.values())
    print(f"wrong-body fills since {a.since}: {total}")
    for kind in ("present", "absent"):
        share = f"{100 * counts[kind] / total:.0f}%" if total else "-"
        print(f"  intent {kind}: {counts[kind]} ({share})")
        for ex in examples[kind]:
            print(f"      {ex}")
    print("  by route: " + ", ".join(f"{r} {k} {n}" for (r, k), n in sorted(by_route.items())))
    print(f"  book rules shown: {rules_shown} of {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
