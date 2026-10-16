"""SQLiteRunStatsStore — the durable record of what each run and phase cost.

`logs/run_stats.db` beside `events.jsonl` (the same directory, so a test that
redirects the event log redirects this too). Two tables:

* `runs` — one row per pipeline process: code identity, configuration,
  starting tree, start/end, terminal outcome and whole-run work. Written
  `unfinished` (`ended` NULL) at start and closed at a normal exit, so a
  signalled run honestly stays unfinished;
* `phase_samples` — one row per closed phase or step (outcome not
  `skipped`), with its wall time and work counters;
* `sweeps` — one row per TDD sweep that measured (`domain.sweep_trajectory`):
  the score, the denominator, and the clock and call count at the reading.
  The fill phase rarely closes inside one process, so this is the only
  per-run record of how its score climbed.

The pipeline is the only writer; the monitor and tools open it read-only.
WAL journaling lets a reader poll while the run writes. Append-only by use:
nothing rewrites a sample.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.phase_cost import WORK_FIELDS, PhaseKey, PhaseSample, PhaseWork
from ..domain.phase_cost_baseline import RunRecord, SampleRow
from ..domain.sweep_trajectory import SweepRow

RUN_STATS_DB_NAME = "run_stats.db"

_WORK_COLS = ", ".join(f"{f} INTEGER NOT NULL DEFAULT 0" for f in WORK_FIELDS)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    crate TEXT NOT NULL,
    code_key TEXT,
    tree_stamp TEXT,
    commit_sha TEXT,
    config_fp TEXT,
    config_json TEXT,
    origin TEXT,
    started TEXT NOT NULL,
    ended TEXT,
    outcome TEXT,
    wall_ms INTEGER,
    argv TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    note TEXT,
    {_WORK_COLS}
);
CREATE TABLE IF NOT EXISTS phase_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    module TEXT NOT NULL DEFAULT '',
    cluster TEXT NOT NULL DEFAULT '',
    scope TEXT,
    parent_scope TEXT,
    started TEXT,
    wall_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    {_WORK_COLS}
);
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    cluster TEXT NOT NULL DEFAULT '',
    iter INTEGER NOT NULL,
    ts TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    pass_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL DEFAULT 0,
    oracle_static INTEGER NOT NULL DEFAULT 0,
    frontier INTEGER NOT NULL DEFAULT 0,
    stall INTEGER NOT NULL DEFAULT 0,
    improved INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS phase_samples_run ON phase_samples(run_id);
CREATE INDEX IF NOT EXISTS sweeps_run ON sweeps(run_id);
CREATE INDEX IF NOT EXISTS runs_crate ON runs(crate, started);
"""


def run_stats_path(events_log: str | Path) -> Path:
    return Path(events_log).with_name(RUN_STATS_DB_NAME)


@dataclass(frozen=True, slots=True)
class StoredRun:
    """A `runs` row as the readers see it."""

    record: RunRecord
    tree_stamp: str | None
    commit: str | None
    config: dict[str, Any]
    ended: str | None
    outcome: str | None
    wall_ms: int | None
    work: PhaseWork
    source: str
    note: str | None


