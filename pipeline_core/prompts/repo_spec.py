"""Repository Specifications — the crate's own spec-document set, injected
into EVERY code-producing prompt from TASK 2 onward.

The set (user-defined, 2026-06-11): functional requirements + genesis (prose
and structured rules) + API surface. Historically these were injected only by
the legacy whole-file Task2 generators; the active skeleton/TDD path produced
code without ever reading them, which broke the
iterate-on-documents learning loop. This module is the single renderer every
prompt path now shares.

Pure text→text: the pipeline loads the documents (I/O, caching) and the
builders call the settable facade in :mod:`pipeline_core.prompts.runtime`;
this module only selects and budgets. Relevance selection keeps the per-fn
prompts focused: FR paragraphs and API file-sections are included only when
they mention the target's identifiers, and every part is budget-capped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline_core.domain.prompt_holdings import FileView, file_block
from pipeline_core.domain.text import trim_to_line

_RUST_NOISE_WORDS = frozenset({
    "impl", "for", "where", "dyn", "mut", "ref", "pub", "the", "and", "src",
})


@dataclass(frozen=True, slots=True)
class RepoSpecDocs:
    """The Repository Specifications document set, as loaded text."""
    genesis_rules: str = ""    # structured rules (genesis_rules_read) — compact
    genesis: str = ""          # genesis.md prose (methodology)
    functional_req: str = ""   # functional_requirements_{module}.md (notes section stripped)
    api_surface: str = ""      # .original_structure/{module}.api.md

    def empty(self) -> bool:
        return not (self.genesis_rules or self.genesis
                    or self.functional_req or self.api_surface)


def identifiers_for_target(
    fn_name: str = "", container: str = "", file_rel: str = "",
) -> set[str]:
    """Relevance identifiers for a fill/fix target: the fn name, the type and
    trait names from its container header, and the file stem."""
    idents: set[str] = set()
    if len(fn_name) >= 3:
        idents.add(fn_name)
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else " " for ch in container)
    for tok in cleaned.split():
        if len(tok) >= 3 and tok not in _RUST_NOISE_WORDS and not tok.isdigit():
            idents.add(tok)
    stem = file_rel.rsplit("/", 1)[-1].split(".", 1)[0]
    if len(stem) >= 3:
        idents.add(stem)
    return idents


def _matching_paragraphs(doc: str, identifiers: set[str], cap: int) -> str:
    """Paragraphs of `doc` that mention any identifier, in document order,
    until `cap` chars."""
    if not doc or not identifiers or cap <= 0:
        return ""
    out: list[str] = []
    used = 0
    for para in doc.split("\n\n"):
        p = para.strip()
        if not p or not any(i in p for i in identifiers):
            continue
        if used + len(p) + 2 > cap:
            break
        out.append(p)
        used += len(p) + 2
    return "\n\n".join(out)


def _declares(body: str, ident: str) -> bool:
    """`body` (a file's API surface) declares `ident` as a type, or opens an
    impl block whose header names it — the file that answers "what can I do
    with an X", not one that merely passes an X along."""
    w = re.escape(ident)
    return re.search(
        rf"(?m)(?:\b(?:struct|enum|union|trait|type)\s+{w}\b"
        rf"|^\s*(?:unsafe\s+)?impl\b[^\n{{;]*\b{w}\b)", body) is not None


def _api_slice(
    api_surface: str, file_rel: str, identifiers: set[str], cap: int,
) -> str:
    """API-surface sections for files OTHER than the target file that mention
    any of the target's identifiers (cross-file ground truth — the target
    file's own signatures are already in the prompt's file view).

    Ranked before the cap is filled: files that DECLARE one of the
    identifiers (its type, or an impl of it) first, by how many; then by how
    many they mention; then in file order. The slice used to be filled in
    file order, so any early file that merely mentioned an identifier took
    the cap. Per-fn fills outside `src/error.rs` asked for that file 53 times
    from 2026-09-01 to 2026-09-24; recomputed 2026-09-24 on the live API
    surface at a 6000-char cap, file order gave `src/context.rs::fmt`
    `src/ensure.rs` and `src/fmt.rs::display` / `::debug` `src/context.rs`
    and `src/ensure.rs`, where this ranking gives `src/error.rs` — the file
    that declares `ContextError`'s and `ErrorImpl`'s impls."""
    if not api_surface or cap <= 0:
        return ""
    ranked: list[tuple[int, int, int, str, str]] = []
    for idx, chunk in enumerate(api_surface.split('<FILE path="')[1:]):
        path, sep, rest = chunk.partition('">')
        if not sep:
            continue
        body = rest.split("</FILE>")[0].rstrip()
        if file_rel and path.endswith(file_rel):
            continue
        stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
        mentioned = [i for i in identifiers if i in body]
        if not (stem in identifiers or mentioned):
            continue
        declared = sum(1 for i in identifiers if _declares(body, i)) + (
            1 if stem in identifiers else 0)
        ranked.append((-declared, -len(mentioned), idx, path, body))
    ranked.sort()
    out: list[str] = []
    used = 0
    for _d, _m, _i, path, body in ranked:
        # The API surface of another file: signatures and docs, never bodies.
        sect = file_block(path, body.lstrip("\n"), view=FileView.PART)
        if used + len(sect) > cap:
            sect = trim_to_line(sect, cap - used, "\n... (truncated)\n</FILE>")
            if len(sect) < 120:
                break
        out.append(sect)
        used += len(sect)
        if used >= cap:
            break
    return "\n".join(out)


def render_repo_spec(
    docs: RepoSpecDocs,
    *,
    file_rel: str = "",
    identifiers: set[str] | frozenset[str] = frozenset(),
    char_budget: int = 6000,
) -> str:
    """One REPOSITORY_SPECIFICATIONS body: build rules first (compact, always
    whole-doc trimmed), then relevance-selected FR excerpts, genesis prose,
    and the cross-file API slice — each consuming what the previous left of
    `char_budget`. Returns "" when nothing relevant fits."""
    if char_budget <= 0 or docs.empty():
        return ""
    idents = set(identifiers)
    parts: list[str] = []
    budget = char_budget

    def take(label: str, text: str, part_cap: int) -> None:
        nonlocal budget
        t = (text or "").strip()
        if not t or budget <= 80:
            return
        t = trim_to_line(t, min(part_cap, budget), "\n... (truncated)")
        parts.append(f"### {label}\n{t}")
        budget -= len(t)

    take("Build rules (genesis, project-independent)",
         docs.genesis_rules, char_budget // 4)
    take("Functional requirements (excerpts relevant to this target)",
         _matching_paragraphs(docs.functional_req, idents, char_budget),
         char_budget * 2 // 5)
    take("Genesis (methodology)", docs.genesis, char_budget // 5)
    take("API surface — other files (ground truth)",
         _api_slice(docs.api_surface, file_rel, idents, budget), budget)
    return "\n\n".join(parts)
