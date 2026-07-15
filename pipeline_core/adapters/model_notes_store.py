"""Model self-correction notes store (adapter).

Owns the I/O and run-state side of the model-notes conduit: read/write the
``## Model Notes`` section of ``functional_requirements_{crate}.md``, dedup-append
new notes, rank them for a per-fn prompt, accumulate recurring rustc error codes
across the run, promote the frequent ones, and author a self-note via one LLM
call. All decision rules are pure and live in ``domain/model_notes.py``; this
class only supplies the filesystem, the run-state accumulators and the model.

The run accumulators (hash cache, error-code counts/excerpts) live ON the
instance — eliminating the former pipeline.py module globals (`_NOTE_HASH_CACHE`
/`_ERROR_CODE_COUNTS`/`_ERROR_CODE_EXCERPTS`) per the ClusterScope precedent.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pipeline_core.application.reconstruct.cargo_errors import (
    ecodes_in_text,
    excerpt_for_code,
)
from pipeline_core.domain import model_notes as mn
from pipeline_core.domain.llm_output import parse_self_note

_NOTE_EXTRACTION_PROMPT = """A model attempted to implement a Rust function and
the first attempt did not compile. The second (or later) attempt did compile.

Read the failed body, the cargo error from that failure, and the body that
finally compiled. Then write a SINGLE short note to your future self, in
first-person voice, that names the specific confusion and how to avoid it.

Format guidance:
  - Lead with what you tried that failed ("I tried X").
  - Name the reason it failed in concrete terms ("because Y").
  - State the correction as a rule ("Use Z, not X").
  - Keep it to ONE sentence; ≤300 chars.
  - Do NOT mention the function name, file name, or crate name — the note
    should generalise. Focus on the language/library/type confusion.
  - Output ONLY the note text, no preamble, no markdown, no quotes.

If no general lesson can be extracted (e.g. the failure was just a typo),
output the literal token NONE.

<FAILED_BODY>
{failed_body}
</FAILED_BODY>

<COMPILE_ERROR>
{error_excerpt}
</COMPILE_ERROR>

