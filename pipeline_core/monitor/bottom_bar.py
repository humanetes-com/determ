"""The monitor's two bottom rows: the action/feedback message and the key legend.

Pure: every state the rows depend on arrives as an argument, so `monitor.py`
(a god-file that may only shrink) keeps a call and the demo banner, the view
tags and the toggles are testable without a terminal.
"""
from __future__ import annotations

from collections.abc import Collection, Sequence

from pipeline_core.monitor.phase_panel import demo_banner
from pipeline_core.monitor.render import bold, cyan, dim, green, red, rev, yellow

__all__ = ["bottom_rows"]


def bottom_rows(
    *, views: Sequence[tuple[str, str, str]], shown: Collection[str], running: bool,
    split: bool, split_enabled: bool, hints_on: bool, shutdown_armed: bool,
    auto_armed: bool, demo: bool, demo_interval: int, action_msg: str,
) -> list[str]:
    """`[message, legend]`. `shown` holds the ids of the views on screen."""
    parts = []
    for vid, key, label in views:
        tag = f"[{key}]{label}"
        parts.append(rev(bold(tag)) if vid in shown else dim(tag))
    if split:
        split_tag = cyan("[v]split on [<>]")
    elif split_enabled:
        split_tag = yellow("[v]split (too narrow)")
    else:
        split_tag = dim("[v]split")

    def view_tag(vid: str, tag: str) -> str:
        return rev(bold(tag)) if vid in shown else dim(tag)

    parts += [
        view_tag("legend", "[l]egend"), view_tag("phases", "[e]xplain"),
        view_tag("health", "[h]ealth"), view_tag("fields", "[f]ields"),
        split_tag,
        cyan("[t]ips on") if hints_on else dim("[t]ips off"),
        green("[c]ontinue"), cyan("[g]raceful"), yellow("[w]ipe"),
        (red if running else dim)("[s]top"),
        red("[p]⏻armed") if shutdown_armed else dim("[p]oweroff"),
        cyan("[a]uto ON") if auto_armed else dim("[a]uto"),
        dim("[↑↓/PgUp/PgDn]"), dim("[q]uit"),
    ]
    # Demo mode switches the view by itself, so it is named in both rows: the
    # banner says it is on and which key stops it, the legend keeps the key
    # discoverable while it is off.
    parts.insert(0, yellow("[d]emo ON") if demo else dim("[d]emo"))
    msg = f"  {action_msg}" if action_msg else ""
    if demo:
        msg = f"  {demo_banner(demo_interval)}{msg}"
    return [msg, "  ".join(parts)]
