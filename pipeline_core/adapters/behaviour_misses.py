"""What the capture phase could NOT observe, and why — the other half of the
behaviour suite.

WHY THIS EXISTS. A probe that fails to compile, fails to run, times out, prints
nothing, is refused as not value-bearing, or does not reproduce between two runs
used to leave exactly one line in the log and nothing else: no event, no stored
program, no stderr — and `materialized_probe` unlinks the source in a `finally`,
so even the program was gone. The set of things we know we cannot observe was
therefore invisible, and a behaviour that vanishes silently is precisely the
silent divergence the capture route exists to remove.

WHY NOT AS A SPEC. The obvious shortcut is to admit a miss into the suite with
`then=None`. That is today's ASSERTION shape: `classify_behaviour` returns
OTHER for it, `MUST_CLASS_ORDER` ranks OTHER last so t1.5's cap discards it, and
`is_pinnable` is false so no generated test can ever pin it. A silently dropped
behaviour would become a silently uncheckable suite row — the same hole under
another name. The misses live in their own file, keyed the same way the suite is
(per cluster), so they can be counted, read and eventually turned into the work
list for a differential harness without ever being mistaken for a specification.

STORAGE. `.skeleton/_behaviour_misses_<cluster>.json`, a sibling of
`_behaviours_<cluster>.json`, rewritten whole on each capture pass INCLUDING a
pass that missed nothing (a miss is a statement about THIS pass's attempt, not
an accumulating log — writing only when the list is non-empty would leave the
previous pass's file standing as if it were current). Missing file means no
pass has run; unreadable is reported rather than read as empty, for the same
reason `load_behaviour_suite` distinguishes them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_core.log import emit, log

MISS_REASONS = (
    "not value-bearing",   # the `when` compared instead of yielding a value
    "not reproducible",    # two runs of the same program printed different text
    "no stdout",           # ran clean and observed nothing
    "failed",              # did not compile, panicked, or timed out
    # …and the reply-level and replay-level ones, which are about the ROUTE
    # rather than about one program:
    "reply arrived in the legacy free-form shape",   # the prompt did not land
    "reply carried no parseable <BEHAVIOUR> block",  # the answer was dropped
    "over the blocks-per-reply cap",                 # proposed, never run
    "replay disagreed with the capture",             # emitted test failed
    "environment-dependent",                         # the value moves with the env
)


def misses_path(project_dir: Path, cfg_cluster: str) -> Path:
    return Path(project_dir) / ".skeleton" / f"_behaviour_misses_{cfg_cluster}.json"


def save_behaviour_misses(
    project_dir: Path, cfg_cluster: str, misses: list[dict[str, str]],
) -> Path:
    """Write this pass's misses and announce the count.

    Announcing matters as much as storing: the count is the denominator half of
    "how complete is the specification" — captured plus known-uncapturable is
    what was attempted, and only the first number was ever visible."""
    p = misses_path(project_dir, cfg_cluster)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"cfg_cluster": cfg_cluster, "misses": misses}
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    by_reason: dict[str, int] = {}
    for m in misses:
        head = (m.get("reason") or "").split(":", 1)[0].strip() or "unknown"
        by_reason[head] = by_reason.get(head, 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(by_reason.items())) or "none"
    log(f"  [task0d] cluster {cfg_cluster}: {len(misses)} behaviour(s) not "
        f"captured ({summary}) -> {p.name}"
        if misses else
        f"  [task0d] cluster {cfg_cluster}: every attempted behaviour was "
        f"captured; empty miss record written -> {p.name}")
    emit("task0d.behaviour_misses",
         data={"cluster": cfg_cluster, "count": len(misses),
               "by_reason": by_reason})
    return p


def load_behaviour_misses(project_dir: Path, cfg_cluster: str) -> list[dict[str, str]]:
    """This pass's misses, or `[]`. An unreadable file is REPORTED, not read as
    empty — an empty list and a corrupt file mean opposite things about whether
    capture was attempted."""
    p = misses_path(project_dir, cfg_cluster)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw["misses"]
        if not isinstance(items, list):
            raise TypeError("misses is not a list")
        return [dict(m) for m in items]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log(f"  [task0d] cluster {cfg_cluster}: miss store unreadable "
            f"({type(exc).__name__}: {exc}) — treating as empty, but it is NOT "
            f"the same as 'nothing was missed': {p}")
        emit("task0d.behaviour_misses_unreadable",
             data={"cluster": cfg_cluster, "path": str(p),
                   "error": type(exc).__name__})
        return []
