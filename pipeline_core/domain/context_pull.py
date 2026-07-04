"""The bounded context-pull protocol (july_plan P2) — domain layer.

The pipeline PUSHES heuristic context slices at the model; when the slice
misses the one fact the fn needs, the model hallucinates (starvation-miner
causes `symbol_absent`/`type_def_absent`). The pull protocol inverts that for
ONE bounded round-trip at a time: instead of a body, the model may reply with
a typed request tag; the pipeline answers with exactly that artifact and
re-asks. Closed kind menu, hard budget — NOT an agent loop.

Hard Rule 11: the kinds are answerable ONLY from post-strip-legal sources
(reconstruction workspace, tests, baked type records, cargo output). Original
source is structurally unreachable — enforcement lives in the resolver's
workspace-rooted reader.

This module is pure: the VO set, the budget arithmetic, and the structured
`<REQUEST …/>` parser (attribute walk, no regex — `extract_fn_bodies` style).
"""
from __future__ import annotations

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
    return tag[a + len(key):b]


def extract_context_pull(response: str) -> ContextPull | None:
    """The LAST well-formed `<REQUEST kind="…" arg="…"/>` in `response`, or
    None. Unknown kinds and argless non-`full_error` requests are ignored
    (malformed output must never crash a fill)."""
    i = response.rfind("<REQUEST")
    if i == -1:
        return None
    end = response.find(">", i)
    if end == -1:
        return None
    tag = response[i:end]
    kind_s = _attr(tag, "kind")
    if kind_s is None:
        return None
    try:
        kind = PullKind(kind_s.strip())
    except ValueError:
        return None
    arg = (_attr(tag, "arg") or "").strip()
    if kind is not PullKind.FULL_ERROR and not arg:
        return None
    return ContextPull(kind, arg)
