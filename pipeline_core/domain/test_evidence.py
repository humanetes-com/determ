"""What a generated test block may carry back into the next prompt (pure).

Three places paste a committed test block into later prompts: the attempt
history ("Test signatures"), the reuse section ("Patterns that achieved
coverage improvement … reuse these constructs") and the retrospective
("Previous test block (already committed)"). Each pasted the model's own
comments with the code. `tests/wrapper_gen_3.rs` on anyhow opened with
"`anyhow!("...")` … create an Error whose inner object is a DisplayError",
which is false (`anyhow!` builds a `MessageError`), and every later wrapper.rs
prompt carried that sentence next to "reuse these constructs".

Two rules, one class (a prompt states as fact what nothing measured):
- the model's comment lines leave the pasted code, and the paste says so;
- a committed block aimed at a function that the next measurement still
  counts at 0 is stated as exactly that — the test file ran and the function
  has 0 executions. Not why: a block that called the right constructor and
  rendered with `{:?}` instead of `{:#?}` would be told a false cause.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

REMOVED_NOTE = ("// (the generation model's own comments are removed from this paste: "
                "they are its claims, not measured facts)")


def without_model_comments(rust: str) -> str:
    """`rust` without whole-line `//` comments (doc comments included), with
    one line saying so when any was removed. Code and attributes are kept."""
    kept: list[str] = []
    removed = False
    for line in rust.splitlines():
        if line.strip().startswith("//"):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return rust
    return "\n".join([REMOVED_NOTE, *kept])


def still_unexecuted(committed_for: Mapping[str, Sequence[str]], labels: Iterable[str]) -> str:
    """One line per still-unexecuted function a committed block was aimed at:
    which test files ran, and that the function has 0 executions."""
    lines = []
    for label in labels:
        files = list(dict.fromkeys(committed_for.get(label, ())))
        if files:
            ran = ", ".join(f"`{f}`" for f in files)
            lines.append(f"- {ran} ran; `{label}` still has 0 executions.")
    if not lines:
        return ""
    return ("Measured on this tree after committed blocks aimed at these functions "
            "(a fact, not a hypothesis):\n" + "\n".join(lines) + "\n")