<FIXED_BODY>
{fixed_body}
</FIXED_BODY>
"""


class _AskClient(Protocol):
    def ask(self, prompt: str, system: str | None = None, model: str | None = None,
            max_tokens: int | None = None, think: bool | None = None,
            temperature: float | None = None) -> str: ...


class FileModelNotesStore:
    """FR-file-backed model-notes store with run-scoped recurring-error state."""

    def __init__(
        self,
        ask_client: _AskClient,
        model: str,
        log: Callable[[str], None],
        *,
        max_chars: int,
        max_entries: int,
        failed_body_cap: int,
        fix_err_cap: int,
        excerpt_max_per_code: int = 10,
        excerpt_max_chars: int = 600,
    ) -> None:
        self._ask = ask_client
        self._model = model
        self._log = log
        self._max_chars = max_chars
        self._max_entries = max_entries
        self._failed_body_cap = failed_body_cap
        self._fix_err_cap = fix_err_cap
        self._excerpt_max_per_code = excerpt_max_per_code
        self._excerpt_max_chars = excerpt_max_chars
        self._hash_cache: dict[Path, set[str]] = {}
        # Run-scoped recurring-error accumulators (P5.5 / P6.3).
        self.error_code_counts: Counter[str] = Counter()
        self.error_code_excerpts: dict[str, list[str]] = {}

    # -- persistence -------------------------------------------------------
    def load(self, fr_path: Path) -> list[str]:
        """The note bullets currently in FR (read file → domain parse)."""
        try:
            fr_text = fr_path.read_text(encoding="utf-8")
        except OSError:
            return []
        _, notes_section = mn.slice_model_notes(fr_text)
        return mn.parse_note_bullets(notes_section)

    def save(self, fr_path: Path, notes: list[str]) -> None:
        """Rewrite FR's ``## Model Notes`` section with ``notes`` (creating FR)."""
        try:
            fr_text = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""
        except OSError:
            fr_text = ""
        out = mn.render_with_notes(fr_text, notes)
        try:
            fr_path.parent.mkdir(parents=True, exist_ok=True)
            fr_path.write_text(out, encoding="utf-8")
        except OSError as e:
            self._log(f"  [model-notes] write failed: {e}")

    def append(
        self,
        fr_path: Path,
        note: str,
        error_codes: list[str] | set[str] | None = None,
    ) -> bool:
        """Append ``note`` to FR (after normalise + literal/semantic dedup +
        LRU trim). Returns True iff it was added."""
        prepared = mn.prepare_note(note, error_codes)
        if prepared is None:
            return False
        existing = self.load(fr_path)
        existing_hashes = self._hash_cache.get(fr_path)
        if existing_hashes is None:
            existing_hashes = {mn.note_hash(n) for n in existing}
            self._hash_cache[fr_path] = existing_hashes
        new_notes, added = mn.dedup_append(
            prepared, existing, existing_hashes, self._max_entries)
        if added:
            self.save(fr_path, new_notes)
        return added

    # -- prompt selection --------------------------------------------------
    def filter_for_prompt(
        self,
        fr_path: Path,
        file_rel: str,
        fn_name: str,
        file_text: str = "",
        current_error: str = "",
        max_chars: int | None = None,
    ) -> str:
        """Relevance-ranked ``<MODEL_NOTES>`` block for the current (file, fn)."""
        notes = [n for n in self.load(fr_path)
                 if not mn.is_bogus_regression_note(n)
                 and not mn.is_stale_regression_note(n, file_text)]
        err_codes = ecodes_in_text(current_error) if current_error else set()
        return mn.rank_notes(
            notes,
            file_rel=file_rel,
            fn_name=fn_name,
            file_text=file_text,
            current_error=current_error,
            err_codes=err_codes,
            max_chars=self._max_chars if max_chars is None else max_chars,
        )

    # -- recurring-error promotion ----------------------------------------
    def record_error_codes_seen(self, output: str) -> None:
        """Tally each unique E0xxx code in ``output`` and keep a capped excerpt
        per code for later type-token mining (P6.3)."""
        if not output:
            return
        for c in ecodes_in_text(output):
            self.error_code_counts[c] += 1
            excerpt = excerpt_for_code(output, c, max_chars=self._excerpt_max_chars)
            if excerpt:
                bucket = self.error_code_excerpts.setdefault(c, [])
                bucket.append(excerpt)
                if len(bucket) > self._excerpt_max_per_code:
                    bucket.pop(0)

    def promoted_error_rules(self, fr_path: Path | None = None) -> str:
        """RECURRING_ERRORS block for codes seen ``threshold``+ times this run."""
        stored = self.load(fr_path) if fr_path is not None else []
        return mn.build_promoted_rules(
            self.error_code_counts, self.error_code_excerpts, stored)

    # -- self-note authoring (one LLM call) -------------------------------
    def extract_self_note(
        self, failed_body: str, error_excerpt: str, fixed_body: str,
    ) -> str | None:
        """One LLM call to author a self-correction note. None when the model
        declines (responds NONE/empty) or inputs are insufficient."""
        if not failed_body or not error_excerpt:
            return None
        prompt = _NOTE_EXTRACTION_PROMPT.format(
            failed_body=failed_body[:self._failed_body_cap],
            error_excerpt=error_excerpt[:self._fix_err_cap],
            fixed_body=(fixed_body[:self._failed_body_cap] if fixed_body
                        else "(no fixed body — fix loop exhausted)"),
        )
        # Two-attempt ladder (default thinking, then think=False) — Qwen3.6
        # occasionally returns empty in one mode and not the other.
        resp = ""
        try:
            resp = self._ask.ask(prompt, model=self._model)
        except Exception as e:
            self._log(f"  [model-notes] extraction failed (default): {e}")
        if not resp or not resp.strip():
            try:
                resp = self._ask.ask(prompt, model=self._model, think=False)
            except Exception as e:
                self._log(f"  [model-notes] extraction failed (think=False): {e}")
                return None
        return parse_self_note(resp)


def write_fr_preserving_notes(path: Path | str, new_content: str) -> None:
    """Write FR content, preserving an existing `## Model Notes` section.
    Use at every TASK 1 write site so regenerating FR never wipes the
    model's accumulated self-corrections."""
    p = Path(path)
    old = p.read_text(encoding="utf-8") if p.exists() else None
    merged = mn.preserve_model_notes(old, new_content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(merged, encoding="utf-8")

