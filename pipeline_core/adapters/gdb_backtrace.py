"""gdb-backed crash localization adapter.

Re-runs a crashed test binary under `gdb -batch` and extracts the faulting
backtrace as structured records via gdb's embedded Python (the script prints
one `CRASH_JSON:{…}` line; we parse that JSON — no scraping of gdb's human
CLI output). Serial harness + optional exact-test filter keep the run short
and the crash deterministic.

Returns None when the binary exits without a fatal signal (the crash was
load-order dependent), when gdb is unavailable, or on timeout — callers
treat None as "no localization", never as proof of health.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pipeline_core.adapters.process import run_bounded

# Runs INSIDE gdb's embedded Python (which provides `import gdb`); written to
# a temp file and `source`d. Emits exactly one CRASH_JSON line.
_GDB_DUMP_SCRIPT = """\
import json
import gdb
res = {"signal": None, "frames": []}
try:
    info = gdb.execute("info program", to_string=True)
    for _ln in info.splitlines():
        if "signal" in _ln:
            _part = _ln.split("signal", 1)[1].strip()
            res["signal"] = _part.split(",", 1)[0].strip().rstrip(".")
            break
    fr = gdb.newest_frame()
    n = 0
    while fr is not None and n < 40:
        try:
            sal = fr.find_sal()
            symtab = sal.symtab if sal is not None else None
            res["frames"].append({
                "func": fr.name() or "",
                "file": symtab.filename if symtab is not None else "",
                "line": sal.line if sal is not None else 0,
            })
        except gdb.error:
            res["frames"].append({"func": fr.name() or "", "file": "", "line": 0})
        fr = fr.older()
        n += 1
except gdb.error:
    pass
print("CRASH_JSON:" + json.dumps(res))
"""

_MARKER = "CRASH_JSON:"


def localize_crashed_binary(
    exe_path: Path,
    cwd: Path,
    env: dict[str, str],
    *,
    test_filter: str | None = None,
    timeout: int = 90,
) -> tuple[str, list[dict[str, object]]] | None:
    """Run ``exe_path`` under gdb and return ``(signal, frames)`` from the
    fatal stop, or None when it exits cleanly / gdb missing / timeout.
    ``test_filter`` narrows the run to one libtest test (``--exact``)."""
    if not exe_path.exists():
        return None
    harness: list[str] = ["--test-threads=1"]
    if test_filter:
        harness += [test_filter, "--exact"]
    with tempfile.NamedTemporaryFile(
            "w", suffix=".py", prefix="gdb_dump_", delete=False) as tf:
        tf.write(_GDB_DUMP_SCRIPT)
        script = tf.name
    cmd = ["gdb", "-batch", "-nx",
           "-ex", "set pagination off",
           "-ex", "set confirm off",
           "-ex", "run",
           "-ex", f"source {script}",
           "--args", str(exe_path), *harness]
    try:
        # env passed byte-identical to the sweep's: stack-layout-sensitive
        # crashes (dangling stack pointers) appear/vanish with env size, so
        # ANY added/removed var can make the localizer miss a real crash.
        # gdb is the child; the inferior (the reconstructed binary) is the
        # grandchild — timeout kills the group.
        ran = run_bounded(cmd, cwd=cwd, env=env, timeout=float(timeout))
    finally:
        Path(script).unlink(missing_ok=True)
    if ran.failure is not None:
        return None
    for line in ran.stdout.splitlines():
        # The marker can land mid-line: the dying test's `test NAME ... `
        # prefix has no trailing newline, so the script's print appends to it.
        if _MARKER not in line:
            continue
        try:
            payload = json.loads(line.split(_MARKER, 1)[1])
        except json.JSONDecodeError:
            return None
        signal = payload.get("signal")
        frames = payload.get("frames") or []
        if not signal:
            return None  # inferior exited normally — nothing to localize
        return str(signal), list(frames)
    return None
