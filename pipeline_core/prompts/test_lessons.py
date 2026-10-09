"""The task 4 per-test lesson prompt (`application/reconstruct/test_lessons`)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["build_test_lesson_prompt"]


def build_test_lesson_prompt(*, test: str, entry: Mapping[str, Any],
                             test_source: str, words: str,
                             bodies: Mapping[str, str],
                             allowed: Sequence[str]) -> str:
    """One red test: what the fill tried for it (its red-ledger entry), its
    source, what it printed, and the current bodies of the functions it runs
    through. The reply is lessons aimed at named functions."""
    facts = "\n".join(
        f"  {k}: {v}" for k, v in sorted(entry.items())
        if k not in ("test",) and v not in ("", [], {}, None))
    body_text = "\n\n".join(
        f"// {u}\n{b}" for u, b in bodies.items() if b) or "(no body read)"
    return f"""A reconstruction of a Rust crate leaves this test failing after a
whole fill phase. Each function below was written by a model that saw the
test's failure. Your job is NOT to write code. Write what the next writer of
these functions must do differently, so the test passes.

TEST: {test}

WHAT THE FILL RECORDED FOR THIS TEST (its red-ledger entry):
{facts or "  (nothing recorded)"}

WHAT THE TEST PRINTED ON THE LAST SWEEP:
{words or "(no output recorded)"}

THE TEST'S SOURCE:
{test_source or "(source not found)"}

THE CURRENT BODIES OF THE FUNCTIONS IT RUNS THROUGH:
{body_text}

Functions you may name (the test's executed slice and failure site): {", ".join(allowed)}

Reply with one or more blocks, each about ONE of those functions:

<LESSON fn="src/file.rs::function_name">
What the current body gets wrong about this test, stated against the test's
assertion and the value it printed, and what the next body must do instead.
Concrete: name the observable, the expected value, the call or format that
produces it. No code blocks.
</LESSON>

Name a function only when the test's evidence implicates it. If the evidence
implicates none of them, reply with no block."""
