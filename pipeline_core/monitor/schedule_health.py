"""The monitor's reading of what the fill schedule has bought this run.

Reads the route ledger the executor appends (`logs/route_ledger/<project>/
index.jsonl`) and the invariant-breach events, scopes them to the live run
(the newest `run_id` in the ledger's tail) and folds them through
`domain.schedule_yield`. The ledger sits beside `events.jsonl`, so the path is
derived from the configured events log rather than a second knob.

The ledger is append-only and grows across runs; only its last
`_TAIL_BYTES` are read, which holds several full runs (run `82925-203ed7b8`
wrote 103 rows in 10 hours).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pipeline_core.domain.fill_schedule import RouteLedgerRow
from pipeline_core.domain.schedule_yield import (
    DryVerdict,
    ScheduleYield,
    dry_verdict,
    rows_for,
    schedule_yield,
)

__all__ = ["ScheduleHealth", "cached_schedule_health", "read_schedule_health",
           "schedule_health_line"]

_TAIL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScheduleHealth:
    run_id: str
    cluster: str
    reading: ScheduleYield
    dry: DryVerdict
    last_row_started: str


def _tail_rows(path: Path) -> list[RouteLedgerRow]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out: list[RouteLedgerRow] = []
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        row = RouteLedgerRow.from_dict(d) if isinstance(d, dict) else None
        if row is not None and row.run_id:
            out.append(row)
    return out


def read_schedule_health(events_log: str | Path, project: str = "anyhow", *,
                         now: datetime | None = None,
                         dry_minutes: float = 90.0) -> ScheduleHealth | None:
    """None until any run has written a ledger row. Scoped to the run of the
    newest row, which is the previous run until the live one closes a route
    run; the line names the run for that reason."""
    path = Path(events_log).parent / "route_ledger" / project / "index.jsonl"
    rows = _tail_rows(path)
    if not rows:
        return None
    last = rows[-1]
    mine = rows_for(rows, last.run_id, last.cluster)
    y = schedule_yield(mine)
    dry = dry_verdict(y, now or datetime.now(UTC), minutes=dry_minutes)
    return ScheduleHealth(last.run_id, last.cluster, y, dry, last.started)


def schedule_health_line(h: ScheduleHealth | None) -> str | None:
    """One plain-text line: run, newest fill's measured gain, yield, re-buy
    minutes, delayed payers, minutes since the last cash.

    `fill N +k` is what the newest fill's cashed runs measured; the score row
    moves only at the sweep, so this is where a mid-fill gain shows first.
    `yield` is cashed over spent minutes across the run's fills. The trailing
    `dry` segment is the live burn reading and the only one flagged (`BURN`).
    The run id leads because the ledger outlives runs."""
    if h is None or not h.reading.fills:
        return None
    y = h.reading
    share = f"{100 * y.cashed / y.spent:.0f}%" if y.spent >= 5 else "-"
    delay = max((p.delay_fills for p in y.payers), default=0)
    burn = "  BURN" if h.dry.burning else ""
    fill = y.fills[-1].fill
    return (f"{h.run_id.split('-')[0]}  fill {fill} {y.fill_net:+d}  "
            f"yield {share} ({y.cashed:.0f}/{y.spent:.0f} min)  "
            f"rebuy {y.rebuy_minutes:.0f} min  "
            f"delayed payers {len(y.payers)}/{y.cashed_runs}"
            + (f" (max {delay} fills)" if delay else "")
            + f"  dry {h.dry.dry_minutes:.0f} min{burn}")


_TTL_S = 30.0
_cache: tuple[float, ScheduleHealth | None] = (-1e9, None)


def cached_schedule_health(events_log: str | Path,
                           project: str = "anyhow") -> ScheduleHealth | None:
    """`read_schedule_health` at most every `_TTL_S` seconds: the ledger only
    moves when a route run closes (minutes apart), the panel redraws every
    refresh."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] >= _TTL_S:
        _cache = (now, read_schedule_health(events_log, project))
    return _cache[1]
