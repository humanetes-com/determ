"""FR "Model Notes" domain — the model's self-authored anti-hallucination notes
section inside ``functional_requirements_{crate}.md`` (CLAUDE.md: "The model
authors its own anti-hallucination notes").

Pure text transforms over the FR markdown: locate the ``## Model Notes`` section,
split it from the body, parse its bullets, and re-attach it when FR is
regenerated (so TASK 1 never wipes the accumulated notes). Stdlib only, **no
regex** — the next-heading boundary is found by a structured char scan
(previously `re.search(r"\\n##\\s")`)."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from .rust_scan import item_names
from .test_results import (
    camelcase_type_tokens,
    parse_error_code_note,
    rustc_error_codes,
)

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


# Pre-canned notes injected deterministically (not LLM-extracted) when a
# specific structural pattern is detected. Each is a single bullet string
# containing ≥2 WRONG→RIGHT pairs for few-shot variety (CLAUDE.md rule).

_ERRORIMPL_OBJECT_NOTE = (
    "ErrorImpl<E> stores the wrapped error in field `_object: E`, not `error` or "
    "`inner`. "
    "WRONG: `self.error.source()` (no field `error` → E0609) / "
    "RIGHT: `self._object.source()`. "
    "WRONG: `self.inner.source()` (no field `inner` → E0609) / "
    "RIGHT: `self._object.source()`."
)


def is_errorimpl_container(container: str) -> bool:
    """True when the impl container involves ErrorImpl — signals that the
    `_object` field canned note should be appended on first compile failure."""
    return "ErrorImpl" in container


def errorimpl_object_note() -> str:
    """Pre-canned WRONG→RIGHT note for ErrorImpl's `_object` field naming.
    Appended on the first compile failure of any ErrorImpl fn so that
    subsequent attempts see the note in MODEL_NOTES."""
    return _ERRORIMPL_OBJECT_NOTE


def render_with_notes(fr_text: str, notes: list[str]) -> str:
    """Rewrite the notes section of ``fr_text`` with ``notes`` as bullets,
    creating a minimal FR body when the input has none."""
    body, _ = slice_model_notes(fr_text)
    if not body.strip():
        body = "# Functional Requirements\n\n"
    section = MODEL_NOTES_HEADER + "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    return body.rstrip() + "\n\n" + section


def strip_model_notes_section(fr_text: str) -> str:
    """Drop ``## Model Notes …`` from an FR copy used for repo-spec excerpts —
    the notes already reach prompts via the dedicated MODEL_NOTES filter."""
    idx = fr_text.find("## Model Notes")
    if idx == -1:
        return fr_text
    nxt = fr_text.find("\n## ", idx + 1)
    return fr_text[:idx] + (fr_text[nxt + 1:] if nxt != -1 else "")


# ---------------------------------------------------------------------------
# Note relevance / dedup / recurring-error promotion — pure domain logic.
# (The FR-file I/O, the run-state accumulators and the LLM extraction call live
# in adapters/model_notes_store.py; only the rules live here.)
# ---------------------------------------------------------------------------

# Curated Rust load-bearing keywords that, co-mentioned in two notes, almost
# certainly mean the notes are about the same confusion (semantic dedup, P3.3).
NOTE_SIGNATURE_KEYWORDS = frozenset({
    "typeid", "'static", "static", "&dyn", "dyn", "box", "nonnull",
    "ptr", "as_ptr", "as_ref", "from_raw", "deref",
    "unimplemented", "todo", "unsafe",
    "trait", "impl", "generic", "lifetime", "sized", "unsized",
    "format", "display", "debug",
    "feature", "no_std", "core::", "std::",
    "ref::", "wrapper", "delegation",
})

ERROR_CODE_PROMOTED_THRESHOLD = 3

ERROR_CODE_HINTS: dict[str, str] = {
    "E0277": "trait bound not satisfied — TWO opposite cases (see the matching gotcha for concrete examples): (a) by-value return/use of a possibly-unsized `T` (\"size cannot be known\") → you MAY add `where T: Sized` to THIS one fn, or return `&T`/`Box<T>`; (b) `T doesn't implement Display/Debug/...` → you may NOT add that bound (it is the caller's contract) — do not call that trait on `T`, build the value another way.",
    "E0308": "mismatched types — check whether you're returning a Result vs Option, owned vs referenced, or wrong generic instantiation.",
    "E0382": "use of moved value — clone, borrow, or use Cow/Arc if you need both old and new value.",
    "E0507": "cannot move out of borrowed — use .clone(), .as_ref(), or destructure with `ref`.",
    "E0599": "no method found — likely calling a method that doesn't exist on this type. Read TYPES_IN_SCOPE for the exact method names.",
    "E0609": "no field — usually trying to access a method as if it were a field. Use .name() not .name. See TYPES_IN_SCOPE.",
    "E0615": "took value of method instead of calling it — `.method` vs `.method()`. Add parens.",
    "E0107": "wrong number of generic arguments — check the type's actual generic signature in TYPES_IN_SCOPE.",
    "E0310": "type parameter may not live long enough — add a `'static` bound.",
    "E0119": "conflicting impls — remove the duplicate or specialise via different generic params.",
    "E0554": "feature attribute only on nightly — guard with #[cfg] or check that the nightly toolchain is active.",
}


def is_wellformed_error_code(code: str) -> bool:
    """True for a canonical ``E`` + 3-or-4-digit rustc code (e.g. ``E0277``) —
    the structured replacement for ``re.fullmatch(r"E\\d{3,4}", code)``."""
    return (
        code[:1] == "E"
        and code[1:].isdigit()
        and 3 <= len(code) - 1 <= 4
    )


def note_hash(text: str) -> str:
    """Exact-text hash for early-rejection of literal duplicate notes."""
    norm = " ".join(text.lower().split())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


def note_signature(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Semantic signature ``(error codes mentioned, keyword set)`` of a note.

    Two notes are semantically duplicate (P3.3) when these signatures' keyword
    sets overlap ≥80% — catches the 4-way paraphrase of "TypeId::of::<T>() needs
    T: 'static" that exact-hash dedup let through."""
    low = text.lower()
    err_codes = frozenset(rustc_error_codes(low, "e"))
    keywords = frozenset(k for k in NOTE_SIGNATURE_KEYWORDS if k in low)
    return err_codes, keywords


