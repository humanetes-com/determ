"""The text of a failing test that its failure is ABOUT, for the questions
that ask whether that failure renders through a format trait.

WHY THIS EXISTS. `refill_stamp.format_traits_used` maps format tokens
(`{:?}`, `{}`, `.to_string()`) to the std formatting traits. Its callers in
the attribution hop, the futility-escalation withhold and the product
sustain all fed it the test's WHOLE source, while the question they ask is
narrower: does the observable this test FAILS on come out of a formatter.
Run `3076-051d385a` (2026-10-13, cluster `stable`, 148/151): a test failed
at `tests/test_boxed.rs:26:14`, `called Option::unwrap() on a None value`,
on the result of `downcast_ref::<…>()`. Its source also calls `.to_string()`
on line 21 (an assertion that passed; the probe observed the expected value)
and on line 27 (after the unwrap that panicked, never evaluated). The hop
attributed it to the Display formatter, recorded a Display pair (23.7 min,
reverted on four regressions), and the withhold dropped the producer
escalation that would have aimed the downcast path.

THE MODEL. A failure's observable is a backward slice of the test fn:

* the statement holding libtest's panic location (`line:col`), cut at the
  panicking call — through the end of the identifier the column names, or
  through the whole invocation when that identifier is a macro
  (`assert_eq!(…)` panics at its own first column);
* plus, transitively, every `let` in the same fn, before the statement that
  reads it, that binds an identifier the cut reads.

When the panic location is not inside the test fn (a panic inside the crate
under reconstruction, or in a helper fn), the first frame of the test's own
file printed in the evidence is the location; sweeps do not set
`RUST_BACKTRACE`, so usually there is none and the answer is None. The
caller then reads the whole source, which is the reading every consumer had
before this module, so ignorance never narrows anything.

WHAT IT DOES NOT DECIDE. `assertion_owned_by_container` asks a different
question (which same-named container the test's text picks), falls through to
a tie-of-zeros keep on an empty trait set, and keeps reading the whole
source. No trait or construct name appears here: the token-to-trait mapping
stays `format_traits_used`'s.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from pipeline_core.domain.rust_fn_headers import locate_all_fns
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.test_report import parse_panic_location
from pipeline_core.domain.test_results import identifiers_in

__all__ = ["observable_slice", "observable_source"]

_OPEN = "([{"
_CLOSE = ")]}"
_MATCH = {")": "(", "]": "[", "}": "{"}
#: A `path.rs:line:col` location as libtest and backtrace frames print it.
_LOCATION = re.compile(r"([^\s:()]+\.rs):(\d+):(\d+)")
#: Words a `let` pattern carries that bind nothing.
_PATTERN_NOISE = frozenset({"mut", "ref", "let", "_"})


def _masked(text: str) -> str:
    """`text` with comments and literals blanked, offsets and newlines kept."""
    mask = code_mask(text)
    return "".join(" " if m and ch != "\n" else ch
                   for ch, m in zip(text, mask, strict=False))


def _offset(text: str, line: int, col: int) -> int | None:
    """Character offset of a 1-based `line:col`, or None past the text."""
    if line < 1 or col < 1:
        return None
    start = 0
    for _ in range(line - 1):
        nl = text.find("\n", start)
        if nl < 0:
            return None
        start = nl + 1
    end = text.find("\n", start)
    end = len(text) if end < 0 else end
    off = start + col - 1
    return off if off <= end else None


def _match_forward(masked: str, open_idx: int, limit: int) -> int:
    """Index of the bracket closing the one at `open_idx`, or `limit`."""
    stack: list[str] = []
    for i in range(open_idx, limit):
        c = masked[i]
        if c in _OPEN:
            stack.append(c)
        elif c in _CLOSE:
            if not stack or stack[-1] != _MATCH[c]:
                return limit
            stack.pop()
            if not stack:
                return i
    return limit


def _enclosing_block(masked: str, body_start: int, offset: int) -> int:
    """Index just after the innermost `{` (from the fn body's own) that is
    still open at `offset`."""
    opens: list[tuple[str, int]] = [("{", body_start - 1)]
    for i in range(body_start, offset):
        c = masked[i]
        if c in _OPEN:
            opens.append((c, i))
        elif c in _CLOSE and len(opens) > 1 and opens[-1][0] == _MATCH[c]:
            opens.pop()
    return next(idx for kind, idx in reversed(opens) if kind == "{") + 1


def _statements(masked: str, start: int, end: int) -> list[tuple[int, int]]:
    """The `;`-separated statements of one block level in [start, end)."""
    out: list[tuple[int, int]] = []
    depth = 0
    seg = start
    for i in range(start, end):
        c = masked[i]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth = max(depth - 1, 0)
        elif c == ";" and depth == 0:
            out.append((seg, i + 1))
            seg = i + 1
    if masked[seg:end].strip():
        out.append((seg, end))
    return out


def _cut_end(masked: str, offset: int, stmt_end: int) -> int:
    """Where the panicking call ends: the identifier at `offset`, or the
    whole invocation when it is a macro."""
    i = offset
    if i < stmt_end and masked[i] == ".":
        i += 1
    j = i
    while j < stmt_end and (masked[j].isalnum() or masked[j] == "_"):
        j += 1
    if j == i:
        return min(offset + 1, stmt_end)
    k = j
    while k < stmt_end and masked[k].isspace():
        k += 1
    if k < stmt_end and masked[k] == "!":
        k += 1
        while k < stmt_end and masked[k].isspace():
            k += 1
        if k < stmt_end and masked[k] in _OPEN:
            return _match_forward(masked, k, stmt_end) + 1
    return j


def _lets(masked: str, body_start: int, body_end: int
          ) -> list[tuple[int, int, frozenset[str]]]:
    """Every `let` statement in the body: (start, end, names it binds)."""
    out: list[tuple[int, int, frozenset[str]]] = []
    for m in re.finditer(r"\blet\b", masked[body_start:body_end]):
        s = body_start + m.start()
        depth = 0
        eq = -1
        e = body_end
        for i in range(s, body_end):
            c = masked[i]
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE:
                if depth == 0:
                    e = i
                    break
                depth -= 1
            elif depth == 0 and c == ";":
                e = i + 1
                break
            elif (depth == 0 and eq < 0 and c == "="
                  and masked[i + 1:i + 2] not in ("=", ">")
                  and masked[i - 1:i] not in ("=", "!", "<", ">")):
                eq = i
        pattern = masked[s + 3:eq if eq >= 0 else e]
        pattern = pattern.split(":", 1)[0] if "(" not in pattern else pattern
        names = frozenset(identifiers_in(pattern)) - _PATTERN_NOISE
        out.append((s, e, names))
    return out


def observable_slice(file_text: str, body_start: int, body_end: int,
                     line: int, col: int) -> str | None:
    """The failure's observable inside one fn body, or None when `line:col`
    is not inside it. `body_start`/`body_end` bound the body between its
    braces (`rust_fn_headers.locate_fn`)."""
    offset = _offset(file_text, line, col)
    if offset is None or not body_start <= offset < body_end:
        return None
    masked = _masked(file_text)
    block = _enclosing_block(masked, body_start, offset)
    block_end = _match_forward(masked, block - 1, body_end + 1) \
        if block > body_start else body_end
    block_end = min(block_end, body_end)
    stmt = next(((s, e) for s, e in _statements(masked, block, block_end)
                 if s <= offset < e), (block, block_end))
    cut_end = _cut_end(masked, offset, stmt[1])
    parts = [file_text[stmt[0]:cut_end]]
    lets = _lets(masked, body_start, body_end)
    need = [(identifiers_in(masked[stmt[0]:cut_end]), stmt[0])]
    taken: set[int] = set()
    while need:
        names, before = need.pop()
        for name in sorted(names):
            binder = max(((s, e) for s, e, bound in lets
                          if name in bound and e <= before),
                         default=None)
            if binder is None or binder[0] in taken:
                continue
            taken.add(binder[0])
            parts.append(file_text[binder[0]:binder[1]])
            need.append((identifiers_in(masked[binder[0]:binder[1]]),
                         binder[0]))
    return "\n".join(p.strip() for p in parts if p.strip())


def _locations(evidence: str) -> Iterable[tuple[str, int, int]]:
    """The panic location first, then every location the text prints."""
    pf, pl, pc, _msg = parse_panic_location(evidence)
    if pf is not None and pl is not None and pc is not None:
        yield pf, pl, pc
    for m in _LOCATION.finditer(evidence):
        yield m.group(1), int(m.group(2)), int(m.group(3))


def _same_file(printed: str, rel: str) -> bool:
    a = printed.replace("\\", "/").lstrip("./")
    b = rel.replace("\\", "/").lstrip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def observable_source(file_text: str, test: str, evidence: str,
                      test_rel: str) -> str | None:
    """The observable of failing test `test` (bare or module-qualified name)
    whose fn lives in `file_text` at `test_rel`, from its own failure words
    `evidence`; None when no printed location falls inside that fn."""
    leaf = test.rsplit("::", 1)[-1]
    spans = [(b, e) for _sig, _s, b, e in locate_all_fns(file_text, leaf)]
    if not spans or not evidence:
        return None
    for path, line, col in _locations(evidence):
        if not _same_file(path, test_rel):
            continue
        for b, e in spans:
            got = observable_slice(file_text, b, e, line, col)
            if got is not None:
                return got
    return None
