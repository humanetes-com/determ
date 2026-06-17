"""FR "Model Notes" domain — the model's self-authored anti-hallucination notes
section inside ``functional_requirements_{crate}.md`` (CLAUDE.md: "The model
authors its own anti-hallucination notes").

Pure text transforms over the FR markdown: locate the ``## Model Notes`` section,
split it from the body, parse its bullets, and re-attach it when FR is
regenerated (so TASK 1 never wipes the accumulated notes). Stdlib only, **no
regex** — the next-heading boundary is found by a structured char scan
(previously `re.search(r"\\n##\\s")`)."""
from __future__ import annotations

MODEL_NOTES_HEADER = "## Model Notes — Confusions to Avoid"


def _next_top_heading_offset(after_header: str) -> int | None:
    """Offset within ``after_header`` of the newline that begins the next
    top-level ``## `` heading (exactly two hashes followed by whitespace), or
    ``None`` if there is none. Byte-equivalent to the old ``\\n##\\s`` scan."""
    n = len(after_header)
    for i in range(n - 3):
        if (after_header[i] == "\n"
                and after_header[i + 1:i + 3] == "##"
                and after_header[i + 3].isspace()):
            return i
    return None


def slice_model_notes(fr_text: str) -> tuple[str, str]:
    """Split FR text into ``(body_without_notes, notes_section_inclusive)``.

    The notes section starts at the ``## Model Notes`` header and runs to the
    next top-level ``##`` heading or EOF. Empty notes string when absent."""
    if MODEL_NOTES_HEADER not in fr_text:
        return fr_text, ""
    idx = fr_text.index(MODEL_NOTES_HEADER)
    body = fr_text[:idx].rstrip() + "\n"
    rest = fr_text[idx:]
    rel = _next_top_heading_offset(rest[len(MODEL_NOTES_HEADER):])
    if rel is not None:
        end = len(MODEL_NOTES_HEADER) + rel + 1  # keep the '\n'
        notes = rest[:end]
        body += rest[end:]
    else:
        notes = rest
    return body, notes.rstrip() + "\n"


def parse_note_bullets(notes_section: str) -> list[str]:
    """The note bullets in a notes section: the text after ``- ``, with
    continuation lines (indented or following a blank) joined into one entry."""
    if not notes_section.strip():
        return []
    notes: list[str] = []
    cur: list[str] = []
    for ln in notes_section.splitlines():
        if ln.startswith("- "):
            if cur:
                notes.append(" ".join(cur).strip())
                cur = []
            cur.append(ln[2:].strip())
        elif cur and ln.startswith("  "):
            cur.append(ln.strip())
        elif cur and not ln.strip():
            notes.append(" ".join(cur).strip())
            cur = []
    if cur:
        notes.append(" ".join(cur).strip())
    return [n for n in notes if n]


def preserve_model_notes(old_fr_text: str | None, new_fr_text: str) -> str:
    """Re-append ``old_fr_text``'s notes section onto ``new_fr_text`` so a TASK 1
    regeneration never drops the model's accumulated self-corrections."""
    if not old_fr_text or MODEL_NOTES_HEADER not in old_fr_text:
        return new_fr_text
    _, notes_section = slice_model_notes(old_fr_text)
    if not notes_section.strip():
        return new_fr_text
    new_body, _ = slice_model_notes(new_fr_text)  # defensive: drop any notes in new
    return new_body.rstrip() + "\n\n" + notes_section


def render_with_notes(fr_text: str, notes: list[str]) -> str:
    """Rewrite the notes section of ``fr_text`` with ``notes`` as bullets,
    creating a minimal FR body when the input has none."""
    body, _ = slice_model_notes(fr_text)
    if not body.strip():
        body = "# Functional Requirements\n\n"
    section = MODEL_NOTES_HEADER + "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    return body.rstrip() + "\n\n" + section
