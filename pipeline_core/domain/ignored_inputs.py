"""A body that ignores what it was given (pure domain).

A fill reply can retreat from the question instead of answering it: drop the
inputs the result has to be computed from and return something that compiles.
Run `486447-5207101c` (2026-10-07, cluster `stable`), fill 2: the file route's
first reply for `src/ensure.rs::render(msg, lhs, rhs)` formatted both operands
and failed `cannot find macro format`; the retry, told `do not resubmit the
same approach`, answered `let _ = (lhs, rhs); Error::msg(msg)`. It compiled, no
attributed test was observed passing, and the route kept it as `compiles, but
no attributed test was observed passing` — seventeen `ensure!` tests expected
`(2 vs 3)` from the two operands that body discards, and the unit was not aimed
again for four fills. The same reply text is stored from an earlier run.

The empty-body decline (`declined.py`) could not see it: the body is not
empty. What it says is the same thing — the model did not answer the unit — and
the signal is in the text: a declared input the body never reads, or reads
only inside a statement that discards it (`let _ = (lhs, rhs);`, `_ = x;`,
`drop(x);`).

It is NOT a refusal on its own. Correct code ignores inputs too: serde's
`IgnoredAny` visitor is `let _ = x; Ok(IgnoredAny)` by design, and any trait
impl may not need an argument. Whether ignoring an input is the right body is
a question only a test answers, so the rule is evidence-gated at the keep: a
body that ignores a declared input is kept only when a test attributed to
THAT body turned green in this keep. A sibling's test turning green in the
same reply says nothing about this body, and a test that was already green
before the keep is observed passing after any body. Without that, the keep
is a decline.

Generic: the names are the signature's, the discard forms are the language's
own statements, nothing names a crate, a test or a unit.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence, Set

from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.oracle_ledger import bare_test_name

__all__ = ["IgnoredInputs", "body_identity", "declined_by_ignored_inputs",
           "identity_name", "ignored_inputs", "ignored_inputs_feedback",
           "input_names", "is_declined", "vouched_bodies", "vouched_for_keep"]

# Separates a body name from its container in a vouch identity. A name
# with no container, and a name whose keep holds only one container, stays
# the bare name the locator-keyed keep already compares.
_BODY_SEP = "\x1f"

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SELF_PARAMS = frozenset({"self"})


def _matching(text: str, open_at: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def input_names(sig: str) -> tuple[str, ...]:
    """The named inputs a fn declares, in order: `name: T` and `mut name: T`.

    Receivers (`self` in any form) and names starting with `_` are not
    inputs this rule asks about — the declaration itself says an
    underscore name is unused. A destructuring pattern contributes the
    identifiers it binds."""
    text = " ".join(sig.split())
    m = re.search(r"\bfn\s+[A-Za-z_][A-Za-z0-9_]*", text)
    if m is None:
        return ()
    i = m.end()
    if i < len(text) and text[i] == "<":
        close = _matching(text, i, "<", ">")
        if close == -1:
            return ()
        i = close + 1
    open_at = text.find("(", i)
    if open_at == -1:
        return ()
    close = _matching(text, open_at, "(", ")")
    if close == -1:
        return ()
    names: list[str] = []
    for part in _split_top_level(text[open_at + 1:close]):
        if ":" not in part:
            continue
        pattern = part.split(":", 1)[0]
        for ident in _IDENT.findall(pattern):
            if ident in ("mut", "ref") or ident in _SELF_PARAMS:
                continue
            if ident.startswith("_") or ident in names:
                continue
            names.append(ident)
    return tuple(names)


def _code_only(body: str) -> str:
    """`body` with comments removed and every string or char literal
    replaced by the identifiers its `{name…}` format captures read, so an
    inline format argument (`"{lhs:?}"`) still counts as a read and a word
    inside a message does not."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if body.startswith("//", i):
            j = body.find("\n", i)
            i = n if j == -1 else j
            continue
        if body.startswith("/*", i):
            j = body.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue
        raw = re.match(r'(?:b?r)(#*)"', body[i:])
        if raw is not None:
            hashes = raw.group(1)
            end = body.find('"' + hashes, i + raw.end())
            lit = body[i + raw.end(): n if end == -1 else end]
            out.append(" " + " ".join(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)", lit)) + " ")
            i = n if end == -1 else end + 1 + len(hashes)
            continue
        if ch == '"':
            j = i + 1
            while j < n and body[j] != '"':
                j += 2 if body[j] == "\\" else 1
            lit = body[i + 1:j]
            out.append(" " + " ".join(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)", lit)) + " ")
            i = j + 1
            continue
        if ch == "'":
            char_lit = re.match(r"'(?:\\.[^']*|[^'\\])'", body[i:])
            if char_lit is not None:
                out.append(" ")
                i += char_lit.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_DISCARD_HEAD = re.compile(
    r"(?:\blet\s+_\s*=(?!=)|(?<![\w.:])_\s*=(?!=)"
    r"|\b(?:(?:core|std)\s*::\s*mem\s*::\s*)?drop\s*\()")
_PURE_TOKENS = re.compile(r"[\s&*(),]|\bmut\b|[A-Za-z_][A-Za-z0-9_]*")


def _discard_spans(code: str, inputs: frozenset[str]) -> list[tuple[int, int]]:
    """Spans of statements whose whole expression is the inputs themselves
    (`let _ = (a, b);`, `_ = a;`, `drop(a);`): nothing is computed from
    them. `let _ = write!(buf, "{:?}", a);` READS `a` and is not one."""
    spans: list[tuple[int, int]] = []
    for m in _DISCARD_HEAD.finditer(code):
        if m.group(0).rstrip().endswith("("):
            close = _matching(code, m.end() - 1, "(", ")")
            if close == -1:
                continue
            expr, end = code[m.end():close], close + 1
        else:
            semi = code.find(";", m.end())
            if semi == -1:
                continue
            expr, end = code[m.end():semi], semi + 1
        pos = 0
        pure = True
        for tok in _PURE_TOKENS.finditer(expr):
            if expr[pos:tok.start()].strip():
                pure = False
                break
            word = tok.group(0)
            if _IDENT.fullmatch(word) and word != "mut" and word not in inputs:
                pure = False
                break
            pos = tok.end()
        if pure and not expr[pos:].strip() and _IDENT.search(expr):
            spans.append((m.start(), end))
    return spans


def ignored_inputs(sig: str, body: str) -> tuple[str, ...]:
    """The declared inputs `body` never reads: absent from it, or present
    only inside a statement that discards them."""
    inputs = input_names(sig)
    if not inputs:
        return ()
    code = _code_only(body)
    spans = _discard_spans(code, frozenset(inputs))
    out: list[str] = []
    for name in inputs:
        reads = [m.start() for m in re.finditer(
            rf"(?<![\w.]){re.escape(name)}\b(?!\s*::)(?!\s*!)", code)]
        if all(any(a <= p < b for a, b in spans) for p in reads):
            out.append(name)
    return tuple(out)


IgnoredInputs = dict[str, tuple[str, ...]]


def body_identity(name: str, container: str = "") -> str:
    """The keep's identity of one body. A container is part of it only when
    the caller is distinguishing homonyms; an empty container is the name."""
    key = container_key(container) if container else ""
    return f"{name}{_BODY_SEP}{key}" if key else name


def identity_name(identity: str) -> str:
    """The function name of a :func:`body_identity`."""
    return identity.split(_BODY_SEP, 1)[0]


def identity_container(identity: str) -> str:
    """The container key of a :func:`body_identity`, "" when the identity
    is the bare name (the keep did not distinguish homonyms)."""
    return identity.split(_BODY_SEP, 1)[1] if _BODY_SEP in identity else ""


def _split_body(item: tuple[str, ...]) -> tuple[str, str, str, str]:
    if len(item) >= 4:
        return item[0], item[1], item[2], item[3]
    return item[0], item[1], item[2], ""


def _distinguished(names: Iterable[tuple[str, str]]) -> set[str]:
    """Names this keep holds under two or more containers. Those are the
    homonyms a bare name cannot vouch separately."""
    got: dict[str, set[str]] = {}
    for name, container in names:
        if container:
            got.setdefault(name, set()).add(container_key(container))
    return {name for name, keys in got.items() if len(keys) >= 2}


def vouched_bodies(
    attributed: Mapping[str, Collection[str]], turned_green: Collection[str],
) -> frozenset[str]:
    """The body names, of `attributed` (name -> the tests attributed to that
    body), with at least one own test in `turned_green` — the tests this keep
    turned from failing to passing. Names compare in bare form, so a
    `binary::test` reading and a bare attribution agree."""
    green = {bare_test_name(t) for t in turned_green}
    return frozenset(
        name for name, tests in attributed.items()
        if any(bare_test_name(t) in green for t in tests))


def vouched_for_keep(
    attributed: Mapping[tuple[str, str], Collection[str]],
    turned_green: Collection[str],
) -> frozenset[str]:
    """The bodies, of `attributed` (`(name, container)` -> the tests
    attributed to that body), with at least one own test in `turned_green`.

    A name the keep holds under one container is the bare name, which is
    what :func:`vouched_bodies` returns. A name it holds under two or more
    is :func:`body_identity` of each, so one body's green test does not
    vouch the other body of that name."""
    green = {bare_test_name(t) for t in turned_green}
    split = _distinguished(attributed)
    out: set[str] = set()
    for (name, container), tests in attributed.items():
        if not any(bare_test_name(t) in green for t in tests):
            continue
        out.add(body_identity(name, container) if name in split else name)
    return frozenset(out)


def is_declined(name: str, container: str,
                declined: Mapping[str, object]) -> bool:
    """True when `declined` (the dict :func:`declined_by_ignored_inputs`
    returned) names this body. A distinguished homonym matches its
    identity; every other body matches its name."""
    if body_identity(name, container) in declined:
        return True
    return name in declined


def declined_by_ignored_inputs(
    bodies: Iterable[tuple[str, ...]], *, vouched: Set[str],
) -> IgnoredInputs:
    """Of `(name, sig, body)` or `(name, sig, body, container)` tuples a
    keep would land, the ones that are a decline: they ignore a declared
    input and are not in `vouched`. `vouched` is the names
    (:func:`vouched_bodies`) or the identities (:func:`vouched_for_keep`)
    whose own attributed tests this keep turned green. A vouched body
    declines nothing — the test, not the text, says whether ignoring an
    input is right — and it vouches for no other body of the same keep.
    Two bodies of one name under different containers are keyed apart, so
    one's vouch is not the other's."""
    parsed = [_split_body(b) for b in bodies]
    split = _distinguished((name, container) for name, _, _, container in parsed)
    out: IgnoredInputs = {}
    for name, sig, body, container in parsed:
        ident = body_identity(name, container) if name in split else name
        if ident in vouched:
            continue
        ignored = ignored_inputs(sig, body)
        if ignored:
            out[ident] = ignored
    return out


def ignored_inputs_feedback(unit: str, ignored: Sequence[str]) -> str:
    """The error text the next turn carries for a declined unit, shaped as
    the compiler error the attempt slot holds."""
    names = ", ".join(f"`{n}`" for n in ignored)
    return (
        f"error: your body for `{unit}` was not kept: it never uses its "
        f"input(s) {names} (absent from the body, or only discarded with "
        "`let _ = …` / `drop(…)`), and no test attributed to it turned green "
        "in this attempt, so nothing shows that ignoring them is right. The "
        "result has to be computed from what the function is given. Write the "
        "body "
        "that uses every input the signature declares; if a compile error "
        "stopped the approach that used them, fix that error instead of "
        "removing the inputs.")
