"""The computed part of `coverage_strategy.md` (pure).

`coverage_strategy.md` is injected into the augmentation, requirements and fill
prompts. Its `TYPES:` lines (how to build each type) and `IMPORTS:` `NEVER:`
lines (what not to import) were written by a model at t0b and never checked:
anyhow's file listed `MessageError` and `ContextError` with a construction,
had no `DisplayError` line, and a prompt elsewhere called `DisplayError`
importable (`use anyhow::DisplayError` -> E0432, 21 blocks on one file).

Both facts are in rustc's output (`domain.fn_routes`): which `pub` types the
public index does not list, and which public calls build one. This module
renders them as the first, marked part of the file and keeps the model's text
after it, minus every model line that names one of those types as importable
or says how to build it — the computed line is the statement about it. The
part is re-rendered, never edited: :func:`strip_computed` removes a previous
one, so rendering twice is rendering once.
"""
from __future__ import annotations

import hashlib
from collections.abc import Sequence

from pipeline_core.domain.fn_reachability import has_word
from pipeline_core.domain.fn_routes import ROUTES_VERSION, TypeFact, WorldFacts

BEGIN = "# --- computed from rustc's MIR and public index (not model-written; do not restate) ---"
END = "# --- end computed ---"


def strip_computed(text: str) -> str:
    """`text` without a previously rendered computed part."""
    start = text.find(BEGIN)
    if start < 0:
        return text
    end = text.find(END, start)
    if end < 0:
        return text[:start].rstrip() + "\n"
    return (text[:start] + text[end + len(END):].lstrip("\n")).strip() + "\n"


def _union(worlds: Sequence[WorldFacts]) -> list[tuple[TypeFact, tuple[str, ...]]]:
    """Each unnameable type once, from the first world that has it, with the
    worlds it is unnameable in (named only when that is not every world)."""
    seen: dict[str, tuple[TypeFact, list[str]]] = {}
    for w in worlds:
        for t in w.unnameable:
            if t.name in seen:
                seen[t.name][1].append(w.cluster)
            else:
                seen[t.name] = (t, [w.cluster])
    every = len(worlds)
    return [(t, tuple(cs) if len(cs) < every else ()) for t, cs in
            sorted(seen.values(), key=lambda v: (v[0].file, v[0].name))]


def computed_block(worlds: Sequence[WorldFacts]) -> str:
    """The computed part, or "" when the worlds name no unnameable type."""
    types = _union(worlds)
    if not types:
        return ""
    def tag(cs: tuple[str, ...]) -> str:
        return f"  [worlds: {', '.join(cs)}]" if cs else ""
    lines = [BEGIN, "IMPORTS (computed):"]
    lines += [f"  {t.never_line()}{tag(cs)}" for t, cs in types]
    lines.append("TYPES (computed):")
    lines += [f"  {t.types_line()}{tag(cs)}" for t, cs in types]
    lines.append(END)
    return "\n".join(lines) + "\n"


def computed_hash(worlds: Sequence[WorldFacts]) -> str:
    """What `coverage_strategy.inputs_hash` stores: the generator version and
    the computed part. A differing hash means the part must be re-rendered."""
    return hashlib.sha256((ROUTES_VERSION + "\n" + computed_block(worlds)).encode()).hexdigest()


def _section_of(line: str, current: str) -> str:
    head = line.strip().split(":", 1)[0].strip().upper()
    if line[:1] not in (" ", "\t") and line.rstrip().endswith(":") and head.isalpha():
        return head
    return current


def drop_contradicted(model_text: str, names: frozenset[str]) -> tuple[str, list[str]]:
    """`model_text` without its lines that state something about an
    unnameable type: an `IMPORTS`/`NEVER` line naming one, a `TYPES` line
    keyed on one. Returns `(text, dropped lines)`."""
    if not names:
        return model_text, []
    kept: list[str] = []
    dropped: list[str] = []
    section = ""
    for line in model_text.splitlines():
        section = _section_of(line, section)
        body = line.strip()
        named = any(has_word(body, n) for n in names)
        key = body.split(":", 1)[0].strip() if ":" in body else ""
        if named and not body.startswith("#") and (
                section in ("IMPORTS", "RULES") and ("use " in body or body.startswith("NEVER"))
                or section == "TYPES" and key in names):
            dropped.append(line)
            continue
        kept.append(line)
    return "\n".join(kept).strip() + "\n", dropped


def render_strategy(text: str, worlds: Sequence[WorldFacts]) -> tuple[str, list[str]]:
    """The strategy file with the computed part first and the model's text
    after it, minus the lines the computed part contradicts. Returns
    `(rendered, dropped model lines)`; with no worlds the text is unchanged."""
    if not worlds:
        return text, []
    model = strip_computed(text)
    names = frozenset(t.name for w in worlds for t in w.unnameable)
    body, dropped = drop_contradicted(model, names)
    block = computed_block(worlds)
    if not block:
        return body, dropped
    return block + "\n" + body, dropped