class SQLiteRunStatsStore:
    """Thread-safe writer/reader over one SQLite file. `read_only` opens an
    existing file without creating it or the schema (the monitor)."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        if read_only:
            uri = f"file:{self.path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False,
                                         timeout=5.0)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                         timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── writes ──────────────────────────────────────────────────────────────

    def _write(self, sql: str, params: Sequence[Any]) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def open_run(self, *, run_id: str, crate: str, code_key: str | None,
                 tree_stamp: str | None, commit: str | None,
                 config_fp: str | None, config: Mapping[str, Any] | None,
                 started: str, argv: Sequence[str] | None = None,
                 source: str = "live", note: str | None = None) -> None:
        self._write(
            "INSERT OR REPLACE INTO runs (run_id, crate, code_key, tree_stamp, "
            "commit_sha, config_fp, config_json, started, argv, source, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, crate, code_key, tree_stamp, commit, config_fp,
             json.dumps(dict(config), default=str) if config is not None else None,
             started, json.dumps(list(argv)) if argv is not None else None,
             source, note))

    def set_origin(self, run_id: str, origin: str) -> None:
        self._write("UPDATE runs SET origin = ? WHERE run_id = ?", (origin, run_id))

    def close_run(self, run_id: str, *, ended: str, outcome: str, wall_ms: int,
                  work: PhaseWork) -> None:
        sets = ", ".join(f"{f} = ?" for f in WORK_FIELDS)
        self._write(
            f"UPDATE runs SET ended = ?, outcome = ?, wall_ms = ?, {sets} "
            "WHERE run_id = ?",
            (ended, outcome, wall_ms, *work.as_dict().values(), run_id))

    def set_run_work(self, run_id: str, work: PhaseWork, *,
                     wall_ms: int | None = None, ended: str | None = None,
                     outcome: str | None = None) -> None:
        """Backfill's form: fill what is known of an imported run."""
        sets = ", ".join(f"{f} = ?" for f in WORK_FIELDS)
        self._write(
            f"UPDATE runs SET {sets}, wall_ms = COALESCE(?, wall_ms), "
            "ended = COALESCE(?, ended), outcome = COALESCE(?, outcome) "
            "WHERE run_id = ?",
            (*work.as_dict().values(), wall_ms, ended, outcome, run_id))

    def add_sample(self, run_id: str, sample: PhaseSample, *,
                   source: str = "live") -> None:
        cols = ", ".join(WORK_FIELDS)
        marks = ", ".join("?" for _ in WORK_FIELDS)
        self._write(
            "INSERT INTO phase_samples (run_id, phase, module, cluster, scope, "
            "parent_scope, started, wall_ms, outcome, source, "
            f"{cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {marks})",
            (run_id, sample.key.phase, sample.key.module, sample.key.cluster,
             sample.scope, sample.parent_scope, sample.started, sample.wall_ms,
             sample.outcome, source, *sample.work.as_dict().values()))

    def add_sweep(self, row: SweepRow, *, source: str = "live") -> None:
        self._write(
            "INSERT INTO sweeps (run_id, phase, cluster, iter, ts, at_ms, llm_calls, "
            "pass_count, fail_count, oracle_static, frontier, stall, improved, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row.run_id, row.phase, row.cluster, row.iter, row.ts, row.at_ms,
             row.llm_calls, row.pass_count, row.fail_count, row.oracle_static,
             row.frontier, row.stall, int(row.improved), source))

    def has_sweeps(self, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM sweeps WHERE run_id = ? LIMIT 1",
                                     (run_id,)).fetchone()
        return row is not None

    def has_run(self, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM runs WHERE run_id = ?",
                                     (run_id,)).fetchone()
        return row is not None

    # ── reads ───────────────────────────────────────────────────────────────

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def runs(self, crate: str | None = None) -> list[StoredRun]:
        if crate is None:
            rows = self._query("SELECT * FROM runs ORDER BY started")
        else:
            rows = self._query("SELECT * FROM runs WHERE crate = ? ORDER BY started",
                               (crate,))
        return [_stored_run(r) for r in rows]

    def run(self, run_id: str) -> StoredRun | None:
        rows = self._query("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return _stored_run(rows[0]) if rows else None

    def latest_run(self, crate: str | None = None) -> StoredRun | None:
        """The most recently started LIVE run (an imported row is history)."""
        sql = "SELECT * FROM runs WHERE source = 'live'"
        params: tuple[str, ...] = ()
        if crate is not None:
            sql += " AND crate = ?"
            params = (crate,)
        rows = self._query(sql + " ORDER BY started DESC LIMIT 1", params)
        return _stored_run(rows[0]) if rows else None

    def samples(self, run_ids: Iterable[str] | None = None) -> list[SampleRow]:
        if run_ids is None:
            rows = self._query("SELECT * FROM phase_samples ORDER BY id")
        else:
            ids = list(run_ids)
            if not ids:
                return []
            marks = ", ".join("?" for _ in ids)
            rows = self._query(
                f"SELECT * FROM phase_samples WHERE run_id IN ({marks}) ORDER BY id",
                ids)
        return [SampleRow(run_id=r["run_id"],
                          key=PhaseKey(r["phase"], r["module"] or "", r["cluster"] or ""),
                          outcome=r["outcome"], wall_ms=int(r["wall_ms"] or 0),
                          work=PhaseWork.from_mapping(dict(r)))
                for r in rows]


    def runs_with_sweeps(self, crate: str | None = None, *,
                         limit: int = 6) -> list[StoredRun]:
        """The `limit` most recently started runs that recorded a sweep,
        newest first. A store written before the table existed has none."""
        if not self._has_table("sweeps"):
            return []
        sql = ("SELECT * FROM runs WHERE run_id IN (SELECT DISTINCT run_id FROM sweeps)"
               + (" AND crate = ?" if crate is not None else "")
               + " ORDER BY started DESC LIMIT ?")
        params: tuple[Any, ...] = (crate, limit) if crate is not None else (limit,)
        return [_stored_run(r) for r in self._query(sql, params)]

    def sweeps(self, run_ids: Iterable[str]) -> list[SweepRow]:
        ids = list(run_ids)
        if not ids or not self._has_table("sweeps"):
            return []
        marks = ", ".join("?" for _ in ids)
        rows = self._query(
            f"SELECT * FROM sweeps WHERE run_id IN ({marks}) ORDER BY id", ids)
        return [SweepRow(run_id=r["run_id"], phase=r["phase"], cluster=r["cluster"],
                         iter=int(r["iter"]), ts=r["ts"], at_ms=int(r["at_ms"]),
                         llm_calls=int(r["llm_calls"]), pass_count=int(r["pass_count"]),
                         fail_count=int(r["fail_count"]),
                         oracle_static=int(r["oracle_static"]),
                         frontier=int(r["frontier"]), stall=int(r["stall"]),
                         improved=bool(r["improved"]))
                for r in rows]

    def entry_phases(self, run_ids: Iterable[str]) -> dict[str, str]:
        """run_id -> the first top-level phase the run actually ran. No
        skipped phase is stored, so the earliest top-level sample is where
        the run entered: a from-scratch restart at augmentation, a resume at
        the phase it resumed. A run with no closed top-level phase is absent."""
        ids = list(run_ids)
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = self._query(
            f"SELECT run_id, phase FROM phase_samples WHERE run_id IN ({marks}) "
            "AND parent_scope IS NULL ORDER BY started, id", ids)
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(r["run_id"], r["phase"])
        return out

    def _has_table(self, name: str) -> bool:
        return bool(self._query(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)))


def _stored_run(r: sqlite3.Row) -> StoredRun:
    d = dict(r)
    config: dict[str, Any] = {}
    if d.get("config_json"):
        try:
            config = dict(json.loads(d["config_json"]))
        except (ValueError, TypeError):
            config = {}
    return StoredRun(
        record=RunRecord(run_id=d["run_id"], crate=d["crate"], code_key=d["code_key"],
                         config_fp=d["config_fp"], origin=d["origin"],
                         started=d["started"]),
        tree_stamp=d["tree_stamp"], commit=d["commit_sha"], config=config,
        ended=d["ended"], outcome=d["outcome"], wall_ms=d["wall_ms"],
        work=PhaseWork.from_mapping(d), source=d["source"], note=d["note"])
