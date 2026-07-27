#!/usr/bin/env python3
"""Show what layout reconciliation would install into a LIVE cluster tree.

Answers offline: does `.skeleton-filled/<cluster>/` still hold definitions the
layout store records as superseded, and exactly which text would replace them?

  tools/probe_layout_reconcile.py <project_dir> <cluster> [--apply]

`--apply` performs the splice (the same adapter the pipeline uses). Without it
the plan is printed and nothing is written.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.layout_outcome_store import load_layout_records  # noqa: E402
from pipeline_core.adapters.layout_tree_splice import apply_layout_to_tree  # noqa: E402
from pipeline_core.adapters.private_surface_store import load_private_surface  # noqa: E402
from pipeline_core.application.reconcile_layout import (  # noqa: E402
    plan_layout_reconciliation,
)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 0
    project_dir, cluster = Path(argv[1]), argv[2]
    live = project_dir / ".skeleton-filled" / cluster / "src"
    if not live.exists():
        print(f"no live tree at {live}")
        return 0

    ex = CliExtractor(Path(
        "tools/skeleton_extractor/target/release/skeleton-extractor"))
    fns = ex.enumerate_fns(live)
    types = ex.enumerate_types(live)
    print(f"live tree: {live} | {len(fns or [])} fn(s), {len(types or [])} type(s)")
    if fns is None or types is None:
        print("extractor unavailable — cannot plan")
        return 0

    surface = load_private_surface(project_dir, cluster)
    records = load_layout_records(project_dir, cluster)
    print(f"surface items: {len(surface.replaceable)} | layout rows: {len(records)}")

    plan, notes = plan_layout_reconciliation(
        surface, records, fns, types,
        lambda rel: _read(live / rel))
    for n in notes:
        print(f"  note| {n}")
    if not plan:
        print("VERDICT: live tree already holds every authored definition.")
        return 0
    for rel, reps in plan.items():
        for r in reps:
            print(f"\n  {rel}:\n    - {r.old.strip()[:200]}\n    + {r.new.strip()[:400]}")
    if "--apply" in argv:
        n, err = apply_layout_to_tree(plan, live)
        print(f"\nAPPLIED: {n} file(s) written; err={err or '(none)'}")
    else:
        print(f"\nVERDICT: {sum(len(v) for v in plan.values())} definition(s) "
              f"in {len(plan)} file(s) are superseded. Re-run with --apply.")
    return 0


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
