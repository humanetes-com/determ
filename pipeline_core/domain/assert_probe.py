"""Observed values for a failing test's assertions — the probe rewrite.

A failing `assert!(s.contains("Stack backtrace"))` panics with
`assertion failed: s.contains("Stack backtrace")` and nothing else: the value
of `s` — the one fact a body author needs — is printed by nobody. Run
`3003-93e9a9bf` (2026-09-02, cluster `stable`) carried that panic line to six
prompts; the string being asserted on was `"backtrace test"` throughout and
appeared in none of them. `assert_eq!` prints `left` / `right`, so a test
written with `assert!` is blind where `assert_eq!` is not.

The probe derives, from the test's OWN source, a copy of the test in which
every `assert!` / `assert_eq!` / `assert_ne!` invocation is replaced by a
print of its operands: `assert!(s.contains(x))` prints `s` and `x`,
`assert!(a == b)` prints `a` and `b`, `assert_eq!(a, b)` prints both, any
other `assert!(expr)` prints `expr`. The printed line carries the test
name and the source line the assertion sat on, so the observation joins the
panic evidence that quotes the same line. The rewrite never touches the
oracle file: the runner writes the derived copy into a scratch crate.

Pure text: no IO, no Rust parser. The scanner knows enough Rust lexis to
skip string / char / raw-string literals and comments while matching
parentheses, which is what the macro argument split needs.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

PROBE_TAG = "<<PROBE"
_ASSERT_MACROS = ("assert_eq!", "assert_ne!", "assert!")
_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@dataclass(frozen=True, slots=True)
class ProbePoint:
    """One rewritten assertion: which test, which source line, which
    operand expressions are printed, and how many source lines the
    assertion spanned (the blamed-line retry restores that span)."""

    test: str
    line: int
    operands: tuple[str, ...]
    span_lines: int = 1


@dataclass(frozen=True, slots=True)
class Observation:
    test: str
    line: int
    expr: str
    value: str


def _skip_literal(text: str, i: int) -> int:
    """Index just past the literal or comment starting at `i`, or `i` when
    nothing literal starts there."""
    n = len(text)
    c = text[i]
    if text.startswith("//", i):
        j = text.find("\n", i)
        return n if j < 0 else j
    if text.startswith("/*", i):
        j = text.find("*/", i + 2)
        return n if j < 0 else j + 2
    if c == '"':
        j = i + 1
        while j < n:
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == '"':
                return j + 1
            j += 1
        return n
    if c == "r" and i + 1 < n and text[i + 1] in '#"':
        j = i + 1
        hashes = 0
        while j < n and text[j] == "#":
            hashes += 1
            j += 1
        if j < n and text[j] == '"':
            close = '"' + "#" * hashes
            k = text.find(close, j + 1)
            return n if k < 0 else k + len(close)
        return i
    if c == "b" and i + 1 < n and text[i + 1] == '"':
        return _skip_literal(text, i + 1)
    if c == "'":
        # A char literal is `'x'` or `'\…'`; anything else is a lifetime.
        if i + 1 < n and text[i + 1] == "\\":
            j = text.find("'", i + 2)
            return n if j < 0 else j + 1
        if i + 2 < n and text[i + 2] == "'":
            return i + 3
        return i + 1
    return i


def _match_paren(text: str, open_idx: int) -> int:
    """Index of the `)` matching the `(` at `open_idx`, or -1."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        j = _skip_literal(text, i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level(args: str, sep: str) -> list[str]:
    """Split `args` on `sep` (a single char or a two-char operator) at
    bracket depth 0, outside literals."""
    out: list[str] = []
    depth = 0
    i = 0
    start = 0
    n = len(args)
    while i < n:
        j = _skip_literal(args, i)
        if j != i:
            i = j
            continue
        c = args[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and args.startswith(sep, i):
            out.append(args[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    out.append(args[start:])
    return [a.strip() for a in out]


def _receiver_of_method_call(expr: str) -> tuple[str, list[str]] | None:
    """`recv.method(args)` at the END of `expr` → (recv, [args]); None when
    the expression is not shaped like a trailing method call."""
    e = expr.strip()
    if not e.endswith(")"):
        return None
    # The `(` that opens the group closing on the final char, found by a
    # forward scan that skips literals (a `)` inside a string argument is
    # not structure).
    depth = 0
    i = -1
    k = 0
    n = len(e)
    while k < n:
        j = _skip_literal(e, k)
        if j != k:
            k = j
            continue
        c = e[k]
        if c in "([{":
            if depth == 0:
                i = k
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0 and k != n - 1:
                i = -1
        k += 1
    if i <= 0 or e[i] != "(":
        return None
    head = e[:i].rstrip()
    j = len(head)
    while j > 0 and head[j - 1] in _IDENT_CHARS:
        j -= 1
    if j == len(head) or j == 0 or head[j - 1] != ".":
        return None
    recv = head[:j - 1].strip()
    if not recv:
        return None
    inner = e[i + 1:-1]
    args = [a for a in _split_top_level(inner, ",") if a]
    return recv, args


def _is_literal(expr: str) -> bool:
    """A string / raw-string / byte-string / char / number / bool literal —
    the test source already shows it, so printing it back is noise."""
    e = expr.strip()
    if not e:
        return True
    if e[0] in "\"'" or e.startswith(("r\"", "r#", "b\"", "br\"")):
        return True
    if e in ("true", "false"):
        return True
    return e.replace("_", "").replace(".", "").isdigit() or (
        e[0] == "-" and e[1:].replace("_", "").replace(".", "").isdigit())


def _condition_operands(cond: str) -> list[str]:
    """The value-bearing sub-expressions of one boolean condition."""
    for junction in ("||", "&&"):
        sides = _split_top_level(cond, junction)
        if len(sides) > 1 and all(sides):
            out: list[str] = []
            for side in sides:
                out.extend(_condition_operands(side))
            return out
    c = cond.strip()
    if c.startswith("!") and not c.startswith("!="):
        return _condition_operands(c[1:])
    if c.startswith("(") and _match_paren(c, 0) == len(c) - 1:
        return _condition_operands(c[1:-1])
    # Comparison operators: the two-char forms never occur inside generics;
    # a bare `<` / `>` does (`downcast_ref::<E>()`), so those are only read
    # with the spaces a binary operator is written with.
    for op in ("==", "!=", " <= ", " >= ", " < ", " > "):
        sides = _split_top_level(c, op)
        if len(sides) == 2 and all(sides):
            return [sides[0], sides[1]]
    call = _receiver_of_method_call(c)
    if call is not None:
        recv, args = call
        return [recv, *args]
    return [c]


def operands_of(macro: str, args_text: str) -> tuple[str, ...]:
    """The expressions a probe prints for one assertion, literals dropped
    and duplicates removed.

    `assert_eq!` / `assert_ne!`: the two compared operands. `assert!`: each
    `||` / `&&` side in turn; within a side both operands of a top-level
    comparison, else the receiver and arguments of a trailing method call
    (`s.contains(x)` → `s`, `x`; `v.is_empty()` → `v`), else the whole
    condition. Message arguments after the operands are dropped. The live
    shape this had to get right: `s.contains("Stack backtrace") ||
    s.contains("stack backtrace")` prints `s` once, not the boolean of the
    first call joined to `s`."""
    parts = [a for a in _split_top_level(args_text, ",") if a]
    if not parts:
        return ()
    cands = (parts[:2] if macro in ("assert_eq!", "assert_ne!")
             else _condition_operands(parts[0]))
    out: list[str] = []
    for c in cands:
        c = c.strip()
        if c and not _is_literal(c) and c not in out:
            out.append(c)
    return tuple(out)


def _raw_str(text: str) -> str:
    hashes = 1
    while ('"' + "#" * hashes) in text:
        hashes += 1
    h = "#" * hashes
    return f"r{h}\"{text}\"{h}"


def _probe_statement(test: str, line: int, operands: Iterable[str]) -> str:
    stmts = []
    for op in operands:
        stmts.append(
            f'eprintln!("{PROBE_TAG} {{}}:{{}}>> {{}} = {{:?}}", '
            f'{_raw_str(test)}, {line}, {_raw_str(op)}, &({op}));')
    return "{ " + " ".join(stmts) + " }"


def _fn_body_span(text: str, name: str) -> tuple[int, int] | None:
    """(body_start, body_end) of `fn <name>(`'s body braces, exclusive of
    the braces, or None."""
    needle = f"fn {name}("
    i = text.find(needle)
    while i != -1:
        if i == 0 or not (text[i - 1] in _IDENT_CHARS):
            break
        i = text.find(needle, i + 1)
    if i == -1:
        return None
    paren = text.find("(", i)
    close = _match_paren(text, paren)
    if close < 0:
        return None
    brace = text.find("{", close)
    if brace < 0:
        return None
    end = _match_paren(text, brace)
    if end < 0:
        return None
    return brace + 1, end


def rewrite_tests_for_probe(
    file_text: str, test_names: Collection[str],
) -> tuple[str, tuple[ProbePoint, ...]]:
    """`file_text` with every assertion inside the named tests replaced by a
    probe print; the probe points in source order. Tests not named, and
    everything outside the named bodies, are byte-identical. A test whose
    body cannot be located is left alone and yields no points."""
    text = file_text
    points: list[ProbePoint] = []
    # Rewrite from the last test backwards so earlier offsets stay valid.
    spans: list[tuple[int, int, str]] = []
    for name in test_names:
        span = _fn_body_span(text, name)
        if span is not None:
            spans.append((span[0], span[1], name))
    for start, end, name in sorted(spans, reverse=True):
        body = text[start:end]
        new_body, pts = _rewrite_body(body, name, text[:start].count("\n") + 1)
        text = text[:start] + new_body + text[end:]
        points.extend(pts)
    points.sort(key=lambda p: (p.test, p.line))
    return text, tuple(points)


def without_probe_points(
    file_text: str, test_names: Collection[str],
    excluded: Collection[tuple[str, int]],
) -> tuple[str, tuple[ProbePoint, ...]]:
    """Rewrite, then put back the original assertion text of each excluded
    point — the whole span, not the first line.

    The rewriter is line-stable for exactly this: a multi-line `assert_eq!`
    becomes a one-line print plus blank lines, so restoring only line
    `p.line` leaves `assert_eq!(` plus blanks plus a leftover semicolon,
    which does not compile. The blamed-line retry then contributes no
    observations for that file."""
    rewritten, points = rewrite_tests_for_probe(file_text, test_names)
    if not excluded:
        return rewritten, points
    bad = set(excluded)
    src_lines = file_text.splitlines(keepends=True)
    new_lines = rewritten.splitlines(keepends=True)
    kept: list[ProbePoint] = []
    for p in points:
        if (p.test, p.line) in bad:
            idx = p.line - 1
            for k in range(p.span_lines):
                j = idx + k
                if 0 <= j < len(new_lines) and j < len(src_lines):
                    new_lines[j] = src_lines[j]
        else:
            kept.append(p)
    return "".join(new_lines), tuple(kept)


def _rewrite_body(body: str, test: str, first_line: int) -> tuple[str, list[ProbePoint]]:
    out: list[str] = []
    points: list[ProbePoint] = []
    i = 0
    n = len(body)
    while i < n:
        j = _skip_literal(body, i)
        if j != i:
            out.append(body[i:j])
            i = j
            continue
        hit = None
        for macro in _ASSERT_MACROS:
            if body.startswith(macro, i) and (i == 0 or body[i - 1] not in _IDENT_CHARS):
                hit = macro
                break
        if hit is None:
            out.append(body[i])
            i += 1
            continue
        open_idx = i + len(hit)
        # Allow `assert! (`.
        k = open_idx
        while k < n and body[k] in " \t":
            k += 1
        if k >= n or body[k] != "(":
            out.append(body[i:k])
            i = k
            continue
        close = _match_paren(body, k)
        if close < 0:
            out.append(body[i:])
            break
        args_text = body[k + 1:close]
        ops = operands_of(hit, args_text)
        line = first_line + body[:i].count("\n")
        if ops:
            span_nl = body[i:close + 1].count("\n")
            points.append(ProbePoint(
                test=test, line=line, operands=ops, span_lines=span_nl + 1))
            # Keep every later line number where it was: the replacement is
            # one line, the assertion may have spanned several.
            out.append(_probe_statement(test, line, ops) + "\n" * span_nl)
        else:
            out.append(body[i:close + 1])
        i = close + 1
    return "".join(out), points


def parse_probe_output(output: str, *, value_cap: int = 600) -> dict[str, list[Observation]]:
    """`<<PROBE test:line>> expr = value` lines → observations per test,
    in print order; a value longer than `value_cap` is cut with an ellipsis.
    Everything else in the capture is ignored."""
    out: dict[str, list[Observation]] = {}
    for raw in output.splitlines():
        s = raw.strip()
        if not s.startswith(PROBE_TAG + " "):
            continue
        head_end = s.find(">>", len(PROBE_TAG))
        if head_end < 0:
            continue
        head = s[len(PROBE_TAG) + 1:head_end]
        test, _, line_s = head.rpartition(":")
        if not test or not line_s.isdigit():
            continue
        rest = s[head_end + 2:].strip()
        expr, sep, value = rest.partition(" = ")
        if not sep:
            continue
        if len(value) > value_cap:
            value = value[:value_cap].rstrip() + " …"
        out.setdefault(test, []).append(
            Observation(test=test, line=int(line_s), expr=expr.strip(), value=value))
    return out


def render_observations(obs: Iterable[Observation]) -> str:
    """The `observed:` lines a prompt appends beneath a test's panic text."""
    lines = [f"observed at line {o.line}: {o.expr} = {o.value}" for o in obs]
    return "\n".join(lines)
