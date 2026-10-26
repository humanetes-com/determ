"""Which file cargo built for a target, read off `--message-format=json`.

Task 0d's exercise runner gave grcov `--binary-path target/debug`, and grcov
loads every binary under that path for every profraw. On the workspace of run
`42057-641e589c` (2026-10-25) that directory held 438 executables in 7.3 GB:
one profraw took 43.8 s there and 0.4 s with the one binary that wrote it,
against a live median of 37.7 s per spec, about 250 of t0d's 520 minutes.
Cargo already names the file: every `compiler-artifact` message carries the
target's kinds and name and, for a runnable target, its `executable`. That
message is emitted for a target that was fresh as well as one just built.
"""
from __future__ import annotations

import json

COMPILER_ARTIFACT = "compiler-artifact"


def executables_from_messages(stdout: str) -> dict[tuple[str, str], str]:
    """`(kind, target name) → executable path` from cargo's JSON messages.

    One key per kind the target lists (`test`, `example`, `bin`, ...). A
    line that is not a JSON object is skipped: `cargo run` interleaves the
    program's own stdout with cargo's messages, and that output is no
    message. An artifact with no executable (a library) adds nothing."""
    out: dict[tuple[str, str], str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or msg.get("reason") != COMPILER_ARTIFACT:
            continue
        exe = msg.get("executable")
        target = msg.get("target")
        if not isinstance(exe, str) or not exe or not isinstance(target, dict):
            continue
        name = target.get("name")
        kinds = target.get("kind")
        if not isinstance(name, str) or not isinstance(kinds, list):
            continue
        for kind in kinds:
            if isinstance(kind, str):
                out[(kind, name)] = exe
    return out
