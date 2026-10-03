"""Determinism gate for outcome-behaviours — pure domain.

An outcome-behaviour compares a CAPTURED observable to a frozen golden. Captured
output that varies run-to-run (pointer addresses, HashMap order, backtrace
frames, panic ``file:line`` prefixes, timings) would fire a FALSE mismatch, so a
candidate outcome-behaviour is kept only if two captures against the REAL lib are
identical AFTER normalization. This module owns the normalization + the two-run
decision; the adapter does the actual running.

Normalization is line-structured, never regex (Hard Rule): a panic line keeps
only its PAYLOAD (the message), dropping the ``panicked at 'msg', file:line:col``
location that shifts with the reconstruction.
"""
from __future__ import annotations

_PANIC_PREFIX = "panicked at "


def strip_panic_location(text: str) -> str:
    """Reduce each ``thread '…' panicked at 'MSG', src/lib.rs:12:5`` line to its
    payload ``MSG``. A panic message embeds a file:line that moves with the
    reconstruction — comparing it would false-mismatch. Non-panic lines pass
    through unchanged."""
    out: list[str] = []
    for line in text.splitlines():
        idx = line.find(_PANIC_PREFIX)
        if idx == -1:
            out.append(line)
            continue
        rest = line[idx + len(_PANIC_PREFIX):]
        # Rust ≥1.65: `panicked at src/lib.rs:12:5:` then the message on the NEXT
        # line — nothing to salvage on THIS line, drop the location entirely.
        if rest.startswith(("'", '"')):
            quote = rest[0]
            end = rest.find(quote, 1)
            payload = rest[1:end] if end != -1 else rest
            out.append(payload)
        # else: the location-only form — drop the whole line.
    return "\n".join(out)


def normalize_observable(text: str) -> str:
    """Canonicalize a captured observable for comparison: strip panic locations
    and trailing whitespace per line. Structured observables (Display/Debug
    strings) are already deterministic and pass through unchanged."""
    stripped = strip_panic_location(text)
    return "\n".join(ln.rstrip() for ln in stripped.splitlines()).strip()


def is_deterministic(capture_a: str, capture_b: str) -> bool:
    """True iff two real-lib captures agree after normalization — the empirical
    filter that keeps a nondeterministic candidate OUT of the oracle."""
    return normalize_observable(capture_a) == normalize_observable(capture_b)
