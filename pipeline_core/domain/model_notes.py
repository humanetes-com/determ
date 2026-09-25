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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .rust_lex import code_mask
from .rust_scan import declared_fn_names, item_names, struct_names
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


#: Continuation lines of a multi-line note carry this indent in the FR file, so
#: a note whose body starts at column 0 (`#[cold]`, `pub(crate) fn …`) is still
#: one bullet on disk. `render_with_notes` adds it; the parser strips it.
NOTE_CONTINUATION_INDENT = "  "


def parse_note_bullets(notes_section: str) -> list[str]:
    """The note bullets in a notes section: the text after ``- ``, with every
    following line until the next bullet kept as part of that note, NEWLINE-
    joined and de-indented.

    Nothing is dropped. The previous reader kept only lines starting with two
    spaces and silently discarded everything else, which destroyed the body of
    every `[regression]` note the writer emits — see
    `regression_note_body_probe`. It also joined with a space, so even a
    surviving body arrived as one run-on line the model could not read as code.
    A blank line no longer closes a note either: a body may contain one, and
    the section boundary is already `slice_model_notes`'s job."""
    if not notes_section.strip():
        return []
    notes: list[str] = []
    cur: list[str] = []

    def close() -> None:
        if cur:
            text = "\n".join(cur).strip()
            if text:
                notes.append(text)

    for ln in notes_section.splitlines():
        if ln.startswith("- "):
            close()
            cur = [ln[2:].rstrip()]
        elif _is_markdown_heading(ln):
            # A heading is structure, never body. The test for it is `#` then a
            # SPACE, which no Rust attribute matches (`#[cold]`, `#![allow]`).
            close()
            cur = []
        elif cur:
            cur.append(ln[len(NOTE_CONTINUATION_INDENT):].rstrip()
                       if ln.startswith(NOTE_CONTINUATION_INDENT)
                       else ln.rstrip())
    close()
    return notes


def _is_markdown_heading(line: str) -> bool:
    stripped = line.lstrip("#")
    return line.startswith("#") and stripped.startswith(" ")


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


# Layout-contradicted notes — reader-side retirement. A private layout is
# re-authorable per cluster and per bake, so a persisted field fact can
# silently invert between the moment a note is written and the moment it is
# injected. The current file text is the single source of truth for field
# names; notes are checked against it at prompt-build time. (Notes must never
# hardcode a specific crate's private field names in pipeline source either —
# a canned note did exactly that and kept teaching the model a field the
# re-authored layout no longer had.)


def _ident_at(text: str, i: int) -> str:
    """The identifier starting at ``i`` (empty when none starts there)."""
    n = len(text)
    j = i
    if j < n and (text[j].isalpha() or text[j] == "_"):
        j += 1
        while j < n and (text[j].isalnum() or text[j] == "_"):
            j += 1
    return text[i:j]


def _tuple_positions(file_text: str, i: int) -> frozenset[str]:
    """Positional field names of the tuple struct whose ``(`` starts at ``i``:
    ``struct X(A, B);`` → ``{"0", "1"}``. A tuple struct HAS fields, they are
    just numbered — so ``self.0`` is a valid claim against it and ``self.name``
    is not, which is exactly the discrimination the sieve needs."""
    n = len(file_text)
    depth = angle = fields = 0
    filled = False
    while i < n:
        two = file_text[i:i + 2]
        if two == "//":
            j = file_text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if two == "/*":
            j = file_text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        ch = file_text[i]
        if ch in "([":
            # a bracket inside the outer parens is field CONTENT too — an
            # array/tuple-typed field like `([u8; 4], …)` must count
            filled = filled or depth > 0
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                break
        elif ch == "<":
            angle += 1
        elif ch == ">" and angle and file_text[i - 1] != "-":  # not `->`
            angle -= 1
        elif ch == "," and depth == 1 and angle == 0:
            fields += filled
            filled = False
        elif depth == 1 and not ch.isspace():
            filled = True
        i += 1
    return frozenset(str(k) for k in range(fields + filled))


