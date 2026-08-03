"""The bounded context-pull protocol — domain layer.

The pipeline PUSHES heuristic context slices at the model; when the slice
misses the one fact the fn needs, the model hallucinates (starvation-miner
causes `symbol_absent`/`type_def_absent`). The pull protocol inverts that for
ONE bounded round-trip at a time: instead of a body, the model may reply with
a typed request tag; the pipeline answers with exactly that artifact and
re-asks. Closed kind menu, hard budget — NOT an agent loop.

Every kind must be answerable from what the reconstruction is allowed to see:
the workspace tree, the tests, the baked type records and cargo's own output.
The upstream crate source is never one of them — a pull that could reach it
would hand the model the answer it is being measured on. That is enforced
structurally, by the resolver's workspace-rooted reader, not by policy.

This module is pure: the VO set, the budget arithmetic, and the structured
`<REQUEST …/>` parser (attribute walk, no regex — `extract_fn_bodies` style).
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from enum import StrEnum


class PullKind(StrEnum):
    """The closed set of requests the model may make."""

    FILE = "file"                      # arg: workspace-relative .rs path
    TEST_SOURCE = "test_source"        # arg: test fn name
    TYPE_DEF = "type_def"              # arg: type name (baked records)
    FULL_ERROR = "full_error"          # no arg: last cargo output, whole
    MACRO_EXPANSION = "macro_expansion"  # arg: file path (cargo expand)


@dataclass(frozen=True, slots=True)
class ContextPull:
    """One parsed request."""

    kind: PullKind
    arg: str

    def tag(self) -> str:
        return f'<REQUEST kind="{self.kind}" arg="{self.arg}"/>'


@dataclass(frozen=True, slots=True)
class PullAnswer:
    """The resolver's reply. `ok` False means the request was legal but not
    answerable (unknown path/type); `payload` then carries the reason."""

    pull: ContextPull
    payload: str
    ok: bool = True
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PullBudget:
    """Hard cap on round-trips per fill. `spend()` returns the reduced budget
    or None when exhausted — an over-budget state is unrepresentable."""

    remaining: int = 2

    def spend(self) -> PullBudget | None:
        return PullBudget(self.remaining - 1) if self.remaining > 0 else None


def _attr(tag: str, name: str) -> str | None:
    key = f'{name}="'
    a = tag.find(key)
    if a == -1:
        return None
    b = tag.find('"', a + len(key))
    if b == -1:
        return None
    # Models emit XML-safe attribute values (`arg="Own&lt;T&gt;"`) — decode
    # them like the FN-block parser does.
    return html.unescape(tag[a + len(key):b])


def _parse_one(response: str, i: int) -> tuple[ContextPull | None, int]:
    """Parse the tag opening at `i`; return it (or None if malformed) and the
    offset to resume scanning from."""
    end = response.find(">", i)
    if end == -1:
        return None, len(response)
    tag = response[i:end]
    nxt = end + 1
    kind_s = _attr(tag, "kind")
    if kind_s is None:
        return None, nxt
    try:
        kind = PullKind(kind_s.strip())
    except ValueError:
        return None, nxt
    arg = (_attr(tag, "arg") or "").strip()
    if kind is not PullKind.FULL_ERROR and not arg:
        return None, nxt
    return ContextPull(kind, arg), nxt


def extract_context_pulls(response: str) -> tuple[ContextPull, ...]:
    """EVERY well-formed `<REQUEST kind="…" arg="…"/>` in `response`, in
    document order, first occurrence of each (kind, arg) kept.

    A reply may legitimately name more than one artifact. On 2026-08-03 at
    11:53 UTC the nightly src/error.rs file-first fill replied
    `<REQUEST kind="file" arg="src/ext.rs"/><REQUEST kind="test_source"
    arg="test_root_cause"/>` — a single reasonable batch. Only the second was
    answered, the first was dropped without a log line, and the model spent
    two further round-trips asking for other things before the budget ran out
    with no body written. Reading them all costs one parse and turns that walk
    into one exchange.

    Malformed tags (unknown kind, argless non-`full_error`, no closing `>`)
    are skipped rather than ending the scan — a stray `<REQUEST` in prose must
    not hide a well-formed request that follows it."""
    out: list[ContextPull] = []
    seen: set[ContextPull] = set()
    i = response.find("<REQUEST")
    while i != -1:
        pull, i = _parse_one(response, i)
        if pull is not None and pull not in seen:
            seen.add(pull)
            out.append(pull)
        i = response.find("<REQUEST", i)
    return tuple(out)


def extract_context_pull(response: str) -> ContextPull | None:
    """The LAST well-formed `<REQUEST kind="…" arg="…"/>` in `response`, or
    None. Unknown kinds and argless non-`full_error` requests are ignored
    (malformed output must never crash a fill)."""
    pulls = extract_context_pulls(response)
    return pulls[-1] if pulls else None
