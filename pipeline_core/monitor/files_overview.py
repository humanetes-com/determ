"""The Pipeline box's per-file coverage overview.

Pure presentation: given the parsed pipeline progress and the per-file coverage
table, order the ``src/`` files the way the pipeline processes them and lay the
per-file cells out like inline blocks (several per row in a wide box, one per
row in a narrow one).
"""

from __future__ import annotations

import os
from typing import Any

from pipeline_core.monitor.frame import flow_columns
from pipeline_core.monitor.render import dim, green, progress_bar, red, yellow


def sorted_files(prog: dict[str, Any], cov_data: dict[str, tuple[int, int]],
                 cluster: str) -> list[tuple[str, int, int]]:
    """(name, covered, coverable) for the src/ files to show, in pipeline order.

    A non-default cluster restricts the list to the files it targets (all files
    when it has not processed any yet). Order: this pass's processing order (the
    same index as the file counter), then files from older passes not yet
    reprocessed, then unknown files by coverage ascending. Tests and build.rs
    are not part of pipeline coverage."""
    cluster_filter: set[str] | None = None
    if cluster and cluster != "stable" and prog["cluster_files"]:
        cluster_filter = set(prog["cluster_files"])
    this_pass = {f: i for i, f in enumerate(prog["files_this_pass"])}
    n = len(this_pass)
    extra = {f: n + i for i, f in enumerate(prog["all_files"]) if f not in this_pass}
    order = {**this_pass, **extra}
    fallback = n + len(extra)

    def key(x: tuple[str, int, int]) -> float:
        return order.get(x[0], fallback + (x[1] / x[2] if x[2] else 0))

    files = [(name, cov, cvbl) for name, (cov, cvbl) in cov_data.items()
             if name.startswith("src/") and (cluster_filter is None or name in cluster_filter)]
    # files_this_pass entries always appear, even when absent from the coverage JSON
    shown = {name for name, *_ in files}
    files += [(f, 0, 0) for f in prog["files_this_pass"]
              if f not in shown and f.startswith("src/")
              and (cluster_filter is None or f in cluster_filter)]
    return sorted(files, key=key)


def _file_cell(prog: dict[str, Any], cur_base: str, max_name: int,
               f: tuple[str, int, int], bar_w: int, tail: bool) -> str:
    name, covered, coverable = f
    short = os.path.basename(name)
    arrow = yellow("▶") if short == cur_base else " "
    hist = prog["queue_history"].get(short, [])
    trend = dim("  " + "→ ".join(str(c) for c in hist[-5:])) if hist and tail else ""
    qcov = prog.get("quick_cov_history", {})
    qhist = qcov.get(name, qcov.get(short, []))
    if len(qhist) >= 2:
        qtrend = dim(f"  {qhist[-2]:.1f}%→ ") + f"{qhist[-1]:.1f}%"
    elif len(qhist) == 1:
        qtrend = dim(f"  {qhist[-1]:.1f}%")
    else:
        qtrend = ""
    head = f"{arrow} {short.ljust(max_name)}  "
    if coverable == 0:
        return f"{head}[{progress_bar(0, bar_w, ' n/a ')}] {dim('  n/a')}{qtrend}{trend}"
    pct = covered / coverable * 100
    done = covered >= coverable
    col = green if done else (yellow if pct >= 80 else red)
    tick = green(" ✓") if done else ""
    return (f"{head}[{col(progress_bar(pct, bar_w, f'{pct:.1f}%'))}]"
            f"  {covered}/{coverable}{tick}{qtrend}{trend}")


def files_overview_rows(prog: dict[str, Any], cov_data: dict[str, tuple[int, int]],
                        cluster: str, width: int) -> list[str]:
    """The rule, the "files overview — n/m complete" header and the cells.

    When the full cell is too wide for a second column, shed the per-file
    history tail, then narrow the bar; whichever variant needs the fewest rows
    wins (the full cell on a tie)."""
    files = sorted_files(prog, cov_data, cluster)
    cur = prog["current_file"]
    cur_base = os.path.basename(cur) if cur != "?" else ""
    max_name = max((len(os.path.basename(n)) for n, *_ in files), default=10)
    n_done = sum(1 for _, cov, cvbl in files if cov >= cvbl)
    rows = [dim(f"  {'─' * max(4, min(width - 2, 60))}"),
            f"  files overview — {n_done}/{len(files)} complete"]
    variants = [
        flow_columns([_file_cell(prog, cur_base, max_name, f, bar_w, tail) for f in files], width)
        for bar_w, tail in ((14, True), (14, False), (8, False))
    ]
    best = variants[0]
    for v in variants[1:]:
        if len(v) < len(best):
            best = v
    return rows + best
