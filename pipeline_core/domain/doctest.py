"""Doc-test harvester (F10 extraction tier 1) — the free behaviours. Pure
domain: text in, behaviour VOs out. No IO (the adapter reads the file).

A Rust doc-comment code fence IS a Given/When/Then: ``let e = anyhow!("boom");
assert_eq!(e.to_string(), "boom");``. Harvesting them yields assertion-behaviours
with zero hallucination. Doc-tests compile as SEPARATE crates against the public
API only, so every harvested behaviour is black-box by construction (Hard Rule
11 safe).

Scanning is line-structured, never regex (Hard Rule): collect contiguous
``///`` / ``//!`` doc lines, strip the marker, then walk the markdown fences
inside them honoring the info-string (``no_run`` / ``ignore`` / ``should_panic``
/ ``compile_fail``). Rustdoc conventions are applied verbatim: a ``#``-prefixed
doc-test line is HIDDEN setup (kept, it runs) and the info-string defaults to a
runnable Rust block when empty.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    FenceDirective,
)
from pipeline_core.domain.layout_pins import classify_layout_assertions

_DOC_MARKERS = ("///", "//!")
_FENCE = "```"
# Info-string tokens that mean "this is not a runnable Rust block".
_NON_RUST_LANGS = frozenset({"text", "bash", "sh", "json", "toml", "console",
                             "ignore", "plain", "markdown", "md", "diff"})


@dataclass(frozen=True, slots=True)
class DocCodeBlock:
    """One fenced code block lifted from a doc-comment."""

    code: str                    # the block body (markers + leading # stripped)
    fence: FenceDirective
    is_rust: bool                # False for ```text / ```json / … non-Rust fences
    start_line: int              # 1-based source line of the opening fence


def _strip_doc_marker(line: str) -> str | None:
    """The doc text of a ``///``/``//!`` line, or None if not a doc line. One
    optional space after the marker is consumed (rustdoc convention)."""
    s = line.lstrip()
    for m in _DOC_MARKERS:
        if s.startswith(m):
            rest = s[len(m):]
            return rest[1:] if rest.startswith(" ") else rest
    return None


def _fence_directive(info: str) -> tuple[FenceDirective, bool]:
    """(directive, is_rust) from a fence info-string. Tokens are comma- or
    whitespace-separated (`rust,no_run`, `should_panic`)."""
    toks = [t.strip() for t in info.replace(",", " ").split() if t.strip()]
    lower = {t.lower() for t in toks}
    is_rust = not (lower & _NON_RUST_LANGS)
    if "ignore" in lower:
        return FenceDirective.IGNORE, is_rust
    if "compile_fail" in lower:
        return FenceDirective.COMPILE_FAIL, is_rust
    if "should_panic" in lower:
        return FenceDirective.SHOULD_PANIC, is_rust
    if "no_run" in lower:
        return FenceDirective.NO_RUN, is_rust
    return FenceDirective.RUN, is_rust


def _unhide(code_line: str) -> str:
    """Rustdoc hides setup lines that begin with ``# ``; they still compile+run,
    so we keep them but drop the hiding marker. A lone ``#`` is a blank line; a
    ``##`` escapes a literal leading ``#``."""
    if code_line.startswith("# "):
        return code_line[2:]
    if code_line == "#":
        return ""
    if code_line.startswith("##"):
        return code_line[1:]
    return code_line


def extract_doc_code_blocks(source: str) -> list[DocCodeBlock]:
    """Every fenced code block in `source`'s doc-comments, fences honored."""
    blocks: list[DocCodeBlock] = []
    in_fence = False
    fence_info = ""
    fence_start = 0
    buf: list[str] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        doc = _strip_doc_marker(raw)
        if doc is None:
            # A gap in the doc-comment run closes any open fence defensively.
            if in_fence:
                in_fence = False
                buf = []
            continue
        stripped = doc.strip()
        if stripped.startswith(_FENCE):
            if not in_fence:
                in_fence = True
                fence_info = stripped[len(_FENCE):].strip()
                fence_start = lineno
                buf = []
            else:
                directive, is_rust = _fence_directive(fence_info)
                blocks.append(DocCodeBlock(
                    code="\n".join(buf), fence=directive,
                    is_rust=is_rust, start_line=fence_start))
                in_fence = False
                buf = []
            continue
        if in_fence:
            buf.append(_unhide(doc))
    return blocks


def harvest_doctest_behaviours(
    source: str, *, source_file: str, cfg_cluster: str = "default",
) -> list[BehaviourSpec]:
    """Assertion-behaviours from `source`'s Rust doc-tests. Non-Rust fences and
    empty blocks are skipped. Each runnable Rust block becomes one
    assertion-behaviour (its own ``assert!`` is the Then); the exercised unit
    set is filled by the caller (return-type + construction scan) — left empty
    here to keep the extractor pure."""
    out: list[BehaviourSpec] = []
    n = 0
    for blk in extract_doc_code_blocks(source):
        if not blk.is_rust or not blk.code.strip():
            continue
        n += 1
        bid = f"doctest::{source_file}::{blk.start_line}"
        out.append(BehaviourSpec(
            behaviour_id=bid, given="", when=blk.code, then=None,
            provenance=BehaviourProvenance.HARVESTED,
            subkind=BehaviourSubKind.ASSERTION, fence=blk.fence,
            cfg_cluster=cfg_cluster, source_ref=f"{source_file}:{blk.start_line}"))
    return out


def harvest_static_assertions(
    source: str, *, source_file: str, cfg_cluster: str = "default",
) -> list[BehaviourSpec]:
    """Layout-pinning STATIC_ASSERTION behaviours from doc-tests — the pins F11's
    t1.5 must satisfy. One outcome-behaviour per distinct static assertion."""
    out: list[BehaviourSpec] = []
    for blk in extract_doc_code_blocks(source):
        if not blk.is_rust:
            continue
        for i, outcome in enumerate(classify_layout_assertions(blk.code)):
            out.append(BehaviourSpec(
                behaviour_id=f"static::{source_file}::{blk.start_line}::{i}",
                given="", when=blk.code, then=outcome,
                provenance=BehaviourProvenance.HARVESTED,
                subkind=BehaviourSubKind.OUTCOME, fence=blk.fence,
                cfg_cluster=cfg_cluster,
                source_ref=f"{source_file}:{blk.start_line}"))
    return out
