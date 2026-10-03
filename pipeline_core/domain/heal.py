"""Heal-hierarchy domain types + surgical doc-comment excision (pure).

When a fill-tree file stops parsing, the recovery must NOT default to copying the
baked skeleton over it — that throws away every accumulated fill (the incident:
a 22-fill tree collapsed to 14 because 6 corrupt lines triggered a whole-file
skeleton restore). The ordered restore points below try the least-destructive
option first; `doccomment_intrusions` is the surgical one — it removes ONLY the
code lines that a splice bug wrote INTO a `///` ```code``` example, keeping every
real fill. parse-check (in the application layer) is the final arbiter after each.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class RestorePoint(Enum):
    """Ordered least-destructive-first recovery options for an unparseable file."""

    SURGICAL_EXCISION = auto()  # delete only code lines intruding a doc-comment
    BEST_TREE_COPY = auto()     # this cluster's best ratcheted snapshot of the file
    GIT_ANCESTOR = auto()       # newest workspace-git version that parses
    BAKED_SKELETON = auto()     # last resort — loses every fill in the file
    NONE = auto()               # nothing worked / no candidate available


@dataclass(frozen=True, slots=True)
class HealOutcome:
    """What a heal did: which restore point committed, and the fill loss it
    incurred (stub count before→after; a jump means fills were discarded)."""

    point: RestorePoint
    stubs_before: int = 0
    stubs_after: int = 0
    excised_lines: int = 0

    @property
    def fills_lost(self) -> int:
        return max(0, self.stubs_after - self.stubs_before)


def count_unimplemented(text: str) -> int:
    """Number of `unimplemented!()` stub bodies — the fill-progress proxy (a
    healed file that gains stubs lost that many fills)."""
    return text.count("unimplemented!()")


def _is_doc(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("///") or s.startswith("//!")


def _doc_content(line: str) -> str:
    return line.lstrip()[3:].strip()


def doccomment_intrusions(text: str) -> list[int]:
    """0-based indices of lines that are CODE intruding an OPEN doc-comment code
    fence — the exact corruption shape from the incident (fn bodies spliced into
    a `/// ```` example). A run of non-doc lines counts ONLY when the doc block
    above it has an unclosed ```` ``` ```` fence AND a doc line resumes below —
    so legitimate code between two separately-documented items (no open fence) is
    never touched. The application layer still parse-checks the excised result."""
    lines = text.splitlines()
    n = len(lines)
    out: list[int] = []
    fence_open = False
    i = 0
    while i < n:
        if _is_doc(lines[i]):
            if _doc_content(lines[i]).startswith("```"):
                fence_open = not fence_open
            i += 1
            continue
        if fence_open:
            j = i
            run: list[int] = []
            while j < n and not _is_doc(lines[j]):
                run.append(j)
                j += 1
            if j < n:               # flanked below by a doc line → intrusion
                out.extend(run)
                i = j
                continue
            break                   # fence never closed → not a bounded intrusion
        i += 1
    return out


def apply_excision(text: str) -> tuple[str, int]:
    """Remove doc-comment-intruding code lines; return (new_text, n_removed).
    Trailing newline preserved when the original had one."""
    drop = set(doccomment_intrusions(text))
    if not drop:
        return text, 0
    lines = text.splitlines()
    kept = [ln for k, ln in enumerate(lines) if k not in drop]
    new_text = "\n".join(kept)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, len(drop)
