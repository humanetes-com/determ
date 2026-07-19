"""Probe: what does the t1.5 layout-pin gate actually CHECK for a crate?

Renders the real harvested pins of each connected component through
`build_layout_probe` and prints the Rust the gate would compile. Answers "is
this component gated, and by what?" without running the pipeline — the pins are
prompt-advisory until they render, so a component whose pins all land in
`skipped` is authored on the compile check alone.

    .venv/bin/python tools/probe_layout_pins.py [cfg_cluster]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.behaviour_store import (  # noqa: E402
    load_behaviour_suite,
)
from pipeline_core.adapters.private_surface_store import (  # noqa: E402
    load_private_surface,
)
from pipeline_core.application.author_layout_phase import (  # noqa: E402
    make_pin_selector,
)
from pipeline_core.domain.layout_probe import build_layout_probe  # noqa: E402

_PROJECT = Path("tmp/anyhow")


def main() -> None:
    cluster = sys.argv[1] if len(sys.argv) > 1 else "stable"
    surface = load_private_surface(_PROJECT, cluster)
    suite = load_behaviour_suite(_PROJECT, cluster)
    select = make_pin_selector(suite, surface)

    for comp in surface.components:
        names = tuple(i.name for i in comp)
        pins = select(comp)
        pinned_types = tuple(sorted(
            n for n in surface.public_direct_refs
            if surface.reachable_from_public(n) & {i.name for i in comp}))
        probe = build_layout_probe(pins, pinned_types=pinned_types)
        head = f"{names[:4]}{'…' if len(names) > 4 else ''} ({len(comp)} items)"
        if not pins:
            print(f"\n=== {head}\n  no pins harvested — authored on compile only")
            continue
        print(f"\n=== {head}\n  pins={len(pins)} gated={len(probe.gated)} "
              f"skipped={len(probe.skipped)} types={pinned_types}")
        for s in probe.skipped:
            print(f"  SKIPPED: {s[:110]}")
        for line in probe.source.splitlines():
            if line.startswith(("const _", "fn _layout")):
                print(f"  GATE: {line[:140]}")


if __name__ == "__main__":
    main()
