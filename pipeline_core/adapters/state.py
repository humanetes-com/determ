"""pipeline_state.md store — phase markers, atomic writes, locking.

The state file is the single source of truth for which phases are complete
(`phase_valid`) and where the run last stood (`current:`). Bodies moved
verbatim from pipeline.py; the only change is the line parser, rewritten
regex-free (project rule) with semantics identical to the old
``\\[([^\\]]+)\\]\\s+(\\S+)\\s+hash=(\\S+)\\s*(.*)`` match.

File format (one phase per line):
  [phase_key]  status  hash=<16-char>  detail text
status: done | in_progress | pending; hash: 16-char SHA-256 prefix of the
phase's source inputs, or "-".

Self-contained (stdlib + the log facade): no deps record needed.
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline_core.log import log

PIPELINE_STATE_NAME = "pipeline_state.md"


def state_path(project_dir: Path) -> Path:
    return project_dir / PIPELINE_STATE_NAME


def _parse_phase_line(line: str) -> tuple[str, str, str, str] | None:
    """Parse `[key]  status  hash=H  detail…` → (key, status, hash, detail).

    Regex-free equivalent of the old pattern: key is everything up to the
    first `]` (must be non-empty), status the next whitespace-delimited
    token, then a `hash=`-prefixed token (non-empty value), then the rest
    of the line as detail. Returns None for anything else.
    """
    s = line.strip()
    if not s.startswith("["):
        return None
    end = s.find("]")
    if end <= 1:  # no `]`, or empty key `[]`
        return None
    key = s[1:end]
    rest = s[end + 1:]
    if not rest or not rest[0].isspace():  # `]status` — old pattern required \s+
        return None
    fields = rest.split(maxsplit=2)
    if len(fields) < 2 or not fields[1].startswith("hash="):
        return None
    hash_ = fields[1][len("hash="):]
    if not hash_:
        return None
    detail = fields[2].strip() if len(fields) == 3 else ""
    return key, fields[0], hash_, detail


def read_state(project_dir: Path) -> dict[str, Any]:
    """Return {'phases': {key: {status, hash, detail}}, 'current': str}."""
    state: dict[str, Any] = {"phases": {}, "current": ""}
    p = state_path(project_dir)
    if not p.exists():
        return state
    for line in p.read_text().splitlines():
        parsed = _parse_phase_line(line)
        if parsed:
            key, status, hash_, detail = parsed
            state["phases"][key] = {
                "status": status,
                "hash":   hash_,
                "detail": detail,
            }
        if line.startswith("current:"):
            state["current"] = line[len("current:"):].strip()
    return state


def write_state(project_dir: Path, state: dict[str, Any]) -> None:
    """Atomic write: render to a sibling tempfile, fsync, then os.replace.
    Guarantees readers never see a partially-written state file even if
    the process is killed mid-write or another pipeline races us.
    """
    lines = [
        f"# pipeline_state — {project_dir.name}",
        f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for key, v in state["phases"].items():
        status = v.get("status", "pending")
        h      = v.get("hash", "-")
        detail = v.get("detail", "")
        lines.append(f"[{key}]  {status:<12}  hash={h}  {detail}")
    lines.append("")
    lines.append(f"current: {state.get('current', '')}")
    body = "\n".join(lines) + "\n"
    target = state_path(project_dir)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        with suppress(OSError):
            os.fsync(f.fileno())
    os.replace(tmp, target)


def mark_phase(project_dir: Path, phase_key: str, status: str,
               detail: str = "", source_hash: str | None = None) -> None:
    """Update a single phase entry and rewrite the state file.

    Holds an fcntl advisory lock on `{project_dir}/.pipeline_state.lock`
    around the read-modify-write so concurrent pipeline processes can't
    clobber each other's updates. The atomic os.replace in `write_state`
    is the second line of defence.
    """
    lock_path = project_dir / ".pipeline_state.lock"
    try:
        lock_path.touch(exist_ok=True)
        import fcntl as _fcntl
        with open(lock_path, "w") as _lf:
            _fcntl.flock(_lf.fileno(), _fcntl.LOCK_EX)
            state = read_state(project_dir)
            state["phases"][phase_key] = {
                "status": status,
                "hash":   source_hash or "-",
                "detail": detail,
            }
            write_state(project_dir, state)
    except Exception:
        # Lock unavailable (e.g. /tmp on read-only FS, exotic env). Fall
        # back to unlocked write — preserves legacy behaviour and the
        # atomic os.replace keeps the file consistent even without the
        # cross-process lock.
        state = read_state(project_dir)
        state["phases"][phase_key] = {
            "status": status,
            "hash":   source_hash or "-",
            "detail": detail,
        }
        write_state(project_dir, state)


def set_current(project_dir: Path, current: str) -> None:
    """Update the 'current:' line without changing any phase entry."""
    state = read_state(project_dir)
    state["current"] = current
    write_state(project_dir, state)


def phase_valid(project_dir: Path, phase_key: str, current_hash: str | None = None) -> bool:
    """Return True if phase is marked done in pipeline_state.md.

    pipeline_state.md is the single source of truth for which phases are
    complete. We do NOT compare against an input hash — test files can change
    legitimately (task0c augments them, the user edits them) without
    invalidating the structural artifacts task0a/0b produce. `current_hash` is
    accepted as a backward-compat shim and ignored.
    """
    state = read_state(project_dir)
    p = state["phases"].get(phase_key, {})
    return bool(p.get("status") == "done")


def log_state_summary(project_dir: Path, current_hash: str) -> None:
    """Log the state file at startup so the user sees what's valid/stale."""
    state = read_state(project_dir)
    if not state["phases"]:
        log("  Pipeline state: no state file found — fresh run.")
        return
    log("  Pipeline state:")
    for key, v in state["phases"].items():
        h      = v.get("hash", "-")
        status = v.get("status", "pending")
        detail = v.get("detail", "")
        if status == "done":
            valid = "valid  " if h == current_hash else "STALE  "
            symbol = "✓" if h == current_hash else "✗"
        elif status == "in_progress":
            valid = "resumed"
            symbol = "~"
        else:
            valid = "pending"
            symbol = " "
        log(f"    [{symbol}] {key:<22}  {valid}  {detail}")
    if state.get("current"):
        log(f"    last position: {state['current']}")
