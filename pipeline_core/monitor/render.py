"""Pure ANSI / pipeline-line text helpers for the monitor TUI.

These are the regex-free (R7) presentation-layer primitives the renderer reuses:
visible-width measurement (ANSI escape stripping) and the two structured scrapes
of pipeline-state / call-start log lines. They hold no I/O and no module state, so
they live in :mod:`pipeline_core.monitor` beside the data readers (run_data /
events_data) rather than in the god-file ``monitor.py``.
"""

from __future__ import annotations


def ansi_seq_len(s: str, i: int) -> int:
    r"""Length of the CSI escape sequence at ``s[i]`` (``\033[`` params final-letter),
    or 0 if none (regex-free, R7; mirrors ``\033\[[0-9;]*[a-zA-Z]``)."""
    if not s.startswith("\033[", i):
        return 0
    j = i + 2
    while j < len(s) and (s[j].isdigit() or s[j] == ";"):
        j += 1
    c = s[j] if j < len(s) else ""
    if ("a" <= c <= "z") or ("A" <= c <= "Z"):
        return j + 1 - i
    return 0


def plain(s: str) -> str:
    """``s`` with every ANSI CSI escape removed — i.e. its visible characters."""
    out: list[str] = []
    i = 0
    while i < len(s):
        seq = ansi_seq_len(s, i)
        if seq:
            i += seq
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def state_done_key(line: str) -> str | None:
    r"""Cluster key from a ``[<key>[/...]] done`` pipeline_state line, else None
    (regex-free, R7; mirrors ``\[([^\]/]+)(?:/[^\]]*)?\]\s+done\b``)."""
    if not line.startswith("["):
        return None
    rest = line[1:]
    e = 0
    while e < len(rest) and rest[e] not in "]/":
        e += 1
    if e == 0:
        return None                       # [^\]/]+ requires ≥1 char
    key = rest[:e]
    k = e
    if k < len(rest) and rest[k] == "/":  # optional /[^\]]*
        k += 1
        while k < len(rest) and rest[k] != "]":
            k += 1
    if k >= len(rest) or rest[k] != "]":
        return None
    tail = rest[k + 1:]
    stripped = tail.lstrip()
    if stripped == tail:                  # \s+ requires ≥1 whitespace
        return None
    nxt = stripped[4:5]
    if stripped.startswith("done") and not (nxt.isalnum() or nxt == "_"):  # done\b
        return key
    return None


def call_start_value(content: str) -> str | None:
    r"""The float-ish token from the first well-formed ``[CALL_START: <num>]``
    marker, else None (regex-free, R7; mirrors ``\[CALL_START: ([\d.]+)\]``)."""
    marker = "[CALL_START: "
    start = 0
    while (i := content.find(marker, start)) >= 0:
        j = i + len(marker)
        k = j
        while k < len(content) and (content[k].isdigit() or content[k] == "."):
            k += 1
        if k > j and k < len(content) and content[k] == "]":
            return content[j:k]
        start = i + 1
    return None
