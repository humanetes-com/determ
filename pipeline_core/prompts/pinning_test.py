"""The end gate's pinning-test prompt (`application/pinning_tests`)."""
from __future__ import annotations

from collections.abc import Sequence

from pipeline_core.domain.pinning_test import PIN_CRITERIA, repair_of

__all__ = ["build_pinning_test_prompt", "judging_contract"]


def judging_contract() -> str:
    """The judge's checks, in the order it applies them, as the writer reads
    them. Rendered from `PIN_CRITERIA`, the list every refusal belongs to, so
    the prompt states every check the judge can refuse on: a two-check
    contract under a three-check judge drew six candidates that discarded
    what the body hands back (run `3991945-1b7ae3e9`, 2026-10-19)."""
    lines = [f"How the file is judged; all {len(PIN_CRITERIA)} checks must hold, "
             "in this order:"]
    lines += [f"{i}. {c.statement}" for i, c in enumerate(PIN_CRITERIA, 1)]
    return "\n".join(lines)


def build_pinning_test_prompt(*, crate: str, world: str, world_flags: str,
                              body: str, signature: str,
                              routes: Sequence[str], api_surface: str,
                              example_header: str,
                              blocking: Sequence[str],
                              previous: Sequence[tuple[str, str, str]]) -> str:
    """One blocking body: where it sits, the public calls that reach it, the
    crate's public API, the header shape of a test file in this build world,
    the world's blocking set (the judge empties all of it), and every earlier
    candidate written for any body of that set, as (written for, file,
    reason refused). The judge decides on the set, so a refusal reached
    while asking for one body is evidence for its siblings. The reply is
    one test file.

    When this body's latest attempt did not compile (`repair_of`), the
    prompt is that file and its diagnostics, not a request for a new file.
    A fresh request is how a compile failure was answered with a different
    file on run `4184066-ce008393` (2026-10-19)."""
    repair = repair_of(body, previous)
    if repair is not None:
        return _repair_prompt(crate=crate, body=body, signature=signature,
                              file=repair[0], diagnostics=repair[1])
    route_text = "\n".join(routes) or ("(no public route was found in the "
                                       "crate's MIR; reach it through the public "
                                       "API that builds or exposes its type)")
    blocking_text = "\n".join(f"- {b}" for b in blocking) or f"- {body}"
    tries = "\n\n".join(
        f"CANDIDATE {i + 1}, written for {site} (refused: {why}):\n"
        f"```rust\n{text.strip()}\n```"
        for i, (site, text, why) in enumerate(previous))
    earlier = ("\nEARLIER CANDIDATES FOR THE BLOCKING BODIES OF THIS WORLD, AND WHY "
               "EACH WAS REFUSED:\n\n" + tries + "\n") if tries else ""
    return f"""The Rust crate `{crate}` is being rebuilt from its tests. Every test
passes, yet one body that the crate's public API reaches is still
`unimplemented!()`, because no test reaches it. Write ONE integration test file
(it goes in `tests/`) whose tests reach that body through the public API and
assert on what it does.

THE BODY (file, function and the impl it belongs to):
{body}
{signature}

EVERY BLOCKING BODY OF THIS BUILD WORLD (all still `unimplemented!()`; check 4
below empties all of them):
{blocking_text}

BUILD WORLD: `{world}` ({world_flags or "default flags"}). The file is compiled
only in this world; the pipeline adds the line that gates it.

PUBLIC CALLS THAT REACH IT (read from the original crate's compiled MIR):
{route_text}

THE CRATE'S PUBLIC API:
{api_surface or "(not available)"}

THE HEADER OF A TEST FILE THAT BUILDS IN THIS WORLD (follow its crate-level
attributes and imports; do not copy its tests):
{example_header or "(no example in this world)"}

{judging_contract()}

Rules: public API only (`use {crate}::...;` and the standard library). No
`catch_unwind`.
{earlier}
Reply with exactly one ```rust fenced block holding the whole file and nothing
else."""


def _repair_prompt(*, crate: str, body: str, signature: str,
                   file: str, diagnostics: str) -> str:
    """The attempt after a compile failure: the same file, the diagnostics,
    and the checks it still has to meet. Not a request to write another."""
    return f"""The file below did not compile, so it was not judged. Return that same file with every diagnostic below resolved. Do not write a different test.

THE BODY IT MUST STILL REACH:
{body}
{signature}

{judging_contract()}

Rules: public API only (`use {crate}::...;` and the standard library). No
`catch_unwind`.

THE FILE:
```rust
{file.strip()}
```

DIAGNOSTICS:
{diagnostics.rstrip()}

Reply with exactly one ```rust fenced block holding the whole file and nothing
else."""