def notes_are_semantic_dupes(a: str, b: str) -> bool:
    """True when two notes mostly mean the same thing.

    Same non-empty error-code set → dupe regardless of phrasing; otherwise
    keyword overlap ≥2 AND ≥50% Jaccard (the 50% catches the overnight
    TypeId-paraphrase set where each note adds one unique extra keyword)."""
    a_err, a_kw = note_signature(a)
    b_err, b_kw = note_signature(b)
    if a_err and b_err and a_err == b_err:
        return True
    if not a_kw or not b_kw:
        return False
    inter = len(a_kw & b_kw)
    union = len(a_kw | b_kw)
    if inter < 2:
        return False
    return union > 0 and inter / union >= 0.5


def prepare_note(note: str, error_codes: list[str] | set[str] | None = None) -> str | None:
    """Normalise a candidate note (strip, drop leading bullet, prepend an
    ``[E0xxx,...]`` tag from ``error_codes``). ``None`` when too short to keep
    (<20 chars) — the caller then skips the append."""
    note = (note or "").strip()
    if not note or len(note) < 20:
        return None
    if note.startswith("- "):
        note = note[2:].strip()
    if error_codes:
        clean = sorted({c for c in error_codes
                        if isinstance(c, str) and is_wellformed_error_code(c)})
        if clean and not note.startswith("[E"):
            note = f"[{','.join(clean)}] {note}"
    return note


def dedup_append(
    note: str,
    existing: list[str],
    existing_hashes: set[str],
    max_entries: int,
) -> tuple[list[str], bool]:
    """Append a prepared ``note`` to ``existing`` unless it is a literal or
    semantic duplicate; LRU-trim to ``max_entries``. Returns
    ``(new_notes, added)`` and updates ``existing_hashes`` in place."""
    h = note_hash(note)
    if h in existing_hashes:
        return existing, False
    if any(notes_are_semantic_dupes(prev, note) for prev in existing):
        return existing, False
    out = [*existing, note]
    existing_hashes.add(h)
    if len(out) > max_entries:
        for dropped in out[:-max_entries]:
            existing_hashes.discard(note_hash(dropped))
        out = out[-max_entries:]
    return out, True


