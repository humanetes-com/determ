#!/usr/bin/env python3
"""How much of the fill frontier the harvested behaviour suite can actually reach.

WHY THIS EXISTS
    On 2026-08-28, during run `9699-7631b787` (`code=ab5f47a5b8f0`), the
    behaviour suite was found to reach ZERO fill prompts. The content was
    fine — `select_usage_examples` returns 956 chars for
    `src/error.rs::fmt [impl Display for Error]` on the live suite — but the
    only construction site that passes `usage_examples` is
    `fill_one_fn.py:546`, feeding `build_per_fn_prompt`, and that builder had
    not produced a prompt since 2026-08-18: over the 2,025 prompts stored in
    the ten days after, the dispatch was 1107 per-impl, 303 file-first, 104
    maximal-impl, 42 per-fn-fix, 13 behaviour-fix, 0 per-fn.

    A section wired to one builder while dispatch moves to another is silent by
    construction — the block simply does not render, and no log line says so.
    This script makes that state measurable in one command, before and after
    any change to the builders, so "behaviours now reach the fills" is a number
    and not an argument.

    Sibling: `scripts/behaviour_suite_census.py` answers what the suite HOLDS
    (classes, pinnability, whether an observable is expressible). This one
    answers where it GOES. Run the census when the question is the harvest;
    run this when the question is the prompt.

USAGE
    scripts/measure_behaviour_prompt_reach.py [cluster] [--since YYYY-MM-DD]

    cluster   defaults to `stable`. Reads
              `tmp/<project>/.skeleton/_behaviours_<cluster>.json` and the stub
              frontier in `tmp/<project>/.skeleton-filled/<cluster>`, so it
              needs a workspace that has been baked and seeded (post t1.5).
    --since   window for the prompt-side census over `logs/prompts`
              (default: 10 days back from today, by file mtime).

    Read-only, no GPU, no cargo, no model. Safe to run against a LIVE run; it
    only reads the skeleton and the prompt log. Needs the extractor binary at
    tools/skeleton_extractor/target/release/skeleton-extractor.

HOW TO INTERPRET THE OUTPUT
    suite            composition of the harvested suite. `probe` is the only
                     row carrying information the tree does not already assert
                     (testassert/doctest specs are read out of tests and doc
                     fences that already run every sweep).
    frontier reach   `N/M units` = how many stubbed units WOULD receive a
                     non-empty <USAGE_EXAMPLES> block if the builder they are
                     dispatched to carried one. This is the ceiling any wiring
                     change can hit. Units with no example are expected to be
                     the vtable/`object_*` internals — they have no public
                     usage, and the selector declining on them is correct, not
                     a miss.
    block/file reach the same ceiling at the granularity the per-impl and
                     file-first builders would select at. A per-block number
                     far below the per-unit number means selecting once per
                     block would discard what per-fn selection finds — that is
                     the specific way this change ships and still does not
                     fire.
    prompts          what actually went out. `with USAGE_EXAMPLES` is the
                     number that must move after a wiring change; the openers
                     table names which builder produced each prompt, so a zero
                     next to a builder that dominates the table is the defect
                     restated.

    Decision rule: if `frontier reach` is high and `prompts with
    USAGE_EXAMPLES` is 0, the suite is being built and thrown away, and the fix
    is at the builder named by the largest opener row — not in the behaviour
    code.
"""
from __future__ import annotations

import collections
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline_core.adapters.behaviour_store import load_behaviour_suite  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.domain.behaviour_select import select_usage_examples  # noqa: E402

PROJECT_DIR = REPO / "tmp" / "anyhow"
EXTRACTOR = REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
CHAR_BUDGET = 1200          # the budget the live call site uses (fill_one_fn.py:546)

# First line -> the builder that wrote it. Kept as a prefix match because the
# openers are stable across runs and the builders are not otherwise labelled in
# the stored prompt.
OPENERS = {
    "Implement ONE Rust function so its tests pass.": "build_per_fn_prompt (carries behaviours)",
    "Implement the marked [STUB] fns in this Rust impl": "build_per_impl_prompt",
    "Write EVERY function listed below for this Rust fi": "build_file_fill_prompt",
    "RE-implement ONE Rust function. Previous attempt b": "build_per_fn_fix_prompt",
    "Previous attempts to implement functions in this R": "maximal-impl retry",
    "ONE Rust function compiles but FAILS its tests": "build_behavior_fix_prompt",
}


