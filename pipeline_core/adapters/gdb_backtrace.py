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
#
# Besides the frames it records the fault address (`$_siginfo`, present for
# SIGSEGV/SIGBUS) and, per frame, every pointer its ARGUMENTS hold as
# `[value, pointee size]` — a pointer argument, or a pointer field of a struct
# argument (`NonNull<T>`, a fat pointer's data half), two levels deep. The
# domain traces which frame produced the faulting address from these
# (`crash_site.traced_origin_index`); the dump only measures.
_GDB_DUMP_SCRIPT = """\
import json
import gdb
res = {"signal": None, "frames": [], "fault_addr": None}

def _ptrs(val, depth, out):
    if depth > 2 or len(out) >= 16:
        return
    try:
        t = val.type.strip_typedefs()
        code = t.code
        if code == gdb.TYPE_CODE_PTR:
            try:
                size = int(t.target().strip_typedefs().sizeof)
            except (gdb.error, ValueError):
                size = 0
            out.append([int(val), size])
        elif code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            for f in t.fields():
                if f.is_base_class or not f.name:
                    continue
                _ptrs(val[f.name], depth + 1, out)
    except (gdb.error, gdb.MemoryError, ValueError, TypeError):
        return

def _symbol(fr):
    # gdb names a trait-impl method by its namespace (`gd::{impl#0}::drop`);
    # the linkage name keeps the trait (`<gd::Holder as
    # core::ops::drop::Drop>::drop`), the form the sanitizer prints.
    try:
        f = fr.function()
        link = getattr(f, "linkage_name", None) if f is not None else None
        if not link:
            return ""
        out = gdb.execute("demangle -l rust -- " + link, to_string=True).strip()
        return out if out and out != link else ""
    except (gdb.error, RuntimeError):
        return ""

def _arg_ptrs(fr):
    out = []
    try:
        block = fr.block()
    except (gdb.error, RuntimeError):
        return out
    while block is not None:
        for sym in block:
            if not sym.is_argument:
                continue
            try:
                _ptrs(sym.value(fr), 0, out)
            except (gdb.error, gdb.MemoryError, ValueError, TypeError):
                pass
        if block.function is not None:
            break
        block = block.superblock
    return out

try:
    info = gdb.execute("info program", to_string=True)
    for _ln in info.splitlines():
        if "signal" in _ln:
            _part = _ln.split("signal", 1)[1].strip()
            res["signal"] = _part.split(",", 1)[0].strip().rstrip(".")
            break
    try:
        _si = gdb.parse_and_eval("$_siginfo")
        if str(res["signal"] or "") not in ("SIGSEGV", "SIGBUS"):
            raise ValueError("no fault address for this signal")
        res["fault_addr"] = int(_si["_sifields"]["_sigfault"]["si_addr"])
    except (gdb.error, ValueError, TypeError):
        pass
    fr = gdb.newest_frame()
    n = 0
    while fr is not None and n < 40:
        try:
            sal = fr.find_sal()
            symtab = sal.symtab if sal is not None else None
            res["frames"].append({
                "func": fr.name() or "",
                "symbol": _symbol(fr),
                "file": symtab.filename if symtab is not None else "",
                "line": sal.line if sal is not None else 0,
                "ptr_args": _arg_ptrs(fr),
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
) -> tuple[str, list[dict[str, object]], int | None] | None:
    """Run ``exe_path`` under gdb and return ``(signal, frames, fault
    address)`` from the fatal stop, or None when it exits cleanly / gdb
    missing / timeout. The address is None for a signal that carries none.
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
        addr = payload.get("fault_addr")
        return (str(signal), list(frames),
                int(addr) if isinstance(addr, int) else None)
    return None
