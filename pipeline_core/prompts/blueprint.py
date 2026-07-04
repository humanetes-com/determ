"""Blueprint-generation prompt (july_plan P3). The model authors its OWN
implementation plan from tests + skeleton signatures + FR — top-down design
before any bottom-up fill. Charter: never steer toward what the original
library did; the model owns every representation choice.
"""
from __future__ import annotations


def build_blueprint_prompt(
    *,
    module: str,
    skeleton_sigs: str,
    tests_block: str,
    functional_req: str,
    topics: tuple[str, ...],
    missing: tuple[str, ...] = (),
) -> str:
    """One prompt → `blueprint_{crate}.md`. `topics` are the section subjects
    the document must cover (source files, dispatch families, macros);
    `missing` re-asks for gaps after a failed validation."""
    topic_lines = "\n".join(f"  - {t}" for t in topics)
    missing_part = ""
    if missing:
        missing_part = ("\nYour previous draft MISSED these required "
                        "sections — include them this time:\n"
                        + "\n".join(f"  - {m}" for m in missing) + "\n")
    return f"""You are about to implement the Rust crate `{module}` from its frozen
signatures and tests. BEFORE writing any code, write YOUR OWN implementation
blueprint: the design you will follow, so every function body you write later
agrees with one coherent scheme. This is YOUR plan — choose representations
freely; the only constraints are the frozen signatures and the tests below.

Format: markdown. Start with `## Overview` (the crate's collaboration story in
a few sentences), then ONE `## <topic>` section per topic:
{topic_lines}
{missing_part}
In each section state CONCRETELY, in 3-10 lines:
  - for a source file: what each type stores (fields you plan), how its fns
    collaborate, which invariants hold;
  - for a dispatch family / macro: the exact expansion or specialization
    scheme you will implement, with a short generic example (placeholder
    names, e.g. `struct Wrapper(Inner);` — never prose like "design X well").

Frozen skeleton signatures:
```rust
{skeleton_sigs}
```

Functional requirements:
{functional_req}

Tests (the behavior oracle — your design must make ALL of these pass):
{tests_block}

Output ONLY the markdown document. No code fences around the whole document,
no commentary before or after."""