def _label(first_line: str) -> str:
    for prefix, name in OPENERS.items():
        if first_line.startswith(prefix):
            return name
    return first_line[:46]


def main() -> int:
    cluster = "stable"
    days = 10
    args = sys.argv[1:]
    if args and not args[0].startswith("-"):
        cluster = args[0]
        args = args[1:]
    if args and args[0] == "--since" and len(args) > 1:
        days = None
        since = args[1]
    else:
        since = None

    specs = load_behaviour_suite(PROJECT_DIR, cluster).specs
    if not specs:
        print(f"no behaviour suite for cluster {cluster!r} — nothing to measure")
        return 1
    kinds = collections.Counter(s.behaviour_id.split("::")[0] for s in specs)
    print(f"suite [{cluster}]: {len(specs)} spec(s) — "
          + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))

    filled = PROJECT_DIR / ".skeleton-filled" / cluster
    recs = CliExtractor(EXTRACTOR).enumerate_fns(filled) or []
    stubs = [r for r in recs
             if r.has_unimplemented_body and r.file.startswith("src/")
             and r.file != "src/nightly.rs"]
    if not stubs:
        print(f"no stubbed units under {filled} — run this after the cluster is seeded")
        return 1

    sized = {(r.file, r.container, r.fn_name):
             len(select_usage_examples(specs, fn_name=r.fn_name,
                                       container=r.container, char_budget=CHAR_BUDGET))
             for r in stubs}
    hit = [k for k, n in sized.items() if n]
    sizes = sorted(n for n in sized.values() if n)
    print(f"frontier reach: {len(hit)}/{len(stubs)} units would get a non-empty "
          f"<USAGE_EXAMPLES> (budget {CHAR_BUDGET})")
    if sizes:
        print(f"  section size: min {sizes[0]} / median {sizes[len(sizes)//2]} / max {sizes[-1]} chars")
    print("  by file: " + ", ".join(f"{f} {n}" for f, n in
                                    collections.Counter(k[0] for k in hit).most_common()))
    empty = [k[2] for k, n in sized.items() if not n]
    print(f"  no example ({len(empty)}): {', '.join(empty[:12])}"
          + (" …" if len(empty) > 12 else ""))

    blocks = collections.defaultdict(list)
    files = collections.defaultdict(list)
    for (f, c, _fn), n in sized.items():
        blocks[(f, c)].append(n)
        files[f].append(n)
    print(f"block reach: {sum(1 for v in blocks.values() if any(v))}/{len(blocks)} impl block(s) "
          f"hold at least one unit with examples")
    print(f"file reach:  {sum(1 for v in files.values() if any(v))}/{len(files)} file(s)")

    prompts = REPO / "logs" / "prompts"
    if not prompts.is_dir():
        print("no logs/prompts — prompt-side census skipped")
        return 0
    cmd = ["find", str(prompts), "-type", "f", "-size", "+5k"]
    cmd += ["-newermt", since] if since else ["-mtime", f"-{days}"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
    window = since or f"last {days} days"
    openers: collections.Counter[str] = collections.Counter()
    carrying = 0
    for path in out:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        openers[_label(text.split("\n", 1)[0])] += 1
        if "USAGE_EXAMPLES" in text:
            carrying += 1
    print(f"prompts ({window}): {len(out)} stored, {carrying} with USAGE_EXAMPLES")
    for name, n in openers.most_common(8):
        print(f"  {n:6}  {name}")
    # Window-independent, and the number that settles the question: when did a
    # prompt last actually carry the section? A window that straddles the day
    # the dispatch changed reports a non-zero count for a dead channel.
    newest, newest_at = "", 0.0
    for entry in prompts.iterdir():
        if not entry.is_file():
            continue
        try:
            if "USAGE_EXAMPLES" not in entry.read_text(encoding="utf-8", errors="replace"):
                continue
            mt = entry.stat().st_mtime
        except OSError:
            continue
        if mt > newest_at:
            newest, newest_at = entry.name, mt
    if newest:
        print(f"newest prompt carrying USAGE_EXAMPLES: "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(newest_at))}  {newest[:16]}…")
    else:
        print("newest prompt carrying USAGE_EXAMPLES: none on record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
