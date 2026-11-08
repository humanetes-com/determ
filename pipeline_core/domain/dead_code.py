"""Which remaining stubs the finished tree itself can never reach (pure domain).

A TDD loop fills what a red test reaches. A private helper the reconstructed
bodies never call is reached by nothing: no test can enter it, no fill of it
could be judged, and the crate behaves the same whatever its body is. Until
2026-10-02 the end gate could only ask the ORIGINAL's execution record about
such a stub, and "the original's suite entered this body" says nothing about
the reconstruction's call graph. Run `292191-d60a5308`: 401 tests passed and
the run exited 1 on `src/nightly.rs::request_ref_backtrace` and
`::provide_ref_backtrace`; the only text naming the first was a macro no body
expands, and the only caller of the second sat under a cfg no cluster sets.

rustc already computes the answer: its dead-code analysis reports every item
no path from the crate's public surface uses. This module reads that analysis
— the spans of its `dead_code` diagnostics, taken per cluster world by the
adapter — and joins them to the stub records by byte range: a diagnostic whose
span starts inside a fn's signature names that fn, however many same-named
siblings its file holds.

**Dead in every world that compiles it, or not dead.** A stub one world
reaches is a hole in that world. So a stub leaves the gate only when at least
one baked world compiles it and every world that compiles it measured it dead
(`domain.stub_removal.unreached_stub_proofs`, the one rule the gate reads).
A world whose analysis did not run (the tree does not build there) measured
nothing, and nothing it compiles is excused.

**What the lint never reports.** A method of a trait impl: the impl is live
as soon as its trait and its self type are. Run `3323407-984b07f2`
(2026-11-08) exited 1 on such a stub nothing could call. Those stubs are
decided by removal in `domain.stub_removal`, which reads this module's dead
set for the callers.

**What the reading cannot see.** A dead stub is not proof that no behaviour
is missing — the call path that used to reach it may be what was lost — which
is why the gate keeps the original's execution record beside this reading and
says so. The adapter reads the library and its test profile, so an in-crate
test module counts as a caller.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from pipeline_core.domain.models import FnRecord

# `(file, byte offset)` where one dead-code diagnostic's span starts; `file`
# is relative to the crate root, as rustc prints it and as the extractor keys
# its records when rooted at the same directory.
DeadSpan = tuple[str, int]

_LINT = "dead_code"


@dataclass(frozen=True, slots=True)
class WorldDeadCode:
    """One cluster world's dead-code reading of the tree, or why it has none."""

    cluster: str
    spans: frozenset[DeadSpan] = frozenset()
    reason: str = ""

    @property
    def measured(self) -> bool:
        return not self.reason


def dead_code_spans(cargo_json: str) -> frozenset[DeadSpan]:
    """Every span of every `dead_code` diagnostic in a `cargo check
    --message-format=json` stream.

    ALL spans, not only the primary: one diagnostic about several associated
    items (`associated items `a`, `b` are never used`) puts its primary span
    on the impl and one further span on each item."""
    out: set[DeadSpan] = set()
    for line in cargo_json.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("reason") != "compiler-message":
            continue
        diag = msg.get("message")
        if not isinstance(diag, dict):
            continue
        code = diag.get("code")
        if not isinstance(code, dict) or code.get("code") != _LINT:
            continue
        for span in diag.get("spans") or ():
            if not isinstance(span, dict):
                continue
            name, start = span.get("file_name"), span.get("byte_start")
            if isinstance(name, str) and isinstance(start, int):
                out.add((name, start))
    return frozenset(out)


def names_record(rec: FnRecord, spans: frozenset[DeadSpan]) -> bool:
    """A dead-code span starts inside `rec`'s signature: the lint names it."""
    return any(f == rec.file and rec.sig_byte_start <= b < rec.body_byte_start
               for f, b in spans)
