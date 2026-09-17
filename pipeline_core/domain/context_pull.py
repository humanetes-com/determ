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
    """One parsed request. `from_line` (1-based) is where the answer starts:
    the address a cut-off answer hands back so the rest of the artifact is
    reachable, rather than the same head served again."""

    kind: PullKind
    arg: str
    from_line: int = 1

    def tag(self) -> str:
        start = f' from_line="{self.from_line}"' if self.from_line > 1 else ""
        return f'<REQUEST kind="{self.kind}" arg="{self.arg}"{start}/>'

    def continued_at(self, line: int) -> ContextPull:
        return ContextPull(self.kind, self.arg, line)


@dataclass(frozen=True, slots=True)
class PullAnswer:
    """The resolver's reply. `ok` False means the request was legal but not
    answerable (unknown path/type); `payload` then carries the reason.

    `first_line`/`last_line`/`total_lines` describe the window served (0 when
    nothing was windowed). A truncated answer's continuation is
    `pull.continued_at(last_line + 1)`."""

    pull: ContextPull
    payload: str
    ok: bool = True
    truncated: bool = False
    first_line: int = 0
    last_line: int = 0
    total_lines: int = 0

    def continuation(self) -> ContextPull | None:
        if not (self.ok and self.truncated and self.last_line < self.total_lines):
            return None
        return self.pull.continued_at(self.last_line + 1)


@dataclass(frozen=True, slots=True)
class LineWindow:
    """A line-aligned slice of an artifact under a char cap."""

    text: str
    first_line: int
    last_line: int
    total_lines: int
    #: The last line served was itself cut at the cap (one line longer than
    #: the whole cap); its remainder has no line address.
    cut_inside: bool = False

    @property
    def truncated(self) -> bool:
        return self.cut_inside or self.last_line < self.total_lines


def window_lines(text: str, from_line: int, cap: int) -> LineWindow | None:
    """Lines `from_line`.. of `text`, as many whole lines as fit in `cap`
    chars (at least one — a single line longer than the cap is cut inside it).
    None when `from_line` is past the end of a non-empty text.

    Why whole lines and a start address: the cap used to be a head cut,
    `text[:cap]`, with a `truncated` flag and no way to ask for what followed.
    Run `603120-24aabfc5` (2026-09-16): a slice session asked twice for a
    55,930-char file and was served the same first 40,000 chars both times;
    the code it was asking about sat past the cut, and the two identical
    answers spent the transcript bound that ended the session."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(1, from_line)
    if total == 0:
        return LineWindow("", 0, 0, 0) if start == 1 else None
    if start > total:
        return None
    out: list[str] = []
    used = 0
    last = start - 1
    for idx in range(start - 1, total):
        ln = lines[idx]
        if out and used + len(ln) > cap:
            break
        if not out and len(ln) > cap:
            return LineWindow(ln[:cap], start, idx + 1, total, cut_inside=True)
        out.append(ln)
        used += len(ln)
        last = idx + 1
    return LineWindow("".join(out), start, last, total)


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
    start_s = (_attr(tag, "from_line") or "").strip()
    start = int(start_s) if start_s.isdigit() and int(start_s) >= 1 else 1
    return ContextPull(kind, arg, start), nxt


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
