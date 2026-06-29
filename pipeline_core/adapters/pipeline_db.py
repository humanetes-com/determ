"""Per-project SQLite knowledge store (adapter).

A stateless I/O boundary over ``pipeline_knowledge.sqlite``: type-difficulty
signals, compile-error patterns, structured genesis rules and module-status
history accumulated across runs and replayed into TASK 1/2 prompts. Each call
opens and closes its own connection (project_dir varies per call), so this is a
module of functions — the module *is* the adapter; there is no shared state to
encapsulate in a class. All writes swallow exceptions: knowledge bookkeeping
must never break a real run.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

PIPELINE_DB_NAME = "pipeline_knowledge.sqlite"


def _pipeline_db(project_dir: Path) -> sqlite3.Connection:
    """Open (and initialize if new) the per-project knowledge store."""
    db_path = project_dir / PIPELINE_DB_NAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _init_pipeline_db(conn)
    return conn


def _init_pipeline_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS type_signals (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        project  TEXT NOT NULL,
        file     TEXT NOT NULL,
        type_name TEXT NOT NULL,
        difficulty INTEGER NOT NULL DEFAULT 1,
        pattern  TEXT,
        ts       TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS compile_patterns (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        project        TEXT NOT NULL,
        module         TEXT NOT NULL,
        error_category TEXT NOT NULL,
        example        TEXT,
        fix_hint       TEXT,
        iteration      INTEGER,
        ts             TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS genesis_rules (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        project      TEXT NOT NULL,
        module_scope TEXT NOT NULL DEFAULT '*',
        category     TEXT NOT NULL,
        rule_text    TEXT NOT NULL,
        iteration    INTEGER,
        ts           TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS module_status (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        project    TEXT NOT NULL,
        module     TEXT NOT NULL,
        iteration  INTEGER NOT NULL,
        outcome    TEXT NOT NULL,
        pass_count INTEGER,
        fail_count INTEGER,
        ts         TEXT NOT NULL
    );
    """)
    conn.commit()


def _db_project_name(project_dir: Path) -> str:
    return project_dir.name


def type_signals_write(project_dir: Path, file: str, type_name: str,
                       difficulty: int = 1, pattern: str = "") -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO type_signals(project,file,type_name,difficulty,pattern,ts) "
            "VALUES(?,?,?,?,?,?)",
            (_db_project_name(project_dir), file, type_name, difficulty, pattern,
             datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def type_signals_read(project_dir: Path) -> str:
    """Return a formatted summary of types that were difficult to test, for TASK 1."""
    try:
        conn = _pipeline_db(project_dir)
        rows = conn.execute(
            "SELECT type_name, SUM(difficulty) as total, GROUP_CONCAT(pattern, ' | ') "
            "FROM type_signals WHERE project=? "
            "GROUP BY type_name ORDER BY total DESC LIMIT 20",
            (_db_project_name(project_dir),)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Types that were difficult to construct in tests (extract especially precise "
                 "requirements for these):"]
        for type_name, total, patterns in rows:
            line = f"  - {type_name} (difficulty score: {total})"
            if patterns:
                first = patterns.split(' | ')[0]
                if first:
                    line += f" — e.g. {first[:120]}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def compile_patterns_write(project_dir: Path, module: str, error_category: str,
                            example: str = "", fix_hint: str = "",
                            iteration: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO compile_patterns(project,module,error_category,example,fix_hint,iteration,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (_db_project_name(project_dir), module, error_category, example[:500],
             fix_hint[:500], iteration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def compile_patterns_read(project_dir: Path, module: str) -> str:
    """Return known compile pitfalls for this module, for TASK 2 prompt."""
    try:
        conn = _pipeline_db(project_dir)
        rows = conn.execute(
            "SELECT error_category, example, fix_hint FROM compile_patterns "
            "WHERE project=? AND (module=? OR module='*') "
            "ORDER BY id DESC LIMIT 15",
            (_db_project_name(project_dir), module)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Known compile pitfalls for this module (avoid repeating these errors):"]
        seen: set[str] = set()
        for cat, example, fix_hint in rows:
            if cat in seen:
                continue
            seen.add(cat)
            line = f"  - [{cat}]"
            if example:
                line += f" e.g. `{example[:100]}`"
            if fix_hint:
                line += f" → fix: {fix_hint[:100]}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def genesis_rules_write(project_dir: Path, module_scope: str, category: str,
                         rule_text: str, iteration: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO genesis_rules(project,module_scope,category,rule_text,iteration,ts) "
            "VALUES(?,?,?,?,?,?)",
            (_db_project_name(project_dir), module_scope, category, rule_text[:1000],
             iteration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def genesis_rules_read(project_dir: Path, module_scope: str = "*",
                        categories: list[str] | None = None) -> str:
    """Return structured rules relevant to a module and failure categories, for TASK 1/2."""
    try:
        conn = _pipeline_db(project_dir)
        placeholders = "?,?" if module_scope != "*" else "?"
        args: list[str] = [_db_project_name(project_dir)]
        if module_scope != "*":
            args += [module_scope, "*"]
        else:
            args += ["*"]
        if categories:
            cat_placeholders = ",".join("?" * len(categories))
            query = (
                f"SELECT category, rule_text FROM genesis_rules "
                f"WHERE project=? AND module_scope IN ({placeholders}) "
                f"AND category IN ({cat_placeholders}) "
                f"ORDER BY id DESC LIMIT 20"
            )
            args += categories
        else:
            query = (
                f"SELECT category, rule_text FROM genesis_rules "
                f"WHERE project=? AND module_scope IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 20"
            )
        rows = conn.execute(query, args).fetchall()
        conn.close()
        if not rows:
            return ""
        seen: set[str] = set()
        lines = ["Structured rules from previous failures (apply these):"]
        for cat, rule in rows:
            key = cat + rule[:50]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  [{cat}] {rule}")
        return "\n".join(lines)
    except Exception:
        return ""


def genesis_rules_count(project_dir: Path, module_scope: str = "*") -> int:
    """Total structured genesis rules recorded for a module scope (richness signal)."""
    try:
        conn = _pipeline_db(project_dir)
        args: list[str] = [_db_project_name(project_dir)]
        ph = "?" if module_scope == "*" else "?,?"
        args += ["*"] if module_scope == "*" else [module_scope, "*"]
        n = conn.execute(f"SELECT COUNT(*) FROM genesis_rules WHERE project=? "
                         f"AND module_scope IN ({ph})", args).fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def module_status_write(project_dir: Path, module: str, iteration: int,
                         outcome: str, pass_count: int = 0,
                         fail_count: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO module_status(project,module,iteration,outcome,pass_count,fail_count,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (_db_project_name(project_dir), module, iteration, outcome, pass_count,
             fail_count, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
