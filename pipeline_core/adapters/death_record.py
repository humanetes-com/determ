"""The run-death record (`logs/deaths.jsonl`), read.

`lib/record_death.sh` wraps every launch and appends one row per pipeline exit:
`{"started_at", "ended_at", "uptime_s", "pid", "exit_status", "signal",
"deliberate_stop", "code_id", "dmesg", "log_tail"}`. It is written for a human
reading back a run that vanished, and until 2026-08-15 nothing in the pipeline
consulted it.

One reader so far: the layout retrigger's interrupted-attempt settlement. An
attempt is charged and marked before the authoring call, so a process killed in
that window leaves a marker with no outcome; the refund is booked as a mechanical
failure, capped at 2, on the argument that an authoring call which reliably kills
its own process must not re-fire for ever. On `anyhow` 2026-08-15 the two charges
that spent that cap on the 34-name construct/vtable component came from operator
SIGTERMs 57 minutes and 24 seconds into runs `462635-c8369188` and
`496645-9b9de7e1` — this file's rows say so in as many words (`"signal":"TERM"`,
one of them `"deliberate_stop":true`) — and the next run refused a component it
had graded OWNS off an in-crate ASAN origin, with no model attempt ever failing.

SIGKILL is deliberately NOT read as a stop: the OOM killer sends it too, and a
death whose cause cannot be told apart from a pipeline fault keeps the charge.
"""
from __future__ import annotations

import json
from pathlib import Path

# The signals a stop REQUEST arrives as. `continue.sh --y` writes the graceful
# sentinel and the pipeline exits at its next checkpoint (recorded
# `deliberate_stop: true`); `--force` and a bare `kill` send SIGTERM with no
# sentinel, which is still an external decision and not evidence about any
# component. SIGINT is the same decision typed at a terminal.
_STOP_SIGNALS = ("TERM", "INT")

_LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"


def deliberate_stop_pids(logs_dir: Path | None = None) -> frozenset[int]:
    """Every pid the death record says ended on an external stop.

    Missing file, unreadable line, absent field: skipped. This answers "may the
    charge be waived", so an unreadable record must fall back to charging —
    never to waiving on evidence nobody could read."""
    path = (logs_dir or _LOGS_DIR) / "deaths.jsonl"
    out: set[int] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        pid = row.get("pid")
        if not isinstance(pid, int):
            continue
        if row.get("deliberate_stop") is True \
                or row.get("signal") in _STOP_SIGNALS:
            out.add(pid)
    return frozenset(out)
