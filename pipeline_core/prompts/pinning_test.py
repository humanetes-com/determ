"""The end gate's pinning-test prompt (`application/pinning_tests`)."""
from __future__ import annotations

from collections.abc import Sequence

__all__ = ["build_pinning_test_prompt"]


def build_pinning_test_prompt(*, crate: str, world: str, world_flags: str,
                              body: str, signature: str,
                              routes: Sequence[str], api_surface: str,
                              example_header: str,
                              previous: Sequence[tuple[str, str]]) -> str:
    """One blocking body: where it sits, the public calls that reach it, the
    crate's public API, the header shape of a test file in this build world,
    and every earlier candidate with the reason it was refused. The reply is
    one test file."""
    route_text = "\n".join(routes) or ("(no public route was found in the "
                                       "crate's MIR; reach it through the public "
                                       "API that builds or exposes its type)")
    tries = "\n\n".join(
        f"CANDIDATE {i + 1} (refused: {why}):\n```rust\n{text.strip()}\n```"
        for i, (text, why) in enumerate(previous))
    earlier = ("\nEARLIER CANDIDATES FOR THIS BODY, AND WHY EACH WAS REFUSED:\n\n"
               + tries + "\n") if tries else ""
    return f"""The Rust crate `{crate}` is being rebuilt from its tests. Every test
passes, yet one body that the crate's public API reaches is still
`unimplemented!()`, because no test reaches it. Write ONE integration test file
(it goes in `tests/`) whose tests reach that body through the public API.

THE BODY (file, function and the impl it belongs to):
{body}
{signature}

BUILD WORLD: `{world}` ({world_flags or "default flags"}). The file is compiled
only in this world; the pipeline adds the line that gates it.

PUBLIC CALLS THAT REACH IT (read from the original crate's compiled MIR):
{route_text}

THE CRATE'S PUBLIC API:
{api_surface or "(not available)"}

THE HEADER OF A TEST FILE THAT BUILDS IN THIS WORLD (follow its crate-level
attributes and imports; do not copy its tests):
{example_header or "(no example in this world)"}

How the file is judged, and both must hold:
1. It is run against the ORIGINAL crate: every test in it must build and pass.
   Assert only what the public API documents or what the call must do; a
   guessed value fails here.
2. It is run against the rebuilt crate: at least one test must fail with its
   first panic inside the body above (the `unimplemented!()`), because the
   test's calls reach it.

Rules: public API only (`use {crate}::...;` and the standard library). No
`#[ignore]`, no `#[should_panic]`, no `catch_unwind`. Each test asserts an
observable result of the call that reaches the body.
{earlier}
Reply with exactly one ```rust fenced block holding the whole file and nothing
else."""
