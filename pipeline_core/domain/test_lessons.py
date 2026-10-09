"""Lessons task 4 writes per red test, for the next t2a to read.

WHY. The pipeline is a loop: t2a fills, t3 judges every cluster, t4 analyses,
and while the module is red t2a is entered again. t4 used to see the test
output, `genesis.md` and a failure log of pass counts, never what t2a tried
for a red test, and it wrote `genesis.md`, which no t2a fill route reads. So
the loop could carry nothing from one iteration's writer to the next one's.
A lesson closes both ends: it is written from a test's red-ledger entry (what
was asked, what landed, what it printed), it names the functions it is about,
and it reaches every prompt that writes them. A new lesson re-owes exactly
those functions for that test (`candidate_tiers.due_reading`, `lessons=`).

Admission is what keeps a lesson evidence and not noise: every function it
names must be on the test's slice or failure site, and a lesson whose text a
stored lesson for the same (function, test) already carries is not new.

Nothing here names a crate, file, test, trait or symbol."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["Lesson", "LessonStore", "admit_lessons", "parse_lessons"]

_BLOCK = re.compile(r'<LESSON\s+fn="([^"]+)"\s*>(.*?)</LESSON>', re.S)
_MAX_TEXT = 1200


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class Lesson:
    unit: str
    test: str
    text: str
    cluster: str = ""
    iteration: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(_norm(self.text).encode("utf-8")).hexdigest()[:12]


def parse_lessons(reply: str, test: str, *, cluster: str = "",
                  iteration: str = "") -> list[Lesson]:
    """Every `<LESSON fn="file::fn">text</LESSON>` block of a reply."""
    out: list[Lesson] = []
    for fn, text in _BLOCK.findall(reply or ""):
        body = text.strip()[:_MAX_TEXT]
        if fn.strip() and body:
            out.append(Lesson(fn.strip(), test, body, cluster, iteration))
    return out


class LessonStore:
    """(function, test) -> the lessons admitted for it, oldest first."""

    def __init__(self, raw: Mapping[str, Any] | None = None) -> None:
        self.rows: dict[str, list[dict[str, str]]] = {
            str(k): [dict(x) for x in v]
            for k, v in dict(raw or {}).items() if isinstance(v, list)}

    @staticmethod
    def _key(unit: str, test: str) -> str:
        return f"{unit}\t{test}"

    def has(self, lesson: Lesson) -> bool:
        return any(r.get("digest") == lesson.digest
                   for r in self.rows.get(self._key(lesson.unit, lesson.test), []))

    def add(self, lesson: Lesson) -> bool:
        if self.has(lesson):
            return False
        self.rows.setdefault(self._key(lesson.unit, lesson.test), []).append(
            {"digest": lesson.digest, "text": lesson.text,
             "cluster": lesson.cluster, "iteration": lesson.iteration})
        return True

    def count(self) -> int:
        return sum(len(v) for v in self.rows.values())

    def digests(self) -> dict[tuple[str, str], str]:
        """(function, test) -> one digest over every lesson stored for it:
        the channel the ask fingerprint folds in."""
        out: dict[tuple[str, str], str] = {}
        for k, v in self.rows.items():
            unit, _, test = k.partition("\t")
            h = hashlib.sha256("\0".join(r.get("digest", "") for r in v)
                               .encode("utf-8")).hexdigest()[:12]
            out[(unit, test)] = "L" + h
        return out

    def render_for(self, unit: str, tests: Iterable[str], *,
                   max_chars: int = 1500) -> str:
        """The lessons stored for `unit` and any of `tests`, newest last."""
        lines: list[str] = []
        for t in sorted(set(tests)):
            for r in self.rows.get(self._key(unit, t), []):
                lines.append(f"- for {t}: {r.get('text', '')}")
        text = "\n".join(lines)
        return text[-max_chars:] if len(text) > max_chars else text

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {k: list(v) for k, v in sorted(self.rows.items())}


def admit_lessons(lessons: Iterable[Lesson], *, allowed: Collection[str],
                  store: LessonStore) -> tuple[list[Lesson], list[tuple[Lesson, str]]]:
    """(admitted, refused with why). A lesson naming a function off the
    test's slice or failure site (`allowed`) is refused; so is one whose text
    a stored lesson for the same (function, test) already carries."""
    admitted: list[Lesson] = []
    refused: list[tuple[Lesson, str]] = []
    allowed_set = set(allowed)
    for le in lessons:
        if le.unit not in allowed_set:
            refused.append((le, "names a function off the test's slice"))
        elif store.has(le):
            refused.append((le, "already stored for this function and test"))
        else:
            admitted.append(le)
    return admitted, refused
