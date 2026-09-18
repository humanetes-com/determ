"""Section-aware context guard — pure domain.

When a prompt overflows the model's context window, drop whole low-priority
SECTIONS (identified by their delimiter markers) before falling back to blind
middle-trimming — so we drop a complete integration-test file rather than
cutting a source file in half. Drop order (reverse priority):

  1. ``<TESTFILE …>`` blocks (integration tests — lowest priority)
  2. ``// --- filename ---`` blocks (sibling source files)
  3. middle ``<FILE …>`` blocks (keep first and last — the target)
  4. fall back to head/tail middle-trim

Pure (stdlib + the domain.text span scanners, no I/O, no regex): given the
prompt + system length + the char limit, return the trimmed prompt plus the
guard events the caller should log. The transport/telemetry lives in the
``adapters.llm`` client.
"""

from __future__ import annotations

from dataclasses import dataclass

from .text import sibling_comment_block_spans, tagged_path_block_spans

_MARGIN = 1_000  # small safety buffer only — prompt builders handle their own budgets


@dataclass(frozen=True, slots=True)
class FittedPrompt:
    """The trimmed prompt + the (tag, message) guard notes to log."""

    prompt: str
    events: tuple[tuple[str, str], ...]


def fit_prompt(prompt: str, sys_len: int, *, context_chars: int) -> FittedPrompt:
    """Trim `prompt` (given a `sys_len`-char system prefix) to fit `context_chars`.
    Returns the original prompt with no events when it already fits."""
    total = sys_len + len(prompt)
    if total <= context_chars - _MARGIN:
        return FittedPrompt(prompt, ())

    head_keep = context_chars // 4
    tail_keep = context_chars // 8
    budget = context_chars - _MARGIN - sys_len
    events: list[tuple[str, str]] = []

    def _fits(p: str) -> bool:
        return sys_len + len(p) <= context_chars - _MARGIN

    trimmed = prompt

    # Pass 1: drop <TESTFILE> blocks (whole blocks), last to first.
    if not _fits(trimmed):
        for s, e in reversed(tagged_path_block_spans(trimmed, "TESTFILE")):
            trimmed = trimmed[:s] + trimmed[e:]
            if _fits(trimmed):
                break

    # Pass 2: drop `// --- sibling_file ---\n...` blocks (end to start).
    if not _fits(trimmed):
        for s, e in reversed(sibling_comment_block_spans(trimmed)):
            trimmed = trimmed[:s] + trimmed[e:]
            if _fits(trimmed):
                break

    # Pass 3: drop middle <FILE> blocks (keep first and last, drop intermediates).
    if not _fits(trimmed):
        file_blocks = tagged_path_block_spans(trimmed, "FILE")
        for s, e in file_blocks[1:-1]:
            trimmed = trimmed[:s] + trimmed[e:]
            if _fits(trimmed):
                break

    if _fits(trimmed):
        dropped_chars = len(prompt) - len(trimmed)
        events.append((
            "context-guard/section_removal",
            f"dropped {dropped_chars:,} chars via section removal "
            f"(prompt was {total:,} chars, limit {context_chars:,})",
        ))
        return FittedPrompt(trimmed, tuple(events))

    # Fall back to middle-trimming.
    if budget < head_keep + tail_keep + 200:
        budget = max(200, budget)
        events.append((
            "context-guard/severe",
            f"only {budget:,} chars available — hard-truncating prompt",
        ))
        return FittedPrompt(trimmed[:budget], tuple(events))

    head = trimmed[:head_keep]
    tail = trimmed[max(head_keep, len(trimmed) - tail_keep):]
    dropped = len(trimmed) - head_keep - len(tail)
    final = (
        head
        + f"\n\n[... {dropped:,} chars trimmed by context-guard to fit "
        f"{context_chars:,}-char context window ...]\n\n"
        + tail
    )
    events.append((
        "context-guard/middle_trim",
        f"prompt {total:,} > effective limit {context_chars - _MARGIN:,} "
        f"(context={context_chars:,} margin={_MARGIN:,}): dropped {dropped:,} chars from middle",
    ))
    return FittedPrompt(final, tuple(events))
