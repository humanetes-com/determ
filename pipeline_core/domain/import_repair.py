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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.rust_use_items import bound_names, names_bound

#: rustc codes for "this name is defined more than once". A mechanical `use`
#: that introduces one of them made the file worse than the check it was
#: repairing, whatever else the re-check says.
_DUPLICATE_CODES = frozenset({"E0252", "E0255", "E0428"})


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


def _duplicate_binding(code: str, message: str) -> bool:
    if code in _DUPLICATE_CODES:
        return True
    return "defined multiple times" in message


def repair_keeps(
    before: Sequence[tuple[str, str]], after: Sequence[tuple[str, str]],
) -> bool:
    """Whether the re-check earned the inserted `use`.

    `before` and `after` are `(code, message)` of the error diagnostics.
    The write stays when the set changed and no new duplicate-definition
    diagnostic appeared. An unchanged set means the suggestion did nothing.
    A new "defined multiple times" means the suggestion bound a name the
    file already had, and the write is taken back."""
    def dups(rows: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
        return {row for row in rows if _duplicate_binding(row[0], row[1])}
    if dups(after) - dups(before):
        return False
    return sorted(before) != sorted(after)


def apply_import_fixes(source: str, fixes: Iterable[ImportFix]) -> str | None:
    """Apply `fixes` (all for the SAME file as `source`) by byte span.

    Deduplicates identical replacements, skips ones already present verbatim
    in the source, and skips a `use` that would bind a name the file already
    binds (another path, a brace member, an alias). A span that overwrites
    an existing `use` frees the names that use bound, so replacing it is
    not a second definition. Applies right-to-left so earlier spans stay
    valid. Returns the new source, or None when nothing was applicable or
    a span falls outside the source."""
    data = source.encode("utf-8")
    seen: set[str] = set()
    bound = set(bound_names(source))
    todo: list[ImportFix] = []
    for f in sorted(fixes, key=lambda f: f.byte_start, reverse=True):
        key = f.replacement.strip()
        if not key or key in seen or key in source:
            continue
        if not (0 <= f.byte_start <= f.byte_end <= len(data)):
            return None
        try:
            replaced = data[f.byte_start:f.byte_end].decode("utf-8")
        except UnicodeDecodeError:
            return None
        # A span that overwrites a `use` frees the names that use bound.
        # An insertion (an empty span) frees nothing, so a second `use` of
        # a name already bound is refused.
        freed = names_bound(replaced)
        added = names_bound(key)
        if added and (added - freed) & bound:
            continue
        seen.add(key)
        bound.update(added)
        todo.append(f)
    if not todo:
        return None
    for f in todo:
        data = data[:f.byte_start] + f.replacement.encode("utf-8") + data[f.byte_end:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
