"""Anti-leakage system note prepended to every LLM call.

Local models often 'know' a popular crate (anyhow, serde, …) and silently
reconstruct its real internals from memory. That defeats tests-only
reconstruction. Stated on every call so any code-viewing step is reminded the
bytes it sees are ours, not upstream's.
"""

from __future__ import annotations

__all__ = ["from_scratch_disclaimer"]


def from_scratch_disclaimer(crate_display_name: str) -> str:
    name = crate_display_name or "this crate"
    return (
        f"This project reuses the name `{name}`, but the implementation has been written "
        f"entirely from scratch and is COMPLETELY DIFFERENT from any public library known as "
        f"`{name}`. Do NOT assume types, signatures, internal structure, or behavior from the "
        f"upstream `{name}`. Reason ONLY from the code, signatures, and tests shown to you; "
        f"the structure of names and modules may differ from what you remember."
    )
