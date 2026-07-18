"""Deterministic missing-import repair from rustc's own suggestions — pure domain.

A body-only splice cannot add file-level `use` items, so a model body that
names a real in-crate item unqualified (E0425/E0433 `cannot find ...`) fails
compile even though rustc itself emits the exact fix as a structured
suggestion ("consider importing ..." with a replacement span). Applying that
suggestion mechanically — rustc's span bytes, rustc's replacement text — is
deterministic and uses zero original-source knowledge, so it is the correct
systemic repair: the model keeps authoring bodies, the compiler supplies the
import plumbing bodies cannot express.

Only unambiguous suggestions are applied: a child diagnostic whose message
starts with "consider importing" and carries exactly ONE replacement span.
Multi-candidate suggestions ("one of these items", several spans) are skipped
— picking a candidate would be a guess, not a repair.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ImportFix:
    """One applicable import insertion: rustc's replacement span for a file."""
    file: str            # span file_name, crate-relative (e.g. "src/error.rs")
    byte_start: int
    byte_end: int
    replacement: str     # e.g. "use crate::wrapper::MessageError;\n"


def import_fixes_from_message(msg: dict[str, Any]) -> tuple[ImportFix, ...]:
    """Extract unambiguous import suggestions from ONE compiler-message
    `message` dict (rustc JSON diagnostic). See module docstring for the
    acceptance rule."""
    out: list[ImportFix] = []
    for child in msg.get("children") or []:
        if not isinstance(child, dict):
            continue
        cmsg = str(child.get("message") or "")
        if not cmsg.startswith("consider importing"):
            continue
        spans = [s for s in (child.get("spans") or [])
                 if isinstance(s, dict) and s.get("suggested_replacement")]
        if len(spans) != 1:
            continue  # zero or ambiguous candidates — never guess
        s = spans[0]
        repl = str(s.get("suggested_replacement") or "")
        if not repl.lstrip().startswith("use "):
            continue
        raw_start, raw_end = s.get("byte_start"), s.get("byte_end")
        if not isinstance(raw_start, int) or not isinstance(raw_end, int):
            continue
        start, end = raw_start, raw_end
        out.append(ImportFix(
            file=str(s.get("file_name") or ""),
            byte_start=start, byte_end=end, replacement=repl))
    return tuple(out)


def import_fixes_from_json(raw: str) -> tuple[ImportFix, ...]:
    """All unambiguous import suggestions in cargo `--message-format=json`
    output (error-level compiler-messages only)."""
    out: list[ImportFix] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("reason") != "compiler-message":
            continue
        msg = obj.get("message") or {}
        if msg.get("level") != "error":
            continue
        out.extend(import_fixes_from_message(msg))
    return tuple(out)


def apply_import_fixes(source: str, fixes: Iterable[ImportFix]) -> str | None:
    """Apply `fixes` (all for the SAME file as `source`) by byte span.

    Deduplicates identical replacements, skips ones already present verbatim
    in the source, applies right-to-left so earlier spans stay valid. Returns
    the new source, or None when nothing was applicable (all duplicates) or a
    span falls outside the source."""
    data = source.encode("utf-8")
    seen: set[str] = set()
    todo: list[ImportFix] = []
    for f in sorted(fixes, key=lambda f: f.byte_start, reverse=True):
        key = f.replacement.strip()
        if not key or key in seen or key in source:
            continue
        if not (0 <= f.byte_start <= f.byte_end <= len(data)):
            return None
        seen.add(key)
        todo.append(f)
    if not todo:
        return None
    for f in todo:
        data = data[:f.byte_start] + f.replacement.encode("utf-8") + data[f.byte_end:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
