"""Run-log rotation — keep the ACTIVE log file bounded without discarding the
history. Filesystem adapter relocated from pipeline.py (C1). Fail-safe: any I/O
error is swallowed so log housekeeping never aborts a run.

History is evidence. The previous implementation bounded the file by deleting
its head, so every run silently ate the oldest lines of the runs before it — a
plateau spanning five runs was unreadable because four of them had been
overwritten in place while the pipeline appended. Bounding the file is still
right (a single multi-GB log is unreadable too); throwing the head away is not.
Rotation gives both: `pipeline_anyhow.log` stays small, `…log.1`, `…log.2`, …
hold everything older, oldest at the highest index.
"""
from __future__ import annotations

import os


def _archive_name(path: str, index: int) -> str:
    return f"{path}.{index}"


def rotate(path: str, max_bytes: int) -> None:
    """Roll ``path`` to ``path.1`` (shifting existing archives up) once it
    exceeds ``max_bytes``. No-op while the file is still small enough.

    Nothing is deleted: there is no archive-count cap, because the point is that
    a later reader can reconstruct the FULL run history. Disk is cheaper than a
    plateau nobody can see."""
    try:
        if os.path.getsize(path) <= max_bytes:
            return
        highest = 0
        while os.path.exists(_archive_name(path, highest + 1)):
            highest += 1
        # Shift existing archives up, highest first, so `.1` is free. Descending
        # order matters: ascending would overwrite `.2` with `.1` before `.2`
        # itself had moved.
        for older in range(highest, 0, -1):
            os.replace(_archive_name(path, older), _archive_name(path, older + 1))
        # COPY the body out, then truncate IN PLACE — never rename the active
        # file. The process appending to this log is not the one rotating it:
        # the launcher redirects the pipeline's stdout here (`>> …log`) and that
        # descriptor can never be told to reopen. Renaming would leave it bound
        # to the renamed inode, so every later line would land in the ARCHIVE
        # while the active file stayed empty forever — and, being empty, would
        # never trip the size check again, so the archive would grow unbounded.
        # Truncating keeps the inode; O_APPEND recomputes the offset to EOF on
        # every write, so the writer continues cleanly at the new start.
        # (This is why the launchers MUST use `>>`: a non-append `>` descriptor
        # keeps its old offset and pads the file with NULs — see
        # test_launchers_open_the_rotated_log_in_append_mode.)
        with open(path, "rb") as src, open(_archive_name(path, 1), "wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
        # Residual race, accepted knowingly: a line appended by another thread
        # between the copy and the truncate is lost. The window is one buffered
        # copy of a bounded file; the alternative (rename) loses the whole rest
        # of the run.
        os.truncate(path, 0)
    except Exception:
        pass


def archived_logs(path: str) -> list[str]:
    """Every rotated archive of ``path``, NEWEST first (``.1`` before ``.2``).
    Excludes the active file — a reader wanting full history reads that first,
    then walks these."""
    out: list[str] = []
    index = 1
    while os.path.exists(_archive_name(path, index)):
        out.append(_archive_name(path, index))
        index += 1
    return out
