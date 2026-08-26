#!/usr/bin/env python3
"""What the baked behaviour suite actually holds — and whether it can pin the defects the tree is stuck on.

WHY THIS EXISTS
---------------
`.skeleton/_behaviours_<cluster>.json` is harvested once at bake
(`strip_bake.py:harvest_behaviour_suite`) from doc-test fences and static
assertions, and read by t1.5's layout pins and nobody else. Two wrong claims
were made about it from memory on 2026-08-26: a tracker entry said it was
"EMPTY on disk, 0 entries" (it holds 30), and a plan priced a "behaviour-
complete specification" without stating what the suite can express today: 24
ASSERTION specs whose `then` is null by invariant (`BehaviourSpec.__post_init__`
— "the assert IS the Then"), 6 OUTCOME specs that are all compile-time
STATIC_ASSERTION pins, `exercises` empty on every spec (no writer exists), and
not one runtime observable — so 0 of the 77 generated tests can pin a
harvested behaviour by construction, and none of the three observables the
98-tree failures pin (`Error::msg("x").is::<&str>()`, the `{:#?}` alternate
Debug strings, the `Condition failed: … (1 vs 2)` ensure strings) is in it.

This script is the fixture the harvest change is judged by: run it on today's
suite for the negative control (0 runtime `then`, 0 of 3 observables), and on
the suite a new harvest writes for the acceptance check.

USAGE
-----
    .venv/bin/python scripts/behaviour_suite_census.py [SUITE.json ...]
        default: tmp/anyhow/.skeleton/_behaviours_*.json

HOW TO INTERPRET THE OUTPUT
---------------------------
  specs                       total records in the suite.
  subkind ASSERTION/OUTCOME   ASSERTION = pass means "ran panic-free", no
                              observable value stored; OUTCOME = a `then`
                              payload is stored and compared.
  runtime then by kind        OUTCOME specs whose kind is a RUNTIME kind
                              (DISPLAY/DEBUG/CHAIN/DOWNCAST_OK/PANIC/RETURN/
                              STDOUT). STATIC_ASSERTION is compile-time and
                              is listed separately. This is the number the
                              step-0 harvest exists to raise from 0.
  exercises non-empty         specs that name the units they reach — the
                              attribution channel; 0 means no writer ran.
  sources                     which files the specs were harvested from; a
                              `*.gen_tests_*.rs` here means the harvest read
                              the pipeline's own generated tests (forbidden:
                              those name the private layout).
  klass                       BehaviourClass histogram (IDENTITY, EXACT_FORMAT,
                              BOUNDED_RESOURCE, OPTION_SEMANTICS, LAYOUT_PIN,
                              CFG_WORLD, OTHER) — what KIND of promise each
                              spec makes; OTHER on a runtime spec is a
                              classifier gap, not a suite gap.
  defect observables          presence of the three literals in given/when/
                              then text, and whether the spec carrying each
                              has a RUNTIME then. Acceptance for the harvest
                              change: 3/3 present, each with a runtime then.
                              The patterns are `domain/behaviour_census.py`'s
                              — the same ones the t0d reading line uses, so
                              this script reproduces what the phase logged.
"""
from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline_core.domain.behaviour import (  # noqa: E402
    RUNTIME_OUTCOME_KINDS,
    BehaviourSubKind,
    BehaviourSuite,
)
from pipeline_core.domain.behaviour_census import (  # noqa: E402
    DEFECT_OBSERVABLES,
    defect_observable_presence,
    klass_histogram,
    render_counts,
    runtime_then_counts,
)


def census(path: Path) -> None:
    suite = BehaviourSuite.from_dict(json.loads(path.read_text(encoding="utf-8")))
    specs = suite.specs
    print(f"{path} (cluster {suite.cfg_cluster}): {len(specs)} specs")
    sub = Counter(s.subkind.name for s in specs)
    print("  subkind: " + ", ".join(f"{k} {v}" for k, v in sorted(sub.items())))
    runtime = runtime_then_counts(specs)
    static = sum(1 for s in specs if s.then is not None and s.then.kind not in RUNTIME_OUTCOME_KINDS)
    print(f"  runtime then: {sum(runtime.values())}"
          + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(runtime.items())) + ")" if runtime else "")
          + f"; compile-time (STATIC_ASSERTION) then: {static}")
    print(f"  exercises non-empty: {sum(1 for s in specs if s.exercises)} / {len(specs)}")
    print("  provenance: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(s.provenance.name for s in specs).items()))
          + "; fence: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(s.fence.name for s in specs).items())))
    src = Counter(s.source_ref.split(":")[0] for s in specs)
    print("  sources: " + ", ".join(f"{k} {v}" for k, v in sorted(src.items())))
    gen = [k for k in src if ".gen_tests_" in k]
    if gen:
        print(f"  WARNING: harvested from generated tests: {gen}")
    print("  klass: " + render_counts(klass_histogram(specs)))
    print("  defect observables:")
    presence = defect_observable_presence(specs)
    for label in DEFECT_OBSERVABLES:
        hits, with_rt = presence[label]
        print(f"    {label}: {'present' if hits else 'ABSENT'}"
              + (f" in {hits} spec(s), {with_rt} with a runtime then" if hits else ""))
    n_assert_then = sum(1 for s in specs if s.subkind is BehaviourSubKind.ASSERTION and s.then is not None)
    if n_assert_then:
        print(f"  INVARIANT BREACH: {n_assert_then} ASSERTION spec(s) carry a then")


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] or [
        Path(p) for p in sorted(glob.glob(str(REPO / "tmp" / "anyhow" / ".skeleton" / "_behaviours_*.json")))]
    if not paths:
        print("no suite files found; pass paths explicitly")
        return 2
    for p in paths:
        census(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
