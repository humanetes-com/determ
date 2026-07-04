"""The model-authored implementation blueprint (july_plan P3) — domain.

Per-fn/per-impl fills each invent their piece of the crate LOCALLY, so
cross-cutting schemes (error representation, dispatch families, macro
strategies) diverge — the starvation miner's `design_conflict` class. The
blueprint restores the top-down half: ONE model-authored design document per
crate, written from tests + skeleton signatures + FR before any fill, then
SLICED into every fill prompt so each local decision sees the global scheme.

Charter-safe: the blueprint's inputs are post-strip-legal, its content is the
model's OWN plan (never "what the upstream does"). Lifecycle = FR's (wiped
together; `blueprint_path` derives the sibling file).

Pure: heading-structured parse (line walk, no regex), identifier-overlap
slicing over the rust_idents tokeniser.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.domain.starvation import has_word


def blueprint_path(functional_req_path: Path) -> Path:
    """`functional_requirements_{crate}.md` → sibling `blueprint_{crate}.md`."""
    name = functional_req_path.name
    stem = name[len("functional_requirements"):] if name.startswith(
        "functional_requirements") else f"_{functional_req_path.stem}.md"
    return functional_req_path.parent / f"blueprint{stem}"


@dataclass(frozen=True, slots=True)
class BlueprintSection:
    heading: str   # text after "## "
    body: str


@dataclass(frozen=True, slots=True)
class Blueprint:
    """The parsed design document: ordered `## `-headed sections. The first
    section (usually Overview) is the always-included anchor."""

    sections: tuple[BlueprintSection, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.sections

    def slice_for(self, identifiers: frozenset[str], *, cap: int) -> str:
        """The prompt slice for one fill target: the anchor section plus the
        sections whose heading or body mentions the most target identifiers
        (whole-word), greedily up to `cap` chars. Deterministic: score desc,
        document order tiebreak."""
        if not self.sections:
            return ""
        idents = {i for i in identifiers if i}
        scored: list[tuple[int, int, BlueprintSection]] = []
        for pos, sec in enumerate(self.sections):
            text = f"{sec.heading}\n{sec.body}"
            score = sum(1 for i in idents if has_word(text, i))
            scored.append((score, pos, sec))
        anchor = self.sections[0]
        chosen: list[tuple[int, BlueprintSection]] = [(0, anchor)]
        used = len(anchor.heading) + len(anchor.body)
        for score, pos, sec in sorted(scored, key=lambda t: (-t[0], t[1])):
            if sec is anchor or score == 0:
                continue
            size = len(sec.heading) + len(sec.body)
            if used + size > cap:
                continue
            chosen.append((pos, sec))
            used += size
        chosen.sort(key=lambda t: t[0])
        return "\n\n".join(f"## {s.heading}\n{s.body.strip()}"
                           for _, s in chosen).strip()


def parse_blueprint(text: str) -> Blueprint:
    """Split on `## ` headings; prose before the first heading becomes an
    `Overview` section so it is never lost."""
    sections: list[BlueprintSection] = []
    heading: str | None = None
    body: list[str] = []

    def _flush() -> None:
        content = "\n".join(body).strip()
        if heading is not None:
            sections.append(BlueprintSection(heading, content))
        elif content:
            sections.append(BlueprintSection("Overview", content))

    for line in (text or "").splitlines():
        if line.startswith("## "):
            _flush()
            heading = line[3:].strip()
            body = []
        elif line.startswith("# ") and heading is None:
            continue   # document title line
        else:
            body.append(line)
    _flush()
    return Blueprint(tuple(sections))


def missing_topics(bp: Blueprint, topics: frozenset[str]) -> tuple[str, ...]:
    """Topics (file stems, family names, macro names) with NO section whose
    heading mentions them — the generation validator's gap list."""
    out = []
    for topic in sorted(topics):
        if not any(has_word(sec.heading, topic) for sec in bp.sections):
            out.append(topic)
    return tuple(out)