def rank_notes(
    notes: list[str],
    *,
    file_rel: str,
    fn_name: str,
    file_text: str,
    current_error: str,
    err_codes: set[str],
    max_chars: int,
) -> str:
    """Select notes ranked by relevance to the current ``(file, fn, error)`` and
    render them as a ``<MODEL_NOTES>`` bullet block capped at ``max_chars``.

    Tiers (lower = higher priority): 1 note names an active error code; 2 keyword
    overlap with the error signature; 3 names the file/fn; 4 names a symbol
    defined in ``file_text``; 5 recency only. ``err_codes`` is supplied by the
    caller (cargo-text parse lives in the application layer)."""
    if not notes:
        return ""
    base = Path(file_rel).stem if file_rel else ""
    symbols: set[str] = set(item_names(file_text)) if file_text else set()
    err_keywords: set[str] = set()
    if current_error:
        low_err = current_error.lower()
        err_keywords = {k for k in NOTE_SIGNATURE_KEYWORDS if k in low_err}

    def tier(n: str) -> int:
        lower = n.lower()
        if err_codes and any(c in lower.upper() or c.lower() in lower
                              for c in err_codes):
            return 1
        _, n_kw = note_signature(n)
        if err_keywords and n_kw and len(err_keywords & n_kw) >= 2:
            return 2
        if (base and base.lower() in lower) or (fn_name and fn_name.lower() in lower):
            return 3
        if any(sym in n for sym in symbols):
            return 4
        return 5

    indexed = sorted(enumerate(notes), key=lambda iv: (tier(iv[1]), -iv[0]))
    out_lines: list[str] = []
    total = 0
    for _, n in indexed:
        line = f"- {n}"
        if total + len(line) + 1 > max_chars:
            break
        out_lines.append(line)
        total += len(line) + 1
    return "\n".join(out_lines)


def mine_common_tokens(
    excerpts: list[str], top_n: int = 3,
) -> list[tuple[str, int]]:
    """The most common Rust type-like CamelCase tokens across cargo error
    ``excerpts`` (≥2 occurrences, one count per excerpt) — used to enrich
    RECURRING_ERRORS with type context."""
    if not excerpts:
        return []
    tok: Counter[str] = Counter()
    skip = {
        "Err", "Ok", "None", "Some", "Box", "Self", "T", "E", "C", "V",
        "From", "Into", "Vec", "String", "Result", "Option",
    }
    for ex in excerpts:
        seen_here: set[str] = set()
        for t in camelcase_type_tokens(ex):
            bare = t.split("<", 1)[0]
            if bare in skip:
                continue
            if len(bare) < 4 and "<" not in t:
                continue
            if t in seen_here:
                continue  # one count per excerpt
            seen_here.add(t)
            tok[t] += 1
    return [(t, n) for t, n in tok.most_common(top_n) if n >= 2]


def build_promoted_rules(
    counts: Counter[str],
    excerpts: dict[str, list[str]],
    stored_notes: list[str],
    *,
    threshold: int = ERROR_CODE_PROMOTED_THRESHOLD,
    hints: dict[str, str] = ERROR_CODE_HINTS,
) -> str:
    """A compact block listing error codes that recurred ``threshold``+ times
    this run, each with its canonical hint, any shared type-tokens mined from
    the excerpts (P6.3), and up to 2 past ``[E0xxx]``-tagged notes (P6.4)."""
    if not counts:
        return ""
    code_to_notes: dict[str, list[str]] = {}
    for note in reversed(stored_notes):  # newest-first → most recent rule wins
        parsed = parse_error_code_note(note)
        if parsed is None:
            continue
        code_list, body = parsed
        for code in code_list.split(","):
            bucket = code_to_notes.setdefault(code, [])
            if body not in bucket:
                bucket.append(body)

    promoted: list[str] = []
    for code, count in counts.most_common():
        if count < threshold:
            break
        hint = hints.get(code, f"recurring; rustc --explain {code} for details.")
        common = mine_common_tokens(excerpts.get(code, []))
        if common:
            shared = ", ".join(f"`{t}`" for t, _ in common[:3])
            promoted.append(f"  {code} (seen {count}×, often on {shared}): {hint}")
        else:
            promoted.append(f"  {code} (seen {count}×): {hint}")
        for past in code_to_notes.get(code, [])[:2]:
            past_short = past if len(past) <= 240 else past[:237] + "..."
            promoted.append(f"    ↳ past note: {past_short}")
    if not promoted:
        return ""
    return ("These errors keep recurring in this run; address them proactively:\n"
            + "\n".join(promoted))