def struct_fields(file_text: str, type_name: str) -> frozenset[str] | None:
    """Field names of ``struct type_name`` as declared in ``file_text`` — named
    fields for a braced struct, positional (``"0"``, ``"1"``, …) for a tuple
    struct — or ``None`` when no struct of that exact name is declared there
    (absent, or a unit struct): ``None`` means "no evidence", callers keep
    notes in doubt.

    Char scan, comment/string aware — including the ``struct NAME`` anchor
    itself: an annotated-stub comment quoting a failed attempt (or a doc
    example) can contain a plain ``struct NAME { … }`` declaration, and
    anchoring there would read a phantom layout. A field name is the
    identifier ending at a ``:`` at brace depth 1 (``::`` path separators
    excluded; attribute args and generic bounds carry their colons inside
    ``[]``/``<>`` nesting or as ``=`` bindings, so they cannot false-hit at
    depth 1)."""
    mask = code_mask(file_text)
    needle = f"struct {type_name}"
    idx = 0
    while True:
        idx = file_text.find(needle, idx)
        if idx == -1:
            return None
        end = idx + len(needle)
        prv = file_text[idx - 1:idx]
        nxt = file_text[end:end + 1]
        if (not mask[idx]
                and (not prv or not (prv.isalnum() or prv == "_"))
                and (not nxt or not (nxt.isalnum() or nxt == "_"))):
            break  # code position, exact name, not part of a longer identifier
        idx = end
    n = len(file_text)
    i = end
    angle = 0
    while i < n:
        ch = file_text[i]
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif angle == 0:
            if ch == "{":
                break
            if ch == "(":
                return _tuple_positions(file_text, i)
            if ch == ";":
                return None  # unit struct: no fields to judge a claim against
        i += 1
    if i >= n:
        return None
    depth = 1
    i += 1
    fields: set[str] = set()
    while i < n and depth > 0:
        two = file_text[i:i + 2]
        if two == "//":
            j = file_text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if two == "/*":
            j = file_text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        ch = file_text[i]
        if ch == '"':
            i += 1
            while i < n and file_text[i] != '"':
                i += 2 if file_text[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif (ch == ":" and depth == 1
              and file_text[i + 1:i + 2] != ":"
              and file_text[i - 1:i] != ":"):
            j = i - 1
            while j >= 0 and file_text[j] in " \t":
                j -= 1
            k = j
            while k >= 0 and (file_text[k].isalnum() or file_text[k] == "_"):
                k -= 1
            name = file_text[k + 1:j + 1]
            if name and not name[0].isdigit():
                fields.add(name)
        i += 1
    return frozenset(fields)


def _digits_at(text: str, i: int) -> str:
    """The digit run starting at ``i`` (empty when none) — a tuple-field index."""
    j = i
    while j < len(text) and text[j].isdigit():
        j += 1
    return text[i:j]


def _field_claims(note: str, struct_names: set[str]) -> frozenset[str]:
    """Field names the note claims to exist, from three concrete shapes:
    ``self.name`` / ``self.0`` access (method calls ``self.name()`` excluded),
    ``field `name``` / ```name` field`` prose, and ``TypeName { name, … }``
    struct literals for the given struct names.

    Tuple indices count as claims because they are how a note recommends a
    tuple layout: guidance reading "replace `_object` with `self.0`" against a
    struct with NAMED fields is a field-name lie the same way ``self._object``
    is against a struct without it."""
    claims: set[str] = set()
    start = 0
    while True:
        i = note.find("self.", start)
        if i == -1:
            break
        name = _ident_at(note, i + 5) or _digits_at(note, i + 5)
        if name and note[i + 5 + len(name):i + 6 + len(name)] != "(":
            claims.add(name)
        start = i + 5
    start = 0
    while True:
        i = note.find("field `", start)
        if i == -1:
            break
        name = _ident_at(note, i + len("field `"))
        if name:
            claims.add(name)
        start = i + 1
    start = 0
    while True:
        i = note.find("`", start)
        if i == -1:
            break
        name = _ident_at(note, i + 1)
        j = i + 1 + len(name)
        if name and note[j:j + 1] == "`" and note[j + 1:].lstrip().startswith("field"):
            claims.add(name)
        start = i + 1
    for s in struct_names:
        start = 0
        while True:
            i = note.find(s, start)
            if i == -1:
                break
            start = i + len(s)
            j = start
            if note[j:j + 1] == "<":
                depth = 0
                while j < len(note):
                    if note[j] == "<":
                        depth += 1
                    elif note[j] == ">":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
            while j < len(note) and note[j] == " ":
                j += 1
            if note[j:j + 1] != "{":
                continue
            j += 1
            for frag in note[j:note.find("}", j) if note.find("}", j) != -1
                             else len(note)].split(","):
                name = _ident_at(frag.strip(), 0)
                if name:
                    claims.add(name)
    return frozenset(claims)


def _call_claims(note: str) -> frozenset[str]:
    """Snake_case names the note prescribes CALLING, from the one shape that
    says so unambiguously: a turbofish, ``name::<...>``.

    Path-qualified segments (``crate::wrapper::helper::<T>``) are skipped — the
    name resolves in another module, so this file's declarations say nothing
    about it. Type paths are skipped by the same test, their last segment being
    CamelCase.

    A field and a fn with the same name coexist happily in Rust, which is what
    makes this worth extracting separately: `_field_claims` sees
    ``object_drop_rest`` on `ErrorVTable` and is satisfied, while the note is
    directing the model at a free fn of that name that the crate never declares.
    """
    claims: set[str] = set()
    start = 0
    while True:
        i = note.find("::<", start)
        if i == -1:
            break
        start = i + 3
        j = i
        while j > 0 and (note[j - 1].isalnum() or note[j - 1] == "_"):
            j -= 1
        name = note[j:i]
        if (name and (name[0].isalpha() or name[0] == "_")
                and not name[0].isupper() and note[max(0, j - 2):j] != "::"):
            claims.add(name)
    return frozenset(claims)


def _fields_declared_anywhere(
    file_text: str, cache: dict[str, frozenset[str] | None],
) -> frozenset[str]:
    """Every field name declared by any struct in ``file_text``.

    Deliberately not scoped to the structs the note names: the note that cost
    the plateau said "replace `object_drop_front::<E>` with `object_drop_rest::<E>`"
    while naming only `Box` and `Self`, so a scoped lookup would have anchored on
    `Error` (field `inner`) and seen nothing to contradict."""
    out: set[str] = set()
    for name in struct_names(file_text):
        if name not in cache:
            cache[name] = struct_fields(file_text, name)
        out.update(cache[name] or ())
    return frozenset(out)


class LayoutJudgement(Enum):
    """Outcome of checking one piece of persisted model-facing text against the
    layout of the file being compiled. ``UNANCHORED`` is not "fine" — it is
    "unjudgeable": nothing in the text ties it to a struct declared here."""
    UNANCHORED = "unanchored"
    CONSISTENT = "consistent"
    CONTRADICTED = "contradicted"


def judge_note_against_layout(
    note: str, file_text: str, *,
    anchor_types: frozenset[str] = frozenset(),
    cache: dict[str, frozenset[str] | None] | None = None,
) -> LayoutJudgement:
    """Judge one note/guidance string against the structs declared in
    ``file_text``.

    Anchoring: the structs the prose NAMES decide the field vocabulary. When it
    names none that this file declares, ``anchor_types`` (the self type of the
    impl the text was earned on) supplies it instead — without that fallback,
    text like "change the body to ``self._object.provide(request)``" carries a
    field claim no sieve can place, and stays injected forever. When the
    prose names at least one of ``anchor_types``, the remaining anchors
    join the vocabulary: a two-member transaction whose prose names one
    half and claims a field of the other is about those members, not a lie
    about the named half (run `106897-f7b6fb77` fill 8: named ErrorImpl,
    claimed `self.inner` of Error, retired as layout_contradicted with no
    log line). A named type outside the anchors is still judged only
    against what it names — the sibling-struct bias of
    `test_anchor_is_a_fallback_not_an_override`. A leftover claim while a
    self type is not declared in this file is unanchored (no evidence),
    not contradicted."""
    if not note or not file_text:
        return LayoutJudgement.UNANCHORED
    cache = {} if cache is None else cache

    def fields_of(name: str) -> frozenset[str] | None:
        if name not in cache:
            cache[name] = struct_fields(file_text, name)
        return cache[name]

    # A prescribed call on a name this file declares as a FIELD and never as a
    # fn: judged before the struct anchoring below, because it needs no anchor
    # — the file's own declarations settle it either way.
    calls = _call_claims(note)
    if calls and (calls & _fields_declared_anywhere(file_text, cache)
                  ) - declared_fn_names(file_text):
        return LayoutJudgement.CONTRADICTED

    named: dict[str, frozenset[str]] = {}
    for tok in camelcase_type_tokens(note):
        f = fields_of(tok.split("<", 1)[0])
        if f is not None:
            named[tok.split("<", 1)[0]] = f
    # Complete the unit's vocabulary when the prose names at least one of
    # its self types. A named type outside the anchors still does not
    # pick up the unit's fields (the sibling-struct bias).
    if not named or (frozenset(named) & anchor_types):
        for anchor in anchor_types:
            f = fields_of(anchor)
            if f is not None:
                named[anchor] = f
    if not named:
        return LayoutJudgement.UNANCHORED
    known: frozenset[str] = frozenset().union(*named.values())
    leftover = [c for c in _field_claims(note, set(named)) if c not in known]
    if leftover:
        # An unresolved self type is not in this file text — the leftover
        # claim may be a field of that type. `struct_fields` already treats
        # absence as no evidence; contradicting here is a lie about the
        # named in-file type (run `106897-f7b6fb77` fill 8: Error lives in
        # lib.rs, the pair's halves are error.rs+fmt.rs, `self.inner` was
        # retired as a lie about ErrorImpl).
        if any(fields_of(a) is None for a in anchor_types):
            return LayoutJudgement.UNANCHORED
        return LayoutJudgement.CONTRADICTED
    return LayoutJudgement.CONSISTENT


def layout_contradicted_notes(
    notes: list[str], file_text: str, *,
    anchor_types: frozenset[str] = frozenset(),
) -> list[str]:
    """Notes claiming a field that exists on none of the structs the note
    names (or, failing that, on none of ``anchor_types``), judged against the
    structs actually declared in ``file_text`` (the current spliced layout for
    the file being filled).

    Observed live: a note teaching ``self._object`` against a re-authored
    layout whose field was ``error: E`` steered every attempt into E0609 on
    the exact file that held the pass plateau; the same note was TRUE for the
    sibling cluster's frozen layout — correctness is per (cluster, bake), so
    it must be judged against the file text of the attempt being prompted.
    Bias to retire: claims inside WRONG-example fragments count too — losing
    a true-but-redundant warning is cheap (the struct definition is already
    visible in the prompt), keeping a false recommendation poisons every
    subsequent attempt. A named type outside ``anchor_types`` is still
    judged only against what it names. Named types that intersect the
    anchors take the rest of the anchors into the vocabulary, so a
    two-member write that names one half and claims a field of the other
    is not retired as a lie about the named half. A leftover claim while
    a self type is not declared in this file is unanchored, not
    contradicted."""
    if not notes or not file_text:
        return []
    cache: dict[str, frozenset[str] | None] = {}
    return [n for n in notes
            if judge_note_against_layout(n, file_text, anchor_types=anchor_types,
                                         cache=cache)
            is LayoutJudgement.CONTRADICTED]


def guidance_contradicts_layout(
    guidance: str, file_text: str, *,
    anchor_types: frozenset[str] = frozenset(),
) -> bool:
    """True when a stored retrospective's guidance claims a field that exists
    on none of the structs it names, judged against ``file_text``.

    Retrospectives are keyed by (file, fn, container) with NO cluster, so one
    store serves clusters whose private layouts differ; a guidance string that
    is correct against one cluster's layout is a field-name lie against the
    other's, and it is injected verbatim into every fix prompt. Judging it
    against the text of the file actually being compiled makes it correct per
    (cluster, bake) by construction, without mutating the shared store."""
    return judge_note_against_layout(
        guidance, file_text, anchor_types=anchor_types,
    ) is LayoutJudgement.CONTRADICTED


def layout_safe_guidance(
    guidance: str, file_text: str, *,
    anchor_types: frozenset[str] = frozenset(),
) -> str:
    """``guidance`` unless it contradicts the current layout, else ``""``."""
    return "" if guidance_contradicts_layout(
        guidance, file_text, anchor_types=anchor_types) else guidance


def src_text_declaring(src_dir: Path, type_names: Iterable[str]) -> str:
    """Concatenate every ``.rs`` file under ``src_dir`` that declares one of
    ``type_names`` as a struct. Empty names or a missing dir yield "".

    A pair's halves are the files it writes; a member's self type may be
    declared in a third file (Error in lib.rs, the Debug impl in error.rs).
    Judging the pair only against the halves treats a field of that type
    as a lie about the in-file implementer."""
    names = frozenset(n for n in type_names if n)
    if not names or not src_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(src_dir.rglob("*.rs")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(struct_fields(text, n) is not None for n in names):
            parts.append(text)
    return "\n".join(parts)


def render_note_bullet(note: str) -> str:
    """One note as a markdown bullet, continuation lines indented so the reader
    can give them back verbatim. A blank line inside a body is rendered as the
    bare indent, because an empty line would look like a note boundary."""
    lines = note.splitlines() or [""]
    out = [f"- {lines[0]}"]
    out.extend(f"{NOTE_CONTINUATION_INDENT}{ln}" if ln.strip()
               else NOTE_CONTINUATION_INDENT
               for ln in lines[1:])
    return "\n".join(out)


def render_with_notes(fr_text: str, notes: list[str]) -> str:
    """Rewrite the notes section of ``fr_text`` with ``notes`` as bullets,
    creating a minimal FR body when the input has none.

    Round-trips through `parse_note_bullets` for multi-line notes as well as
    single-line ones — the property the `[regression]` notes' bodies depend on,
    since every FR append is a read-modify-write and one lossy read destroys
    them for good."""
    body, _ = slice_model_notes(fr_text)
    if not body.strip():
        body = "# Functional Requirements\n\n"
    section = (MODEL_NOTES_HEADER + "\n\n"
               + "\n".join(render_note_bullet(n) for n in notes) + "\n")
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
# certainly mean the notes are about the same confusion (semantic dedup).
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

    Two notes are semantically duplicate when these signatures' keyword
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
        # Same bullet shape the FR file uses, so a multi-line note reaches the
        # prompt as the code it is rather than as one run-on line.
        line = render_note_bullet(n)
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
    the excerpts, and up to 2 past ``[E0xxx]``-tagged notes."""
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


def is_bogus_regression_note(note: str) -> bool:
    """A `[regression]` note whose recorded aggregate did NOT drop is provably
    invalid feedback: "pass count dropped 22\u219222" (no change) or
    "dropped 15\u219216" (an INCREASE) came from the shotgun-blame era —
    every fn filled in a pass was blamed for any test flip regardless of the
    aggregate direction. Injecting them steers the model AWAY from correct
    bodies (observed live: textbook `source`/`root_cause` bodies marked
    WRONG). Readers drop them; the writer no longer produces them."""
    if not note.lstrip().startswith("[regression]"):
        return False
    marker = "count dropped "
    i = note.find(marker)
    if i == -1:
        return False
    rest = note[i + len(marker):]
    arrow = rest.find("\u2192")
    if arrow == -1:
        return False
    prev_s = rest[:arrow].strip()
    after = rest[arrow + 1:]
    now_digits = ""
    for ch in after:
        if ch.isdigit():
            now_digits += ch
        else:
            break
    if not (prev_s.isdigit() and now_digits):
        return False
    return int(now_digits) >= int(prev_s)


def regression_note_body_probe(note: str) -> str:
    """The first substantial line of a `[regression]` note's recorded body, or
    "" when the note carries no body that can be checked against a file.

    "" is the interesting case and it is not rare: the writer emits
    ``Body applied:\\n<body>\\nRe-implement `fn` so <tests> keep passing.``
    with the body's own indentation, and until the round trip was fixed
    `render_with_notes` + `parse_note_bullets` discarded every body line that
    started at column 0 — a Rust body opens `#[cold]` or `pub(crate) fn`. The
    next append is a read-modify-write, so the mutilated note overwrote the
    intact one on disk. Measured on `tmp/anyhow/functional_requirements_anyhow.md`
    2026-08-22: of 29 `[regression]` notes, 0 kept the `Re-implement` line, 14
    kept no body line at all, 15 kept only whichever line happened to be
    indented. Whether a note survived was decided by that accident alone."""
    if not note.lstrip().startswith("[regression]"):
        return ""
    body = ""
    for marker in ("Body applied:", "WRONG (this body broke those tests):"):
        i = note.find(marker)
        if i != -1:
            body = note[i + len(marker):]
            break
    for ln in body.splitlines():
        probe = ln.strip()
        # The trailing instruction line is not body text.
        if len(probe) >= 12 and not probe.startswith("Re-implement"):
            return probe
    return ""


def regression_note_subject(note: str) -> str:
    """The fn a `[regression]` note blames, from its ``after `fn` was written``
    clause; "" for any other note."""
    if not note.lstrip().startswith("[regression]"):
        return ""
    i = note.find("after `")
    if i == -1:
        return ""
    j = note.find("`", i + len("after `"))
    return note[i + len("after `"):j] if j != -1 else ""


def escalation_superseded_notes(
    notes: list[str], escalated_fns: frozenset[str],
) -> list[str]:
    """`[regression]` notes blaming a fn this sweep has graded ESCALATED.

    The grade says the loop MEASURED that rewriting this unit's consumers moved
    no verdict, so the prompt orders it rewritten; the note says a past rewrite
    of it cost N tests. Two instructions in one prompt is not a steer, and the
    measurement is both newer and checkable where the note's blame is spread
    over every fn of a batch. This is the second channel of the same collision
    the retrospectives store had — that one is suspended at the fire
    (`suspend_futile_guidance`), and leaving this one live is what run
    `263257-9e46def4` iter 3 (2026-08-22) measured: five escalated constructors
    dispatched, five returned byte-identical, the reply's reasoning citing the
    notes."""
    if not escalated_fns:
        return []
    return [n for n in notes
            if (s := regression_note_subject(n)) and s in escalated_fns]


def is_stale_regression_note(note: str, file_text: str) -> bool:
    """A `[regression]` note whose recorded body is no longer the file's
    current body describes a SUPERSEDED attempt — its causal claim ("this
    body broke those tests") cannot bind the model's next attempt. Retire it
    from prompts (the note stays on disk).

    A note with NO recoverable body is retired too, and that is a deliberate
    inversion of the old "no evidence either way → keep". The rule reads
    differently for a note that ADVISES than for one that ACCUSES: the whole
    content of a `[regression]` note is "THIS body broke those tests", so with
    the body gone the claim cannot be checked against the tree, cannot be acted
    on, and — because the check that retires it is exactly the one that needs
    the body — can never be retired either. Run `263257-9e46def4` iter 3
    (2026-08-22) dispatched `src/error.rs::construct_from_adhoc` and
    `construct_from_display` under the ESCALATED grade, i.e. told to rewrite,
    with body-less notes in the same prompt saying a rewrite of each had cost
    83→25; the reply's own reasoning cites them three times and returns both
    bodies byte-identical. `file_text` empty still keeps a note that HAS a
    body — there the comparison, not the claim, is what is missing."""
    probe = regression_note_body_probe(note)
    if not probe:
        return note.lstrip().startswith("[regression]")
    if not file_text:
        return False
    return probe[:60] not in file_text


@dataclass(frozen=True, slots=True)
class NoteProvenance:
    """The premise a durable lesson was learned under — the record the writer
    may not erase even though the prose generalises.

    The note-extraction prompt orders the model to name no function, file or
    crate ("the note should generalise"), which is right for the PROSE and was
    fatal for the RECORD: the `[E0425]` lesson "use the std-compatible
    construction helper that omits the vtable" was learned when the vtable
    free fns were neither writable nor on the tree — a scope-conditional
    workaround — and with the condition erased it stood as immortal design
    guidance, steering the pass-93 escalated rewrites into the exact wrong
    body (run `40938-f4e8e94b`, prompt line 1213). Provenance is stored BESIDE
    the prose, never inside it, so the anonymity order is inverted at the
    store level only. A lesson that carries no provenance is run-scoped,
    never durable."""

    unit: str                          # "<file>::<fn>" the failed attempt was on
    error_codes: tuple[str, ...]       # the codes the attempt died with
    rejected_symbols: tuple[str, ...]  # what the compiler said does not exist
    failed_body_hash: str              # fingerprint of the rejected body
    # True: a later attempt of the same loop COMPILED (the recovered path),
    # so the lesson's correction was seen to work. False: the loop was
    # exhausted and nothing validated it. None: a record written before
    # 2026-09-25, which does not say — treated as not validated.
    validated: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {"unit": self.unit, "error_codes": list(self.error_codes),
                "rejected_symbols": list(self.rejected_symbols),
                "failed_body_hash": self.failed_body_hash,
                "validated": self.validated}

    @staticmethod
    def from_dict(raw: object) -> NoteProvenance | None:
        """Total by design — a corrupt sidecar entry reads as no provenance."""
        if not isinstance(raw, dict):
            return None
        try:
            return NoteProvenance(
                unit=str(raw.get("unit", "")),
                error_codes=tuple(str(c) for c in raw.get("error_codes") or ()),
                rejected_symbols=tuple(
                    str(s) for s in raw.get("rejected_symbols") or ()),
                failed_body_hash=str(raw.get("failed_body_hash", "")),
                validated=(bool(raw["validated"])
                           if raw.get("validated") is not None else None))
        except (TypeError, ValueError):
            return None


def body_fingerprint(text: str) -> str:
    """Whitespace-normalised md5 of a body — the failed-body identity a
    `NoteProvenance` records (case preserved: Rust identifiers are cased)."""
    return hashlib.md5(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def is_error_code_lesson(note: str) -> bool:
    """True for a note carrying an ``[E0xxx,...]`` tag — the compile-lesson
    class 204 is about. `[regression]` notes have their own class and rules."""
    return parse_error_code_note(note.strip()) is not None


def error_code_lessons(notes: list[str]) -> list[str]:
    """The subset of ``notes`` that are ``[E0xxx]`` compile lessons."""
    return [n for n in notes if is_error_code_lesson(n)]


def disproved_lessons(
    notes: list[str],
    provenance: Mapping[str, NoteProvenance],
    *, unit: str, error_codes: Iterable[str], shown: str,
) -> list[str]:
    """The stored lessons ``unit``'s own failure just disproved: learned on
    that very unit (`NoteProvenance.unit`), never validated by a compiling
    body (`validated` is not True), and about a code the unit has just
    failed with again — with the lesson in its prompt.

    Such a note prescribes a cure nothing ever saw work, and here its own
    unit re-fails the way it claims to prevent. Run `116341-24c76a97`
    (2026-09-25): an exhausted `context` loop wrote "[E0277] … use self
    directly, not a boxed dyn conversion" at 11:10 — a cure invented with no
    fixed body — and `context` then failed E0277 in fills 5, 6 and 7 with
    that note twice in its prompt (MODEL_NOTES and the RECURRING_ERRORS
    echo). The premise check (`void_premise_lessons`) keys on rejected
    symbols, of which this note had none, and `contradicted_notes` on
    E0599/E0425, so nothing could retire it. A validated lesson is kept: a
    compiling body once proved it, and one more failure of its unit is not
    evidence against it. ``shown`` is the text of the prompt the failed
    attempt answered: a lesson that was not in it was not followed, and its
    unit's failure says nothing about it."""
    codes = frozenset(error_codes)
    seen = " ".join(shown.split())
    if not codes or not seen:
        return []
    out: list[str] = []
    for n in notes:
        prov = provenance.get(note_hash(n))
        if (prov is not None and prov.validated is not True
                and prov.unit == unit and codes & set(prov.error_codes)
                and " ".join(n.split())[:80] in seen):
            out.append(n)
    return out


def void_premise_lessons(
    notes: list[str],
    provenance: Mapping[str, NoteProvenance],
    *,
    declared_symbols: frozenset[str],
    writable_fns: frozenset[str],
) -> list[tuple[str, tuple[str, ...]]]:
    """``(note, voiding symbols)`` for every provenance-carrying lesson whose
    premise no longer holds — 204 N3, the premise-checked retirement.

    A lesson's premise is that its rejected symbols did not exist in the scope
    of the failed attempt. When EVERY rejected symbol now exists on the shown
    tree or sits in the current prompt's write list, the premise is void and
    the lesson is retirable ON DISK — the discipline the retro store received
    (a conviction by no test dies once a test blames the unit) and the floor
    received (the cursor trim), applied to the last claim-store without it.
    Legacy prose notes carry no provenance and are never retired here — N2 is
    the only sound rule for them (204.3)."""
    out: list[tuple[str, tuple[str, ...]]] = []
    known = declared_symbols | writable_fns
    for n in notes:
        prov = provenance.get(note_hash(n))
        if prov is None or not prov.rejected_symbols:
            continue
        if all(sym in known for sym in prov.rejected_symbols):
            out.append((n, tuple(sorted(prov.rejected_symbols))))
    return out


def rejected_symbols_in_errors(error_text: str) -> frozenset[str]:
    """Symbols the compiler REJECTED as nonexistent, from E0599/E0425/E0433
    diagnostics: the backtick-quoted name after ``named``/``find``. These are
    ground truth that the symbol is not available — any note recommending it
    is teaching a compile error."""
    out: set[str] = set()
    for raw in error_text.splitlines():
        line = raw.strip()
        if "error[E0599]" in line or "error[E0425]" in line or "error[E0433]" in line:
            for marker in (" named `", " find value `", " find function `",
                           " find type `", " find `"):
                if marker in line:
                    sym = line.split(marker, 1)[1].split("`", 1)[0]
                    if len(sym) >= 4:  # short names (new/get) false-hit notes
                        out.add(sym)
                    break
    return frozenset(out)


def found_types_in_errors(error_text: str) -> frozenset[str]:
    """Types rustc named as the ``found`` side of an E0308 mismatch.

    Missing-name codes are :func:`rejected_symbols_in_errors`. A type
    mismatch names the rejected approach as ``found `U` `` (and often a
    note ``found raw pointer `U` ``). A lesson that still prescribes U is
    teaching the mismatch: run ``235765-62f1505b`` fill 2's pair record
    kept honouring ``*const`` after rustc printed ``expected Ref, found
    `*const _` ``, because this parser did not exist and the missing-name
    parser does not read E0308."""
    out: set[str] = set()
    in_e0308 = False
    for raw in error_text.splitlines():
        line = raw.strip()
        if "error[E0308]" in line:
            in_e0308 = True
        elif line.startswith("error["):
            in_e0308 = False
        if not in_e0308:
            continue
        for marker in ("found raw pointer `", "found struct `",
                       "found enum `", "found reference `", "found `"):
            if marker in line:
                ty = line.split(marker, 1)[1].split("`", 1)[0].strip()
                if ty:
                    out.add(ty)
                break
    return frozenset(out)


def _found_type_needles(found: str) -> tuple[str, ...]:
    """Search tokens a lesson uses when it still prescribes ``found``.

    rustc writes ``*const _``; the lesson writes ``*const Holder``. The
    distinctive prefix is the match, not the placeholder. A raw-pointer
    found type also makes ``as_ptr`` a needle: that is the method that
    produces ``*const`` / ``*mut``, and the live pair lesson prescribed
    the call when the diagnostic named the pointer."""
    t = (found or "").strip()
    if not t:
        return ()
    needles = [t.lower()]
    low = t.lower()
    if low.startswith("*const"):
        needles.append("*const")
        needles.append("as_ptr")
    elif low.startswith("*mut"):
        needles.append("*mut")
        needles.append("as_ptr")
    return tuple(dict.fromkeys(needles))


def _note_recommends(low: str, token: str) -> bool:
    """True when ``low`` (already lowercased) prescribes ``token``, not
    merely mentions it as the thing to avoid."""
    if token not in low:
        return False
    if f"not {token}" in low and f"use {token}" not in low:
        return False
    if f"instead of {token}" in low and f"use {token}" not in low:
        return False
    if token.startswith("*"):
        # Type prescriptions: "receives a `*const T`", "treat this as *const".
        # "do not pass `*const T`" is the warning-against shape.
        return not ("do not" in low or "don't" in low)
    return (f"use {token}" in low or f"use `{token}" in low
            or f"always use {token}" in low or f"{token}()" in low
            or f"{token}(),' " in low or f"::{token}()" in low)


def contradicted_notes(notes: list[str], error_text: str) -> list[str]:
    """Notes whose advice recommends a symbol or type the compiler just
    rejected — the model's own compile feedback disproves them.

    Two diagnostic shapes, one retirement: E0599/E0425/E0433 name a
    missing symbol (``MaybeUninit::uninit_array()`` after rustc said that
    fn does not exist); E0308 names a ``found`` type the lesson still
    tells the next fill to pass. Mechanical validation by compiler
    evidence, not human curation."""
    rejected = set(rejected_symbols_in_errors(error_text))
    for ty in found_types_in_errors(error_text):
        rejected.update(_found_type_needles(ty))
    if not rejected:
        return []
    out: list[str] = []
    for note in notes:
        low = note.lower()
        for sym in rejected:
            if _note_recommends(low, sym.lower()):
                out.append(note)
                break
    return out
